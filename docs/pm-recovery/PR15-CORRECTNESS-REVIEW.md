# FL4WRITE exhaustive-loop draft correctness review

## Verdict

**REQUEST CHANGES.** Candidate `5c3c0c40be9bedc7ad8f4efad4abd42fcd50c952` against base `68ca576f4febbb34b46b18cc83589f4f03ae6af4` is safely quarantined from forge publication, but it is not correct enough to approve even as a local certification path. A forged persisted counter still returns success without any rounds, runner outages are recorded as completed non-green rounds, standard aggregate JUnit failures can be accepted as green, and referenced evidence remains mutable. The required authenticated fix -> refresh -> note workflow is explicitly absent.

This verdict is for the candidate delta `origin/main...HEAD`. It does not reject the already-deployed recovery tranche `cc1e48663986ab50af07fd44fbded7f7aa0bc92a..68ca576f4febbb34b46b18cc83589f4f03ae6af4`, whose focused recovery checks passed below. No merge, deployment, live model, network mutation, forge mutation, source edit, or commit was performed.

## Candidate findings

### C1 — Critical — persisted counters still forge a zero-round success

- Location: `fl4write/exhaustive.py:123-165`, `fl4write/exhaustive.py:517-545`.
- Evidence: `_load_state` validates each field's shallow shape but never binds `consecutive_green` and `green_sha` to trailing green ledger rows or evidence. A version-2 state with `round=0`, `ledger=[]`, `consecutive_green=3`, `green_sha=<HEAD>`, and `certified_sha=null` loads successfully. `run()` then returns `0` at lines 532-534 without executing recon or tests.
- Reproduction result: `forged_state_load=3`; `forged_state_run_rc=0`; persisted `certified_sha=None`.
- Adjudication: **known, unresolved F13-D01**. The new malformed-`green_baseline` pin does not cover cross-field state-machine consistency.
- Required repair: require exactly `consecutive_green` trailing green rows at `green_sha`, validate their `tested_head`, evidence hashes, timestamps, and baseline relationship, and prohibit every success path without three such rows.

### C2 — Major — runner outage advances the round and destroys the green streak

- Location: `fl4write/exhaustive.py:365-381`, `fl4write/exhaustive.py:596-600`, `fl4write/exhaustive.py:478-494`.
- Evidence: `_test` translates process unavailability/timeout into `NonGreen`; `run` then calls `_non_green`, increments `round`, appends a completed ledger row, and resets `consecutive_green`. The canonical contract says a runner outage defers the round and leaves the counter untouched.
- Reproduction result: after one green round, invoking `/definitely/missing-runner {junit}` changed state from `round=1, consecutive_green=1` to `round=2, consecutive_green=0`, reason `process unavailable: [Errno 2] ...`.
- Adjudication: **newly reproduced candidate defect**, distinct from known F13-D02 forge-publication outage behavior. Forge publication is now quarantined; runner-outage accounting is not.
- Required repair: distinguish infrastructure deferral from a completed red suite; retain/retry a pending attempt without consuming a round or changing the streak.

### C3 — Major — JUnit root aggregate failures can certify green

- Location: `fl4write/exhaustive.py:346-362`.
- Evidence: for a `<testsuites>` root, `_junit` checks counters only on direct `<testsuite>` children and ignores aggregate `tests`, `failures`, `errors`, and `skipped` on the root.
- Reproduction: `<testsuites tests="1" failures="1"><testsuite tests="1"><testcase classname="c" name="n"/></testsuite></testsuites>` returned `green=True`.
- Adjudication: **new**. Existing pins cover malformed child-suite counts and child nodes, not root aggregate disagreement.
- Required repair: validate root and suite aggregates consistently, reject contradictory declarations, namespaces, and unsupported shapes, and pin representative pytest/JUnit producers.

### C4 — Major — green evidence referenced by the ledger remains mutable

- Location: `fl4write/exhaustive.py:299-333`, `fl4write/exhaustive.py:557-633`.
- Evidence: after a green round, modes were `coverage_manifest=0600`, `ledger-input.json=0600`, `worker-request.json=0600`, `worker-result.json=0644`, and `full-suite.xml=0644`; only `pack_manifest` was `0400`. The ledger stores only a JUnit digest, does not store/revalidate hashes for the other evidence, and does not freeze the bundle.
- Adjudication: **known, unresolved F13-D05**.
- Required repair: finalize a content-addressed, read-only evidence bundle and re-hash every referenced artifact on load, retry, and certification.

### C5 — Major when quarantine is lifted — public DTO still leaks absolute paths

- Location: `fl4write/exhaustive.py:397-427`.
- Evidence: `_public_value` strips directory components only for `pack_manifest` and `coverage_manifest`; nested finding messages, evidence, reasons, and future string fields retain absolute machine paths. A ledger containing `<private-home>/...` in `reason` and nested `findings[].message` produced a body containing `<private-home>/`.
- Adjudication: **known, unresolved F13-D03; currently contained** because both `run` and `_publish` refuse publication (`fl4write/exhaustive.py:503-504`, `430-431`). F13-D04 issue ownership and F13-D02 publication retry are likewise unreachable, not repaired.
- Required repair: publish a strict allow-listed DTO with recursive local-identity/path redaction, ownership proof, and persisted identical retry payload before removing quarantine.

### C6 — Major — the accepted workflow is intentionally incomplete

- Location: `fl4write/exhaustive.py:439-475`, `fl4write/exhaustive.py:582-595`.
- Evidence: every finding either defers because fixes are disabled/unsupported or reaches an unconditional `Deferred` stating the executor lacks the atomic fix-plus-regression-pin API. There is no successful authenticated fix, post-fix HEAD refresh, fresh-context recon, or owned-PR acceptance path.
- Adjudication: **known acceptance gap**, not a regression. It directly fails acceptance criteria 2, 4, 5, and 6 in `EXHAUSTIVE-BUG-RESOLUTION.md`.
- Required repair: implement and regression-pin the exact owned-PR/fork/base/HEAD/auth rails, atomic fix plus pin, post-fix full-suite evidence, refreshed artifacts, fresh worker context, and three-round dogfood before shipment review.

## Already-deployed recovery regression check

The deployed range is `cc1e48663986ab50af07fd44fbded7f7aa0bc92a..68ca576f4febbb34b46b18cc83589f4f03ae6af4`. Candidate commits do not modify the deployed recovery source files; they add the exhaustive module/tests/docs and alter README text.

- `python3 -m pytest -q tests/test_exhaustive.py tests/test_round15.py tests/test_pm_recovery.py --junitxml=fl4write-review-focused.xml`: **93 passed in 19.43s**. This re-exercised counter/recovery pins plus deployed strict numeric config, invalid ports, timezone-aware timestamps, malformed PR listings, uncertain marker identity, readiness rendering/refresh, and refresh-outage fix deferral.
- `python3 -m ruff check fl4write/exhaustive.py tests/test_exhaustive.py fl4write/state.py fl4write/config.py fl4write/engine.py tests/test_round15.py tests/test_pm_recovery.py`: **passed**.
- `git diff --check origin/main...HEAD`: **passed**.
- Full `python3 -m pytest -q --junitxml=fl4write-pr-review-full.xml`: **2 failed, 696 passed, 3 skipped**. Both failures derive from `tests/test_gauntlet_fixes.py:4386`, whose temporary-repository commit inherits the operator's signing/hooks configuration. The direct case fails GPG signing; the nested README truth suite repeats it and therefore fails too. This is **known harness non-hermeticity**, already called out in `ROUND15-REPAIR-REVIEW.md`; it is not evidence of a deployed product regression, but it means this candidate has no current full-suite green proof in this environment.
- Live model cases were not run. `docs/pm-recovery/LIVE-EVAL-VERIFICATION.json` is historical evidence for 668/668 at an earlier tree, not current candidate evidence. The current candidate verification JSON accurately says live evaluation was false and records the current exhaustive/test hashes, but its `base_commit` is stale (`4a0b09a...`, not reviewed base `68ca576...`).

## Evidence integrity and exact acceptance

- Reviewed head: `5c3c0c40be9bedc7ad8f4efad4abd42fcd50c952`.
- Reviewed base: `68ca576f4febbb34b46b18cc83589f4f03ae6af4`.
- Candidate source SHA-256: `fl4write/exhaustive.py` `80bc4eb0ac24d1bed1af299c06a7f111d6cf7b4873ace059b928909a21e5296a`; `tests/test_exhaustive.py` `b1dcc5ed3818813e6c962c4050d947bfe9ae2089f112f926772e1efdae07069e`.
- Focused JUnit SHA-256: `b3f0a41977b3fd37987afe6441363a4effdfd637306412ae21144230a5e39a2f`.
- Full-run JUnit SHA-256: `9fc01ba15d68d1e075a70135c12816d11a3304a99e3f94c0affd1d5401e7cf65` (red run; not approval evidence).
- Approval requires all candidate findings above fixed and pinned, an actually green hermetic full suite at the final SHA, authenticated exact fix/refresh completion, immutable evidence, three valid trailing clean rounds at one SHA, and independent quorum review. Passing focused tests or keeping publication quarantined does not satisfy those gates.

## IMPROVEMENTS

1. **Encode exhaustive state as explicit transitions.** Why: independently plausible fields still combine into zero-round success and outages are misclassified as completed rounds. Fix: use validated `pending_recon`, `pending_test`, `deferred`, `green`, and `certified` states with cross-field/property tests.
2. **Adopt one strict evidence-bundle schema.** Why: JUnit aggregate contradictions pass while most referenced artifacts stay mutable and unhashed. Fix: normalize supported JUnit forms, hash every artifact into a manifest, chmod/finalize the whole bundle, and verify it on recovery/certification.
3. **Make tests hermetic from host Git settings.** Why: the full suite failed twice because a fixture commit inherited global signing and hooks. Fix: set `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, `core.hooksPath=/dev/null`, and `commit.gpgsign=false` in every temporary-repository helper and the nested doc-truth subprocess.
