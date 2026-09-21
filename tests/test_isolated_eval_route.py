"""Pins for the selected route crossing the isolated live-test boundary."""
import json
from pathlib import Path
import runpy
import subprocess

import pytest

from fl4write import exhaustive_sandbox as sandbox
from fl4write.analyzer import _call_model
from fl4write.config import ModelRoute
from fl4write.model_proxy import ModelProxy


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64


def _live_helpers(monkeypatch):
    helpers = runpy.run_path(str(ROOT / "tests/test_planted_diffs.py"))
    config = helpers["_config"]()
    config.fallback_model = config.model.model_copy(update={"model": "old-fallback"})
    monkeypatch.setattr(helpers["cfg"], "load_config", lambda path: config)
    return helpers["_live_config"], config


def test_selected_route_reaches_analyzer_through_supervisor_without_credential(tmp_path, monkeypatch):
    route = ModelRoute(endpoint="https://selected.invalid/v1/chat/completions", model="selected",
                       key_env="SELECTED_TEST_KEY", temperature=0.0, max_tokens=37, seed=19)
    monkeypatch.setenv(route.key_env, "fixture-only-key")
    proxy = ModelProxy(route, max_calls=1, max_output_tokens=37)
    payloads = []

    def forward(payload):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "stop"}]}

    monkeypatch.setattr(proxy, "_forward", forward)
    with proxy:
        argv = sandbox.container_command(["pytest", "{junit}"], tmp_path, IMAGE, "fixture", 30,
                                         model_proxy=proxy)
        assert "fixture-only-key" not in " ".join(argv)
        assert route.key_env not in " ".join(argv)
        captured = {}
        worker = runpy.run_path(str(ROOT / "tools/exhaustive-runtime/worker.py"))
        worker_globals = worker["main"].__globals__
        worker_globals["STATUS"] = tmp_path / "status.json"

        class Finished:
            # pid present: the supervisor killpgs the group on EVERY exit
            # path post-Arch-1 (success included)
            pid = -1

            def __init__(self, command, **kwargs):
                captured.update(kwargs["env"])

            def wait(self, timeout=None):
                return 0

        class SupervisorIdle(Exception):
            pass

        def idle(_seconds):
            raise SupervisorIdle

        with monkeypatch.context() as scoped:
            scoped.setattr(worker["sys"], "argv", argv[argv.index(sandbox.WORKER):])
            scoped.setattr(worker["subprocess"], "Popen", Finished)
            scoped.setattr(worker["time"], "sleep", idle)
            scoped.setattr(worker["os"], "killpg", lambda pid, sig: None)
            with pytest.raises(SupervisorIdle):
                worker["main"]()
        assert json.loads((tmp_path / "status.json").read_text())["returncode"] == 0
        assert route.key_env not in captured
        assert captured["FL4WRITE_LIVE_EVAL_PROXY_SOCKET"] == "/model-proxy/model.sock"
        load, config = _live_helpers(monkeypatch)
        monkeypatch.setenv("FL4WRITE_EVAL_MODEL", captured["FL4WRITE_EVAL_MODEL"])
        monkeypatch.setenv("FL4WRITE_LIVE_EVAL_PROXY_SOCKET", str(proxy.socket_path))
        monkeypatch.setenv("FL4WRITE_MODEL_PROXY_SOCKET", str(proxy.socket_path))
        monkeypatch.delenv(route.key_env)
        selected = load()
        assert selected.model == route.model_copy(update={"key_env": ""})
        assert selected.fallback_model is None
        assert selected.review == config.review
        assert _call_model(selected.model, "review fixture") == '{"findings": []}'
        assert payloads[0]["model"] == "selected"
        assert payloads[0]["seed"] == 19
        assert proxy.snapshot()["calls"] == 1


def test_live_config_without_override_preserves_existing_configuration(monkeypatch):
    load, config = _live_helpers(monkeypatch)
    monkeypatch.delenv("FL4WRITE_EVAL_MODEL", raising=False)
    monkeypatch.delenv("FL4WRITE_LIVE_EVAL_PROXY_SOCKET", raising=False)
    assert load() is config
    assert config.fallback_model.model == "old-fallback"


def test_live_route_override_requires_proxy(monkeypatch):
    load, _ = _live_helpers(monkeypatch)
    monkeypatch.setenv("FL4WRITE_EVAL_MODEL", "{}")
    monkeypatch.delenv("FL4WRITE_LIVE_EVAL_PROXY_SOCKET", raising=False)
    with pytest.raises(ValueError, match="bounded proxy"):
        load()


@pytest.mark.parametrize("metadata", ["[]", '{"model": "missing-fields"}'])
def test_live_route_override_rejects_invalid_metadata(monkeypatch, metadata):
    load, _ = _live_helpers(monkeypatch)
    monkeypatch.setenv("FL4WRITE_EVAL_MODEL", metadata)
    monkeypatch.setenv("FL4WRITE_LIVE_EVAL_PROXY_SOCKET", "/fixture/model.sock")
    with pytest.raises(ValueError):
        load()


def test_old_runtime_cannot_silently_drop_selected_route(monkeypatch):
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, IMAGE + " 1 1", ""))
    with pytest.raises(sandbox.SandboxUnavailable, match="not the installed"):
        sandbox.validate_runtime(IMAGE, require_model_proxy=True)


@pytest.mark.parametrize("extra", [["live-model"], ["live-model", '{"key_env":"UNSAFE"}']])
def test_supervisor_refuses_incomplete_or_credential_bearing_route(monkeypatch, extra):
    worker = runpy.run_path(str(ROOT / "tools/exhaustive-runtime/worker.py"))
    monkeypatch.setattr(worker["sys"], "argv", ["worker", "run", "123", "123", "30", '["pytest"]', *extra])
    monkeypatch.setattr(worker["subprocess"], "Popen", lambda *a, **kw: pytest.fail("test process must not start"))
    assert worker["main"]() == 2


def test_selected_route_cannot_collide_with_forge_credentials(monkeypatch):
    """Arch-3 (2026-09-16 architecture review): _live_config must re-run the
    credential-namespace invariants on the assembled route — a selected model
    naming a reserved env (GH_TOKEN) is refused here, not silently honored."""
    import json as _json
    import pytest as _pytest
    from test_planted_diffs import _live_config

    monkeypatch.setenv("FL4WRITE_EVAL_CONFIG", str(
        Path(__file__).parents[1] / "fl4write.fl4write.yaml"))
    monkeypatch.setenv("FL4WRITE_LIVE_EVAL_PROXY_SOCKET", "/tmp/none.sock")
    monkeypatch.setenv("FL4WRITE_EVAL_MODEL", _json.dumps({
        "endpoint": "http://provider/v1/chat/completions", "model": "m",
        "key_env": "GH_TOKEN", "temperature": 0.0, "max_tokens": 4000}))
    with _pytest.raises(Exception, match="key_env|namespace|reserved"):
        _live_config()
