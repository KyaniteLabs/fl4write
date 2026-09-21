"""Ordinary Git filenames retain identity through proof and changed-path checks."""
import os
import subprocess

import pytest

from fl4write import exhaustive_fix as repair
from test_exhaustive_fix import _patch, _verify


@pytest.mark.parametrize('name', ['café.py', 'a"quote.py', ' leading space.py', 'line\r\nbreak.py'])
def test_real_proof_and_changed_paths_preserve_git_filenames(tmp_path, name):
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
               GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_EMAIL='fixture@example.invalid')

    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    git('init', '-q')
    (repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
    (repo / 'tests').mkdir()
    (repo / 'tests/test_old.py').write_text('def test_old(): assert True\n')
    (repo / name).write_text('VALUE = 1\n')
    git('add', '--', '.')
    git('commit', '-qm', 'fixture')
    head = git('rev-parse', 'HEAD')
    fixed, ids, digest, baseline = repair._prove_patch(
        repo, head, *_patch(), ['pytest'], tmp_path / 'evidence', _verify,
        work_root=tmp_path / 'proof',
    )
    assert baseline < ids
    assert len(digest) == 64
    assert (fixed / name).read_text() == 'VALUE = 1\n'
    (fixed / name).write_text('VALUE = 2\n')
    new = 'new-' + name
    (fixed / new).write_text('NEW = 1\n')
    assert repair._changed_paths(fixed) == set(_patch()[0]) | {name, new}
