# Local validation of this package

The exact publication package passed:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -B -m pytest -q -p no:cacheprovider tools/review-hardening/tests
304 passed in 0.77s

python3 -B -m ruff check --select E4,E7,E9,F tools/review-hardening
All checks passed!
```

Tests prohibit live HTTP/model calls and use authored synthetic records. Private
historical scorer replay and real held-out fixtures remain local; portable
synthetic tests exercise the same denominator and missing-coverage boundaries.
The isolated engine-wrapper and source-fidelity checks were separately validated
in the development worktree; those are not part of the published 304-test count.

The CI workflow uses push and pull_request events. Publication uses the supported
`[skip ci]` commit annotation under the existing CI resource limit; hosted CI
has not been claimed green. Workflow configuration and branch protection remain
unchanged. This toolkit is not wired into the live fleet.

Original runtime failures, frozen cases and denominators are preserved locally.
Effective reasoning budget and adequacy remain unknown. This evidence validates
the code boundary, not general model precision/recall or live deployment.
