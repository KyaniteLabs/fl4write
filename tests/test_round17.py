"""Functional regression pins for the fresh whole-project round 17 findings."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fl4write import analyzer, engine, executor, model_proxy, state, telemetry
from fl4write.config import ForgeBinding, ModelRoute, RepoConfig
from fl4write.forges import ForgeError
from fl4write.models import Finding, ReviewDoc
from test_gauntlet_fixes import (
    RAW, _NO_EXTRA_LANES, _R4Forge, _r4_cycle, _r4_date, _r4_pr, _r4_seed, make_config,
)


@pytest.mark.parametrize("transport", ["http", "proxy"])
def test_source_survives_actual_model_transport(monkeypatch, transport):
    source = '<!-- layout note -->\n<div style="display:none">text</div>\nimg="data:image/png;base64,AA=="'
    prompt = json.dumps({"sources": {"page.html": source}})
    captured = []
    response = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    route = make_config().model
    monkeypatch.delenv("FL4WRITE_MODEL_PROXY_SOCKET", raising=False)
    monkeypatch.setenv(route.key_env, "test-only")
    if transport == "proxy":
        monkeypatch.setenv("FL4WRITE_MODEL_PROXY_SOCKET", "/unused-test-socket")
        monkeypatch.setattr(model_proxy, "request", lambda socket, endpoint, payload:
                            captured.append(payload) or response)
    else:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps(response).encode()

        def capture(request, **kwargs):
            captured.append(json.loads(request.data))
            return Response()

        monkeypatch.setattr(analyzer.urllib.request, "urlopen", capture)
    assert analyzer._call_model(route, prompt) == "{}"
    assert captured[0]["messages"][1]["content"] == prompt
    assert json.loads(captured[0]["messages"][1]["content"])["sources"]["page.html"] == source


@pytest.mark.parametrize("deferred_retries", [0, 1, 3])
def test_expired_retro_park_retries_after_older_pr_advances_cursor(tmp_path, monkeypatch, deferred_retries):
    newer = _r4_pr(number=1, merged_at=_r4_date(20))
    older = _r4_pr(number=2, merged_at=_r4_date(21))
    forge = _R4Forge(merged=[newer, older])
    path = tmp_path / "state.json"
    _r4_seed(path)
    config = {**_NO_EXTRA_LANES, "retro_audit": {"enabled": True}}
    calls = []
    recovered = False

    def review(pr, *args):
        calls.append(pr.number)
        return "reviewed" if recovered or pr.number == 2 else "deferred"

    monkeypatch.setattr(engine, "_retro_review_pr", review)
    for _ in range(3):
        _r4_cycle(forge, monkeypatch, path, config)
    _r4_cycle(forge, monkeypatch, path, config)
    assert calls == [1, 1, 1, 2]
    assert state.load_state(path)["retro_cursor"] == older.merged_at
    now = engine.time.time()
    monkeypatch.setattr(engine.time, "time", lambda: now + 90000)
    for _ in range(deferred_retries):
        _r4_cycle(forge, monkeypatch, path, config)
        assert "1" in state.load_state(path)["retro_parked"]
    if deferred_retries == 3:
        monkeypatch.setattr(engine.time, "time", lambda: now + 180000)
    recovered = True
    _r4_cycle(forge, monkeypatch, path, config)
    final = state.load_state(path)
    assert calls == [1, 1, 1, 2] + [1] * (deferred_retries + 1)
    assert final["retro_cursor"] == older.merged_at
    assert "1" not in final.get("retro_parked", {})


@pytest.mark.parametrize("lane", ["post_merge", "retro_audit"])
@pytest.mark.parametrize("listing", ["outage", "envelope", "bad_row"])
def test_real_merged_listing_failure_preserves_review_and_retry_state(tmp_path, monkeypatch, lane, listing):
    class Forge(_R4Forge):
        def list_merged_prs(self, *args):
            if listing == "outage":
                raise ForgeError("ordinary listing outage")
            return {} if listing == "envelope" else [None]

    path = tmp_path / "state.json"
    record = {"head_sha": "a" * 40, "outcome": "reviewed"}
    _r4_seed(path, prs={"7": record}, model_failures={"7:" + "a" * 40: 2})
    before = state.load_state(path)
    config = {**_NO_EXTRA_LANES, "retro_audit": {"enabled": False},
              "omnisweep": {"enabled": False}, lane: {"enabled": True}}
    report = _r4_cycle(Forge(), monkeypatch, path, config)
    after = state.load_state(path)
    assert report.alerts
    assert after["prs"] == before["prs"]
    assert after["model_failures"] == before["model_failures"]


def test_omni_disabled_gatekeeper_preserves_findings_without_call(tmp_path, monkeypatch):
    config = make_config(gatekeeper=False, omnisweep={"enabled": True, "fix": False})
    forge = SimpleNamespace(name="github", list_tree_files=lambda *a: ([("a.py", 20)], False),
                            get_file=lambda *a: "def value(): return 1")
    finding = Finding(rule_id="general", severity="Major", path="a.py", line=1,
                      message="Ordinary concrete defect")
    monkeypatch.setattr(engine, "_probe_head", lambda *a: "a" * 40)
    monkeypatch.setattr(analyzer, "analyze", lambda pr, *a, **kw: ReviewDoc(pr=pr, findings=[finding]))
    monkeypatch.setattr(engine.gatekeeper, "filter_findings", lambda *a: pytest.fail("disabled gatekeeper called"))
    monkeypatch.setattr(engine, "_omni_upsert_issue", lambda *a, **kw: None)
    st = {}
    report = engine.CycleReport(repo=config.repo)
    engine._omnisweep_step(config, forge, tmp_path / "state.json", None, st, report, None)
    assert st["omni_findings"][0]["msg"] == finding.message
    assert report.gatekeeper_dropped == report.gatekeeper_failed == 0


@pytest.mark.parametrize("instances", [False, True])
def test_config_accepts_nested_mappings_and_models(instances):
    raw = {**RAW, "forges": dict(RAW["forges"])}
    if instances:
        raw["model"] = ModelRoute.model_validate(raw["model"])
        raw["fallback_model"] = raw["model"]
        raw["forges"] = {name: ForgeBinding.model_validate(value)
                         for name, value in raw["forges"].items()}
    result = RepoConfig.model_validate(raw)
    assert result.model.model == "test"


@pytest.mark.parametrize("field,bad", [("model", [1]), ("model", 3),
                                       ("fallback_model", [1]), ("fallback_model", 3),
                                       ("key_env", []), ("key_env", {}),
                                       ("token_env", []), ("token_env", {})])
def test_malformed_nested_config_uses_validation_error(field, bad):
    raw = {**RAW, "model": dict(RAW["model"]),
           "forges": {name: dict(value) for name, value in RAW["forges"].items()}}
    if field == "key_env":
        raw["model"][field] = bad
    elif field == "token_env":
        raw["forges"]["github"][field] = bad
    else:
        raw[field] = bad
    with pytest.raises(ValidationError):
        RepoConfig.model_validate(raw)


@pytest.mark.parametrize("step", ["fetch", "checkout"])
def test_verifier_setup_failure_emits_terminal_reason(monkeypatch, step):
    events = []
    monkeypatch.setattr(executor, "_run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, int(argv[:2] == ["git", step]), "", ""))
    monkeypatch.setattr(executor, "_push_token_env", lambda *a: {})
    monkeypatch.setattr(executor, "_drop_askpass", lambda *a: None)
    monkeypatch.setattr(telemetry, "emit", lambda event, **fields: events.append((event, fields)))
    assert executor.verify_diff_tests(_r4_pr(), make_config(), ["tests/test_value.py"]) is None
    terminal = [fields for event, fields in events if event == "verify_tests"]
    assert len(terminal) == 1
    assert terminal[0]["unverified"] is True
    assert terminal[0]["reason"] == f"git {step} failed"


def test_gauntlet_script_performs_successful_changed_file_check(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    gh = '''import sys
a = sys.argv[1:]
if a[:2] == ["repo", "view"]: print("main")
elif a[:2] == ["pr", "list"]:
    fields = a[a.index("--json") + 1].split(",")
    if "number" in fields and "headRefName" in fields: print("17")
elif a[:2] == ["pr", "view"]: print("8")
elif a[0] == "api": print("8")
'''
    for name, source in {"gh": gh, "git": 'print("a" * 40)\n', "sleep": "pass\n"}.items():
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
    result = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "make-gauntlet-pr.sh"),
                             "owner/project", str(work)], capture_output=True, text=True,
                            env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.defpath}, timeout=30)
    assert "gauntlet PR #17: changed_files=8" in result.stdout
    assert result.returncode == 0, result.stderr
