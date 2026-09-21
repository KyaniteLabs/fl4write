import pytest
from pydantic import ValidationError

from fl4write.analyzer import _model_payload
from fl4write.config import ModelRoute
from fl4write.model_proxy import ModelProxy, ProxyError, request


@pytest.mark.parametrize("thinking", [None, "enabled", "disabled"])
def test_selected_thinking_is_transported_and_cannot_be_overridden(thinking):
    options = {} if thinking is None else {"thinking": thinking}
    route = ModelRoute(endpoint="https://provider.invalid/v1/chat/completions", model="test", **options)
    payload = _model_payload(route, "test")
    if thinking is None:
        assert "thinking" not in payload
    else:
        assert payload["thinking"] == {"type": thinking}
    observed = []
    with ModelProxy(route, max_calls=2, max_output_tokens=8000) as proxy:
        proxy._forward = lambda value: observed.append(value) or {"ok": True}
        assert request(str(proxy.socket_path), route.endpoint, payload) == {"ok": True}
        changed = {**payload, "thinking": {"type": "enabled" if thinking != "enabled" else "disabled"}}
        with pytest.raises(ProxyError):
            request(str(proxy.socket_path), route.endpoint, changed)
        assert observed == [payload]
        assert proxy.snapshot()["calls"] == 1


@pytest.mark.parametrize("value", [False, "automatic", {"type": "disabled"}])
def test_thinking_rejects_invalid_config(value):
    with pytest.raises(ValidationError):
        ModelRoute(endpoint="https://provider.invalid/v1", model="test", thinking=value)
