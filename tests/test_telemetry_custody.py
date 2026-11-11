"""Custody sink + boundary-scrub tests (card t_dd1d922a).

The contract under test:
  * every record is scrubbed (scrub.scrub + scrub.redact_credentials) BEFORE
    it is written, on either sink;
  * structured identity values (repo, commit sha, kind, model, ts) survive
    exactly, so downstream registry rows keep their exact-head linkage;
  * `FL4WRITE_TELEMETRY=journald:<ident>` selects the append-only journald
    sink, and custody kinds (guard_refusal / merge_blocked) RAISE when the
    record does not reach it — no silent loss, no local buffer;
  * metric kinds keep the historical never-raise contract.
"""

from __future__ import annotations

import json

import pytest

from fl4write import telemetry as tel

TOKEN = "ghp_FAKE0123456789abcdefghijKLmnopqrst"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(tel, "_STREAM", None)
    yield
    monkeypatch.setattr(tel, "_STREAM", None)


def _lines(path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_boundary_scrub_redacts_secret_before_write(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    tel.emit("guard_refusal", reason=f"push refused, token was {TOKEN}",
             repo="KyaniteLabs/fl4write", head="a" * 40)
    raw = p.read_text()
    assert TOKEN not in raw, raw
    assert "ghp_" not in raw, raw
    assert "[redacted]" in raw
    rec = _lines(p)[0]
    # identity values survive byte-exact
    assert rec["repo"] == "KyaniteLabs/fl4write"
    assert rec["head"] == "a" * 40
    assert rec["kind"] == "guard_refusal"


def test_boundary_scrub_redacts_assignment_and_long_runs(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    tel.emit("fix_attempt", reason="password=SuperSecretValue123 "
                                   "context AKIAABCDEFGHIJKLMNOP")
    raw = p.read_text()
    for leaked in ("SuperSecretValue123", "AKIAABCDEFGHIJKLMNOP"):
        assert leaked not in raw, raw


def test_boundary_scrub_neutralizes_injection_classes(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    tel.emit("review", detail="![x](https://evil.example/pixel.png)"
                              "\u202e<!-- hidden --><script>alert(1)</script>")
    raw = p.read_text()
    assert "evil.example" not in raw
    assert "<script>" not in raw
    assert "<!--" not in raw
    assert "\u202e" not in raw


def test_long_kind_and_model_survive(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    tel.emit("telemetry_custody_gap", model="deepseek-v4.1-flash:1017")
    rec = _lines(p)[0]
    assert rec["kind"] == "telemetry_custody_gap"
    assert rec["model"] == "deepseek-v4.1-flash:1017"


def test_journald_value_selects_custody_sink(monkeypatch):
    monkeypatch.setenv("FL4WRITE_TELEMETRY", "journald:fl4write-telemetry")
    assert tel._sink() == ("journald", "fl4write-telemetry")
    assert tel._sink_is_custody() is True


def test_invalid_journald_value_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("FL4WRITE_TELEMETRY", "journald:not a valid ident")
    monkeypatch.setattr(tel, "_path", lambda: tmp_path / "fallback.jsonl")
    kind, _ = tel._sink()
    assert kind == "file"
    assert tel._sink_is_custody() is False


def test_custody_kind_raises_when_sink_is_down(monkeypatch):
    monkeypatch.setenv("FL4WRITE_TELEMETRY", "journald:fl4write-telemetry")

    def _boom(ident, line):
        raise OSError("journal socket gone")

    monkeypatch.setattr(tel, "_send_journald", _boom)
    with pytest.raises(tel.TelemetryCustodyError):
        tel.emit("guard_refusal", reason="refused")


def test_metric_kind_never_raises_when_sink_is_down(monkeypatch):
    monkeypatch.setenv("FL4WRITE_TELEMETRY", "journald:fl4write-telemetry")

    def _boom(ident, line):
        raise OSError("journal socket gone")

    monkeypatch.setattr(tel, "_send_journald", _boom)
    tel.emit("model_call", model="m", ok=True)  # must not raise


def test_journald_sink_never_touches_the_legacy_file(tmp_path, monkeypatch):
    legacy = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv("FL4WRITE_TELEMETRY", "journald:fl4write-telemetry")
    monkeypatch.setattr(tel, "_path", lambda: legacy)
    sent = []
    monkeypatch.setattr(tel, "_send_journald", lambda i, line: sent.append((i, line)))
    tel.emit("guard_refusal", reason="nope")
    assert sent and sent[0][0] == "fl4write-telemetry"
    assert not legacy.exists()


def test_sink_probe_reads_back(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    res = tel.sink_probe()
    assert res["ok"] is True, res
    assert res["custody"] is False
    assert any(ev.get("kind") == "sink_probe" for ev in _lines(p))


def test_sink_probe_fails_when_write_fails(monkeypatch):
    monkeypatch.setenv("FL4WRITE_TELEMETRY", "journald:fl4write-telemetry")
    monkeypatch.setattr(tel, "_send_journald",
                        lambda i, line: (_ for _ in ()).throw(OSError("down")))
    res = tel.sink_probe()
    assert res["ok"] is False and res["stage"] == "write"


def test_read_events_skips_corrupt_lines(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    p.write_text('{"kind":"model_call","ok":true}\n{"broken":\n[]\n')
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    evs = tel.read_events(limit=10)
    assert [e["kind"] for e in evs] == ["model_call"]


def test_calibration_snapshot_still_reads_file_sink(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    p.write_text('{"kind":"model_call","model":"m1","ok":true,'
                 '"prompt_tokens":3,"completion_tokens":4}\n')
    monkeypatch.setenv("FL4WRITE_TELEMETRY", str(p))
    assert tel.calibration_snapshot()["m1"].startswith("1/1")
