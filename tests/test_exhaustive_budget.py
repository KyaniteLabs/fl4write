import json

import pytest

from fl4write import exhaustive
from fl4write.analyzer import _call_model
from fl4write.config import load_config
from fl4write.exhaustive_budget import RoundBudget, round_transport
from fl4write.model_proxy import ModelProxy, ProxyError, request
from test_exhaustive import _args, _repo


def _answer(payload):
    return {"choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "stop"}]}


def test_recon_repair_and_live_suite_share_persistent_reservations(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    args = _args(repo, tmp_path / "state", None)
    args.max_model_calls, args.max_output_tokens = 5, 50
    args.isolation, args.live_model_tests = "docker", True
    config = load_config(repo / ".fl4write.yaml")
    path = tmp_path / "round-budget.json"
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"ledger": []}')
    monkeypatch.setattr(ModelProxy, "_forward", lambda self, payload: _answer(payload))
    def suite(command, tree, evidence, timeout, *, isolation, image, model_proxy):
        assert isolation == "docker" and model_proxy is args._model_proxy
        route = config.model
        payload = {"model": route.model, "temperature": route.temperature,
                   "max_tokens": route.max_tokens,
                   "messages": [{"role": "system", "content": "review"},
                                {"role": "user", "content": "fixture"}]}
        assert request(str(model_proxy.socket_path), route.endpoint, payload)["choices"]
        return {"fixture"}, "digest"
    monkeypatch.setattr(exhaustive, "_test", suite)
    tree, _ = exhaustive._pack(repo, exhaustive._git(repo, "rev-parse", "HEAD"), tmp_path / "packed")
    with round_transport(args, config, path, "selected") as budget:
        findings, _, recon_usage = exhaustive._recon(
            tree, ledger, config.model, 5, 50, 15, tmp_path / "evidence", 48000)
        assert findings == [] and recon_usage["calls"] == 3
        assert _call_model(config.model, "Generate a repair") == '{"findings": []}'
        assert exhaustive._suite_runner(args)([], repo, tmp_path / "suite.xml", 30)[0] == {"fixture"}
        assert budget.usage == {"calls": 5, "reserved_output_tokens": 50}
    assert args._model_proxy is None
    with round_transport(args, config, path, "selected") as budget:
        with pytest.raises(ProxyError):
            _call_model(config.model, "Retry must retain spend")
        assert budget.usage["calls"] == 5


def test_failed_real_worker_request_is_reserved_across_retry(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    args = _args(repo, tmp_path / "state", None)
    args.max_model_calls, args.max_output_tokens = 1, 10
    calls = []
    def unavailable(self, payload):
        calls.append(payload)
        raise RuntimeError("fixture outage")
    monkeypatch.setattr(ModelProxy, "_forward", unavailable)
    assert exhaustive.run(args) == 2
    assert exhaustive.run(args) == 2
    assert len(calls) == 1
    budget_path = next((tmp_path / "state").glob("*/budgets/round-0001.json"))
    assert json.loads(budget_path.read_text())["usage"] == {"calls": 1, "reserved_output_tokens": 10}


def test_partial_real_worker_resumes_with_failed_reservation_retained(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    args = _args(repo, tmp_path / "state", None)
    args.max_model_calls, args.max_output_tokens = 4, 40
    calls = []

    def interrupted(self, payload):
        calls.append(json.loads(payload["messages"][1]["content"])["path"])
        if len(calls) == 2:
            raise RuntimeError("fixture outage")
        return _answer(payload)

    monkeypatch.setattr(ModelProxy, "_forward", interrupted)
    assert exhaustive.run(args) == 2
    assert len(calls) == 2
    assert exhaustive.run(args) == 2  # One complete green round is short of three.
    assert len(calls) == 4 and calls[1] == calls[2] and calls.count(calls[0]) == 1
    state = json.loads(next((tmp_path / "state").glob("*/state.json")).read_bytes())
    assert state["consecutive_green"] == 1
    budget_path = next((tmp_path / "state").glob("*/budgets/round-0001.json"))
    assert json.loads(budget_path.read_bytes())["usage"] == {"calls": 4, "reserved_output_tokens": 40}


@pytest.mark.parametrize("data", [{}, {"identity": {}, "usage": []}, "broken"])
def test_unreadable_budget_never_resets_spend(tmp_path, data):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ProxyError, match="recovered"):
        RoundBudget(path, "request", 1, 10)
    assert path.read_bytes() == before


def test_live_tests_require_docker_before_execution(tmp_path):
    repo = _repo(tmp_path)
    args = _args(repo, tmp_path / "state", None)
    args.live_model_tests = True
    assert exhaustive.run(args) == 2
    escalation = json.loads(next((tmp_path / "state").glob("*/escalation.json")).read_text())
    assert "Docker" in escalation["reason"]
