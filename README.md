# FL4WRITE (`fl4write`)

**Capability-grounded review:** findings ground in the configured rule set (the org's own CheckYourself capabilities merge into every config); the ``general`` rule remains the bounded fallback for defects no capability names (F2-004 doc truth). Readiness score per repo.

**Day-1 state (2026-09-02, HISTORICAL — current numbers in Status below):** 54 repos across GitHub + Forgejo (own bot identity on both); five review modes live (open-PR, post-merge, retro, ci_watch, omnisweep full-tree); fix lane working (first real fix: Epoch #199); verify-tests runs the diff's own tests deterministically; plus-ultra telemetry stream + Quality Loop (issue #5). 224 tests at the time.

*Official stylization (CEO, 2026-09-01): **FL4WRITE**. Identity surfaces unchanged by law: repo/package `fl4write`, env vars `CODESITTER_*`, markers `fl4write:v1:`, bot logins.*

Self-hosted, multi-forge code review bot — the org's CodeRabbit replacement.
Chartered by the CEO 2026-08-31; built under wayfinder map
[KyaniteLabs/.github #63](https://github.com/KyaniteLabs/.github/issues/63)
(ralplan consensus-approved; behavior contract = BEHAVIOR.md from ticket #64;
best-practices register = ticket #70).

## What it does

One poll-invariant cycle per run: collect open PRs (GitHub primary + Forgejo
mirror, SHA-deduped) → scrub all untrusted text → LLM-brained findings
(champion route + fallback) → ground every finding (rule in vocab, severity in
vocab, path in diff — ungrounded findings are dropped+logged, never posted) →
render the persistent comment (one per PR, edited in place, 🆕 deltas) →
hand actionable findings to the fix lane. **The fix lane is live**: findings
become own-PR fixes under hard rails (fork = comment-only; bot-authored
dependency PRs = read-only; merge re-verifies authorship+CI at the call site;
sandboxed test evidence gates every push). The v0.1 plan shipped the lane as a
tested library first; engine wiring landed with the first live deployment.

## Safety architecture (the parts that are laws, not features)

- **State correctness never depends on timestamps**: re-review fires on
  `head_sha != last_reviewed_sha`, so missed events self-heal next cycle.
  Atomic writes (kill-mid-write safe), cycle lock (no double-post), corrupt
  state = bounded reconcile.
- **The model is never on a direct path to a write.** Its output is data
  until code validates it (grounding + scrub); actions are code-gated.
- **Scrub gate**: control/bidi/zero-width chars, data: URLs, base64 images,
  remote src=, hidden HTML, and our own comment markers are neutralized on
  everything crossing the trust boundary (PR bodies, commit text, findings).
- **Fix-lane rails asserted in code at call sites**: fork = comment-only;
  bot-authored dependency PRs = read-only; merge re-verifies authorship+CI
  regardless of config.
- **Tone is a renderer-only preset** (`quiet|balanced|assertive|roast`); the
  analyzer is tone-blind; forks are hard-overridden to `balanced`; Critical
  findings always render urgency.

## Adding a repo

Drop a `.fl4write.yaml` in the repo (see the tracked reference instance
`kinocut.fj.fl4write.yaml` for the canonical all-Forgejo form, or any
`*.fl4write.yaml` central config in this repo's root): forge bindings
(exactly one `primary`, others `mirror`), model routes, repo law as `review:`
rules, severity vocab, tone, fix-lane autonomy, known-env failures. Config
validates fail-loud at startup.

## Deployment

v1 trigger = cron (the engine's entry point takes a normalized trigger —
cron is an adapter, not the architecture; the flip list for an event adapter:
hosted brain / acceptable standing listener / existing self-hosted runner).
Run in shadow mode (`shadow: true`) first — findings log, nothing posts.
Cutover checklist: 48h shadow diff reviewed → host decision recorded (always-on
host OR explicitly accepted sleep gap) → PAT scopes enumerated + rotation set.

## Status — v0.4+ production (fleet of 129 central configs; see PILOT.md)

All five review modes live (open-PR, post-merge, retro audit, ci_watch,
omnisweep) plus gatekeeper, verify-tests and acceptance metrics. The issues
triage lane is available per-repo via `issues_enabled` + `--issues` (opt-in;
no fleet repo has enabled it yet — luna F3-004 doc truth).
**Usage:** `python3 -m fl4write.cli <config> [--live] [--fixes] [--issues] [--omni]`
(mode flag typos are refused — unknown flags exit 2).

Current fleet state (2026-09-05; recovered [audit ledger](docs/pm-recovery/ROUND-LEDGER.md)): **129 central configs**
across GitHub + Forgejo in hourly cycles; the tier scheduler selects due repos
(`run-cycle.sh`, hourly crontab, single-host law — LEARNINGS #17).

The merged suite (exhaustive-loop candidate + main) is **1253 passing + 4 skipped** — count stamped by the doc-truth verifier run on a clean tree; hand-editing this figure is a false receipt (REVIEW LAWS 2026-09-19).

The unmerged repair candidate at `81a97d5` passed **1039 live tests with zero
skips**, Ruff and canonical Forgejo CI. Its default suite passed 1036 tests
with three paid-model skips. One real automatic repair→PR→CI→merge→refresh
trial passed on the integration branch. Three fresh clean whole-project
rounds, full recon/quorum dogfood, security review, CEO quality adjudication
and final main/mirror/runner delivery remain open. These candidate results
do not describe a production rollout. See the [current audit ledger](docs/pm-recovery/ROUND-LEDGER.md)
and [round27 evidence](docs/pm-recovery/ROUND27-REPAIR-CHECKPOINT.md).
The round-13 desk pass reported 605 passing tests; that historical count is
superseded by the linked recovery checkpoints. The round27 repair candidate
achieved 1036 passing + 3 skipped by default, or 1039 passing + 0 skipped live;
its exact validation status is recorded in the audit ledger.
The round29 metrics repair targets 1050 passing + 3 skipped by default,
or 1053 passing + 0 skipped live. Live validation is pending provider access.
The round30 renamed-path repair achieved 1062 passing + 3 skipped by default
and targets 1065 passing + 0 skipped live. Its validation is recorded in the ledger;
the live gate remains blocked by the configured provider's HTTP 402 response.
The round31 credential regressions achieved 1066 passing + 3 skipped by default
and target 1069 passing + 0 skipped live; live validation remains pending.
The isolated evaluation-route regression repair targets 1074 passing + 3 skipped
by default and 1077 passing + 0 skipped live; current-head validation is pending.
The recon line-location, request-size, and parsing regressions target 1085 passing + 3 skipped
by default and 1088 passing + 0 skipped live; current-head validation is pending.
The optional source-bound desk-disposition path targets 1113 passing + 3 skipped
by default and 1116 passing + 0 skipped live; current-head validation is pending.
The configured PAT identity and pending-state authority regressions target 1119
passing + 3 skipped by default and 1122 passing + 0 skipped live; current-head validation is pending.
The resumable recon, proxy boundary and authenticated refresh changes target 1162 passing + 3 skipped by default
and 1165 passing + 0 skipped live; current-head validation is pending.
The 2026-09-16 integration + adversarial-fix pass measures
**1177 passing + 3 skipped** on the default suite. The live suite
(**1172 passing + 0 skipped**) was measured at 474719a — the same
analyzer/model request path (unchanged since; the later fix pass touched
cycle, sandbox-collection and publication-containment code only). A live
re-run at the final tree was blocked when the shared Champion floor service
went inactive mid-session (documented, not restarted from this desk). Route facts kept separate
(close-doc lesson 2): the CONFIGURED route remains
`deepseek-ai/DeepSeek-V4-Flash-0731` via DeepInfra (`fl4write.fl4write.yaml`);
the 2026-09-16 live run was executed with
`FL4WRITE_EVAL=1 FL4WRITE_EVAL_CONFIG=<champion-eval.yaml>` pointing at the
operator-approved self-hosted Champion Qwen3.8-27B (nucbox floor, 2026-09-06
approval; Ornith selection was DOWN that day). A bare `FL4WRITE_EVAL=1` run
without the override exercises the configured DeepSeek route, not Champion.

For providers that support it, a model route can set `thinking: enabled` or
`thinking: disabled`. Omitting it preserves the provider default. The proxy pins
this setting to the selected route, so isolated callers cannot override it.
Run it with `FL4WRITE_EVAL=1 python3 -m pytest -q`;
it uses `fl4write.fl4write.yaml` (override with `FL4WRITE_EVAL_CONFIG`) and
runtime model credentials. The default suite skips those three paid model
cases. Round 14 remains open; no exhaustive certification is
claimed. CI on every push. Quality loop on
issue #5; desk charter + incident history on issue #3 and map #63.

Requested behavior (CEO 2026-09-04, implemented in this draft but not certified or deployed): the **exhaustive bug-resolution
loop** — repeated fresh-eyes recon→fix→refresh→note rounds (new context every round) until
THREE CONSECUTIVE 100%-green loops with zero regressions, then the repo is certified
exhaustively flushed. Spec: [EXHAUSTIVE-BUG-RESOLUTION.md](EXHAUSTIVE-BUG-RESOLUTION.md) ·
tracker: issue #13. Feature tranche; delegate-audited before fleet enablement.
The draft's isolated test runtime and its validation limits are documented in
[the runtime guide](tools/exhaustive-runtime/README.md).

Honest status line (post 2026-09-03 adjudication): the bot is NOT yet
declared fit-for-use at scale — the desk's own adjudicated sample found the
majority of posted Criticals were false positives (see #5 and LEARNINGS
#38-42). Severity-integrity gates, execution-evidence test gating, adapter
containment and the scrub/injection surface were rebuilt under the readiness
gauntlet; phase-2 citation grounding (findings must quote the reviewed
code's bytes) is the remaining gate before re-quoting Q1.
