"""The omnisweep lifecycle, made explicit.

The sweep's phase used to be implied by a dozen loose state keys
(omni_complete, omni_published, omni_report_version, omni_clean_published,
omni_scope_pending, omni_cursor, omni_findings, ...) written from many
places in engine.py. This module names the phases, derives the current one
from those keys (the persisted format is unchanged), and is the only writer
of the phase keys, so every transition is one named function.

    IDLE ──scan──▶ SCANNING ──last file──▶ COMPLETE ──report lands──▶ PUBLISHED
      ▲                                       │  ▲                      │
      └──────────── reset (new HEAD, tree change, truncation) ◀─────────┘
                                              │  └─ report failed ──────┘
    COMPLETE/PUBLISHED with no scope fingerprint ──▶ SCOPE_PENDING ──▶ reset

Per-finding fix state lives here too (OmniFix): PENDING ─attempt─▶ ATTEMPTED
(back to PENDING on a transient error), PENDING ─file gone─▶ STALE (final).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

REPORT_VERSION = 2

# Every key that belongs to ONE sweep of ONE scope. A reset removes them all;
# omni_issue survives so the next audit edits the same issue in place.
SWEEP_KEYS = (
    "omni_complete", "omni_published", "omni_clean_published", "omni_report_version",
    "omni_pub_fail", "omni_cursor", "omni_head", "omni_fp", "omni_findings",
    "omni_next_id", "omni_total", "omni_scanned_total", "omni_file_fails",
    "omni_unscannable", "omni_unfetchable", "omni_truncated_seen", "omni_scope_pending",
)


class OmniPhase(str, Enum):
    IDLE = "idle"                    # nothing scanned for the current scope
    SCANNING = "scanning"            # cursor/findings progress, not complete
    SCOPE_PENDING = "scope-pending"  # legacy completion of an unknown scope
    COMPLETE = "complete"            # every file visited; report not current
    PUBLISHED = "published"          # complete and the current report landed


def report_current(st: dict[str, Any]) -> bool:
    """The completed report is published in the current format. A clean
    sweep that still owns an issue must have published its clean update,
    or the old findings would stay visible."""
    if not st.get("omni_published") or st.get("omni_report_version") != REPORT_VERSION:
        return False
    if st.get("omni_issue") and not st.get("omni_findings") and not st.get("omni_clean_published"):
        return False
    return True


def phase(st: dict[str, Any]) -> OmniPhase:
    if st.get("omni_scope_pending"):
        return OmniPhase.SCOPE_PENDING
    if st.get("omni_complete"):
        return OmniPhase.PUBLISHED if report_current(st) else OmniPhase.COMPLETE
    if st.get("omni_cursor") or st.get("omni_findings"):
        return OmniPhase.SCANNING
    return OmniPhase.IDLE


# ---------------------------------------------------------------- transitions
def reset(st: dict[str, Any]) -> None:
    """Any phase -> IDLE: a new HEAD, a tree change mid-sweep, or progress
    made on a truncated listing. Nothing from the old scope may survive."""
    for key in SWEEP_KEYS:
        st.pop(key, None)


def complete(st: dict[str, Any], total: int) -> None:
    """SCANNING/IDLE -> COMPLETE: every file of the scope was visited."""
    st["omni_complete"] = True
    st["omni_total"] = total


def published(st: dict[str, Any], *, clean: bool) -> None:
    """COMPLETE -> PUBLISHED: the final report landed (or a clean sweep with
    no issue needed nothing published)."""
    st["omni_published"] = True
    st["omni_report_version"] = REPORT_VERSION
    st["omni_clean_published"] = clean
    st.pop("omni_pub_fail", None)


def publish_failed(st: dict[str, Any]) -> int:
    """The report did not land; the phase stays COMPLETE and retries.
    Returns the consecutive failure count."""
    st["omni_published"] = False
    fails = int(st.get("omni_pub_fail", 0) or 0) + 1
    st["omni_pub_fail"] = fails
    return fails


def forget_issue(st: dict[str, Any]) -> None:
    """A dead issue id is dropped so the next publication creates a fresh one."""
    st.pop("omni_issue", None)
    st["omni_pub_fail"] = 0
    st["omni_published"] = False


def schedule_fresh_scope(st: dict[str, Any]) -> None:
    """COMPLETE/PUBLISHED with no scope fingerprint -> SCOPE_PENDING: the
    old report and receipt stay until a trusted listing restarts the sweep."""
    st["omni_complete"] = False
    st["omni_scope_pending"] = True


# ---------------------------------------------------------------- per finding
class OmniFix(str, Enum):
    PENDING = "pending"
    ATTEMPTED = "attempted"
    STALE = "stale"          # the file left HEAD: never attempted again


def fix_state(row: dict[str, Any]) -> OmniFix:
    if row.get("fix_stale"):
        return OmniFix.STALE
    if row.get("fix_attempted"):
        return OmniFix.ATTEMPTED
    return OmniFix.PENDING


_FIX_MOVES = {
    (OmniFix.PENDING, OmniFix.ATTEMPTED),
    (OmniFix.ATTEMPTED, OmniFix.PENDING),   # transient error: retry later
    (OmniFix.PENDING, OmniFix.STALE),
}


def set_fix_state(row: dict[str, Any], new: OmniFix) -> None:
    old = fix_state(row)
    if (old, new) not in _FIX_MOVES:
        raise ValueError(f"omni finding fix state {old.value} -> {new.value} is not allowed")
    if new is OmniFix.STALE:
        row["fix_stale"] = True
    else:
        row["fix_attempted"] = new is OmniFix.ATTEMPTED
