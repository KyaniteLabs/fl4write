#!/usr/bin/env bash
# K-row CODE-NOW: scheduled recall/determinism measurement (weekly + on demand).
# Runs the frozen reliability harness through the local Champion route, appends
# one trend row to ~/.fl4write/reliability/trend.jsonl (OUTSIDE the checkout —
# the runner's dirty-tree guard must never see trend data; the 38-hour runner
# death taught that law), and RED-exits when a regression gate trips so cron
# surfaces it instead of swallowing it.
#
# Usage: tools/reliability-cadence.sh [--quick]   (--quick: runs=3 spot check)
set -euo pipefail

REPO_HOME="${FL4WRITE_HOME:-$HOME/workspaces/fl4write}"
OUT_BASE="$HOME/.fl4write/reliability"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUNS=5; MIN_DET=0.93; [ "${1:-}" = "--quick" ] && RUNS=3 && MIN_DET=0.80
OUT_DIR="$OUT_BASE/run-$STAMP"
mkdir -p "$OUT_DIR"

cd "$REPO_HOME"
# Cycle-3: --quick runs only 3 repeats, where one divergent run already
# scores 0.667 — the full 0.93 bar false-reds on noise there. The quick
# lane keeps a looser tripwire; full runs keep the baseline gate.
python3 -m fl4write.reliability \
  --config fl4write.fl4write.yaml \
  --endpoint "${FL4WRITE_REL_ENDPOINT:-http://gpu-host:8908/v1/chat/completions}" \
  --model "${FL4WRITE_REL_MODEL:-Qwen3.8-27B}" \
  --key-env "" --temperature 0.0 --max-tokens 4000 \
  --runs "$RUNS" \
  --min-determinism "$MIN_DET" --min-recall 1.0 --max-actionable-fp 0.4 \
  --out "$OUT_DIR" > "$OUT_DIR/run.log" 2>&1

python3 - "$OUT_DIR/reliability-report.json" "$OUT_BASE/trend.jsonl" <<'PY'
import fcntl, json, sys
from fl4write.reliability import trend_row
report_path, trend_path = sys.argv[1], sys.argv[2]
rep = json.load(open(report_path))
row = trend_row(rep, report_path)
# Cycle-3: exclusive lock so overlapping cron runs or kill -9 mid-append
# cannot interleave/corrupt the trend log.
with open(trend_path, "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    f.write(json.dumps(row) + "\n")
    f.flush()
print(json.dumps(row))
PY
