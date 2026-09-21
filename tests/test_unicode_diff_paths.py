import pytest

from fl4write.analyzer import _git_diff_path


@pytest.mark.parametrize('path', ['café.py', '東京.py'], ids=['latin', 'cjk'])
def test_literal_unicode_path_is_preserved(path):
    assert _git_diff_path(f'diff --git a/{path} b/{path}') == path
