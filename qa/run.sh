#!/usr/bin/env bash
# FL4WRITE QA runner — executes qa/operator-paths.yaml end-to-end.
#
# Usage:            bash qa/run.sh
# Override workdir: FL4WRITE_QA_WORK=/path bash qa/run.sh
# Pin a SHA:        FL4WRITE_QA_SHA=<sha> bash qa/run.sh
#                   (default: the HEAD of the checkout this script lives in —
#                   run it from the Security-merged clone)
# Retain evidence:  everything lands in $QA_WORK/evidence/ ; set
#                   FL4WRITE_QA_EVIDENCE to also copy it somewhere durable.
#
# Isolation: the runner clones THIS repository into a disposable QA_WORK,
# pins it to the merge SHA, installs it into a fresh venv (network to PyPI
# needed, same as CI), builds an isolated HOME and the fixture shims, and
# executes every required catalog entry under a constructed env (env -i
# semantics): no host credentials, no host HOME, FL4WRITE_FORGEJO_API_BASE
# unset in the base environment.
#
# Exit codes: 0 = every required path passed (fixture-gated paths are
# reported as BLOCKED, never as PASS); 1 = at least one required path FAILED
# or preflight rejected the catalog.
set -euo pipefail

QA_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CATALOG="$QA_SRC/qa/operator-paths.yaml"
MERGE_SHA="${FL4WRITE_QA_SHA:-$(git -C "$QA_SRC" rev-parse HEAD)}"
QA_WORK="${FL4WRITE_QA_WORK:-$(mktemp -d "${TMPDIR:-/tmp}/fl4write-qa.XXXXXX")}"
QA_HOME="$QA_WORK/home"
QA_CHECKOUT="$QA_WORK/checkout"
QA_VENV="$QA_WORK/venv"
QA_SANDBOX="$QA_HOME/workspaces/fl4write"
QA_LOCK="$QA_HOME/.fl4write/runner.lock"
QA_RUNLOG="$QA_SANDBOX/runner.log"
QA_FIXTURE="$QA_WORK/fixtures/qa-loopback.fl4write.yaml"
QA_SHIM_GH="$QA_WORK/shim"
QA_SHIM_NOFLOCK="$QA_WORK/shim-noflock"

log() { echo "[qa-run] $*"; }

[ -f "$CATALOG" ] || { echo "catalog missing: $CATALOG" >&2; exit 1; }
log "source: $QA_SRC @ $MERGE_SHA"
log "work:   $QA_WORK"

# --- setup: isolate + clone_and_pin + install + sandbox + fixtures ----------
mkdir -p "$QA_HOME/.config" "$QA_HOME/.fl4write" "$QA_WORK/fixtures" \
         "$QA_WORK/nonrepo" "$QA_WORK/evidence" "$QA_SHIM_NOFLOCK"

git clone -q --no-local "$QA_SRC" "$QA_CHECKOUT"
git -C "$QA_CHECKOUT" checkout -q --detach "$MERGE_SHA"
[ "$(git -C "$QA_CHECKOUT" rev-parse HEAD)" = "$MERGE_SHA" ] || {
  echo "SHA pin failed" >&2; exit 1; }
log "checkout pinned at $MERGE_SHA"

if command -v uv >/dev/null 2>&1; then
  export UV_CACHE_DIR="${UV_CACHE_DIR:-$QA_WORK/uv-cache}"
  uv venv -q --python "$(command -v python3)" "$QA_VENV" >/dev/null
  uv pip install -q --python "$QA_VENV/bin/python" -e "$QA_CHECKOUT" >/dev/null
else
  python3 -m venv "$QA_VENV"
  "$QA_VENV/bin/pip" install -q -e "$QA_CHECKOUT"
fi
"$QA_VENV/bin/python" -c 'import fl4write, pydantic, yaml, jwt' && log "venv install ok"

git clone -q --no-local "$QA_CHECKOUT" "$QA_SANDBOX"
git -C "$QA_SANDBOX" checkout -q --detach "$MERGE_SHA"
rm -f "$QA_RUNLOG"

cp "$QA_SRC/qa/fixtures/qa-loopback.fl4write.yaml" "$QA_FIXTURE"
mkdir -p "$QA_SHIM_GH"
cp "$QA_SRC/qa/fixtures/shim/gh" "$QA_SHIM_GH/gh"
chmod +x "$QA_SHIM_GH/gh"
# shim PATH WITHOUT flock: every binary the pre-flock path needs, except flock
for b in env bash sh mkdir date grep head cut tr cat; do
  ln -sf "/usr/bin/$b" "$QA_SHIM_NOFLOCK/$b"
done
if [ -e "$QA_SHIM_NOFLOCK/flock" ]; then echo "shim-noflock contains flock" >&2; exit 1; fi
log "fixtures ready (dead-loopback config, gh stub, no-flock shim)"

# --- execute the catalog -----------------------------------------------------
export QA_SRC QA_WORK QA_HOME QA_CHECKOUT QA_VENV QA_SANDBOX QA_LOCK QA_RUNLOG \
       QA_FIXTURE QA_SHIM_GH QA_SHIM_NOFLOCK MERGE_SHA
"$QA_VENV/bin/python" "$QA_SRC/qa/run_paths.py" "$CATALOG" "$QA_WORK"
rc=$?

if [ -n "${FL4WRITE_QA_EVIDENCE:-}" ]; then
  mkdir -p "$FL4WRITE_QA_EVIDENCE"
  cp -r "$QA_WORK/evidence/." "$FL4WRITE_QA_EVIDENCE/" && log "evidence copied to $FL4WRITE_QA_EVIDENCE"
fi
log "evidence: $QA_WORK/evidence"
exit $rc
