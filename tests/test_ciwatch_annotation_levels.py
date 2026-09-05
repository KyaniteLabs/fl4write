"""Informational annotations cannot direct an automatic CI repair."""

import pytest

from test_ciwatch import CIRedForge, _run


@pytest.mark.parametrize("level", ["notice", "warning"])
def test_informational_annotations_escalate_without_fix(tmp_path, monkeypatch, level):
    forge = CIRedForge(annotations=[
        {"path": "tests/test_x.py", "start_line": 12, "message": "Information", "level": level},
    ])
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "pr_opened"})
    assert report.ci_red_heads == report.ci_escalations == 1
    assert report.fix_attempts == report.ci_fix_prs_opened == 0
    assert forge.fix_attempts == []
    assert len(forge.issues_opened) == 1
    assert "(no file-level annotation findings" in forge.issues_opened[0][1]


@pytest.mark.parametrize("level", ["notice", "warning"])
def test_only_failure_annotation_reaches_fix(tmp_path, monkeypatch, level):
    forge = CIRedForge(annotations=[
        {"path": "tests/test_x.py", "start_line": 11, "message": "Information", "level": level},
        {"path": "tests/test_x.py", "start_line": 12, "message": "Actual failure", "level": "failure"},
    ])
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "pr_opened"},
                  ci_watch={"enabled": True, "max_annotations": 1})
    assert report.fix_attempts == report.ci_fix_prs_opened == 1
    assert len(forge.fix_attempts) == 1
    assert forge.fix_attempts[0][1].line == 12
    assert "Actual failure" in forge.fix_attempts[0][1].message
    assert forge.issues_opened == []


def test_legacy_annotation_without_level_remains_actionable(tmp_path, monkeypatch):
    forge = CIRedForge(annotations=[
        {"path": "tests/test_x.py", "start_line": 12, "message": "Legacy failure"},
    ])
    report = _run(tmp_path, forge, monkeypatch, fix_result={"status": "pr_opened"})
    assert report.fix_attempts == report.ci_fix_prs_opened == 1
    assert len(forge.fix_attempts) == 1
