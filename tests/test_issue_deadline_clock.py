"""Regression tests for R17-001: issues deadline must use monotonic clock.

The CLI builds the deadline from time.monotonic() + budget_s, but
run_issues_cycle compared it against time.time(). On normal systems the
monotonic clock is far behind the wall clock, so every future deadline
appeared expired and no issue was ever triaged.
"""

from types import SimpleNamespace

import fl4write.issues as issues


def _config():
    return SimpleNamespace(repo="owner/repo", shadow=True)


def _forge(issues_list):
    class FakeForge:
        def _paginated(self, _path, page_size=50):
            return iter(issues_list)

    return FakeForge()


def test_future_monotonic_deadline_processes_eligible_issue(monkeypatch):
    """A future monotonic deadline must not appear expired."""
    issue = {"number": 7, "title": "hello", "body": "world"}
    monkeypatch.setattr(issues, "collect_new_issues",
                        lambda forge, repo, last, retry=None: [issue])
    monkeypatch.setattr(issues, "triage_issue",
                        lambda issue, config: {"labels": [], "urgency": "low"})

    # Simulate a monotonic clock far behind wall-clock time.
    monkeypatch.setattr(issues.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(issues.time, "time", lambda: 1_700_000_000.0)

    summary = issues.run_issues_cycle(
        _config(), {"last_triaged_number": 0}, _forge([]),
        deadline=1000.0 + 60.0,
    )

    assert summary["triaged"] == 1
    assert summary["errors"] == 0


def test_expired_monotonic_deadline_defers_issue(monkeypatch):
    """An expired monotonic deadline must defer the issue."""
    issue = {"number": 7, "title": "hello", "body": "world"}
    monkeypatch.setattr(issues, "collect_new_issues",
                        lambda forge, repo, last, retry=None: [issue])
    monkeypatch.setattr(issues, "triage_issue",
                        lambda issue, config: {"labels": [], "urgency": "low"})

    monkeypatch.setattr(issues.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(issues.time, "time", lambda: 1_700_000_000.0)

    summary = issues.run_issues_cycle(
        _config(), {"last_triaged_number": 0}, _forge([]),
        deadline=1000.0 - 1.0,
    )

    assert summary["triaged"] == 0
    assert summary["errors"] == 0
