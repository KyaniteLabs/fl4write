# R17-001 independent repair review

The exact replacement candidate has bounded clearance: no actionable
implementation defect was found. Full isolated execution and publication
remain separate gates.

The reviewed artifact has SHA-256
`c5ef9c52f6e95fd428bafee99faeed4e1648df2a3508c8f8b427049b60251d85`
and baseline `dcaf4c72af0b34194b47ebd7166c6d6710ba8ba1`. Its three files are
`README.md`, `fl4write/issues.py`, and `tests/test_issue_deadline_clock.py`.
The reviewer independently applied the patch in an isolated checkout and
verified all three file contents match the artifact exactly.

The caller creates a monotonic deadline; the issue lane now compares it with
the monotonic clock, retaining the existing five-second guard. The two tests
explicitly simulate different wall and monotonic clock values. A future
deadline permits triage; an expired deadline defers it. The README updates
only the current target count to 841 live passes or 838 default passes with
three paid-live skips, preserving historical counts.

Independent verification:

- The future-deadline regression fails on unchanged baseline source and
  passes after applying the exact repair.
- The new tests plus round 15 and round 17 neighboring tests: 46 passed.
- Ruff on the changed Python files: passed.
- Full collection: 841 tests.

These tests exercise the real issue-cycle branch with isolated intake/model
fixtures and shadow mode. They do not establish live comment publication,
full-suite success, or a fresh whole-project certification round. The earlier
two-file generated patch was rejected for omitting the README update; its
results cannot substitute for this replacement's full trial.

## CI trigger follow-up

A second independent reviewer cleared the one-line workflow change adding
`feature/exhaustive-loop-draft` to the existing `pull_request` branch filter.
YAML validation passed. The main target, push trigger, runtime image, checkout
options and test/lint commands remain unchanged. This repairs the missing
trigger discovered when PR16 had no checks. Actual delivery and a subsequent
candidate-target PR run remain separate from this bounded configuration review.
