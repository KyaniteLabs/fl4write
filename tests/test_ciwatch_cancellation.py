"""A cancellation stays quiet without consuming a later failure's action."""

import json

import pytest

from test_ciwatch import CIRedForge, _run


@pytest.mark.parametrize("conclusion", ["cancelled", "canceled"])
def test_cancelled_check_then_failure_same_sha(tmp_path, monkeypatch, conclusion):
    forge = CIRedForge(checks=[
        {"id": 1, "name": "test", "status": "completed", "conclusion": conclusion},
    ], annotations=[])
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "nofix"})
    assert report.ci_red_heads == report.ci_escalations == 0
    assert forge.issues_opened == forge.fix_attempts == []
    assert not json.loads((tmp_path / "state.json").read_text()).get(f"ci_acted:{forge.head}")

    forge.checks[0]["conclusion"] = "failure"
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "nofix"})
    assert report.ci_red_heads == report.ci_escalations == 1
    assert len(forge.issues_opened) == 1
    assert json.loads((tmp_path / "state.json").read_text())[f"ci_acted:{forge.head}"]
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "nofix"})
    assert report.ci_escalations == 0
    assert len(forge.issues_opened) == 1


def test_cancellation_does_not_displace_failure_at_check_cap(tmp_path, monkeypatch):
    forge = CIRedForge(checks=[
        {"id": 1, "name": "abandoned", "status": "completed", "conclusion": "cancelled"},
        {"id": 2, "name": "actual failure", "status": "completed", "conclusion": "failure"},
    ], annotations=[])
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "nofix"},
                  ci_watch={"enabled": True, "max_checks": 1})
    assert report.ci_red_heads == report.ci_escalations == 1
    title, body = forge.issues_opened[0]
    assert "actual failure" in title and "actual failure" in body
    assert "abandoned" not in title and "abandoned" not in body
