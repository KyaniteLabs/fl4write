import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest

from fl4write.config import ModelRoute
from fl4write.model_proxy import ModelProxy, ProxyError, request
from fl4write.model_proxy import MAX_RESPONSE


def _route():
    return ModelRoute(endpoint="https://model.invalid/v1/chat/completions", model="fixture",
                      key_env="MODEL_PROXY_FIXTURE_KEY", max_tokens=10, temperature=0.2)


def _payload(route):
    return {"model": route.model, "max_tokens": route.max_tokens, "temperature": route.temperature,
            "messages": [{"role": "system", "content": "Review code."},
                         {"role": "user", "content": "def value(): return 1"}]}


def test_real_unix_transport_uses_selected_route_and_retains_budget_after_failure(monkeypatch):
    monkeypatch.setenv("MODEL_PROXY_FIXTURE_KEY", "fixture-only")
    route = _route()
    calls = []
    def forward(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("provider unavailable")
        return {"choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "stop"}]}
    proxy = ModelProxy(route, max_calls=2, max_output_tokens=20)
    monkeypatch.setattr(proxy, "_forward", forward)
    with proxy:
        with pytest.raises(ProxyError, match="unavailable"):
            request(str(proxy.socket_path), route.endpoint, _payload(route))
        assert request(str(proxy.socket_path), route.endpoint, _payload(route))["choices"]
        with pytest.raises(ProxyError):
            request(str(proxy.socket_path), route.endpoint, _payload(route))
        assert proxy.snapshot() == {"calls": 2, "reserved_output_tokens": 20,
                                    "completed": 1, "failed": 1, "active": 0}
        directory = proxy.socket_path.parent
    assert not directory.exists() and not proxy.key


@pytest.mark.parametrize("change", ["endpoint", "model", "max_tokens", "temperature", "messages"])
def test_proxy_rejects_route_or_payload_drift_before_reserving_a_call(monkeypatch, change):
    monkeypatch.setenv("MODEL_PROXY_FIXTURE_KEY", "fixture-only")
    route = _route()
    proxy = ModelProxy(route, max_calls=1, max_output_tokens=10)
    value = {"endpoint": route.endpoint, "payload": _payload(route)}
    if change == "endpoint":
        value[change] = "https://other.invalid/"
    else:
        value["payload"][change] = "different"
    with pytest.raises(ProxyError):
        proxy._dispatch(value)
    assert proxy.snapshot()["calls"] == 0


def test_analyzer_uses_proxy_without_receiving_provider_credential(monkeypatch):
    from fl4write.analyzer import _call_model
    monkeypatch.setenv("MODEL_PROXY_FIXTURE_KEY", "fixture-only")
    route = _route()
    proxy = ModelProxy(route, max_calls=1, max_output_tokens=10)
    monkeypatch.setattr(proxy, "_forward", lambda payload: {
        "choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "stop"}]})
    with proxy:
        monkeypatch.delenv("MODEL_PROXY_FIXTURE_KEY")
        monkeypatch.setenv("FL4WRITE_MODEL_PROXY_SOCKET", str(proxy.socket_path))
        assert json.loads(_call_model(route, "Review this code.")) == {"findings": []}


def test_response_near_limit_survives_unicode_encoding_and_envelope(monkeypatch):
    monkeypatch.setenv("MODEL_PROXY_FIXTURE_KEY", "fixture-only")
    route = _route()
    value = {"x": "é" * (MAX_RESPONSE // 2 - 10)}
    proxy = ModelProxy(route, max_calls=1, max_output_tokens=10)
    monkeypatch.setattr(proxy, "_forward", lambda payload: value)
    with proxy:
        assert request(str(proxy.socket_path), route.endpoint, _payload(route)) == value
        assert proxy.snapshot()["completed"] == 1


def test_oversize_result_is_failure_with_reservation_retained(monkeypatch):
    monkeypatch.setenv("MODEL_PROXY_FIXTURE_KEY", "fixture-only")
    route = _route()
    proxy = ModelProxy(route, max_calls=1, max_output_tokens=10)
    monkeypatch.setattr(proxy, "_forward", lambda payload: {"x": "a" * MAX_RESPONSE})
    with proxy:
        with pytest.raises(ProxyError):
            request(str(proxy.socket_path), route.endpoint, _payload(route))
        assert proxy.snapshot() == {"calls": 1, "reserved_output_tokens": 10,
                                    "completed": 0, "failed": 1, "active": 0}


def test_exit_reaps_real_provider_process_without_waiting_for_http(monkeypatch):
    monkeypatch.setenv("MODEL_PROXY_FIXTURE_KEY", "fixture-only")
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            release.wait(10)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    route = _route().model_copy(update={"endpoint": f"http://127.0.0.1:{server.server_port}/"})
    proxy = ModelProxy(route, max_calls=1, max_output_tokens=10)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with proxy:
                future = pool.submit(request, str(proxy.socket_path), route.endpoint, _payload(route))
                assert entered.wait(5), "provider process never connected"
                children = list(proxy.children)
                started = time.monotonic()
            assert time.monotonic() - started < 3
            assert children and all(child.poll() is not None for child in children)
            with pytest.raises(ProxyError):
                future.result(timeout=2)
            assert proxy.snapshot()["active"] == 0
            assert proxy.snapshot()["failed"] == 1
            assert not proxy.socket_path.parent.exists()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
