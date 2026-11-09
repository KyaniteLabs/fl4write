# FL4WRITE

A self-hosted multi-forge review bot: collect/deduplicate PRs, scrub untrusted text, ask a configured model for findings, ground them against rule/severity/diff evidence, and maintain a persistent review comment. Optional gated lanes verify changed tests, propose own-PR fixes and handle broader review/triage modes.

## What exists

- GitHub/Forgejo adapters, per-repository rules/configs, model routes, persistent comments/deltas, head-SHA review state and atomic checkpoints.
- Open-PR/post-merge/retro/CI/full-tree review paths, optional issue triage, test verification and fix executor. Actual fleet counts/enablement in README are dated deployment claims, not live state verified here.
- Scrubbing/grounding, fork/dependency constraints, own-PR authorship/CI checks and Docker test isolation. Repository fixtures/capability guidance are engineering assets, not independent evidence the bot catches every defect.

CLI is not a safe dry-run default: `shadow` is a config field; absent means live, and `--live` forces writes. Shadow still may call models/use credentials. The README names BEHAVIOR.md, but that file is absent in this checkout; do not pretend its contract was read.

## Docs proof and limits

Read actual CLI/engine/analyzer/scrub/state/executor source. Pure hostile-text scrub/idempotence/assert-clean probes passed; 33 Python files parsed. No config/credential load, model route/fallback, forge query/comment, fix/test executor, service, live fleet or user-side review acceptance was exercised. These checks are not security sign-off or approval to activate/automerge anything. See [ARCHITECTURE.md](ARCHITECTURE.md) and [README.md](README.md).
