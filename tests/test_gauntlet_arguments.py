"""Exercise the real shell entry point without contacting a forge."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_gauntlet_clone_destination(tmp_path, refresh, explicit):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "argv.json"
    git = tools / "git"
    git.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['GAUNTLET_ARGV']).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.exit(73)\n"
    )
    git.chmod(0o755)
    automatic = tmp_path / "automatic"
    mktemp = tools / "mktemp"
    mktemp.write_text('#!/bin/sh\nprintf "%s\\n" "$GAUNTLET_TEMP"\n')
    mktemp.chmod(0o755)
    chosen = tmp_path / "explicit workdir"
    args = (["--refresh"] if refresh else []) + ["fixture/repo"]
    if explicit:
        args.append(str(chosen))
    # A matching nonempty relative directory must never become the default WD.
    occupied = tmp_path / "fixture" / "repo"
    occupied.mkdir(parents=True)
    (occupied / "owned.txt").write_text("preserve")
    script = Path(__file__).resolve().parents[1] / "make-gauntlet-pr.sh"
    result = subprocess.run(
        ["bash", str(script), *args], cwd=tmp_path, capture_output=True,
        env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
             "GAUNTLET_ARGV": str(log), "GAUNTLET_TEMP": str(automatic)},
    )
    assert result.returncode == 73
    assert json.loads(log.read_text()) == [
        "clone", "-q", "https://github.com/fixture/repo.git",
        str(chosen if explicit else automatic),
    ]
    assert (occupied / "owned.txt").read_text() == "preserve"
