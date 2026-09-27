"""Lever 3 (2026-09-27): the LAN-endpoint adjudication carve-out + ${VAR} config expansion.

(a) Recon re-flagged the fleet's ~100 *.fl4write.yaml model endpoints as
    privacy/insecurity findings every round (the desk re-invalidated them every
    round). The carve-out adjudicates plain-http model transport to non-routable
    private/loopback hosts as the documented accepted LAN self-hosting posture —
    narrow: public/routable endpoints and credentials still flag.
(b) The tracked fl4write.fl4write.yaml keeps a ${FL4WRITE_MODEL_ENDPOINT}
    placeholder; the launcher environment supplies the real endpoint, and the
    credential destination guard still accepts the expanded value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fl4write import exhaustive
from fl4write.config import (
    assert_credential_endpoints_trusted,
    credential_endpoint_trusted,
    load_config,
)
from fl4write.exhaustive import _RECON_SYSTEM
from fl4write.exhaustive_adjudication import accepted_lan_endpoint, apply_endpoint_carveout
from test_exhaustive_fix import _config

TRACKED = Path(__file__).resolve().parents[1] / "fl4write.fl4write.yaml"
LIVE_MESSAGE = (
    "Configuration exposes a private IP address (192.168.1.72), violating the "
    "privacy policy which prohibits private IPs in the tree."
)


def _finding(evidence, message=LIVE_MESSAGE, **over):
    row = {"path": "DialectOS.fl4write.yaml", "line": 8, "evidence": evidence,
           "severity": "Major", "message": message}
    row.update(over)
    return row


# ---------------------------------------------------------------- (a) carve-out
class TestEndpointCarveout:
    @pytest.mark.parametrize("evidence", [
        "endpoint: http://192.168.1.72:8908/v1/chat/completions",  # the live round's exact row
        "  endpoint: http://192.168.1.72:8908/v1/chat/completions",
        # the form the worker actually classifies: scrub.redact_credentials
        # collapses port+path into [redacted], the host must still carve
        "  endpoint: http://192.168.1.72:[redacted]",
        "  endpoint: http://127.0.0.1:[redacted]",
        'endpoint: "http://192.168.1.72:8908/v1/chat/completions"',
        "endpoint: http://10.1.2.3/v1/chat/completions",
        "endpoint: http://172.20.15.9:11434/v1",
        "endpoint: http://127.0.0.1:46399/v1/chat/completions",
        "endpoint: http://localhost:11434/v1",
        "endpoint: http://[::1]:8080/v1",
        "endpoint: http://[fe80::1]:8080/v1",
        "endpoint: https://192.168.1.5/v1",  # host is non-routable; scheme is not the trigger
    ])
    def test_private_and_loopback_model_endpoints_are_accepted(self, evidence):
        assert accepted_lan_endpoint(_finding(evidence))

    @pytest.mark.parametrize("evidence", [
        "endpoint: http://203.0.113.9/v1",  # documentation space, not RFC1918 — still flags
        "endpoint: http://8.8.8.8:8908/v1",
        "endpoint: http://api.example.com/v1",
        "endpoint: http://192.168.1.72.evil.example/v1",  # private-looking public name
        "endpoint: http://user:hunter2@192.168.1.5:9/v1",  # credential in userinfo
        "endpoint: http://192.168.1.5:9/v1?token=abc",  # credential query parameter
        "endpoint: http://192.168.1.5:9/v1?api_key=abc",
        'requests.post("http://192.168.1.72:8908/v1/chat/completions")',  # not an endpoint: line
        "url = http://192.168.1.72:8908/v1/chat/completions",
    ])
    def test_public_and_credential_cases_still_flag(self, evidence):
        assert not accepted_lan_endpoint(_finding(evidence))

    def test_credential_asserting_message_on_a_private_endpoint_still_flags(self):
        assert not accepted_lan_endpoint(_finding(
            "endpoint: http://192.168.1.5:9/v1", message="API key is sent to a private endpoint"))

    def test_carveout_splits_round_findings(self):
        lan = _finding("endpoint: http://192.168.1.72:8908/v1/chat/completions")
        public = _finding("endpoint: http://8.8.8.8:8908/v1")
        real = {"path": "value.py", "line": 1, "evidence": "VALUE = 1",
                "severity": "Major", "message": "bad value"}
        actionable, carved = apply_endpoint_carveout([real, lan, public])
        assert actionable == [real, public]
        assert carved == [lan]

    def test_recon_system_prompt_states_the_accepted_posture(self):
        assert "documented LAN self-hosting" in _RECON_SYSTEM
        assert "${ENV} endpoint placeholder" in _RECON_SYSTEM


def _worker_request(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "DialectOS.fl4write.yaml").write_text(
        "repo: fixture/DialectOS\n"
        "model:\n"
        "  endpoint: http://192.168.1.72:8908/v1/chat/completions\n"
        "  model: Qwen3.8-27B\n"
        "  key_env: ''\n"
    )
    (tree / "value.py").write_text("VALUE = 1\n")
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"ledger": []}')
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "tree": str(tree), "ledger": str(ledger), "result": str(tmp_path / "result.json"),
        "checkpoint": str(tmp_path / "checkpoint.json"),
        "binding": {"round": 1, "head": "a" * 40},
        "route": _config().model.model_dump(), "chunk_chars": 48_000,
        "max_calls": 4, "max_tokens": 4 * _config().model.max_tokens,
    }))
    return request


def test_recon_worker_carves_the_lan_endpoint_and_records_it(tmp_path):
    request = _worker_request(tmp_path)
    queue = [
        {"findings": [{
            "path": "DialectOS.fl4write.yaml", "line": 3,
            "evidence": "  endpoint: http://192.168.1.72:8908/v1/chat/completions",
            "severity": "Major", "message": LIVE_MESSAGE,
        }]},
        {"findings": [{
            "path": "value.py", "line": 1, "evidence": "VALUE = 1",
            "severity": "Major", "message": "bad value",
        }]},
    ]

    def model(*args):
        return json.dumps(queue.pop(0))

    assert exhaustive._worker(request, model) == 0
    result = json.loads((tmp_path / "result.json").read_bytes())
    assert [row["path"] for row in result["findings"]] == ["value.py"]
    carved = result["carved_findings"]
    assert [row["path"] for row in carved] == ["DialectOS.fl4write.yaml"]
    # evidence rides in its scrubbed form (port+path collapse to [redacted]);
    # the non-routable host is what the carve-out classifies on
    assert carved[0]["evidence"] == "  endpoint: http://192.168.1.72:[redacted]"


# ----------------------------------------------------------- (b) env expansion
class TestEnvExpansion:
    def test_tracked_config_resolves_the_endpoint_from_the_environment(self, monkeypatch):
        endpoint = "http://192.168.1.72:8908/v1/chat/completions"
        monkeypatch.setenv("FL4WRITE_MODEL_ENDPOINT", endpoint)
        config = load_config(TRACKED)
        assert config.model.endpoint == endpoint

    def test_expanded_endpoint_is_accepted_by_the_destination_guard(self, monkeypatch):
        endpoint = "http://192.168.1.72:8908/v1/chat/completions"
        monkeypatch.setenv("FL4WRITE_MODEL_ENDPOINT", endpoint)
        config = load_config(TRACKED)
        assert credential_endpoint_trusted(config.model.endpoint, trusted="192.168.1.72:8908")
        assert not credential_endpoint_trusted(config.model.endpoint, trusted="")

    def test_missing_endpoint_environment_fails_loudly(self, monkeypatch):
        monkeypatch.delenv("FL4WRITE_MODEL_ENDPOINT", raising=False)
        with pytest.raises(ValueError, match="FL4WRITE_MODEL_ENDPOINT"):
            load_config(TRACKED)

    def test_credential_route_on_the_expanded_endpoint_passes_admission(self, tmp_path, monkeypatch):
        endpoint = "http://192.168.1.72:8908/v1/chat/completions"
        monkeypatch.setenv("FL4WRITE_TEST_MODEL_ENDPOINT", endpoint)
        monkeypatch.setenv("FL4WRITE_TRUSTED_MODEL_ENDPOINTS", "192.168.1.72:8908")
        path = tmp_path / "widget.fl4write.yaml"
        path.write_text(
            "repo: acme/widget\n"
            "forges:\n"
            "  origin: {role: primary, api_base: 'https://api.github.com', token_env: TEST_FORGE_TOKEN}\n"
            "model:\n"
            "  endpoint: ${FL4WRITE_TEST_MODEL_ENDPOINT}\n"
            "  model: m\n"
            "  key_env: MODEL_KEY\n"
        )
        config = load_config(path)
        assert config.model.endpoint == endpoint
        assert_credential_endpoints_trusted(config, source=str(path))

    def test_expansion_cannot_bypass_strict_booleans(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FL4WRITE_TEST_FLAG", "true")
        path = tmp_path / "widget.fl4write.yaml"
        path.write_text(
            "repo: acme/widget\n"
            "forges:\n"
            "  origin: {role: primary, api_base: 'https://api.github.com', token_env: TEST_FORGE_TOKEN}\n"
            "model:\n"
            "  endpoint: http://127.0.0.1:9/v1\n"
            "  model: m\n"
            "shadow: ${FL4WRITE_TEST_FLAG}\n"
        )
        with pytest.raises(ValueError, match="boolean field 'shadow'"):
            load_config(path)
