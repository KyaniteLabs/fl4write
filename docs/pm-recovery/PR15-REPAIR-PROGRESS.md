# PR15 repair candidate

Status: implementation in progress; not approved for merge or deployment.

The candidate derives clean-round counters from sealed ledger evidence,
rechecks archived source and JUnit digests, rejects contradictory JUnit
aggregates, and preserves pending recon when the test runner is unavailable.
Owned issue publication uses a strict public field allowlist and live identity,
author, repository and marker checks. Its transaction stages the exact body
before writing and advances canonical state only after readback succeeds.

The atomic fix executor prepares an implementation plus regression tests,
requires assertion-failure evidence on the original source, checks the full
baseline and rejects source mutation during tests. Model patches persist across
pending retries and produce deterministic commits. Forgejo merge request and
empty-response handling were checked against the live server OpenAPI schema.

Verification: the default full suite passed 752 tests with 3 live skips.
The integrated live full suite passed all 755 tests with zero skips in 116.66s.
The atomic executor has 25 focused passing tests; lint and diff checks pass.
The first full run found only a stale README count, which was corrected.
These results are not an exhaustion certificate or live merge-path proof.

Outstanding execution and review gates:

- Fresh independent review of the integrated candidate, including complete
  replay, state tampering, forge API, credential and process-isolation behavior.
- Real fix, regression, owned-PR merge, refresh and three clean-round dogfood.
- Review and repair all new findings; three consecutive fresh whole-project
  zero-new-defect rounds with full green evidence before exhaustion.
- Canonical PR merge, GitHub mirror and production runner synchronization.
- Original pilot obligations retain their existing independent gates, including
  organic-fix evidence, clean hourly health and CEO quality adjudication.

Known follow-up areas include replay after HEAD drift, recovery of uncertain
merge outcomes, complete pagination and required-CI semantics, issue creation,
and process isolation beyond scrubbed environment variables. A passing focused
suite does not establish these behaviors.
