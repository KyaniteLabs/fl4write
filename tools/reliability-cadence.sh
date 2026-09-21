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
RUNS=5; [ "${1:-}" = "--quick" ] && RUNS=3
OUT_DIR="$OUT_BASE/run-$STAMP"
mkdir -p "$OUT_DIR"

cd "$REPO_HOME"
python3 -m fl4write.reliability \
  --config fl4write.fl4write.yaml \
  --endpoint "${FL4WRITE_REL_ENDPOINT:-http://nucbox:8908/v1/chat/completions}" \
  --model "${FL4WRITE_REL_MODEL:-Qwen3.8-27B}" \
  --key-env "" --temperature 0.0 --max-tokens 4000 \
  --runs "$RUNS" \
  --min-determinism 0.93 --min-recall 1.0 --max-actionable-fp 0.4 \
  --out "$OUT_DIR" > "$OUT_DIR/run.log" 2>&1

python3 - "$OUT_DIR/reliability-report.json" "$OUT_BASE/trend.jsonl" <<'PY'
import json, sys, time
rep = json.load(open(sys.argv[1]))
det = rep["determinism"]; rec = rep["recall"]; fp = rep["false_positives"]
rc = rec.get("per_class", {})
tot = sum(v["cases"] for v in rc.values())
caught = sum(v["cases"] for v in rc.values() if v["caught_any"])
caught_a = sum(v["cases"] for v in rc.values() if v["caught_actionable"])
tbl = rep["calibration"].get("table", {})
tp = sum(v["tp"] for v in tbl.values()); fpn = sum(v["fp"] for v in tbl.values())
row = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "route": f'{rep["route"]["model"]}@{rep["route"]["endpoint"]}',
    "determinism": round(det["mean_agreement"], 4),
    "det_min": det["min_agreement"],
    "recall_any": f"{caught}/{tot}",
    "recall_actionable": f"{caught_a}/{tot}",
    "actionable_fp": fp["actionable_fp_rate"],
    "noise_fp": fp["noise_fp_rate"],
    "severity_precision": round(tp / (tp + fpn), 4) if tp + fpn else None,
    "report": sys.argv[1],
}
with open(sys.argv[2], "a") as f:
    f.write(json.dumps(row) + "\n")
print(json.dumps(row))
PY
