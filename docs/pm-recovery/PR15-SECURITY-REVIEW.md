# PR15 independent security and containment review

Status: DONE_WITH_CONCERNS

## Scope and verdict

- Repository: `KyaniteLabs/fl4write`, PR15, branch `feature/exhaustive-loop-draft`.
- Reviewed HEAD: `5c3c0c40be9bedc7ad8f4efad4abd42fcd50c952` (matches the requested and `origin/feature/exhaustive-loop-draft` SHA).
- Review base / `origin/main`: `68ca576f4febbb34b46b18cc83589f4f03ae6af4` (matches the requested SHA).
- Draft range: `origin/main...HEAD`. Deployed-recovery range: `cc1e486..origin/main`.
- Verdict: **REQUEST CHANGES / DO NOT SHIP.** Forge issue publication is effectively quarantined, but the five previously recorded defects and missing atomic fix API remain unresolved. One additional credential-boundary defect exists in the reachable `--enable-fixes` path.

No source edits, commits, deployments, live models, network calls, forge mutations, or delegation were performed. The only created file is this review artifact.

## Verification performed

- `git rev-parse HEAD` -> `5c3c0c40be9bedc7ad8f4efad4abd42fcd50c952`.
- `git rev-parse origin/main` -> `68ca576f4febbb34b46b18cc83589f4f03ae6af4`.
- Codegraph exploration covered the exhaustive runner and its state, subprocess, forge, renderer, and config boundaries. The documentation index did not contain this worktree, so the prior review document was read with a scoped fallback.
- `python3 -m pytest -q tests/test_exhaustive.py tests/test_round15.py tests/test_pm_recovery.py` -> **93 passed in 16.06s**.
- Quarantine/credential/public-ledger focused selection -> **10 passed, 19 deselected in 4.63s**.
- `python3 -m ruff check fl4write/exhaustive.py tests/test_exhaustive.py` -> **All checks passed**.
- `git diff --check origin/main...HEAD` -> clean.
- Current hashes match `quarantine_verification.current_source_sha256`: `fl4write/exhaustive.py` `80bc4eb0...e5296a`; `tests/test_exhaustive.py` `b1dcc5ed...7069e`.
- Both deployed verification JSON files parsed successfully. No security/authority regression was found in the scoped `cc1e486..origin/main` recovery diff or its exercised tests.

## Draft findings

### F13-D01 — Critical — known, independently reproduced: forged clean state returns success without evidence

`_load_state` checks local field shapes but does not bind `consecutive_green`, `green_sha`, and `green_baseline` to trailing green ledger rows (`fl4write/exhaustive.py:123-165`). `run()` trusts those fields and returns success (`fl4write/exhaustive.py:532-534`).

Adversarial probe persisted `consecutive_green=3`, `green_sha=<current HEAD>`, one baseline ID, and an empty ledger, then supplied a test command that would exit 99. Result: `run()` returned `0`; the ledger remained empty. **Shipment blocker.**

### F13-D02 — Major — known, unresolved behind quarantine: publication is not transactional

The draft still advances and persists the green round before the publication boundary (`fl4write/exhaustive.py:619-651`). There is no pending body/hash replay state. `_publish` now always defers, so the prior outage/body-drift behavior cannot reach a forge; removing the quarantine would immediately restore it. **Contained, not fixed.**

### F13-D03 — Major — known, independently reproduced: public DTO leaks absolute paths

`_public_value` basename-reduces only `pack_manifest` and `coverage_manifest`; every other string is credential/control scrubbed but not path-redacted (`fl4write/exhaustive.py:397-414`).

Adversarial `_ledger_body` input retained synthetic absolute paths in `reason`, nested finding `message`, and `archived_source`. The path values are omitted from this report. **Contained by `_publish`, not fixed.**

### F13-D04 — Major — known, unresolved behind quarantine: no issue ownership contract

`_publish` is a hard stop (`fl4write/exhaustive.py:430-431`), which prevents overwrite today. No product-minted marker, fetch/compare, author check, or repo/issue identity pin was added. Re-enabling publication still lacks an ownership-safe implementation. **Contained, not fixed.**

### F13-D05 — Major — known, independently reproduced: green evidence remains mutable

`_pack` makes the archive, manifest, and extracted tree read-only, but `_atomic_json`, `_worker`, `_recon`, and `_test` do not finalize all referenced evidence (`fl4write/exhaustive.py:60-72, 168-190, 245-333, 365-381`). Probe modes were: manifest `0400`, ledger input `0600`, coverage `0600`, worker result `0644`. JUnit is likewise not sealed or re-hashed before later certification. **Shipment blocker.**

### F13-D06 — Major — new: missing fix API still mints and retains a privileged token

The GitHub fix path calls `install_token_to_env`, copies the installation token into the configured token environment, validates findings, and only then reports that the required atomic API does not exist (`fl4write/exhaustive.py:439-475`). This performs credentialed auth work and broadens the process environment even though no fix can possibly proceed. It also has no `finally` cleanup.

A no-network mock probe invoked `_request_owned_fixes` with a GitHub binding. The mint hook ran once, the function deferred for the missing atomic API, and the synthetic token remained in both process environment variables afterward. Move the missing-capability gate before authentication; when the API exists, scope the token to the single forge call and restore/delete environment values in `finally`. **Shipment blocker.**

## Containment adjudication

- Run boundary: any non-null `ledger_issue` defers before lock acquisition, config loading, adapter construction, state progress, or forge calls (`fl4write/exhaustive.py:502-504`).
- Publication boundary: `_publish` unconditionally raises `Deferred` and cannot call an adapter (`fl4write/exhaustive.py:430-431`).
- Focused tests and injected-adapter probes confirm first attempts and retries make zero forge calls and create no state progress.
- This is narrow forge-publication containment. It does not repair state integrity, public DTO safety, issue ownership, evidence immutability, or the fix/auth boundary. `--enable-fixes` remains a separate reachable path.

## Deployed recovery repairs (`cc1e486..origin/main`)

No request-change finding was discovered in the scoped deployed recovery changes for credential handling, adapter ownership, fork/HEAD rails, strict config parsing, or receipt JSON integrity. The 93-test recovery/exhaustive subset passed, and the deployed release/round-15 receipts are syntactically valid. This does not convert the draft review into approval and is not a fresh full-suite or live-model certification.

## Required before approval

1. Fix and regression-pin F13-D01 through F13-D06, including a strict public DTO, ownership-verified transactional publication, and immutable content-addressed evidence.
2. Implement the authenticated atomic fix-plus-regression-pin API with repository, author, fork, base, HEAD, changed-path, test-ID, and JUnit-hash rails; gate capability before minting credentials.
3. Re-run independent review and a current full suite at the final candidate SHA. Only then begin the separately authorized dogfood/quorum/three-clean-round gates.

## IMPROVEMENTS

1. **Gate capabilities before credentials.** Why: the missing API is known before auth, yet the code mints and retains a token. Fix: capability-check first and use a scoped token context manager with cleanup.
2. **Model certification as one validated state machine.** Why: individually plausible fields still combine into zero-evidence success. Fix: derive counters from immutable trailing ledger records rather than persisting independent authority fields.
3. **Generate verification receipts from the final SHA.** Why: the receipt mixes pre-quarantine full-suite hashes with post-quarantine focused hashes. Fix: emit one signed/hash-bound receipt per exact candidate SHA and label inherited evidence separately.
