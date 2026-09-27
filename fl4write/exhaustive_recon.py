"""Durable, identity-bound progress for one exhaustive recon round."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ReconProgress:
    """Retain successful chunk responses and charge every attempted call before dispatch."""

    def __init__(self, request, sources, ledger):
        from .exhaustive import Deferred

        self.path = Path(request["checkpoint"]) if request.get("checkpoint") else None
        identity = {
            "version": 1,
            "request": {k: v for k, v in request.items()
                        if k not in {"tree", "ledger", "result", "checkpoint", "fake_responses"}},
            "ledger": ledger,
            "sources": [(path, digest) for path, _, digest in sources],
            "implementation": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.glob("*.py"))
            },
            "fake": hashlib.sha256(Path(request["fake_responses"]).read_bytes()).hexdigest()
            if request.get("fake_responses") else None,
        }
        self.identity = _digest(identity)
        self.value = {"identity": self.identity, "attempts": 0, "entries": []}
        if self.path and self.path.exists():
            try:
                saved = json.loads(self.path.read_bytes())
                value = saved["value"]
                valid = (
                    set(saved) == {"value", "sha256"} and saved["sha256"] == _digest(value)
                    and set(value) == {"identity", "attempts", "entries"}
                    and value["identity"] == self.identity
                    and type(value["attempts"]) is int and value["attempts"] >= 0
                    and isinstance(value["entries"], list)
                    and len(value["entries"]) <= value["attempts"] <= request["max_calls"]
                    and value["attempts"] * request["route"]["max_tokens"] <= request["max_tokens"]
                )
                if not valid:
                    raise ValueError("invalid checkpoint")
                self.value = value
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise Deferred("recon checkpoint invalid or identity changed; retained for recovery") from exc

    def save(self):
        from .exhaustive import _atomic_json

        if self.path:
            _atomic_json(self.path, {"value": self.value, "sha256": _digest(self.value)})

    def cached(self, index, prompt):
        """Return the checkpointed response covering chunk `index`, else None."""
        from .exhaustive import Deferred

        key = _digest(prompt)
        if index < len(self.value["entries"]):
            entry = self.value["entries"][index]
            if not isinstance(entry, dict) or set(entry) != {"prompt_sha256", "response"} or entry["prompt_sha256"] != key:
                raise Deferred("recon checkpoint chunk differs from current request")
            return entry["response"]
        return None

    def admits(self, request, route, attempt):
        """Would a call charged as the (attempt+1)-th pass both ceilings?
        Shared by the serial loop and the concurrent windows so the two
        paths refuse at exactly the same charges."""
        return not (attempt >= request["max_calls"]
                    or (attempt + 1) * route.max_tokens > request["max_tokens"])

    def charge(self):
        """Persist one attempted call BEFORE its dispatch (crash-exact spend)."""
        self.value["attempts"] += 1
        self.save()

    def response(self, index, prompt, request, route, call, system):
        from .analyzer import extract_json
        from .exhaustive import Deferred

        cached = self.cached(index, prompt)
        if cached is not None:
            return cached, True
        if not self.admits(request, route, self.value["attempts"]):
            raise Deferred("model call or reserved output-token budget exhausted before full coverage")
        self.charge()
        try:
            return extract_json(call(route, prompt, "file", system), envelope_key="findings"), False
        except Exception as exc:
            raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc

    def accept(self, prompt, response):
        self.value["entries"].append({"prompt_sha256": _digest(prompt), "response": response})
        self.save()


def _fake_call(queue, ordinal):
    """Deterministic per-task fake caller: the n-th fresh call of this run
    consumes the n-th queued fixture — thread dispatch order must not pick
    which response a chunk sees."""
    def call(*_args):
        if ordinal >= len(queue):
            raise RuntimeError("fake response queue exhausted")
        return json.dumps(queue[ordinal])
    return call


def _invoke(call, route, prompt, system):
    """Run one model call + envelope parse inside the pool thread. Kept at
    module level (not a closure) so submit() binds its arguments eagerly and
    the transport call itself executes in the worker thread."""
    from .analyzer import extract_json

    return extract_json(call(route, prompt, "file", system), envelope_key="findings")


def _concurrent_pass(sources, request, route, ledger, progress, caller, fake_queue):
    """Parallel recon with serial semantics: prompts dispatch concurrently,
    but every charge persists before its dispatch, entries accept strictly in
    coverage order (an entry at index i must answer chunk i), and the first
    failing chunk in that order surfaces the same Deferred as the serial loop."""
    from concurrent.futures import ThreadPoolExecutor

    from .exhaustive import Deferred, _RECON_SYSTEM, _recon_prompts, _validated

    tasks = [
        (path, source, digest, start, end, prompt)
        for path, source, digest in sources
        for start, end, prompt in _recon_prompts(source, request["chunk_chars"], ledger, path, route)
    ]
    findings, coverage = [], []
    # Covered prefix replays exactly like the serial loop: no calls, no charges.
    for index, (path, source, digest, start, end, prompt) in enumerate(tasks):
        value = progress.cached(index, prompt)
        if value is None:
            break
        findings += _validated(value, path, start, end, source)
        coverage.append({"path": path, "sha256": digest, "bytes": len(source.encode()),
                         "start_line": start, "end_line": end})
    fresh = len(coverage)
    fresh_dispatched = 0
    with ThreadPoolExecutor(max_workers=request["concurrency"]) as pool:
        window_start = fresh
        while window_start < len(tasks):
            window = tasks[window_start:window_start + request["concurrency"]]
            admissible = 0
            base = progress.value["attempts"]
            for offset in range(len(window)):
                if not progress.admits(request, route, base + offset):
                    break
                admissible += 1
            if admissible == 0:
                raise Deferred("model call or reserved output-token budget exhausted before full coverage")
            futures = []
            for offset in range(admissible):
                # Reserve-then-dispatch per task, in coverage order — the same
                # charge-before-call contract as the serial loop and the model
                # proxy (reservations are not refunded on failure).
                progress.charge()
                path, source, digest, start, end, prompt = window[offset]
                if fake_queue is not None:
                    call = _fake_call(fake_queue, fresh_dispatched + offset)
                else:
                    call = caller
                futures.append(pool.submit(_invoke, call, route, prompt, _RECON_SYSTEM))
            for offset, future in enumerate(futures):
                path, source, digest, start, end, prompt = window[offset]
                try:
                    value = future.result()
                except Exception as exc:
                    raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc
                findings += _validated(value, path, start, end, source)
                progress.accept(prompt, value)
                coverage.append({"path": path, "sha256": digest, "bytes": len(source.encode()),
                                 "start_line": start, "end_line": end})
            fresh_dispatched += admissible
            window_start += admissible
            if admissible < len(window):
                # The window clipped on a ceiling: the admissible chunks above
                # completed and accepted, the remainder is refused — the same
                # point the serial loop would raise.
                raise Deferred("model call or reserved output-token budget exhausted before full coverage")
    return findings, coverage


def worker(request_path, caller=None):
    from .config import ModelRoute
    from .exhaustive import Deferred, _RECON_SYSTEM, _atomic_json, _recon_prompts, _text_sources, _validated

    request = json.loads(request_path.read_text(encoding="utf-8"))
    route = ModelRoute.model_validate(request["route"])
    sources = list(_text_sources(Path(request["tree"])))
    ledger = json.loads(Path(request["ledger"]).read_bytes())
    progress = ReconProgress(request, sources, ledger)
    fake_queue = None
    if request.get("fake_responses"):
        fake_queue = list(json.loads(Path(request["fake_responses"]).read_bytes()))[progress.value["attempts"]:]

        def call(*_args):
            if not fake_queue:
                raise RuntimeError("fake response queue exhausted")
            return json.dumps(fake_queue.pop(0))
    else:
        if caller is None:
            from .analyzer import _call_model

            caller = _call_model
        call = caller
    if request.get("concurrency", 1) > 1:
        findings, coverage = _concurrent_pass(
            sources, request, route, ledger, progress,
            None if fake_queue is not None else call,
            fake_queue)
    else:
        findings, coverage = [], []
        for path, source, digest in sources:
            for start, end, prompt in _recon_prompts(source, request["chunk_chars"], ledger, path, route):
                value, cached = progress.response(len(coverage), prompt, request, route, call, _RECON_SYSTEM)
                findings += _validated(value, path, start, end, source)
                if not cached:
                    progress.accept(prompt, value)
                coverage.append({"path": path, "sha256": digest, "bytes": len(source.encode()),
                                 "start_line": start, "end_line": end})
    if len(coverage) != len(progress.value["entries"]):
        raise Deferred("recon checkpoint contains excess coverage")
    _atomic_json(Path(request["result"]), {
        "findings": findings, "coverage": coverage, "calls": progress.value["attempts"],
        "reserved_output_tokens": progress.value["attempts"] * route.max_tokens,
    })
    return 0
