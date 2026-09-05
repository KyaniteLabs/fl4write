"""Safe publication of the exhaustive audit ledger to an owned forge issue."""

from __future__ import annotations

import json
import re
from typing import Any

from .forges import ForgeAdapter


class PublicationError(RuntimeError):
    """Publication could not be proven safe or complete."""


_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MARKER_TEMPLATE = "<!-- fl4write:exhaustive-ledger:v1 repo={repo} -->"
_GREEN_REQUIRED = 3


def _canonical_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise PublicationError("invalid canonical repository")
    return repo


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicationError(f"invalid numeric {field}")
    return value


def _hash(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PublicationError(f"invalid {field}")
    return value


def _round_dto(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get("green"), bool):
        raise PublicationError("invalid ledger round")
    dto: dict[str, Any] = {
        "round": _integer(row.get("round"), "round"),
        "green": row["green"],
        "reviewed_head": _hash(row.get("reviewed_head"), _COMMIT, "reviewed head"),
        "finding_count": _integer(row.get("finding_count", 0), "finding count"),
    }
    tested = row.get("tested_head")
    if tested is not None:
        dto["tested_head"] = _hash(tested, _COMMIT, "tested head")
    junit = row.get("junit_sha256")
    if junit is not None:
        dto["junit_sha256"] = _hash(junit, _SHA256, "JUnit hash")
    usage = row.get("model_usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise PublicationError("invalid model usage")
        dto["model_usage"] = {
            "calls": _integer(usage.get("calls"), "model calls"),
            "reserved_output_tokens": _integer(
                usage.get("reserved_output_tokens"), "reserved output tokens"
            ),
        }
    return dto


def _certification_sha(state: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    certified = _hash(state.get("certified_sha"), _COMMIT, "certified head")
    if (
        state.get("consecutive_green") != _GREEN_REQUIRED
        or state.get("head") != certified
        or state.get("green_sha") != certified
        or len(rows) < _GREEN_REQUIRED
    ):
        raise PublicationError("certification state is not three-round green")
    for row in rows[-_GREEN_REQUIRED:]:
        if (
            not row["green"]
            or row["reviewed_head"] != certified
            or row.get("tested_head") != certified
            or row["finding_count"] != 0
            or "junit_sha256" not in row
        ):
            raise PublicationError("certification rounds do not match the certified head")
    return certified


def ledger_body(state: dict, repo: str, certification: bool = False) -> str:
    """Build a deterministic, strictly allowlisted public audit ledger."""
    canonical = _canonical_repo(repo)
    if not isinstance(certification, bool):
        raise PublicationError("invalid certification flag")
    if not isinstance(state, dict) or not isinstance(state.get("ledger"), list):
        raise PublicationError("invalid exhaustive state")
    rows = [_round_dto(row) for row in state["ledger"]]
    round_number = _integer(state.get("round"), "state round")
    consecutive = _integer(state.get("consecutive_green"), "consecutive green")
    if round_number != len(rows) or any(row["round"] != n for n, row in enumerate(rows, 1)):
        raise PublicationError("ledger rounds are not contiguous")

    public: dict[str, Any] = {
        "certified": bool(certification),
        "consecutive_green": consecutive,
        "round": round_number,
        "rounds": rows,
    }
    heading = "FL4WRITE exhaustive round ledger"
    if certification:
        public["certified_head"] = _certification_sha(state, rows)
        heading = "FL4WRITE exhaustive certification"
    marker = _MARKER_TEMPLATE.format(repo=canonical)
    payload = json.dumps(public, indent=2, sort_keys=True, separators=(",", ": "))
    return f"{marker}\n## {heading}\n\n```json\n{payload}\n```\n"


def _login(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    login = value.get("login") or value.get("username")
    return login if isinstance(login, str) and login else None


def _issue_author(issue: dict[str, Any]) -> str | None:
    return _login(issue.get("user")) or _login(issue.get("author"))


def _repo_name(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    full_name = payload.get("full_name") or payload.get("fullName")
    if isinstance(full_name, str) and full_name:
        return full_name
    owner = _login(payload.get("owner"))
    name = payload.get("name")
    return f"{owner}/{name}" if owner and isinstance(name, str) and name else None


def publish_owned(adapter: ForgeAdapter, repo: str, issue: int, bot_login: str, body: str) -> None:
    """PATCH an exact owned ledger body after live guards, then verify it."""
    canonical = _canonical_repo(repo)
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        raise PublicationError("invalid issue number")
    if not isinstance(bot_login, str) or not bot_login or bot_login != adapter.bot_login:
        raise PublicationError("configured bot identity mismatch")
    marker = _MARKER_TEMPLATE.format(repo=canonical)
    if not isinstance(body, str) or body.count(marker) != 1 or not body.startswith(marker + "\n"):
        raise PublicationError("publication body lacks the repository-bound marker")

    try:
        identity = adapter._call("GET", "/user")
        repository = adapter._call("GET", f"/repos/{canonical}")
        current = adapter._call("GET", f"/repos/{canonical}/issues/{issue}")
    except Exception:
        raise PublicationError("forge ownership preflight failed") from None

    if _login(identity) != bot_login:
        raise PublicationError("authenticated identity mismatch")
    if _repo_name(repository) != canonical:
        raise PublicationError("repository identity mismatch")
    if not isinstance(current, dict) or current.get("number") != issue:
        raise PublicationError("issue identity mismatch")
    if _issue_author(current) != bot_login:
        raise PublicationError("issue is not owned by the configured bot")
    old_body = current.get("body")
    if not isinstance(old_body, str) or marker not in old_body:
        raise PublicationError("issue is not an owned exhaustive ledger")

    try:
        adapter._call("PATCH", f"/repos/{canonical}/issues/{issue}", {"body": body})
        observed = adapter._call("GET", f"/repos/{canonical}/issues/{issue}")
    except Exception:
        raise PublicationError("forge write outcome is uncertain; retry the identical body") from None
    if not isinstance(observed, dict) or observed.get("number") != issue or observed.get("body") != body:
        raise PublicationError("forge readback mismatch; retry the identical body")
    if _issue_author(observed) != bot_login:
        raise PublicationError("forge readback ownership mismatch")
