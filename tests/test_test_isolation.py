"""Executed test code runs isolated or not at all (2026-10-12 review).

A PR's own tests (verify gate) and model-written fixes (fix lane) are
untrusted code. As the runner user they can read every host secret by
absolute path, so the default routes them through the reviewed Docker
runtime (fl4write.exhaustive_sandbox) and refuses to run without an
immutable image. FL4WRITE_TEST_ISOLATION=host is the explicit opt-out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fl4write import exhaustive_sandbox, executor
from fl4write.models import Finding, PullRequest
from test_fl4write import make_config

IMAGE = "sha256:" + "a" * 64
GREEN = ('<?xml version="1.0"?><testsuites><testsuite tests="2" failures="0" '
         'errors="0"/></testsuites>')


def _no_host_run(*args, **kwargs):
    raise AssertionError("executed code must never run on the host here")


@pytest.mark.default_isolation
class TestPolicy:
    def test_default_without_image_is_unavailable(self):
        mode, detail = executor._test_isolation()
        assert mode == "unavailable" and "FL4WRITE_TEST_IMAGE" in detail

    @pytest.mark.parametrize("image", ["", "latest", "fl4write-test-runtime:v1",
                                       "sha256:" + "a" * 63, "sha256:" + "A" * 64])
    def test_mutable_or_malformed_image_is_unavailable(self, monkeypatch, image):
        monkeypatch.setenv("FL4WRITE_TEST_IMAGE", image)
        assert executor._test_isolation()[0] == "unavailable"

    def test_immutable_image_selects_container(self, monkeypatch):
        monkeypatch.setenv("FL4WRITE_TEST_IMAGE", f"  {IMAGE} ")
        assert executor._test_isolation() == ("container", IMAGE)

    def test_host_is_an_explicit_opt_out(self, monkeypatch):
        monkeypatch.setenv("FL4WRITE_TEST_ISOLATION", "HOST")
        assert executor._test_isolation() == ("host", "")

    def test_unknown_mode_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("FL4WRITE_TEST_ISOLATION", "none")
        assert executor._test_isolation()[0] == "unavailable"


@pytest.mark.default_isolation
class TestEvidenceRun:
    ARGV = ["python3", "-m", "pytest", "-q", "--", "tests/test_a.py"]

    def test_refuses_without_image_and_never_runs_on_host(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executor, "_run", _no_host_run)
        green, result = executor._pytest_evidence(self.ARGV, tmp_path, 60, {}, tmp_path / "r.xml")
        assert green is False and result.returncode == 125
        assert not (tmp_path / "r.xml").exists()

    def test_container_run_uses_supervisor_evidence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FL4WRITE_TEST_IMAGE", IMAGE)
        monkeypatch.setattr(executor, "_run", _no_host_run)
        seen = {}

        def fake_isolated(command, tree, evidence, timeout, image, **kw):
            seen.update(command=command, tree=tree, image=image)
            evidence.write_text(GREEN)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(exhaustive_sandbox, "run_isolated", fake_isolated)
        junit = tmp_path / "r.xml"
        green, _ = executor._pytest_evidence(self.ARGV, tmp_path, 60, {}, junit)
        assert green is True
        assert seen["image"] == IMAGE and seen["tree"] == tmp_path
        cmd = seen["command"]
        # standalone placeholder, before the path separator; no host path leaks in
        assert cmd.index("{junit}") == cmd.index("--junitxml") + 1 < cmd.index("--")
        assert str(junit) not in cmd

    def test_runtime_unavailable_is_a_closed_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FL4WRITE_TEST_IMAGE", IMAGE)
        monkeypatch.setattr(executor, "_run", _no_host_run)

        def broken(*a, **kw):
            raise exhaustive_sandbox.SandboxUnavailable("container runtime unavailable")

        monkeypatch.setattr(exhaustive_sandbox, "run_isolated", broken)
        junit = tmp_path / "r.xml"
        green, result = executor._pytest_evidence(self.ARGV, tmp_path, 60, {}, junit)
        assert green is False and result.returncode == 125 and not junit.exists()

    def test_cleanup_pending_keeps_the_run_verdict(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FL4WRITE_TEST_IMAGE", IMAGE)

        def finished_then_cleanup_failed(command, tree, evidence, timeout, image, **kw):
            evidence.write_text(GREEN)
            raise exhaustive_sandbox.SandboxCleanupPending(
                "cleanup pending", subprocess.CompletedProcess(command, 0, "", ""))

        monkeypatch.setattr(exhaustive_sandbox, "run_isolated", finished_then_cleanup_failed)
        green, _ = executor._pytest_evidence(self.ARGV, tmp_path, 60, {}, tmp_path / "r.xml")
        assert green is True


@pytest.mark.default_isolation
class TestLanesFailClosed:
    def test_fix_lane_refuses_before_any_fetch_or_model_call(self, monkeypatch):
        monkeypatch.setattr(executor, "_get_file_content", _no_host_run)
        monkeypatch.setattr(executor, "_call_model", _no_host_run)
        pr = PullRequest(forge="github", number=1, repo="o/r", head_sha="a" * 40)
        f = Finding(rule_id="secrets", severity="Critical", path="x.py", line=1, message="m")
        result = executor.attempt_fix(pr, f, make_config(fix={"enabled": True, "merge_own_prs": False}))
        assert result["status"] == "error" and "isolation unavailable" in result["reason"]

    def test_verify_is_unverified_before_any_git_work(self, monkeypatch):
        monkeypatch.setattr(executor, "_run", _no_host_run)
        pr = PullRequest(forge="github", number=1, repo="o/r", head_sha="a" * 40)
        assert executor.verify_diff_tests(pr, make_config(), ["tests/test_a.py"]) is None


def test_runtime_image_ships_the_worker_the_sandbox_execs():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "tools/exhaustive-runtime/Dockerfile").read_text()
    assert exhaustive_sandbox.WORKER in dockerfile
    assert 'org.fl4write.test-runtime="1"' in dockerfile  # validate_runtime's label
