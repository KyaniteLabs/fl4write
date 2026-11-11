"""Protected-merge policy — guard/auth/merge changes never auto-merge.

Product decision (card t_5a7e1b77, 2026-11-11): a pull request that touches
FL4WRITE's guard, auth or merge code may NEVER be merged by FL4WRITE's own
auto-merge scan, even with every check completed green. Such a change needs
the second key (``kyanite-factory``) plus a Security exact-head review,
performed by the external merge gate — never by this process.

Authority is deliberately OUTSIDE this repository: the platform-owned JSON
file named by ``FL4WRITE_PROTECTED_PATHS``. FL4WRITE only READS that file and
never writes it, so a PR cannot widen its own protection. If the variable is
unset, the file is missing / unreadable / malformed, EVERY merge is refused
(fail closed). A policy with an empty or non-list ``protected_paths`` is
malformed for the same reason: an empty list would silently protect nothing.

Policy document (JSON object):

    {"protected_paths": [
        "fl4write/appauth.py",
        "fl4write/forges.py",
        "fl4write/executor.py",
        "fl4write/gatekeeper.py",
        "fl4write/telemetry.py",
        "fl4write/scrub.py",
        "fl4write/writeguard.py",           # E1's outbound guard
        ".github/workflows/**",
        "fl4write/mergepolicy.py",          # this classifier
        "fl4write/fixlane.py",              # merge_own_pr, THE gate
        "fl4write/exhaustive_fix.py"        # second merge client (SEC-E2-1)
    ]}

``protected_paths`` is a non-empty list of POSIX glob patterns matched against
each changed file name with case-sensitive ``fnmatch`` semantics. A changed
``*.fl4write.yaml`` is protected when its diff adds or removes a ``repo:``
binding (the target-repo rule); other edits in that file are unaffected.
Adding, deleting or renaming the config file itself is always protected
(conservative: the set of target repos can change without a visible binding
diff).

The classifier decides ONLY from the PR's changed-file list — never from the
title, the commit message or the author (card AC, "not from the title, the
commit message or the author").
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import telemetry

log = logging.getLogger("fl4write.mergepolicy")

POLICY_ENV = "FL4WRITE_PROTECTED_PATHS"
REPO_CONFIG_SUFFIX = ".fl4write.yaml"

# A YAML line that binds (or unbinds) a repo. Conservative on purpose: an
# indented / quoted key is still a binding; refusing a harmless edit here is
# safe, allowing a target-repo swap is not.
_REPO_KEY_RE = re.compile(r"""^\s*["']?repo["']?\s*:""")

# Reasons carried by a refusal. ``protected_path`` is the card's required
# reason for a protected changed file; the others are fail-closed conditions.
REASON_PROTECTED = "protected_path"
REASON_POLICY = "policy_unavailable"
REASON_FILES = "files_unavailable"


class PolicyUnavailable(RuntimeError):
    """The platform-owned policy cannot be read or trusted."""


@dataclass(frozen=True)
class MergeDecision:
    """One classifier verdict for one PR, bound to the exact head it judged."""

    allowed: bool
    reason: str
    repo: str
    pr: int
    head: str
    matched_paths: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.allowed:
            return "allowed"
        paths = ", ".join(self.matched_paths) if self.matched_paths else "(no file evidence)"
        return f"{self.reason}: {paths}"


def load_protected_paths() -> tuple[str, ...]:
    """Read the platform-owned policy. READ-ONLY: this function never writes,
    creates or chmods the file — FL4WRITE must not be able to widen its own
    list. Every failure raises ``PolicyUnavailable`` so the caller fails
    closed rather than guessing."""
    raw = os.environ.get(POLICY_ENV, "").strip()
    if not raw:
        raise PolicyUnavailable(f"{POLICY_ENV} is not set")
    try:
        path = Path(raw)
        with path.open("r", encoding="utf-8") as fh:  # read-only by construction
            doc = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyUnavailable(f"cannot read policy {raw!r}: {exc}") from exc
    if not isinstance(doc, dict):
        raise PolicyUnavailable(f"policy {raw!r} is not a JSON object")
    patterns = doc.get("protected_paths")
    if not isinstance(patterns, list) or not patterns:
        raise PolicyUnavailable(f"policy {raw!r} has no non-empty protected_paths list")
    cleaned: list[str] = []
    for item in patterns:
        if not isinstance(item, str):
            raise PolicyUnavailable(f"policy {raw!r} has a non-string protected path: {item!r}")
        pattern = item.strip()
        if not pattern or "\x00" in pattern:
            raise PolicyUnavailable(f"policy {raw!r} has an empty/unsafe protected path")
        if pattern.startswith("./"):
            pattern = pattern[2:]
        cleaned.append(pattern)
    return tuple(cleaned)


def _matches(path: str, pattern: str) -> bool:
    """Case-sensitive glob match: GitHub paths are case-sensitive and ``*``
    in fnmatch also spans ``/``, so ``.github/workflows/**`` covers nested
    workflow files."""
    return fnmatch.fnmatchcase(path, pattern)


def _is_repo_config(path: str) -> bool:
    return path.rsplit("/", 1)[-1].endswith(REPO_CONFIG_SUFFIX)


def _changes_target_repo(record: dict) -> bool:
    """True when this ``*.fl4write.yaml`` change adds or removes a target repo.

    Adding / deleting / renaming the file can add or remove a config without a
    visible binding hunk, so it is refused conservatively. For a content edit
    the PR's own diff hunks decide: a ``repo:`` line added or removed is a
    target change. A missing patch (binary / truncated / too large) cannot be
    judged and therefore fails closed.
    """
    if record.get("status") in {"added", "removed", "renamed"}:
        return True
    patch = record.get("patch")
    if not isinstance(patch, str) or not patch:
        return True
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if not line or line[0] not in "+-":
            continue
        if _REPO_KEY_RE.match(line[1:]):
            return True
    return False


def decide(*, repo: str, pr: int, head: str,
           changed_files: list[dict] | None) -> MergeDecision:
    """Classify one PR from its changed-file list.

    ``changed_files`` is the paginated ``GET /pulls/{n}/files`` payload, or
    ``None`` when the list could not be read completely. Both an unreadable
    list and an unreadable policy yield ``allowed=False`` (fail closed).
    """
    if not isinstance(changed_files, list) or not changed_files:
        return MergeDecision(False, REASON_FILES, repo, pr, head)
    try:
        patterns = load_protected_paths()
    except PolicyUnavailable as exc:
        log.warning("protected-merge policy unavailable — ALL merges refused: %s", exc)
        return MergeDecision(False, REASON_POLICY, repo, pr, head)
    matched: list[str] = []
    for record in changed_files:
        if not isinstance(record, dict):
            return MergeDecision(False, REASON_FILES, repo, pr, head)
        name = record.get("filename")
        if not isinstance(name, str) or not name:
            # Without every file name the list is not evidence — fail closed.
            return MergeDecision(False, REASON_FILES, repo, pr, head)
        previous = record.get("previous_filename")
        # A rename can move a protected module OUT of its protected path; the
        # old name is evidence too, so both ends of the rename are matched.
        names = [name]
        if isinstance(previous, str) and previous:
            names.append(previous)
        hits = [candidate for candidate in names
                if any(_matches(candidate, pattern) for pattern in patterns)]
        if hits:
            matched.extend(hits)
        elif any(_is_repo_config(candidate) for candidate in names):
            if _changes_target_repo(record):
                matched.append(name)
    if matched:
        # dict.fromkeys preserves the PR's file order while de-duplicating.
        return MergeDecision(False, REASON_PROTECTED, repo, pr, head, tuple(dict.fromkeys(matched)))
    return MergeDecision(True, "", repo, pr, head)


def record_block(decision: MergeDecision) -> None:
    """Emit exactly ONE ``merge_blocked`` event for a refused merge.

    Called by the merge gate itself, so a refusal can never be silent and a
    single refusal never produces duplicate events.
    """
    telemetry.emit(
        "merge_blocked",
        repo=decision.repo,
        pr=decision.pr,
        head=decision.head,
        matched_paths=list(decision.matched_paths),
        reason=decision.reason,
    )
