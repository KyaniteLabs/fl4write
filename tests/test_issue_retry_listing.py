"""Incomplete listings retain failed work until recovery proves its status."""
from types import SimpleNamespace

import pytest

from fl4write import issues
from fl4write.forges import ForgeError


class ListingForge:
    def __init__(self, failure):
        self.failure = failure
        self.pages = 0
        self.created = []
        self.rows = []

    def _paginated(self, path, page_size=50):
        if path.endswith('/comments'):
            return []
        if self.failure:
            raise ForgeError('primary listing unavailable')
        return self.rows

    def _call(self, method, path):
        assert method == 'GET' and 'state=open' in path
        self.pages += 1
        if self.failure == 'outage':
            raise ForgeError('fallback unavailable')
        if self.failure == 'malformed':
            return {'unexpected': []}
        assert self.failure == 'cap'
        return [{'number': 100 + n} for n in range(100)]

    def create_comment(self, repo, number, body):
        self.created.append(number)


@pytest.mark.parametrize('failure', ['outage', 'malformed', 'cap'])
def test_incomplete_listing_preserves_retry_through_recovery(monkeypatch, failure):
    forge = ListingForge(failure)
    config = SimpleNamespace(repo='owner/repo', shadow=False, bot_login='fl4write')
    state = {'last_triaged_number': 10, 'issues_retry': [5]}
    triaged = []

    def triage(issue, config):
        triaged.append(issue['number'])
        return {'labels': [], 'draft_reply': 'Investigating', 'urgency': 'low'}

    monkeypatch.setattr(issues, 'triage_issue', triage)
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 0
    assert forge.pages == (10 if failure == 'cap' else 1)
    assert state == {'last_triaged_number': 10, 'issues_retry': [5]}
    assert triaged == [] and forge.created == []

    forge.failure = None
    forge.rows = [{'number': 5, 'title': 'Retry me', 'body': ''}]
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 1
    assert state == {'last_triaged_number': 10, 'issues_retry': []}
    assert triaged == [5] and forge.created == [5]


def test_complete_empty_listing_removes_closed_retry():
    forge = ListingForge(None)
    config = SimpleNamespace(repo='owner/repo', shadow=False, bot_login='fl4write')
    state = {'last_triaged_number': 10, 'issues_retry': [5]}
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 0
    assert state == {'last_triaged_number': 10, 'issues_retry': []}
