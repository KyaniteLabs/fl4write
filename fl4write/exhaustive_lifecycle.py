"""The exhaustive loop's green-streak / certification machine, made explicit.

Per repository HEAD the loop is in one phase, derived from the persisted
state (round state format unchanged):

    ROUND_PENDING   recon recorded for a round whose tests have not finished
    NO_STREAK       no green round at this HEAD yet
    GREEN_STREAK    1..GREEN_REQUIRED-1 consecutive green rounds at this HEAD
    READY           GREEN_REQUIRED green rounds at this HEAD, not yet certified
    CERTIFIED       the ledger publication certified this HEAD

    NO_STREAK ─green round─▶ GREEN_STREAK ─…─▶ READY ─published─▶ CERTIFIED
        ▲                        │               │                  │
        └──── finding, red round, HEAD moved: break_streak ◀────────┘

The invariant the validator enforces — a certification implies a full green
streak at exactly that HEAD — is kept by construction: every streak break
also clears the certification.

certification.json carries one of the CertStatus values.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class LoopPhase(str, Enum):
    ROUND_PENDING = "round-pending"
    NO_STREAK = "no-streak"
    GREEN_STREAK = "green-streak"
    READY = "ready"
    CERTIFIED = "certified"


class CertStatus(str, Enum):
    """The status line of certification.json."""

    LOCAL_EVIDENCE_GREEN = "local_evidence_green"                  # streak done; no ledger issue
    FORGE_CERTIFICATION_PENDING = "forge_certification_pending"    # streak done; publishing
    FORGE_PUBLISHED = "forge_published"                            # certified
    INVALIDATED = "invalidated"                                    # HEAD moved off a certification
    HUMAN_ACTION_REQUIRED = "human_action_required"                # the loop stopped on a refusal


def loop_phase(state: dict[str, Any], head: str, required: int) -> LoopPhase:
    if state.get("pending_round"):
        return LoopPhase.ROUND_PENDING
    if state.get("certified_sha") and state["certified_sha"] == head:
        return LoopPhase.CERTIFIED
    streak = state.get("consecutive_green", 0) if state.get("green_sha") == head else 0
    if streak >= required:
        return LoopPhase.READY
    return LoopPhase.GREEN_STREAK if streak else LoopPhase.NO_STREAK


def break_streak(state: dict[str, Any]) -> None:
    """Any phase -> NO_STREAK: a finding, a red round or a moved HEAD. The
    certification goes with the streak it depended on."""
    state["consecutive_green"] = 0
    state["green_sha"] = None
    state["certified_sha"] = None


def extend_streak(state: dict[str, Any], head: str) -> None:
    """One more green round at this HEAD (the caller has broken any streak
    held for another HEAD first)."""
    if state.get("green_sha") not in (None, head):
        raise ValueError("a green streak cannot span two HEADs")
    state["consecutive_green"] = int(state.get("consecutive_green", 0)) + 1
    state["green_sha"] = head


def certify(state: dict[str, Any], head: str, required: int) -> None:
    """READY -> CERTIFIED."""
    if state.get("green_sha") != head or state.get("consecutive_green") != required:
        raise ValueError("only a full green streak at this HEAD can be certified")
    state["certified_sha"] = head


class MergePhase(str, Enum):
    """The atomic-fix receipt's merge phase. A receipt without a phase never
    reached the merge request; only these two can be recovered on retry."""

    MERGING = "merging"   # merge requested; outcome not yet proven
    MERGED = "merged"     # merge proven against the default branch


# a tuple, not a set: persisted phases are read back as plain strings
RECOVERABLE_PHASES = (MergePhase.MERGING, MergePhase.MERGED)


class FixOutcome(str, Enum):
    """status of an atomic exhaustive fix result."""

    PENDING = "pending"   # retry can still settle it
    BLOCKED = "blocked"   # a guard refused (identity, checks, scope)
    MERGED = "merged"
    ERROR = "error"


class ObsoletePublication(str, Enum):
    """status of a publication made obsolete by a moved HEAD."""

    OLD_BODY_OBSERVED = "old_body_observed"
    OLD_BODY_NOT_OBSERVED = "old_body_not_observed"
