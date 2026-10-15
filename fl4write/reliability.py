"""Reliability measurement harness (CEO verdict 2026-09-16: "not even a
reliable code review bot yet").

Measures the THREE ruled properties through the REAL product path
(analyzer.analyze -> _call_model -> grounding gates), so every number
reflects the shipped behavior, not the model in isolation:

1. DETERMINISM  — same input diff, N fresh calls (cache bypassed by
   design: cached repeats would fabricate stability), verdict signature
   agreement measured per case.
2. PLANTED-DEFECT RECALL — labeled corpus (reliability_corpus), catch
   rate per defect class at two bars: any-finding and actionable
   (Critical/Major). False-positive rate on clean diffs at the same bars.
3. HONEST UNCERTAINTY — the product schema emits an OPTIONAL numeric
   confidence per finding (Finding.confidence: the model's own probability
   [0,1] that the finding is a real defect; absent = no calibrated claim).
   Calibration is measured two ways: per-severity precision (P(finding is on
   truly-defective code | severity)) and, when confidence is emitted, a
   binned ECE against path-level labels. Path-level labeling caveat: every
   grounded finding on a defect-case file counts TP, every finding on a
   clean-case file counts FP — an upper bound on precision for Nit/Minor
   style claims.

Posted findings are signature-voted (analyzer.analyze: best-of-N modal over
config.signature_vote_n samples) — the determinism lane measures the POSTED
verdict, so the anchor-flip class (2026-09-17 baseline) is killed engine-
side, not papered over in the metric.

Re-usability: `python -m fl4write.reliability --config <yaml> [route
overrides] --runs 5` re-measures any time; recall/clean lanes cache
honestly (key = exact prompt + route params + vote slot; parse + gates
always re-run locally), determinism lanes never cache. Optional regression
gates (--min-determinism/--min-recall/--max-actionable-fp/--max-ece) make
reliability a CI-able property once thresholds are set from a recorded
baseline.

Call-budget law: determinism = len(DETERMINISM_SAMPLE) * runs * vote_n
fresh calls; recall/clean = vote_n calls per case (cache-served per slot
after first run).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import reliability_corpus as corpus
from .analyzer import ModelUnavailable, _failure_claimed, _system_prompt, analyze
from .analyzer import verdict_signature  # canonical home: analyzer (re-export)
from .config import ModelRoute, RepoConfig, load_config
from .models import PullRequest

SEVERITY_ORDER = ["Critical", "Major", "Minor", "Nit"]
ACTIONABLE = ("Critical", "Major")


# ---------------------------------------------------------------------------
# run one case through the REAL product path
# ---------------------------------------------------------------------------

def _pr(case_id: str) -> PullRequest:
    return PullRequest(forge="forgejo", number=1, repo="eval/reliability",
                       title=f"eval: {case_id}", head_sha="0" * 40)


def run_case(case: dict, config: RepoConfig) -> dict:
    """One analyze() call on one corpus case -> a verdict record.

    Raises only what analyze raises (ModelUnavailable after all routes
    fail); transport errors are part of the measurement, never hidden.
    """
    diff_files, diff_text = corpus.build_case_diff(case)
    doc = analyze(_pr(case["id"]), diff_files, diff_text, config)
    findings = doc.findings
    return {
        "case_id": case["id"],
        "defect_class": case.get("defect_class", ""),
        "kind": "defect" if case.get("defect_class") else "clean",
        "signature": [list(t) for t in verdict_signature(findings)],
        "flag": "defect" if findings else "clean",
        "actionable": any(f.severity in ACTIONABLE for f in findings),
        "severity_counts": dict(Counter(f.severity for f in findings)),
        "findings": [
            {"rule_id": f.rule_id, "severity": f.severity, "path": f.path,
             "line": f.line, "message": f.message[:160],
             "confidence": f.confidence}
            for f in findings
        ],
    }


# verdict_signature lives in analyzer (the post-time signature-vote needs
# the same identity); it is re-exported above for harness callers.


# ---------------------------------------------------------------------------
# honest cache: raw model content keyed by route+prompt; gates re-run local
# ---------------------------------------------------------------------------

class ResponseCache:
    """Disk cache of RAW model content. Key = endpoint|model|temperature|
    seed|max_tokens|system-hash|prompt-hash. A hit returns the recorded
    content — parse + grounding gates re-run in-process, so gate or parser
    changes always re-measure from cached content without new spend."""

    def __init__(self, dirpath: Path | None):
        self.dir = dirpath
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(route: ModelRoute, system: str, prompt: str) -> str:
        h = hashlib.sha256()
        for part in (route.endpoint, route.model, repr(route.temperature),
                     repr(route.seed), repr(route.max_tokens),
                     hashlib.sha256(system.encode()).hexdigest(),
                     hashlib.sha256(prompt.encode()).hexdigest()):
            h.update(part.encode() if isinstance(part, str) else repr(part).encode())
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        if self.dir is None:
            return None
        p = self.dir / f"{key}.txt"
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, key: str, content: str) -> None:
        if self.dir is None:
            return
        try:
            (self.dir / f"{key}.txt").write_text(content, encoding="utf-8")
        except OSError:
            pass


def install_cached_call(cache: ResponseCache | None) -> Callable[[], None]:
    """Wrap analyzer._call_model/_call_sample with the cache (None =
    pass-through). Returns an uninstall callable. Real-call accounting is
    exposed via cache.real_calls for budget receipts.

    Signature-vote samples key by ``key#slot``: N vote samples are N
    INDEPENDENT recorded responses. Slot-blind caching would serve sample
    1's content N times and fabricate a unanimous vote (the same defect as
    a cached determinism repeat)."""
    from . import analyzer as _an

    real = _an._call_model
    real_sample = _an._call_sample
    if cache is None:
        cache = ResponseCache(None)
    cache.real_calls = 0
    cache.hits = 0
    cache.misses = 0

    def _fetch(key: str, produce) -> str:
        hit = cache.get(key)
        if hit is not None:
            cache.hits += 1
            return hit
        cache.misses += 1
        cache.real_calls += 1
        content = produce()
        cache.put(key, content)
        return content

    def _produce(route: ModelRoute, prompt: str, mode: str, system: str | None):
        # arity guard: 3-arg monkeypatched stubs must keep working
        if system is None:
            return real(route, prompt, mode)
        return real(route, prompt, mode, system)

    def cached(route: ModelRoute, prompt: str, mode: str = "pr",
               system: str | None = None) -> str:
        sys_prompt = system if system is not None else _system_prompt(mode)
        key = ResponseCache.key(route, sys_prompt, prompt)
        return _fetch(key, lambda: _produce(route, prompt, mode, system))

    def cached_sample(route: ModelRoute, prompt: str, mode: str = "pr",
                      system: str | None = None, slot: int = 0) -> str:
        sys_prompt = system if system is not None else _system_prompt(mode)
        key = ResponseCache.key(route, sys_prompt, prompt) + f"#{slot}"
        return _fetch(key, lambda: _produce(route, prompt, mode, system))

    _an._call_model = cached
    _an._call_sample = cached_sample

    def uninstall() -> None:
        _an._call_model = real
        _an._call_sample = real_sample

    return uninstall


# ---------------------------------------------------------------------------
# metrics — pure functions, unit-tested offline
# ---------------------------------------------------------------------------

def _flag_of(record: dict) -> str:
    return record["flag"]


def _sev_key(record: dict) -> tuple:
    return tuple(sorted(record["severity_counts"].items()))


def determinism_metrics(records: list[dict]) -> dict:
    """Per-case and aggregate verdict stability over N runs.

    agreement_rate = frequency of the MODAL signature (1.0 = every run
    identical). flag_flips = runs whose defect/clean verdict differs from
    the modal verdict. severity_flips = runs whose severity multiset
    differs from the modal one. Error records are EXCLUDED from agreement:
    N identical outages used to score 1.0 (empty signatures agree); a case
    with no successful runs now scores 0.0 fail-closed (errors are counted
    separately and the CLI gate fails the run). The mean is over successful
    runs by design: partial-outage errors neither inflate agreement nor
    count as flips — they ride the errors count and the gate, so a mixed
    run reads 1.0 + N errors rather than a diluted fraction.
    """
    by_case: dict[str, list[dict]] = {}
    for r in records:
        by_case.setdefault(r["case_id"], []).append(r)
    per_case: dict[str, dict] = {}
    for case_id, runs in by_case.items():
        clean = [r for r in runs if not r.get("error")]
        n_err = len(runs) - len(clean)
        if not clean:
            per_case[case_id] = {
                "runs": len(runs),
                "errors": n_err,
                "distinct_signatures": 0,
                "agreement_rate": 0.0,
                "flag_flips": 0,
                "severity_flips": 0,
                "modal_signature": [],
                "signatures_seen": [],
            }
            continue
        sigs = [tuple(map(tuple, r["signature"])) for r in clean]
        modal_sig, modal_n = Counter(sigs).most_common(1)[0]
        modal_flag = Counter(_flag_of(r) for r in clean).most_common(1)[0][0]
        modal_sev = Counter(_sev_key(r) for r in clean).most_common(1)[0][0]
        per_case[case_id] = {
            "runs": len(runs),
            "errors": n_err,
            "distinct_signatures": len(set(sigs)),
            "agreement_rate": modal_n / len(clean),
            "flag_flips": sum(1 for r in clean if _flag_of(r) != modal_flag),
            "severity_flips": sum(1 for r in clean if _sev_key(r) != modal_sev),
            "modal_signature": [list(t) for t in modal_sig],
            "signatures_seen": sorted({json.dumps([list(t) for t in s]) for s in sigs}),
        }
    rates = [c["agreement_rate"] for c in per_case.values()]
    return {
        "per_case": per_case,
        "mean_agreement": sum(rates) / len(rates) if rates else 0.0,
        "min_agreement": min(rates) if rates else 0.0,
        "total_runs": len(records),
        "errors": sum(1 for r in records if r.get("error")),
    }


def recall_metrics(defect_records: list[dict]) -> dict:
    """Case-weighted recall, with counts summarized per defect class.

    Each distinct case counts once and is caught at a bar when at least
    one run has any finding / any actionable finding on that case.
    Class booleans indicate coverage of at least one case, not every case.
    Error records (``flag == "error"``) count as misses: fail-closed, so an
    outage reads as 0/N rather than silently shrinking the denominator.
    Trend rows are only appended from error-free runs (the CLI error gate
    plus the cadence script's ``set -e`` stop a tainted run before render).
    """
    by_class: dict[str, list[dict]] = {}
    for r in defect_records:
        by_class.setdefault(r["defect_class"], []).append(r)
    per_class = {}
    for cls, runs in sorted(by_class.items()):
        by_case: dict[str, list[dict]] = {}
        for record in runs:
            by_case.setdefault(record["case_id"], []).append(record)
        caught_any = sum(any(r["flag"] == "defect" for r in samples) for samples in by_case.values())
        caught_actionable = sum(any(r["actionable"] for r in samples) for samples in by_case.values())
        per_class[cls] = {
            "cases": len(by_case),
            "caught_any": bool(caught_any),
            "caught_actionable": bool(caught_actionable),
            "caught_any_cases": caught_any,
            "caught_actionable_cases": caught_actionable,
        }
    n = sum(c["cases"] for c in per_class.values())
    return {
        "per_class": per_class,
        "recall_any": sum(c["caught_any_cases"] for c in per_class.values()) / n if n else 0.0,
        "recall_actionable": sum(c["caught_actionable_cases"] for c in per_class.values()) / n if n else 0.0,
    }


def _caught_counts(entry: dict) -> tuple[int, int, int]:
    """Per-class (total, caught_any, caught_actionable) with backward compat.

    Current reports carry explicit ``caught_*_cases`` counts. Pre-REL-002
    reports only have boolean ``caught_any``/``caught_actionable`` meaning
    "at least one case in the class was caught", so a true boolean maps to
    a lower bound of 1 (not the whole class) and a false/absent one to 0.
    Each bar falls back independently, so partially migrated entries never
    silently zero the other bar.
    """
    total = int(entry.get("cases", 0))

    def _one(count_key: str, flag_key: str) -> int:
        if count_key in entry:
            raw = int(entry.get(count_key, 0))
        else:
            raw = 1 if entry.get(flag_key) else 0
        # Cycle-3: clamp to the class size — hand-edited rot like
        # caught=3/cases=2 (or caught=1/cases=0) renders sanely instead
        # of recall > 1 or "1/0".
        return max(0, min(raw, total))

    return (total, _one("caught_any_cases", "caught_any"),
            _one("caught_actionable_cases", "caught_actionable"))


def trend_row(report: dict, report_path: str = "") -> dict:
    """Single trend-row emitter shared by the cadence runner and tests.

    Keeps recall math in one place so metric fixes never need parallel
    edits in shell-embedded Python.
    """
    det = report.get("determinism", {})
    rec = report.get("recall", {})
    fp = report.get("false_positives", {})
    totals = [_caught_counts(v) for v in rec.get("per_class", {}).values()]
    tot = sum(t for t, _, _ in totals)
    caught = sum(c for _, c, _ in totals)
    caught_a = sum(a for _, _, a in totals)
    tbl = report.get("calibration", {}).get("table", {})

    def _count(row: dict, key: str) -> int:
        # Cycle-5 review: one coercion for both the aggregate and the
        # per-severity list (exact int; bools and junk read as 0).
        value = row.get(key, 0) if isinstance(row, dict) else 0
        return value if type(value) is int else 0

    tp = sum(_count(v, "tp") for v in tbl.values())
    fpn = sum(_count(v, "fp") for v in tbl.values())
    precisions = [_count(v, "tp") / (_count(v, "tp") + _count(v, "fp"))
                  for v in tbl.values()
                  if _count(v, "tp") + _count(v, "fp") > 0]
    route = report.get("route", {})
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "route": f'{route.get("model", "?")}@{route.get("endpoint", "?")}',
        "determinism": round(float(det.get("mean_agreement", 0.0)), 4),
        "det_min": det.get("min_agreement", 0.0),
        "recall_any": f"{caught}/{tot}",
        "recall_actionable": f"{caught_a}/{tot}",
        "actionable_fp": fp.get("actionable_fp_rate", 0.0),
        "noise_fp": fp.get("noise_fp_rate", 0.0),
        # FP split (queue item 3): coverage-only law notes ride their own bar
        "law_note_fp": fp.get("law_note_rate", 0.0),
        # honest uncertainty: confidence ECE (null = no confidence data)
        "ece": (report.get("confidence_calibration") or {}).get("ece"),
        "severity_precision": round(tp / (tp + fpn), 4) if tp + fpn else None,
        # Cycle-5: the aggregate above can hide a per-severity inversion
        # (Critical 1/1 + Nit 0/10 reads ~0.09); the worst bar rides along.
        "worst_severity_precision": round(min(precisions), 4) if precisions else None,
        "report": report_path,
    }


_LAW_NOTE_RULES = ("tests", "testing-quality")


def _is_law_note(finding: dict) -> bool:
    """Coverage-only testing-law note: a tests-family finding asserting
    missing/weak coverage WITHOUT a failure claim. These are law-reading
    noise, not defect claims — they ride their own bar (law_note_rate) so
    actionable_fp_rate measures DEFECT-FINDING ERROR (reliability queue
    item 3: both actionable FPs in the 09-17 baseline were coverage claims
    and inflated the defect-error signal)."""
    if finding.get("rule_id") not in _LAW_NOTE_RULES:
        return False
    return not _failure_claimed(finding.get("message", ""))


def _actionable_defect_claim(runs: list[dict]) -> bool:
    """Case-level actionable-FP bar: any run posts an actionable-severity
    finding that is NOT a coverage-only law note. Records without a
    findings list (pre-split synthetic records) fall back to the record's
    own actionable flag — same bar as before the split."""
    for r in runs:
        findings = r.get("findings") or []
        if not findings:
            if r.get("actionable"):
                return True
            continue
        for f in findings:
            if f.get("severity") in ACTIONABLE and not _is_law_note(f):
                return True
    return False


def fp_metrics(clean_records: list[dict]) -> dict:
    """Fail-closed like recall: error records are non-findings here, and
    tainted runs never reach the trend renderer (CLI gate + ``set -e``).

    Error records are excluded from the denominator: an outage is missing
    data, not a clean review, so dead-route clean lanes no longer score a
    0.0 false-positive rate on reviews that never happened. The excluded
    count is reported as ``errors``.

    Two FP bars (queue item 3 split): ``actionable_fp_rate`` counts only
    ACTIONABLE DEFECT CLAIMS on clean fixtures — the defect-finding error;
    coverage-only testing-law notes are counted on ``law_note_rate``
    instead. ``noise_fp_rate`` keeps its original any-finding meaning."""
    by_case: dict[str, list[dict]] = {}
    for r in clean_records:
        by_case.setdefault(r["case_id"], []).append(r)
    errors = sum(1 for runs in by_case.values() for r in runs if r.get("error"))
    measured = {cid: [r for r in runs if not r.get("error")]
                for cid, runs in by_case.items()}
    measured = {cid: runs for cid, runs in measured.items() if runs}
    n = len(measured)
    noise = sum(any(r["flag"] == "defect" for r in runs) for runs in measured.values())
    actionable = sum(_actionable_defect_claim(runs) for runs in measured.values())
    law_note = sum(any(_is_law_note(f) for r in runs for f in (r.get("findings") or []))
                   for runs in measured.values())
    return {
        "clean_cases": n,
        "errors": errors,
        "noise_fp_rate": noise / n if n else 0.0,
        "actionable_fp_rate": actionable / n if n else 0.0,
        "law_note_rate": law_note / n if n else 0.0,
        "flagged_case_ids": sorted(cid for cid, runs in measured.items()
                                   if any(r["flag"] == "defect" for r in runs)),
    }


def severity_calibration(records: list[dict]) -> dict:
    """Honest-uncertainty table. Severity is the ordinal claim-strength
    signal; its calibration = per-severity precision on labeled fixtures
    (defect-case finding = TP, clean-case finding = FP; path-level labeling,
    upper bound for Nit/Minor). Numeric confidence (Finding.confidence) is
    calibrated separately — see confidence_calibration() (ECE)."""
    rows: dict[str, dict[str, int]] = {}
    for r in records:
        label = "tp" if r["kind"] == "defect" else "fp"
        for sev, count in r["severity_counts"].items():
            row = rows.setdefault(sev, {"n": 0, "tp": 0, "fp": 0})
            row["n"] += count
            row[label] += count
    table = {}
    for sev, row in sorted(rows.items(), key=lambda kv: SEVERITY_ORDER.index(kv[0])
                           if kv[0] in SEVERITY_ORDER else 99):
        table[sev] = {**row, "precision": row["tp"] / row["n"] if row["n"] else None}
    emitted = any(f.get("confidence") is not None
                  for r in records for f in (r.get("findings") or []))
    return {
        "confidence_field_emitted": emitted,
        "proxy": "severity",
        "labeling": "path-level: defect-case finding = TP, clean-case finding = FP",
        "table": table,
    }


ECE_BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))


def confidence_calibration(records: list[dict]) -> dict:
    """Numeric-confidence calibration (the CEO's honest-uncertainty bar,
    answered with a number): ECE over the findings that carry
    Finding.confidence, labeled path-level (defect-case finding = 1,
    clean-case finding = 0).

    ECE = sum_b (n_b / N) * |empirical_precision_b - mean_confidence_b|
    over 5 fixed bins. A model that emits 0.9 on claims that turn out true
    90% of the time scores near 0. ``emitted_rate`` is the share of findings
    carrying any confidence claim — honest ABSSENCE is fine (optional
    field), but a gate on ECE cannot pass without data (fail-closed)."""
    points: list[tuple[float, int]] = []
    total = 0
    for r in records:
        label = 1 if r["kind"] == "defect" else 0
        for f in (r.get("findings") or []):
            total += 1
            conf = f.get("confidence")
            if conf is None:
                continue
            points.append((float(conf), label))
    bins = []
    ece = 0.0
    n = len(points)
    for lo, hi in ECE_BINS:
        in_bin = [(c, y) for c, y in points
                  if (lo <= c < hi) or (hi == 1.0 and c == 1.0)]
        if not in_bin:
            bins.append({"lo": lo, "hi": hi, "n": 0,
                         "mean_confidence": None, "empirical_precision": None})
            continue
        mean_conf = sum(c for c, _ in in_bin) / len(in_bin)
        precision = sum(y for _, y in in_bin) / len(in_bin)
        ece += (len(in_bin) / n) * abs(precision - mean_conf)
        bins.append({"lo": lo, "hi": hi, "n": len(in_bin),
                     "mean_confidence": round(mean_conf, 4),
                     "empirical_precision": round(precision, 4)})
    return {
        "n_findings": total,
        "n_with_confidence": n,
        "emitted_rate": (n / total) if total else 0.0,
        "ece": round(ece, 4) if n else None,
        "bins": bins,
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def _route_overrides(args: argparse.Namespace) -> dict[str, Any]:
    ov: dict[str, Any] = {}
    if args.endpoint:
        ov["endpoint"] = args.endpoint
    if args.model:
        ov["model"] = args.model
    if args.key_env is not None:
        ov["key_env"] = args.key_env
    if args.temperature is not None:
        ov["temperature"] = args.temperature
    if args.max_tokens is not None:
        ov["max_tokens"] = args.max_tokens
    if args.seed is not None:
        ov["seed"] = args.seed
    return ov


def build_config(base_config_path: str | Path,
                 overrides: dict[str, Any] | None = None) -> RepoConfig:
    base = load_config(base_config_path)
    if overrides:
        route = base.model.model_copy(update=overrides)
        base = base.model_copy(update={"model": route})
    return base


def _guarded_run(case: dict, config: RepoConfig) -> dict:
    try:
        rec = run_case(case, config)
        rec["error"] = ""
        return rec
    except ModelUnavailable as exc:
        return {"case_id": case["id"], "defect_class": case.get("defect_class", ""),
                "kind": "defect" if case.get("defect_class") else "clean",
                "signature": [], "flag": "error", "actionable": False,
                "severity_counts": {}, "findings": [], "error": str(exc)[:200]}
    except Exception as exc:  # transport-level failures are measured, not fatal
        return {"case_id": case["id"], "defect_class": case.get("defect_class", ""),
                "kind": "defect" if case.get("defect_class") else "clean",
                "signature": [], "flag": "error", "actionable": False,
                "severity_counts": {}, "findings": [], "error": f"{type(exc).__name__}: {exc}"[:200]}


def run_measurement(config: RepoConfig, runs: int = 5,
                    determinism_ids: list[str] | None = None,
                    cache_dir: Path | None = None,
                    out_dir: Path | None = None) -> dict:
    """Full measurement. Determinism lane: N FRESH calls per sample case,
    completely outside the cache (repeats must be independent samples —
    a cached repeat would fabricate stability). Recall/clean lanes: one
    cached call per case (parse + gates always re-run in-process).

    TransportCollector wraps the measurement to capture per-request
    transport evidence from every actual HTTP call.  Cache hits produce
    no transport record — only real HTTP exchanges are observed.
    """
    sample_ids = determinism_ids or corpus.DETERMINISM_SAMPLE
    vote_n = max(1, min(9, int(getattr(config, "signature_vote_n", 1) or 1)))
    cache = ResponseCache(cache_dir)
    collector = TransportCollector()

    with collector:
        # --- determinism lane: bare calls, no wrapper installed at all ---
        det_records: list[dict] = []
        for cid in sample_ids:
            try:
                case = corpus.case_by_id(cid)
            except KeyError:
                # Cycle-3: a stale sample id must fail closed as error records,
                # not crash the whole measurement out of _guarded_run's belt.
                det_records.extend({
                    "case_id": cid, "defect_class": "", "kind": "clean",
                    "signature": [], "flag": "error", "actionable": False,
                    "severity_counts": {}, "findings": [],
                    "error": f"unknown determinism case id: {cid}",
                } for _ in range(runs))
                continue
            for _ in range(runs):
                det_records.append(_guarded_run(case, config))
        det = determinism_metrics(det_records)

        # --- recall + clean lanes: honest cache ---
        uninstall = install_cached_call(cache)
        try:
            recall_records = [_guarded_run(c, config) for c in corpus.DEFECT_CASES]
            clean_records = [_guarded_run(c, config) for c in corpus.CLEAN_CASES]
        finally:
            uninstall()

    report = {
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "route": {"endpoint": config.model.endpoint, "model": config.model.model,
                  "temperature": config.model.temperature,
                  "seed": config.model.seed, "max_tokens": config.model.max_tokens},
        "determinism": det,
        "recall": recall_metrics(recall_records),
        "false_positives": fp_metrics(clean_records),
        "calibration": severity_calibration(
            [r for r in recall_records + clean_records if not r.get("error")]),
        "confidence_calibration": confidence_calibration(
            [r for r in recall_records + clean_records if not r.get("error")]),
        # Cycle-1: real_calls counts attempted model calls (failed attempts
        # still cost sockets/time); failed_calls breaks out the error share
        # so a dead route no longer reads as full-measure spend. Vote-aware:
        # every sample is a real call (determinism lane = runs * cases *
        # vote_n, cache lane counts per slot); a fully failed record cost
        # all vote_n of its samples (partial sample failures inside a
        # successful vote are invisible here — the same approximation class
        # as multi-route fallback attempts).
        "call_budget": {"real_calls": cache.real_calls + runs * len(sample_ids) * vote_n,
                        "failed_calls": (det["errors"] + sum(
                            1 for r in recall_records + clean_records
                            if r.get("error"))) * vote_n,
                        "cache_hits": cache.hits, "cache_misses": cache.misses},
        "records": {"determinism": det_records, "recall": recall_records,
                    "clean": clean_records},
        # Transport-real evidence: one record per actual HTTP call observed.
        "transport_records": collector.records,
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "reliability-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    return report


def format_summary(report: dict) -> str:
    lines = []
    r = report["route"]
    lines.append(f"# fl4write reliability report ({r['model']} @ {r['endpoint']}, "
                 f"T={r['temperature']}, seed={r['seed']})")
    d = report["determinism"]
    lines.append(f"\n## Determinism (verdict-signature agreement, N runs/case)\n"
                 f"- mean agreement: {d['mean_agreement']:.1%}  "
                 f"(min across cases: {d['min_agreement']:.1%}, "
                 f"{d['total_runs']} runs, errors: {d['errors']})")
    for cid, c in d["per_case"].items():
        lines.append(f"  - {cid}: {c['agreement_rate']:.1%} agreement, "
                     f"{c['distinct_signatures']} distinct signatures, "
                     f"{c['flag_flips']} flag flips, {c['severity_flips']} severity flips")
    rc = report["recall"]
    lines.append(f"\n## Planted-defect recall\n- any-finding: {rc['recall_any']:.1%}  "
                 f"actionable (Crit/Major): {rc['recall_actionable']:.1%}")
    for cls, c in rc["per_class"].items():
        _, any_n, act_n = _caught_counts(c)
        lines.append(f"  - {cls}: any={any_n}/{c.get('cases', 0)} "
                     f"actionable={act_n}/{c.get('cases', 0)}")
    fp = report["false_positives"]
    lines.append(f"\n## False positives (clean diffs)\n- noise: {fp['noise_fp_rate']:.1%}  "
                 f"actionable defect-claims: {fp['actionable_fp_rate']:.1%} "
                 f"({fp['clean_cases']} clean cases; flagged: {', '.join(fp['flagged_case_ids']) or 'none'})")
    if fp.get("law_note_rate") is not None:
        lines.append(f"- coverage-only law notes (excluded from actionable bar): "
                     f"{fp['law_note_rate']:.1%}")
    cal = report["calibration"]
    lines.append(f"\n## Honest uncertainty\n- numeric confidence emitted by product: "
                 f"{cal['confidence_field_emitted']}")
    cc = report.get("confidence_calibration") or {}
    if cc.get("n_with_confidence"):
        lines.append(f"- confidence ECE (5 bins, path-level labels): {cc['ece']}  "
                     f"(emitted on {cc['n_with_confidence']}/{cc['n_findings']} "
                     f"findings, rate {cc['emitted_rate']:.1%})")
        for b in cc.get("bins", []):
            if b["n"]:
                lines.append(f"  - [{b['lo']:.1f},{b['hi']:.1f}]: n={b['n']} "
                             f"mean_conf={b['mean_confidence']} "
                             f"precision={b['empirical_precision']}")
    else:
        lines.append("- confidence ECE: NO confidence data (a --max-ece gate "
                     "cannot pass without it)")
    lines.append(f"- labeling: {cal['labeling']}")
    for sev, row in cal["table"].items():
        p = f"{row['precision']:.1%}" if row["precision"] is not None else "n/a"
        lines.append(f"  - {sev}: n={row['n']} tp={row['tp']} fp={row['fp']} precision={p}")
    lines.append(f"\ncall budget: {report['call_budget']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fl4write.reliability",
                                 description="measure review reliability "
                                 "(determinism / recall / FP / calibration)")
    ap.add_argument("--config", required=True, help="base repo yaml (review rules "
                    "+ severity vocab are taken from it)")
    ap.add_argument("--endpoint", help="override model endpoint")
    ap.add_argument("--model", help="override model name")
    ap.add_argument("--key-env", help="override key env var (empty string = no auth)")
    ap.add_argument("--temperature", type=float, help="override temperature")
    ap.add_argument("--max-tokens", type=int, help="override max_tokens")
    ap.add_argument("--seed", type=int, help="set a generation seed")
    ap.add_argument("--runs", type=int, default=5, help="determinism runs per case (>=2)")
    ap.add_argument("--vote-n", type=int, default=None,
                    help="override config.signature_vote_n (posted-findings "
                    "best-of-N modal; 1 = single-shot)")
    ap.add_argument("--out", default=None, help="report output dir (default "
                    "~/.fl4write/reliability)")
    ap.add_argument("--cache-dir", default=None, help="response cache dir "
                    "(default ~/.fl4write/rel-cache; 'none' disables)")
    ap.add_argument("--min-determinism", type=float, default=None,
                    help="regression gate: fail exit 1 below this agreement")
    ap.add_argument("--min-recall", type=float, default=None,
                    help="regression gate: fail exit 1 below this actionable recall")
    ap.add_argument("--max-actionable-fp", type=float, default=None,
                    help="regression gate: fail exit 1 above this actionable FP rate")
    ap.add_argument("--max-ece", type=float, default=None,
                    help="regression gate: fail exit 1 above this confidence ECE "
                    "(fail-closed: no confidence data = gate fails)")
    args = ap.parse_args(argv)
    if args.runs < 2:
        ap.error("--runs must be >= 2 (stability needs repeats)")

    config = build_config(args.config, _route_overrides(args))
    if args.vote_n is not None:
        config = config.model_copy(
            update={"signature_vote_n": max(1, min(9, args.vote_n))})
    out_dir = Path(args.out) if args.out else Path.home() / ".fl4write" / "reliability"
    cache_dir = None if (args.cache_dir == "none") else (
        Path(args.cache_dir) if args.cache_dir else Path.home() / ".fl4write" / "rel-cache")
    report = run_measurement(config, runs=args.runs, cache_dir=cache_dir,
                             out_dir=out_dir)
    print(format_summary(report))
    print(f"\nreport: {out_dir / 'reliability-report.json'}")

    failed_gates = []
    for lane, records in report["records"].items():
        errors = sum(1 for record in records
                     if record.get("error") or record.get("flag") == "error")
        if errors:
            failed_gates.append(f"{lane} measurement errors: {errors}/{len(records)}")
    if args.min_determinism is not None \
            and report["determinism"]["mean_agreement"] < args.min_determinism:
        failed_gates.append(f"determinism {report['determinism']['mean_agreement']:.1%} "
                            f"< {args.min_determinism:.1%}")
    if args.min_recall is not None \
            and report["recall"]["recall_actionable"] < args.min_recall:
        failed_gates.append(f"recall {report['recall']['recall_actionable']:.1%} "
                            f"< {args.min_recall:.1%}")
    if args.max_actionable_fp is not None \
            and report["false_positives"]["actionable_fp_rate"] > args.max_actionable_fp:
        failed_gates.append(f"actionable-FP {report['false_positives']['actionable_fp_rate']:.1%} "
                            f"> {args.max_actionable_fp:.1%}")
    if args.max_ece is not None:
        _ece = (report.get("confidence_calibration") or {}).get("ece")
        if _ece is None:
            failed_gates.append(f"confidence ECE: no confidence data "
                                f"(gate {args.max_ece} cannot pass fail-closed)")
        elif _ece > args.max_ece:
            failed_gates.append(f"confidence ECE {_ece} > {args.max_ece}")
    if failed_gates:
        print("RELIABILITY GATES FAILED: " + "; ".join(failed_gates), file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# transport observation — per-request evidence from actual HTTP calls
# ---------------------------------------------------------------------------

class TransportCollector:
    """Collects per-request transport observations from actual HTTP calls.

    Each record represents one real HTTP exchange that actually happened
    during the measurement run.  Records are never fabricated or
    synthesized post-hoc.  The collector intercepts urllib.request.urlopen
    to capture HTTP-level observables (status code, response Date header)
    alongside timing data.

    Usage::

        collector = TransportCollector()
        with collector:
            # ... run measurement (any urllib HTTP calls are captured) ...
        print(len(collector.records))  # one per actual HTTP exchange
    """

    def __init__(self):
        self.records: list[dict] = []
        self._seq: int = 0
        self._original_urlopen = None

    def __enter__(self):
        self._original_urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._intercept
        return self

    def __exit__(self, *exc_info):
        if self._original_urlopen is not None:
            urllib.request.urlopen = self._original_urlopen
            self._original_urlopen = None

    def _intercept(self, req, *args, **kwargs):
        """Intercept urllib.request.urlopen to capture transport evidence."""
        import uuid

        self._seq += 1
        call_seq = self._seq
        trace_id = str(uuid.uuid4())
        request_ts = (datetime.datetime.now(datetime.timezone.utc)
                      .isoformat())
        t0 = time.time()
        endpoint = getattr(req, 'full_url', str(req))

        record: dict[str, Any] = {
            "trace_id": trace_id,
            "call_seq": call_seq,
            "request_ts": request_ts,
            "endpoint": endpoint,
        }

        try:
            original = self._original_urlopen
            if original is None:
                raise RuntimeError("TransportCollector not entered")
            resp = original(req, *args, **kwargs)
            record["http_status"] = resp.status
            # Capture a small set of response headers for evidence.
            # Date proves server-observed time; others are useful context.
            _EVIDENCE_HEADERS = {'date', 'server', 'content-type',
                                 'x-request-id', 'x-trace-id'}
            record["response_headers"] = {
                k: v for k, v in resp.headers.items()
                if k.lower() in _EVIDENCE_HEADERS
            }
            record["success"] = True
            record["error"] = ""
            return resp
        except Exception as exc:
            record["success"] = False
            record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise
        finally:
            record["response_ts"] = (datetime.datetime.now(
                datetime.timezone.utc).isoformat())
            record["latency_s"] = round(time.time() - t0, 4)
            self.records.append(record)


# ---------------------------------------------------------------------------
# watchtower-route-receipt/v1 row builder (CS binding contract)
# ---------------------------------------------------------------------------

def _canonical_json_bytes(value):
    """watchtower-effective/v1 canonical JSON bytes (sorted keys, compact, UTF-8)."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _sha256_dict(value):
    """SHA-256 of canonical JSON of a value."""
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_file(path):
    """SHA-256 of a file (streamed, 1 MiB blocks)."""
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _save_artifact(content, path):
    """Write canonical JSON artifact to path; return its sha256 hex digest."""
    data = _canonical_json_bytes(content)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


_ROUTE_PATH = Path('/home/simon/exp/CHAMPION-ROUTE-HASH')


def _gather_local_serving_state():
    """Gather live serving state from the local machine.

    Reads systemd unit files, /proc, and the CHAMPION-ROUTE-HASH to
    produce the fields the watchtower detector requires.  Linux/systemd
    only.

    Fail-closed per the receipt contract: any probe failure returns
    ``{'unavailable': True, 'reason': ...}`` instead of raising — same
    stage reasons as the remote probe (route read / systemctl / /proc),
    and the same lenient weight resolution (unresolvable identities are
    skipped, matching gather_remote_serving_state).

    Returns dict with: runtime, manifest, manifest_sha256, endpoint,
    route_hash — or the unavailable dict.
    """
    import re
    import shlex
    import subprocess

    _CANON = 'watchtower-effective/v1'
    _UNIT = 'qwen27.service'

    try:
        route_text = _ROUTE_PATH.read_text().strip()
    except Exception as exc:
        return {'unavailable': True,
                'reason': f'route read failed: {exc}',
                'collected_from': 'local'}
    route_hash = route_text.split()[0]
    fields = dict(re.findall(r'(\w+)=([^\s]+)', route_text))
    endpoint = fields.get('route', 'unknown')

    try:
        props = subprocess.check_output(
            ['systemctl', 'show', _UNIT, '-p', 'FragmentPath',
             '-p', 'DropInPaths', '-p', 'MainPID'], text=True)
    except Exception as exc:
        return {'unavailable': True,
                'reason': f'systemctl failed: {exc}',
                'collected_from': 'local'}
    props = dict(line.split('=', 1) for line in props.splitlines())
    pid = int(props['MainPID'])
    if pid == 0:
        return {'unavailable': True,
                'reason': 'unit not running (MainPID=0)',
                'collected_from': 'local'}
    paths = [props['FragmentPath']] + shlex.split(props.get('DropInPaths', ''))
    files = []
    for p in paths:
        try:
            files.append((p, Path(p).read_text()))
        except Exception:
            pass

    proc = Path('/proc') / str(pid)
    try:
        live = (proc.joinpath('cmdline').read_bytes()
                .rstrip(b'\x00').decode().split('\x00'))
        stat = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        boot_id = (Path('/proc/sys/kernel/random/boot_id')
                   .read_text().strip())
        live_env = dict(
            x.split('=', 1)
            for x in proc.joinpath('environ').read_bytes()
                   .decode().split('\x00') if '=' in x)
    except Exception as exc:
        return {'unavailable': True,
                'reason': f'/proc read failed: {exc}',
                'collected_from': 'local'}
    env_keys = ['HOME', 'HSA_ENABLE_SDMA', 'HSA_XNACK',
                'LD_LIBRARY_PATH']
    env = {k: live_env.get(k) for k in env_keys}

    runtime = {
        'boot_id': boot_id,
        'pid': pid,
        'start_ticks': int(stat[19]),
        'argv_sha256': _sha256_dict(live),
        'executable_sha256': _sha256_file(proc / 'exe'),
        'environment_sha256': _sha256_dict(env),
    }

    def _model_identity(path):
        p = Path(path)
        match = re.search(
            r'/models--([^/]+)/snapshots/([0-9a-f]{40})/([^/]+)$',
            str(p))
        if not match or not p.is_file() or p.stat().st_size <= 0:
            return None
        return {'repository': match[1], 'revision': match[2],
                'file': match[3], 'size': p.stat().st_size}

    weights = []
    for flag in ('-m', '--mmproj'):
        try:
            weight = _model_identity(live[live.index(flag) + 1])
        except (IndexError, OSError, ValueError):
            weight = None
        if weight:
            weights.append(weight)

    try:
        manifest = {
            'canonicalization': _CANON, 'unit': _UNIT,
            'files': [{'path': p, 'sha256': hashlib.sha256(
                text.encode()).hexdigest()} for p, text in files],
            'argv': live, 'environment': env, 'weights': weights,
            'binary': {'path': live[0],
                        'sha256': _sha256_file(live[0])},
        }
        manifest_sha256 = _sha256_dict(manifest)
    except Exception as exc:
        return {'unavailable': True,
                'reason': f'manifest build failed: {exc}',
                'collected_from': 'local'}

    return {
        'runtime': runtime,
        'manifest': manifest,
        'manifest_sha256': manifest_sha256,
        'endpoint': endpoint,
        'route_hash': route_hash,
    }


def gather_remote_serving_state(host: str) -> dict:
    """Gather serving state from a remote host via SSH.

    Connects to *host* (must be reachable via ssh with no passphrase),
    executes the same /proc + systemctl probes that the local path uses,
    and returns an equivalent dict.  The remote host must be Linux/systemd.

    If the SSH connection fails or the remote probes fail, returns a
    dict with ``unavailable: True`` and ``reason`` — the receipt contract
    requires fail-closed on unavailable state.

    Returns dict with the same shape as _gather_local_serving_state(),
    or ``{'unavailable': True, 'reason': ..., 'host': host}``.
    """
    import subprocess

    _CANON = 'watchtower-effective/v1'
    _UNIT = 'qwen27.service'
    _ROUTE = str(_ROUTE_PATH)

    # Remote probe script — runs on the serving host, outputs JSON.
    # Collects boot_id, PID, start_ticks, cmdline, exe SHA, env SHA,
    # route hash/endpoint, and unit file paths + content.
    _PROBE = r'''
import hashlib, json, os, re, shlex, subprocess, sys
from pathlib import Path

try:
    route_text = Path("''' + _ROUTE + r'''").read_text().strip()
except Exception as e:
    json.dump({"unavailable": True, "reason": f"route read failed: {e}"}, sys.stdout)
    sys.exit(0)

route_hash = route_text.split()[0]
fields = dict(re.findall(r'(\w+)=([^\s]+)', route_text))
endpoint = fields.get("route", "unknown")

try:
    props = subprocess.check_output(
        ["systemctl", "show", "''' + _UNIT + r'''", "-p", "FragmentPath",
         "-p", "DropInPaths", "-p", "MainPID"], text=True)
except Exception as e:
    json.dump({"unavailable": True, "reason": f"systemctl failed: {e}"}, sys.stdout)
    sys.exit(0)

props = dict(line.split("=", 1) for line in props.splitlines())
pid = int(props["MainPID"])
if pid == 0:
    json.dump({"unavailable": True, "reason": "unit not running (MainPID=0)"}, sys.stdout)
    sys.exit(0)

proc = Path("/proc") / str(pid)
try:
    cmdline = proc.joinpath("cmdline").read_bytes().rstrip(b"\x00").decode().split("\x00")
    stat = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    environ_raw = proc.joinpath("environ").read_bytes().decode().split("\x00")
except Exception as e:
    json.dump({"unavailable": True, "reason": f"/proc read failed: {e}"}, sys.stdout)
    sys.exit(0)

live_env = dict(x.split("=", 1) for x in environ_raw if "=" in x)
env_keys = ["HOME", "HSA_ENABLE_SDMA", "HSA_XNACK", "LD_LIBRARY_PATH"]
env = {k: live_env.get(k) for k in env_keys}

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def sha256_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()

runtime = {
    "boot_id": boot_id,
    "pid": pid,
    "start_ticks": int(stat[19]),
    "argv_sha256": sha256_json(cmdline),
    "executable_sha256": sha256_file(proc / "exe"),
    "environment_sha256": sha256_json(env),
}

# Unit file manifest
paths = [props["FragmentPath"]] + shlex.split(props.get("DropInPaths", ""))
files = []
for p in paths:
    try:
        files.append((p, Path(p).read_text()))
    except Exception:
        pass

# Model weights from cmdline
def model_identity(path):
    p = Path(path)
    match = re.search(r"/models--([^/]+)/snapshots/([0-9a-f]{40})/([^/]+)$", str(p))
    if not match or not p.is_file() or p.stat().st_size <= 0:
        return None
    return {"repository": match[1], "revision": match[2], "file": match[3], "size": p.stat().st_size}

weights = []
for flag in ("-m", "--mmproj"):
    try:
        idx = cmdline.index(flag)
        w = model_identity(cmdline[idx + 1])
        if w:
            weights.append(w)
    except (ValueError, IndexError):
        pass

manifest = {
    "canonicalization": "''' + _CANON + r'''",
    "unit": "''' + _UNIT + r'''",
    "files": [{"path": p, "sha256": sha256_bytes(text.encode())} for p, text in files],
    "argv": cmdline,
    "environment": env,
    "weights": weights,
    "binary": {"path": cmdline[0], "sha256": sha256_file(cmdline[0])},
}
manifest_sha256 = sha256_json(manifest)

json.dump({
    "runtime": runtime,
    "manifest": manifest,
    "manifest_sha256": manifest_sha256,
    "endpoint": endpoint,
    "route_hash": route_hash,
}, sys.stdout)
'''

    # LAW 22 (SSH quoting): the probe body travels over STDIN, never as
    # `python3 -c <body>`.  SSH joins remote argv into a shell command
    # line, so the body is re-parsed by the remote shell and any multi-word
    # body breaks at runtime (verified 2026-10-15, three probes: single-line
    # argv repro -> bash syntax error; multi-line body -> SyntaxError at
    # import; same body via stdin -> rc=0 with real state).  `python3 -`
    # reads the program from stdin — nothing to quote, nothing to re-parse.
    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=no',
             host, 'python3', '-'],
            input=_PROBE, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {'unavailable': True,
                'reason': f'ssh connection failed: {exc}',
                'host': host}

    if result.returncode != 0:
        return {'unavailable': True,
                'reason': f'ssh probe exited {result.returncode}: {result.stderr[:200]}',
                'host': host}

    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {'unavailable': True,
                'reason': f'unparseable probe output: {exc}',
                'host': host}

    if state.get('unavailable'):
        state['host'] = host
        return state

    state['collected_from'] = host
    return state


def gather_serving_state(host: str | None = None) -> dict:
    """Gather serving state from local or remote host.

    *host=None* (default): local /proc+systemd collection — requires
    Linux with the serving unit running locally.
    *host* given: SSH-based remote collection from that host.

    Returns dict with runtime, manifest, manifest_sha256, endpoint,
    route_hash — or ``{'unavailable': True, 'reason': ...}`` if
    collection failed (fail-closed per receipt contract).
    """
    if host is None:
        return _gather_local_serving_state()
    return gather_remote_serving_state(host)


def check_identity_binding(serving_before: dict,
                           serving_after: dict) -> dict:
    """Check that before/after serving identity match (no restart).

    Compares boot_id and pid between before and after snapshots.
    Returns ``{'ok': True}`` if identity is stable, or
    ``{'ok': False, 'reason': ..., 'before': ..., 'after': ...}``
    if identity changed or either snapshot is unavailable.

    This is a pure function — it does not take any action, only
    reports the binding status for the receipt to carry.
    """
    if serving_before.get('unavailable'):
        return {
            'ok': False,
            'reason': f'before state unavailable: {serving_before.get("reason", "unknown")}',
            'before': serving_before,
            'after': serving_after,
        }
    if serving_after.get('unavailable'):
        return {
            'ok': False,
            'reason': f'after state unavailable: {serving_after.get("reason", "unknown")}',
            'before': serving_before,
            'after': serving_after,
        }

    before_rt = serving_before.get('runtime', {})
    after_rt = serving_after.get('runtime', {})

    if before_rt.get('boot_id') != after_rt.get('boot_id'):
        return {
            'ok': False,
            'reason': 'boot_id changed (system reboot during measurement)',
            'before_boot_id': before_rt.get('boot_id'),
            'after_boot_id': after_rt.get('boot_id'),
            'before': serving_before,
            'after': serving_after,
        }

    if before_rt.get('pid') != after_rt.get('pid'):
        return {
            'ok': False,
            'reason': 'pid changed (serving process restarted during measurement)',
            'before_pid': before_rt.get('pid'),
            'after_pid': after_rt.get('pid'),
            'before': serving_before,
            'after': serving_after,
        }

    return {'ok': True}


def build_receipt_row(report, report_path, serving_state_before,
                      serving_state_after, transport_records,
                      out_dir='', is_dry_run=False):
    """Build a watchtower-route-receipt/v1 compliant row plus artifacts.

    *report* is the dict produced by run_measurement().
    *serving_state_before* is the dict from gather_serving_state()
    captured BEFORE the measurement run began.
    *serving_state_after* is the dict from gather_serving_state()
    captured AFTER the measurement run completed.
    *transport_records* is the list of TransportCollector.records —
    one per actual HTTP call that happened during the run.
    *out_dir* controls where artifacts are written (default: beside
    *report_path*).  Artifacts land under ARTIFACT_ROOTS so the
    detector can load them.

    Returns (row_dict, artifact_paths_dict).

    Evidence contract:
    - Each request in evidence.requests corresponds to one actual HTTP
      call observed during the run (transport_records entries).
    - request_id is the trace_id generated at call initiation, bound to
      the actual HTTP exchange — never synthesized from run_id.
    - before_runtime and after_runtime are independently sampled serving
      identity snapshots, not duplicates of a single sample.
    """
    import time as _time
    import uuid as _uuid

    _SCHEMA = 'watchtower-route-receipt/v1'

    run_id = str(_uuid.uuid4())
    ts = _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())
    evaluator = {'name': 'fl4write-reliability', 'version': 'cycle-5'}
    artifacts_dir = (Path(out_dir) if out_dir
                     else Path(report_path).parent)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # --- scores (full report scores for the report artifact) ---
    scores = {}
    for k in ('determinism', 'recall', 'false_positives',
              'calibration', 'call_budget'):
        if k in report:
            scores[k] = report[k]

    # --- report artifact ---
    report_artifact = {
        'run_id': run_id,
        'evaluator': evaluator,
        'outcome': 'success',
        'scores': scores,
        'measured_at': report.get('measured_at', ts),
        'route': report.get('route', {}),
    }
    report_sha = _save_artifact(
        report_artifact,
        str(artifacts_dir / ('report-%s.json' % run_id[:12])))

    # --- corpus artifact ---
    from . import reliability_corpus as corpus
    corpus_data = {
        'defect_cases': [
            {'id': c['id'], 'title': c['title'],
             'defect_class': c.get('defect_class', '')}
            for c in corpus.DEFECT_CASES],
        'clean_cases': [
            {'id': c['id'], 'title': c['title']}
            for c in corpus.CLEAN_CASES],
    }
    corpus_sha = _save_artifact(
        corpus_data,
        str(artifacts_dir / ('corpus-%s.json' % run_id[:12])))

    # --- inputs artifact ---
    inputs_artifact = {
        'suite': {'name': 'fl4write-reliability', 'revision': 'main'},
        'parameters': {
            'runs': report.get('determinism', {}).get('total_runs', 0),
            'endpoint': report.get('route', {}).get('endpoint', ''),
            'model': report.get('route', {}).get('model', ''),
            'temperature': report.get('route', {}).get('temperature', 0),
        },
        'corpus_sha256': corpus_sha,
        'corpus': {
            'path': str(artifacts_dir
                         / ('corpus-%s.json' % run_id[:12])),
            'sha256': corpus_sha,
        },
    }
    inputs_sha = _save_artifact(
        inputs_artifact,
        str(artifacts_dir / ('inputs-%s.json' % run_id[:12])))

    # --- evidence artifact (transport-real, not synthesized) ---
    #
    # Build request records from actual transport observations.
    # Each entry in transport_records is one real HTTP call that happened
    # during the measurement run.  The trace_id was generated at call
    # initiation and is bound to the actual HTTP exchange — not derived
    # from the run_id.
    requests = []
    for tr in transport_records:
        requests.append({
            'request_id': tr['trace_id'],
            'call_seq': tr['call_seq'],
            'endpoint': tr['endpoint'],
            'request_ts': tr['request_ts'],
            'response_ts': tr['response_ts'],
            'latency_s': tr['latency_s'],
            'http_status': tr.get('http_status'),
            'response_headers': tr.get('response_headers', {}),
            'success': tr['success'],
            'error': tr.get('error', ''),
        })

    # --- identity binding check (fail-closed on changed identity) ---
    identity_check = check_identity_binding(
        serving_state_before, serving_state_after)

    # Gracefully handle unavailable serving states — the identity_binding
    # field carries the failure; the receipt builder must not crash.
    _UNAVAIL_MANIFEST = {'canonicalization': 'unavailable',
                         'unit': 'unavailable'}
    _UNAVAIL_RUNTIME = {'boot_id': 'unavailable', 'pid': 0}

    evidence_artifact = {
        'run_id': run_id,
        'inputs_sha256': inputs_sha,
        'report_sha256': report_sha,
        'manifest': serving_state_before.get('manifest', _UNAVAIL_MANIFEST),
        'before_runtime': serving_state_before.get('runtime', _UNAVAIL_RUNTIME),
        'after_runtime': serving_state_after.get('runtime', _UNAVAIL_RUNTIME),
        'identity_binding': identity_check,
        'requests': requests,
        'transport_summary': {
            'total_calls': len(requests),
            'successful_calls': sum(1 for r in requests if r['success']),
            'failed_calls': sum(1 for r in requests if not r['success']),
            'distinct_endpoints': sorted(set(
                r['endpoint'] for r in requests)),
            'first_request_ts': (requests[0]['request_ts']
                                 if requests else None),
            'last_response_ts': (requests[-1]['response_ts']
                                 if requests else None),
        },
    }
    evidence_sha = _save_artifact(
        evidence_artifact,
        str(artifacts_dir / ('evidence-%s.json' % run_id[:12])))

    # --- trend row (minus payload_sha256) ---
    # Use .get() for serving_state_before fields — unavailable state
    # has no route_hash/endpoint/manifest fields.
    row = {
        'schema': _SCHEMA,
        'outcome': 'success',
        'run_id': run_id,
        'producer': 'fl4write-reliability',
        'route_hash': serving_state_before.get('route_hash', 'unavailable'),
        'endpoint': serving_state_before.get('endpoint', 'unavailable'),
        'serving_manifest_sha256': serving_state_before.get(
            'manifest_sha256', 'unavailable'),
        'runtime': serving_state_before.get('runtime', _UNAVAIL_RUNTIME),
        'started_at': ts,
        'ts': ts,
        'evaluator': evaluator,
        'report': {
            'path': str(artifacts_dir
                         / ('report-%s.json' % run_id[:12])),
            'sha256': report_sha,
        },
        'inputs': {
            'path': str(artifacts_dir
                         / ('inputs-%s.json' % run_id[:12])),
            'sha256': inputs_sha,
        },
        'evidence': {
            'path': str(artifacts_dir
                         / ('evidence-%s.json' % run_id[:12])),
            'sha256': evidence_sha,
        },
    }
    # payload_sha256 = canonical SHA-256 of the row without itself
    row['payload_sha256'] = _sha256_dict(
        {k: v for k, v in row.items() if k != 'payload_sha256'})

    artifact_paths = {
        'report': str(artifacts_dir
                       / ('report-%s.json' % run_id[:12])),
        'inputs': str(artifacts_dir
                       / ('inputs-%s.json' % run_id[:12])),
        'evidence': str(artifacts_dir
                        / ('evidence-%s.json' % run_id[:12])),
        'corpus': str(artifacts_dir
                       / ('corpus-%s.json' % run_id[:12])),
    }
    return row, artifact_paths


if __name__ == "__main__":
    raise SystemExit(main())
