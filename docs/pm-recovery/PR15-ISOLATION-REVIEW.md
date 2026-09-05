# Independent isolation correctness review

Verdict: **REQUEST_CHANGES** on `d91df89391db2c4211335b44e4e3c5d05ec65284`.

The independent native reviewer ran the existing exhaustive and sandbox suites:
40 passed in 21.49 seconds. The frozen checkout remained unchanged. Findings
follow from source control flow; no new reproductions or external calls were run.

1. **Major — cleanup failure accepted.** The runtime ignored the return code of
   `docker rm --force`, permitting a success result while its supervisor remained
   alive. Required: contain cleanup failure and preserve the owned container ID
   for recovery, retaining the original failure when both operations fail.
2. **Major — selected timeout ignored.** Atomic baseline, red-pin and fixed-suite
   phases passed 1800 seconds instead of the request's selected test timeout.
   Required: thread the selected timeout through all proof phases.
3. **Major — post-merge red suite escaped containment.** A refreshed suite's
   `NonGreen` exception bypassed ledger recording and normal escalation.
   Required: record the non-green result and completed merge receipt, reset the
   clean counter, and return the contained deferred outcome.

The parent is repairing these findings; this review does not approve those
subsequent changes. Its scope was functional correctness of the isolated runtime,
its exhaustive integration, and staged-byte comparison. It is neither whole-project
nor security certification. The earlier security review remains incomplete after
an automatic cybersecurity restriction; the stopped experiment was not repeated.

## Independent follow-up

The same reviewer independently checked the repairs at
`f53b7b191166f63f266f0e49893847680037d8af`: **APPROVE for these bounded repairs**.
All three findings cleared, 79 focused tests passed, and no new material functional
finding was identified. This follow-up is not a new whole-project clean round.
The parent full local suite and real isolated-container suite each passed 781
cases with three paid live-model skips at that checkpoint. Repository-wide Ruff
also passed. Security and full live certification remain open.

## CI diagnosis

Forgejo run 7227 failed in `actions/setup-python@v5`, before dependency installation
or tests: Python 3.12 was unavailable for the runner's Debian ARM64 image. The
workflow now selects the immutable Python 3.12 image used by the isolated runtime
and installs Git and Node before checkout. Forgejo requires Node in the job image
for JavaScript actions; see the [official Actions documentation](https://forgejo.org/docs/latest/user/actions/actions/).
Shared runner configuration is unchanged. This workflow correction still requires
a successful live CI run.

The install also now declares `pyjwt[crypto]`: App authentication signs RS256
tokens, which needs PyJWT's cryptographic dependency. A regression generates an
ephemeral key and verifies a real App JWT without contacting a forge or reading
operator credentials. See [PyJWT installation guidance](https://pyjwt.readthedocs.io/en/stable/installation.html).
