# PR15 independent code review

**Both reviewers request changes. Keep this PR draft; do not merge or deploy.**

Two separate reviewer contexts assessed the same frozen code head
`5c3c0c40be9bedc7ad8f4efad4abd42fcd50c952` against
`68ca576f4febbb34b46b18cc83589f4f03ae6af4`. The second reviewer did not receive
the first review. The reports and this summary were added afterward; they do
not change the reviewed implementation.

- [Correctness review](PR15-CORRECTNESS-REVIEW.md): state transitions, outages,
  JUnit aggregation, evidence and regression proof.
- [Security review](PR15-SECURITY-REVIEW.md): credentials, ownership, publication,
  subprocess boundaries, evidence and scoped deployed repairs.

## Findings without duplication

Eight feature defects remain open: the five prior findings (forged clean state,
nontransactional publication, private-path leakage, missing issue ownership,
mutable evidence), plus three newly reproduced findings (runner outages consume
rounds, root JUnit failure totals can be ignored, and the missing fix API still
mints and retains credentials). Publication quarantine contains three of the
prior findings; it does not repair them. The fix/authentication path is separate
from that quarantine.

The atomic fix-plus-regression-pin workflow, post-fix refresh, runner integration,
and real dogfood/three-clean-round acceptance remain unfinished. The clean-round
counter remains0/3. This review is not exhaustive certification.

## Verification

The parent full suite at the reviewed head passed698 with3 live-model skips;
lint passed. [PR15-VERIFICATION.json](PR15-VERIFICATION.json) binds that result to
the exact head, source and JUnit hashes. Each reviewer independently passed the
93-test focused recovery/candidate subset. The correctness reviewer's full run
had696 passes,3 skips and2 failures caused by inherited Git signing settings in
a temporary-repository fixture and its nested README run. That known portability
defect remains open; the differing environments and results are preserved.

Both reviewers also examined the deployed recovery range `cc1e486..68ca576`.
Neither discovered a new request-change finding in that scoped review. This
does not imply a new full-suite live-model certification for production.

## Delivery

[Forgejo PR15](https://git.kyanitelabs.tech/KyaniteLabs/fl4write/pulls/15) is the
canonical review surface. The user authorized committing, pushing and independent
review. The branch is published for review; its implementation is unmerged and
not deployed. The already-delivered recovery repairs remain on main.
