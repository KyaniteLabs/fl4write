"""Persisted numeric identities reconcile without crashing valid sibling work."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fl4write import engine, state
from test_retro_forgejo import RetroForge, make_config, make_pr


BAD_KEYS = ['²', '9' * 5000]


@pytest.mark.parametrize('bad', BAD_KEYS, ids=['nondecimal-digit', 'oversized-decimal'])
@pytest.mark.parametrize('field', ['prs', 'model_failures', 'retro_parked'])
def test_load_and_prune_preserve_valid_identity_with_invalid_sibling(tmp_path, bad, field):
    data = {'version': 1, 'prs': {'7': {'last_reviewed_sha': 'abc'}},
            'model_failures': {'7:abc': 2}, 'retro_parked': {'7': 1}}
    if field == 'prs':
        data[field][bad] = {'last_reviewed_sha': 'bad'}
    elif field == 'model_failures':
        data[field][bad + ':abc'] = 1
    else:
        data[field][bad] = 1
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(data))
    loaded = state.load_state(path)
    state.prune_closed(loaded, {7})
    assert loaded['prs'] == {'7': {'last_reviewed_sha': 'abc'}}
    assert loaded['model_failures'] == {'7:abc': 2}
    assert loaded['retro_parked'] == {'7': 1}
    state.save_state(path, loaded)
    assert state.load_state(path) == loaded


@pytest.mark.parametrize('bad', BAD_KEYS, ids=['nondecimal-digit', 'oversized-decimal'])
@pytest.mark.parametrize('field', ['retro_seen', 'retro_parked', 'park_expiry'])
def test_actual_retro_cycle_preserves_valid_work_with_invalid_numeric_state(tmp_path, monkeypatch, bad, field):
    merged = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    forge = RetroForge()
    forge.merged = [make_pr(number=7, merged_at=merged)]
    data = {'version': 1, 'prs': {}, 'merged_since': merged}
    if field == 'park_expiry':
        data['retro_parked'] = {'7': bad}
    else:
        data[field] = {bad: 1}
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(data))
    loaded = state.load_state(path)
    reviewed = []
    monkeypatch.setattr(engine, '_retro_review_pr', lambda pr, *a: reviewed.append(pr.number) or 'reviewed')
    config = make_config()
    engine._retro_sweep(config, forge, path, lambda pr: None, None, loaded,
                        engine.CycleReport(repo=config.repo), None)
    assert reviewed == [7]
    assert loaded['retro_seen'] == {7: True}


def test_convertible_decimal_key_forms_keep_valid_records(tmp_path):
    path = tmp_path / 'state.json'
    data = {'version': 1, 'prs': {'٧': {'fix_depth': 2}, '08': {'fix_depth': 3}},
            'model_failures': {'٧:abc': 2, '08:def': 3}}
    path.write_text(json.dumps(data))
    loaded = state.load_state(path)
    state.prune_closed(loaded, {7, 8})
    assert loaded['prs'] == data['prs']
    assert loaded['model_failures'] == data['model_failures']


@pytest.mark.parametrize('belt', ['seen', 'active-park', 'expired-park'])
def test_valid_decimal_retro_identities_keep_their_scheduling_contract(tmp_path, monkeypatch, belt):
    merged = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    forge = RetroForge()
    forge.merged = [make_pr(number=7, merged_at=merged)]
    data = {'version': 1, 'prs': {}, 'merged_since': merged}
    if belt == 'seen':
        data['retro_seen'] = {'٧': True}
    else:
        expiry = int(datetime.now(timezone.utc).timestamp()) + 86400 if belt == 'active-park' else 1
        data['retro_parked'] = {'٧': str(expiry)}
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(data))
    loaded = state.load_state(path)
    reviewed = []
    monkeypatch.setattr(engine, '_retro_review_pr', lambda pr, *a: reviewed.append(pr.number) or 'reviewed')
    config = make_config()
    engine._retro_sweep(config, forge, path, lambda pr: None, None, loaded,
                        engine.CycleReport(repo=config.repo), None)
    assert reviewed == ([7] if belt == 'expired-park' else [])
    if belt == 'expired-park':
        assert loaded['retro_parked'] == {}


@pytest.mark.parametrize('identity', ['7', '٧'])
def test_active_string_park_survives_two_persisted_cycles(tmp_path, monkeypatch, identity):
    merged = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    expiry = int(datetime.now(timezone.utc).timestamp()) + 86400
    forge = RetroForge()
    forge.merged = [make_pr(number=7, merged_at=merged)]
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'version': 1, 'prs': {}, 'merged_since': merged,
                               'retro_parked': {identity: str(expiry)}}))
    reviewed = []
    monkeypatch.setattr(engine, '_retro_review_pr', lambda pr, *a: reviewed.append(pr.number) or 'reviewed')
    config = make_config()
    for _ in range(2):
        loaded = state.load_state(path)
        considered = engine._retro_sweep(config, forge, path, lambda pr: None, None, loaded,
                                         engine.CycleReport(repo=config.repo), None)
        state.prune_closed(loaded, considered)
        state.save_state(path, loaded)
    assert reviewed == []
    assert state.load_state(path)['retro_parked'] == {identity: expiry}
