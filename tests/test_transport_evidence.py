"""Tests for transport-real evidence in the reliability receipt builder.

Proves that request records arise from actual HTTP calls, not synthesized
placeholders.  All tests run offline (no real HTTP, no serving state).
"""

from __future__ import annotations

import datetime
import http.client
import json
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fl4write.reliability import (
    TransportCollector,
    _sha256_dict,
    build_receipt_row,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_serving_state(runtime_id="before"):
    """Synthetic serving state for offline tests."""
    return {
        "runtime": {
            "boot_id": f"boot-{runtime_id}",
            "pid": 1000 if runtime_id == "before" else 1001,
            "start_ticks": 100 if runtime_id == "before" else 200,
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
    }


def _make_transport_record(trace_id=None, call_seq=1, success=True,
                           endpoint="http://127.0.0.1:46377/v1/chat/completions",
                           http_status=200, latency=0.5):
    """Synthetic transport record mimicking a real HTTP call."""
    import uuid
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "trace_id": trace_id or str(uuid.uuid4()),
        "call_seq": call_seq,
        "request_ts": now,
        "response_ts": now,
        "endpoint": endpoint,
        "latency_s": latency,
        "http_status": http_status,
        "response_headers": {"date": "Sun, 13 Oct 2026 12:00:00 GMT",
                             "server": "llama.cpp"},
        "success": success,
        "error": "" if success else "ConnectionRefusedError: refused",
    }


def _make_urlopen_response(status=200, body=None, headers=None):
    """Create a fake urllib response object."""
    if body is None:
        body = json.dumps({
            "choices": [{"message": {"content": '{"findings": []}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    hdrs = http.client.HTTPMessage()
    hdrs["Date"] = "Sun, 13 Oct 2026 12:00:00 GMT"
    hdrs["Server"] = "llama.cpp"
    if headers:
        for k, v in headers.items():
            hdrs[k] = v
    resp.headers = hdrs
    return resp


# ---------------------------------------------------------------------------
# TransportCollector — captures per-call observations
# ---------------------------------------------------------------------------

class TestTransportCollector:
    def test_records_one_entry_per_urlopen_call(self):
        """Each urllib.request.urlopen call produces exactly one record."""
        call_count = 0

        def mock_urlopen(req, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_urlopen_response()

        with patch("urllib.request.urlopen", mock_urlopen):
            collector = TransportCollector()
            with collector:
                urllib.request.urlopen(
                    urllib.request.Request("http://test/v1/chat/completions",
                                          data=b"{}", method="POST"))
                urllib.request.urlopen(
                    urllib.request.Request("http://test/v1/chat/completions",
                                          data=b"{}", method="POST"))

        assert len(collector.records) == 2
        assert call_count == 2

    def test_each_record_has_unique_trace_id(self):
        """trace_id is a UUID generated per call, not derived from run_id."""
        with patch("urllib.request.urlopen",
                   lambda *a, **k: _make_urlopen_response()):
            collector = TransportCollector()
            with collector:
                for _ in range(5):
                    urllib.request.urlopen(
                        urllib.request.Request("http://test/api",
                                              data=b"{}", method="POST"))

        trace_ids = [r["trace_id"] for r in collector.records]
        assert len(trace_ids) == 5
        assert len(set(trace_ids)) == 5  # all unique
        # UUIDs are not derived from any run_id
        for tid in trace_ids:
            assert len(tid) == 36  # UUID format

    def test_timestamps_vary_between_calls(self):
        """request_ts and response_ts are real per-call timestamps, not
        all-identical synthetic values."""
        def mock_urlopen(req, *args, **kwargs):
            time.sleep(0.01)  # ensure measurable time difference
            return _make_urlopen_response()

        with patch("urllib.request.urlopen", mock_urlopen):
            collector = TransportCollector()
            with collector:
                for _ in range(3):
                    urllib.request.urlopen(
                        urllib.request.Request("http://test/api",
                                              data=b"{}", method="POST"))

        timestamps = [r["request_ts"] for r in collector.records]
        # Not all identical (timestamps are per-call, not per-run)
        assert len(set(timestamps)) > 1 or len(timestamps) == 1

    def test_captures_http_status_and_response_headers(self):
        """Records include HTTP status code and evidence headers."""
        def mock_urlopen(req, *args, **kwargs):
            return _make_urlopen_response(
                status=200, headers={"X-Trace-Id": "server-trace-123"})

        with patch("urllib.request.urlopen", mock_urlopen):
            collector = TransportCollector()
            with collector:
                urllib.request.urlopen(
                    urllib.request.Request("http://test/api",
                                          data=b"{}", method="POST"))

        rec = collector.records[0]
        assert rec["http_status"] == 200
        # Headers preserve original case from HTTP response
        assert "Date" in rec["response_headers"] or "date" in rec["response_headers"]
        server_hdr = rec["response_headers"].get("Server") or rec["response_headers"].get("server")
        assert server_hdr == "llama.cpp"

    def test_captures_endpoint_from_request(self):
        """endpoint field records the actual URL hit."""
        def mock_urlopen(req, *args, **kwargs):
            assert req.full_url == "http://model:8908/v1/chat/completions"
            return _make_urlopen_response()

        with patch("urllib.request.urlopen", mock_urlopen):
            collector = TransportCollector()
            with collector:
                urllib.request.urlopen(
                    urllib.request.Request(
                        "http://model:8908/v1/chat/completions",
                        data=b"{}", method="POST"))

        assert collector.records[0]["endpoint"] == \
            "http://model:8908/v1/chat/completions"

    def test_captures_failure_records_and_still_raises(self):
        """Failed calls get a record with success=False and the error,
        and the exception propagates."""
        def mock_urlopen(req, *args, **kwargs):
            raise ConnectionRefusedError("connection refused")

        with patch("urllib.request.urlopen", mock_urlopen):
            collector = TransportCollector()
            with collector:
                with pytest.raises(ConnectionRefusedError):
                    urllib.request.urlopen(
                        urllib.request.Request("http://test/api",
                                              data=b"{}", method="POST"))

        assert len(collector.records) == 1
        rec = collector.records[0]
        assert rec["success"] is False
        assert "ConnectionRefusedError" in rec["error"]
        assert rec["latency_s"] >= 0

    def test_mixed_success_and_failure_calls(self):
        """A run with some failures records both successes and failures."""
        call_n = 0
        def mock_urlopen(req, *args, **kwargs):
            nonlocal call_n
            call_n += 1
            if call_n == 2:
                raise RuntimeError("route down")
            return _make_urlopen_response()

        with patch("urllib.request.urlopen", mock_urlopen):
            collector = TransportCollector()
            with collector:
                for _ in range(3):
                    try:
                        urllib.request.urlopen(
                            urllib.request.Request("http://test/api",
                                                  data=b"{}", method="POST"))
                    except RuntimeError:
                        pass

        assert len(collector.records) == 3
        assert collector.records[0]["success"] is True
        assert collector.records[1]["success"] is False
        assert collector.records[2]["success"] is True

    def test_call_seq_increments(self):
        """call_seq is a monotonically-increasing sequence number."""
        with patch("urllib.request.urlopen",
                   lambda *a, **k: _make_urlopen_response()):
            collector = TransportCollector()
            with collector:
                for _ in range(4):
                    urllib.request.urlopen(
                        urllib.request.Request("http://test/api",
                                              data=b"{}", method="POST"))

        seqs = [r["call_seq"] for r in collector.records]
        assert seqs == [1, 2, 3, 4]

    def test_restores_urlopen_on_exit(self):
        """Original urlopen is restored after context exit."""
        original = urllib.request.urlopen
        collector = TransportCollector()
        with collector:
            assert urllib.request.urlopen is not original
        assert urllib.request.urlopen is original

    def test_empty_collector_no_calls(self):
        """Collector with no calls has empty records."""
        collector = TransportCollector()
        with collector:
            pass
        assert collector.records == []

    def test_latency_is_realistic(self):
        """latency_s reflects actual wall-clock time, not zero."""
        def slow_urlopen(req, *args, **kwargs):
            time.sleep(0.05)
            return _make_urlopen_response()

        with patch("urllib.request.urlopen", slow_urlopen):
            collector = TransportCollector()
            with collector:
                urllib.request.urlopen(
                    urllib.request.Request("http://test/api",
                                          data=b"{}", method="POST"))

        assert collector.records[0]["latency_s"] >= 0.04


# ---------------------------------------------------------------------------
# build_receipt_row — uses transport records, not synthesized requests
# ---------------------------------------------------------------------------

class TestBuildReceiptRow:
    def test_requests_come_from_transport_records(self):
        """evidence.requests entries use trace_ids from transport records,
        NOT synthesized 'receipt-<run_id>' values."""
        trs = [_make_transport_record(call_seq=i) for i in range(1, 4)]
        report = _make_report()
        before = _fake_serving_state("before")
        after = _fake_serving_state("after")

        row, paths = build_receipt_row(
            report, "/tmp/test-report.json", before, after, trs,
            out_dir="/tmp/test-evidence")

        evidence = json.loads(
            Path(paths["evidence"]).read_text())
        requests = evidence["requests"]

        # One request per transport record
        assert len(requests) == 3
        # request_ids match the trace_ids (not 'receipt-<run_id>')
        for i, tr in enumerate(trs):
            assert requests[i]["request_id"] == tr["trace_id"]
            assert not requests[i]["request_id"].startswith("receipt-")

    def test_before_and_after_runtime_differ(self):
        """before_runtime and after_runtime are independently sampled."""
        trs = [_make_transport_record()]
        report = _make_report()
        before = _fake_serving_state("before")
        after = _fake_serving_state("after")

        row, paths = build_receipt_row(
            report, "/tmp/test-report.json", before, after, trs,
            out_dir="/tmp/test-evidence2")

        evidence = json.loads(
            Path(paths["evidence"]).read_text())
        assert evidence["before_runtime"]["boot_id"] == "boot-before"
        assert evidence["after_runtime"]["boot_id"] == "boot-after"
        assert evidence["before_runtime"] != evidence["after_runtime"]

    def test_transport_summary_counts(self):
        """transport_summary reflects actual call counts."""
        trs = [
            _make_transport_record(success=True),
            _make_transport_record(success=False),
            _make_transport_record(success=True),
        ]
        report = _make_report()
        before = _fake_serving_state("before")
        after = _fake_serving_state("after")

        row, paths = build_receipt_row(
            report, "/tmp/test-report.json", before, after, trs,
            out_dir="/tmp/test-evidence3")

        evidence = json.loads(
            Path(paths["evidence"]).read_text())
        summary = evidence["transport_summary"]
        assert summary["total_calls"] == 3
        assert summary["successful_calls"] == 2
        assert summary["failed_calls"] == 1

    def test_empty_transport_records(self):
        """Zero transport records is valid (e.g., all cached)."""
        report = _make_report()
        before = _fake_serving_state("before")
        after = _fake_serving_state("after")

        row, paths = build_receipt_row(
            report, "/tmp/test-report.json", before, after, [],
            out_dir="/tmp/test-evidence4")

        evidence = json.loads(
            Path(paths["evidence"]).read_text())
        assert evidence["requests"] == []
        assert evidence["transport_summary"]["total_calls"] == 0

    def test_row_schema_is_watchtower_receipt_v1(self):
        """The trend row carries the correct schema identifier."""
        trs = [_make_transport_record()]
        row, _ = build_receipt_row(
            _make_report(), "/tmp/r.json",
            _fake_serving_state("before"), _fake_serving_state("after"),
            trs, out_dir="/tmp/test-evidence5")
        assert row["schema"] == "watchtower-route-receipt/v1"
        assert row["producer"] == "fl4write-reliability"

    def test_payload_sha256_is_self_referential(self):
        """payload_sha256 is the canonical hash of the row minus itself."""
        trs = [_make_transport_record()]
        row, _ = build_receipt_row(
            _make_report(), "/tmp/r.json",
            _fake_serving_state("before"), _fake_serving_state("after"),
            trs, out_dir="/tmp/test-evidence6")

        expected = _sha256_dict(
            {k: v for k, v in row.items() if k != "payload_sha256"})
        assert row["payload_sha256"] == expected

    def test_all_artifacts_written(self):
        """All four artifacts (report, inputs, evidence, corpus) are written."""
        trs = [_make_transport_record()]
        _, paths = build_receipt_row(
            _make_report(), "/tmp/r.json",
            _fake_serving_state("before"), _fake_serving_state("after"),
            trs, out_dir="/tmp/test-evidence7")

        for name in ("report", "inputs", "evidence", "corpus"):
            p = Path(paths[name])
            assert p.exists(), f"{name} artifact missing at {p}"
            data = json.loads(p.read_text())
            assert isinstance(data, dict)

    def test_request_ids_are_uuids_not_synthesized(self):
        """Every request_id in the evidence is a UUID (trace_id format),
        never 'receipt-<run_id[:12]>'."""
        import uuid
        trs = [_make_transport_record() for _ in range(5)]
        row, paths = build_receipt_row(
            _make_report(), "/tmp/r.json",
            _fake_serving_state("before"), _fake_serving_state("after"),
            trs, out_dir="/tmp/test-evidence8")

        evidence = json.loads(Path(paths["evidence"]).read_text())
        for req in evidence["requests"]:
            # Must be a valid UUID
            parsed = uuid.UUID(req["request_id"])
            assert str(parsed) == req["request_id"]
            # Must NOT be the old synthesized format
            assert not req["request_id"].startswith("receipt-")

    def test_preserves_route_hash_from_before_state(self):
        """The trend row uses route_hash from before_state (stable identity)."""
        trs = [_make_transport_record()]
        before = _fake_serving_state("before")
        row, _ = build_receipt_row(
            _make_report(), "/tmp/r.json", before,
            _fake_serving_state("after"), trs,
            out_dir="/tmp/test-evidence9")
        assert row["route_hash"] == "e" * 64
        assert row["serving_manifest_sha256"] == "d" * 64