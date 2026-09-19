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
