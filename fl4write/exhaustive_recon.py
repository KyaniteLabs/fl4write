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
        from .config import ModelRoute
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

    def _charged_call(self, request, route, prompt, call, system, envelope):
        """Charge one attempt (call slot + reserved output tokens) before dispatch."""
        from .analyzer import extract_json
        from .exhaustive import Deferred

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
        self.value["entries"].append({"prompt_sha256": _digest(prompt), "response": response})
        self.save()

    def accept_screened(self, prompt, flagged, response):
        self.value["entries"].append(
            {"prompt_sha256": _digest(prompt), "flagged": flagged, "response": response}
        )
        self.save()


def worker(request_path, caller=None):
    from .config import ModelRoute
    from .exhaustive import Deferred, _RECON_SYSTEM, _atomic_json, _recon_prompts, _text_sources, _validated

    request = json.loads(request_path.read_text(encoding="utf-8"))
    route = ModelRoute.model_validate(request["route"])
    screen_route = ModelRoute.model_validate(request["screen_route"]) if request.get("screen_route") else None
    sources = list(_text_sources(Path(request["tree"])))
    ledger = json.loads(Path(request["ledger"]).read_bytes())
    progress = ReconProgress(request, sources, ledger)
    if request.get("fake_responses"):
        queue = list(json.loads(Path(request["fake_responses"]).read_bytes()))[progress.value["attempts"]:]

        def call(*_args):
            if not queue:
                raise RuntimeError("fake response queue exhausted")
            return json.dumps(queue.pop(0))
    else:
        if caller is None:
            from .analyzer import _call_model

            caller = _call_model
        call = caller
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
