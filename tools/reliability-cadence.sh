#!/usr/bin/env bash
# K-row CODE-NOW: eval-on-demand recall/determinism measurement (triggered by product/corpus/model changes).
# Runs the frozen reliability harness through the local Champion route, appends
# one watchtower-route-receipt/v1 trend row to ~/.fl4write/reliability/trend.jsonl
# (OUTSIDE the checkout — the runner's dirty-tree guard must never see trend data;
# the 38-hour runner death taught that law), and RED-exits when a regression gate
# trips so cron surfaces it instead of swallowing it.
#
# Usage: tools/reliability-cadence.sh [--quick] [--dry-run FILE] [--serving-host HOST]
#   --quick:          runs=3 spot check
#   --dry-run:        write receipt row to FILE instead of trend.jsonl
#   --serving-host:   SSH host (alias) for remote serving state collection.
#                     PINNED SEMANTICS (2026-10-15): when the flag is given
#                     it is authoritative — even an empty value, which
#                     forces LOCAL collection.  When omitted, the target is
#                     derived from the endpoint (see derivation below).
set -euo pipefail

REPO_HOME="${FL4WRITE_HOME:-$HOME/workspaces/fl4write}"
OUT_BASE="$HOME/.fl4write/reliability"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUNS=5; MIN_DET=0.93; DRY_RUN_FILE=""; SERVING_HOST=""; SERVING_HOST_GIVEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) RUNS=3; MIN_DET=0.80; shift ;;
    --dry-run) DRY_RUN_FILE="$2"; shift 2 ;;
    --serving-host) SERVING_HOST="$2"; SERVING_HOST_GIVEN=1; shift 2 ;;
    *) shift ;;
  esac
done
OUT_DIR="$OUT_BASE/run-$STAMP"
mkdir -p "$OUT_DIR"

# --- endpoint: the CHAMPION route (CHAMPION-ROUTE-HASH route=127.0.0.1:46377
# on the serving host).  The llama-server binds 127.0.0.1, so the tailnet-IP
# form (http://100.113.174.74:46377/...) is REFUSED from every host including
# the serving host itself (verified 2026-10-15: curl -> 000 everywhere;
# loopback answers).  The measurement therefore runs ON the serving host
# against loopback.  NOTE (route story, decision pending): run-cycle.sh
# exports FL4WRITE_MODEL_ENDPOINT=http://127.0.0.1:46399 (CRACK-Ornith);
# this cadence deliberately measures the CHAMPION route, not that one.
ENDPOINT="${FL4WRITE_REL_ENDPOINT:-http://127.0.0.1:46377/v1/chat/completions}"

# --- derive serving host from endpoint if not explicitly given ---
if [ "$SERVING_HOST_GIVEN" = 0 ]; then
  # Extract host:port from URL; strip protocol and path
  _ep_host="${ENDPOINT#http://}"
  _ep_host="${_ep_host#https://}"
  _ep_host="${_ep_host%%/*}"
  _ep_ip="${_ep_host%%:*}"
  # Map endpoint host -> authorized ssh target (LAW 20: bare tailnet IPs are
  # refused by tailscale policy; only ssh-config aliases are authorized).
  # Loopback / this machine -> local collection.  Unmapped hosts fail with an
  # actionable message instead of a broken ssh attempt later.
  case "$_ep_ip" in
    127.0.0.1|localhost|::1|"$(hostname)"|"$(hostname -s 2>/dev/null || true)")
      SERVING_HOST="" ;;
    100.113.174.74|nucbox)
      # Endpoint is this machine's own address -> collect locally on the
      # serving host; otherwise reach it via the authorized alias.
      if hostname -I 2>/dev/null | tr ' ' '\n' | grep -qx "$_ep_ip"; then
        SERVING_HOST=""
      else
        SERVING_HOST="${FL4WRITE_SERVING_SSH:-nucbox}"
      fi ;;
    *)
      echo "PREFLIGHT FAILED: cannot derive an authorized ssh target for endpoint host '$_ep_ip'." >&2
      echo "Pass --serving-host HOST (authorized ssh alias) or --serving-host '' for local collection." >&2
      exit 1 ;;
  esac
fi

# --- PREFLIGHT (all before any state capture; fail fast with the cause named) ---
# 1. Config env: fl4write.fl4write.yaml references ${FL4WRITE_MODEL_ENDPOINT}
#    and the bare weekly crontab exports nothing (reproduced ValueError:
#    "config references unset environment variable").  Default it to the
#    measured endpoint (champion route), then verify every ${VAR} referenced
#    by the config is set — naming each missing one.
export FL4WRITE_MODEL_ENDPOINT="${FL4WRITE_MODEL_ENDPOINT:-$ENDPOINT}"
_MISSING=""
for _var in $(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*\}' "$REPO_HOME/fl4write.fl4write.yaml" 2>/dev/null | sort -u | tr -d '${}' || true); do
  if [ -z "${!_var:-}" ]; then _MISSING="$_MISSING $_var"; fi
done
if [ -n "$_MISSING" ]; then
  echo "PREFLIGHT FAILED: config references unset environment variable(s):$_MISSING" >&2
  echo "Export them before running (see $REPO_HOME/fl4write.fl4write.yaml)." >&2
  exit 1
fi

# 2. Endpoint reachability (anything but a curl transport failure counts —
#    400/401/500 mean the server is listening; 000 means refused/absent).
_ep_code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST "$ENDPOINT" \
  -H 'Content-Type: application/json' -d '{}' || true)"
if [ -z "$_ep_code" ] || [ "$_ep_code" = "000" ]; then
  echo "PREFLIGHT FAILED: model endpoint not reachable: $ENDPOINT (curl http_code '$_ep_code')." >&2
  echo "The champion server binds 127.0.0.1 — run this cadence on the serving host, or set FL4WRITE_REL_ENDPOINT." >&2
  exit 1
fi

# 3. Serving-state collection target: if remote, the ssh alias must be
#    authorized and reachable before any state capture.
if [ -n "$SERVING_HOST" ]; then
  if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$SERVING_HOST" true 2>/dev/null; then
    echo "PREFLIGHT FAILED: ssh target '$SERVING_HOST' unreachable or unauthorized." >&2
    echo "Use an authorized ssh alias (tailscale policy refuses bare tailnet IPs), or --serving-host '' for local collection." >&2
    exit 1
  fi
fi

cd "$REPO_HOME"

# --- CAPTURE SERVING STATE BEFORE measurement ---
# This MUST happen before the measurement run starts.  The receipt
# contract requires a true BEFORE sample, not two post-run samples.
echo "Capturing serving state BEFORE measurement (host: ${SERVING_HOST:-local})..."
BEFORE_STATE_FILE="$OUT_DIR/serving-state-before.json"
python3 -c "
import json, sys
sys.path.insert(0, '.')
from fl4write.reliability import gather_serving_state
host = sys.argv[1] if sys.argv[1] else None
state = gather_serving_state(host)
json.dump(state, open(sys.argv[2], 'w'), indent=2)
# Fail closed: if state is unavailable, exit non-zero
if state.get('unavailable'):
    print(f'FAIL CLOSED: before state unavailable: {state.get(\"reason\")}', file=sys.stderr)
    sys.exit(1)
print(json.dumps({'status': 'ok', 'boot_id': state['runtime']['boot_id'],
                   'pid': state['runtime']['pid']}, indent=2))
" "${SERVING_HOST}" "$BEFORE_STATE_FILE"
echo "Before state captured: $(python3 -c "import json; d=json.load(open('$BEFORE_STATE_FILE')); print(d['runtime']['boot_id'], d['runtime']['pid'])")"

# --- RUN MEASUREMENT ---
# Cycle-3: --quick runs only 3 repeats, where one divergent run already
# scores 0.667 — the full 0.93 bar false-reds on noise there. The quick
# lane keeps a looser tripwire; full runs keep the baseline gate.
echo "Running reliability measurement (${RUNS} runs)..."
python3 -m fl4write.reliability \
  --config fl4write.fl4write.yaml \
  --endpoint "$ENDPOINT" \
  --model "${FL4WRITE_REL_MODEL:-Qwen3.8-27B}" \
  --key-env "" --temperature 0.0 --max-tokens 4000 \
  --runs "$RUNS" \
  --min-determinism "$MIN_DET" --min-recall 1.0 --max-actionable-fp 0.4 \
  --out "$OUT_DIR" > "$OUT_DIR/run.log" 2>&1

# --- CAPTURE SERVING STATE AFTER measurement ---
echo "Capturing serving state AFTER measurement (host: ${SERVING_HOST:-local})..."
AFTER_STATE_FILE="$OUT_DIR/serving-state-after.json"
python3 -c "
import json, sys
sys.path.insert(0, '.')
from fl4write.reliability import gather_serving_state
host = sys.argv[1] if sys.argv[1] else None
state = gather_serving_state(host)
json.dump(state, open(sys.argv[2], 'w'), indent=2)
if state.get('unavailable'):
    print(f'FAIL CLOSED: after state unavailable: {state.get(\"reason\")}', file=sys.stderr)
    sys.exit(1)
print(json.dumps({'status': 'ok', 'boot_id': state['runtime']['boot_id'],
                   'pid': state['runtime']['pid']}, indent=2))
" "${SERVING_HOST}" "$AFTER_STATE_FILE"
echo "After state captured: $(python3 -c "import json; d=json.load(open('$AFTER_STATE_FILE')); print(d['runtime']['boot_id'], d['runtime']['pid'])")"

# --- BUILD RECEIPT ROW with proper before/after binding ---
TREND_PATH="$OUT_BASE/trend.jsonl"
python3 - "$OUT_DIR/reliability-report.json" "$TREND_PATH" "$DRY_RUN_FILE" \
    "$BEFORE_STATE_FILE" "$AFTER_STATE_FILE" <<'PY'
import fcntl, json, sys, pathlib
from fl4write.reliability import trend_row, build_receipt_row, check_identity_binding

report_path, trend_path, dry_run_file = sys.argv[1], sys.argv[2], sys.argv[3]
before_state_path, after_state_path = sys.argv[4], sys.argv[5]
rep = json.load(open(report_path))

# --- legacy trend row (backward compat) ---
legacy_row = trend_row(rep, report_path)

# --- load pre-captured serving states (captured before/after measurement) ---
serving_before = json.load(open(before_state_path))
serving_after = json.load(open(after_state_path))

# --- identity binding check: fail closed on changed identity ---
identity = check_identity_binding(serving_before, serving_after)
if not identity['ok']:
    print(f"IDENTITY BINDING FAILED: {identity['reason']}", file=sys.stderr)
    # Write failure receipt and exit non-zero
    failure_row = {
        'schema': 'watchtower-route-receipt/v1',
        'outcome': 'identity_binding_failed',
        'reason': identity['reason'],
        'identity_check': identity,
    }
    failure_path = pathlib.Path(report_path).parent / 'identity-failure.json'
    failure_path.write_text(json.dumps(failure_row, indent=2))
    print(json.dumps(failure_row, indent=2))
    sys.exit(1)

# --- extract transport records from the report ---
# run_measurement() automatically collects transport records via
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
                       'identity_binding': identity,
                       'legacy_row': legacy_row}, indent=2))
else:
    # Live: append receipt row to trend.jsonl with exclusive lock
    with open(trend_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row) + "\n")
        f.flush()
    print(json.dumps(row))
PY