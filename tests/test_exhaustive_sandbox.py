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
    assert "--init" in argv  # Reap orphaned test descendants without raising the process cap.
    assert argv[argv.index("--pids-limit") + 1] == "256"
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


@pytest.mark.parametrize("timed_out", [False, True])
def test_failed_cleanup_defers_and_retains_owned_recovery_identity(tmp_path, monkeypatch, timed_out):
    calls = []
    def docker(argv, timeout, binary=False):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            output = IMAGE + " 1"
        elif "wait" in argv:
            output = json.dumps({"kind": "deferred"} if timed_out else
                                {"kind": "completed", "returncode": 0})
        elif "report" in argv:
            output = b'<testsuite><testcase name="ok"/></testsuite>'
        else:
            output = "container"
        return subprocess.CompletedProcess(argv, int(argv[1:3] == ["rm", "--force"]), output, "")
    monkeypatch.setattr(sandbox, "_docker", docker)
    with pytest.raises(sandbox.SandboxUnavailable, match="cleanup remains unresolved") as caught:
        sandbox.run_isolated(["pytest", "{junit}"], tmp_path, tmp_path / "result.xml", 30, IMAGE)
    saved = json.loads((tmp_path / "result.xml.container.json").read_text())
    assert saved["container"] == calls[1][calls[1].index("--name") + 1]
    assert saved["image"] == IMAGE
    if timed_out:
        assert "timed out" in str(caught.value)
        assert not (tmp_path / "result.xml").exists()


# ---- P2b: the retained record must be consumable by the next run -------------

def test_retained_recovery_record_is_consumed_so_the_next_run_proceeds(tmp_path, monkeypatch):
    """P2b: a retained cleanup record must be RESOLVED on the next run at the
    same evidence path — the exclusive create alone wedged every retry."""
    evidence = tmp_path / "fixed-green.xml"
    containers: set[str] = set()
    fail_rm = {"on": True}
    calls: list[list[str]] = []
    report = b'<testsuite><testcase name="ok"/></testsuite>'

    def docker(argv, timeout, binary=False):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, IMAGE + " 1\n", "")
        if argv[1:3] == ["inspect", "--type"]:
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in containers else 1, "", "")
        if argv[1] == "run":
            containers.add(argv[argv.index("--name") + 1])
            return subprocess.CompletedProcess(argv, 0, "container\n", "")
        if argv[1:3] == ["rm", "--force"]:
            if fail_rm["on"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            containers.discard(argv[3])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "wait" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"kind": "completed", "returncode": 0}), "")
        if "report" in argv:
            return subprocess.CompletedProcess(argv, 0, report, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox, "_docker", docker)
    with pytest.raises(sandbox.SandboxUnavailable, match="cleanup remains unresolved"):
        sandbox.run_isolated(["pytest", "{junit}"], tmp_path, evidence, 30, IMAGE)
    record = json.loads((tmp_path / "fixed-green.xml.container.json").read_text())
    assert record["state"] == "cleanup_required"
    assert record["container"] in containers  # the owned container is really still there

    fail_rm["on"] = False
    result = sandbox.run_isolated(["pytest", "{junit}"], tmp_path, evidence, 30, IMAGE)
    assert result.returncode == 0 and evidence.read_bytes() == report
    assert record["container"] not in containers  # the retained container was removed


def test_recovery_record_for_an_absent_container_is_released(tmp_path, monkeypatch):
    """An owned container that is already gone satisfies the cleanup
    obligation: the stale record must be released, not wedge the path."""
    record = tmp_path / "r.xml.container.json"
    record.write_text(json.dumps({"container": "fl4write-test-" + "a" * 32,
                                  "image": IMAGE, "state": "cleanup_required"}))
    calls: list[list[str]] = []

    def docker(argv, timeout, binary=False):
        calls.append(argv)
        if argv[1:3] == ["inspect", "--type"]:
            return subprocess.CompletedProcess(argv, 1, "", "")  # container is gone
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox, "_docker", docker)
    sandbox._consume_prior_recovery(record)
    assert not record.exists()
    assert not any(argv[1:3] == ["rm", "--force"] for argv in calls)


def test_recovery_record_naming_a_foreign_container_is_refused(tmp_path, monkeypatch):
    """Only names this module generates may reach `docker rm --force`."""
    record = tmp_path / "r.xml.container.json"
    record.write_text(json.dumps({"container": "some-other-container",
                                  "image": IMAGE, "state": "cleanup_required"}))
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox, "_docker",
                        lambda argv, timeout, binary=False: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    with pytest.raises(sandbox.SandboxUnavailable, match="no removable container"):
        sandbox._consume_prior_recovery(record)
    assert record.exists() and calls == []


# ---- P2c: a cleanup failure must not erase a finished run's verdict ----------

def _completed_then_failed_cleanup(report: bytes, returncode: int):
    def docker(argv, timeout, binary=False):
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, IMAGE + " 1\n", "")
        if argv[1:3] == ["rm", "--force"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if "wait" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"kind": "completed", "returncode": returncode}), "")
        if "report" in argv:
            return subprocess.CompletedProcess(argv, 0, report, "")
        return subprocess.CompletedProcess(argv, 0, "container\n", "")
    return docker


def test_completed_run_with_unresolved_cleanup_carries_its_result(tmp_path, monkeypatch):
    """The cleanup failure travels WITH the finished result instead of
    replacing it."""
    evidence = tmp_path / "suite.xml"
    monkeypatch.setattr(sandbox, "_docker", _completed_then_failed_cleanup(
        b'<testsuite><testcase classname="t" name="test_x"><failure/></testcase></testsuite>', 1))
    with pytest.raises(sandbox.SandboxCleanupPending) as caught:
        sandbox.run_isolated(["pytest", "{junit}"], tmp_path, evidence, 30, IMAGE)
    assert caught.value.result.returncode == 1
    assert evidence.is_file()
    assert "cleanup remains unresolved" in str(caught.value)


def test_red_suite_with_unresolved_cleanup_stays_non_green(tmp_path, monkeypatch):
    """P2c: the cleanup failure must not reclassify a red suite as a deferral —
    a non-green round must still be recorded."""
    from fl4write import exhaustive as ex

    evidence = tmp_path / "suite.xml"
    monkeypatch.setattr(sandbox, "_docker", _completed_then_failed_cleanup(
        b'<testsuite><testcase classname="t" name="test_x"><failure/></testcase></testsuite>', 1))
    with pytest.raises(ex.NonGreen, match="not green") as caught:
        ex._test(["pytest", "{junit}"], tmp_path, evidence, 30, isolation="docker", image=IMAGE)
    assert "cleanup remains unresolved" in str(caught.value)


def test_green_suite_with_unresolved_cleanup_defers_without_certifying(tmp_path, monkeypatch):
    """The lenient half of the rule: unresolved cleanup still blocks a green
    certification (only the verdict's CLASSIFICATION is preserved)."""
    from fl4write import exhaustive as ex

    evidence = tmp_path / "suite.xml"
    monkeypatch.setattr(sandbox, "_docker", _completed_then_failed_cleanup(
        b'<testsuite><testcase classname="t" name="test_x"/></testsuite>', 0))
    with pytest.raises(ex.Deferred, match="cleanup remains unresolved"):
        ex._test(["pytest", "{junit}"], tmp_path, evidence, 30, isolation="docker", image=IMAGE)


# ---- 2026-09-16 adversarial Arch-1: evidence frozen before host readback ------

def test_embedded_supervisor_report_is_preferred_over_post_exit_readback(tmp_path, monkeypatch):
    """The JUnit bytes must arrive EMBEDDED in the root-owned supervisor
    status (snapshot taken after the test process group is killed), never
    via a post-exit read of the 1777 evidence tmpfs a surviving child can
    still write. The separate `report` exec is legacy-runtime only."""
    import base64
    calls = []
    report = b'<testsuite><testcase name="frozen"/></testsuite>'
    def docker(argv, timeout, binary=False):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            output = IMAGE + " 1\n"
        elif "wait" in argv:
            output = json.dumps({"kind": "completed", "returncode": 0,
                                 "report_b64": base64.b64encode(report).decode()})
        else:
            output = "container\n"
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(sandbox, "_docker", docker)
    result = sandbox.run_isolated(["pytest", "{junit}"], tmp_path, tmp_path / "r.xml", 30, IMAGE)
    assert result.returncode == 0
    assert (tmp_path / "r.xml").read_bytes() == report
    assert not any("report" in a for a in calls), (
        "host re-read /evidence after exit — the forgery window Arch-1 closed is open again"
    )


def test_corrupt_embedded_report_fails_closed(tmp_path, monkeypatch):
    def docker(argv, timeout, binary=False):
        if argv[1:3] == ["image", "inspect"]:
            output = IMAGE + " 1\n"
        elif "wait" in argv:
            output = json.dumps({"kind": "completed", "returncode": 0,
                                 "report_b64": "!!not-base64!!"})
        else:
            output = "container\n"
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(sandbox, "_docker", docker)
    with pytest.raises(sandbox.SandboxUnavailable, match="base64"):
        sandbox.run_isolated(["pytest", "{junit}"], tmp_path, tmp_path / "r.xml", 30, IMAGE)
    assert not (tmp_path / "r.xml").exists()


def test_worker_kills_group_on_success_and_snapshots_report(tmp_path):
    """Contract pin on the container supervisor source: the process-group
    kill lives in the FINALLY of the wait (every exit path, success
    included) and the report snapshot is embedded into the ROOT-OWNED
    status document — the two halves of the Arch-1 closure."""
    from pathlib import Path as _P
    src = (_P(sandbox.__file__).resolve().parents[1]
           / "tools" / "exhaustive-runtime" / "worker.py").read_text()
    assert "os.killpg(process.pid, signal.SIGKILL)" in src
    # the kill is inside a finally attached to the wait, not only the
    # timeout branch (which is where it lived pre-fix)
    start = src.index("process.wait(timeout=timeout)")
    wait_block = src[start:src.index("except OSError:", start)]
    assert "finally:" in wait_block and "killpg" in wait_block, (
        "group kill must cover the SUCCESS path, not just timeouts"
    )
    assert 'result["report_b64"] = embedded' in src
    assert "STATUS" in src and "mode=0700" not in src  # status path is /control
