# Round 19 repair checkpoint

Fresh independent functional review of `8a2638a` found two Major defects.
The default suite passed 855 tests with three live-model skips; Ruff and shell
syntax checks passed. The round is not clean: certification remains 0/3.

F19-001: a malformed issue identity was filtered out while the listing still
claimed completeness. An old retry could therefore be deleted as closed.
The separate manual repair retains valid rows for direct-call compatibility,
marks malformed identity listings incomplete, and defers the issue cycle
before any triage or watermark changes. Twelve cases cover six malformed
identity forms through primary and fallback listing, mixed with valid newer
work, followed by recovery using the same state. All twelve fail before repair.

F19-002: archive packing removed tracked executable permission. The manual
repair preserves owner read/execute for executable files and directories,
while keeping ordinary files read-only. A tracked shell-helper regression
raises PermissionError before repair and executes after repair. Parent scoped
tests passed 43 cases; independent recheck passed 45 cases and Ruff.

The real generated F19-001 trial at `d5e26a2` is separate. Its first response
omitted required non-test regression flags and failed validation before tests.
The second passed schema validation and the full 858-test live baseline, but
independent review found the proposed source still accepts string and boolean
identities as complete; all four generated regressions fail. Its recovery
assertions also incorrectly demand a decreasing watermark. The full trial's
terminal result was `error: full suite not green (exit=1)`, with no PR or merge.
The durable ledger used 20 calls and 96000 reserved output tokens of its original
49-call/196000-token cap. No generated success or automated lifecycle
completion is claimed; original generation and test evidence remain intact.

Evidence: `/tmp/fl4write-fresh-round19-report.md`,
`/tmp/fl4write-round19-exec-review.md`,
`/tmp/fl4write-round19-issue-review.md`,
`/tmp/fl4write-round19-exec-red.xml`, and
`/tmp/fl4write-round19-row-red.xml`. The independent issue-repair recheck
reproduced all twelve baseline failures, then passed 45 issue/triage tests
and Ruff with the repair. The generated cache was unchanged. The full manual
default suite passed **868 tests with three paid live-model skips** in86.80s;
full Ruff passed. JUnit SHA256:
`45738fb033f9e837526931f6849a2034e4b01fd15d1fc79f3c82a4b2ec850198`.
Full paid live verification of this combined repair is the next validation gate.

## Original hourly-health gate

Read-only inspection of the sole production runner log verified the completed
2026-09-05 11:00:05–11:00:33 UTC scheduled cycle: 30 repos due, 30 successful,
zero errors. All 30 per-repo reports have zero model_down, mirror_degraded,
gate_fail, fix_fail, ci_red and ci_esc counters. No textual alerts or errors
occur in the cycle. This satisfies the completed zero-alert hourly-cycle
observation; it is not a full-fleet active-workload or sustained-health claim.
Production has not been changed by these candidate repairs.

Remaining delivery gates include fresh clean review rounds, the incomplete
security review, successful automated lifecycle proof, CEO quality-sample
adjudication, and final main/mirror/runner delivery. PR15 remains a draft.
