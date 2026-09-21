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
3. HONEST UNCERTAINTY — the product schema emits NO numeric confidence
   (Finding has no confidence field); severity is the only ordinal claim
   strength. Calibration is therefore measured as per-severity precision
   on labeled fixtures: P(finding is on truly-defective code | severity).
   Path-level labeling caveat: every grounded finding on a defect-case
   file counts TP, every finding on a clean-case file counts FP — an
   upper bound on precision for Nit/Minor style claims.

Re-usability: `python -m fl4write.reliability --config <yaml> [route
overrides] --runs 5` re-measures any time; recall/clean lanes cache
honestly (key = exact prompt + route params; parse + gates always re-run
locally), determinism lanes never cache. Optional regression gates
(--min-determinism/--min-recall/--max-actionable-fp) make reliability a
CI-able property once thresholds are set from a recorded baseline.

Call-budget law: determinism = len(DETERMINISM_SAMPLE) * runs fresh
calls; recall/clean = one call per case (cache-served after first run).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import reliability_corpus as corpus
from .analyzer import ModelUnavailable, _system_prompt, analyze
from .config import ModelRoute, RepoConfig, load_config
from .models import Finding, PullRequest

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
             "line": f.line, "message": f.message[:160]}
            for f in findings
        ],
    }


def verdict_signature(findings: list[Finding]) -> list[tuple[str, str, str, int]]:
    """The stable verdict identity: which findings, at what severity,
    anchored where. Message text is EXCLUDED (prose wording varies;
    the verdict a maintainer acts on is rule+severity+path+line)."""
    return sorted((f.rule_id, f.severity, f.path, f.line) for f in findings)


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
    """Wrap analyzer._call_model with the cache (None = pass-through).
    Returns an uninstall callable. Real-call accounting is exposed via
    cache.real_calls for budget receipts."""
    from . import analyzer as _an

    real = _an._call_model
    if cache is None:
        cache = ResponseCache(None)
    cache.real_calls = 0
    cache.hits = 0
    cache.misses = 0

    def cached(route: ModelRoute, prompt: str, mode: str = "pr",
               system: str | None = None) -> str:
        sys_prompt = system if system is not None else _system_prompt(mode)
        key = ResponseCache.key(route, sys_prompt, prompt)
        hit = cache.get(key)
        if hit is not None:
            cache.hits += 1
            return hit
        cache.misses += 1
        cache.real_calls += 1
        content = real(route, prompt, mode, system)
        cache.put(key, content)
        return content

    _an._call_model = cached

    def uninstall() -> None:
        _an._call_model = real

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
    differs from the modal one.
    """
    by_case: dict[str, list[dict]] = {}
    for r in records:
        by_case.setdefault(r["case_id"], []).append(r)
    per_case: dict[str, dict] = {}
    for case_id, runs in by_case.items():
        sigs = [tuple(map(tuple, r["signature"])) for r in runs]
        modal_sig, modal_n = Counter(sigs).most_common(1)[0]
        modal_flag = Counter(_flag_of(r) for r in runs).most_common(1)[0][0]
        modal_sev = Counter(_sev_key(r) for r in runs).most_common(1)[0][0]
        per_case[case_id] = {
            "runs": len(runs),
            "distinct_signatures": len(set(sigs)),
            "agreement_rate": modal_n / len(runs),
            "flag_flips": sum(1 for r in runs if _flag_of(r) != modal_flag),
            "severity_flips": sum(1 for r in runs if _sev_key(r) != modal_sev),
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
    """Catch rate per defect class; a case is caught at a bar when >=1 run
    (for multi-run cases: modal verdict, single-run: that run) has any
    finding / any actionable finding on the case."""
    by_class: dict[str, list[dict]] = {}
    for r in defect_records:
        by_class.setdefault(r["defect_class"], []).append(r)
    per_class = {}
    for cls, runs in sorted(by_class.items()):
        caught_any = any(r["flag"] == "defect" for r in runs)
        caught_actionable = any(r["actionable"] for r in runs)
        per_class[cls] = {
            "cases": len({r["case_id"] for r in runs}),
            "caught_any": caught_any,
            "caught_actionable": caught_actionable,
        }
    n = len(per_class)
    return {
        "per_class": per_class,
        "recall_any": sum(c["caught_any"] for c in per_class.values()) / n if n else 0.0,
        "recall_actionable": sum(c["caught_actionable"] for c in per_class.values()) / n if n else 0.0,
    }


def fp_metrics(clean_records: list[dict]) -> dict:
    by_case: dict[str, list[dict]] = {}
    for r in clean_records:
        by_case.setdefault(r["case_id"], []).append(r)
    n = len(by_case)
    noise = sum(any(r["flag"] == "defect" for r in runs) for runs in by_case.values())
    actionable = sum(any(r["actionable"] for r in runs) for runs in by_case.values())
    return {
        "clean_cases": n,
        "noise_fp_rate": noise / n if n else 0.0,
        "actionable_fp_rate": actionable / n if n else 0.0,
        "flagged_case_ids": sorted(cid for cid, runs in by_case.items()
                                   if any(r["flag"] == "defect" for r in runs)),
    }


def severity_calibration(records: list[dict]) -> dict:
    """Honest-uncertainty table. The product emits NO numeric confidence
    (Finding has no confidence field — that absence is finding #1).
    Severity is the claim-strength signal; its calibration = per-severity
    precision on labeled fixtures (defect-case finding = TP, clean-case
    finding = FP; path-level labeling, upper bound for Nit/Minor)."""
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
    return {
        "confidence_field_emitted": False,
        "proxy": "severity",
        "labeling": "path-level: defect-case finding = TP, clean-case finding = FP",
        "table": table,
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
    cached call per case (parse + gates always re-run in-process)."""
    sample_ids = determinism_ids or corpus.DETERMINISM_SAMPLE
    cache = ResponseCache(cache_dir)

    # --- determinism lane: bare calls, no wrapper installed at all ---
    det_records: list[dict] = []
    for cid in sample_ids:
        case = corpus.case_by_id(cid)
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
        "call_budget": {"real_calls": cache.real_calls + runs * len(sample_ids),
                        "cache_hits": cache.hits, "cache_misses": cache.misses},
        "records": {"determinism": det_records, "recall": recall_records,
                    "clean": clean_records},
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
        lines.append(f"  - {cls}: any={c['caught_any']} actionable={c['caught_actionable']}")
    fp = report["false_positives"]
    lines.append(f"\n## False positives (clean diffs)\n- noise: {fp['noise_fp_rate']:.1%}  "
                 f"actionable: {fp['actionable_fp_rate']:.1%} "
                 f"({fp['clean_cases']} clean cases; flagged: {', '.join(fp['flagged_case_ids']) or 'none'})")
    cal = report["calibration"]
    lines.append(f"\n## Honest uncertainty\n- numeric confidence emitted by product: "
                 f"{cal['confidence_field_emitted']} (severity is the only claim-strength signal)")
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
    args = ap.parse_args(argv)
    if args.runs < 2:
        ap.error("--runs must be >= 2 (stability needs repeats)")

    config = build_config(args.config, _route_overrides(args))
    out_dir = Path(args.out) if args.out else Path.home() / ".fl4write" / "reliability"
    cache_dir = None if (args.cache_dir == "none") else (
        Path(args.cache_dir) if args.cache_dir else Path.home() / ".fl4write" / "rel-cache")
    report = run_measurement(config, runs=args.runs, cache_dir=cache_dir,
                             out_dir=out_dir)
    print(format_summary(report))
    print(f"\nreport: {out_dir / 'reliability-report.json'}")

    failed_gates = []
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
    if failed_gates:
        print("RELIABILITY GATES FAILED: " + "; ".join(failed_gates), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
