import json
import subprocess

import pytest

from fl4write import exhaustive_sandbox as sandbox


IMAGE = "sha256:" + "a" * 64


@pytest.mark.parametrize("image", [None, "python:latest", "sha256:short"])
def test_runtime_requires_immutable_image_identity(tmp_path, image):
    with pytest.raises(sandbox.SandboxUnavailable, match="immutable"):
        sandbox.container_command(["pytest", "{junit}"], tmp_path, image, "fixture", 30)


def test_container_has_only_readonly_source_mount_and_bounded_private_outputs(tmp_path):
    argv = sandbox.container_command(["python3", "-m", "pytest", "--junitxml", "{junit}"],
                                     tmp_path, IMAGE, "fixture", 30)
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv.count("--mount") == 1
    assert argv[argv.index("--mount") + 1] == f"type=bind,src={tmp_path.resolve()},dst=/work,readonly"
    assert any(arg.startswith("/evidence:") and "size=16777216" in arg for arg in argv)
    # Test runners may generate helper executables; Docker defaults tmpfs to noexec.
    assert any(arg.startswith("/tmp:") and "exec" in arg.split(":", 1)[1].split(",") for arg in argv)
    assert any(arg.startswith("/control:") and "mode=0700" in arg for arg in argv)
    assert json.loads(argv[-1])[-1] == "/evidence/report.xml"


def test_completed_report_is_copied_after_supervisor_and_owned_container_is_removed(tmp_path, monkeypatch):
    calls = []
    report = b'<testsuite><testcase name="test_ok"/></testsuite>'
    def docker(argv, timeout, binary=False):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            output = IMAGE + " 1\n"
        elif "wait" in argv:
            output = json.dumps({"kind": "completed", "returncode": 0})
        elif "report" in argv:
            output = report
        else:
            output = "container\n"
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(sandbox, "_docker", docker)
    result = sandbox.run_isolated(["pytest", "{junit}"], tmp_path, tmp_path / "result.xml", 30, IMAGE)
    assert result.returncode == 0 and (tmp_path / "result.xml").read_bytes() == report
    name = calls[1][calls[1].index("--name") + 1]
    assert calls[-1] == ["docker", "rm", "--force", name]


def test_runner_timeout_retains_no_false_test_report_and_removes_container(tmp_path, monkeypatch):
    calls = []
    def docker(argv, timeout, binary=False):
        calls.append(argv)
        output = IMAGE + " 1" if argv[1:3] == ["image", "inspect"] else "container"
        if "wait" in argv:
            output = json.dumps({"kind": "deferred", "reason": "test process timed out"})
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(sandbox, "_docker", docker)
    with pytest.raises(sandbox.SandboxUnavailable, match="timed out"):
        sandbox.run_isolated(["pytest", "{junit}"], tmp_path, tmp_path / "result.xml", 30, IMAGE)
    assert not (tmp_path / "result.xml").exists()
    assert calls[-1][1:3] == ["rm", "--force"]
