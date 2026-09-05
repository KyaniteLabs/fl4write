# Independent FL4WRITE correctness and integration review

Status: DONE_WITH_CONCERNS

## Verdict

**REQUEST_CHANGES** on frozen commit `0217dcb5863b658e1ea1e0c86161be91bf2f7a89`.

The repair fixes several PR15 defects: persisted counters are derived from verified ledger rows, JUnit aggregates are checked recursively, runner outages retain recon, evidence bundles are content-addressed, publication is ownership-checked and body-staged, and the fix lane proves a red regression pin plus a green fixed tree. Those repairs do not complete the integrated contract. Five material defects below remain. This review is therefore not a clean exhaustion round.

No tracked file, commit, branch, remote, forge, model, publication, or deployment was changed or called. Controlled probes used temporary directories only.

## Findings

### R2-01 — Critical — local execution certifies without required publication

- Location: `fl4write/exhaustive.py:569-582`, `fl4write/exhaustive.py:710-730`.
- Trigger: complete three green rounds while omitting `--ledger-issue`, which the CLI permits.
- Observed: the runner sets `certified_sha`, writes a `local_evidence_green` artifact, returns `0`, and every later invocation returns success at line 559. Repository ownership, issue ownership, ledger publication, and forge readback are never exercised.
- Expected: three local green rounds may be recorded as locally complete, but certification and success must require the canonical owned ledger publication required by the loop contract. Absence of a publication target must remain an explicit incomplete gate.
- Reproduction: `python3 -m pytest -q tests/test_exhaustive.py::test_real_snapshot_fixed_subprocess_and_coverage_certify` passes and explicitly asserts certification after calling `run()` without `ledger_issue`; direct fixture execution produced `certified_sha=<HEAD>` and return code `0` after three local rounds.
- Suggested fix: separate `locally_complete_sha` from `certified_sha`; never set the latter or return certification success until an owned forge artifact is staged, written, and read back. Require or safely create the owned issue before certification.

### R2-02 — Major — pending recon can be certified under a different test command or config

- Location: `fl4write/exhaustive.py:116-123`, `fl4write/exhaustive.py:598-630`, `fl4write/exhaustive.py:668-709`.
- Trigger: recon completes, the configured runner is unavailable, then the next invocation changes `args.test_command` (or external config) while HEAD remains unchanged.
- Observed: `pending_round` binds only the reviewed HEAD, finding count, recon data, and evidence. Retry reuses the sealed recon but runs the newly supplied command. A controlled two-invocation probe changed the command to a script emitting one green `replacement::only` testcase; the retry advanced to `consecutive_green=1` with that test ID. Output: `outage_rc=2`, `changed_command_rc=2`, `changed_command_green=1 ['replacement::only']`.
- Expected: a pending round must be replayed under the identical selected test argv and relevant config, or be invalidated without counting. A caller must not weaken/change the suite after recon.
- Exact reproduction: create the repository with `tests/test_exhaustive.py::_repo`; monkeypatch `_test` to raise `Deferred` for invocation one; restore `_test`; invoke the same state with `test_command=[python, -c, <write one-case green JUnit>, '{junit}']`; inspect `state.json`.
- Suggested fix: persist canonical hashes of fixed argv, resolved executable identity, test timeout, config, model route, budgets, and relevant environment selection in `pending_round` and every completed row; reject drift before executing tests.

### R2-03 — Major — publication HEAD drift creates a permanently unreplayable transaction

- Location: `fl4write/exhaustive_transaction.py:36-69`; called before HEAD reconciliation at `fl4write/exhaustive.py:538-543`.
- Trigger: a forge write is uncertain, leaving `publication-pending.json`, then repository HEAD advances before retry.
- Observed: replay checks current HEAD against the staged state's old HEAD and raises `Deferred`. Because replay always runs before normal HEAD invalidation and no path archives/cancels the obsolete transaction, every later invocation repeats the same failure. Controlled output across two retries was `publication_after_drift_rc1=2`, `publication_after_drift_rc2=2`, `pending_retained=True`, `canonical_round=0`.
- Expected: retry must first determine whether the exact old body landed. If it did, record the old publication outcome then invalidate it for the new HEAD; if it did not, safely close/archive the obsolete transaction and restart at the new HEAD. It must not deadlock the repository indefinitely.
- Exact reproduction: run one fixture round with `ledger_issue=13` and an adapter whose PATCH raises after staging; commit a new local HEAD; invoke twice with the same state and adapter.
- Suggested fix: add explicit transaction states and an old-HEAD reconciliation path. Read before write, compare the exact staged body/hash, finalize or archive the old transaction, then invalidate counters and proceed at current HEAD.

### R2-04 — Major — prepared fix retry is not bound to findings, config, or test command

- Location: `fl4write/exhaustive_fix.py:399-418`.
- Trigger: `prepared-patch.json` already exists for a HEAD, but a later round at the same HEAD has different findings or the repair model/rules/test command changed.
- Observed: only `reviewed_head` is checked. A controlled probe prepared a patch for finding `OLD`, retried with finding `NEW`, and returned the old patch without calling the model: `changed_findings_reused=True`; persisted keys were only `patch` and `reviewed_head`.
- Expected: model-call idempotency must reuse a patch only for the exact repair request. Different findings or policy/config/test inputs require a distinct transaction or fail-closed invalidation.
- Exact reproduction: call `_prepared_patch(config, head, [{'id':'OLD','path':'calc.py'}], ...)`, replace `_model_patch` with a function that fails if called, then call with `NEW`; the second call returns the first result.
- Suggested fix: key the fix transaction directory and persisted request by canonical hashes of reviewed HEAD, normalized findings, model route, repository laws, fixed test argv, and source hashes. Validate all hashes before reuse.

### R2-05 — Major — both forge CI gates truncate evidence and do not prove required checks

- Location: `fl4write/exhaustive_fix.py:334-352`.
- Trigger: GitHub exposes more than 100 check runs, or Forgejo exposes more than 100 commit statuses; a failing/pending required result lies after page one. Separately, the code never queries which checks are required by branch protection/rules.
- Observed: GitHub requests one `per_page=100` page and ignores `total_count`; Forgejo requests one `limit=100` page. Controlled adapters returned 100 green rows while declaring/withholding an additional row; both returned `True` and made no page-2 call. Output: `github_checks_green=True` with only `...check-runs?per_page=100`; `forgejo_checks_green=True` with only `...statuses?limit=100`.
- Expected: incomplete enumeration is pending/unqueryable, never green, and merge approval must prove every required check/context for the protected base branch.
- Suggested fix: implement bounded complete pagination with fail-closed cap handling for both APIs; query branch-protection/rules-required contexts where supported, map latest status per context, and refuse merge when required-check semantics cannot be established.

### R2-06 — Major — uncertain merge recovery cannot recognize a merge that actually landed

- Location: `fl4write/exhaustive_fix.py:448-490`, `fl4write/exhaustive_fix.py:498-565`.
- Trigger: the merge request succeeds remotely but its response/readback is lost, so the receipt records `pending: merge outcome was not proven`.
- Observed: on retry, remote default HEAD is no longer `reviewed_head`; the function returns `pending: remote default HEAD drifted` before looking up the prior PR or determining whether that exact proved commit was merged. `find_pr` also searches only open PRs, so a closed merged PR is invisible.
- Expected: retry must resolve the recorded PR/commit, accept only proof that the exact bot-owned PR merged into the expected base lineage, and return the verified merged HEAD; unrelated drift must remain pending.
- Reproduction: use the existing `test_unproven_merge_is_pending` fake but advance its default branch as the failed response would imply, then retry with the same evidence directory. The second call exits at the base-drift check without querying the merged PR.
- Suggested fix: persist PR number, commit SHA, base SHA, and transaction phase before merge; on retry fetch the PR by number (open or closed), verify ownership/head/base/merge SHA and default ancestry, then finalize or report genuine conflicting drift.

## Additional whole-project defect

### R2-07 — Minor — clean-check script treats benign Git stderr warnings as dirty files

- Location: `check-dirty.sh:11-25`; pin at `tests/test_gauntlet_fixes.py:4379-4394`.
- Trigger: `git status --porcelain` exits zero but emits a macOS `confstr()` temporary-directory warning on stderr.
- Observed: `2>&1` stores the warning in `STATUS`; the script counts it as one changed file and exits 1. This caused the current full-suite failure and its nested README-truth duplicate.
- Expected: exit-zero stderr diagnostics must not be parsed as porcelain records, while nonzero Git status must remain fail-closed with captured diagnostics.
- Reproduction: the current `python3 -m pytest -q` run failed `test_check_dirty_clean_checkout_passes`; stdout reported `ALERT: 0 untracked + 1 changed files`, while stderr showed the `confstr()` warning.
- Suggested fix: capture stdout and stderr separately, parse only stdout after a zero exit, and include stderr only in diagnostics. Add a fake-git pin for exit-zero stderr noise.

## Verification and limitations

- Recorded HEAD: `0217dcb5863b658e1ea1e0c86161be91bf2f7a89` (also the checked-out branch and recorded origin feature ref at review start).
- Worktree was clean before and after review.
- Focused exhaustive suite: `83 passed in 20.34s`.
- Ruff on all exhaustive implementation and focused test modules: passed.
- `git diff --check`: passed.
- Full suite: **2 failed, 750 passed, 3 skipped in 180.20s**. One underlying sandbox-specific `check-dirty.sh` failure caused both the direct failure and the nested README-truth failure. This run is not green. Parent-reported runs (752/3 skipped and 755/0 skipped) were not treated as this review's execution evidence.
- Live model checks: not run by instruction. The three skips are live tests and are reported as skips, not passes.
- No live forge/API contract call was made. API conclusions are from implementation and controlled adapters; this limits confidence in provider-specific response compatibility, but does not weaken the reproduced pagination and recovery defects.
- CodeGraph was attempted first but this worktree has no `.codegraph/` index; its CLI explicitly directed normal-tool fallback. `jcode`/`jdoc` were unavailable.
- The review scanned all new exhaustive modules and focused tests plus relevant forge/config/state surfaces. It did not claim semantic exhaustion of every older 10k+ line module.

## Approval conditions

Repair and regression-pin R2-01 through R2-06, rerun a genuinely green full suite at the final SHA, then repeat independent whole-project review. Certification additionally requires real owned publication, uncertain-outcome recovery, complete required-CI proof, real fix/PR/merge/refresh execution, and three consecutive fresh zero-new-defect rounds. R2-07 should be repaired so the standard suite is hermetic across supported runner environments.

## IMPROVEMENTS

1. **Model the loop as durable typed transactions.** Why: recon, test, publication, and merge each persist only part of their identity, producing drift acceptance or permanent pending states. Fix: use phase-specific schemas whose canonical request hashes cover every authority-bearing input and whose recovery transitions are explicit.
2. **Centralize complete forge evidence collection.** Why: the new fix client reintroduced one-page CI logic already treated as unsafe elsewhere in the project. Fix: reuse one bounded pagination and required-check abstraction for GitHub and Forgejo rather than a second bespoke client.
3. **Test real restart boundaries.** Why: same-process happy paths hide changed argv/config, uncertain writes, merged-but-unread responses, and HEAD movement. Fix: make acceptance tests stop after every persisted phase, mutate one input, construct a new runner instance, and assert safe recovery.
