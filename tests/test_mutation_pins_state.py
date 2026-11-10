"""Mutation-testing pins for fl4write/state.py — the bot's memory.

State decides what gets re-reviewed, how far the fix lane may loop and
where every sweep resumes. These pins hold the load/reconcile contract,
the head-SHA predicate, pruning bounds, the watermark and the cycle lock.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from fl4write import state as st


def _write(tmp_path, data, raw=None):
    p = tmp_path / "s.json"
    p.write_bytes(raw if raw is not None else json.dumps(data).encode())
    return p


FRESH = {"version": 1, "prs": {}}


# ---------------------------------------------------------------- load + reconcile
def test_missing_file_is_fresh_state(tmp_path):
    out = st.load_state(tmp_path / "nope.json")
    assert out == FRESH
    out["prs"]["1"] = {}
    assert st.load_state(tmp_path / "nope.json") == FRESH        # never a shared object


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b"[1, 2]", b'"x"',
                                 b'{"version": 2, "prs": {}}', b'{"version": true, "prs": {}}',
                                 b'{"version": "1", "prs": {}}', b'{"version": 1, "prs": []}',
                                 b'{"prs": {}}'])
def test_corrupt_or_foreign_state_reconciles_to_fresh(tmp_path, raw):
    assert st.load_state(_write(tmp_path, None, raw)) == FRESH


def test_unreadable_state_aborts_instead_of_forgetting(tmp_path):
    d = tmp_path / "dir.json"
    d.mkdir()                                                    # IsADirectoryError
    with pytest.raises(st.StateIOError, match="aborting cycle"):
        st.load_state(d)


def test_valid_state_round_trips(tmp_path):
    data = {"version": 1, "prs": {"7": {"last_reviewed_sha": "a", "fix_depth": 2,
                                        "model_failures": {"x": 1}}},
            "merged_since": "2026-01-01T00:00:00Z"}  # time-rot-safe: echo only
    p = tmp_path / "s.json"
    st.save_state(p, data)
    assert st.load_state(p) == data


def test_malformed_pr_records_are_dropped_one_by_one(tmp_path):
    prs = {"1": {"last_reviewed_sha": "a"}, "2": None, "x": {}, "-3": {}, "²": {},
           "4": {"fix_depth": True}, "5": {"fix_depth": "2"}, "6": {"fix_depth": 3},
           "7": {"model_failures": [1]}, "8": {"model_failures": {"a": 1, "b": True, "c": "2"}}}
    out = st.load_state(_write(tmp_path, {"version": 1, "prs": prs}))
    assert out["prs"] == {"1": {"last_reviewed_sha": "a"}, "4": {}, "5": {},
                          "6": {"fix_depth": 3}, "7": {}, "8": {"model_failures": {"a": 1}}}


# ---------------------------------------------------------------- aux normalization
def _aux(tmp_path, **extra):
    return st.load_state(_write(tmp_path, {"version": 1, "prs": {}, **extra}))


@pytest.mark.parametrize("key", ["merged_since", "retro_cursor"])
@pytest.mark.parametrize("value,kept", [
    ("2026-01-01T00:00:00Z", True), ("2026-01-01T00:00:00", False),   # naive: dropped
    ("0000", False), (5, False), (None, True)])
def test_watermarks_must_be_aware_iso(tmp_path, key, value, kept):
    out = _aux(tmp_path, **{key: value})
    assert (key in out) is kept


def test_counters_flags_and_ledgers_degrade_safely(tmp_path):
    out = _aux(tmp_path,
               omni_pub_fail=True, omni_complete="false", omni_published=1,
               retro_complete=True, omni_aborted=False,
               open_ids=[1, True, "3", 4], omni_unfetchable=["a", 3, "b"],
               omni_unscannable="nope",
               model_failures={"1:a": 2, "2:b": "x", "3:c": False}, omni_file_fails=[1],
               retro_seen=[], pm_shadow_seen={"a": 1}, retro_shadow_seen="x",
               **{"ci_acted:abc": "false", "ci_acted:def": True,
                  "retro_defer:1:aa": "3", "retro_defer:2:bb": True,
                  "retro_defer:3:cc": -1, "retro_defer:4:dd": 2})
    assert "omni_pub_fail" not in out and "omni_complete" not in out
    assert "omni_published" not in out
    assert out["retro_complete"] is True and out["omni_aborted"] is False
    assert out["open_ids"] == [1, 4]
    assert out["omni_unfetchable"] == ["a", "b"] and "omni_unscannable" not in out
    assert out["model_failures"] == {"1:a": 2} and "omni_file_fails" not in out
    assert "retro_seen" not in out and out["pm_shadow_seen"] == {"a": 1}
    assert "retro_shadow_seen" not in out
    assert "ci_acted:abc" not in out and out["ci_acted:def"] is True
    assert out["retro_defer:1:aa"] == 3 and out["retro_defer:4:dd"] == 2
    assert "retro_defer:2:bb" not in out and "retro_defer:3:cc" not in out


def test_ledgers_are_bounded(tmp_path):
    out = _aux(tmp_path, omni_unfetchable=[f"p{i}" for i in range(2500)],
               issues_foreign_quarantined=list(range(1, 300)))
    assert len(out["omni_unfetchable"]) == 2000 and out["omni_unfetchable"][0] == "p0"
    assert out["issues_foreign_quarantined"] == list(range(100, 300))


def test_foreign_quarantine_keeps_only_positive_ints(tmp_path):
    out = _aux(tmp_path, issues_foreign_quarantined=[1, 0, -2, True, "3", 4])
    assert out["issues_foreign_quarantined"] == [1, 4]
    assert "issues_foreign_quarantined" not in _aux(tmp_path, issues_foreign_quarantined="x")
    assert _aux(tmp_path, issues_foreign_quarantined=[5])["issues_foreign_quarantined"] == [5]


def test_retro_parks_keep_numeric_keys_and_expiries(tmp_path):
    out = _aux(tmp_path, retro_parked={"1": 100, "x": 5, "2": "7", "3": True, "4": -1})
    assert out["retro_parked"] == {"1": 100, "2": 7}
    assert "retro_parked" not in _aux(tmp_path, retro_parked=[1])


_SWEEP = dict(omni_complete=True, omni_published=True, omni_cursor="a.py", omni_head="h",
              omni_fp="f", omni_next_id=3, omni_total=9, omni_scanned_total=4,
              omni_unscannable=["u"], omni_unfetchable=["v"], omni_file_fails={"a": 1},
              omni_issue=12)


def _good_row(**over):
    row = {"id": 1, "path": "a.py", "line": 2, "rule": "r", "sev": "Major", "msg": "m"}
    row.update(over)
    return row


def test_clean_sweep_state_survives(tmp_path):
    out = _aux(tmp_path, omni_findings=[_good_row()], **_SWEEP)
    for k, v in _SWEEP.items():
        assert out[k] == v
    assert out["omni_findings"] == [_good_row()]


@pytest.mark.parametrize("bad", [
    dict(omni_cursor=5), dict(omni_head=["h"]), dict(omni_fp=1),
    dict(omni_next_id="3"), dict(omni_total=True), dict(omni_scanned_total=1.5)])
def test_a_bad_core_field_resets_the_whole_sweep_but_keeps_the_issue(tmp_path, bad):
    out = _aux(tmp_path, omni_findings=[_good_row()], **{**_SWEEP, **bad})
    for k in ("omni_complete", "omni_published", "omni_cursor", "omni_head", "omni_fp",
              "omni_findings", "omni_next_id", "omni_total", "omni_scanned_total",
              "omni_unscannable", "omni_unfetchable", "omni_file_fails"):
        assert k not in out, k
    assert out["omni_issue"] == 12


@pytest.mark.parametrize("row", [
    "junk", _good_row(id=True), _good_row(id="1"), _good_row(line=0), _good_row(line=True),
    _good_row(path=None), _good_row(rule=1), _good_row(sev=None), _good_row(msg=2)])
def test_a_malformed_finding_row_resets_progress_but_keeps_good_rows(tmp_path, row):
    out = _aux(tmp_path, omni_findings=[_good_row(), row], **_SWEEP)
    assert out["omni_findings"] == [_good_row()]
    for k in ("omni_complete", "omni_published", "omni_cursor", "omni_head", "omni_fp",
              "omni_next_id", "omni_total", "omni_scanned_total", "omni_file_fails",
              "omni_unscannable", "omni_unfetchable"):
        assert k not in out, k
    assert out["omni_issue"] == 12


def test_finding_flags_must_be_real_bools(tmp_path):
    rows = [_good_row(fix_attempted=True, fix_stale="yes"), _good_row(id=2, fix_stale=False)]
    out = _aux(tmp_path, omni_findings=rows)
    assert out["omni_findings"] == [_good_row(fix_attempted=True), _good_row(id=2, fix_stale=False)]
    assert "omni_findings" not in _aux(tmp_path, omni_findings={"a": 1})


# ---------------------------------------------------------------- number keys
@pytest.mark.parametrize("value,out", [
    (0, 0), (7, 7), (-1, None), (True, None), ("12", 12), ("٣", 3), ("²", None),
    ("1.0", None), ("", None), (None, None), (1.0, None), ("9" * 5000, None)])
def test_parse_number_key(value, out):
    assert st.parse_number_key(value) == out


# ---------------------------------------------------------------- review predicate
def test_needs_review_on_new_unknown_or_shadow_heads():
    s = {"prs": {"3": {"last_reviewed_sha": "aaa", "last_outcome": "posted"},
                 "4": {"last_reviewed_sha": "bbb", "last_outcome": "shadow-posted"},
                 "5": {"last_reviewed_sha": "ccc"}}}
    assert st.needs_review(s, 3, "aaa") is False
    assert st.needs_review(s, 3, "zzz") is True
    assert st.needs_review(s, 4, "bbb") is True
    assert st.needs_review(s, 5, "ccc") is False
    assert st.needs_review(s, 9, "x") is True


def test_mark_reviewed_merges_into_the_record():
    s = {"prs": {"3": {"fix_depth": 2, "last_reviewed_sha": "old"}}}
    st.mark_reviewed(s, 3, "new", "posted")
    st.mark_reviewed(s, 4, "h", "shadow")
    assert s["prs"] == {"3": {"fix_depth": 2, "last_reviewed_sha": "new", "last_outcome": "posted"},
                        "4": {"last_reviewed_sha": "h", "last_outcome": "shadow"}}


# ---------------------------------------------------------------- pruning
def test_prune_keeps_open_and_fix_state_records():
    s = {"prs": {"1": {}, "2": {}, "3": {"fix_depth": 1}, "4": {"model_failures": {}},
                 "x": {"fix_depth": 1}}}
    st.prune_closed(s, {1})
    assert s["prs"] == {"1": {}, "3": {"fix_depth": 1}, "4": {"model_failures": {}}}


def test_prune_bounds_closed_history_to_the_newest_2000():
    s = {"prs": {str(i): {"fix_depth": 1} for i in range(1, 2006)}}
    s["prs"]["5000"] = {}
    st.prune_closed(s, {5000, 3})
    closed = [k for k in s["prs"] if k not in ("5000", "3")]
    assert len(closed) == 2000 and closed[0] == "6" and closed[-1] == "2005"
    assert "5000" in s["prs"] and "3" in s["prs"]
    s = {"prs": {str(i): {"fix_depth": 1} for i in range(1, 2001)}}
    st.prune_closed(s, set())
    assert len(s["prs"]) == 2000


def test_prune_collects_lane_belts(monkeypatch):
    now = int(time.time())
    s = {"prs": {}, "model_failures": {"1:a": 1, "2:b": 1, "x:c": 1, 3: 1},
         "retro_parked": {"1": now + 100, "2": now - 100, "3": now - 100, "x": now + 9,
                          "4": "bad"}}
    for i in range(105):
        s[f"ci_acted:{i}"] = True
    st.prune_closed(s, {1, 3})
    assert s["model_failures"] == {"1:a": 1}
    assert s["retro_parked"] == {"1": now + 100, "3": now - 100}
    acted = [k for k in s if k.startswith("ci_acted:")]
    assert len(acted) == 100 and acted[0] == "ci_acted:5"


def test_prune_leaves_non_dict_belts_alone():
    s = {"prs": {}, "model_failures": None, "retro_parked": None}
    st.prune_closed(s, set())
    assert s["model_failures"] is None and s["retro_parked"] is None


# ---------------------------------------------------------------- watermark
def test_merged_watermark_only_advances():
    s: dict = {}
    assert st.merged_watermark(s) is None
    st.advance_merged_watermark(s, "garbage")
    assert st.merged_watermark(s) is None
    st.advance_merged_watermark(s, "2026-01-02T00:00:00Z")  # time-rot-safe: ordering only
    st.advance_merged_watermark(s, "2026-01-01T00:00:00Z")  # time-rot-safe: ordering only
    assert s[st.MERGED_SINCE_KEY] == "2026-01-02T00:00:00Z"
    st.advance_merged_watermark(s, "2026-01-02T01:00:00+01:00")  # time-rot-safe: same instant
    assert s[st.MERGED_SINCE_KEY] == "2026-01-02T00:00:00Z"
    st.advance_merged_watermark(s, "2026-01-03T00:00:00Z")  # time-rot-safe: ordering only
    assert s["merged_since"] == "2026-01-03T00:00:00Z"


def test_a_bad_stored_watermark_is_replaced():
    s = {"merged_since": "junk"}
    st.advance_merged_watermark(s, "2026-01-01T00:00:00Z")  # time-rot-safe: ordering only
    assert s["merged_since"] == "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------- atomic write + lock
def test_atomic_write_leaves_no_temp_files_and_cleans_up_on_failure(tmp_path):
    p = tmp_path / "sub" / "s.json"
    st.save_state(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    assert p.read_text().startswith("{\n \"a\"")                    # indent=1
    with pytest.raises(TypeError):
        st.save_state(p, {"a": object()})
    assert json.loads(p.read_text()) == {"a": 1}
    assert sorted(x.name for x in p.parent.iterdir()) == ["s.json"]


def test_cycle_lock_is_exclusive_and_released(tmp_path):
    lock = tmp_path / "locks" / "repo.lock"
    with st.CycleLock(lock) as held:
        assert lock.exists() and held._fd is not None
        token = lock.read_text().split()
        assert token[0] == str(os.getpid()) and len(token) == 3
        with pytest.raises(st.CycleLockHeld, match="cycle lock held"):
            with st.CycleLock(lock):
                pass
    assert held._fd is None
    with st.CycleLock(lock):                                       # free again
        pass
    assert oct(lock.stat().st_mode & 0o777) == "0o600"


def test_valid_pub_fail_counter_survives(tmp_path):
    assert _aux(tmp_path, omni_pub_fail=3)["omni_pub_fail"] == 3


def test_malformed_finding_row_also_resets_the_truncation_marker(tmp_path):
    out = _aux(tmp_path, omni_findings=[_good_row(), "junk"], omni_truncated_seen=True)
    assert "omni_truncated_seen" not in out
