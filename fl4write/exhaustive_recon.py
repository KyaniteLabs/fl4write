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

    def response(self, index, prompt, request, route, call, system):
        from .analyzer import extract_json
        from .exhaustive import Deferred

        key = _digest(prompt)
        if index < len(self.value["entries"]):
            entry = self.value["entries"][index]
            if not isinstance(entry, dict) or set(entry) != {"prompt_sha256", "response"} or entry["prompt_sha256"] != key:
                raise Deferred("recon checkpoint chunk differs from current request")
            return entry["response"], True
        if (self.value["attempts"] >= request["max_calls"]
                or (self.value["attempts"] + 1) * route.max_tokens > request["max_tokens"]):
            raise Deferred("model call or reserved output-token budget exhausted before full coverage")
        self.value["attempts"] += 1
        self.save()
        try:
            return extract_json(call(route, prompt, "file", system), envelope_key="findings"), False
        except Exception as exc:
            raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc

    def accept(self, prompt, response):
        self.value["entries"].append({"prompt_sha256": _digest(prompt), "response": response})
        self.save()


def worker(request_path, caller=None):
    from .config import ModelRoute
    from .exhaustive import Deferred, _RECON_SYSTEM, _atomic_json, _recon_prompts, _text_sources, _validated

    request = json.loads(request_path.read_text(encoding="utf-8"))
    route = ModelRoute.model_validate(request["route"])
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
