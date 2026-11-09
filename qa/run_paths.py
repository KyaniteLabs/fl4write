#!/usr/bin/env python3
"""FL4WRITE operator-path catalog driver — executed by qa/run.sh.

Reads qa/operator-paths.yaml, preflights it (schema + `bash -n` on every
command), then executes every required entry under the isolated QA
environment and checks the machine assertions (expect_exit / expect_stdout /
expect_stderr / expect_log_delta / expect_no_log_delta / *_empty).

Evidence per entry: $QA_WORK/evidence/<id>.{out,err,rc,json} plus a
human-readable run log. Exit 0 iff every required path passed;
fixture-gated restricted paths are reported as BLOCKED, never as PASS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

REQUIRED_KEYS = ("id", "surface", "command", "expected", "sources")
BASE_TIMEOUT_S = 300


def qa_env() -> dict[str, str]:
    keep = (
        "QA_SRC", "QA_WORK", "QA_HOME", "QA_CHECKOUT", "QA_VENV", "QA_SANDBOX",
        "QA_LOCK", "QA_RUNLOG", "QA_FIXTURE", "QA_SHIM_GH", "QA_SHIM_NOFLOCK",
        "MERGE_SHA",
    )
    env = {k: os.environ[k] for k in keep if k in os.environ}
    # Constructed environment: no host credentials, no host HOME. The venv is
    # the ONLY python on PATH; FL4WRITE_FORGEJO_API_BASE is deliberately absent.
    env.update({
        "HOME": os.environ.get("QA_HOME", ""),
        "PATH": f"{os.environ.get('QA_VENV', '')}/bin:/usr/bin:/bin",
        "TERM": "dumb",
        "LANG": "C.UTF-8",
    })
    return env


def read_runlog() -> str:
    p = Path(os.environ["QA_RUNLOG"])
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def preflight(catalog: dict, evidence_dir: Path) -> None:
    problems: list[str] = []
    if catalog.get("schema_version") != 1:
        problems.append("schema_version != 1")
    if catalog.get("run_status") != "NOT_EXECUTED":
        problems.append("run_status != NOT_EXECUTED (catalog must land unexecuted)")
    entries = list(catalog.get("paths", [])) + list(catalog.get("negative_cases", []))
    if not entries:
        problems.append("no executable entries")
    seen_ids: set[str] = set()
    for e in entries:
        eid = e.get("id", "<no-id>")
        if eid in seen_ids:
            problems.append(f"{eid}: duplicate id")
        seen_ids.add(eid)
        for k in REQUIRED_KEYS:
            if not e.get(k):
                problems.append(f"{eid}: missing {k}")
        for s in e.get("sources", []) or []:
            if not (s.get("file") and s.get("line") and s.get("quote")):
                problems.append(f"{eid}: source citation missing file/line/quote")
        cmd = e.get("command", "")
        if cmd:
            chk = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
            if chk.returncode != 0:
                problems.append(f"{eid}: bash -n failed: {chk.stderr.strip()[:200]}")
    if problems:
        print("PREFLIGHT REJECTED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print(f"preflight ok: {len(entries)} entries, all commands pass bash -n")
    (evidence_dir / "preflight.txt").write_text(
        "\n".join(f"{e.get('id')}: bash -n ok" for e in entries) + "\n", encoding="utf-8")


def run_entry(e: dict, env: dict, evidence_dir: Path) -> tuple[str, list[str]]:
    eid = e["id"]
    cmd = e["command"]
    before = read_runlog()
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd], cwd=os.environ["QA_CHECKOUT"], env=env,
            capture_output=True, text=True, timeout=e.get("timeout_s", BASE_TIMEOUT_S),
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        def _s(v: object) -> str:
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace")
            return v or "" if isinstance(v, str) else ""
        rc, out, err = 124, _s(exc.stdout), "TIMEOUT"
    elapsed = time.monotonic() - t0
    after = read_runlog()
    delta = after[len(before):] if after.startswith(before) else after

    fails: list[str] = []
    if "expect_exit" in e and rc != e["expect_exit"]:
        fails.append(f"exit {rc} != {e['expect_exit']}")
    for s in e.get("expect_stdout") or []:
        if s not in out:
            fails.append(f"stdout missing {s[:90]!r}")
    for s in e.get("expect_stderr") or []:
        if s not in err:
            fails.append(f"stderr missing {s[:90]!r}")
    for s in e.get("expect_log_delta") or []:
        if s not in delta:
            fails.append(f"log-delta missing {s[:90]!r}")
    if e.get("expect_no_log_delta") and delta.strip():
        fails.append(f"runner.log grew by {len(delta)} bytes (expected silence)")
    if e.get("expect_stdout_empty") and out:
        fails.append(f"stdout not empty: {out[:90]!r}")
    if e.get("expect_stderr_empty") and err:
        fails.append(f"stderr not empty: {err[:90]!r}")

    (evidence_dir / f"{eid}.out").write_text(out, encoding="utf-8")
    (evidence_dir / f"{eid}.err").write_text(err, encoding="utf-8")
    (evidence_dir / f"{eid}.rc").write_text(f"{rc}\n", encoding="utf-8")
    (evidence_dir / f"{eid}.json").write_text(json.dumps({
        "id": eid, "command": cmd, "exit": rc, "elapsed_s": round(elapsed, 2),
        "log_delta_bytes": len(delta), "failures": fails,
    }, indent=2), encoding="utf-8")

    status = "PASS" if not fails else "FAIL"
    return status, fails


def main() -> int:
    catalog_path, work = sys.argv[1], sys.argv[2]
    catalog = yaml.safe_load(Path(catalog_path).read_text(encoding="utf-8"))
    evidence_dir = Path(work) / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    preflight(catalog, evidence_dir)
    env = qa_env()

    summary_lines: list[str] = []
    passed = failed = 0
    for section in ("paths", "negative_cases"):
        for e in catalog.get(section, []) or []:
            if not e.get("required", False):
                print(f"SKIP  {e['id']} (required: false)")
                continue
            status, fails = run_entry(e, env, evidence_dir)
            line = f"{status}  {e['id']}"
            if fails:
                line += "  :: " + "; ".join(fails)
            print(line)
            summary_lines.append(line)
            if status == "PASS":
                passed += 1
            else:
                failed += 1

    for e in catalog.get("restricted_paths", []) or []:
        if e.get("status") == "fixture-required":
            line = f"BLOCKED  {e['id']}  fixture-required: {e.get('fixture_ask', '<unspecified>')}"
            print(line)
            summary_lines.append(line)

    print(f"\nrequired paths: {passed} pass / {failed} fail")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (evidence_dir / "catalog-run-summary.txt").write_text(
        f"fl4write qa catalog run {stamp}\nmerge_sha: {os.environ.get('MERGE_SHA', '?')}\n"
        + "\n".join(summary_lines)
        + f"\nrequired paths: {passed} pass / {failed} fail\n", encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
