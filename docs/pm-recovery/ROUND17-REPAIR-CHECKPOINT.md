# Round 17 repair checkpoint

PR15 remains a draft. Candidate `4831797` passed canonical Forgejo CI24.
The next repair, `e87db37`, corrects the Forgejo branch-head API discovered by
real execution; 48 focused tests and Ruff pass. Seven fresh-review findings
are repaired and independently cleared. The issue-deadline model patch passed
real baseline/red/fixed tests, but publication and lint gates remain open. No main
merge, production deployment or exhaustive certification is claimed.

## Independent review

Fresh whole-project review D041 inspected `4b1a141` and found eight defects:
five Major and three Minor. Its offline suite passed 802 tests with three paid
live-model skips. The parent's exact-head isolated live suite passed all 805
tests with no skips; that result does not cancel the review findings.

| Finding | Repair and current evidence |
| --- | --- |
| R17-001: incompatible issue deadline clocks | Model-generated monotonic-clock repair and two tests exist; real regression-red proof confirmed the new future-deadline test fails on the original source. Not merged. |
| R17-002: transport rewrites source | Transport preserves the complete caller prompt; HTTP and proxy payload pins cover ordinary HTML/CSS. Independently cleared. |
| R17-003: retro retry lost behind cursor | Expired parks remain eligible independently of cursor progress, survive pruning while the PR is still listed, and clear on success. Pins cover immediate recovery, another deferral, and re-parking. Independently cleared after a second review caught the pruning gap. |
| R17-004: merged listing failure permits pruning | Listing exceptions, malformed envelopes/rows and deadline skips preserve the prune barrier. Both real lane call paths are pinned. Independently cleared. |
| R17-005: omni ignores gatekeeper flag | Disabled gatekeeper preserves findings and makes no gatekeeper call. Independently cleared. |
| R17-006: nested config objects fail | Nested model/binding instances are accepted; malformed fields raise validation errors. Independently cleared. |
| R17-007: verifier setup telemetry unreachable | Fetch and checkout failures emit before returning. Independently cleared. |
| R17-008: gauntlet script misses self-check | PR query includes the field it filters; successful changed-file verification exits successfully. Shell fixture exercises the actual script. Independently cleared. |

D043 rechecked the seven repairs at `727b9b4`, then the retry follow-up through
`4831797`. Final bounded verdict: all R17-002–008 cleared; 74 independently run
tests and Ruff passed. This is a repair recheck, not a fresh certification round.

D042 separately reviewed explicit base-branch selection and compact exact-text
repairs. Its overlapping-match finding was fixed in `73a482b` and independently
cleared with 44 passing tests. No whole-project or security clearance follows.

## Parent verification

- `727b9b4`: full default suite 830 passed, three paid live-model skips.
- `4831797`: full default suite 832 passed, three paid live-model skips; full Ruff passed.
- Focused repair and neighboring suites 331 passed, with only the nested full-suite README check deselected; that check passed in the full run above.
- Canonical Forgejo CI23 at `73a482b` and CI24 at `4831797` passed.
- Isolated live baseline at `4831797`: 835 passed, zero skips, before the generated clock repair.
- Clock pin-only run: 837 tests, zero skips; the new future-deadline regression and the machine-checked README count failed. The new regression failure was independently required by the trial wrapper, so README failure alone could not qualify as a red pin.
- Clock fixed run: 837 passed, zero skips. Publication subsequently failed; this is test proof, not delivery proof.

JUnit SHA-256 records:

| Evidence | SHA-256 |
| --- | --- |
| `4b1a141` isolated live805 | `54c6db26122bfb2b81258ababa87bbeef99ec95fe4b25a239581dc3ba92f21dc` |
| `727b9b4` default833 collected | `c54de3f9faf3ada81478c9d91d182e10b1dd488c8709ad8d42de2e12a29a4247` |
| `4831797` default835 collected | `561468959fef564d38453d4fe4eab59360998c59bf77ceef55c23ba3004ed8fb` |
| Clock pin-only837 | `806ac570aff222f1f36b7e7fc9b7571c35295208e6262ed48981b0c49641620a` |
| Clock fixed837 | `f5076f85c4e225d0e5770815dca9758ea3dcc2ff5520736a22c8a7950d9bd4ec` |

## Real repair trial and remaining gates

The first trial at `73a482b` retained three failed generations: truncation at
4000 and 8000 output tokens, then an invalid row shape at 16000. Its durable ledger
records three calls and 28000 reserved output tokens; no repair PR was created.
The response schema was made explicit in `4831797`. A new exact-head trial has
a separate bounded ledger (49 calls, 196000 reserved-output-token cap); prior
failures remain recorded. Generation uses 16000 maximum output tokens; live
suite calls retain the repository's 4000-token route and share the trial ledger.
Provider credentials remain host-owned; tests run in the immutable Docker
runtime without general network access or forge credentials.

The revised prompt produced a valid three-file patch: one clock substitution,
two regression tests and the corresponding README count update. Inspection also
found an unused `pytest` import (Ruff F401) in the generated test. All three test
phases completed, but the Forgejo commit endpoint returned HTTP404 for the
branch name before a PR could be created. Live read-only probes confirmed that
both `main` and slash-containing names require Forgejo's branches endpoint,
whose commit identifier is `commit.id`. `e87db37` uses that endpoint and validates
the returned branch identity. The completed trial reserved 88000 output tokens
across 19 calls; the earlier failed trial's 28000-token record remains separate.
The lint defect also needs a corrected generation; subsequent proof must run
Ruff as well as the full test suite. The target is the draft feature branch,
leaving production unchanged.

Fresh whole-project zero-defect fully green rounds remain 0/3. Remaining work:
complete the real repair/CI/owned-PR merge/refresh lifecycle, resolve every
required review gate, earn three fresh green rounds, merge PR15 and verify
mirror/sole-runner synchronization. D036's security review remains incomplete
after an automatic restriction; its earlier experiment was not repeated and
the functional reviews here do not clear it.

The latest inspected hourly production cycle ended 09:03:06Z with 48 ok/0 errors,
but it alerted on incomplete merged-PR enumeration for a fleet repository. The zero-alert
pilot gate therefore remains open, as does the CEO quality-sample adjudication.
