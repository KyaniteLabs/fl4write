"""Durable publication transactions for evidence-verified exhaustive rounds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .exhaustive_publication import PublicationError, ledger_body, publish_owned


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
        saved = json.loads(candidate.read_bytes())
        state = _load_state(candidate, saved["repo_identity"])
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
            raise Deferred("HEAD moved before publication; staged certification cannot be published")
        publish_owned(adapter, config.repo, issue, config.bot_login, request["body"])
        _atomic_json(state_path, state)
        if request["certification"]:
            _atomic_json(state_path.parent / "certification.json", {
                "status": "forge_published", "sha": state["certified_sha"], "ledger_issue": issue,
            })
        pending.unlink()
        return True
    except (PublicationError, OSError, ValueError, TypeError, KeyError) as exc:
        raise Deferred("publication unresolved; exact staged write retained") from exc
