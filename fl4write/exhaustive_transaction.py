"""Durable publication transactions for evidence-verified exhaustive rounds."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from .exhaustive_publication import PublicationError, inspect_owned, ledger_body, publish_owned


@contextmanager
def _publication_adapter(adapter, config):
    """Bind a late, repository-scoped App token without changing process env."""
    from .forges import _is_github_base, adapter_for

    binding = next(b for b in config.forges.values() if b.role == "primary")
    if not _is_github_base(binding.api_base):
        yield adapter
        return
    from .appauth import get_repository_token, verified_app_login

    token = get_repository_token(config.repo, config.bot_login)
    bound = adapter_for(binding)
    bound.bot_login = config.bot_login
    headers = {**bound._headers(), "Authorization": "Bearer " + token}
    original_call, original_headers = bound._call, bound._headers
    bound._headers = lambda: dict(headers)
    def call(method, path, payload=None, **kwargs):
        if method == "GET" and path == "/user":
            # Installation tokens have no user endpoint. The signing App is
            # verified live, and this token was minted for that exact App.
            return {"login": verified_app_login()}
        return original_call(method, path, payload, **kwargs)
    bound._call = call
    try:
        yield bound
    finally:
        headers.clear()
        token = ""
        bound._headers, bound._call = original_headers, original_call


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def publish_round(repo: Path, state_path: Path, adapter, config, issue: int,
                  state: dict, certification: bool = False) -> None:
    """Persist an exact candidate and body before the first forge write."""
    from .exhaustive import Deferred, _atomic_json

    pending = state_path.parent / "publication-pending.json"
    if pending.exists():
        raise Deferred("an earlier publication must be replayed first")
    body = ledger_body(state, config.repo, certification)
    candidate = state_path.parent / "publication-candidate.json"
    _atomic_json(candidate, state)
    _atomic_json(pending, {
        "version": 1, "repo": config.repo, "issue": issue,
        "config_sha256": _digest(config.model_dump(mode="json")),
        "candidate_sha256": _digest(state), "certification": certification,
        "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    })
    replay_publication(repo, state_path, adapter, config, issue)


def replay_publication(repo: Path, state_path: Path, adapter, config, issue: int) -> bool:
    """Retry precisely the staged write; advance canonical state only after proof."""
    from .exhaustive import Deferred, _atomic_json, _git, _load_state

    pending = state_path.parent / "publication-pending.json"
    if not pending.exists():
        return False
    try:
        request = json.loads(pending.read_bytes())
        candidate = state_path.parent / "publication-candidate.json"
        from .exhaustive import _identity

        if not candidate.exists():
            # NEW-3 (2026-09-16): a pending transaction whose candidate is
            # gone used to fail the candidate_sha256 check on EVERY run —
            # a permanent RC-2 deadlock. Archive it beside the obsolete
            # transactions and proceed with a fresh round instead.
            _atomic_json(state_path.parent / ("publication-orphaned-"
                                              + str(request.get("candidate_sha256", "unknown"))[:12]
                                              + ".json"), request)
            pending.unlink()
            return False
        state = _load_state(candidate, _identity(repo)[1])
        expected = {
            "version", "repo", "issue", "config_sha256", "candidate_sha256",
            "certification", "body", "body_sha256",
        }
        if (set(request) != expected or request["version"] != 1
                or request["repo"] != config.repo or request["issue"] != issue
                or request["config_sha256"] != _digest(config.model_dump(mode="json"))
                or request["candidate_sha256"] != _digest(state)
                or not isinstance(request["certification"], bool)
                or request["body"] != ledger_body(state, config.repo, request["certification"])
                or request["body_sha256"] != hashlib.sha256(request["body"].encode()).hexdigest()):
            raise Deferred("publication transaction identity or evidence changed")
        if _git(repo, "rev-parse", "HEAD") != state["head"]:
            with _publication_adapter(adapter, config) as authenticated:
                observed = inspect_owned(authenticated, config.repo, issue, config.bot_login)
            outcome = "old_body_observed" if observed["body"] == request["body"] else "old_body_not_observed"
            _atomic_json(state_path.parent / ("publication-obsolete-" + request["candidate_sha256"] + ".json"),
                         {"status": outcome, "previous_head": state["head"],
                          "body_sha256": request["body_sha256"], "issue": issue})
            _atomic_json(state_path.parent / "certification.json",
                         {"status": "invalidated", "previous_sha": state["head"], "reason": "HEAD moved"})
            pending.unlink()
            return False
        with _publication_adapter(adapter, config) as authenticated:
            publish_owned(authenticated, config.repo, issue, config.bot_login, request["body"])
        _atomic_json(state_path, state)
        if request["certification"]:
            _atomic_json(state_path.parent / "certification.json", {
                "status": "forge_published", "sha": state["certified_sha"], "ledger_issue": issue,
            })
        pending.unlink()
        return True
    except Deferred:
        raise
    except Exception as exc:  # NEW-1 (2026-09-16): appauth validation errors
        # (RuntimeError on a 404 installation, scope/identity failures) and
        # any other publication-path failure must defer with the staged
        # write retained — an uncaught traceback here crash-looped every
        # later invocation (fail-closed, but unrecoverable without manual
        # file surgery). Same contract as the tuple below, one boundary.
        if not isinstance(exc, (PublicationError, OSError, ValueError,
                                TypeError, KeyError, RuntimeError)):
            raise
        raise Deferred("publication unresolved; exact staged write retained") from exc
