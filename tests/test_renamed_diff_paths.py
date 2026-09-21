"""Complete Git rename metadata identifies destinations with ambiguous headers."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from fl4write import analyzer, cli, engine
from fl4write.config import ForgeBinding
from fl4write.forges import ForgejoAdapter
from fl4write.models import PullRequest
from test_quality_tranche import make_config


def real_diff(tmp_path, old, new, mode):
    env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
               GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_EMAIL='fixture@example.invalid')

    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, env=env, text=True,
                              capture_output=True, check=True).stdout

    git('init', '-q')
    source = b'def value():\n    return 1\n' + b'# unchanged context\n' * 20
    if mode in ('binary', 'binary-modify'):
        source += b'\0binary\n'
    original = tmp_path / old
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(source)
    git('add', '--', old)
    git('commit', '-qm', 'fixture')
    if mode == 'delete':
        original.unlink()
    else:
        destination = tmp_path / new
        destination.parent.mkdir(parents=True, exist_ok=True)
        original.rename(destination)
        if mode == 'edit':
            destination.write_bytes(source.replace(b'return 1', b'return 0'))
        elif mode == 'binary-modify':
            destination.write_bytes(source + b'new binary bytes\0')
    git('add', '-A')
    return git('diff', '--cached', '-M')


@pytest.mark.parametrize('old,new,mode', [
    ('old.py', 'dir b/new.py', 'edit'),
    ('old b/name.py', 'dir b/new b/name.py', 'edit'),
    ('old.py', 'dir b/new.py', 'pure'),
    ('old.py', 'café b/new.py', 'edit'),
    ('old.py', 'quote" b/new.py', 'edit'),
    ('old.py', 'tab\t b/new.py', 'edit'),
    ('old.py', ' leading b/new.py ', 'edit'),
    ('same b/name.py', 'same b/name.py', 'edit'),
    ('old.py', 'dir b/new.bin', 'binary'),
    ('dir b/old.py', None, 'delete'),
    ('café b/deleted.py', None, 'delete'),
    ('café b/data.bin', 'café b/data.bin', 'binary-modify'),
])
def test_actual_git_block_identity_reaches_both_readers_and_grounding(tmp_path, monkeypatch, old, new, mode):
    diff = real_diff(tmp_path, old, new, mode)
    expected = old if mode == 'delete' else new
    config = make_config(gatekeeper=False, shadow=False, verify_tests=False)
    pr = PullRequest(forge='github', repo=config.repo, number=1, head_sha='a' * 40)
    forgejo = ForgejoAdapter(ForgeBinding(role='primary', api_base='https://forge.example.invalid/api/v1',
                                         token_env='UNUSED'))
    monkeypatch.setattr(cli, '_gh', lambda *a: diff)
    monkeypatch.setattr(forgejo, '_call_text', lambda *a, **k: diff)
    github_files, text = cli.make_get_diff(config.repo)(pr)
    forgejo_files, _ = forgejo.get_pr_diff(config.repo, 1)
    assert github_files == forgejo_files == {expected}
    assert set(analyzer._diff_path_texts(diff)) == {expected}
    assert set(analyzer._diff_line_spans(diff)) == {expected}
    if mode != 'edit':
        return
    finding = {'rule_id': 'general', 'severity': 'Major', 'path': expected, 'line': 2,
               'message': 'Returning zero breaks the API result for input one.', 'category': 'api-validation'}
    monkeypatch.setattr(analyzer, '_call_model', lambda *a, **k: json.dumps({'findings': [finding]}))
    monkeypatch.setattr('fl4write.telemetry.emit', lambda *a, **k: None)
    monkeypatch.setattr('fl4write.telemetry.record_route', lambda *a, **k: None)
    reviewed = analyzer.analyze(pr, github_files, text, config)
    assert [f.path for f in reviewed.findings] == [expected]
    comments = []
    primary = SimpleNamespace(name='github', get_persistent_comment=lambda *a: None,
                              create_comment=lambda repo, number, body: comments.append(body))
    st = {'version': 1, 'prs': {}}
    outcome = engine._review_pr(pr, config, primary, lambda p: (github_files, text), None, st,
                               engine.CycleReport(repo=config.repo), run_fixes=False)
    assert outcome == 'reviewed'
    assert len(analyzer._diff_line_spans(diff)[expected]) == 1
    assert 'Go merge it.' not in comments[0]
    assert 'Returning zero breaks' in comments[0]
