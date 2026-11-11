"""App-auth fallback must keep the selected PAT account's comment identity."""
import json

import pytest

from fl4write import appauth, cli, renderer, telemetry
from fl4write.engine import CycleReport
from fl4write.forges import GitHubAdapter
from test_fl4write import make_config


@pytest.mark.parametrize("app_available", [False, True], ids=["configured-pat", "minted-app"])
def test_cli_keeps_persistent_comments_findable_for_effective_auth(monkeypatch, app_available):
    config = make_config(repo="KyaniteLabs/fixture", bot_login="fixture-service-account")
    expected = "fl4write[bot]" if app_available else config.bot_login
    body = renderer.LEGACY_MARKER_PREFIXES[0] + " fixture review"
    monkeypatch.setattr("sys.argv", ["fl4write.cli", "fixture.yaml"])
    monkeypatch.setattr(cli, "_install_sigterm_handler", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "make_get_diff", lambda _: lambda pr: None)
    monkeypatch.setattr(cli, "_org_model_keys", lambda: None)
    monkeypatch.setattr(cli, "_probe_adoption", lambda *a: None)
    monkeypatch.setattr(cli, "_cycle_budget_s", lambda: 30)
    monkeypatch.setattr(telemetry, "calibration_snapshot", lambda: None)
    monkeypatch.setattr(telemetry, "route_stats", lambda: {})
    monkeypatch.setenv("GHT", "fixture-pat-token")

    def mint(**kwargs):
        if not app_available:
            raise RuntimeError("fixture app unavailable")
        monkeypatch.setenv("CODESITTER_GITHUB_TOKEN", "fixture-app-token")

    monkeypatch.setattr(appauth, "install_token_to_env", mint)

    def cycle(selected, *args, **kwargs):
        adapter = GitHubAdapter(selected.forges["github"])
        adapter.bot_login = selected.bot_login
        monkeypatch.setattr(adapter, "_paginated", lambda *a, **kw: [
            {"id": 7, "user": {"login": expected}, "body": body},
        ])
        assert adapter.get_persistent_comment(selected.repo, 1) == (7, body)
        assert selected.bot_login == expected
        return CycleReport(repo=selected.repo)

    monkeypatch.setattr(cli, "run_cycle", cycle)
    assert cli.main() == 0


def test_cli_refuses_the_pat_fallback_outside_the_allowlist(monkeypatch, tmp_path, capsys):
    """A personal-account repo must not run at all when App auth fails: the
    fallback would swap fl4write[bot] for a full-scope personal PAT."""
    config = make_config(repo="simongonzalezdc/ast-grep", bot_login="simongonzalezdc")
    monkeypatch.setattr("sys.argv", ["fl4write.cli", "fixture.yaml"])
    monkeypatch.setattr(cli, "_install_sigterm_handler", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "make_get_diff", lambda _: lambda pr: None)
    monkeypatch.setattr(telemetry, "_STREAM", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(appauth, "install_token_to_env",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("app unavailable")))
    ran: list[int] = []
    monkeypatch.setattr(cli, "run_cycle", lambda *a, **k: ran.append(1))
    assert cli.main() == 2
    assert ran == [], "cycle ran on a refused PAT fallback"
    err = capsys.readouterr().err
    assert "refusing the PAT fallback" in err
    events = [json.loads(ln) for ln in (tmp_path / "telemetry.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["guard_refusal"]
    assert events[0]["reason"] == "pat_fallback_not_allowlisted"
