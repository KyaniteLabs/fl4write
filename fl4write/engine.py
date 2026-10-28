"""Engine: one poll-invariant cycle per repo — collect, dedupe, review, fix, triage.

Design laws (ralplan-approved):
- The head-SHA predicate decides re-review; trigger reason is annotation only.
- get_diff is REQUIRED: grounding without a real diff file-set is vacuous.
- ONE state owner per cycle: this module loads once and saves once; lanes
  mutate the dict (a lane doing its own load+save caused the email-storm
  lost update — LEARNINGS #17). Checkpoint saves after each PR keep a
  mid-cycle kill from losing the whole cycle's memory.
- Per-PR containment: one PR's forge/model failure never aborts the others
  and never loses the cycle's state (the mirror degrade law, extended to
  primaries).

Audit 2026-09-01:
- previous_findings now parses the RENDERER's actual format via
  renderer.parse_finding_lines (the old regex matched a format nothing
  emitted — every finding was 🆕 forever and ✅ resolution never existed).
- config.gatekeeper and config.issues_enabled are honored (dead knobs).
- A model-failure retry cap per (PR, SHA) stops the infinite retry loop on
  permanently unparseable generations.
- Deadline awareness: start no new PR when the remaining budget can't fit a
  review, so a runner kill can't livelock the repo (LEARNINGS #24 class).
"""

from __future__ import annotations

import logging

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import fixlane, gatekeeper, renderer, state
from .config import RepoConfig
from .forges import ForgeAdapter, ForgeError, adapter_for
from .models import CycleReport  # CycleReport moved to models (L6 split)
# sweep family moved to engine_omni (L6 split); run_cycle calls the steps,
# the aliased names stay re-exported because the tests import them from here
from .engine_omni import (REVIEW_BUDGET_S, ShadowSink, _ci_watch_step,
                          _fix_freshness_gate, _omnisweep_step, _retro_sweep)
from .engine_omni import _CI_BENIGN as _CI_BENIGN  # noqa: F401 re-export
from .engine_omni import _has_prior_findings as _has_prior_findings
from .engine_omni import _omni_completed_actions as _omni_completed_actions
from .engine_omni import _omni_fix_phase as _omni_fix_phase
from .engine_omni import _omni_readiness as _omni_readiness
from .engine_omni import _omni_report_body as _omni_report_body
from .engine_omni import _omni_reset_sweep as _omni_reset_sweep
from .engine_omni import _omni_upsert_issue as _omni_upsert_issue
from .engine_omni import _prior_findings as _prior_findings
from .engine_omni import _probe_head as _probe_head
from .engine_omni import _retro_review_pr as _retro_review_pr
from .models import Finding, PullRequest
from .state import CycleLock, CycleLockHeld

log = logging.getLogger("fl4write.engine")



# F10-E004 (sol DOM-E): test discovery must cover the standard conventions —
# *_test.go/_test.rs/_test.ts/_test.js, *.test.* and *.spec.* for the JS/TS
# families, python test_* files, and the __tests__/ + tests?/ directory
# conventions for non-python files. Widening matters: a changed test that
# discovery misses bypasses the deterministic verifier SILENTLY (no UNVERIFIED
# state); one it flags but cannot target degrades to UNVERIFIED, never a false
# Critical.
_TEST_FILE_RE = re.compile(
    r"(^|/)(test_[^/]+\.py|[^/]+_test\.(go|rs|ts|js)|"
    r"[^/]+\.(test|spec)\.(ts|js|tsx|jsx|mjs|cjs)|tests?\.py)$"
)
_DIR_TEST_RE = re.compile(
    r"(^|/)(__tests__/|tests?/)[^/]+\.(ts|js|tsx|jsx|mjs|cjs|go|rs)$"
)


def _is_test_path(path: str) -> bool:
    """Single source of truth for 'is this changed file a test?' — used by
    the deterministic verify gate (python files are targetable by pytest;
    every other convention must reach the UNVERIFIED degrade, never silence)."""
    return bool(_TEST_FILE_RE.search(path) or _DIR_TEST_RE.search(path))
MODEL_FAILURE_CAP = 3  # per (PR, head SHA); exceed = alert-and-stop retrying


def _mirror_shas(mirrors: list[ForgeAdapter], repo: str, report: CycleReport) -> set[str]:
    """Mirror completeness set — degradation law: unreachable mirror logs+skips."""
    shas: set[str] = set()
    for m in mirrors:
        try:
            shas |= {pr.head_sha for pr in m.list_open_prs(repo)}
        except Exception as exc:  # mirror degrade law

            report.mirror_degraded += 1
            log.warning("mirror %s degraded (log+skip, never abort): %s", m.name, exc)
    return shas


def _review_pr(
    pr: PullRequest,
    config: RepoConfig,
    primary: ForgeAdapter,
    get_diff: Callable[[PullRequest], tuple[set[str], str] | None],
    shadow_sink: ShadowSink | None,
    st: dict[str, Any],
    report: CycleReport,
    run_fixes: bool,
    post_merge: bool = False,
    deadline: float | None = None,
) -> str:
    """Review one PR. Contained: any failure logs and returns — the cycle and
    its state survive. The post-merge sweep treats "reviewed" and
    "model-failed-cap" as terminal. It records "shadow" separately for live
    cutover and stops on deferred outcomes ("diff-unavailable",
    "model-unavailable", "deferred", "fix-deferred") for later retry.
    Dependency filtering occurs separately before this function is called."""
    from .analyzer import ModelUnavailable, analyze

    diff = get_diff(pr)
    if diff is None:
        # Diff fetch failed: DO NOT review (vacuous grounding posts 🎉 over
        # real findings — LEARNINGS #3) and DO NOT mark reviewed.
        report.skipped_diff_unavailable += 1
        report.alerts.append(f"diff unavailable for #{pr.number} — not reviewed, will retry")
        return "diff-unavailable"
    diff_files, diff_text = diff
    findings: list = []

    # Deterministic spec check first: run the diff's own tests (sandboxed).
    # Rails (audit A2): NEVER on fork PRs (hostile by org law); GitHub-only
    # (the fetch is github.com — on Forgejo it silently died per-cycle).
    # Budget (audit A7): verify can cost ~500s worst case — reserve it or skip.
    test_like = sorted(p for p in diff_files if _is_test_path(p))
    if (
        test_like and config.verify_tests and not config.shadow
        and not pr.is_fork and primary.name == "github"
        and (deadline is None or (deadline - time.monotonic()) > REVIEW_BUDGET_S + 500)
    ):
        from . import executor as _ex

        failing = _ex.verify_diff_tests(pr, config, test_like, deadline=deadline)
        if failing is not None:
            findings.append(failing)
            report.alerts.append(
                f"#{pr.number}: diff's own tests FAIL at head — deterministic Critical filed"
            )
            # MECE round-6 (luna-max F6-C016): verify consumed the budget —
            # re-check BEFORE starting model work even when it FAILED (the
            # deterministic finding posts on a later cycle with the model)
            if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                report.alerts.append(f"#{pr.number}: verify consumed the review budget — deferred")
                return "deferred"
        elif deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S + 5:
            # MECE round-3 (glm F3-3): verify_tests can consume ~500s of a
            # ~90s review budget — defer the analyze rather than overrun the
            # whole cycle on one PR (the old comment claimed a later gate
            # that never existed)
            report.alerts.append(f"#{pr.number}: verify consumed the review budget — deferred")
            return "deferred"

    try:
        doc = analyze(pr, diff_files, diff_text, config)
    except ModelUnavailable as exc:
        deterministic = [f for f in findings if f.category == "CI"]
        if deterministic:
            # MECE round-6 (luna-max F6-C007): the deterministic verify
            # finding must never be swallowed by a model outage — post it now
            # (verify evidence is model-independent), then mark reviewed
            rh = f"{pr.head_sha[:12]}{len(deterministic):04x}"
            body = renderer.render_review(
                pr, deterministic, config, rh, [], diff_truncated=False,
                post_merge=post_merge)
            if config.shadow:
                if shadow_sink:
                    shadow_sink(config.repo, pr.number, body)
            else:
                existing = primary.get_persistent_comment(config.repo, pr.number)
                if existing:
                    primary.update_comment(config.repo, pr.number, existing[0], body)
                else:
                    primary.create_comment(config.repo, pr.number, body)
            report.reviewed += 1
            if config.shadow:
                report.alerts.append(
                    f"#{pr.number}: model unavailable — deterministic verify finding "
                    f"posted to the shadow sink (live retries under its own cap)")
                return "model-unavailable"
            # F11-C003 (reopened F6-C007): posting the deterministic finding
            # must NOT terminalize the SHA — the old mark_reviewed made
            # 'reviewed:1' and needs_review False, so the deferred model
            # analysis was NEVER retried and other defects at this SHA were
            # permanently invisible. The SHA stays reviewable under the
            # ordinary model-failure cap; the edit-in-place comment is
            # idempotent while only the deterministic finding exists.
            key = f"{pr.number}:{pr.head_sha[:10]}"
            fails = int(st.get("model_failures", {}).get(key, 0)) + 1
            st.setdefault("model_failures", {})[key] = fails
            report.alerts.append(
                f"#{pr.number}: model unavailable — deterministic verify finding posted "
                f"(model analysis deferred; attempt {fails}/{MODEL_FAILURE_CAP} at this SHA)")
            if fails >= MODEL_FAILURE_CAP:
                state.mark_reviewed(st, pr.number, pr.head_sha, "model-failed-cap")
                report.alerts.append(
                    f"#{pr.number}: model failed {fails}x at this SHA after the "
                    f"deterministic finding — needs human look")
                return "model-failed-cap"
            return "model-unavailable"
        report.model_unavailable += 1
        # MECE round-6 (luna-max F6-C005): shadow runs must not consume the
        # LIVE model-failure counters or reach the live model-failed-cap — the
        # live cutover then never retries the SHA
        if config.shadow:
            log.warning("model unavailable for %s#%s (shadow): %s", config.repo, pr.number, exc)
            return "model-unavailable"
        key = f"{pr.number}:{pr.head_sha[:10]}"
        fails = int(st.get("model_failures", {}).get(key, 0)) + 1
        st.setdefault("model_failures", {})[key] = fails
        if fails >= MODEL_FAILURE_CAP:
            # Terminal: stop retrying this SHA (the alert asks for a human);
            # mark it so a re-listed PR is not re-modeled every sweep.
            state.mark_reviewed(st, pr.number, pr.head_sha, "model-failed-cap")
            report.alerts.append(f"#{pr.number}: model failed {fails}x at this SHA — needs human look")
            return "model-failed-cap"
        log.warning("model unavailable for %s#%s: %s", config.repo, pr.number, exc)
        return "model-unavailable"
    st.get("model_failures", {}).pop(f"{pr.number}:{pr.head_sha[:10]}", None)

    deterministic = [f for f in findings if f.category == "CI"]
    findings = [f for f in findings if f.category != "CI"] + doc.findings
    dropped_here = 0  # MECE round-4 (luna F4-006): per-PR count for the comment
    if findings and config.gatekeeper:
        findings, dropped, failed_open = gatekeeper.filter_findings(findings, config)
        report.gatekeeper_dropped += dropped
        dropped_here = dropped
        if failed_open:
            report.gatekeeper_failed += 1
    # the deterministic verify finding is NEVER gatekeeper-droppable (A4)
    findings = deterministic + findings

    rh = f"{pr.head_sha[:12]}{len(findings):04x}"
    previous: list[Finding] = []
    existing = primary.get_persistent_comment(config.repo, pr.number)
    if existing:
        previous = _prior_findings(existing[1])
    body = renderer.render_review(
        pr, findings, config, rh, previous,
        gatekeeper_dropped=dropped_here or 0,
        diff_truncated=bool(doc.digest.get("_diff_truncated")),
        post_merge=post_merge,
    )
    if config.shadow:
        if shadow_sink:
            shadow_sink(config.repo, pr.number, body)
        report.shadow_only = True
    elif existing:
        primary.update_comment(config.repo, pr.number, existing[0], body)
    else:
        primary.create_comment(config.repo, pr.number, body)
    outcome = f"shadow:{len(findings)}" if config.shadow else f"reviewed:{len(findings)}"
    state.mark_reviewed(st, pr.number, pr.head_sha, outcome)
    report.reviewed += 1

    # finding-level severity into telemetry (the gap: the quality loop
    # couldn't measure "fixable findings per day" without this)
    from . import telemetry as _tel
    sev_counts = Counter(f.severity for f in findings)
    _tel.emit("review", repo=config.repo, pr=pr.number,
              findings_total=len(findings), severity=dict(sev_counts),
              lane="post-merge" if post_merge else "pr")

    if run_fixes and config.fix.enabled and not config.shadow:
        if any(f.category == "CI" for f in findings):
            # audit A5: a verify Critical anchors at the TEST file — the fix
            # lane patching it is structurally test-weakening. Human surface.
            findings_for_fix = [f for f in findings if f.category != "CI"]
            report.alerts.append(
                f"#{pr.number}: failing-diff Critical posted for HUMAN action — "
                "the fix lane will not patch test files"
            )
        else:
            findings_for_fix = findings
        if primary.name != "github":
            # Comorbidity pass catch: the executor's PR/merge path is
            # GitHub-hardcoded — attempting anyway burns a model call then
            # dies as a contained error every cycle (silent feature death).
            # Fail LOUD as an escalation instead.
            blocked = "fix lane is GitHub-only in v1 — Forgejo repos are review/comment-only"
            # MECE round-1 (M3 DOM-C #7): escalate ONCE with the full finding
            # list — one comment per finding was comment-spam on FJ PRs
            escalatable = [f for f in findings_for_fix if f.severity in ("Critical", "Major")]
            if escalatable:
                primary.create_comment(config.repo, pr.number,
                                       fixlane.escalate(pr, escalatable, blocked))
                report.fix_escalations += len(escalatable)
        else:
            _fix_lane(pr, findings_for_fix, config, primary, st, report,
                      post_merge=post_merge, deadline=deadline)
            if st["prs"][str(pr.number)].get("pending_fixes"):
                return "fix-deferred"

    return "shadow" if config.shadow else "reviewed"


def _fix_lane(
    pr: PullRequest,
    findings: list[Finding],
    config: RepoConfig,
    primary: ForgeAdapter,
    st: dict[str, Any],
    report: CycleReport,
    *, post_merge: bool = False, deadline: float | None = None,
) -> None:
    """Attempt fixes for Critical/Major findings; capped by fix_depth in state
    (which now PERSISTS across pushes — mark_reviewed merges, not replaces)."""
    from . import executor

    pr_state = st["prs"].setdefault(str(pr.number), {})
    pending = [f for f in findings if f.severity in ("Critical", "Major")]
    pr_state["pending_fix_sha"] = pr.head_sha
    pr_state["pending_fixes"] = [f.model_dump() for f in pending]
    for f in pending:
        if deadline is not None and time.monotonic() >= deadline:
            report.alerts.append(f"#{pr.number}: fix work deferred — cycle deadline reached")
            return
        depth = int(pr_state.get("fix_depth", 0))
        try:
            blocked = fixlane.fix_allowed(pr, config, depth)
        except fixlane.FixLaneBlocked as exc:
            # Cycle-3 review: the -O-proof rail raise must not escape the
            # lane and kill the cycle — escalate exactly like a rail block.
            blocked = f"fix rail diverged from config ({exc})"
        if blocked is not None:
            body = fixlane.escalate(pr, [f], blocked)
            primary.create_comment(config.repo, pr.number, body)
            report.fix_escalations += 1
            pr_state["pending_fixes"].pop(0)
            continue
        if not _fix_freshness_gate(primary, config, f, None if post_merge else pr.head_sha):
            pr_state["pending_fixes"].pop(0)
            continue
        if deadline is not None and time.monotonic() >= deadline:
            report.alerts.append(f"#{pr.number}: fix work deferred — cycle deadline reached")
            return
        report.fix_attempts += 1
        try:
            result = executor.attempt_fix(pr, f, config, depth_used=depth)
        except Exception as exc:  # noqa: BLE001 — unexpected escape past the
            # containment belts must not lose the pending entry: it stays
            # queued and is re-derived next cycle (entries rebuild per lane).
            report.alerts.append(f"#{pr.number}: fix attempt crashed, entry kept: {type(exc).__name__}")
            report.fix_failures += 1
            continue
        pr_state["pending_fixes"].pop(0)
        status = result.get("status")
        if status == "pr_opened":
            report.fix_prs_opened += 1
            pr_state["fix_depth"] = depth + 1
        elif status in ("error", "testfail", "nofix"):
            # enumerated failure classes (the Architect's V2): every outcome
            # is COUNTED; the summarizing alert fires once at cycle end
            report.fix_failures += 1
            report._fix_failure_notes.append(f"#{pr.number} {status}: {str(result.get('reason'))[:80]}")
        elif status == "blocked":
            report.fix_escalations += 1
        else:
            # Sol audit: unknown statuses had no denominator — count + surface
            report.fix_failures += 1
            report._fix_failure_notes.append(f"#{pr.number} unknown-status {status!r}")
    pr_state.pop("pending_fixes", None)
    pr_state.pop("pending_fix_sha", None)


def _resume_fix_lane(pr, config, primary, st, report, run_fixes, deadline, *, post_merge=False):
    """Resume only unattempted fixes; a completed review stays completed.

    Returns "gated-off" when the fix lane is structurally unable to run
    (run_fixes=False, fix.enabled=false, shadow, or a non-GitHub primary —
    the Forgejo cutover shape), "dropped" when a stale/malformed record was
    cleared, and "attempted" when the lane actually executed for this PR."""
    if not (run_fixes and config.fix.enabled and not config.shadow and primary.name == "github"):
        return "gated-off"
    rec = st["prs"].get(str(pr.number), {})
    if rec.get("pending_fix_sha") != pr.head_sha:
        rec.pop("pending_fixes", None)
        rec.pop("pending_fix_sha", None)
        return "dropped"
    from pydantic import ValidationError
    try:
        rows = rec.get("pending_fixes", [])
        if not isinstance(rows, list):
            raise ValueError("pending fixes must be a list")
        findings = [Finding.model_validate(row) for row in rows]
    except (ValidationError, ValueError, TypeError):
        rec.pop("pending_fixes", None)
        rec.pop("pending_fix_sha", None)
        report.alerts.append(f"#{pr.number}: malformed pending fixes dropped")
        return "dropped"
    try:
        _fix_lane(pr, findings, config, primary, st, report,
                  post_merge=post_merge, deadline=deadline)
    except ForgeError as exc:
        report.alerts.append(f"#{pr.number}: deferred fix forge error contained: {exc}")
    return "attempted"


def _poll_fix_merges(config: RepoConfig, primary: ForgeAdapter, report: CycleReport,
                     deadline: float | None = None) -> None:
    """Revisit owned fix PRs even when no source PR needs another review."""
    from . import executor

    try:
        # bot_identity REQUIRED (post-merge build): the merge gate re-verifies
        # authorship against it — a missing identity fails every merge closed.
        merged = executor.check_and_merge_own_prs(config, primary.bot_login, deadline=deadline)
        report.fix_prs_merged += len(merged)  # Sol audit: was int += list
    except Exception as exc:  # merge scan must not kill the cycle
        log.warning("merge scan failed for %s: %s", config.repo, exc)
        report.alerts.append("owned fix merge scan unavailable — will retry")


def _post_merge_sweep(
    config: RepoConfig,
    primary: ForgeAdapter,
    state_path: Path,
    get_diff: Callable[[PullRequest], tuple[set[str], str] | None],
    shadow_sink: ShadowSink | None,
    st: dict[str, Any],
    report: CycleReport,
    run_fixes: bool,
    deadline: float | None,
) -> set[int]:
    """Post-merge review mode (LEARNINGS #24): review PRs merged since the
    watermark — this org's PRs open and merge in ~60s, invisible to the
    open-PR poller. Findings land as post-merge comments; fixes ride follow-up
    PRs off the PR head (an ancestor of main once merged).

    Watermark law: it only advances past TERMINALLY-processed PRs (oldest
    first), so deferred ones (diff/model unavailable) are retried next cycle.
    At-most-once never depends on it: the head-SHA predicate + persistent-
    comment marker hold even on a full rewind. Returns the PR numbers this
    sweep considered — prune keeps their records one cycle so a rewind
    re-list hits the head-SHA guard, not a fresh model call."""
    from datetime import datetime, timedelta, timezone

    since = state.merged_watermark(st)
    if not since:
        since = (
            datetime.now(timezone.utc) - timedelta(hours=config.post_merge.initial_lookback_h)
        ).isoformat()
    try:
        merged_prs = primary.list_merged_prs(config.repo, since)
    except ForgeError as exc:
        report.alerts.append(f"post-merge listing failed (skipped this cycle): {exc}")
        log.warning("merged-PR listing failed for %s: %s", config.repo, exc)
        report._merged_listing_incomplete = True
        return set()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # UltraQA round 3: shape drift degrades the lane, never crashes
        report.alerts.append(f"post-merge listing degraded (skipped this cycle): {exc}")
        report._merged_listing_incomplete = True
        return set()
    if not isinstance(merged_prs, list):
        # MECE round-6 (luna-max F6-C012): a truthy non-list envelope
        # (dict/None) crashed the row filter below — degrade loudly
        report.alerts.append(
            f"post-merge listing wrong shape ({type(merged_prs).__name__}; skipped this cycle)")
        report._merged_listing_incomplete = True
        return set()
    _raw_rows = merged_prs
    merged_prs = [p for p in merged_prs if isinstance(p, PullRequest)]
    if len(merged_prs) != len(_raw_rows):
        report._merged_listing_incomplete = True
        first_bad = next(i for i, r in enumerate(_raw_rows) if not isinstance(r, PullRequest))
        report.alerts.append(
            f"post-merge: {len(_raw_rows) - len(merged_prs)} malformed merged rows "
            f"(first at position {first_bad}) — watermark will NOT advance past them")
        _row_gap = first_bad  # terminal may never cross the gap
    else:
        _row_gap = -1

    # The cap bounds MODEL WORK (reviews), not list position: PRs already
    # terminal at this SHA (reviewed while open, dependency-skipped) pass
    # through free — otherwise a free-to-skip PR could starve a same-second
    # sibling behind it (caught by the same-second cap-split regression).
    terminal = 0  # watermark position: PRs terminally processed, oldest first
    reviewed_budget = 0
    considered: set[int] = set()
    # MECE round-5 (sol F5-001): shadow runs keep their own dedupe belt and
    # never advance the live watermark — a shadow-reviewed merged PR must be
    # re-reviewed (posted) after the live cutover
    pm_shadow: dict[str, str] = {}
    if config.shadow:
        sb = st.get("pm_shadow_seen")
        if isinstance(sb, dict):
            pm_shadow = {str(k): v for k, v in sb.items() if isinstance(v, str)}
    for pr in merged_prs:
        considered.add(pr.number)
        _belt = pm_shadow.get(str(pr.number))
        if _belt == pr.head_sha or (_belt or "").startswith(pr.head_sha + ":dep"):
            # F13-C007: dependency skips are recorded as '<sha>:dep' — the
            # bare compare re-considered the same shadow dependency every cycle
            continue  # already shadow-reviewed at this SHA — belt, not terminal
        if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
            report.alerts.append("post-merge sweep deferred — cycle deadline reached")
            break
        bot_authored = bool(pr.is_bot_author)
        if bot_authored and fixlane.dependency_depth(pr, pr.title, config) in ("skip",):
            report.skipped_dependency += 1
            if config.shadow:
                # F11-C002 (reopened F5-201): shadow NEVER writes live
                # per-PR terminal state nor advances the live watermark —
                # the old dependency-skip branch did both, so a live cutover
                # could miss a shadow-processed PR entirely
                pm_shadow[str(pr.number)] = pr.head_sha + ":dep"
                continue
            state.mark_reviewed(st, pr.number, pr.head_sha, "dependency-skip")
            terminal += 1
            continue
        if not state.needs_review(st, pr.number, pr.head_sha):
            report.already_done += 1  # poll-invariant no-op: counted, never silent
            resume = _resume_fix_lane(pr, config, primary, st, report, run_fixes,
                                      deadline, post_merge=True)
            state.save_state(state_path, st)
            if st["prs"][str(pr.number)].get("pending_fixes"):
                if config.shadow:
                    # shadow touches no live belts — skip without mutation
                    continue
                if resume == "gated-off":
                    # PM 2026-09-16 (adversarial finding A1): a structurally
                    # off fix lane (Forgejo cutover, fixes disabled, shadow)
                    # used to wedge the merged sweep on this PR forever —
                    # silent, un-healing, blocking every later merge. The
                    # config opted out of fixes; the review itself is
                    # complete, so drop the stale record WITH an alert and
                    # let the sweep proceed.
                    st["prs"][str(pr.number)].pop("pending_fixes", None)
                    st["prs"][str(pr.number)].pop("pending_fix_sha", None)
                    state.save_state(state_path, st)
                    report.alerts.append(
                        f"#{pr.number}: stale pending fixes dropped — fix lane is off "
                        "(fixes disabled, shadow, or non-GitHub primary); review itself complete"
                    )
                    terminal += 1
                    continue
                # attempted-but-deferred (e.g. deadline): the record stays
                # for the next cycle's resume — but never silently
                report.alerts.append(
                    f"#{pr.number}: merged sweep stalled — pending fixes await resume"
                )
                if not config.shadow:  # A2 (2026-09-16): shadow never owns the live watermark
                    st.setdefault("merged_since", since)
                break
            terminal += 1  # already reviewed at this SHA (e.g. while open)
            continue
        if reviewed_budget >= config.post_merge.max_per_cycle:
            report.alerts.append(
                f"post-merge backlog: merged PRs pending beyond this cycle's cap "
                f"({config.post_merge.max_per_cycle}) — resuming next cycle"
            )
            break  # watermark stops BEFORE this unprocessed PR
        try:
            outcome = _review_pr(
                pr, config, primary, get_diff, shadow_sink, st, report, run_fixes,
                post_merge=True, deadline=deadline,
            )
        except ForgeError as exc:
            report.alerts.append(f"#{pr.number}: forge error contained: {exc}")
            break  # do not advance the watermark past unprocessed PRs
        state.save_state(state_path, st)  # checkpoint after each merged PR
        reviewed_budget += 1
        if outcome == "shadow":
            pm_shadow[str(pr.number)] = pr.head_sha
            report.postmerge_reviewed += 1
            continue  # NOT terminal — the live cutover re-reviews and posts
        if outcome == "fix-deferred":
            report.postmerge_reviewed += 1
            if not config.shadow:  # A2 (2026-09-16): shadow never owns the live watermark
                st.setdefault("merged_since", since)
            break
        if outcome in ("reviewed", "model-failed-cap"):
            if outcome == "reviewed":
                report.postmerge_reviewed += 1
            terminal += 1
        else:
            break  # deferred (diff/model): watermark stops before this PR

    if config.shadow and pm_shadow:
        if len(pm_shadow) > 200:
            # F11-C016: the shadow belt is bounded — a long shadow soak must
            # not grow state without limit (entries beyond the newest 200 are
            # stale merges the live cutover has long passed)
            pm_shadow = dict(list(pm_shadow.items())[-200:])
        st["pm_shadow_seen"] = pm_shadow
    elif not config.shadow and isinstance(st.get("pm_shadow_seen"), dict):
        st.pop("pm_shadow_seen", None)
    # Shadow uses pm_shadow_seen as its private dedupe belt. Advancing the live
    # cursor here can bury earlier shadow-only rows when later rows are already
    # terminal in live state: `terminal` counts those later rows, while the
    # prefix slice still contains the shadow rows. A shadow sweep therefore
    # never owns the live watermark.
    if terminal and not config.shadow:
        if _row_gap >= 0 and terminal > _row_gap:
            # MECE round-6 (luna-max F6-C013): never advance past a malformed
            # row — the sweep re-lists it next cycle and the alert persists
            terminal = _row_gap
        terminal_prs = [p for p in merged_prs[:terminal]]
        if terminal_prs:
            state.advance_merged_watermark(st, terminal_prs[-1].merged_at)
    return considered


# CI-watch conclusions that mean RED (everything not in the benign set).

def run_cycle(
    config: RepoConfig,
    state_path: Path,
    get_diff: Callable[[PullRequest], tuple[set[str], str] | None],
    trigger_reason: str = "cron",
    shadow_sink: ShadowSink | None = None,
    run_issues: bool = False,
    run_fixes: bool = False,
    deadline: float | None = None,
) -> CycleReport:
    """One poll-invariant cycle. `reason` is annotation only — the predicate
    in state.needs_review decides; nothing bypasses it. `get_diff` is
    REQUIRED and may return None on fetch failure (skip, don't fake)."""
    report = CycleReport(repo=config.repo, shadow_only=config.shadow)
    primary_name = next(k for k, b in config.forges.items() if b.role == "primary")
    primary: ForgeAdapter = adapter_for(config.forges[primary_name])
    primary.bot_login = config.bot_login
    mirrors = [adapter_for(b) for b in config.forges.values() if b.role == "mirror"]
    for m in mirrors:
        m.bot_login = config.bot_login

    try:
        with CycleLock(state_path.with_suffix(".lock")):
            st = state.load_state(state_path)
            mirror_shas = _mirror_shas(mirrors, config.repo, report)

            listing_failed = False
            try:
                prs = primary.list_open_prs(config.repo)
            except ForgeError as exc:
                listing_failed = True
                report.alerts.append(f"primary unreachable: {exc}")
                log.warning("primary unreachable for %s: %s", config.repo, exc)
                prs = []
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                # UltraQA round 2: a forge API SHAPE change (rows missing
                # fields, unexpected types) is an external-surface failure,
                # not a bug in the cycle — degrade this lane, never crash it.
                listing_failed = True
                report.alerts.append(f"primary list_open_prs degraded: {exc}")
                log.warning("list_open_prs shape failure for %s (degraded): %s", config.repo, exc)
                prs = []

            # F9-C002: the listing ENVELOPE must be a list — a truthy dict
            # iterates its keys as 'rows' and used to pass the row guard while
            # listing_failed stayed False (prune then deleted live state)
            if not isinstance(prs, list):
                listing_failed = True
                report.alerts.append(
                    f"primary list_open_prs wrong envelope ({type(prs).__name__}; "
                    "degraded this cycle)")
                prs = []

            # Adapter contract guard: garbage rows (None, dicts, strings from a
            # half-parsed API) are dropped with an alert — one bad row must not
            # crash the cycle (UltraQA round 2, ENG-garbage-rows). F11-C001:
            # a listing with dropped rows is INCOMPLETE — it must never
            # authorize destructive pruning or an open_ids replacement (the
            # drop used to leave listing_failed False and delete reviewed-PR
            # memory)
            if not all(isinstance(p, PullRequest) for p in prs):
                dropped_rows = sum(1 for p in prs if not isinstance(p, PullRequest))
                report.alerts.append(f"primary returned {dropped_rows} malformed PR rows (dropped)")
                log.warning("dropped %d malformed PR rows for %s", dropped_rows, config.repo)
                prs = [p for p in prs if isinstance(p, PullRequest)]
                listing_failed = True

            open_numbers = set()
            truncated_by_deadline = False
            for pr in prs:
                open_numbers.add(pr.number)
                report.scanned += 1
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    truncated_by_deadline = True
                    report.alerts.append("cycle deadline reached — remaining PRs deferred to next cycle")
                    break
                mirror_seen = pr.head_sha in mirror_shas
                if mirror_seen and trigger_reason == "mirror":
                    report.mirror_dedup += 1  # SHA-dedup skip: counted, never silent
                    continue  # mirrored PRs are never reviewed twice
                bot_authored = bool(pr.is_bot_author)
                if bot_authored and fixlane.dependency_depth(pr, pr.title, config) in ("skip",):
                    report.skipped_dependency += 1
                    if config.shadow:
                        # F14-C001 (reopened F5-201): shadow never writes the
                        # live reviewed marker — a later LIVE run must still
                        # see this PR as unreviewed
                        continue
                    state.mark_reviewed(st, pr.number, pr.head_sha, "dependency-skip")
                    continue
                if not state.needs_review(st, pr.number, pr.head_sha):
                    report.already_done += 1  # poll-invariant no-op: counted, never silent
                    resume = _resume_fix_lane(pr, config, primary, st, report, run_fixes, deadline)
                    if resume == "gated-off":
                        # Round-3 hunt: the twin post-merge path drops stale
                        # records with an alert; the open path wedged silently
                        # every cycle. Same drop, same alert, same rationale
                        # (config opted out; the review itself is complete).
                        rec = st.get("prs", {}).get(str(pr.number), {})
                        if rec.pop("pending_fixes", None) is not None:
                            rec.pop("pending_fix_sha", None)
                            report.alerts.append(
                                f"#{pr.number}: stale pending fixes dropped — fix lane is off "
                                "(fixes disabled, shadow, or non-GitHub primary); review itself complete"
                            )
                    if resume == "attempted":
                        # Zero-findings review: mirror the post-merge stall
                        # alert — attempted-but-deferred fixes here were
                        # equally silent while the watermark moved on.
                        pending = st.get("prs", {}).get(str(pr.number), {}).get("pending_fixes") or []
                        if pending:
                            report.alerts.append(
                                f"#{pr.number}: open-PR sweep stalled — "
                                f"{len(pending)} pending fixes await resume"
                            )
                    state.save_state(state_path, st)
                    continue
                try:
                    _review_pr(pr, config, primary, get_diff, shadow_sink, st, report, run_fixes,
                               deadline=deadline)
                except ForgeError as exc:
                    # Per-PR containment (audit C7): a throttled/broken call
                    # must not abort the cycle or lose already-reviewed state.
                    report.alerts.append(f"#{pr.number}: forge error contained: {exc}")
                    log.warning("forge error on %s#%s (contained): %s", config.repo, pr.number, exc)
                state.save_state(state_path, st)  # checkpoint after each PR

            merged_keep: set[int] = set()
            merged_lane_failed = False  # F13-C002: prune barrier
            # F11-C011 (reopened F6-C016/C017): every post-review lane checks
            # the remaining budget BEFORE starting — the deadline used to stop
            # review work but the issues/metrics lanes still ran model/forge
            # calls past expiry, defeating the inner budget (outer kill risk)
            if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                report.alerts.append("cycle deadline reached — post-review lanes deferred to next cycle")
                state.save_state(state_path, st)
                return report
            if config.post_merge.enabled:
                # BEFORE prune_closed: the sweep's head-SHA guard must see this
                # cycle's merged records before prune could drop them.
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — post-merge deferred")
                    report._merged_listing_incomplete = True
                else:
                    try:
                        merged_keep = _post_merge_sweep(
                            config, primary, state_path, get_diff, shadow_sink, st,
                            report, run_fixes, deadline,
                        )
                    except ForgeError as exc:
                        # F13-C002: a failed merged listing must not authorize
                        # destructive pruning (empty keep set = closed!)
                        report.alerts.append(f"post-merge degraded ({exc}) — prune barred")
                        merged_lane_failed = True

            if config.ci_watch.enabled:
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — ci_watch deferred")
                else:
                    _ci_watch_step(config, primary, st, report, run_fixes, deadline)

            if config.retro_audit.enabled:
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — retro deferred")
                    report._merged_listing_incomplete = True
                else:
                    try:
                        merged_keep |= _retro_sweep(
                            config, primary, state_path, get_diff, shadow_sink, st,
                            report, deadline,
                        )
                    except ForgeError as exc:
                        report.alerts.append(f"retro degraded ({exc}) — prune barred")
                        merged_lane_failed = True

            if config.omnisweep.enabled:
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — omnisweep deferred")
                else:
                    _omnisweep_step(
                        config, primary, state_path, shadow_sink, st, report, deadline,
                    )

            # MECE round-4 (luna F4-002): never prune on a listing FAILURE or
            # deadline truncation — empty open_numbers would delete every
            # per-PR record as if the PRs had closed (re-review storms + lost
            # fix-depth/model-failure state on the next healthy cycle)
            # MECE round-6 (luna-max F6-C003): never prune under shadow either
            # — a dry-run must not delete live records
            if (not listing_failed and not truncated_by_deadline
                    and not config.shadow and not merged_lane_failed
                    and not report._merged_listing_incomplete):
                st["open_ids"] = sorted(open_numbers)  # F8-C002: for tiers
                state.prune_closed(st, open_numbers | merged_keep)

            if run_issues and config.issues_enabled:
                from . import issues as issues_lane

                # F12-C004 (reopened F11-C011): re-check the budget BEFORE the
                # lane — earlier lanes may have consumed it (model/forge calls).
                # F13-C001: the summary must exist even when the lane is
                # deferred (UnboundLocalError used to abort the whole cycle)
                issue_summary: dict = {}
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — issues lane deferred")
                else:
                    issue_summary = issues_lane.run_issues_cycle(
                        config, st, primary, deadline=deadline)
                report.issues_triaged = issue_summary.get("triaged", 0)
                # F10-B003: a foreign-marker quarantine is a STRUCTURED cycle
                # alert — the lane claims per-cycle visibility; the claim must
                # land where operators actually look
                qn = issue_summary.get("quarantined", 0)
                if qn:
                    report.alerts.append(
                        f"issues lane: {qn} foreign triage marker(s) quarantined "
                        "(no duplicate posted, watermark held) — remove the "
                        "marker or triage the issue manually")

            if not config.shadow:
                from . import metrics

                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — acceptance metrics skipped")
                else:
                    report.acceptance = metrics.acceptance_snapshot(primary, config)

            if (run_fixes and primary.name == "github"
                    and config.fix.enabled and config.fix.merge_own_prs
                    and not config.shadow):
                if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
                    report.alerts.append("cycle deadline reached — owned fix merges deferred")
                else:
                    _poll_fix_merges(config, primary, report, deadline=deadline)

            if report.fix_failures:
                # ONE summarizing alert per cycle (V2) — alert fatigue is a
                # named org incident class; never one line per finding
                report.alerts.append(
                    f"fix failures: {report.fix_failures} — " + "; ".join(getattr(report, "_fix_failure_notes", [])[:5])
                )
            state.save_state(state_path, st)
    except CycleLockHeld as exc:
        log.warning("cycle lock held — skipping this cycle (never double-post): %s", exc)
        report.alerts.append(f"LOCK HELD: {exc}")
        report.scanned = 0
    except state.StateIOError as exc:
        report.alerts.append(str(exc))
    return report
