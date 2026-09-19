"""D5-own-instruments: direct tests for the in-process route_stats /
record_route measurement layer (telemetry.py). These functions had zero
direct test coverage — only exercised transitively via CLI prints."""
from __future__ import annotations

import pytest

from fl4write import telemetry as tel


@pytest.fixture(autouse=True)
def _reset_route_stats():
    """Isolate each test: clear the module-level accumulator."""
    tel._ROUTE_STATS.clear()
    yield
    tel._ROUTE_STATS.clear()


class TestRecordRoute:
    def test_basic_ok_call(self):
        tel.record_route("gpt-4", ok=True, latency_s=1.5, parse_ok=True,
                         prompt_tokens=100, completion_tokens=50)
        st = tel.route_stats()["gpt-4"]
        assert st["calls"] == 1
        assert st["ok"] == 1
        assert st["parse_fail"] == 0
        assert st["latency_s"] == pytest.approx(1.5)
        assert st["prompt_tokens"] == 100
        assert st["completion_tokens"] == 50

    def test_failed_call(self):
        tel.record_route("gpt-4", ok=False, latency_s=0.3, parse_ok=False)
        st = tel.route_stats()["gpt-4"]
        assert st["calls"] == 1
        assert st["ok"] == 0
        assert st["parse_fail"] == 1

    def test_multiple_calls_accumulate(self):
        tel.record_route("m1", ok=True, latency_s=1.0, parse_ok=True,
                         prompt_tokens=10, completion_tokens=5)
        tel.record_route("m1", ok=False, latency_s=2.0, parse_ok=False,
                         prompt_tokens=20, completion_tokens=15)
        st = tel.route_stats()["m1"]
        assert st["calls"] == 2
        assert st["ok"] == 1
        assert st["parse_fail"] == 1
        assert st["latency_s"] == pytest.approx(3.0)
        assert st["prompt_tokens"] == 30
        assert st["completion_tokens"] == 20

    def test_separate_models_tracked_independently(self):
        tel.record_route("a", ok=True, latency_s=1.0, parse_ok=True)
        tel.record_route("b", ok=False, latency_s=2.0, parse_ok=False)
        stats = tel.route_stats()
        assert stats["a"]["calls"] == 1
        assert stats["b"]["calls"] == 1
        assert stats["a"]["ok"] == 1
        assert stats["b"]["ok"] == 0

    def test_boolean_tokens_never_count(self):
        """F13-B008: bool is not a token count."""
        tel.record_route("m", ok=True, latency_s=0.0, parse_ok=True,
                         prompt_tokens=True, completion_tokens=False)
        st = tel.route_stats()["m"]
        assert st["prompt_tokens"] == 0
        assert st["completion_tokens"] == 0

    def test_negative_tokens_clamped_to_zero(self):
        tel.record_route("m", ok=True, latency_s=0.0, parse_ok=True,
                         prompt_tokens=-5, completion_tokens=-3)
        st = tel.route_stats()["m"]
        assert st["prompt_tokens"] == 0
        assert st["completion_tokens"] == 0

    def test_string_tokens_never_crash(self):
        tel.record_route("m", ok=True, latency_s=0.0, parse_ok=True,
                         prompt_tokens="unknown", completion_tokens="42")
        st = tel.route_stats()["m"]
        assert st["prompt_tokens"] == 0
        assert st["completion_tokens"] == 42

    def test_none_latency_defaults_zero(self):
        tel.record_route("m", ok=True, latency_s=None, parse_ok=True)
        st = tel.route_stats()["m"]
        assert st["latency_s"] == 0.0

    def test_never_raises_on_garbage(self):
        """Telemetry contract: never raises."""
        tel.record_route(None, ok=None, latency_s=None, parse_ok=None)
        tel.record_route(12345, ok=1, latency_s="x", parse_ok="y")
        # just must not raise


class TestRouteStats:
    def test_empty_returns_empty_dict(self):
        assert tel.route_stats() == {}

    def test_returns_copy_not_reference(self):
        tel.record_route("m", ok=True, latency_s=1.0, parse_ok=True)
        s1 = tel.route_stats()
        s1["m"]["calls"] = 999
        s2 = tel.route_stats()
        assert s2["m"]["calls"] == 1


class TestEmit:
    """D5: emit() is the write-side of the telemetry stream — it had zero
    direct tests (only exercised transitively via CLI prints)."""

    def test_emit_appends_valid_jsonl(self, tmp_path, monkeypatch):
        p = tmp_path / "t.jsonl"
        monkeypatch.setattr(tel, "_path", lambda: p)
        tel.emit("model_call", model="m1", ok=True)
        import json
        lines = p.read_text().splitlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["kind"] == "model_call"
        assert ev["model"] == "m1"
        assert ev["ok"] is True
        assert "ts" in ev

    def test_emit_multiple_appends(self, tmp_path, monkeypatch):
        p = tmp_path / "t.jsonl"
        monkeypatch.setattr(tel, "_path", lambda: p)
        tel.emit("a", x=1)
        tel.emit("b", y=2)
        lines = p.read_text().splitlines()
        assert len(lines) == 2

    def test_emit_never_raises_on_unwritable_path(self, tmp_path, monkeypatch):
        p = tmp_path / "no_such_dir" / "t.jsonl"
        monkeypatch.setattr(tel, "_path", lambda: p)
        tel.emit("x", y=1)  # must not raise

    def test_emit_never_raises_on_unserializable_field(self, tmp_path, monkeypatch):
        p = tmp_path / "t.jsonl"
        monkeypatch.setattr(tel, "_path", lambda: p)
        tel.emit("x", obj=object())  # default=str handles it
        import json
        ev = json.loads(p.read_text().splitlines()[0])
        assert ev["kind"] == "x"


class TestReadTail:
    """D5: _read_tail is the bounded-read primitive — zero direct tests."""

    def test_small_file_returns_whole(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text("line1\nline2\n")
        assert tel._read_tail(p) == "line1\nline2\n"

    def test_large_file_returns_bounded_tail(self, tmp_path):
        p = tmp_path / "t.jsonl"
        # 200 bytes of data, max_bytes=100 → should get last ~100 bytes
        p.write_bytes(b"A" * 200 + b"\n")
        out = tel._read_tail(p, max_bytes=100)
        assert len(out.encode()) <= 100
        # single 201-byte line: the bounded tail is a PARTIAL line, so it is
        # fully discarded and nothing is returned.
        assert out == ""

    def test_partial_single_line_discarded(self, tmp_path):
        p = tmp_path / "t.jsonl"
        # 151-byte single line, max_bytes=100 → tail is a PARTIAL line,
        # so the first (only) line is discarded and nothing is returned.
        p.write_bytes(b"X" * 150 + b"\n")
        out = tel._read_tail(p, max_bytes=100)
        assert out == ""

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nope.jsonl"
        assert tel._read_tail(p) == ""

    def test_discards_first_partial_line(self, tmp_path):
        p = tmp_path / "t.jsonl"
        # 150 bytes, max_bytes=100 → seek to byte 50, first line is partial
        p.write_bytes(b"X" * 150 + b"\n")
        out = tel._read_tail(p, max_bytes=100)
        # the bounded tail is a single PARTIAL line → fully discarded
        assert out == ""
