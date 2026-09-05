# Round 18 issue retry repair

F18-004: a failed or incomplete issue listing returned the same empty list as
a successful listing with no open issues. Retry cleanup then discarded older
failed work below the watermark. The repair carries listing completeness in a
list-compatible result and only removes absent retries after a complete listing.

Four regression cases exercise the real issue cycle: fallback outage, malformed
envelope and exhausted pagination each preserve retry 5 below watermark 10,
then recover and triage that same issue; a complete empty listing removes a
closed retry. The three failure cases fail against 4593430; all four pass with
the manual repair. Existing plain-list callers remain supported.

The separate model-generated trial is not a successful repair receipt. Its
first response failed patch validation. Its second response passed the 854-test
live baseline and produced a red regression run, but independent review found
invalid fixtures: primary pagination ignored every failure flag, fallback used
the wrong exception class, and five collected cases contradicted its four-case
README update. The original generated patch and evidence remain unchanged.
The manual repair does not retroactively clear that automated trial.
The trial ended with `status=error`, `full suite not green (exit=1)`, no PR
and no merge. Its durable ledger consumed 20 calls and 100000 reserved output
tokens of the original 49-call / 196000-token cap. No extra generation follows
this failure.

Evidence: `/tmp/fl4write-issue-retry-manual-red.xml`,
`/tmp/fl4write-issue-retry-review.md`, and the isolated trial's
`issue-retry-evidence` directory. The full default suite passed 855 tests with
three paid live-model skips in 111.07 seconds; full Ruff passed. The independent
manual recheck passed 333 neighboring tests with no skips and targeted Ruff,
and found no actionable defect in the scoped repair. This manual tree has not
yet completed the full paid live suite at that checkpoint. The subsequent exact
`54521395392a3acada143360621d31d72789b9b5` isolated live run passed **858 tests,
zero skips, and Ruff**, using six real model calls and 24000 reserved output
tokens. JUnit SHA256:
`20ef09d8c3aa3434e28af7dacf0009ce00bc31ee708211beeaf0f4523d8de32c`.
Canonical Forgejo CI also passed at that exact commit. PR15 remains a draft; fresh certification
is 0/3, security review remains incomplete, and production is unchanged.
