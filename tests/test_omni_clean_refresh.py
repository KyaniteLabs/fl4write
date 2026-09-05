"""A clean new sweep replaces the existing audit and retries failed updates."""
import pytest

from fl4write import state
from test_omnisweep import F1, OmniForge, _run as _run_cycle


def _run(*args, **kwargs):
    return _run_cycle(*args, gatekeeper=False, **kwargs)


class RefreshForge(OmniForge):
    def __init__(self):
        super().__init__(files=[("src/a.py", 100)])
        self.head = "a" * 40
        self.update_attempts = []

    def head_check_runs(self, repo):
        return self.head, []

    def update_issue(self, repo, number, body):
        self.update_attempts.append((number, body))
        return super().update_issue(repo, number, body)


def _old_audit(tmp_path, monkeypatch):
    forge = RefreshForge()
    path = tmp_path / "state.json"
    _run(forge, monkeypatch, path, findings=[F1])
    before = state.load_state(path)
    assert before["omni_complete"] and before["omni_published"]
    assert len(forge.issues) == 1
    forge.head = "b" * 40
    return forge, path, before["omni_issue"]


@pytest.mark.parametrize("empty_tree", [False, True])
def test_new_clean_head_replaces_prior_findings(tmp_path, monkeypatch, empty_tree):
    forge, path, number = _old_audit(tmp_path, monkeypatch)
    if empty_tree:
        forge.tree = []
    _run(forge, monkeypatch, path, findings=[])
    after = state.load_state(path)
    assert after["omni_complete"] and after["omni_published"]
    assert after["omni_issue"] == number and after["omni_head"] == forge.head
    assert after.get("omni_findings", []) == []
    assert len(forge.issues) == 1 and len(forge.issue_updates) == 1
    updated_number, body = forge.issue_updates[0]
    assert updated_number == number and "(COMPLETE)" in body
    assert "| Critical | 0 |" in body
    assert "### Critical" not in body
    assert "No findings" in body


def test_clean_update_failures_keep_issue_and_retry_without_rescan(tmp_path, monkeypatch):
    forge, path, number = _old_audit(tmp_path, monkeypatch)
    forge.fail_update = True
    for _ in range(4):
        _run(forge, monkeypatch, path, findings=[], model_spy=True)
        current = state.load_state(path)
        assert current["omni_complete"] and not current.get("omni_published", False)
        assert current["omni_issue"] == number
    assert len(forge.update_attempts) == 4 and len(forge.model_calls) == 1
    forge.fail_update = False
    _run(forge, monkeypatch, path, findings=[], model_spy=True)
    assert state.load_state(path)["omni_published"]
    assert len(forge.issues) == 1 and len(forge.issue_updates) == 1
    _run(forge, monkeypatch, path, findings=[], model_spy=True)
    assert len(forge.update_attempts) == 5 and len(forge.model_calls) == 1


def test_first_clean_sweep_creates_no_empty_issue(tmp_path, monkeypatch):
    forge = RefreshForge()
    path = tmp_path / "state.json"
    _run(forge, monkeypatch, path, findings=[])
    assert state.load_state(path)["omni_published"]
    assert not forge.issues and not forge.update_attempts


def test_legacy_clean_publication_flag_does_not_preserve_stale_issue(tmp_path, monkeypatch):
    forge, path, number = _old_audit(tmp_path, monkeypatch)
    legacy = state.load_state(path)
    legacy.update(omni_head=forge.head, omni_findings=[], omni_complete=True,
                  omni_published=True, omni_report_version=2)
    legacy.pop("omni_clean_published", None)
    state.save_state(path, legacy)
    _run(forge, monkeypatch, path, findings=[], model_spy=True)
    assert forge.issue_updates[0][0] == number
    assert "No findings" in forge.issue_updates[0][1]
    assert state.load_state(path)["omni_clean_published"]
    assert not forge.model_calls


def test_shadow_preserves_existing_audit_without_publication(tmp_path, monkeypatch):
    forge, path, number = _old_audit(tmp_path, monkeypatch)
    _run(forge, monkeypatch, path, findings=[], shadow=True, model_spy=True)
    assert state.load_state(path)["omni_issue"] == number
    assert len(forge.issues) == 1 and not forge.update_attempts and not forge.model_calls
