"""Durable round reservations shared by recon, repair and optional live tests."""
from contextlib import contextmanager
import json
import os

from .model_proxy import ModelProxy, ProxyError


class RoundBudget:
    def __init__(self, path, request_sha, max_calls, max_tokens):
        self.path = path
        self.identity = {"version": 1, "request_sha256": request_sha,
                         "max_calls": max_calls, "max_output_tokens": max_tokens}
        self.usage = {"calls": 0, "reserved_output_tokens": 0}
        if path.exists():
            try:
                value = json.loads(path.read_bytes())
                if (not isinstance(value, dict) or set(value) != {"identity", "usage"}
                        or value["identity"] != self.identity
                        or not isinstance(value["usage"], dict)
                        or set(value["usage"]) != set(self.usage)
                        or any(type(v) is not int or v < 0 for v in value["usage"].values())):
                    raise ValueError("invalid budget")
                self.usage.update(value["usage"])
            except (OSError, ValueError, TypeError) as exc:
                raise ProxyError("round budget cannot be recovered") from exc

    def reserve(self, tokens):
        # Called under the proxy lock; the enclosing loop owns the repository lock.
        from .exhaustive import _atomic_json
        usage = {"calls": self.usage["calls"] + 1,
                 "reserved_output_tokens": self.usage["reserved_output_tokens"] + tokens}
        if (usage["calls"] > self.identity["max_calls"]
                or usage["reserved_output_tokens"] > self.identity["max_output_tokens"]):
            raise ProxyError("whole-round model budget exhausted")
        _atomic_json(self.path, {"identity": self.identity, "usage": usage})
        self.usage.update(usage)


@contextmanager
def round_transport(args, config, budget_path, request_sha):
    """All real model calls use the same host proxy; fake fixtures stay offline."""
    if getattr(args, "_fake_responses", None):
        yield None
        return
    budget = RoundBudget(budget_path, request_sha, args.max_model_calls, args.max_output_tokens)
    previous = os.environ.get("FL4WRITE_MODEL_PROXY_SOCKET")
    try:
        with ModelProxy(config.model, max_calls=args.max_model_calls,
                        max_output_tokens=args.max_output_tokens, reserve=budget.reserve) as proxy:
            args._model_proxy = proxy
            os.environ["FL4WRITE_MODEL_PROXY_SOCKET"] = str(proxy.socket_path)
            yield budget
    finally:
        args._model_proxy = None
        if previous is None:
            os.environ.pop("FL4WRITE_MODEL_PROXY_SOCKET", None)
        else:
            os.environ["FL4WRITE_MODEL_PROXY_SOCKET"] = previous
