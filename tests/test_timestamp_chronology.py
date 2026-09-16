"""Timestamp spelling must not change capped sweep chronology."""
from datetime import datetime, timedelta, timezone

import pytest

from fl4write import engine, state
from fl4write.config import ForgeBinding
from fl4write.forges import ForgejoAdapter, GitHubAdapter
from test_postmerge import make_config


@pytest.mark.parametrize('stamp', [
    '2026-09-01 12:00:00Z', '2026-09-01t12:00:00Z',
    '2026-09-01T12:00:00.500Z', '2026-09-01T14:00:00+02:00',
    ' 2026-09-01T12:00:00Z ',
])
def test_accepted_timestamp_survives_actual_state_roundtrip(tmp_path, stamp):
    from fl4write.timestamps import parse_iso

    st = {'version': 1, 'prs': {}, 'retro_cursor': stamp}
    state.advance_merged_watermark(st, stamp)
    assert parse_iso(stamp) is not None
    assert st['merged_since'] == stamp
    path = tmp_path / 'state.json'
    state.save_state(path, st)
    restored = state.load_state(path)
    assert restored['merged_since'] == stamp
    assert restored['retro_cursor'] == stamp


@pytest.mark.parametrize('stamp', ['garbage', '2026-09-01', '2026-09-01T12:00:00', None, 12])
def test_invalid_timestamp_cannot_become_persisted_cursor(tmp_path, stamp):
    from fl4write.timestamps import parse_iso

    st = {'version': 1, 'prs': {}}
    state.advance_merged_watermark(st, stamp)
    assert 'merged_since' not in st
    assert parse_iso(stamp) is None
    st.update(merged_since=stamp, retro_cursor=stamp)
    path = tmp_path / 'state.json'
    state.save_state(path, st)
    restored = state.load_state(path)
    assert restored.get('merged_since') is None
    assert restored.get('retro_cursor') is None


def adapter(kind, stamps):
    forge = kind(ForgeBinding(role='primary', api_base='https://example.test', token_env='UNUSED'))
    forge._paginated = lambda *a, **k: [
        {'number': n, 'merged': True, 'merged_at': stamp, 'head': {'sha': str(n) * 40},
         'title': 'Review', 'user': {'login': 'author'}}
        for n, stamp in enumerate(stamps, 1)]
    return forge


@pytest.mark.parametrize('kind', [GitHubAdapter, ForgejoAdapter])
@pytest.mark.parametrize('stamps', [
    ['2026-09-01T12:00:00Z', '2026-09-01T12:00:00.500Z'],
    ['2026-09-01T12:00:00.1Z', '2026-09-01T12:00:00.100001Z'],
    ['2026-09-01T13:00:00+02:00', '2026-09-01T12:00:00Z'],
])
def test_actual_adapters_capped_postmerge_reviews_every_pr(tmp_path, monkeypatch, kind, stamps):
    forge = adapter(kind, stamps)
    config = make_config(post_merge={'enabled': True, 'max_per_cycle': 1})
    st = {'prs': {}, 'merged_since': '2026-09-01T00:00:00Z'}  # time-rot-safe: stamps are compared pairwise (fractional/tz spellings), never to now()
    reviewed = []

    def review(pr, *args, **kwargs):
        reviewed.append(pr.number)
        state.mark_reviewed(st, pr.number, pr.head_sha, 'reviewed:0')
        return 'reviewed'

    monkeypatch.setattr(engine, '_review_pr', review)
    for _ in range(2):
        engine._post_merge_sweep(config, forge, tmp_path / 'state.json', lambda p: None,
                                 None, st, engine.CycleReport(repo=config.repo), False, None)
    assert reviewed == [1, 2]
    assert st['merged_since'] == stamps[1]


@pytest.mark.parametrize('current,incoming,expected', [
    ('2026-09-01T12:00:00Z', '2026-09-01T12:00:00.5Z', '2026-09-01T12:00:00.5Z'),
    ('2026-09-01T12:00:00Z', '2026-09-01T13:00:00+02:00', '2026-09-01T12:00:00Z'),
    ('2026-09-01T12:00:00Z', '2026-09-01T14:00:00+02:00', '2026-09-01T12:00:00Z'),
    ('2026-09-01T12:00:00Z', 'garbage', '2026-09-01T12:00:00Z'),
    ('garbage', '2026-09-01T12:00:00Z', '2026-09-01T12:00:00Z'),
    (None, 'garbage', None),
])
def test_watermark_advances_by_instant_only(current, incoming, expected):
    st = {'merged_since': current}
    state.advance_merged_watermark(st, incoming)
    assert st['merged_since'] == expected


@pytest.mark.parametrize('kind', [GitHubAdapter, ForgejoAdapter])
def test_equivalent_instants_remain_visible_with_malformed_sibling(kind):
    stamps = ['2026-09-01T12:00:00Z', 'invalid', '2026-09-01T14:00:00+02:00']
    assert [p.number for p in adapter(kind, stamps).list_merged_prs(
        'owner/repo', '2026-09-01T12:00:00.000Z')] == [1, 3]


@pytest.mark.parametrize('kind', [GitHubAdapter, ForgejoAdapter])
def test_retro_cursor_capped_cycles_use_instant_order(tmp_path, monkeypatch, kind):
    date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%d')
    stamps = [date + 'T12:00:00Z', date + 'T12:00:00.500Z']
    forge = adapter(kind, stamps)
    config = make_config(retro_audit={'enabled': True, 'max_per_cycle': 1})
    st = {'prs': {}, 'merged_since': date + 'T14:00:00+02:00',
          'retro_cursor': date + 'T12:00:01Z'}
    reviewed = []

    def review(pr, *args):
        reviewed.append(pr.number)
        state.mark_reviewed(st, pr.number, pr.head_sha, 'reviewed:0')
        return 'reviewed'

    monkeypatch.setattr(engine, '_retro_review_pr', review)
    for _ in range(2):
        engine._retro_sweep(config, forge, tmp_path / 'state.json', lambda p: None,
                            None, st, engine.CycleReport(repo=config.repo), None)
    assert reviewed == [2, 1]
    assert st['retro_cursor'] == stamps[0]


@pytest.mark.parametrize('cursor', ['T14:00:00+02:00', 'invalid'])
def test_retro_offset_and_invalid_cursor_preserve_valid_work(tmp_path, monkeypatch, cursor):
    date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%d')
    forge = adapter(GitHubAdapter, [date + 'T12:00:00Z'])
    config = make_config(retro_audit={'enabled': True})
    st = {'prs': {}, 'merged_since': date + 'T12:00:00Z',
          'retro_cursor': date + cursor if cursor.startswith('T') else cursor}
    reviewed = []

    def review(pr, *args):
        reviewed.append(pr.number)
        return 'reviewed'

    monkeypatch.setattr(engine, '_retro_review_pr', review)
    engine._retro_sweep(config, forge, tmp_path / 'state.json', lambda p: None,
                        None, st, engine.CycleReport(repo=config.repo), None)
    assert reviewed == [1]
    from fl4write.timestamps import parse_iso
    assert parse_iso(st['retro_cursor']) == parse_iso(date + 'T12:00:00Z')


def test_retro_invalid_timestamp_blocks_completion_without_hiding_valid_sibling(tmp_path, monkeypatch):
    date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%d')
    forge = adapter(GitHubAdapter, [date + 'T12:00:00Z', date + 'T12:00:00Z'])
    rows = forge.list_merged_prs('owner/repo', date + 'T00:00:00Z')
    rows[1].merged_at = 'invalid'
    forge.list_merged_prs = lambda *a: rows
    config = make_config(retro_audit={'enabled': True})
    st = {'prs': {}, 'merged_since': date + 'T12:00:00Z'}
    reviewed = []
    monkeypatch.setattr(engine, '_retro_review_pr', lambda pr, *a: reviewed.append(pr.number) or 'reviewed')
    for _ in range(2):
        report = engine.CycleReport(repo=config.repo)
        engine._retro_sweep(config, forge, tmp_path / 'state.json', lambda p: None,
                            None, st, report, None)
        assert report._merged_listing_incomplete
    assert reviewed == [1]
    assert not st.get('retro_complete')
