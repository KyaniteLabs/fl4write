"""Malformed identities defer intake without losing older or newer work."""
from types import SimpleNamespace

import pytest

from fl4write import issues
from fl4write.forges import ForgeError


@pytest.mark.parametrize('fallback', [False, True], ids=['primary', 'fallback'])
@pytest.mark.parametrize('bad_row', [None, {}, {'number': '5'}, {'number': True},
                                   {'number': 0}, {'number': -5}],
                         ids=['non-row', 'missing', 'string', 'bool', 'zero', 'negative'])
def test_malformed_row_defers_mixed_listing_until_recovery(monkeypatch, fallback, bad_row):
    class Forge:
        rows = [bad_row, {'number': 11}]
        calls = 0
        comments = []

        def _paginated(self, path, page_size=50):
            if path.endswith('/comments'):
                return []
            if fallback:
                raise ForgeError('use fallback')
            return self.rows

        def _call(self, method, path):
            assert method == 'GET' and 'state=open' in path
            self.calls += 1
            return self.rows

        def create_comment(self, repo, number, body):
            self.comments.append(number)

    forge = Forge()
    config = SimpleNamespace(repo='owner/repo', shadow=False, bot_login='fl4write')
    state = {'last_triaged_number': 10, 'issues_retry': [5]}
    triaged = []

    def triage(issue, config):
        triaged.append(issue['number'])
        return {'labels': [], 'draft_reply': 'Checking', 'urgency': 'low'}

    monkeypatch.setattr(issues, 'triage_issue', triage)
    listing = issues.collect_new_issues(forge, config.repo, 10, retry={5})
    assert list(listing) == [{'number': 11}]
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 0
    assert state == {'last_triaged_number': 10, 'issues_retry': [5]}
    assert triaged == [] and forge.comments == []
    assert forge.calls == (2 if fallback else 0)

    forge.rows = [{'number': 5}, {'number': 11}]
    result = issues.run_issues_cycle(config, state, forge)
    assert result['triaged'] == 2
    assert state == {'last_triaged_number': 11, 'issues_retry': []}
    assert triaged == [5, 11] and forge.comments == [5, 11]
