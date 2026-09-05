"""Unicode filenames survive both byte escape decoding and finding grounding."""
import json

import pytest

from fl4write import analyzer, cli
from fl4write.models import PullRequest
from test_quality_tranche import make_config


@pytest.mark.parametrize('token, expected', [
    ('café.py', 'café.py'), ('東京.py', '東京.py'),
    (r'東京-caf\303\251.py', '東京-café.py'),
], ids=['quoted-latin', 'quoted-cjk', 'mixed-literal-octal'])
def test_quoted_unicode_keeps_literal_and_escaped_bytes(token, expected):
    assert analyzer._git_diff_path(f'diff --git "a/{token}" "b/{token}"') == expected


@pytest.mark.parametrize('path', ['café.py', '東京.py'], ids=['latin', 'cjk'])
def test_unicode_diff_finding_survives_cli_extraction_and_grounding(monkeypatch, path):
    config = make_config(gatekeeper=False)
    pr = PullRequest(forge='github', repo=config.repo, number=1, head_sha='a' * 40)
    diff = (f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n'
            '@@ -1 +1 @@\n-return 1\n+return 0\n')
    response = json.dumps({'findings': [{
        'rule_id': 'general', 'severity': 'Major', 'path': path, 'line': 1,
        'message': 'Returning zero breaks the API result for input one.',
        'category': 'api-validation',
    }]})
    monkeypatch.setattr(cli, '_gh', lambda *args: diff)
    monkeypatch.setattr(analyzer, '_call_model', lambda *args, **kwargs: response)
    monkeypatch.setattr('fl4write.telemetry.emit', lambda *args, **kwargs: None)
    monkeypatch.setattr('fl4write.telemetry.record_route', lambda *args, **kwargs: None)
    files, text = cli.make_get_diff(config.repo)(pr)
    result = analyzer.analyze(pr, files, text, config)
    assert files == {path}
    assert [f.path for f in result.findings] == [path]
