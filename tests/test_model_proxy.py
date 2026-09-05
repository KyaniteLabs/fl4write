import json

import pytest

from fl4write.config import ModelRoute
from fl4write.model_proxy import ModelProxy, ProxyError, request


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
