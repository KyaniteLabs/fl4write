"""Outbound write containment — card t_c7791d3f.

The bar: no write leaves FL4WRITE for anything outside KyaniteLabs, and no
token is minted for a personal-account installation. Every refusal test
monkeypatches the transport and asserts it was called ZERO times: a guard that
raises after opening a socket does not pass.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from fl4write import appauth
from fl4write import config as cfg
from fl4write import executor, forges, telemetry, tiers, writeguard

ROOT = Path(__file__).resolve().parent.parent
ENV_SEAMS = {
    "CODESITTER_GITHUB_TOKEN": "test",
    "CODESITTER_DEEPSEEK_KEY": "test",
    "CODESITTER_FORGEJO_TOKEN": "test",
    "FL4WRITE_MODEL_ENDPOINT": "http://127.0.0.1:46399/v1/chat/completions",
    "FL4WRITE_FORGEJO_API_BASE": "http://127.0.0.1:3000/api/v1",
}


@pytest.fixture
def tel(tmp_path, monkeypatch):
    """Point the REAL telemetry writer at a test file (one JSON line/event)."""
    stream = tmp_path / "telemetry.jsonl"
    monkeypatch.setattr(telemetry, "_STREAM", stream)
    return stream


@pytest.fixture
def seams(monkeypatch):
    for name, value in ENV_SEAMS.items():
        monkeypatch.setenv(name, value)


def _events(stream: Path) -> list[dict]:
    if not stream.exists():
        return []
    return [json.loads(line) for line in stream.read_text().splitlines() if line]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _no_net(monkeypatch):
    """Transport that records the attempt and fails the test if reached."""
    calls: list[str] = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise AssertionError(f"socket opened to {req.full_url}")

    monkeypatch.setattr(executor.urllib.request, "urlopen", urlopen)
    return calls


def _ok_net(monkeypatch, body: bytes = b'{"id": 1}'):
    seen: list[dict] = []

    def urlopen(req, timeout=None):
        seen.append({"url": req.full_url, "method": req.get_method()})
        return _Resp(body)

    monkeypatch.setattr(executor.urllib.request, "urlopen", urlopen)
    return seen


# ---------------------------------------------------------------- AC1 (a)
def test_personal_fork_post_is_refused_before_any_io(tel, monkeypatch):
    calls = _no_net(monkeypatch)
    monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "tok")
    with pytest.raises(writeguard.OutOfBoundsWrite) as excinfo:
        executor._gh_api("POST", "/repos/simongonzalezdc/ast-grep/issues",
                         {"title": "would be a write"})
    assert calls == [], "refused write still opened a socket"
    assert excinfo.value.reason == "github_owner_not_allowlisted"
    events = _events(tel)
    assert len(events) == 1, f"expected exactly one guard_refusal line, got {events}"
    assert events[0]["kind"] == "guard_refusal"
    assert {k: events[0][k] for k in ("reason", "host", "repo", "method", "caller")} == {
        "reason": "github_owner_not_allowlisted",
        "host": "api.github.com",
        "repo": "simongonzalezdc/ast-grep",
        "method": "POST",
        "caller": "executor._gh_api",
    }


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_every_mutating_method_is_gated(tel, monkeypatch, method):
    calls = _no_net(monkeypatch)
    with pytest.raises(writeguard.OutOfBoundsWrite):
        executor._gh_api(method, "/repos/simongonzalezdc/x/issues/1", {})
    assert calls == []
    assert [e["method"] for e in _events(tel)] == [method]


def test_unparseable_mutation_target_fails_closed(tel, monkeypatch):
    calls = _no_net(monkeypatch)
    with pytest.raises(writeguard.OutOfBoundsWrite) as excinfo:
        executor._gh_api("POST", "/markdown", {"text": "x"})
    assert calls == []
    assert excinfo.value.reason == "unparseable_write_target"
    assert len(_events(tel)) == 1


# ---------------------------------------------------------------- AC1 (b)
def test_installation_resolution_refuses_personal_repo_before_jwt(tel, monkeypatch):
    calls = _no_net(monkeypatch)

    def _must_not_sign():
        raise AssertionError("App JWT minted for a personal-account installation")

    monkeypatch.setattr(appauth, "_make_jwt", _must_not_sign)
    with pytest.raises(writeguard.OutOfBoundsWrite) as excinfo:
        appauth.resolve_installation_id("simongonzalezdc/x")
    assert calls == []
    assert excinfo.value.reason == "github_owner_not_allowlisted"
    events = _events(tel)
    assert len(events) == 1
    assert events[0]["caller"] == "appauth.resolve_installation_id"
    assert events[0]["repo"] == "simongonzalezdc/x"


def test_installation_resolution_refuses_malformed_repo(tel):
    for bad in ("not a repo", "KyaniteLabs/../simongonzalezdc/x",
                "KyaniteLabs/x/y", "KyaniteLabs/x?ref=main"):
        with pytest.raises(writeguard.OutOfBoundsWrite):
            appauth.resolve_installation_id(bad)


# ---------------------------------------------------------------- AC1 (c)
def test_allowed_kyanitelabs_github_write_passes(tel, monkeypatch):
    seen = _ok_net(monkeypatch)
    monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "tok")
    assert executor._gh_api("POST", "/repos/KyaniteLabs/fl4write/issues",
                            {"title": "x"}) == {"id": 1}
    assert seen == [{"url": "https://api.github.com/repos/KyaniteLabs/fl4write/issues",
                     "method": "POST"}]
    assert _events(tel) == []


def test_github_read_of_a_personal_repo_is_not_a_write(tel, monkeypatch):
    # The guard bounds WRITES (the card's blast radius); reads stay available
    # so refusal telemetry and migration work can still inspect a repo.
    seen = _ok_net(monkeypatch, b'{"default_branch": "main"}')
    assert executor._gh_api("GET", "/repos/simongonzalezdc/x") == {"default_branch": "main"}
    assert len(seen) == 1 and _events(tel) == []


# ---------------------------------------------------------------- AC1 (d)
def test_allowed_simon_org_forgejo_write_passes(tel, monkeypatch):
    seen = _ok_net(monkeypatch, b'{"id": 3}')
    binding = cfg.ForgeBinding(role="primary",
                               api_base="https://git.kyanitelabs.tech/api/v1",
                               token_env="GHT")
    adapter = forges.ForgejoAdapter(binding)
    assert adapter._call("POST", "/repos/simon/CNCL/issues",
                         {"title": "t"}) == {"id": 3}
    assert seen[0]["url"] == "https://git.kyanitelabs.tech/api/v1/repos/simon/CNCL/issues"
    assert _events(tel) == []


def test_tailnet_api_base_is_the_same_org_forgejo(tel, monkeypatch):
    # The tailnet literal lives only in the runner env (run-cycle.sh); the
    # guard reads the same variable, so the two cannot disagree.
    monkeypatch.setenv("FL4WRITE_FORGEJO_API_BASE", "http://203.0.113.10:3000/api/v1")
    seen = _ok_net(monkeypatch, b'{"id": 4}')
    binding = cfg.ForgeBinding(role="primary",
                               api_base="http://203.0.113.10:3000/api/v1",
                               token_env="GHT")
    assert forges.ForgejoAdapter(binding)._call(
        "POST", "/repos/KyaniteLabs/x/issues", {"title": "t"}) == {"id": 4}
    assert len(seen) == 1


def test_forgejo_personal_namespace_elsewhere_is_refused(tel, monkeypatch):
    calls = _no_net(monkeypatch)
    binding = cfg.ForgeBinding(role="primary",
                               api_base="https://git.example.com/api/v1",
                               token_env="GHT")
    with pytest.raises(writeguard.OutOfBoundsWrite) as excinfo:
        forges.ForgejoAdapter(binding)._call("POST", "/repos/KyaniteLabs/x/issues", {})
    assert calls == []
    assert excinfo.value.reason == "host_not_allowlisted"
    assert len(_events(tel)) == 1


def test_personal_owner_on_the_org_forgejo_is_refused(tel, monkeypatch):
    calls = _no_net(monkeypatch)
    binding = cfg.ForgeBinding(role="primary",
                               api_base="https://git.kyanitelabs.tech/api/v1",
                               token_env="GHT")
    with pytest.raises(writeguard.OutOfBoundsWrite) as excinfo:
        forges.ForgejoAdapter(binding)._call("POST", "/repos/mallory/x/issues", {})
    assert calls == []
    assert excinfo.value.reason == "forgejo_owner_not_allowlisted"


# ---------------------------------------------------------------- planning
def test_fork_configs_are_out_of_the_root_glob():
    root = sorted(p.name for p in ROOT.glob("*.fl4write.yaml"))
    assert not any(name.endswith(".fork.fl4write.yaml") for name in root), (
        "a personal-fork config is back in the planner's root glob")
    parked = sorted(ROOT.glob("parked-forks/*.fork.fl4write.yaml"))
    assert len(parked) == 44
    for path in parked:
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, dict) and data["repo"].startswith("simongonzalezdc/"), path.name


def test_plan_contains_zero_non_allowlisted_repos(tel, seams, monkeypatch, capsys):
    monkeypatch.setattr(tiers, "_probe_pushed", lambda *a, **k: {})
    # Same enumeration as the runner's shell glob: root, non-hidden only.
    configs = sorted(str(p) for p in ROOT.glob("*.fl4write.yaml")
                     if not p.name.startswith("."))
    assert configs, "fleet checkout expected"
    monkeypatch.setattr(sys, "argv", ["fl4write.tiers", "--plan", *configs])
    assert tiers.main() == 0
    plan = json.loads(capsys.readouterr().out)

    allowed: dict[str, bool] = {}
    for name in configs:
        c = cfg.load_config(name)
        primary = next(b for b in c.forges.values() if b.role == "primary")
        allowed[name.rsplit("/", 1)[-1]] = writeguard.write_allowed(primary.api_base, c.repo)
    parked = sorted(n for n, ok in allowed.items() if not ok)
    # Measured at this commit: the personal-account PRODUCT configs (not forks)
    # that the guard parks out of the plan. A new one must show up here.
    assert len(parked) == 33, parked
    assert f"{len(parked)} parked (non-allowlisted)" in plan["summary"]
    assert not set(plan["due"]) & set(parked), "the plan scheduled a refused target"


def test_planner_parks_a_personal_config_instead_of_scheduling_it(tel, seams, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(tiers, "_probe_pushed", lambda *a, **k: {})
    src = sorted(ROOT.glob("parked-forks/*.fork.fl4write.yaml"))[0]
    target = tmp_path / src.name
    target.write_text(src.read_text())
    monkeypatch.setattr(sys, "argv", ["fl4write.tiers", "--plan", str(target)])
    assert tiers.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["due"] == []
    assert "1 parked (non-allowlisted)" in plan["summary"]
    assert any("parked by the outbound write guard" in a for a in plan["alerts"])
