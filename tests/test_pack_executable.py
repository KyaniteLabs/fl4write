"""Archived source retains the executable behavior recorded in Git."""
import stat
import subprocess

from fl4write import exhaustive
from test_exhaustive import _git, _repo


def test_packed_tracked_helper_executes_without_writable_source(tmp_path):
    repo = _repo(tmp_path)
    helper = repo / 'helper.sh'
    helper.write_text('#!/bin/sh\nprintf "ready\\n"\n')
    helper.chmod(0o755)
    _git(repo, 'add', 'helper.sh')
    _git(repo, 'commit', '-qm', 'add executable fixture')
    tree, manifest = exhaustive._pack(
        repo, _git(repo, 'rev-parse', 'HEAD'), tmp_path / 'packed')
    result = subprocess.run([str(tree / 'helper.sh')], cwd=tree,
                            capture_output=True, text=True, check=True)
    assert result.stdout == 'ready\n'
    assert stat.S_IMODE((tree / 'helper.sh').stat().st_mode) == 0o500
    assert stat.S_IMODE((tree / 'value.py').stat().st_mode) == 0o400
    assert stat.S_IMODE(tree.stat().st_mode) == 0o500
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o400
