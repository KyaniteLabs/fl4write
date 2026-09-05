# PR15 repair candidate

Status: implementation in progress; not approved for merge or deployment.

## Second repair pass

Independent review R2 requested changes on six integrated recovery and
certification defects plus a portable Git warning bug. The next candidate
adds request-bound recon and fix retries, requires owned publication before
certification, reconciles obsolete publication after HEAD movement, recovers
lost merge responses through exact owned-PR proof, and queries required CI
contexts with complete pagination. It also separates Git diagnostics from
porcelain status and uses verified single-repository GitHub App tokens.

Focused checks: 98 passed. Default full suite: 767 passed, 3 live skips.
Latest integrated live full suite: 769 passed, 1 failed. The failed live
median-unsorted recall case remains a quality gate; the earlier all-green
checkpoint does not override this result. Fresh security review, isolated
live fix/merge/refresh execution and three clean fresh rounds remain required.

The signing GitHub App and token scope were verified live. Docker access is
available on the canonical runner, but unprivileged bubblewrap could not
configure its isolated loopback interface. No production runner was replaced
or second production runner started during these capability probes.

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
