"""Fallback intake honors each forge's actual page-size parameter."""
from urllib.parse import parse_qs, urlsplit

import pytest

from fl4write.forges import ForgejoAdapter, GitHubAdapter
from fl4write.issues import collect_new_issues


@pytest.mark.parametrize('adapter_type', [ForgejoAdapter, GitHubAdapter],
                         ids=['forgejo', 'github'])
def test_fallback_reads_all_pages_and_retains_old_retry(adapter_type):
    class Server(adapter_type):
        def __init__(self):
            self.sizes = []
            self.rows = [{'number': n} for n in range(600, 0, -1)]

        def _call(self, method, path):
            assert method == 'GET'
            query = parse_qs(urlsplit(path).query)
            page = int(query['page'][0])
            size = int(query.get(self.page_size_param, ['50'])[0])
            self.sizes.append(size)
            return self.rows[(page - 1) * size:page * size]

    forge = Server()
    result = collect_new_issues(forge, 'owner/repo', 500, retry={100})
    assert result.complete is True
    assert [row['number'] for row in result] == [100] + list(range(501, 601))
    assert forge.sizes == [50] * 10 + [100] * 7
