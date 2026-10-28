import os
import subprocess
import tempfile

from fl4write import exhaustive
from fl4write import exhaustive_core


def test_git_uses_contained_runner_and_hardened_environment(monkeypatch, tmp_path):
    call = {}

    def fake_run(argv, cwd, timeout, env, new_session=False):
        call.update(
            argv=argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            new_session=new_session,
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=" clean\n",
            stderr="",
        )

    monkeypatch.setenv("FL4WRITE_TEST_SECRET", "must-not-be-forwarded")
    monkeypatch.setattr(exhaustive_core, "_run", fake_run)

    assert exhaustive._git(tmp_path, "status", "--short") == "clean"
    assert call["argv"] == [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "status",
        "--short",
    ]
    assert call["cwd"] == tmp_path
    assert call["timeout"] == 120
    assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert call["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert call["env"]["GIT_CONFIG_SYSTEM"] == os.devnull
    assert call["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert call["env"]["HOME"] == tempfile.gettempdir()
    assert "FL4WRITE_TEST_SECRET" not in call["env"]
