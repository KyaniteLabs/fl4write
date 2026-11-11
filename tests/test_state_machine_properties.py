"""Stateful property tests for the explicit state machines.

Each machine is driven through random sequences of its named transitions
(Hypothesis RuleBasedStateMachine). After EVERY step the invariants the
code relies on are checked, and a small reference model predicts the
phase, so a transition that drifts from its contract fails with the exact
shortest sequence that breaks it.
"""

from __future__ import annotations

import hypothesis.strategies as st_
from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from fl4write import exhaustive_lifecycle as loop
from fl4write import omni_lifecycle as omni
from fl4write import sweep_lifecycle as sw
from fl4write.exhaustive_lifecycle import LoopPhase
from fl4write.omni_lifecycle import OmniFix, OmniPhase

SETTINGS = settings(max_examples=150, stateful_step_count=40, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
HEADS = st_.sampled_from(["a" * 40, "b" * 40, "c" * 40])


# ---------------------------------------------------------------- omnisweep
class OmnisweepMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.st: dict = {"omni_issue": 9}
        self.model = OmniPhase.IDLE

    @rule(path=st_.sampled_from(["a.py", "b.py", "c.py"]))
    def scan_a_file(self, path):
        if self.model in (OmniPhase.IDLE, OmniPhase.SCANNING):
            self.st["omni_cursor"] = path
            self.model = OmniPhase.SCANNING

    @rule(total=st_.integers(0, 50))
    def complete(self, total):
        if self.model in (OmniPhase.IDLE, OmniPhase.SCANNING):
            omni.complete(self.st, total)
            self.model = OmniPhase.COMPLETE

    @rule(clean=st_.booleans())
    def publish(self, clean):
        if self.model in (OmniPhase.COMPLETE, OmniPhase.PUBLISHED):
            if not clean:
                self.st["omni_findings"] = [{"id": 1}]
            else:
                self.st.pop("omni_findings", None)
            omni.published(self.st, clean=clean)
            self.model = OmniPhase.PUBLISHED

    @rule()
    def publish_fails(self):
        if self.model in (OmniPhase.COMPLETE, OmniPhase.PUBLISHED):
            before = int(self.st.get("omni_pub_fail", 0) or 0)
            assert omni.publish_failed(self.st) == before + 1
            self.model = OmniPhase.COMPLETE

    @rule()
    def scope_unknown(self):
        if self.model in (OmniPhase.COMPLETE, OmniPhase.PUBLISHED):
            omni.schedule_fresh_scope(self.st)
            self.model = OmniPhase.SCOPE_PENDING

    @rule()
    def reset(self):
        issue = self.st.get("omni_issue")
        omni.reset(self.st)
        assert self.st.get("omni_issue") == issue           # the issue survives
        # independent of omni.SWEEP_KEYS: no other omni_* key may survive a reset
        assert [k for k in self.st if k.startswith("omni_") and k != "omni_issue"] == []
        self.model = OmniPhase.IDLE

    @invariant()
    def phase_matches_model(self):
        assert omni.phase(self.st) is self.model

    @invariant()
    def published_means_report_current(self):
        assert (omni.phase(self.st) is OmniPhase.PUBLISHED) == (
            bool(self.st.get("omni_complete")) and not self.st.get("omni_scope_pending")
            and omni.report_current(self.st))


TestOmnisweepMachine = OmnisweepMachine.TestCase
TestOmnisweepMachine.settings = SETTINGS


# ---------------------------------------------------------------- omni finding fix
class OmniFixMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.row: dict = {}
        self.model = OmniFix.PENDING

    @rule(target_state=st_.sampled_from(list(OmniFix)))
    def move(self, target_state):
        allowed = (self.model, target_state) in {
            (OmniFix.PENDING, OmniFix.ATTEMPTED), (OmniFix.ATTEMPTED, OmniFix.PENDING),
            (OmniFix.PENDING, OmniFix.STALE)}
        before = dict(self.row)
        try:
            omni.set_fix_state(self.row, target_state)
        except ValueError:
            assert not allowed and self.row == before        # refusals change nothing
        else:
            assert allowed
            self.model = target_state

    @invariant()
    def state_matches_model(self):
        assert omni.fix_state(self.row) is self.model

    @invariant()
    def stale_is_final(self):
        if self.model is OmniFix.STALE:
            assert self.row.get("fix_stale") is True


TestOmniFixMachine = OmniFixMachine.TestCase
TestOmniFixMachine.settings = SETTINGS


# ---------------------------------------------------------------- retro per PR
class RetroMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.st: dict = {}
        self.parked: dict = {}
        self.model_tries: dict = {}     # (number, sha) -> consecutive deferrals
        self.model_parked: set = set()
        self.now = 1_000_000

    @rule(number=st_.integers(1, 3), sha=HEADS)
    def deferred(self, number, sha):
        tries, parked_now = sw.retro_deferred(self.st, self.parked, number, sha, now=self.now)
        expect = self.model_tries.get((number, sha), 0) + 1
        assert tries == expect
        if expect >= sw.RETRO_DEFER_LIMIT:
            assert parked_now
            self.model_tries.pop((number, sha), None)
            self.model_parked.add(number)
        else:
            assert not parked_now
            self.model_tries[(number, sha)] = expect

    @rule(number=st_.integers(1, 3), sha=HEADS)
    def done(self, number, sha):
        sw.retro_done(self.st, self.parked, number, sha)
        self.model_tries.pop((number, sha), None)
        self.model_parked.discard(number)

    @invariant()
    def counters_match_model(self):
        stored = {k: v for k, v in self.st.items() if k.startswith("retro_defer:")}
        assert stored == {f"retro_defer:{n}:{s[:10]}": t for (n, s), t in self.model_tries.items()}
        assert all(1 <= t < sw.RETRO_DEFER_LIMIT for t in stored.values())

    @invariant()
    def parks_match_model(self):
        assert {int(k) for k in self.parked} == self.model_parked
        assert sw.load_parked({"retro_parked": self.parked}) == self.parked


TestRetroMachine = RetroMachine.TestCase
TestRetroMachine.settings = SETTINGS


# ---------------------------------------------------------------- exhaustive streak
REQ = 2


class StreakMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.st = {"consecutive_green": 0, "green_sha": None, "certified_sha": None,
                   "pending_round": None}

    @rule(head=HEADS)
    def green_round(self, head):
        if self.st["green_sha"] not in (None, head):
            loop.break_streak(self.st)                        # the loop breaks first
        if self.st["consecutive_green"] < REQ:
            loop.extend_streak(self.st, head)

    @rule(head=HEADS)
    def streak_cannot_span_heads(self, head):
        if self.st["green_sha"] not in (None, head):
            before = dict(self.st)
            try:
                loop.extend_streak(self.st, head)
            except ValueError:
                assert self.st == before
            else:
                raise AssertionError("a streak spanned two HEADs")

    @rule()
    def red_round(self):
        loop.break_streak(self.st)

    @precondition(lambda self: self.st["green_sha"] is not None)
    @rule()
    def certify(self):
        head = self.st["green_sha"]
        if self.st["consecutive_green"] == REQ:
            loop.certify(self.st, head, REQ)
        else:
            before = dict(self.st)
            try:
                loop.certify(self.st, head, REQ)
            except ValueError:
                assert self.st == before
            else:
                raise AssertionError("certified a partial streak")

    @invariant()
    def validator_invariants_hold(self):
        st = self.st
        # the same rules exhaustive._valid_state / the ledger check enforce
        assert (st["consecutive_green"] > 0) == (st["green_sha"] is not None)
        assert 0 <= st["consecutive_green"] <= REQ
        if st["certified_sha"] is not None:
            assert st["consecutive_green"] == REQ and st["green_sha"] == st["certified_sha"]

    @invariant()
    def phase_is_consistent(self):
        head = self.st["green_sha"] or "a" * 40
        phase = loop.loop_phase(self.st, head, REQ)
        if self.st["certified_sha"]:
            assert phase is LoopPhase.CERTIFIED
        elif self.st["consecutive_green"] == REQ:
            assert phase is LoopPhase.READY
        elif self.st["consecutive_green"]:
            assert phase is LoopPhase.GREEN_STREAK
        else:
            assert phase is LoopPhase.NO_STREAK


TestStreakMachine = StreakMachine.TestCase
TestStreakMachine.settings = SETTINGS
