"""Resolved metrics follow the renderer's variable-width code fences."""
from types import SimpleNamespace

import pytest

from fl4write import metrics, renderer
from fl4write.models import Finding
from test_fl4write import make_config, make_pr


@pytest.mark.parametrize('path', ['normal.py', 'tick`name.py', 'tick``name.py', 'tick```name.py'])
def test_actual_rendered_resolution_counts_in_comment_and_snapshot(path):
    config, pr = make_config(), make_pr()
    finding = Finding(rule_id='general', severity='Major', path=path, line=2, message='Incorrect result')
    first = renderer.render_review(pr, [finding], config, 'first')
    previous = [Finding(severity=severity, path=parsed_path, line=line, rule_id=rule, message='previous')
                for severity, parsed_path, line, rule in renderer.parse_finding_lines(first)]
    assert len(previous) == 1
    resolved = renderer.render_review(pr, [], config, 'second', previous_findings=previous)
    forge = SimpleNamespace(get_persistent_comment=lambda *a: (7, resolved),
                            list_open_prs=lambda *a: [pr])
    assert metrics.comment_signals(forge, pr.repo, pr.number) == {
        'findings': 0, 'resolved': 1, 'reactions': 0, 'addressed': 1,
    }
    assert metrics.acceptance_snapshot(forge, config) == {'total': 1, 'addressed': 1, 'rate': '100%'}


@pytest.mark.parametrize('body', [
    'Message quotes - ✅ `~normal.py:2`',
    '> - ✅ ``~tick`name.py:2``',
    ' - ✅ ``~tick`name.py:2``',
    '- ✅ ``tick`name.py:2``',
    '- ✅ ~normal.py:2',
    '- ✅ `\n~normal.py:2`',
])
def test_non_marker_and_near_match_lines_do_not_count(body):
    forge = SimpleNamespace(get_persistent_comment=lambda *a: (7, body))
    assert metrics.comment_signals(forge, 'owner/repo', 1)['resolved'] == 0


@pytest.mark.parametrize('existing', [None, (7, None), (7,), {'body': 'not a comment tuple'}])
def test_malformed_comment_envelopes_keep_existing_degradation(existing):
    forge = SimpleNamespace(get_persistent_comment=lambda *a: existing)
    assert metrics.comment_signals(forge, 'owner/repo', 1) is None
