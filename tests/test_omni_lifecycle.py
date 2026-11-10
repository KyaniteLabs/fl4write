"""The omnisweep lifecycle made explicit (fl4write/omni_lifecycle.py).

The sweep's phase used to be implied by a dozen loose keys written from many
places. These tests pin the derived phase for every key combination that
matters, each named transition, the full reset, the per-finding fix moves,
and the two bugs the explicit machine exposed.
"""

from __future__ import annotations

import pytest

from fl4write import omni_lifecycle as omni
from fl4write import state
from fl4write.omni_lifecycle import OmniFix, OmniPhase
from test_omnisweep import F1, OmniForge, _run


# ---------------------------------------------------------------- derived phase
@pytest.mark.parametrize("st,expect", [
    ({}, OmniPhase.IDLE),
    ({"omni_issue": 4}, OmniPhase.IDLE),
    ({"omni_cursor": "a.py"}, OmniPhase.SCANNING),
    ({"omni_findings": [{"id": 1}]}, OmniPhase.SCANNING),
    ({"omni_complete": True}, OmniPhase.COMPLETE),
    ({"omni_complete": True, "omni_published": True}, OmniPhase.COMPLETE),   # old format
    ({"omni_complete": True, "omni_published": True, "omni_report_version": 2}, OmniPhase.PUBLISHED),
    ({"omni_complete": True, "omni_published": True, "omni_report_version": 2,
      "omni_issue": 4}, OmniPhase.COMPLETE),               # clean update of an issue still owed
    ({"omni_complete": True, "omni_published": True, "omni_report_version": 2,
      "omni_issue": 4, "omni_clean_published": True}, OmniPhase.PUBLISHED),
    ({"omni_complete": True, "omni_published": True, "omni_report_version": 2,
      "omni_issue": 4, "omni_findings": [{"id": 1}]}, OmniPhase.PUBLISHED),
    ({"omni_scope_pending": True, "omni_complete": False}, OmniPhase.SCOPE_PENDING),
    ({"omni_scope_pending": True, "omni_cursor": "x"}, OmniPhase.SCOPE_PENDING),
])
def test_phase_is_derived_from_the_persisted_keys(st, expect):
    assert omni.phase(st) is expect


# ---------------------------------------------------------------- transitions
def test_scanning_to_published_happy_path():
    st = {"omni_cursor": "z.py", "omni_findings": [{"id": 1}], "omni_pub_fail": 2}
    omni.complete(st, 9)
    assert omni.phase(st) is OmniPhase.COMPLETE and st["omni_total"] == 9
    omni.published(st, clean=False)
    assert omni.phase(st) is OmniPhase.PUBLISHED
    assert st["omni_clean_published"] is False and "omni_pub_fail" not in st


def test_publish_failure_stays_complete_and_counts():
    st = {"omni_complete": True, "omni_published": True, "omni_report_version": 2, "omni_issue": 3,
          "omni_findings": [{"id": 1}]}
    assert omni.publish_failed(st) == 1 and omni.publish_failed(st) == 2
    assert omni.phase(st) is OmniPhase.COMPLETE and st["omni_pub_fail"] == 2
    omni.forget_issue(st)
    assert "omni_issue" not in st and st["omni_pub_fail"] == 0 and st["omni_published"] is False


def test_fresh_scope_keeps_the_old_receipt_until_reset():
    st = {"omni_complete": True, "omni_published": True, "omni_report_version": 2, "omni_issue": 3}
    omni.schedule_fresh_scope(st)
    assert omni.phase(st) is OmniPhase.SCOPE_PENDING and st["omni_published"] is True


def test_reset_clears_every_sweep_key_but_keeps_the_issue():
    st = {k: "x" for k in omni.SWEEP_KEYS}
    st.update(omni_issue=7, merged_since="keep", prs={})
    omni.reset(st)
    assert st == {"omni_issue": 7, "merged_since": "keep", "prs": {}}
    assert omni.phase(st) is OmniPhase.IDLE


def test_reset_no_longer_carries_publication_debt_into_a_new_sweep():
    """Bug: the old reset kept omni_pub_fail and omni_report_version, so a
    brand-new sweep's audit issue could be reconciled away after fewer than
    the three allowed update failures."""
    st = {"omni_complete": True, "omni_pub_fail": 2, "omni_report_version": 2, "omni_issue": 5}
    omni.reset(st)
    assert "omni_pub_fail" not in st and "omni_report_version" not in st


# ---------------------------------------------------------------- per finding
def test_fix_state_moves():
    row: dict = {}
    assert omni.fix_state(row) is OmniFix.PENDING
    omni.set_fix_state(row, OmniFix.ATTEMPTED)
    assert row == {"fix_attempted": True} and omni.fix_state(row) is OmniFix.ATTEMPTED
    omni.set_fix_state(row, OmniFix.PENDING)                 # transient error: retry
    assert row == {"fix_attempted": False}
    omni.set_fix_state(row, OmniFix.STALE)
    assert row == {"fix_attempted": False, "fix_stale": True}
    assert omni.fix_state({"fix_stale": True, "fix_attempted": True}) is OmniFix.STALE


@pytest.mark.parametrize("start,new", [
    ({}, OmniFix.PENDING), ({"fix_attempted": True}, OmniFix.ATTEMPTED),
    ({"fix_attempted": True}, OmniFix.STALE), ({"fix_stale": True}, OmniFix.PENDING),
    ({"fix_stale": True}, OmniFix.ATTEMPTED)])
def test_illegal_fix_moves_are_refused(start, new):
    row = dict(start)
    with pytest.raises(ValueError):
        omni.set_fix_state(row, new)
    assert row == start


# ---------------------------------------------------------------- engine, end to end
def test_completion_alert_never_hides_unreadable_files(tmp_path, monkeypatch):
    """Bug: when the final batch held only unreadable files, the sweep
    finished through a second copy of the completion code that dropped the
    unfetchable count from its 'complete' alert."""
    forge = OmniForge(files=[("src/a.py", 100), ("src/b.py", 100)])
    forge.get_file = lambda repo, path, ref: None if path == "src/b.py" else "x = 1\n"
    sp = tmp_path / "s.json"
    _run(forge, monkeypatch, sp, findings=[], omnisweep={"enabled": True, "max_files_per_cycle": 1})
    r2 = _run(forge, monkeypatch, sp, findings=[], omnisweep={"enabled": True, "max_files_per_cycle": 1})
    r3 = _run(forge, monkeypatch, sp, findings=[], omnisweep={"enabled": True, "max_files_per_cycle": 1})
    done = [a for r in (r2, r3) for a in r.alerts if a.startswith("omnisweep complete")]
    assert done == ["omnisweep complete: 0 findings across 2 files (1 unfetchable — recorded, not audited)"]
    st = state.load_state(sp)
    assert omni.phase(st) is OmniPhase.PUBLISHED and st["omni_unfetchable"] == ["src/b.py"]


def test_full_sweep_walks_the_phases(tmp_path, monkeypatch):
    forge = OmniForge(files=[("src/a.py", 100), ("src/b.py", 100)])
    sp = tmp_path / "s.json"
    _run(forge, monkeypatch, sp, findings=[F1], omnisweep={"enabled": True, "max_files_per_cycle": 1})
    assert omni.phase(state.load_state(sp)) is OmniPhase.SCANNING
    _run(forge, monkeypatch, sp, findings=[F1], omnisweep={"enabled": True, "max_files_per_cycle": 1})
    st = state.load_state(sp)
    assert omni.phase(st) is OmniPhase.PUBLISHED and st["omni_issue"] == forge.issue_number
