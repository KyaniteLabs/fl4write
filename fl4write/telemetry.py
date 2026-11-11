"""Plus-ultra telemetry — the append-only event stream future improvement
rounds read (CEO order 2026-09-02: "plus ultra the telemetry for future
runs"). One JSONL line per event; never raises, never blocks, never truncates
the truth. The calibration loop (fl4write #5) consumes this instead of
asking models to re-derive history.

Event kinds:
  model_call   — route, latency, tokens (prompt/completion), finish_reason,
                 parse_ok. The usage field was previously DISCARDED — the
                 single richest calibration signal we had and threw away.
  gatekeeper   — kept/dropped/demoted counts + demotion identities.
  review       — lane (pr/postmerge/retro/omni), findings by severity
                 BEFORE and AFTER gates, posted_severity final, route mix.
  fix_attempt  — status, reason head, latency, lane.
  verify_tests — cmd, files, head, outcome.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import scrub as _scrub

_STREAM: Path | None = None

# --- custody sink (card t_dd1d922a) -----------------------------------------
# The stream has TWO possible destinations, chosen by FL4WRITE_TELEMETRY:
#
#   journald:<IDENT>   append to the systemd journal over its AF_UNIX
#                      datagram socket (DEFAULT for the runner). The journal
#                      files are root:systemd-journal and the ingest socket
#                      rejects open(O_TRUNC) — the producing process (uid
#                      simon) can APPEND and READ but cannot truncate,
#                      rewrite, delete or rotate a record. Rotation/eviction
#                      is journald's (uid 0) alone. This is the custody sink.
#   /path/...jsonl     append to a file. Kept for tests, ad-hoc runs and
#                      historical replay. NOT a custody sink: the writer
#                      owns the file and can rewrite it, so guard-refusal
#                      records must never land here in the runner.
#
# Every record is scrubbed at this boundary BEFORE it is written, whichever
# sink is in use: `scrub.scrub()` (control/bidi/exfil/injection classes) plus
# `scrub.redact_credentials()` (credential material) on free-text values.
# Structured identity values (repo, commit sha, kind, model, ts) keep their
# exact bytes only when they match their strict shape — otherwise they are
# redacted like any other text (fail-closed: an unexpected shape is treated
# as potentially hostile).
DEFAULT_JOURNAL_SINK = "journald:fl4write-telemetry"
JOURNAL_SOCKET = "/run/systemd/journal/socket"
LEGACY_TELEMETRY = Path.home() / ".fl4write" / "telemetry.jsonl"

# Kinds that are compliance records, not metrics. A custody write failure for
# these MUST NOT be swallowed: the caller has to abort. Everything else keeps
# the historical best-effort contract (a lost metric line beats a lost cycle).
_CUSTODY_KINDS = frozenset({"guard_refusal", "merge_blocked",
                            "telemetry_custody_gap"})

_MAX_LINE = 32 * 1024
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}$")
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_.:@/+=-]{1,128}$")
_SHA_KEYS = frozenset({"head", "base", "sha", "commit", "merge_sha",
                        "head_sha", "base_sha"})
_PLAIN_KEYS = frozenset({"ts", "kind", "lane", "model", "route", "status",
                         "outcome", "verdict"})
_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
# The probe nonce is a GENERATED id (never untrusted text), and the probe is
# unreadable without it — so it is shape-exempt from the run redactor. Strict
# shape: lowercase hex only, so nothing credential-shaped can ride along.
_PROBE_KEYS = frozenset({"nonce"})
_PROBE_NONCE_RE = re.compile(r"^[0-9a-f]{8,24}$")


class TelemetryCustodyError(RuntimeError):
    """A custody record (guard_refusal / merge_blocked) did not reach the
    append-only sink. Callers MUST treat this as a hard stop: proceeding
    would mean acting without a durable refusal record."""


def _sink() -> tuple[str, str]:
    """Resolve the destination from FL4WRITE_TELEMETRY on every call.

    Returns ("journald", ident) or ("file", path). Deliberately uncached: a
    long-lived process (the runner) may be re-pointed, and tests flip the
    variable per test."""
    raw = (os.environ.get("FL4WRITE_TELEMETRY") or "").strip()
    if raw.startswith("journald:"):
        ident = raw.split(":", 1)[1].strip()
        if _IDENT_RE.match(ident):
            return "journald", ident
        # fail SAFE, not silent-open: an unusable journald value names no
        # sink, so fall back to the file destination and let the shape guard
        # in _path() decide. _sink_is_custody() reports False either way.
    return "file", str(_path())


def _sink_is_custody() -> bool:
    return _sink()[0] == "journald"


def _scrub_value(key: str, value: Any) -> Any:
    """Boundary scrub for one field value. Never raises."""
    if not isinstance(value, str):
        return value
    try:
        s = _scrub.controls(value)
        if key in _SHA_KEYS and _SHA_RE.match(s):
            return s
        if key in _PROBE_KEYS and _PROBE_NONCE_RE.match(s):
            return s
        if key == "repo" and _REPO_RE.match(s):
            return s
        if key in _PLAIN_KEYS and _PLAIN_RE.match(s):
            return s
        return _scrub.redact_credentials(_scrub.scrub(s))
    except Exception:  # noqa: BLE001 — a scrub bug must not lose the record
        return "[unscrubbable]"


def scrub_record(event: dict[str, Any]) -> dict[str, Any]:
    """Apply the boundary scrub to every value of a record (recursively for
    one level of dict/list, which is all the emitters use)."""
    out: dict[str, Any] = {}
    for k, v in event.items():
        key = str(k)
        if isinstance(v, dict):
            out[key] = {str(kk): _scrub_value(str(kk), vv) for kk, vv in v.items()}
        elif isinstance(v, (list, tuple)):
            out[key] = [_scrub_value(key, x) for x in v]
        else:
            out[key] = _scrub_value(key, v)
    return out


def _serialize(event: dict[str, Any]) -> str:
    """JSONL line, bounded. Truncation keeps the record (with a marker)
    rather than dropping it: a truncated failure record still proves the
    failure happened."""
    line = json.dumps(event, default=str)
    if len(line) > _MAX_LINE:
        trimmed = {}
        for k, v in event.items():
            trimmed[k] = v if not isinstance(v, str) else v[:2000]
        trimmed["truncated"] = True
        line = json.dumps(trimmed, default=str)[:_MAX_LINE]
    return line


def _send_journald(ident: str, line: str) -> None:
    """Append one MESSAGE to the journal. Raises on failure — journald is
    the only writable path, and a closed socket means no record."""
    payload = ("SYSLOG_IDENTIFIER=%s\nPRIORITY=6\nMESSAGE=%s\n"
               % (ident, line)).encode("utf-8", "replace")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2.0)
        sock.sendto(payload, JOURNAL_SOCKET)
    finally:
        sock.close()


def _write_line(line: str) -> None:
    kind, target = _sink()
    if kind == "journald":
        _send_journald(target, line)
        return
    with Path(target).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _path() -> Path:
    global _STREAM
    if _STREAM is None:
        _STREAM = Path(os.environ.get(
            "FL4WRITE_TELEMETRY",
            Path.home() / ".fl4write" / "telemetry.jsonl",
        ))
        # Cycle-1 fix: the stream path is env-controlled. Refuse values
        # that could redirect appends into a repo's .git directory (e.g. a
        # hook overwrite) or non-log destinations; fall back to default.
        try:
            ok_suffix = _STREAM.suffix in {".jsonl", ".log", ".ndjson"}
            ok_parts = ".git" not in _STREAM.parts
        except Exception:  # noqa: BLE001 — unparseable path is unsafe
            ok_suffix, ok_parts = False, False
        if not (ok_suffix and ok_parts):
            _STREAM = Path.home() / ".fl4write" / "telemetry.jsonl"
        try:
            _STREAM.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return _STREAM


def emit(kind: str, **fields: Any) -> None:
    """Append one event, scrubbed at the boundary BEFORE it is written.

    Metrics stay best-effort (a lost metric line beats a lost review cycle).
    CUSTODY kinds (guard_refusal / merge_blocked / telemetry_custody_gap) are
    compliance records: if one did not reach the append-only sink this raises
    TelemetryCustodyError and the caller MUST abort. Nothing is buffered
    locally — a buffer the producing account can rewrite would defeat the
    custody property the record exists to provide."""
    custody = kind in _CUSTODY_KINDS
    try:
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "kind": kind, **fields}
        _write_line(_serialize(scrub_record(event)))
    except Exception as exc:  # noqa: BLE001 — telemetry never takes the product down
        if custody:
            raise TelemetryCustodyError(
                "custody record %r did not reach the %s sink: %s"
                % (kind, _sink()[0], exc)) from exc
        return


def route_stats() -> dict[str, dict[str, float]]:
    """Aggregate the current process's model-call stats (per route/model):
    calls, ok, parse_failures, total latency, tokens. Surfaced per cycle."""
    return {k: dict(v) for k, v in _ROUTE_STATS.items()}


_ROUTE_STATS: dict[str, dict[str, float]] = {}


def _safe_int(v: object) -> int:
    # F13-B008: booleans are not token counts — 'true' used to count as 1
    if isinstance(v, bool):
        return 0
    # F12-B011 (reopened F2-108): provider numbers can be non-finite
    # (1e309 parses to inf and int(inf) raises OverflowError) — a hostile or
    # corrupt event used to crash calibration AFTER a completed cycle
    try:
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return 0
        n = int(v or 0)  # "unknown"/None/floats never raise (Sol#5)
        return n if n > 0 else 0  # negative token counts are corrupt
    except (TypeError, ValueError, OverflowError):
        return 0


def record_route(model: str, ok: bool, latency_s: float, parse_ok: bool,
                 prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    try:
        key = str(model)
        st = _ROUTE_STATS.setdefault(key, {
            "calls": 0, "ok": 0, "parse_fail": 0, "latency_s": 0.0,
            "prompt_tokens": 0, "completion_tokens": 0,
        })
        latency_s = float(latency_s or 0.0)
        if latency_s < 0 or not math.isfinite(latency_s):
            # F14-B001: negative latency is corrupt data. Round-4 hunt:
            # NaN/inf poisoned the running aggregate permanently (nan +
            # anything stays nan) — non-finite reads as 0.0 like negatives.
            latency_s = 0.0
        st["calls"] += 1
        st["ok"] += int(bool(ok))
        st["parse_fail"] += int(not parse_ok)
        st["latency_s"] += latency_s
        st["prompt_tokens"] += _safe_int(prompt_tokens)
        st["completion_tokens"] += _safe_int(completion_tokens)
    except Exception:  # telemetry never raises (Sol#5)
        pass


def _read_tail(path, max_bytes: int = 8 * 1024 * 1024) -> str:
    """F8-012: bounded read of an append-only stream tail — never loads the
    whole (unbounded) file; the first partial line is discarded."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="replace")
    with path.open("rb") as fh:
        fh.seek(size - max_bytes)
        tail = fh.read()
    text = tail.decode("utf-8", errors="replace")
    nl = text.find("\n")
    return text[nl + 1:] if nl != -1 else text


def calibration_snapshot(recent: int = 500) -> dict[str, Any]:
    """L3 (GLM-B4): the feedback loop CONSUMES the stream — per-model call
    health over the last N model_call events (MECE round-4 F4-4: the
    docstring promised a per-route severity mix this aggregation never
    computed — body now matches the promise). Returns {} when empty."""
    try:
        # MECE round-6 (luna F6-002): the stream may hold corrupt bytes (kill
        # mid-append) — telemetry never raises by contract, and this call runs
        # AFTER the cycle in the CLI; a UnicodeDecodeError here crashed the
        # process on the way out.
        # MECE round-8 (sol F8-012): read a BOUNDED tail (the stream is
        # append-only and never truncated) instead of loading it whole.
        chunk = _read_stream(max_bytes=8 * 1024 * 1024)
        # F11-B006 (round 11, luna DOM-B, reopened F8-011): the BYTE-bounded
        # tail is the bound — slicing [-recent*12:] by LINES discarded
        # qualifying model_call events whenever noise events outnumbered the
        # slack, silently erasing failures from calibration
        lines = chunk.splitlines()
    except OSError:
        return {}
    if recent <= 0:
        return {}
    models: dict[str, dict[str, int]] = {}
    # F8-011: the contract is the last N model-CALL events — slicing the
    # last 3N raw lines erased calibration when reviews outnumbered calls.
    # Walk backward from the tail until exactly N model_call events are seen.
    collected = 0
    order: list[str] = []
    for ln in reversed(lines):
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue  # a stray JSON scalar must not break the snapshot (Sol#5)
        if ev.get("kind") == "model_call" and "ok" in ev:
            # F9-001: only OUTCOME events count — pre-validation events (no
            # ok field) must never default to healthy
            order.append(ln)
            collected += 1
            if collected >= recent:
                break
    for ln in reversed(order):
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue  # a stray JSON scalar must not break the snapshot (Sol#5)
        if ev.get("kind") == "model_call":
            # F11-B007: ok must be a REAL boolean — the string "false" is
            # truthy and used to count as a success; malformed events are
            # quarantined (neither ok nor fail) instead of corrupting ratios
            _ok = ev.get("ok")
            if not isinstance(_ok, bool):
                continue
            m = models.setdefault(str(ev.get("model", "?")), {"calls": 0, "fails": 0, "tokens": 0})
            m["calls"] += 1
            m["fails"] += int(not _ok)
            # MECE round-2 (M3 DOM-B): provider usage fields can arrive as
            # strings ("unknown") — never let the snapshot crash on them
            m["tokens"] += (_safe_int(ev.get("completion_tokens"))
                            + _safe_int(ev.get("prompt_tokens")))
    if not models:
        return {}
    out = {}
    for m, st in models.items():
        out[m] = f"{st['calls'] - st['fails']}/{st['calls']} ok, {st['tokens']} tok"
    return out


# --- reading the stream back ------------------------------------------------
def _journal_lines(ident: str, max_lines: int = 20000,
                   timeout: float = 30.0) -> str:
    """Read the MESSAGE payloads of our own journal entries, oldest-first.

    `-o cat` prints MESSAGE only, which for this sink is exactly the JSONL
    line that was written. Unreadable journal (or a missing journalctl) is an
    empty stream, never an exception: reading must not take the cycle down."""
    try:
        cp = subprocess.run(
            ["journalctl", "--no-pager", "-o", "cat", "-n", str(max_lines),
             "SYSLOG_IDENTIFIER=" + ident],
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout if cp.returncode == 0 else ""


def _read_stream(max_bytes: int = 8 * 1024 * 1024) -> str:
    """The stream as text for consumers (calibration, registry generation).
    Same bytes as the sink holds; the custody sink is read back through
    journalctl, a file sink through the file."""
    kind, target = _sink()
    if kind == "journald":
        return _journal_lines(target)[-max_bytes:]
    return _read_tail(Path(target), max_bytes=max_bytes)


def read_events(limit: int = 500) -> list[dict[str, Any]]:
    """Parsed events, oldest-first, last `limit` records. Malformed lines are
    skipped (a kill mid-append is not a reason to lose the rest)."""
    evs: list[dict[str, Any]] = []
    for ln in _read_stream().splitlines():
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict):
            evs.append(ev)
    return evs[-limit:] if limit and limit > 0 else evs


def sink_probe() -> dict[str, Any]:
    """Write one probe record and READ IT BACK from the sink. This is the
    preflight the runner uses: `python3 -m fl4write.telemetry --probe`.
    A probe that was written but not read back means the sink is not durable
    as seen by this identity, and custody records must not be attempted."""
    kind, target = _sink()
    nonce = "%x" % ((os.getpid() << 48) ^ time.time_ns())
    try:
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "kind": "sink_probe", "nonce": nonce}
        _write_line(_serialize(scrub_record(event)))
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return {"ok": False, "sink": "%s:%s" % (kind, target),
                "stage": "write", "error": str(exc)}
    seen = any(ev.get("nonce") == nonce for ev in read_events(limit=200))
    return {"ok": seen, "sink": "%s:%s" % (kind, target),
            "custody": kind == "journald", "nonce": nonce,
            **({} if seen else {"stage": "readback",
                                "error": "probe not readable from the sink"})}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m fl4write.telemetry",
        description="telemetry sink probe / JSONL export (custody preflight)")
    ap.add_argument("--probe", action="store_true",
                    help="write one probe record and read it back; exit 1 on failure")
    ap.add_argument("--export", type=int, default=0, metavar="N",
                    help="print the last N records as JSONL (read-only view)")
    args = ap.parse_args(argv)
    if args.probe:
        res = sink_probe()
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    if args.export:
        for ev in read_events(limit=args.export):
            print(json.dumps(ev, default=str))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
