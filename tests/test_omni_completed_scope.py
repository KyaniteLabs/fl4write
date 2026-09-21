"""Completed sweeps revalidate the same effective scope as active sweeps."""
import pytest

from fl4write import engine, state
from test_omnisweep import OmniForge, _run, make_config


def _scope(kind, excluded, cap=10):
    return {
        'gatekeeper': False,
        'omnisweep': {'enabled': True, 'max_files_per_cycle': cap,
                      'exclude': ['src/b.py'] if excluded and kind == 'omni' else []},
        'path_filters': {'ignore': ['src/b.py'] if excluded and kind == 'ignore' else []},
    }


@pytest.mark.parametrize('kind', ['omni', 'ignore'])
def test_completed_scope_expansion_restarts_and_honors_cycle_cap(tmp_path, monkeypatch, kind):
    forge = OmniForge(files=[('src/a.py', 100), ('src/b.py', 100)])
    path = tmp_path / 'state.json'
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope(kind, True))
    before = state.load_state(path)
    assert before['omni_complete'] and forge.model_calls == ['src/a.py']
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope(kind, False, cap=1))
    active = state.load_state(path)
    assert not active.get('omni_complete')
    assert active['omni_fp'] != before['omni_fp']
    assert forge.model_calls == ['src/a.py', 'src/a.py']
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope(kind, False, cap=1))
    after = state.load_state(path)
    assert after['omni_complete'] and after['omni_total'] == 2
    assert forge.model_calls == ['src/a.py', 'src/a.py', 'src/b.py']


@pytest.mark.parametrize('kind', ['omni', 'ignore'])
def test_unchanged_completed_scope_does_not_spend_again(tmp_path, monkeypatch, kind):
    forge = OmniForge(files=[('src/a.py', 100), ('src/b.py', 100)])
    path = tmp_path / 'state.json'
    for _ in range(3):
        _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope(kind, True))
    assert forge.model_calls == ['src/a.py']
    assert state.load_state(path)['omni_total'] == 1


def test_legacy_completed_scope_is_reaudited_not_blessed(tmp_path, monkeypatch):
    forge = OmniForge(files=[('src/a.py', 100), ('src/b.py', 100)])
    path = tmp_path / 'state.json'
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope('omni', True))
    old = state.load_state(path)
    old.pop('omni_fp')
    state.save_state(path, old)
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope('omni', False))
    deferred = state.load_state(path)
    assert not deferred.get('omni_complete') and deferred['omni_scope_pending']
    assert 'omni_fp' not in deferred and forge.model_calls == ['src/a.py']
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope('omni', False))
    assert forge.model_calls == ['src/a.py', 'src/a.py', 'src/b.py']
    assert state.load_state(path)['omni_total'] == 2


def test_expanded_scope_over_total_cap_cannot_retain_completion(tmp_path, monkeypatch):
    forge = OmniForge(files=[(f'src/{n}.py', 100) for n in range(11)])
    path = tmp_path / 'state.json'
    _run(forge, monkeypatch, path, findings=[], model_spy=True, gatekeeper=False,
         omnisweep={'enabled': True, 'exclude': ['src/10.py'], 'max_total_files': 10})
    assert state.load_state(path)['omni_complete']
    report = _run(forge, monkeypatch, path, findings=[], model_spy=True, gatekeeper=False,
                  omnisweep={'enabled': True, 'exclude': [], 'max_total_files': 10})
    assert not state.load_state(path).get('omni_complete')
    assert len(forge.model_calls) == 10
    assert any('exceeds max_total_files' in alert for alert in report.alerts)


@pytest.mark.parametrize('bad_listing', [None, ([], True), ([None], False)])
def test_uncertain_completed_scope_defers_actions(tmp_path, monkeypatch, bad_listing):
    forge = OmniForge(files=[('src/a.py', 100)])
    path = tmp_path / 'state.json'
    _run(forge, monkeypatch, path, findings=[], model_spy=True, **_scope('omni', False))
    st = state.load_state(path)
    st['omni_findings'] = [{'id': 1, 'path': 'src/a.py', 'line': 1,
                           'rule': 'general', 'sev': 'Major', 'msg': 'Incorrect result'}]
    monkeypatch.setattr(forge, 'list_tree_files', lambda repo: bad_listing)
    monkeypatch.setattr(engine, '_omni_fix_phase', lambda *a: pytest.fail('fix on unverified scope'))
    monkeypatch.setattr(engine, '_omni_upsert_issue', lambda *a, **k: pytest.fail('publish unverified scope'))
    config = make_config(omnisweep={'enabled': True, 'fix': True})
    report = engine.CycleReport(repo=config.repo)
    engine._omnisweep_step(config, forge, path, None, st, report, None)
    assert report.alerts
    assert forge.model_calls == ['src/a.py']
