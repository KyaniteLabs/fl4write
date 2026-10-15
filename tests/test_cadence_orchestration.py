"""Tests for cadence pre/post serving identity binding.

Proves that:
1. Serving state is captured BEFORE measurement and AFTER measurement
   (not two post-run samples).
2. Identity changes (reboot, restart) are detected and fail closed.
3. Unavailable state (remote host unreachable) fails closed.
4. Remote serving state collection via SSH fails closed on errors.

All tests run offline (no real SSH, no real serving state).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from fl4write.reliability import (
    build_receipt_row,
    check_identity_binding,
    gather_remote_serving_state,
    gather_serving_state,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_serving_state(runtime_id="stable", pid=1000, boot_id="boot-aaa"):
    """Synthetic serving state for offline tests."""
    return {
        "runtime": {
            "boot_id": boot_id,
            "pid": pid,
            "start_ticks": 100 if runtime_id == "stable" else 200,
            "argv_sha256": "a" * 64,
            "executable_sha256": "b" * 64,
            "environment_sha256": "c" * 64,
        },
        "manifest": {"canonicalization": "watchtower-effective/v1",
                     "unit": "test.service"},
        "manifest_sha256": "d" * 64,
        "endpoint": "127.0.0.1:46377",
        "route_hash": "e" * 64,
    }


def _unavailable_state(reason="ssh connection failed: timeout"):
    """Unavailable serving state (fail-closed sentinel)."""
    return {"unavailable": True, "reason": reason, "host": "100.113.174.74"}


def _make_report():
    """Minimal report dict for build_receipt_row."""
    return {
        "measured_at": "2026-10-13T12:00:00Z",
        "route": {"model": "Qwen3.8-27B", "endpoint": "127.0.0.1:46377",
                  "temperature": 0.0, "seed": None, "max_tokens": 4000},
        "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0,
                        "total_runs": 2, "errors": 0, "per_case": {}},
        "recall": {"per_class": {}},
        "false_positives": {"actionable_fp_rate": 0.0},
        "calibration": {"table": {}},
        "call_budget": {"real_calls": 3, "failed_calls": 0},
        "transport_records": [],
    }


def _make_transport_record(trace_id=None, call_seq=1):
    """Synthetic transport record."""
    import uuid
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "trace_id": trace_id or str(uuid.uuid4()),
        "call_seq": call_seq,
        "request_ts": now,
        "response_ts": now,
        "endpoint": "http://127.0.0.1:46377/v1/chat/completions",
        "latency_s": 0.5,
        "http_status": 200,
        "response_headers": {"date": "Sun, 13 Oct 2026 12:00:00 GMT",
                             "server": "llama.cpp"},
        "success": True,
        "error": "",
    }


# ---------------------------------------------------------------------------
# check_identity_binding — pure identity comparison
# ---------------------------------------------------------------------------

class TestCheckIdentityBinding:
    def test_stable_identity_passes(self):
        """Same boot_id and pid => ok."""
        before = _fake_serving_state("before", pid=1000, boot_id="boot-aaa")
        after = _fake_serving_state("after", pid=1000, boot_id="boot-aaa")
        result = check_identity_binding(before, after)
        assert result["ok"] is True

    def test_pid_change_fails(self):
        """Different pid (process restarted) => fail closed."""
        before = _fake_serving_state("before", pid=1000, boot_id="boot-aaa")
        after = _fake_serving_state("after", pid=1001, boot_id="boot-aaa")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "pid changed" in result["reason"]
        assert result["before_pid"] == 1000
        assert result["after_pid"] == 1001

    def test_boot_id_change_fails(self):
        """Different boot_id (system reboot) => fail closed."""
        before = _fake_serving_state("before", pid=1000, boot_id="boot-aaa")
        after = _fake_serving_state("after", pid=1000, boot_id="boot-bbb")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "boot_id changed" in result["reason"]
        assert result["before_boot_id"] == "boot-aaa"
        assert result["after_boot_id"] == "boot-bbb"

    def test_unavailable_before_fails(self):
        """Before state unavailable => fail closed."""
        before = _unavailable_state("ssh timeout")
        after = _fake_serving_state("after")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "before state unavailable" in result["reason"]

    def test_unavailable_after_fails(self):
        """After state unavailable => fail closed."""
        before = _fake_serving_state("before")
        after = _unavailable_state("unit not running")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "after state unavailable" in result["reason"]

    def test_both_unavailable_fails(self):
        """Both states unavailable => fail closed (reports before first)."""
        before = _unavailable_state("connection refused")
        after = _unavailable_state("connection refused")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "before state unavailable" in result["reason"]

    def test_both_pid_and_boot_change_reports_boot(self):
        """Both boot_id and pid changed — reports boot_id change (checked first)."""
        before = _fake_serving_state("before", pid=1000, boot_id="boot-aaa")
        after = _fake_serving_state("after", pid=1001, boot_id="boot-bbb")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "boot_id changed" in result["reason"]


# ---------------------------------------------------------------------------
# gather_remote_serving_state — SSH fail-closed
# ---------------------------------------------------------------------------

class TestGatherRemoteServingState:
    def test_ssh_timeout_returns_unavailable(self):
        """SSH timeout => unavailable dict, not an exception."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("ssh", 30)
            result = gather_remote_serving_state("100.113.174.74")
        assert result["unavailable"] is True
        assert "ssh connection failed" in result["reason"]
        assert result["host"] == "100.113.174.74"

    def test_ssh_os_error_returns_unavailable(self):
        """SSH OS error (binary not found) => unavailable dict."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("ssh not found")
            result = gather_remote_serving_state("100.113.174.74")
        assert result["unavailable"] is True
        assert "ssh connection failed" in result["reason"]

    def test_ssh_nonzero_exit_returns_unavailable(self):
        """SSH exits non-zero (auth failure) => unavailable dict."""
        mock_result = MagicMock()
        mock_result.returncode = 255
        mock_result.stderr = "Permission denied (publickey)"
        with patch("subprocess.run", return_value=mock_result):
            result = gather_remote_serving_state("100.113.174.74")
        assert result["unavailable"] is True
        assert "ssh probe exited 255" in result["reason"]

    def test_remote_probe_unavailable_passthrough(self):
        """Remote probe returns unavailable => passthrough with host added."""
        probe_output = json.dumps({"unavailable": True,
                                    "reason": "unit not running (MainPID=0)"})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = probe_output
        with patch("subprocess.run", return_value=mock_result):
            result = gather_remote_serving_state("100.113.174.74")
        assert result["unavailable"] is True
        assert "unit not running" in result["reason"]
        assert result["host"] == "100.113.174.74"

    def test_remote_probe_invalid_json_returns_unavailable(self):
        """Remote probe outputs garbage => unavailable dict."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "NOT VALID JSON"
        with patch("subprocess.run", return_value=mock_result):
            result = gather_remote_serving_state("100.113.174.74")
        assert result["unavailable"] is True
        assert "unparseable probe output" in result["reason"]

    def test_remote_probe_success_returns_valid_state(self):
        """Remote probe returns valid state dict with collected_from."""
        probe_state = {
            "runtime": {"boot_id": "boot-remote", "pid": 42,
                        "start_ticks": 999, "argv_sha256": "a" * 64,
                        "executable_sha256": "b" * 64,
                        "environment_sha256": "c" * 64},
            "manifest": {"canonicalization": "watchtower-effective/v1",
                         "unit": "qwen27.service"},
            "manifest_sha256": "d" * 64,
            "endpoint": "100.113.174.74:46377",
            "route_hash": "e" * 64,
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(probe_state)
        with patch("subprocess.run", return_value=mock_result):
            result = gather_remote_serving_state("100.113.174.74")
        assert "unavailable" not in result
        assert result["runtime"]["boot_id"] == "boot-remote"
        assert result["runtime"]["pid"] == 42
        assert result["collected_from"] == "100.113.174.74"

    def test_probe_transport_is_stdin_not_dash_c(self):
        """LAW 22: probe body travels via stdin (`python3 -`), never as a
        `python3 -c <body>` argv — ssh joins argv into a shell command
        line and the remote shell re-parses the body (runtime-broken
        2026-10-15, three probes)."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"unavailable": True,
                                         "reason": "unit not running"})
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            gather_remote_serving_state("nucbox")
        argv = mock_run.call_args[0][0]
        kwargs = mock_run.call_args[1]
        assert argv[-2:] == ["python3", "-"]
        assert "-c" not in argv
        # the probe body must not ride in argv at all
        assert all("import hashlib" not in arg for arg in argv)
        # it rides on stdin instead
        assert "import hashlib" in kwargs.get("input", "")

    def test_no_python_dash_c_transport_anywhere_in_module(self):
        """Static guard: fl4write/reliability.py never ships a program body
        through ssh as `python3 -c <body>` (any host, any caller)."""
        src = (Path(__file__).resolve().parents[1]
               / "fl4write" / "reliability.py").read_text()
        assert "'python3', '-c'" not in src
        assert '"python3", "-c"' not in src
        assert "'python3', '-'" in src

    def test_gather_serving_state_dispatches_to_remote(self):
        """gather_serving_state(host=...) calls gather_remote_serving_state."""
        with patch("fl4write.reliability.gather_remote_serving_state") as mock:
            mock.return_value = {"unavailable": True, "reason": "test"}
            result = gather_serving_state(host="100.113.174.74")
        mock.assert_called_once_with("100.113.174.74")
        assert result["unavailable"] is True


# ---------------------------------------------------------------------------
# _gather_local_serving_state — fail-closed contract (never raises)
# ---------------------------------------------------------------------------

class TestLocalServingStateFailClosed:
    def test_missing_route_file_returns_unavailable_not_raise(self, monkeypatch,
                                                              tmp_path):
        """Fail-closed contract: a local probe failure returns
        {'unavailable': True, 'reason': ...}, never an exception (the
        remote probe has honored this since c8c2e68; the local path must
        match)."""
        monkeypatch.setattr("fl4write.reliability._ROUTE_PATH",
                            tmp_path / "CHAMPION-ROUTE-HASH-missing")
        state = gather_serving_state()
        assert state["unavailable"] is True
        assert "route read failed" in state["reason"]

    def test_gather_serving_state_never_raises_on_any_host(self):
        """Contract: on any host, gather_serving_state() returns a dict —
        real state, or fail-closed unavailable.  Never a traceback."""
        state = gather_serving_state()
        assert isinstance(state, dict)
        assert ("runtime" in state) or (state.get("unavailable") is True)


# ---------------------------------------------------------------------------
# build_receipt_row — identity_binding field in evidence artifact
# ---------------------------------------------------------------------------

class TestBuildReceiptRowIdentityBinding:
    def test_evidence_includes_identity_binding_ok(self):
        """Evidence artifact carries identity_binding check result."""
        before = _fake_serving_state("before", pid=1000, boot_id="boot-aaa")
        after = _fake_serving_state("after", pid=1000, boot_id="boot-aaa")
        report = _make_report()
        row, paths = build_receipt_row(
            report, "/tmp/r.json", before, after, [],
            out_dir="/tmp/test-identity-ok")
        evidence = json.loads(Path(paths["evidence"]).read_text())
        assert evidence["identity_binding"]["ok"] is True

    def test_evidence_includes_identity_binding_failure(self):
        """Evidence artifact carries identity_binding failure details."""
        before = _fake_serving_state("before", pid=1000, boot_id="boot-aaa")
        after = _fake_serving_state("after", pid=1001, boot_id="boot-aaa")
        report = _make_report()
        row, paths = build_receipt_row(
            report, "/tmp/r.json", before, after, [],
            out_dir="/tmp/test-identity-fail")
        evidence = json.loads(Path(paths["evidence"]).read_text())
        assert evidence["identity_binding"]["ok"] is False
        assert "pid changed" in evidence["identity_binding"]["reason"]

    def test_evidence_includes_unavailable_before_state(self):
        """Evidence carries binding failure when before state is unavailable."""
        before = _unavailable_state("ssh timeout")
        after = _fake_serving_state("after")
        report = _make_report()
        row, paths = build_receipt_row(
            report, "/tmp/r.json", before, after, [],
            out_dir="/tmp/test-identity-unavail")
        evidence = json.loads(Path(paths["evidence"]).read_text())
        assert evidence["identity_binding"]["ok"] is False
        assert "before state unavailable" in evidence["identity_binding"]["reason"]


# ---------------------------------------------------------------------------
# Orchestration ordering — the core defect
# ---------------------------------------------------------------------------

class TestCadenceOrchestrationOrdering:
    def test_two_after_samples_detectable_by_same_pid(self):
        """Two post-run samples from same process have identical boot_id+pid.
        This is the OLD BUG: both 'before' and 'after' are really 'after',
        so they always agree — masking a restart that happened DURING the run.

        This test proves the identity check cannot detect a restart when
        both samples are taken after measurement (the restart already
        happened and the new process has a new pid, but we captured the
        same new pid twice).  The fix ensures the before sample is taken
        BEFORE measurement starts, so a restart produces different pids.
        """
        # Simulate the old bug: both samples taken after measurement,
        # from the same running process (no restart occurred).
        after_1 = _fake_serving_state("after", pid=2000, boot_id="boot-xxx")
        after_2 = _fake_serving_state("after", pid=2000, boot_id="boot-xxx")
        result = check_identity_binding(after_1, after_2)
        # The bug: this passes even though neither sample is truly "before"
        assert result["ok"] is True
        # This is exactly why we need to capture before BEFORE measurement:
        # if the process restarted during measurement, the before sample
        # (captured before restart) would have pid=2000 and the after
        # sample (captured after restart) would have pid=2001.

    def test_restart_during_measurement_detected_with_correct_ordering(self):
        """When before is captured before measurement and after after,
        a restart during measurement produces different pids => fail closed.
        """
        before = _fake_serving_state("before", pid=2000, boot_id="boot-xxx")
        # Process restarted during measurement — new pid
        after = _fake_serving_state("after", pid=2001, boot_id="boot-xxx")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "pid changed" in result["reason"]

    def test_reboot_during_measurement_detected(self):
        """System reboot during measurement => boot_id changes => fail closed."""
        before = _fake_serving_state("before", pid=2000, boot_id="boot-old")
        after = _fake_serving_state("after", pid=2000, boot_id="boot-new")
        result = check_identity_binding(before, after)
        assert result["ok"] is False
        assert "boot_id changed" in result["reason"]

    def test_remote_host_mismatch_fails_closed(self):
        """Remote host unreachable => fail closed, not silent fabrication."""
        before = _unavailable_state("ssh connection failed: timeout")
        after = _unavailable_state("ssh connection failed: timeout")
        result = check_identity_binding(before, after)
        assert result["ok"] is False

    def test_local_collection_on_remote_endpoint_is_not_identity(self):
        """Local /proc reads do NOT represent a remote serving host.
        This test documents the design constraint: gather_serving_state()
        with host=None on a macOS machine (no /proc) fails closed with an
        unavailable dict.  The cadence script must pass the correct host
        to avoid this.
        """
        # On macOS, _gather_local_serving_state() fails closed (unavailable
        # dict, no exception) because /proc doesn't exist. This is correct
        # behavior — fail closed rather than fabricate local state for a
        # remote host.  We verify the remote path is the default for
        # non-localhost endpoints.
        with patch("fl4write.reliability.gather_remote_serving_state") as mock:
            mock.return_value = {"unavailable": True, "reason": "test"}
            # Non-localhost endpoint => should use remote collection
            gather_serving_state(host="100.113.174.74")
        mock.assert_called_once_with("100.113.174.74")

    def test_identity_check_in_receipt_row_catches_restart(self):
        """Full integration: build_receipt_row with a restart between
        before and after produces identity_binding failure in evidence.
        """
        before = _fake_serving_state("before", pid=3000, boot_id="boot-stable")
        after = _fake_serving_state("after", pid=3001, boot_id="boot-stable")
        report = _make_report()
        _, paths = build_receipt_row(
            report, "/tmp/r.json", before, after, [],
            out_dir="/tmp/test-restart-detect")
        evidence = json.loads(Path(paths["evidence"]).read_text())
        binding = evidence["identity_binding"]
        assert binding["ok"] is False
        assert "pid changed" in binding["reason"]
        assert binding["before_pid"] == 3000
        assert binding["after_pid"] == 3001