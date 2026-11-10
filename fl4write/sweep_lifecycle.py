"""The merged-PR sweeps' implicit state, made explicit.

Retro audit, per merged PR (persisted across retro_seen, retro_parked and
the retro_defer:<number>:<sha10> counters):

    PENDING ─reviewed─▶ DONE
       │
       └─deferred─▶ PENDING (tries 1..2) ─3rd deferral─▶ PARKED(until)
                                                          │
                                       window expires ────┘ (retried, re-parked
                                                             while the cause lasts)

The sweep itself is ACTIVE until the lookback window holds nothing left to
audit, then COMPLETE (retro_complete) — terminal.

Post-merge shadow belt: one entry per merged PR the shadow run handled,
either "<sha>" (shadow-reviewed) or "<sha>:dep" (shadow dependency skip).
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from .state import parse_number_key

RETRO_DEFER_LIMIT = 3
RETRO_PARK_SECONDS = 86400


class RetroPhase(str, Enum):
    ACTIVE = "active"
    COMPLETE = "complete"


def retro_phase(st: dict[str, Any]) -> RetroPhase:
    return RetroPhase.COMPLETE if st.get("retro_complete") is True else RetroPhase.ACTIVE


def retro_finish(st: dict[str, Any]) -> None:
    """ACTIVE -> COMPLETE: nothing left in the lookback window."""
    st["retro_complete"] = True


class RetroPR(str, Enum):
    PENDING = "pending"
    PARKED = "parked"        # deferred too often; waiting out its window
    DONE = "done"            # reviewed this cycle or an earlier one


def _defer_key(number: int, head_sha: str) -> str:
    return f"retro_defer:{number}:{head_sha[:10]}"


def load_parked(st: dict[str, Any]) -> dict[str, int]:
    """The park map, normalized: {"<number>": expiry_epoch}."""
    raw = st.get("retro_parked", {})
    if not isinstance(raw, dict):
        return {}
    return {str(number): expiry for k, v in raw.items()
            if (number := parse_number_key(k)) is not None
            and (expiry := parse_number_key(v)) is not None}


def park_split(parked: dict[str, int], now: int | None = None) -> tuple[set[int], set[int]]:
    """(still parked, park expired: retry now)."""
    now = int(time.time()) if now is None else now
    active = {int(k) for k, v in parked.items() if v > now}
    expired = {int(k) for k, v in parked.items() if v <= now}
    return active, expired


def load_seen(st: dict[str, Any]) -> set[int]:
    raw = st.get("retro_seen")
    if not isinstance(raw, dict):
        return set()
    return {number for k in raw if (number := parse_number_key(k)) is not None}


def store_seen(st: dict[str, Any], seen: set[int]) -> None:
    st["retro_seen"] = {int(n): True for n in seen}


def retro_done(st: dict[str, Any], parked: dict[str, int], number: int, head_sha: str) -> None:
    """PENDING/PARKED -> DONE: the deferral counter and any park die with it."""
    st.pop(_defer_key(number, head_sha), None)
    parked.pop(str(number), None)
    st["retro_parked"] = parked


def retro_deferred(st: dict[str, Any], parked: dict[str, int], number: int, head_sha: str,
                   now: int | None = None) -> tuple[int, bool]:
    """A deferral. Returns (tries, parked_now). The RETRO_DEFER_LIMIT-th
    consecutive deferral at one head parks the PR for RETRO_PARK_SECONDS."""
    key = _defer_key(number, head_sha)
    tries = int(st.get(key, 0) or 0) + 1
    if tries < RETRO_DEFER_LIMIT:
        st[key] = tries
        return tries, False
    st.pop(key, None)
    now = int(time.time()) if now is None else now
    parked[str(number)] = now + RETRO_PARK_SECONDS
    st["retro_parked"] = parked
    return tries, True


# ---------------------------------------------------------------- post-merge belt
def pm_belt_entry(head_sha: str, *, dependency: bool = False) -> str:
    return f"{head_sha}:dep" if dependency else head_sha


def pm_belt_hit(entry: object, head_sha: str) -> bool:
    """Has the shadow run already handled this PR at this head?"""
    return isinstance(entry, str) and entry in (head_sha, f"{head_sha}:dep")
