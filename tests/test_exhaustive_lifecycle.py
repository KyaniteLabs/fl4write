"""The exhaustive loop's streak/certification machine and the vocabularies
of its fix receipts, publication records and sandbox recovery record
(fl4write/exhaustive_lifecycle.py, exhaustive_sandbox.CLEANUP_REQUIRED)."""

from __future__ import annotations

import json

import pytest

from fl4write import exhaustive_lifecycle as loop
from fl4write import exhaustive_sandbox
from fl4write.exhaustive_lifecycle import (RECOVERABLE_PHASES, CertStatus, FixOutcome, LoopPhase,
                                           MergePhase, ObsoletePublication)

H = "a" * 40
H2 = "b" * 40
REQ = 2


def _st(**over):
    st = {"consecutive_green": 0, "green_sha": None, "certified_sha": None, "pending_round": None}
    st.update(over)
    return st


@pytest.mark.parametrize("st,head,expect", [
    (_st(), H, LoopPhase.NO_STREAK),
    (_st(pending_round={"reviewed_head": H}), H, LoopPhase.ROUND_PENDING),
    (_st(consecutive_green=1, green_sha=H), H, LoopPhase.GREEN_STREAK),
    (_st(consecutive_green=1, green_sha=H), H2, LoopPhase.NO_STREAK),     # streak is per HEAD
    (_st(consecutive_green=2, green_sha=H), H, LoopPhase.READY),
    (_st(consecutive_green=2, green_sha=H, certified_sha=H), H, LoopPhase.CERTIFIED),
    (_st(consecutive_green=2, green_sha=H, certified_sha=H), H2, LoopPhase.NO_STREAK),
])
def test_loop_phase(st, head, expect):
    assert loop.loop_phase(st, head, REQ) is expect


def test_streak_to_certification_and_back():
    st = _st()
    loop.extend_streak(st, H)
    assert loop.loop_phase(st, H, REQ) is LoopPhase.GREEN_STREAK
    loop.extend_streak(st, H)
    assert loop.loop_phase(st, H, REQ) is LoopPhase.READY
    loop.certify(st, H, REQ)
    assert loop.loop_phase(st, H, REQ) is LoopPhase.CERTIFIED
    loop.break_streak(st)
    assert st == _st() and loop.loop_phase(st, H, REQ) is LoopPhase.NO_STREAK


def test_break_streak_always_drops_the_certification():
    st = _st(consecutive_green=2, green_sha=H, certified_sha=H)
    loop.break_streak(st)
    assert st["certified_sha"] is None


def test_a_streak_cannot_span_two_heads():
    st = _st(consecutive_green=1, green_sha=H)
    with pytest.raises(ValueError):
        loop.extend_streak(st, H2)
    assert st == _st(consecutive_green=1, green_sha=H)


@pytest.mark.parametrize("st", [_st(), _st(consecutive_green=1, green_sha=H),
                                _st(consecutive_green=2, green_sha=H2)])
def test_only_a_full_streak_at_this_head_can_be_certified(st):
    with pytest.raises(ValueError):
        loop.certify(st, H, REQ)
    assert st["certified_sha"] is None


def test_vocabularies_keep_their_persisted_spellings():
    assert {c.value for c in CertStatus} == {
        "local_evidence_green", "forge_certification_pending", "forge_published",
        "invalidated", "human_action_required"}
    assert {o.value for o in FixOutcome} == {"pending", "blocked", "merged", "error"}
    assert {p.value for p in MergePhase} == {"merging", "merged"}
    assert {o.value for o in ObsoletePublication} == {"old_body_observed", "old_body_not_observed"}
    assert exhaustive_sandbox.CLEANUP_REQUIRED == "cleanup_required"


def test_recoverable_phases_match_plain_strings_read_from_disk():
    saved = json.loads(json.dumps({"phase": MergePhase.MERGING}))
    assert saved == {"phase": "merging"} and saved["phase"] in RECOVERABLE_PHASES
    assert "merged" in RECOVERABLE_PHASES and "pending" not in RECOVERABLE_PHASES
    assert None not in RECOVERABLE_PHASES


def test_statuses_serialize_as_their_plain_values():
    assert json.dumps({"status": CertStatus.FORGE_PUBLISHED}) == '{"status": "forge_published"}'
    assert json.dumps({"status": FixOutcome.PENDING}) == '{"status": "pending"}'
