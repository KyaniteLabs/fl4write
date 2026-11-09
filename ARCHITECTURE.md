# FL4WRITE architecture

```text
cron/CLI + repo config -> forge collection/SHA dedup -> diff + scrub
 -> configured LLM/fallback -> grounding -> renderer -> persistent comment/state
accepted actionable finding -> gated own-PR executor -> isolated tests -> CI/authorship gates
```

[cli.py](fl4write/cli.py) loads config/runtime credentials and wires one cycle. [engine.py](fl4write/engine.py) owns cycle state, required diff fetch, per-PR containment and checkpoints. [analyzer.py](fl4write/analyzer.py) validates model envelopes and grounds rule IDs/severity/paths; [scrub.py](fl4write/scrub.py) neutralizes selected hostile controls, hidden markup, image beacons and marker spoofing. These defenses reduce risk, not prove prompt injection impossible.

[state.py](fl4write/state.py) uses head-SHA divergence rather than timestamps for review correctness; actual cycle ownership uses kernel flock with a persistent diagnostic file, despite older module-header age-break prose. Atomic state persistence and missing-diff/model-unavailable outcomes prevent a failed fetch becoming a clean review. [executor.py](fl4write/executor.py) handles PR-head worktrees, patch/model output, containment, tests and own-fix PR flow; review and fix lanes are separate authority boundaries.

## Risks / failure modes

- Bare CLI/config execution can post publicly and invoke paid models. Missing `shadow` is live; shadow does not mean offline/no-spend. Never run an actual central config just to verify docs, and never silently use a paid fallback.
- PR content and test code are adversarial. Scrubbing/grounding do not establish semantic correctness; truncated diff or dropped findings must be disclosed. Missing diff/model failure must remain unreviewed, not “clean”.
- Fix worktrees/patches/test containers/merge credentials have larger blast radius. Default reviewed Docker isolation is different from the explicit host opt-out; enabling host execution breaks the claimed sandbox boundary. This docs PR does not grant merge authority.
- State corruption, concurrent cycles and forged/edited comments can duplicate or lose decisions. Kernel ownership and exact head/CI checks matter more than stale fleet/test counts in prose.

## Docs verification

Purpose and actual critical boundaries read; missing BEHAVIOR.md recorded. Pure scrub/idempotence/assert-clean probe and 33 Python ASTs passed. No model/config/credential reads, forge mutation, tests of untrusted code, fix lane, host service or live status check. No user-side/security acceptance claimed. Runtime unchanged; revert docs for rollback.
