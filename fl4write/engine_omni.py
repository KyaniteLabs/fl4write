"""engine_omni: the sweep steps behind run_cycle.

The omni/retro/CI-watch sweep family (L6 backlog: split engine.py):
omni readiness, reporting, fix phases, completed-actions and reset;
probe-head and issue upsert; retro sweep and its review; CI watch.
engine.run_cycle orchestrates these steps; this module holds the bodies.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import renderer, scrub, state
from .analyzer import MAX_FILE_BYTES
from .config import RepoConfig
from .forges import ForgeAdapter, ForgeError
from .models import CycleReport, Finding, PullRequest

log = logging.getLogger(__name__)

ShadowSink = Callable[[str, int, str], None]

# CI-watch conclusions that mean RED (everything not in the benign set).
_CI_BENIGN = {"success", "skipped", "neutral", "canceled", "cancelled"}
REVIEW_BUDGET_S = 90  # worst-case model time for one review (2 routes x ~45s + slack)


def _prior_findings(prev_body: str) -> list[Finding]:
    """Reconstruct prior findings from our own comment via the renderer's
    parse contract (single source of truth with the emitter)."""
    return [
        Finding(rule_id=rule, severity=sev, path=path, line=line, category="prior", message="")
        for sev, path, line, rule in renderer.parse_finding_lines(prev_body)
    ]



def _fix_freshness_gate(primary: ForgeAdapter, config: RepoConfig, finding,
                        ref: str | None = None) -> bool:
    """True = fresh enough to attempt a fix. The #503 class: a finding on a
    file deleted/moved after review made every attempt fail at fetch,
    invisibly. False = skip the fix (finding stays posted); None-probe =
    fail-open (proceed)."""
    exists = (primary.path_exists(config.repo, finding.path, ref=ref)
              if ref is not None else primary.path_exists(config.repo, finding.path))
    if exists is False:
        log.info("fix freshness gate: %s gone from HEAD — skipping fix", finding.path)
        return False
    return True

# Omnisweep bounds (consensus-gated): files above this are skipped (the
# contents API refuses >1MB; anything near it is generated/vendored).
_OMNI_MAX_FILE_BYTES = MAX_FILE_BYTES
_OMNI_FIX_ATTEMPTS_PER_CYCLE = 3



def _omni_readiness(findings: list[dict]) -> tuple[int, str]:
    """CheckYourself-style readiness score from the sweep's findings.
    MECE round-1 (terra F1-11): the missing-evidence cap was INERT — no caller
    ever passed categories_checked. Now derived from each finding's rule
    category, so a sweep that never checked e.g. Auth & Access is capped."""
    from .capabilities import CAPABILITIES, readiness_score, score_label
    from collections import Counter

    sev = Counter(f.get("sev", f.get("severity", "Nit")) for f in findings)
    rule_to_cat = {rid: cat for rid, cat, _, _ in CAPABILITIES}
    # persisted omni findings carry the field "rule" (round-1 wiring bug:
    # helper read rule_id and no caller existed — terra F2-007)
    cats = {rule_to_cat.get(f.get("rule_id") or f.get("rule", ""), "")
            for f in findings if rule_to_cat.get(f.get("rule_id") or f.get("rule", ""))}
    if not findings:
        # F11-A7 (round 11, luna-max DOM-A, reopened F9-A08): this helper
        # runs on COMPLETE publishes only — a clean full-tree sweep audited
        # EVERY category. Deriving coverage from findings made a zero-finding
        # audit score 80 (MEDIUM) as if nothing had been checked.
        from .capabilities import SCORING_CATEGORIES
        cats = set(SCORING_CATEGORIES)
    score = readiness_score(dict(sev), categories_checked=cats)
    return score, score_label(score)


def _omni_report_body(config: RepoConfig, findings: list[dict], scanned: int, total: int, complete: bool) -> str:
    """The audit-issue body: severity table + top-N findings + progress.
    Capped — the rest live in state (GitHub issue bodies cap at 64KB)."""
    cap = config.omnisweep.max_findings_in_issue
    sev_order = {s: i for i, s in enumerate(config.severity_vocab)}
    ordered = sorted(findings, key=lambda f: sev_order.get(f["sev"], 99))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["sev"]] = counts.get(f["sev"], 0) + 1
    table = "| Severity | Count |\n|---|---|\n" + "\n".join(
        f"| {s} | {counts.get(s, 0)} |" for s in config.severity_vocab
    )
    lines = [
        "## 🔍 FL4WRITE omnisweep — full-tree audit" + (" (COMPLETE)" if complete else ""),
        "",
        table,
        "",
        f"Progress: **{scanned}/{total}** files scanned at HEAD." if total else "",
        "",
    ]
    for f in ordered[:cap]:
        lines.append(f"### {f['sev']} — `{f['path']}:{f['line']}` — `{f['rule']}`\n{f['msg']}\n")
    if len(ordered) > cap:
        lines.append(f"… and {len(ordered) - cap} more findings recorded in sweep state.\n")
    if complete:
        if not findings:
            lines.append("No findings were recorded in this completed sweep.\n")
        score, label = _omni_readiness(findings)
        lines.append(f"**Readiness: {score}/100 — {label}**\n")
        lines.append("_This finding-based score is subject to missing-evidence caps; "
                     "it is not an exhaustive certification._\n")
        lines.append("_Sweep complete. Findings above are at current HEAD; the fix lane (if enabled for this repo) will open PRs for qualifying severities._")
    return "\n".join(lines)


def _omni_fix_phase(
    config: RepoConfig, primary: ForgeAdapter, st: dict[str, Any], report: CycleReport,
    deadline: float | None = None,
) -> None:
    """Post-completion fix drain. SEPARATELY gated (omnisweep.fix) — cold-code
    findings lack the reviewed-diff premise the fix lane was built on.
    Rails asserted HERE, not only in config: severity floor, GitHub-only,
    stable hash-salted branch numbers (blake2b(head:id) — no positional
    arithmetic, no wrap, ever)."""
    import hashlib

    from . import executor

    if config.omnisweep.fix_min_severity not in config.severity_vocab:  # rail, in code
        report.alerts.append("omnisweep.fix_min_severity invalid — fix phase skipped")
        return
    if primary.name != "github":
        report.alerts.append("omnisweep fix phase: GitHub-only in v1 — findings stay in the audit issue")
        return
    sev_floor = config.severity_vocab.index(config.omnisweep.fix_min_severity)
    head = st.get("omni_head", "")
    if not head:
        return
    attempts = 0
    for f in st.get("omni_findings", []):
        if attempts >= _OMNI_FIX_ATTEMPTS_PER_CYCLE:
            break
        if deadline is not None and time.monotonic() >= deadline:
            # Cycle-3: each fix runs model+clone+push past a possible kill —
            # stop draining like every other lane does.
            report.alerts.append("omnisweep fix drain deferred — cycle deadline reached")
            break
        sev = f.get("sev")
        if sev not in config.severity_vocab:
            continue  # MECE round-3 (glm F3-2): vocab-drifted persisted rows
        if f.get("fix_attempted") or f.get("fix_stale") or config.severity_vocab.index(sev) > sev_floor:
            continue
        from .models import Finding as _Finding

        if not _fix_freshness_gate(primary, config, _Finding(
                rule_id=f["rule"], severity=f["sev"], path=f["path"], line=f["line"],
                category="omnisweep", message=f["msg"])):
            f["fix_stale"] = True  # short-circuits eligibility forever (V3)
            continue
        f["fix_attempted"] = True
        attempts += 1
        report.fix_attempts += 1
        synth = PullRequest(
            forge=primary.name,
            number=int(hashlib.blake2b(f"{head}:{f['id']}".encode(), digest_size=8).hexdigest()[:12], 16),
            repo=config.repo,
            title=f"omnisweep fix: {f['path']}:{f['line']}",
            head_sha=head,
        )
        from .models import Finding as _F

        finding = _F(
            rule_id=f["rule"], severity=f["sev"], path=f["path"], line=f["line"],
            category="omnisweep", message=f["msg"],
        )
        # Cycle-3: one-shot synth context — no tracked depth exists here;
        # bounded instead by fix_attempted flags + the per-cycle cap.
        result = executor.attempt_fix(synth, finding, config, depth_used=0)
        status = result.get("status")
        if status == "error":
            # MECE round-6 (M3 stream candidate, desk-verified): a transient
            # error (network/model) marked the finding attempted BEFORE the
            # call, so it was never retried on a later cycle — un-mark it:
            # terminal outcomes (pr_opened/testfail/nofix) stay attempted,
            # transient errors retry (per-cycle cap still bounds the burn)
            f["fix_attempted"] = False
        if status == "pr_opened":
            report.fix_prs_opened += 1
        elif status in ("error", "testfail", "nofix"):
            report.fix_failures += 1
            report._fix_failure_notes.append(f"omni {f['path']}:{f['line']} {status}: {str(result.get('reason'))[:60]}")
        else:
            report.fix_failures += 1
            report._fix_failure_notes.append(f"omni {f['path']}:{f['line']} unknown-status {status!r}")


def _omni_completed_actions(config, primary, st, report, *, allow_fixes=True,
                              deadline: float | None = None):
    """Retry a completed report before draining fixes; False means retry pending."""
    if (not st.get("omni_published") or st.get("omni_report_version") != 2
            or (st.get("omni_issue") and not st.get("omni_findings")
                and not st.get("omni_clean_published"))):
        findings = st.get("omni_findings", [])
        total = int(st.get("omni_total", 0) or 0)
        if findings or st.get("omni_issue"):
            _omni_upsert_issue(config, primary, st, findings, total, total,
                               complete=True, report=report)
            if not st.get("omni_published") or st.get("omni_report_version") != 2:
                return False
        else:
            st["omni_published"] = True
            st["omni_report_version"] = 2
    if allow_fixes and config.omnisweep.fix and st.get("omni_findings"):
        _omni_fix_phase(config, primary, st, report, deadline=deadline)
    return True


def _omnisweep_step(
    config: RepoConfig,
    primary: ForgeAdapter,
    state_path: Path,
    shadow_sink: ShadowSink | None,
    st: dict[str, Any],
    report: CycleReport,
    deadline: float | None,
) -> None:
    """Omnisweep: full-tree scan at HEAD, one file per model call, findings
    compacted into state and surfaced as ONE audit issue edited in place.
    Cursor-resumable, capped per cycle, terminal on exhaustion; the total-
    files cap aborts the sweep loudly. Fix phase runs only after completion
    and only through the separately-gated _omni_fix_phase."""
    import fnmatch

    from . import gatekeeper
    from .analyzer import ModelUnavailable, analyze

    if config.shadow:
        # MECE round-6 (sol F6-E01): shadow is a dry run — omnisweep under
        # shadow touches NO live state (cursor/findings/completion belong to
        # the live sweep; the cutover scans from scratch and publishes).
        # Zero model spend: shadow omnisweep has no consumer.
        log.info("omnisweep: shadow mode — sweep skipped (dry-run law)")
        return
    if st.get("omni_complete"):
        report.omni_findings = len(st.get("omni_findings", []))  # F5-011
        # F10-C002: a COMPLETED sweep is only complete for the HEAD it
        # audited — if the forge reports a new head, restart from scratch
        if st.get("omni_head"):
            _cur = _probe_head(primary, config.repo)
            if _cur is not None and _cur != st["omni_head"]:
                # F11-C007: atomic reset — stale failure/quarantine counts
                # must not follow the sweep onto the new HEAD
                _omni_reset_sweep(st)
                st["omni_head"] = _cur
                report.alerts.append(
                    "omnisweep: HEAD changed after completion — re-auditing from scratch")
        if st.get("omni_complete") and not st.get("omni_fp"):
            # Legacy reports may need their format/publication retry, but an
            # unknown old scope cannot be blessed with today's fingerprint.
            if not _omni_completed_actions(config, primary, st, report, allow_fixes=False,
                                            deadline=deadline):
                return
            st["omni_complete"] = False
            st["omni_scope_pending"] = True
            report.alerts.append("omnisweep: completed scope unknown — fresh audit scheduled")
            return  # preserve the publication receipt and prior report this cycle

    tree_getter = getattr(primary, "list_tree_files", None)
    tree = tree_getter(config.repo) if callable(tree_getter) else None
    if tree is None:
        report.alerts.append("omnisweep: tree listing unqueryable (skipped this cycle)")
        return
    if not (isinstance(tree, (tuple, list)) and len(tree) == 2):
        # UltraQA round 3: wrong-shape tree response degrades, never crashes
        report.alerts.append(f"omnisweep: tree listing wrong shape ({type(tree).__name__}, skipped)")
        return
    files, truncated = tree
    if not isinstance(files, list):
        report.alerts.append("omnisweep: tree files not a list (skipped this cycle)")
        return
    if truncated:
        # MECE round-5 (sol F5-007): a known-truncated listing must NEVER
        # render COMPLETE — completion is a correctness claim about the whole
        # tree, and the alert alone used to be followed by omni_complete=True.
        # F11-C004: truncated-snapshot progress is UNTRUSTED — it advances the
        # cursor with no fingerprint; when a complete listing later returns,
        # every path that was newly visible before the cursor would be skipped
        # and the sweep falsely certified. Flag it; progress is discarded on
        # the first complete listing below.
        st["omni_truncated_seen"] = True
        report.alerts.append(
            "omnisweep: tree listing TRUNCATED by the forge — completion BLOCKED; "
            "files past the truncation are unaudited (widen caps/excludes or split the repo)")
    elif st.pop("omni_truncated_seen", None) and (
            st.get("omni_cursor") or st.get("omni_complete")
            or st.get("omni_findings") or st.get("omni_fp")):
        _omni_reset_sweep(st)
        report.alerts.append(
            "omnisweep: complete listing after truncated snapshots — "
            "truncated progress discarded, re-auditing from scratch")
    # row-shape guard: (path, size) pairs only; one garbage row must not
    # abort. F10-C003: malformed rows mean the listing is NOT a trustworthy
    # whole tree — completion is blocked while rows are bad
    rows_bad = False
    if len(files) != sum(1 for row in files
                         if isinstance(row, (tuple, list)) and len(row) == 2
                         and isinstance(row[0], str)
                         and isinstance(row[1], int)
                         and not isinstance(row[1], bool)  # F14-C005
                         and row[1] >= 0):
        rows_bad = True
        report.alerts.append(
            "omnisweep: malformed tree rows dropped \u2014 completion BLOCKED "
            "(retry next cycle)")
    files = [row for row in files
             if isinstance(row, (tuple, list)) and len(row) == 2
             and isinstance(row[0], str)
             and isinstance(row[1], int)
             and not isinstance(row[1], bool)
             and row[1] >= 0]

    excludes = config.omnisweep.exclude + (config.path_filters or {}).get("ignore", [])
    scan = sorted(
        p for (p, size) in files
        if 0 < size <= _OMNI_MAX_FILE_BYTES
        and not any(fnmatch.fnmatch(p, pat) for pat in excludes)
    )
    total = len(scan)
    if st.get("omni_scope_pending"):
        if truncated or rows_bad:
            return
        _omni_reset_sweep(st)
        report.alerts.append("omnisweep: legacy scope unknown — re-auditing from scratch")
    scanned_total = int(st.get("omni_scanned_total", 0))
    if st.get("omni_complete") and (truncated or rows_bad):
        return  # no publication or fixes before verifying the completed scope
    if total > config.omnisweep.max_total_files:
        if st.get("omni_complete"):
            _omni_reset_sweep(st)
        # MECE round-2 (terra F2-001): the cap bounds the TREE (one-shot);
        # the old scanned_total+total check double-counted the tree every
        # cycle and aborted large-but-legal sweeps mid-flight
        report.alerts.append(
            f"omnisweep ABORTED: tree has {total} files exceeds "
            f"max_total_files={config.omnisweep.max_total_files} — widen the cap or narrow excludes"
        )
        # MECE round-5 (sol F5-007): an abort is NOT a completion — no
        # omni_complete, nothing publishable; the sweep re-lists and re-alerts
        # each cycle until the cap/excludes are fixed
        st["omni_total"] = total
        report.omni_findings = len(st.get("omni_findings", []))
        return

    # MECE round-6 (luna-max F6-C009): the path cursor resumes mid-tree — a
    # file ADDED before the cursor was permanently skipped. Fingerprint the
    # scan list; when the tree changes mid-sweep, restart from scratch (the
    # alert is the audit trail; completed findings belong to an older HEAD)
    _anchor0 = _probe_head(primary, config.repo)
    if _anchor0 is None and not config.shadow and not truncated:
        # F13-C008/F14-C004: with NO trusted HEAD anchor a sweep cannot
        # detect same-size content edits NOR persist a defensible completion
        # (a fresh scan used to complete with no SHA, and the completed fast
        # path then never probed) — defer until an anchor exists
        report.alerts.append(
            "omnisweep: no trusted HEAD anchor — sweep deferred (unanchored "
            "certification refused)")
        return
    if not config.shadow and not truncated:
        import hashlib as _hl
        # F9-C003: content edits with unchanged paths must restart the sweep
        # too — fingerprint paths AND sizes, plus the anchored HEAD when the
        # forge can report one (a stale 'at HEAD' audit is a false claim)
        # F13-C003: fingerprint the EFFECTIVE scan — raw-tree rows let an
        # exclusion change (removing an exclude before the cursor) leave files
        # unscanned while the sweep still certified complete
        _eff = [(pp, sz) for (pp, sz) in files
                if 0 < sz <= _OMNI_MAX_FILE_BYTES
                and not any(fnmatch.fnmatch(pp, pat) for pat in excludes)]
        fp_meta = "\x00".join(f"{pp}:{sz}" for (pp, sz) in sorted(_eff))
        fp_meta += "\x00excl:" + "\x00".join(sorted(excludes))
        _anchor = _probe_head(primary, config.repo)
        if _anchor:
            fp_meta += "\x00head:" + _anchor
        fp = _hl.sha256(fp_meta.encode()).hexdigest()[:16]
        if (st.get("omni_fp") and fp != st["omni_fp"]
                and (st.get("omni_cursor", "") or st.get("omni_complete"))):
            # F11-C007: atomic reset — stale failure/quarantine counts and
            # totals must not ride the restart onto the new tree
            _omni_reset_sweep(st)
            scanned_total = 0
            report.alerts.append("omnisweep: tree CHANGED mid-sweep — restarting from scratch")
        st["omni_fp"] = fp
    if st.get("omni_complete"):
        _omni_completed_actions(config, primary, st, report, deadline=deadline)
        return
    cursor = st.get("omni_cursor", "")
    pending = [p for p in scan if p > cursor][: config.omnisweep.max_files_per_cycle]
    if not pending:
        # MECE round-5 (sol F5-001/007): shadow runs and truncated listings
        # must NOT set live completion — completion is a live-publishable,
        # whole-tree claim. F10-C003: malformed rows block it too
        if config.shadow or truncated or rows_bad:
            return
        st["omni_complete"] = True
        st["omni_total"] = total
        findings = st.get("omni_findings", [])
        report.omni_scanned = len(scan)
        report.omni_findings = len(findings)
        if findings or st.get("omni_issue"):
            _omni_upsert_issue(config, primary, st, findings, len(scan), len(scan),
                               complete=True, report=report)
            if not st.get("omni_published"):
                report.alerts.append("omnisweep: final audit-issue publication failed — retrying next cycle")
                return  # retry via the complete fast path before anything else
        else:
            # MECE round-6 (luna-max F6-C018): a CLEAN sweep publishes
            # nothing — that is success, never a publication failure
            st["omni_published"] = True
            st["omni_report_version"] = 2
        report.alerts.append(f"omnisweep complete: {len(findings)} findings across {total} files")
        if config.omnisweep.fix and findings:
            _omni_fix_phase(config, primary, st, report, deadline=deadline)
        return

    # anchor for the fix phase: HEAD moves mid-sweep → findings from an older
    # HEAD are re-anchored by the freshness of the file fetch at fix time.
    if not st.get("omni_head"):
        _sha = _probe_head(primary, config.repo)
        if _sha:
            st["omni_head"] = _sha

    scanned_this_cycle = 0
    for path in pending:
        if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
            report.alerts.append("omnisweep deferred — cycle deadline reached")
            break
        try:
            content = primary.get_file(config.repo, path, st.get("omni_head", "") or "HEAD")
        except (ForgeError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # UltraQA round 3: per-file fetch failure skips the file, never
            # aborts the sweep. F11-C006 (reopened F6-C008): the EXCEPTION
            # path used to advance the cursor WITHOUT the quarantine ledger —
            # the next cycle then certified the sweep complete with the file
            # never audited. Record it exactly like the None-content path.
            report.alerts.append(f"omnisweep: file fetch failed for {path}: {exc}")
            st["omni_cursor"] = path  # skip, not retried forever
            st.setdefault("omni_unfetchable", [])
            if path not in st["omni_unfetchable"]:
                st["omni_unfetchable"].append(path)
            continue
        if content is None:
            # MECE round-6 (luna-max F6-C008): unfetchable files are recorded
            # as QUARANTINED (not silently treated as audited) — completion
            # alerts carry the count so 'COMPLETE' never hides missing files
            st["omni_cursor"] = path  # skip, not retried forever
            st.setdefault("omni_unfetchable", [])
            if path not in st["omni_unfetchable"]:
                st["omni_unfetchable"].append(path)
            report.alerts.append(f"omnisweep: {path} unfetchable — quarantined")
            continue
        synth = PullRequest(
            forge=primary.name, number=0, repo=config.repo,
            title=f"omnisweep: {path}", head_sha=st.get("omni_head", "") or "HEAD",
        )
        try:
            doc = analyze(synth, {path}, content, config, mode="file")
            st.setdefault("omni_file_fails", {}).pop(path, None)
        except ModelUnavailable as exc:
            report.model_unavailable += 1
            fails = int(st.setdefault("omni_file_fails", {}).get(path, 0)) + 1
            st["omni_file_fails"][path] = fails
            # A linear tree walk must never LIVeLOCK on one file (live-caught:
            # a file whose review exceeds max_tokens deferred forever, stalling
            # everything behind it). One retry, then skip-and-record.
            if fails >= 2:
                st.setdefault("omni_unscannable", []).append(path)
                st["omni_cursor"] = path
                log.warning("omnisweep: %s unscannable after %d attempts — skipped, recorded", path, fails)
                continue
            log.warning("omnisweep model unavailable at %s (attempt %d): %s", path, fails, exc)
            break  # one retry next cycle, cursor holds
        findings = doc.findings
        if findings and config.gatekeeper:
            findings, dropped, failed_open = gatekeeper.filter_findings(findings, config)
            report.gatekeeper_dropped += dropped
            if failed_open:
                report.gatekeeper_failed += 1
        next_id = int(st.get("omni_next_id", 1))
        for f in findings:
            st.setdefault("omni_findings", []).append({
                "id": next_id, "path": f.path, "line": f.line, "rule": f.rule_id,
                "sev": f.severity, "msg": f.message[:200],  # compact: no proposals in state
                "via": config.model.model,  # route attribution: per-model quality becomes measurable
            })
            next_id += 1
        st["omni_next_id"] = next_id
        st["omni_cursor"] = path
        scanned_this_cycle += 1
        state.save_state(state_path, st)  # checkpoint after each file
    st["omni_scanned_total"] = scanned_total + scanned_this_cycle
    report.omni_scanned = scanned_this_cycle
    report.omni_findings = len(st.get("omni_findings", []))
    done = st.get("omni_cursor", "") >= scan[-1] if scan else True
    # F10-C003: malformed rows make the listing an untrustworthy whole-tree
    # claim — the same-cycle finalize must honor the same rows_bad gate as
    # the empty-pending block above (round-10 pin caught the leak)
    if done and scanned_this_cycle and not truncated and not rows_bad and not config.shadow:
        # finalized in the SAME cycle as the last file — no idle hourly hop.
        # MECE round-5 (sol F5-007/F5-001): a truncated tree or a shadow run
        # never finalizes — completion is a whole-tree, publishable claim
        st["omni_complete"] = True
        st["omni_total"] = total
        unscannable = len(st.get("omni_unscannable", []))
        quarantined = len(st.get("omni_unfetchable", []))
        report.alerts.append(
            f"omnisweep complete: {report.omni_findings} findings across {total} files"
            + (f" ({unscannable} unscannable — model failed twice, skipped + recorded)" if unscannable else "")
            + (f" ({quarantined} unfetchable — recorded, not audited)" if quarantined else "")
        )
        if st.get("omni_findings") or st.get("omni_issue"):
            _omni_upsert_issue(
                config, primary, st, st.get("omni_findings", []),
                total, total, complete=True, report=report,
            )
            if not st.get("omni_published"):
                # MECE round-5 (sol F5-002): the completion is recorded but the
                # final publication failed — the next cycle's complete fast
                # path retries the upsert before doing anything else
                report.alerts.append("omnisweep: final audit-issue publication failed — retrying next cycle")
                return
        else:
            # MECE round-6 (luna-max F6-C018): clean sweeps publish nothing —
            # that IS success, never a publication failure
            st["omni_published"] = True
            st["omni_report_version"] = 2
        if config.omnisweep.fix and st.get("omni_findings"):
            _omni_fix_phase(config, primary, st, report, deadline=deadline)
    else:
        if config.shadow:
            return  # shadow sweeps never touch live progress belts
        if truncated and done and not scanned_this_cycle:
            return  # truncated completion-blocked: retry next cycle, no update
        _omni_upsert_issue(
            config, primary, st, st.get("omni_findings", []),
            len([p for p in scan if p <= st.get("omni_cursor", "")]), total, complete=False, report=report,
        )






def _omni_reset_sweep(st: dict) -> None:
    """F11-C007: ONE atomic reset for every omni restart class (HEAD change,
    tree change mid-sweep, truncated-snapshot comeback). Every HEAD-scoped
    field goes — the old partial resets left stale failure/quarantine counts
    that prematurely quarantined new content, and stale totals contaminated
    the completion report. omni_issue survives: the next audit reuses it."""
    for key in ("omni_complete", "omni_published", "omni_clean_published", "omni_cursor", "omni_head",
                "omni_fp", "omni_findings", "omni_next_id", "omni_total",
                "omni_scanned_total", "omni_file_fails", "omni_unscannable",
                "omni_unfetchable", "omni_truncated_seen", "omni_scope_pending"):
        st.pop(key, None)


def _probe_head(primary: ForgeAdapter, repo: str) -> str | None:
    """F11-C015: ONE validated HEAD probe for omnisweep — returns a full SHA
    string or None. Every raw adapter probe used to re-validate shape and
    index anchor[0] itself; a truthy wrong-shape envelope escaped and aborted
    the whole cycle with an uncaught KeyError/TypeError."""
    import re as _re_sha
    try:
        _h = primary.head_check_runs(repo)
    except Exception:  # noqa: BLE001 - probing never blocks a sweep
        _h = None
    if isinstance(_h, (tuple, list)) and len(_h) == 2 \
            and isinstance(_h[0], str) and _re_sha.fullmatch(r"[0-9a-f]{40}", _h[0]):
        return _h[0]
    # F11-C005: Forgejo exposes no check-runs — fall back to the adapter's
    # commit-sha anchor so multi-cycle sweeps on Forgejo repos are not
    # certified unanchored (same-size content edits must restart the sweep).
    # F12-C001: ONLY a full hex SHA is a trusted anchor — arbitrary strings
    # (or the "HEAD" fallback) let same-size edits pass undetected
    _hs = getattr(primary, "head_sha", None)
    if callable(_hs):
        try:
            _sha = _hs(repo)
        except Exception:  # noqa: BLE001 - probing never blocks a sweep
            return None
        if isinstance(_sha, str) and _re_sha.fullmatch(r"[0-9a-f]{40}", _sha):
            return _sha
    return None


def _omni_upsert_issue(
    config: RepoConfig, primary: ForgeAdapter, st: dict[str, Any],
    findings: list[dict], scanned: int, total: int, complete: bool, report: CycleReport,
) -> None:
    """Create-or-edit the ONE audit issue per repo, through the adapter.
    Shadow mode touches nothing. Findings live in state — an issue failure
    degrades to next-cycle retry, never data loss. On a COMPLETE publish the
    state records omni_published — the retry contract the complete fast path
    honors (MECE round-5, sol F5-002)."""
    if config.shadow or (not findings and (not complete or not st.get("omni_issue"))):
        return
    if complete:
        _score, _label = _omni_readiness(findings)
        report.alerts.append(f"omnisweep readiness: {_score}/100 ({_label})")
    body = _omni_report_body(config, findings, scanned, total, complete)
    number = st.get("omni_issue")
    if number:
        if not primary.update_issue(config.repo, number, body):
            st["omni_published"] = False
            # F12-C006: a DEAD issue id (deleted/forbidden) used to fail
            # forever — every completed fast path retried the same id and no
            # replacement was ever created. After bounded consecutive
            # failures the id is reconciled away (recreate next cycle); a
            # transient error keeps retrying.
            fails = int(st.get("omni_pub_fail", 0)) + 1
            if not findings:
                # Round-3 hunt proposed reconciling here like the non-empty
                # branch — REVERTED: test_clean_update_failures pins
                # indefinite retry with recovery on the 5th failure, and a
                # boolean update_issue cannot distinguish a dead id from a
                # transient outage. A dead id wedges (one failed call per
                # cycle, alerted every cycle — visible, never silent) until
                # adapter read APIs can prove absence. See registry.
                st["omni_pub_fail"] = fails
                report.alerts.append(
                    f"omnisweep: clean update of issue #{number} failed ({fails} attempts) — retrying next cycle")
                return
            if fails >= 3:
                report.alerts.append(
                    f"omnisweep: issue #{number} update failed {fails}x — id "
                    "reconciled; a replacement audit issue will be created")
                st.pop("omni_issue", None)
                st["omni_pub_fail"] = 0
                st["omni_published"] = False
            else:
                st["omni_pub_fail"] = fails
                report.alerts.append(
                    f"omnisweep: issue #{number} update failed ({fails}/3) — retrying next cycle")
            return
        st.pop("omni_pub_fail", None)
        if complete:
            st["omni_published"] = True
            st["omni_report_version"] = 2
            st["omni_clean_published"] = not findings
        return
    created = primary.open_issue(
        config.repo, f"omnisweep: full-tree audit of {config.repo}", body,
    )
    if created is None:
        report.alerts.append("omnisweep: audit-issue creation failed — retrying next cycle")
        return
    st["omni_issue"] = created
    if complete:
        st["omni_published"] = True
        st["omni_report_version"] = 2


def _retro_sweep(
    config: RepoConfig,
    primary: ForgeAdapter,
    state_path: Path,
    get_diff: Callable[[PullRequest], tuple[set[str], str] | None],
    shadow_sink: ShadowSink | None,
    st: dict[str, Any],
    report: CycleReport,
    deadline: float | None,
) -> set[int]:
    """Retro audit (CEO ask: catch any OLD mistakes): walk merged PRs OLDER
    than the forward post-merge watermark — newest-first, capped per cycle,
    cursor-resumable (`retro_cursor` = oldest merged_at processed; everything
    above it is pending until done). Freshness gate drops findings whose path
    no longer exists on HEAD (zombies on fixed code); an all-zombie PR posts
    NOTHING. Guards: cursor (primary), retro_seen set (same-second belt),
    head-SHA records (suspenders). Returns considered numbers for prune."""
    from datetime import datetime, timedelta, timezone

    if config.shadow:
        # MECE round-6 (luna-max F6-C006): retro under shadow is a dry run —
        # ARCH-2 NOTE (2026-09-16): this early return SUBSUMES the
        # retro_shadow_seen belt machinery below (belt load/write and the
        # per-PR shadow branch are unreachable while this return stands);
        # they are retained for history. Do not "restore" shadow retro by
        # deleting this return without deleting the belt code too.
        # with no consumer and every outcome path leaked live state (seen
        # belts, cursors, clean/deferred/park records) — skip entirely.
        log.info("retro: shadow mode — sweep skipped (dry-run law)")
        return set()
    if st.get("retro_complete"):
        return set()
    boundary = (
        datetime.now(timezone.utc) - timedelta(days=config.retro_audit.lookback_days)
    ).isoformat()
    from .timestamps import parse_iso

    upper = state.merged_watermark(st)
    if parse_iso(upper) is None:
        upper = datetime.now(timezone.utc).isoformat()
    cursor = st.get("retro_cursor")
    if parse_iso(cursor) is None:
        cursor = upper
    cursor_instant = parse_iso(cursor)

    try:
        listed = primary.list_merged_prs(config.repo, boundary)
    except ForgeError as exc:
        report.alerts.append(f"retro listing failed (skipped this cycle): {exc}")
        report._merged_listing_incomplete = True
        return set()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # UltraQA round 2: API shape drift degrades the lane, never crashes
        report.alerts.append(f"retro listing degraded (skipped this cycle): {exc}")
        report._merged_listing_incomplete = True
        return set()

    # F9-C001: envelope guard — a truthy dict iterated its keys as 'rows',
    # became an empty list and falsely declared the audit COMPLETE
    if not isinstance(listed, list):
        report.alerts.append(
            f"retro listing wrong envelope ({type(listed).__name__}; skipped this cycle)")
        report._merged_listing_incomplete = True
        return set()

    # row-shape guard (UltraQA round 2): one malformed merged row must not
    # abort the sweep. F10-C001: a malformed listing must never become a
    # terminal 'clean audit' — completion is blocked while rows are bad
    _dropped_rows = sum(1 for p in listed
                        if not isinstance(p, PullRequest) or parse_iso(p.merged_at) is None)
    if _dropped_rows:
        report._merged_listing_incomplete = True
        report.alerts.append(
            f"retro listing: {_dropped_rows} malformed merged rows \u2014 "
            "completion BLOCKED (retry next cycle)")
    listed = [p for p in listed if isinstance(p, PullRequest) and parse_iso(p.merged_at) is not None]

    # MECE round-2 (terra F2-002): JSON persistence turns int keys into
    # strings — the seen-set belt was comparing int numbers against str keys
    # and NEVER excluded (masked by the strict '<' cursor). Normalize here.
    _rs = st.get("retro_seen")
    if not isinstance(_rs, dict):
        _rs = {}  # MECE round-4 (luna F4-003): null/wrong-shape state degrades
    seen: set[int] = {number for k in _rs if (number := state.parse_number_key(k)) is not None}
    # MECE round-5 (sol F5-001): shadow runs keep their own dedupe belt and
    # never touch live cursors/belts — the live cutover must re-audit what
    # shadow only looked at
    shadow_belt: dict[str, str] = {}  # unreachable under the F6-C006 early return (ARCH-2)
    if config.shadow:
        sb = st.get("retro_shadow_seen")
        if isinstance(sb, dict):
            shadow_belt = {str(k): v for k, v in sb.items() if isinstance(v, str)}
    parked = st.get("retro_parked", {})
    if not isinstance(parked, dict):
        parked = {}
    parked = {str(number): expiry for k, v in parked.items()
              if (number := state.parse_number_key(k)) is not None
              and (expiry := state.parse_number_key(v)) is not None}
    now_i = int(time.time())
    # MECE round-5 (sol F5-009): parked PRs re-arm AUTOMATICALLY after their
    # window — no false "re-arms on the next repo commit" promise, no manual
    # reset needed
    active_park = {int(k) for k, v in parked.items() if v > now_i}
    expired_park = {int(k) for k, v in parked.items() if v <= now_i}
    pending = sorted(
        (p for p in listed
         if (parse_iso(p.merged_at) <= cursor_instant or p.number in expired_park) and p.number not in seen
         and p.number not in active_park
         and (not config.shadow or shadow_belt.get(str(p.number)) != p.head_sha)),
        key=lambda p: parse_iso(p.merged_at),
        reverse=True,  # newest unprocessed first: recent mistakes matter most
    )[: config.retro_audit.max_per_cycle]

    # A listed parked PR remains unresolved even when the work cap or deadline
    # postpones its retry. Preserve its identity through end-of-cycle pruning.
    parked_ids = active_park | expired_park
    considered: set[int] = {p.number for p in listed if p.number in parked_ids}
    oldest_processed: str | None = None
    for pr in pending:
        considered.add(pr.number)
        if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
            report.alerts.append("retro sweep deferred — cycle deadline reached")
            break
        seen.add(pr.number)
        if not config.shadow:  # MECE round-6 (sol F6-E01): shadow runs never
            # touch the LIVE seen belt — the local set dedupes the run only
            st["retro_seen"] = {int(n): True for n in seen}
        if not state.needs_review(st, pr.number, pr.head_sha):
            report.already_done += 1  # poll-invariant no-op: counted, never silent
            if not config.shadow:
                parked.pop(str(pr.number), None)
                parked.pop(pr.number, None)
                st["retro_parked"] = parked
                oldest_processed = pr.merged_at
            continue  # already reviewed at this SHA while it was open — nothing to catch
        try:
            outcome = _retro_review_pr(
                pr, config, primary, get_diff, shadow_sink, st, report,
            )
        except ForgeError as exc:
            # MECE round-3 (glm F3-1): per-PR containment like post-merge —
            # a forge hiccup defers THIS PR, never crashes the cycle
            report.alerts.append(f"retro #{pr.number}: forge error contained: {exc}")
            seen.discard(pr.number)
            if not config.shadow:
                st["retro_seen"] = {int(n): True for n in seen}
            state.save_state(state_path, st)
            break
        if outcome == "shadow":
            shadow_belt[str(pr.number)] = pr.head_sha
            if not config.shadow:
                oldest_processed = pr.merged_at
            continue
        if outcome == "deferred":
            seen.discard(pr.number)  # retry next cycle
            if not config.shadow:
                st["retro_seen"] = {n: True for n in seen}
            # MECE round-4 (luna F4-004): consecutive deferrals on one PR burn
            # the lane every cycle while the model/env is down — park after a
            # bounded retry count, surfaced by the alert
            key = f"retro_defer:{pr.number}:{pr.head_sha[:10]}"
            tries = int(st.get(key, 0)) + 1
            st[key] = tries
            if tries >= 3:
                # MECE round-5 (sol F5-009): parked entries carry an expiry —
                # the PR is retried automatically after the window, re-parked
                # only while the deferral cause persists
                st.pop(key, None)
                st["retro_parked"] = {**{str(k): v for k, v in parked.items()},
                                      str(pr.number): int(time.time()) + 86400}
                parked = st["retro_parked"]
                report.alerts.append(
                    f"retro #{pr.number}: deferred {tries}x (model/env) — parked 24h; "
                    "retried automatically after the window")
            state.save_state(state_path, st)  # MECE round-5 (sol F5-003): the
            # corrected seen-set/park state is saved HERE — the old
            # save-before-classification checkpointed the pre-discard seen-set
            # (a kill between the two left the PR permanently skipped)
            break
        # F11-C016: a deferred retry counter dies with its reason — terminal
        # success clears it (one- or two-attempt counters used to survive
        # successful reviews forever)
        st.pop(f"retro_defer:{pr.number}:{pr.head_sha[:10]}", None)
        parked.pop(str(pr.number), None)
        parked.pop(pr.number, None)
        st["retro_parked"] = parked
        oldest_processed = pr.merged_at

    if config.shadow and shadow_belt:
        st["retro_shadow_seen"] = shadow_belt
        while len(st["retro_shadow_seen"]) > 2000:  # bounded belt
            st["retro_shadow_seen"].pop(next(iter(st["retro_shadow_seen"])))
    elif not config.shadow and isinstance(st.get("retro_shadow_seen"), dict):
        st.pop("retro_shadow_seen", None)  # live runs stop honoring the belt
    if oldest_processed:
        st["retro_cursor"] = min(cursor, oldest_processed, key=parse_iso)
    if oldest_processed is None and not pending and not active_park and not config.shadow:
        # window exhausted between boundary and cursor — nothing left to audit
        # (also fires for repos with no merges in the window: stop re-listing).
        # MECE round-6 (sol F6-E01): shadow runs never set live completion;
        # F10-C001: neither do malformed listings
        if not _dropped_rows:
            st["retro_complete"] = True
            report.alerts.append(
                f"retro audit complete: {len(seen)} merged PRs within "
                f"{config.retro_audit.lookback_days}d swept"
            )
    return considered


def _retro_review_pr(
    pr: PullRequest,
    config: RepoConfig,
    primary: ForgeAdapter,
    get_diff: Callable[[PullRequest], tuple[set[str], str] | None],
    shadow_sink: ShadowSink | None,
    st: dict[str, Any],
    report: CycleReport,
) -> str:
    """One retro review: same pipeline as post-merge, plus the freshness gate.
    Returns 'reviewed' | 'shadow' | 'deferred' (deferred = retried later)."""
    from .analyzer import ModelUnavailable, analyze
    from . import gatekeeper

    diff = get_diff(pr)
    if diff is None:
        report.skipped_diff_unavailable += 1
        return "deferred"
    diff_files, diff_text = diff

    try:
        doc = analyze(pr, diff_files, diff_text, config)
    except ModelUnavailable:
        report.model_unavailable += 1
        return "deferred"  # retro never caps: the cursor already passed it; retry is free

    findings = doc.findings
    dropped_here = 0  # MECE round-4 (luna F4-006): per-PR count for the comment
    if findings and config.gatekeeper:
        findings, dropped, _fo = gatekeeper.filter_findings(findings, config)
        report.gatekeeper_dropped += dropped
        dropped_here = dropped

    if findings and config.retro_audit.freshness_gate:
        fresh: list = []
        for f in findings:
            exists = primary.path_exists(config.repo, f.path)
            if exists is False:
                report.retro_zombies += 1  # path gone from HEAD: fixed/moved code
                continue
            fresh.append(f)  # True or None (unqueryable → fail open, keep)
        findings = fresh

    if not findings and not _has_prior_findings(primary, config, pr.number):
        # all-zombie or clean: post NOTHING on the old PR (zero-noise law);
        # the state record is the audit trail.
        state.mark_reviewed(st, pr.number, pr.head_sha, "retro:0")
        return "reviewed"

    rh = f"{pr.head_sha[:12]}{len(findings):04x}"
    previous: list[Finding] = []
    existing = primary.get_persistent_comment(config.repo, pr.number)
    if existing:
        previous = _prior_findings(existing[1])
    body = renderer.render_review(
        pr, findings, config, rh, previous, post_merge=True,
        gatekeeper_dropped=dropped_here or 0,
        diff_truncated=bool(doc.digest.get("_diff_truncated")),
    )
    if config.shadow:
        if shadow_sink:
            shadow_sink(config.repo, pr.number, body)
        report.shadow_only = True
        state.mark_reviewed(st, pr.number, pr.head_sha, f"shadow:{len(findings)}")
        report.retro_reviewed += 1
        return "shadow"
    if existing:
        primary.update_comment(config.repo, pr.number, existing[0], body)
    else:
        primary.create_comment(config.repo, pr.number, body)
    state.mark_reviewed(st, pr.number, pr.head_sha, f"retro:{len(findings)}")
    report.retro_reviewed += 1
    return "reviewed"


def _has_prior_findings(primary: ForgeAdapter, config: RepoConfig, number: int) -> bool:
    """Does our own persistent comment on this PR already carry findings?
    (Edit-in-place continues the thread; clean/zombie results stay silent.)"""
    existing = primary.get_persistent_comment(config.repo, number)
    return bool(existing and renderer.parse_finding_lines(existing[1]))


def _ci_watch_step(
    config: RepoConfig,
    primary: ForgeAdapter,
    st: dict[str, Any],
    report: CycleReport,
    run_fixes: bool,
    deadline: float | None = None,
) -> None:
    """CEO directive 2026-09-01: a red default-branch HEAD on an OWN repo
    summons review + fix. SHA-keyed (state['ci_acted:{sha}']) — no timestamp
    watermark, self-healing on the next commit, same law as the head-SHA
    predicate. Findings come from the failing checks' annotations (a
    deterministic signal — the model is never asked to invent them); the fix
    lane attempts one patch per finding; no fix landing escalates to an issue.
    Contained: any failure logs and returns."""
    from . import executor
    from .models import Finding

    queried = primary.head_check_runs(config.repo)
    if queried is None:
        log.info("ci_watch: head check-runs unqueryable for %s (degraded this cycle)", config.repo)
        return
    if not isinstance(queried, (tuple, list)) or len(queried) != 2:
        report.alerts.append("ci_watch: head_check_runs wrong shape (degraded this cycle)")
        return
    head, runs = queried
    if not isinstance(head, str) or not head:
        report.alerts.append("ci_watch: head value unusable (degraded this cycle)")
        return
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        # MECE round-7 (terra F7-003): a non-hex 'head' crashed the synthetic
        # PR number int(head[:6], 16) on fixes-enabled cycles — unusable head,
        # degrade before any action
        report.alerts.append(
            f"ci_watch: head {head[:12]!r} is not a full hex SHA (degraded this cycle)")
        return
    if not isinstance(runs, list):
        report.alerts.append("ci_watch: check-runs wrong shape (degraded this cycle)")
        return
    # UltraQA round 3 (P2): check-run fields are forge-external — coerce types
    # so a numeric name or dict conclusion degrades instead of crashing
    # (a dict is unhashable against the benign set).
    failing = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        if r.get("status") != "completed":
            continue
        conclusion = r.get("conclusion")
        if not isinstance(conclusion, str):
            conclusion = "?"
        name = r.get("name")
        if not isinstance(name, str):
            name = "unnamed check"
        run_id = r.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, (str, int)) \
                or not str(run_id).strip():
            # F12-C005: bool ids are ints to isinstance — True would address
            # /check-runs/True/annotations and lose every file finding
            continue  # unusable run identity: skip, never crash (UltraQA P2)
        if conclusion not in _CI_BENIGN:
            failing.append(dict(r, conclusion=conclusion, name=name, id=run_id))
    failing = failing[: config.ci_watch.max_checks]
    if not failing:
        st.pop("ci_red_sha", None)
        return
    report.ci_red_heads += 1
    if st.get(f"ci_acted:{head}"):
        return  # already acted at this SHA — a new commit re-arms the watch
    # MECE round-2 (terra F2-004): acted is set only after a SUCCESSFUL
    # action below; an escalation failure stays un-acted and retries.

    findings: list[Finding] = []
    summaries: list[str] = []
    for run in failing:
        name = run.get("name")
        if not isinstance(name, str):
            name = "unnamed check"
        conclusion = run.get("conclusion") or "?"
        if not isinstance(conclusion, str):
            conclusion = "?"
        out = run.get("output")
        summary = out.get("summary") if isinstance(out, dict) else None
        summary = summary.strip() if isinstance(summary, str) else ""
        # CI check text is forge-external: single-line it before it lands in an
        # escalation body (UltraQA round 2, P2 — annotations/summaries bypass
        # the analyzer, so the scrub that protects posted findings doesn't run)
        summaries.append(f"- **{scrub.inline(name, 60)}** — {scrub.inline(conclusion, 20)}"
                         + (f": {scrub.inline(summary, 300)}" if summary else ""))
        _anns_raw = primary.check_annotations(config.repo, run.get("id"))
        # MECE round-6 (luna-max F6-C012): adapter envelopes are forge-
        # external — a truthy non-list (dict/None) used to crash the slice
        anns = _anns_raw if isinstance(_anns_raw, list) else []
        # Informational rows cannot direct repairs or consume the failure cap.
        # Missing levels retain compatibility with legacy forge annotations.
        actionable = [a for a in anns if isinstance(a, dict)
                      and a.get("level") not in ("notice", "warning")]
        for a in actionable[: config.ci_watch.max_annotations]:
            if not a.get("path") or not a.get("message"):
                continue
            try:
                # annotation line is forge-external: non-numeric values must not
                # crash the watch (UltraQA round 2)
                ann_line = int(a.get("start_line") or a.get("line") or 0) or 1
            except (TypeError, ValueError, OverflowError):
                ann_line = 1
            # MECE round-5 (sol F5-008): every annotation field is forge-
            # external — a numeric/object message crashed the slice below
            msg = a.get("message")
            if not isinstance(msg, str):
                msg = str(msg) if msg is not None else ""
            findings.append(
                Finding(
                    rule_id="ci",
                    severity="Major",
                    path=scrub.inline(str(a["path"]), 200),
                    line=ann_line,
                    category="CI",
                    message=scrub.scrub(f"[{scrub.inline(name, 60)}] {msg[:400]}"),
                )
            )

    if findings:
        # GH Actions emits RUN-LEVEL auto-annotations anchored at the workflow
        # dir (path ".github", workflow line numbers — deprecation warnings,
        # step failures). Those are not code findings: the fix lane fetches
        # the anchored path and a directory is unfetchable (live 2026-09-03:
        # every red head burned a fix attempt on ".github"). Mint findings
        # only for paths that are FILES at the red head; None (unqueryable)
        # keeps the finding — fail-open.
        kept: list[Finding] = []
        for f in findings:
            is_file = primary.path_is_file(config.repo, f.path, ref=head)
            if is_file is False:
                log.info("ci_watch: dropped annotation at non-file path %s (run-level meta?)", f.path)
            else:
                kept.append(f)
        findings = kept

    if deadline is not None and (deadline - time.monotonic()) < REVIEW_BUDGET_S:
        # MECE round-6 (luna-max F6-C017): never start fix/escalation work
        # after the cycle budget expired — defer to a later cycle, and do NOT
        # persist the acted belt (the head stays summonable)
        report.alerts.append("ci_watch: cycle deadline reached — red head deferred to next cycle")
        return
    if findings and run_fixes and config.fix.enabled and not config.shadow:
        # Synthetic PR anchored at the red head: the fix lane fetches the file
        # at this SHA, patches, tests sandboxed, opens a follow-up PR. A
        # sha-derived number keeps branch names unique per red head.
        synth = PullRequest(
            forge=primary.name,
            number=int(head[:6], 16),
            repo=config.repo,
            title=f"CI fix @ {head[:8]}",
            head_sha=head,
        )
        for f in findings[: config.ci_watch.max_annotations]:
            if not _fix_freshness_gate(primary, config, f):
                continue
            if deadline is not None and time.monotonic() >= deadline:
                # Cycle-3: the deadline was checked once before this loop;
                # each attempt runs minutes, so recheck per finding.
                report.alerts.append("ci-watch fix work deferred — cycle deadline reached")
                break
            report.fix_attempts += 1
            # Cycle-3: one-shot synth context like omnisweep — bounded by
            # max_annotations, not by a tracked depth.
            result = executor.attempt_fix(synth, f, config, depth_used=0)
            status = result.get("status")
            if status == "pr_opened":
                report.ci_fix_prs_opened += 1
            elif status in ("error", "testfail", "nofix"):
                report.fix_failures += 1
                report._fix_failure_notes.append(f"ci@{head[:8]} {status}: {str(result.get('reason'))[:80]}")
                if status == "error":
                    break  # environmental failure: stop burning attempts this cycle
        opened = report.ci_fix_prs_opened
    else:
        opened = 0

    if not opened and config.ci_watch.escalate_issues and not config.shadow:
        try:
            # F11-C014: check names are forge-external content — the body is
            # scrubbed/truncated but the title used to carry up to 20 RAW
            # names; newline-bearing or overlong names broke the forge title
            # limit and the escalation failed forever at that HEAD
            _names = [scrub.inline(r.get("name") or "?", 60) for r in failing]
            _title = f"CI red on main @ {head[:8]} — {', '.join(_names)}"
            executor.open_issue(
                config.repo,
                title=_title[:140],
                body=(
                    "## FL4WRITE CI watch — human action required\n\n"
                    f"Default-branch HEAD `{head}` is red; no automated fix landed.\n\n"
                    f"Failing checks:\n" + "\n".join(summaries) +
                    "\n\n_Findings from annotations:_\n"
                    + ("\n".join(f"- {renderer._code_span(renderer.path_display(f.path) + ':' + str(f.line))} — {scrub.inline(f.message, 120)}" for f in findings) or "(no file-level annotation findings — run-level/meta annotations only, or none)")
                ),
            )
            report.ci_escalations += 1
            st[f"ci_acted:{head}"] = True
        except Exception as exc:  # noqa: BLE001 — escalation must not kill the cycle
            report.alerts.append(f"ci_watch escalation failed for {config.repo}: {exc}")
    elif opened:
        st[f"ci_acted:{head}"] = True
    elif not config.shadow:
        # no fix opened and no escalation channel: acting again next cycle
        # changes nothing — mark acted to keep the red-head count honest (one
        # summon per SHA). MECE round-6 (luna-max F6-C004): shadow runs never
        # persist the live belt — the live cutover must still act on the head
        st[f"ci_acted:{head}"] = True
