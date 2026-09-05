# Round 20 repair checkpoint

Fresh review of `46c27ef` reproduced three defects despite 868 passing default
tests and three paid-model skips. Ruff passed. This is another non-clean round;
the clean counter remains 0/3.

F20-001 (Minor): lossy prompt formatting changed unusual file paths while the
gatekeeper compared model decisions with original paths. The repair gives
changed paths collision-free prompt identities and uses those identities for
keep and demotion matching, retaining original Finding objects and separate
safe display names. Five regression cases fail before repair and pass after:
repeated spaces, leading/trailing spaces, a tab and a long path. They also
check identity collisions, same-line rule siblings and selective demotion.
Independent scoped review passed 14 gatekeeper tests. A broader run initially
failed only the README count check; that count is now updated for all repairs.

F20-002 (Major): a clean rescan marked publication successful without updating
an existing issue containing old findings. Both clean completion paths now
refresh that issue, retaining its identity and retrying failed clean updates.
A successful-update marker repairs legacy falsely-published clean state too.
New clean sweeps still create no issue, and shadow mode publishes nothing.
Six real-cycle regressions cover fresh and legacy state, repeated failures,
recovery and no-op behavior. Independent review passed 37 tests and Ruff;
the original reproduction now updates the existing report exactly once.

F20-003 (Major): issue fallback pagination used GitHub's `per_page` even for
Forgejo, whose `limit` parameter otherwise defaulted to 50 and caused an early
false-complete result. The repair uses the adapter parameter, preserving
`per_page` for existing plain test doubles. Two cases inherit the real adapter
pagination against 600 server rows and verify all pages plus an older retry.
The Forgejo case fails before repair; both pass after repair.

The separate generated pagination trial remains distinct. Its patch uses the
adapter attribute without the compatibility default, and its regression tests
incorrectly expect two initial pages instead of ten. The live baseline passed
871 tests; the trial ended `error: full suite not green (exit=1)`, with no PR
or merge. The original durable budget consumed19 calls and84000 reserved
output tokens of49 calls/196000 tokens. No automated repair success or
publication is claimed. Original generated artifacts remain unchanged.

All 29 new and neighboring targeted cases pass; full Ruff passes. The combined
default suite passed **881 tests with three paid live-model skips** in79.53s.
Default JUnit SHA256:
`3b846230f90eab8ea38a6b86062e8d7ccc2ab32bc44225473ee079819c014bad`.
The independent pagination recheck passed64 neighboring tests and Ruff,
and reproduced the Forgejo baseline failure while GitHub remained green. Evidence:
`/tmp/fl4write-fresh-round20-report.md`,
`/tmp/fl4write-round20-path-review.md`,
`/tmp/fl4write-round20-clean-review.md`,
`/tmp/fl4write-round20-pagination-review.md`, and the baseline-red JUnit files
`/tmp/fl4write-round20-path-red.xml` and
`/tmp/fl4write-round20-pagination-red.xml`.

PR15 remains a draft. Main merge, mirror/runner delivery, successful automated
lifecycle proof, security review and CEO quality adjudication remain open.
