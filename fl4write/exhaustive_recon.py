"""Durable, identity-bound progress for one exhaustive recon round."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ReconProgress:
    """Retain successful chunk responses and charge every attempted call before dispatch."""

    def __init__(self, request, sources, ledger):
        from .config import ModelRoute
        from .exhaustive import Deferred

        # One lock covers EVERY mutation of self.value — attempt charges and
        # entry accepts alike — so the concurrent dispatch windows can never
        # interleave a check-then-increment or an append-then-save with
        # another thread's.
        self.lock = threading.Lock()
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
        # Cascade budget rule: every attempted call — screen or deep — reserves
        # the largest configured output window, so the checkpoint invariant
        # (attempts * reservation <= max_tokens) stays exact for both stages.
        # With no screen route this equals the primary route's max_tokens.
        self.attempt_tokens = max(
            ModelRoute.model_validate(request[name]).max_tokens
            for name in ("route", "screen_route") if request.get(name)
        )
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
                    and value["attempts"] * self.attempt_tokens <= request["max_tokens"]
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

    def cached(self, index, prompt, screened):
        """Return the checkpointed entry covering chunk `index`, else None.
        `screened` selects the required entry shape (cascade vs plain recon);
        any mismatch raises the same Deferred a serial resume would."""
        from .exhaustive import Deferred

        key = _digest(prompt)
        if index < len(self.value["entries"]):
            entry = self.value["entries"][index]
            shape = {"prompt_sha256", "flagged", "response"} if screened else {"prompt_sha256", "response"}
            if not isinstance(entry, dict) or set(entry) != shape or entry["prompt_sha256"] != key:
                raise Deferred("recon checkpoint chunk differs from current request")
            if screened and (not isinstance(entry["flagged"], bool)
                             or (entry["flagged"] and not isinstance(entry["response"], dict))
                             or (not entry["flagged"] and entry["response"] is not None)):
                raise Deferred("recon checkpoint chunk differs from current request")
            return entry
        return None

    def admits(self, request, route, attempt):
        """Would a call charged as the (attempt+1)-th pass both ceilings?
        Shared by the serial loop and the concurrent windows so the two
        paths refuse at exactly the same charges. Cascade arithmetic: the
        reservation per attempt is attempt_tokens (the largest configured
        route window), not the deep route's alone."""
        return not (attempt >= request["max_calls"]
                    or (attempt + 1) * self.attempt_tokens > request["max_tokens"])

    def charge(self):
        """Persist one attempted call BEFORE its dispatch (crash-exact spend)."""
        with self.lock:
            self.value["attempts"] += 1
            self.save()

    def _charged_call(self, request, route, prompt, call, system, envelope):
        """Charge one attempt (call slot + reserved output tokens) before dispatch."""
        from .analyzer import extract_json
        from .exhaustive import Deferred

        with self.lock:
            if (self.value["attempts"] >= request["max_calls"]
                    or (self.value["attempts"] + 1) * self.attempt_tokens > request["max_tokens"]):
                raise Deferred("model call or reserved output-token budget exhausted before full coverage")
            self.value["attempts"] += 1
            self.save()
        try:
            return extract_json(call(route, prompt, "file", system), envelope_key=envelope)
        except Exception as exc:
            raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc

    def response(self, index, prompt, request, route, call, system):
        from .exhaustive import Deferred

        key = _digest(prompt)
        if index < len(self.value["entries"]):
            entry = self.value["entries"][index]
            if not isinstance(entry, dict) or set(entry) != {"prompt_sha256", "response"} or entry["prompt_sha256"] != key:
                raise Deferred("recon checkpoint chunk differs from current request")
            return entry["response"], True
        return self._charged_call(request, route, prompt, call, system, "findings"), False

    def cascade(self, index, prompt, request, route, screen_route, call):
        """Screen-then-deep dispatch for one chunk: the cheap route flags
        suspect chunks and only flagged chunks reach the primary route. Both
        passes are charged — screening never rides free on the call budget."""
        from .exhaustive import Deferred, _RECON_SYSTEM, _SCREEN_SYSTEM

        key = _digest(prompt)
        if index < len(self.value["entries"]):
            entry = self.value["entries"][index]
            if (not isinstance(entry, dict)
                    or set(entry) != {"prompt_sha256", "flagged", "response"}
                    or entry["prompt_sha256"] != key
                    or not isinstance(entry["flagged"], bool)
                    or (entry["flagged"] and not isinstance(entry["response"], dict))
                    or (not entry["flagged"] and entry["response"] is not None)):
                raise Deferred("recon checkpoint chunk differs from current request")
            return entry["flagged"], (entry["response"] if entry["flagged"] else None), True
        verdict = self._charged_call(request, screen_route, prompt, call, _SCREEN_SYSTEM, "flag")
        if set(verdict) != {"flag"} or not isinstance(verdict["flag"], bool):
            raise Deferred("model unavailable or returned unusable output: screen verdict is not a boolean flag")
        if not verdict["flag"]:
            return False, None, False
        return True, self._charged_call(request, route, prompt, call, _RECON_SYSTEM, "findings"), False

    def accept(self, prompt, response):
        with self.lock:
            self.value["entries"].append({"prompt_sha256": _digest(prompt), "response": response})
            self.save()

    def accept_screened(self, prompt, flagged, response):
        with self.lock:
            self.value["entries"].append(
                {"prompt_sha256": _digest(prompt), "flagged": flagged, "response": response}
            )
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


def _invoke(call, route, prompt, system, envelope):
    """Run one model call + envelope parse inside the pool thread. Kept at
    module level (not a closure) so submit() binds its arguments eagerly and
    the transport call itself executes in the worker thread."""
    from .analyzer import extract_json

    return extract_json(call(route, prompt, "file", system), envelope_key=envelope)


def _concurrent_pass(sources, request, route, screen_route, ledger, progress, caller, fake_queue):
    """Parallel recon with serial semantics: prompts dispatch concurrently,
    but every charge persists before its dispatch, entries accept strictly in
    coverage order (an entry at index i must answer chunk i), and the first
    failing chunk in that order surfaces the same Deferred as the serial loop.
    With a screen route each window is two-phase — every chunk's cheap screen
    leg dispatches first, then flagged chunks' deep legs join the same pool in
    coverage order — so both legs keep the charge-before-dispatch contract and
    the fake-fixture ordinals stay deterministic."""
    from concurrent.futures import ThreadPoolExecutor

    from .exhaustive import Deferred, _RECON_SYSTEM, _SCREEN_SYSTEM, _recon_prompts, _validated

    screened = screen_route is not None
    tasks = [
        (path, source, digest, start, end, prompt)
        for path, source, digest in sources
        for start, end, prompt in _recon_prompts(source, request["chunk_chars"], ledger, path, route, screen_route)
    ]
    findings, coverage = [], []
    # Covered prefix replays exactly like the serial loop: no calls, no charges.
    for index, (path, source, digest, start, end, prompt) in enumerate(tasks):
        entry = progress.cached(index, prompt, screened)
        if entry is None:
            break
        if not screened or entry["flagged"]:
            findings += _validated(entry["response"], path, start, end, source)
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

            def _fake(ordinal):
                return _fake_call(fake_queue, ordinal) if fake_queue is not None else caller

            if not screened:
                futures = []
                for offset in range(admissible):
                    # Reserve-then-dispatch per task, in coverage order — the same
                    # charge-before-call contract as the serial loop and the model
                    # proxy (reservations are not refunded on failure).
                    progress.charge()
                    path, source, digest, start, end, prompt = window[offset]
                    futures.append(pool.submit(
                        _invoke, _fake(fresh_dispatched + offset), route, prompt, _RECON_SYSTEM, "findings"))
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
                continue

            # Cascade window, phase one: dispatch every admissible screen leg.
            screens = []
            for offset in range(admissible):
                progress.charge()
                path, source, digest, start, end, prompt = window[offset]
                screens.append(pool.submit(
                    _invoke, _fake(fresh_dispatched + offset), screen_route, prompt, _SCREEN_SYSTEM, "flag"))
            fresh_dispatched += admissible
            # Phase two: consume the screens in coverage order. Clean verdicts
            # ahead of the first flagged chunk accept immediately (nothing can
            # precede them); flagged ones reserve-then-dispatch their deep leg
            # into the same pool.
            deeps, rows = [], [None] * admissible
            for offset, future in enumerate(screens):
                path, source, digest, start, end, prompt = window[offset]
                try:
                    verdict = future.result()
                except Deferred:
                    raise
                except Exception as exc:
                    raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc
                if set(verdict) != {"flag"} or not isinstance(verdict["flag"], bool):
                    raise Deferred("model unavailable or returned unusable output: screen verdict is not a boolean flag")
                if not verdict["flag"]:
                    if not deeps:
                        rows[offset] = True
                        progress.accept_screened(prompt, False, None)
                        coverage.append({"path": path, "sha256": digest, "bytes": len(source.encode()),
                                         "start_line": start, "end_line": end})
                    continue
                if not progress.admits(request, route, progress.value["attempts"]):
                    raise Deferred("model call or reserved output-token budget exhausted before full coverage")
                progress.charge()
                deeps.append((offset, pool.submit(
                    _invoke, _fake(fresh_dispatched), route, prompt, _RECON_SYSTEM, "findings")))
                fresh_dispatched += 1
            # Phase three: consume the deep legs in coverage order — findings
            # and entries are held, never written here, so the window's accepts
            # stay strictly in index order.
            for offset, future in deeps:
                try:
                    rows[offset] = future.result()
                except Deferred:
                    raise
                except Exception as exc:
                    raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc
            for offset in range(admissible):
                path, source, digest, start, end, prompt = window[offset]
                value = rows[offset]
                if value is True:
                    continue  # already accepted ahead of the first flagged chunk
                if value is not None:
                    findings += _validated(value, path, start, end, source)
                    progress.accept_screened(prompt, True, value)
                else:
                    progress.accept_screened(prompt, False, None)
                coverage.append({"path": path, "sha256": digest, "bytes": len(source.encode()),
                                 "start_line": start, "end_line": end})
            window_start += admissible
    return findings, coverage


def worker(request_path, caller=None):
    from .config import ModelRoute
    from .exhaustive import Deferred, _RECON_SYSTEM, _atomic_json, _recon_prompts, _text_sources, _validated

    request = json.loads(request_path.read_text(encoding="utf-8"))
    route = ModelRoute.model_validate(request["route"])
    screen_route = ModelRoute.model_validate(request["screen_route"]) if request.get("screen_route") else None
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
            sources, request, route, screen_route, ledger, progress,
            None if fake_queue is not None else call,
            fake_queue)
    else:
        findings, coverage = [], []
        for path, source, digest in sources:
            for start, end, prompt in _recon_prompts(source, request["chunk_chars"], ledger, path, route, screen_route):
                if screen_route is None:
                    value, cached = progress.response(len(coverage), prompt, request, route, call, _RECON_SYSTEM)
                    findings += _validated(value, path, start, end, source)
                    if not cached:
                        progress.accept(prompt, value)
                else:
                    flagged, value, cached = progress.cascade(len(coverage), prompt, request, route, screen_route, call)
                    if flagged:
                        findings += _validated(value, path, start, end, source)
                    if not cached:
                        progress.accept_screened(prompt, flagged, value)
                coverage.append({"path": path, "sha256": digest, "bytes": len(source.encode()),
                                 "start_line": start, "end_line": end})
    if len(coverage) != len(progress.value["entries"]):
        raise Deferred("recon checkpoint contains excess coverage")
    from .exhaustive_adjudication import apply_endpoint_carveout

    findings, carved = apply_endpoint_carveout(findings)
    _atomic_json(Path(request["result"]), {
        "findings": findings, "coverage": coverage, "calls": progress.value["attempts"],
        # attempt_tokens (the largest configured route window) — not
        # route.max_tokens — so a screen model with a larger window is not
        # under-reported in the result artifact; carved_findings records the
        # standing carve-out rows (log-what-you-dropped).
        "reserved_output_tokens": progress.value["attempts"] * progress.attempt_tokens,
        "carved_findings": carved,
    })
    return 0
