"""Bounded retry state must never hide an open issue behind the watermark."""
from types import SimpleNamespace

import pytest

from fl4write import issues


class Forge:
    def __init__(self, numbers):
        self.numbers = list(numbers)
        self.foreign = set()
        self.posted = []

    def _paginated(self, path, page_size=50):
        if path.endswith('/comments'):
            number = int(path.split('/')[-2])
            if number in self.foreign:
                return [{'id': number, 'body': '<!-- fl4write-triage:v1 -->',
                         'user': {'login': 'another-bot'}}]
            return []
        return [{'number': n, 'title': 'Pending work', 'body': ''} for n in self.numbers]

    def create_comment(self, repo, number, body):
        self.posted.append(number)


def setup_cycle(monkeypatch, numbers, failed=()):
    forge = Forge(numbers)
    config = SimpleNamespace(repo='owner/repo', shadow=False, bot_login='fl4write')
    failed = set(failed)
    called = []

    def triage(issue, config):
        number = issue['number']
        called.append(number)
        if number in failed:
            return None
        return {'labels': [], 'draft_reply': 'Investigating', 'urgency': 'low'}

    monkeypatch.setattr(issues, 'triage_issue', triage)
    return forge, config, failed, called


@pytest.mark.parametrize('quarantine', [False, True])
def test_overflow_remains_discoverable_and_recovers_next_cycle(monkeypatch, quarantine):
    forge, config, failed, called = setup_cycle(monkeypatch, range(1, 203), range(1, 202))
    if quarantine:
        forge.foreign = failed.copy()
    state = {'last_triaged_number': 0}
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 1
    assert len(state['issues_retry']) <= 200
    pending = set(range(1, 202))
    visible = {row['number'] for row in issues.collect_new_issues(
        forge, config.repo, state['last_triaged_number'], set(state['issues_retry']))}
    assert pending <= visible
    if quarantine:
        assert len(state['issues_foreign_quarantined']) <= 200
        assert called == [202]
    failed.clear()
    forge.foreign.clear()
    called.clear()
    issues.run_issues_cycle(config, state, forge)
    assert pending <= set(called)
    assert state['issues_retry'] == []
    assert state['last_triaged_number'] == 202


@pytest.mark.parametrize('processed', [0, 2])
def test_deadline_preserves_preexisting_overflow_below_watermark(monkeypatch, processed):
    forge, config, failed, called = setup_cycle(monkeypatch, range(1, 203), range(1, 203))
    state = {'last_triaged_number': 500, 'issues_retry': list(range(1, 203))}
    ticks = iter([0] * processed + [10])
    monkeypatch.setattr(issues.time, 'monotonic', lambda: next(ticks))
    issues.run_issues_cycle(config, state, forge, deadline=10)
    assert len(called) == processed
    assert len(state['issues_retry']) <= 200
    failed.clear()
    called.clear()
    issues.run_issues_cycle(config, state, forge)
    assert set(called) == set(range(1, 203))
    assert state['issues_retry'] == []


@pytest.mark.parametrize('watermark', [0, 500])
def test_complete_listing_collects_closed_retries_before_compaction(monkeypatch, watermark):
    forge, config, failed, called = setup_cycle(monkeypatch, [202], [202])
    state = {'last_triaged_number': watermark, 'issues_retry': list(range(1, 203))}
    issues.run_issues_cycle(config, state, forge)
    assert state == {'last_triaged_number': watermark, 'issues_retry': [202]}
    forge.numbers.clear()
    issues.run_issues_cycle(config, state, forge)
    assert state == {'last_triaged_number': watermark, 'issues_retry': []}


def test_overflow_above_watermark_is_rediscovered_without_retry_ids(monkeypatch):
    forge, config, failed, called = setup_cycle(monkeypatch, range(1, 203), range(1, 203))
    state = {'last_triaged_number': 0}
    issues.run_issues_cycle(config, state, forge)
    assert state == {'last_triaged_number': 0, 'issues_retry': []}
    failed.clear()
    called.clear()
    issues.run_issues_cycle(config, state, forge)
    assert set(called) == set(range(1, 203))
    assert state == {'last_triaged_number': 202, 'issues_retry': []}


def test_shadow_does_not_truncate_preexisting_live_retry_state(monkeypatch):
    forge, config, failed, called = setup_cycle(monkeypatch, range(1, 203))
    config.shadow = True
    state = {'last_triaged_number': 500, 'issues_retry': list(range(1, 203))}
    original = {'last_triaged_number': 500, 'issues_retry': list(range(1, 203))}
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 202
    assert state == original
    assert forge.posted == []
