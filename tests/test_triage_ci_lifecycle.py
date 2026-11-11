"""Issue triage and CI watch state made explicit.

Issue triage: NEW / RETRY / QUARANTINED / TRIAGED, derived from the watermark,
the retry belt and the quarantine list (fl4write/issues.py). CI watch: one
HEAD is GREEN, RED or ACTED (fl4write/sweep_lifecycle.py).
"""

from __future__ import annotations

import pytest

from fl4write import issues as iss
from fl4write import sweep_lifecycle as sw
from fl4write.issues import IssueState
from fl4write.sweep_lifecycle import CIHead

HEAD = "c" * 40


# ---------------------------------------------------------------- CI watch
def test_ci_head_states():
    st: dict = {}
    assert sw.ci_head_state(st, HEAD, failing=False) is CIHead.GREEN
    assert sw.ci_head_state(st, HEAD, failing=True) is CIHead.RED
    sw.ci_mark_acted(st, HEAD)
    assert st == {f"ci_acted:{HEAD}": True}
    assert sw.ci_head_state(st, HEAD, failing=True) is CIHead.ACTED
    assert sw.ci_head_state(st, "d" * 40, failing=True) is CIHead.RED   # new commit re-arms
    assert sw.ci_head_state(st, HEAD, failing=False) is CIHead.GREEN    # green wins


def test_ci_only_a_real_true_counts_as_acted():
    assert sw.ci_head_state({f"ci_acted:{HEAD}": "true"}, HEAD, failing=True) is CIHead.RED


# ---------------------------------------------------------------- issue triage
@pytest.mark.parametrize("st,number,expect", [
    ({}, 5, IssueState.NEW),
    ({"last_triaged_number": 5}, 5, IssueState.TRIAGED),
    ({"last_triaged_number": 5}, 6, IssueState.NEW),
    ({"last_triaged_number": 9, "issues_retry": [5]}, 5, IssueState.RETRY),
    ({"last_triaged_number": 9, "issues_retry": [5], "issues_foreign_quarantined": [5]},
     5, IssueState.QUARANTINED),
    ({"last_triaged_number": 9, "issues_foreign_quarantined": [5]}, 5, IssueState.TRIAGED),
    ({"last_triaged_number": True}, 1, IssueState.NEW),      # bool is not a watermark
    ({"issues_retry": "5"}, 5, IssueState.NEW),
])
def test_issue_state_is_derived(st, number, expect):
    assert iss.issue_state(st, number) is expect


def test_quarantine_is_bounded_deduped_and_keeps_the_retry():
    st: dict = {"issues_foreign_quarantined": list(range(1, iss.QUARANTINE_CAP + 1))}
    retry: set = set()
    iss._quarantine(st, 999, retry)
    q = st["issues_foreign_quarantined"]
    assert len(q) == iss.QUARANTINE_CAP and q[0] == 2 and q[-1] == 999 and retry == {999}
    iss._quarantine(st, 999, retry)
    assert st["issues_foreign_quarantined"].count(999) == 1
