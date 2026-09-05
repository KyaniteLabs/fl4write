# Round 18 repair checkpoint

Fresh independent review of `4bb9e79` found five Major defects. The exact
candidate's isolated live suite passed 841 tests with zero skips and Ruff;
that passing run did not cancel the findings. The independent default run
passed 838 tests with three paid-live skips. Fresh green rounds remain 0/3.

| Finding | Current evidence |
| --- | --- |
| F18-001: PR freshness checks the default branch | Open PRs use the reviewed SHA; post-merge repairs retain current-default-branch freshness. Independently cleared through `66c9f27`. |
| F18-002: whole-file reviews truncate and discard findings | Complete supported input uses a shared 200000-byte file bound; PR diffs retain their 60000-character cap. Unsupported files defer before a model call. Independently cleared. |
| F18-003: repair attempts leak temporary clones | The public operation owns a temporary directory through publication and removes it in `finally`; durable evidence remains outside it. Success, pending, blocked and checkout-error cases are pinned. Independently cleared. |
| F18-004: listing failures erase old issue retries | Still open. Reserved for a real model-generated repair and automated PR/CI/merge/refresh trial. |
| F18-005: cached reviews stop merge polling | Owned repair PRs are polled once per enabled live GitHub fix cycle, respecting config, shadow and deadline gates. Independently cleared. |

Commit `60f2b73` implemented four repairs. The new 11 regression cases failed
against the original candidate and passed after repair. Independent review
then found two introduced boundary regressions: historical post-merge
freshness, and missing GitHub gating on the cycle merge drain. Two additional
real-cycle pins failed at `60f2b73`; `66c9f27` repairs both. The independent
recheck passed 166 focused tests with no skips and Ruff, clearing all four
scoped repairs. This is a repair recheck, not whole-project certification.

The first full repair run exposed an outdated freshness test double; it was
updated to accept the adapter's optional revision. Full default suite at
`60f2b73`: 849 passed, three paid-live skips. Full default suite at `66c9f27`:
851 passed, three paid-live skips. The README target is now 854 live passes.

Exact `4bb9e79` live JUnit SHA-256:
`aa2a767b05c16ca68c191b9475a0592ea37d2ebf9dcb24d3bccb9258c9d26305`.
That run used six model calls and 24000 reserved output tokens in the immutable
isolated runtime. It is evidence for the pre-repair candidate only.

Review covered all 28 package modules, runtime tooling and main contracts.
Every test implementation and every remote fleet configuration was not
individually audited. The parent created a local source index for efficient
subsequent reads; no AI summarization provider was used for indexing.

Remaining gates include the issue-retry repair, successful automated lifecycle,
three fresh clean rounds, required security review, PR15 main merge and
mirror/sole-runner synchronization, hourly health and CEO sample adjudication.
The 10:00 production cycle logged 34 ok/0 errors and no textual alerts, but
included one model-unavailable counter; this is not a clean provider-health
claim. The prepared 20-finding sample still needs the CEO's verdicts.

The earlier restricted security experiment was not repeated. That incomplete
review remains separate from functional clearance. Production is unchanged.
