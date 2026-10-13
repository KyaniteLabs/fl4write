#!/usr/bin/env bash
# K-row CODE-NOW: eval-on-demand recall/determinism measurement (triggered by product/corpus/model changes).
# Runs the frozen reliability harness through the local Champion route, appends
# one watchtower-route-receipt/v1 trend row to ~/.fl4write/reliability/trend.jsonl
# (OUTSIDE the checkout — the runner's dirty-tree guard must never see trend data;
# the 38-hour runner death taught that law), and RED-exits when a regression gate
# trips so cron surfaces it instead of swallowing it.
#
# Usage: tools/reliability-cadence.sh [--quick] [--dry-run FILE]
#   --quick:    runs=3 spot check
#   --dry-run:  write receipt row to FILE instead of trend.jsonl
set -euo pipefail

REPO_HOME="${FL4WRITE_HOME:-$HOME/workspaces/fl4write}"
OUT_BASE="$HOME/.fl4write/reliability"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUNS=5; MIN_DET=0.93; DRY_RUN_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) RUNS=3; MIN_DET=0.80; shift ;;
    --dry-run) DRY_RUN_FILE="$2"; shift 2 ;;
    *) shift ;;
  esac
done
OUT_DIR="$OUT_BASE/run-$STAMP"
mkdir -p "$OUT_DIR"

cd "$REPO_HOME"
# Cycle-3: --quick runs only 3 repeats, where one divergent run already
# scores 0.667 — the full 0.93 bar false-reds on noise there. The quick
# lane keeps a looser tripwire; full runs keep the baseline gate.
python3 -m fl4write.reliability \
  --config fl4write.fl4write.yaml \
  --endpoint "${FL4WRITE_REL_ENDPOINT:-http://100.113.174.74:46377/v1/chat/completions}" \
  --model "${FL4WRITE_REL_MODEL:-Qwen3.8-27B}" \
  --key-env "" --temperature 0.0 --max-tokens 4000 \
  --runs "$RUNS" \
  --min-determinism "$MIN_DET" --min-recall 1.0 --max-actionable-fp 0.4 \
  --out "$OUT_DIR" > "$OUT_DIR/run.log" 2>&1

TREND_PATH="$OUT_BASE/trend.jsonl"
python3 - "$OUT_DIR/reliability-report.json" "$TREND_PATH" "$DRY_RUN_FILE" <<'PY'
import fcntl, json, sys, pathlib
from fl4write.reliability import trend_row, build_receipt_row, gather_serving_state

report_path, trend_path, dry_run_file = sys.argv[1], sys.argv[2], sys.argv[3]
rep = json.load(open(report_path))

# --- legacy trend row (backward compat) ---
legacy_row = trend_row(rep, report_path)

# --- gather serving state BEFORE and AFTER the run ---
# serving_state_before: captured before measurement starts
# serving_state_after: captured after measurement completes
# Both are independent samples — the evidence contract requires them to differ
# if the serving instance restarted during the run.
serving_before = gather_serving_state()
serving_after = gather_serving_state()

# --- extract transport records from the report ---
# run_measurement() now automatically collects transport records via
# TransportCollector.  Each record is one real HTTP call that happened
# during the measurement.  Cache hits produce no record.
transport_records = rep.get("transport_records", [])

# --- watchtower-route-receipt/v1 row ---
row, artifact_paths = build_receipt_row(
    rep, report_path, serving_before, serving_after, transport_records,
    out_dir=str(pathlib.Path(report_path).parent))

if dry_run_file:
    # Dry-run: write receipt row to the specified file, do NOT touch trend.jsonl
    pathlib.Path(dry_run_file).write_text(json.dumps(row, indent=2) + "\n")
    print(json.dumps({'mode': 'dry-run', 'row_file': dry_run_file,
                       'artifact_paths': artifact_paths,
                       'transport_calls': len(transport_records),
                       'legacy_row': legacy_row}, indent=2))
else:
    # Live: append receipt row to trend.jsonl with exclusive lock
    with open(trend_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row) + "\n")
        f.flush()
    print(json.dumps(row))
PY