"""Reliability harness tests — offline by default (no keys, no network).

The METRIC functions are pure and pinned here with synthetic records; the
runner is exercised through analyzer.analyze with a canned model, proving
the harness drives the REAL product path (prompt build -> parse ->
grounding gates -> verdict record) without any live route. The live
lane (FL4WRITE_EVAL=1) re-measures against a real configured route.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fl4write import config as cfg
from fl4write import reliability as rel
from fl4write import reliability_corpus as corpus
from fl4write.reliability import (
    ResponseCache,
    determinism_metrics,
    fp_metrics,
    install_cached_call,
    recall_metrics,
    run_case,
    severity_calibration,
    verdict_signature,
)

_CORPUS_SRC = Path(corpus.__file__).read_text(encoding="utf-8")


def _offline_config() -> cfg.RepoConfig:
    return cfg.RepoConfig.model_validate({
        "repo": "eval/reliability",
        "forges": {"forgejo": {"role": "primary",
                               "api_base": "http://forge.local/api/v1",
                               "token_env": "EVAL_TOK"}},
        "model": {"endpoint": "http://m/v1/chat/completions", "model": "t",
                  "key_env": ""},
        "review": {"tests": "Changes to logic ship with tests; tests stay green.",
                   "security": "Flag injection, traversal, unsafe subprocess."},
        "severity_vocab": ["Critical", "Major", "Minor", "Nit"],
    })


# ---------------------------------------------------------------------------
# corpus integrity
# ---------------------------------------------------------------------------

class TestCorpus:
    def test_ids_unique(self):
        ids = [c["id"] for c in corpus.DEFECT_CASES + corpus.CLEAN_CASES]
        assert len(ids) == len(set(ids))

    def test_defect_classes_labeled_and_distinct(self):
        classes = [c["defect_class"] for c in corpus.DEFECT_CASES]
        assert len(classes) >= 10, "corpus must cover >= 10 defect classes"
        assert len(classes) == len(set(classes)), "one class label per case"
        for case in corpus.DEFECT_CASES:
            assert case["defect_class"], case["id"]

    def test_clean_cases_have_no_defect_label(self):
        assert len(corpus.CLEAN_CASES) >= 4
        assert all("defect_class" not in c for c in corpus.CLEAN_CASES)

    def test_determinism_sample_resolves_and_mixes_kinds(self):
        for cid in corpus.DETERMINISM_SAMPLE:
            corpus.case_by_id(cid)  # raises on a stale id
        kinds = ["defect" if "defect_class" in corpus.case_by_id(cid) else "clean"
                 for cid in corpus.DETERMINISM_SAMPLE]
        assert "defect" in kinds and "clean" in kinds
        assert len(corpus.DETERMINISM_SAMPLE) >= 3

    def test_diffs_are_realistic_and_groundable(self):
        from fl4write.analyzer import _diff_line_spans, _diff_path_texts
        for case in corpus.DEFECT_CASES + corpus.CLEAN_CASES:
            files, text = corpus.build_case_diff(case)
            assert files == set(case["files"])
            for path in files:
                assert f"diff --git a/{path} b/{path}" in text
                assert re.search(
                    rf"\+\+\+ b/{re.escape(path)}\n@@ -0,0 \+1,\d+ @@", text), path
            # grounding helpers must recover every path with a full span
            texts = _diff_path_texts(text)
            spans = _diff_line_spans(text)
            for path, code in case["files"].items():
                assert path in texts
                n_lines = len(code.splitlines())
                assert (1, n_lines) in spans.get(path, []), \
                    f"{case['id']}/{path}: spans {spans.get(path)} vs {n_lines} lines"

    def test_corpus_carries_no_calendar_dates(self):
        """LEARNINGS #63 discipline, extended to eval fixture data: the
        corpus is pure code and must survive any calendar rotation."""
        assert not re.search(r"20\d{2}-\d{2}-\d{2}", _CORPUS_SRC), \
            "fixture data must not embed ISO dates (time-rot tombstone)"

    def test_unknown_case_id_raises(self):
        with pytest.raises(KeyError):
            corpus.case_by_id("no-such-case")


# ---------------------------------------------------------------------------
# metric math on synthetic records
# ---------------------------------------------------------------------------

def _rec(cid, cls, sig, flag, action, sevs):
    return {"case_id": cid, "defect_class": cls, "kind": "defect" if cls else "clean",
            "signature": sig, "flag": flag, "actionable": action,
            "severity_counts": sevs, "findings": [], "error": ""}


SIG_A = [["general", "Major", "a.py", 2]]
SIG_B = [["general", "Minor", "a.py", 2]]
SIG_C = []


class TestDeterminismMetrics:
    def test_perfect_agreement(self):
        recs = [_rec("c", "cls", SIG_A, "defect", True, {"Major": 1}) for _ in range(5)]
        m = determinism_metrics(recs)
        assert m["mean_agreement"] == 1.0
        assert m["min_agreement"] == 1.0
        assert m["per_case"]["c"]["distinct_signatures"] == 1
        assert m["per_case"]["c"]["flag_flips"] == 0

    def test_one_divergent_run_of_five(self):
        recs = ([_rec("c", "cls", SIG_A, "defect", True, {"Major": 1})] * 4
                + [_rec("c", "cls", SIG_B, "defect", False, {"Minor": 1})])
        m = determinism_metrics(recs)
        assert m["per_case"]["c"]["agreement_rate"] == pytest.approx(0.8)
        assert m["per_case"]["c"]["distinct_signatures"] == 2
        assert m["per_case"]["c"]["flag_flips"] == 0  # both say defect
        assert m["per_case"]["c"]["severity_flips"] == 1

    def test_flag_flip_detected(self):
        recs = ([_rec("c", "cls", SIG_A, "defect", True, {"Major": 1})] * 3
                + [_rec("c", "cls", SIG_C, "clean", False, {})] * 2)
        m = determinism_metrics(recs)
        assert m["per_case"]["c"]["flag_flips"] == 2
        assert m["per_case"]["c"]["agreement_rate"] == pytest.approx(0.6)

    def test_aggregate_across_cases(self):
        recs = ([_rec("a", "cls", SIG_A, "defect", True, {"Major": 1})] * 5
                + [_rec("b", "cls", SIG_A, "defect", True, {"Major": 1})] * 3
                + [_rec("b", "cls", SIG_C, "clean", False, {})])
        m = determinism_metrics(recs)
        assert m["mean_agreement"] == pytest.approx((1.0 + 0.75) / 2)
        assert m["min_agreement"] == pytest.approx(0.75)
        assert m["total_runs"] == 9


class TestRecallMetrics:
    def test_per_class_catch_bars(self):
        recs = [
            _rec("c1", "sql-injection", SIG_A, "defect", True, {"Critical": 1}),
            _rec("c2", "resource-leak", SIG_B, "defect", False, {"Minor": 1}),
            _rec("c3", "exception-swallow", SIG_C, "clean", False, {}),
        ]
        m = recall_metrics(recs)
        assert m["recall_any"] == pytest.approx(2 / 3)
        assert m["recall_actionable"] == pytest.approx(1 / 3)
        assert m["per_class"]["sql-injection"]["caught_actionable"] is True
        assert m["per_class"]["resource-leak"]["caught_any"] is True
        assert m["per_class"]["exception-swallow"]["caught_any"] is False

    def test_multi_run_case_caught_when_any_run_flags(self):
        recs = [
            _rec("c1", "cls", SIG_C, "clean", False, {}),
            _rec("c1", "cls", SIG_A, "defect", True, {"Major": 1}),
        ]
        assert recall_metrics(recs)["per_class"]["cls"]["caught_any"] is True


class TestFpMetrics:
    def test_clean_rates(self):
        recs = [
            _rec("k1", "", SIG_C, "clean", False, {}),
            _rec("k2", "", SIG_B, "defect", False, {"Minor": 1}),
            _rec("k3", "", SIG_A, "defect", True, {"Major": 1}),
            _rec("k4", "", SIG_C, "clean", False, {}),
        ]
        m = fp_metrics(recs)
        assert m["clean_cases"] == 4
        assert m["noise_fp_rate"] == pytest.approx(0.5)
        assert m["actionable_fp_rate"] == pytest.approx(0.25)
        assert m["flagged_case_ids"] == ["k2", "k3"]


class TestSeverityCalibration:
    def test_precision_table(self):
        recs = [
            _rec("d1", "cls", SIG_A, "defect", True, {"Critical": 2, "Major": 1}),
            _rec("d2", "cls", SIG_B, "defect", False, {"Minor": 1, "Nit": 2}),
            _rec("k1", "", SIG_A, "defect", True, {"Critical": 1, "Major": 2, "Nit": 1}),
        ]
        cal = severity_calibration(recs)
        assert cal["confidence_field_emitted"] is False
        t = cal["table"]
        assert t["Critical"]["n"] == 3 and t["Critical"]["tp"] == 2
        assert t["Critical"]["precision"] == pytest.approx(2 / 3)
        assert t["Major"]["precision"] == pytest.approx(1 / 3)
        assert t["Nit"]["precision"] == pytest.approx(2 / 3)
        assert list(t.keys()) == ["Critical", "Major", "Minor", "Nit"]

    def test_no_confidence_field_in_product_schema(self):
        """Honest-uncertainty precondition, pinned: the Finding schema has
        no confidence field — severity is the only claim-strength signal
        the product emits, which is exactly what calibration measures."""
        from fl4write.models import Finding
        assert "confidence" not in Finding.model_fields


# ---------------------------------------------------------------------------
# verdict signature
# ---------------------------------------------------------------------------

class TestVerdictSignature:
    def test_sorted_and_stable(self):
        from fl4write.models import Finding
        fs = [
            Finding(rule_id="general", severity="Major", path="b.py", line=9,
                    message="m", proposal=""),
            Finding(rule_id="tests", severity="Critical", path="a.py", line=1,
                    message="m", proposal=""),
        ]
        assert verdict_signature(fs) == [
            ("general", "Major", "b.py", 9), ("tests", "Critical", "a.py", 1)]

    def test_message_wording_not_part_of_identity(self):
        from fl4write.models import Finding
        f1 = [Finding(rule_id="general", severity="Major", path="a.py", line=2,
                      message="wording one", proposal="")]
        f2 = [Finding(rule_id="general", severity="Major", path="a.py", line=2,
                      message="totally different wording", proposal="")]
        assert verdict_signature(f1) == verdict_signature(f2)


# ---------------------------------------------------------------------------
# honest cache
# ---------------------------------------------------------------------------

class TestResponseCache:
    def test_hit_miss_and_independence(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        from fl4write.config import ModelRoute
        route = ModelRoute(endpoint="http://m/v1/chat/completions", model="t")
        calls = []

        def fake(r, prompt, mode="pr", system=None):
            calls.append(prompt)
            return '{"findings": []}'

        monkeypatch.setattr(an, "_call_model", fake)
        cache = ResponseCache(tmp_path / "c")
        uninstall = install_cached_call(cache)
        try:
            assert an._call_model(route, "p1") == '{"findings": []}'
            assert an._call_model(route, "p1") == '{"findings": []}'  # hit
            assert an._call_model(route, "p2") == '{"findings": []}'  # miss
        finally:
            uninstall()
        assert len(calls) == 2  # p1 once (cached 2nd), p2 once
        assert cache.hits == 1 and cache.misses == 2
        assert an._call_model is fake  # uninstall restores

    def test_route_params_change_the_key(self, tmp_path):
        from fl4write.config import ModelRoute
        r1 = ModelRoute(endpoint="http://m/v1/chat/completions", model="t",
                        temperature=0.2)
        r2 = ModelRoute(endpoint="http://m/v1/chat/completions", model="t",
                        temperature=0.9)
        assert ResponseCache.key(r1, "s", "p") != ResponseCache.key(r2, "s", "p")


# ---------------------------------------------------------------------------
# offline runner through the REAL product path
# ---------------------------------------------------------------------------

_CANNED = (
    '{{"findings": [{{"rule_id": "general", "severity": "Major", '
    '"path": "{path}", "line": 1, "category": "Reliability", '
    '"message": "Arbitrary SQL executes from the unsanitized input in this '
    'change.", "proposal": "Parameterize the query."}}]}}'
)


class TestRunnerOffline:
    def test_run_case_drives_analyze_and_gates(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", tmp_path / "tel.jsonl")

        def fake(route, prompt, mode="pr", system=None):
            m = re.search(r"\+\+\+ b/(\S+)", prompt)
            assert m, "harness prompt must carry the diff"
            return _CANNED.format(path=m.group(1))

        monkeypatch.setattr(an, "_call_model", fake)
        case = corpus.case_by_id("sql-string-concat")
        rec = run_case(case, _offline_config())
        assert rec["case_id"] == "sql-string-concat"
        assert rec["flag"] == "defect" and rec["actionable"] is True
        assert rec["severity_counts"] == {"Major": 1}
        assert rec["signature"] == [["general", "Major", "users_query.py", 1]]

    def test_run_case_clean_when_model_finds_nothing(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", tmp_path / "tel.jsonl")
        monkeypatch.setattr(an, "_call_model",
                            lambda r, p, mode="pr", system=None: '{"findings": []}')
        rec = run_case(corpus.case_by_id("clean-pagination"), _offline_config())
        assert rec["flag"] == "clean" and rec["signature"] == []

    def test_measurement_call_budget_and_cache_reuse(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", tmp_path / "tel.jsonl")
        calls = []

        def fake(route, prompt, mode="pr", system=None):
            calls.append(1)
            m = re.search(r"\+\+\+ b/(\S+)", prompt)
            return _CANNED.format(path=m.group(1))

        monkeypatch.setattr(an, "_call_model", fake)
        config = _offline_config()
        cache_dir = tmp_path / "rel-cache"
        det_ids = corpus.DETERMINISM_SAMPLE
        n_cases = len(corpus.DEFECT_CASES) + len(corpus.CLEAN_CASES)

        r1 = rel.run_measurement(config, runs=2, determinism_ids=det_ids,
                                 cache_dir=cache_dir, out_dir=tmp_path / "out")
        # lane law: determinism always FRESH (2 x sample), recall/clean one
        # real call per case on first run
        assert len(calls) == 2 * len(det_ids) + n_cases
        assert r1["call_budget"]["cache_hits"] == 0
        assert r1["call_budget"]["real_calls"] == 2 * len(det_ids) + n_cases
        # second run: recall/clean fully cache-served, determinism fresh
        calls.clear()
        r2 = rel.run_measurement(config, runs=2, determinism_ids=det_ids,
                                 cache_dir=cache_dir, out_dir=tmp_path / "out2")
        assert len(calls) == 2 * len(det_ids)
        assert r2["call_budget"]["cache_hits"] == n_cases
        # budget counts the ALWAYS-fresh determinism lane too
        assert r2["call_budget"]["real_calls"] == 2 * len(det_ids)
        assert (tmp_path / "out" / "reliability-report.json").exists()
        report = json.loads((tmp_path / "out" / "reliability-report.json").read_text())
        assert report["determinism"]["mean_agreement"] == 1.0  # canned model is stable
        assert report["recall"]["recall_any"] == 1.0  # canned model flags everything
        assert report["false_positives"]["actionable_fp_rate"] == 1.0  # ...clean too
        assert report["calibration"]["table"]["Major"]["tp"] == n_cases - len(corpus.CLEAN_CASES)

    def test_measurement_records_model_unavailability(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", tmp_path / "tel.jsonl")

        def dead(route, prompt, mode="pr", system=None):
            raise RuntimeError("route down")

        monkeypatch.setattr(an, "_call_model", dead)
        report = rel.run_measurement(_offline_config(), runs=2,
                                     determinism_ids=["clean-pagination"],
                                     cache_dir=tmp_path / "c", out_dir=tmp_path / "o")
        det = report["determinism"]
        assert det["errors"] == 2 and det["per_case"]["clean-pagination"]["runs"] == 2
        assert all(r["flag"] == "error" for r in report["records"]["recall"])
        assert report["recall"]["recall_any"] == 0.0


# ---------------------------------------------------------------------------
# gates + config building
# ---------------------------------------------------------------------------

class TestConfigAndGates:
    def test_build_config_route_overrides(self, tmp_path):
        base = tmp_path / "base.yaml"
        base.write_text(
            "repo: eval/reliability\n"
            "forges:\n  forgejo: {role: primary, api_base: http://f/api/v1, token_env: T}\n"
            "model: {endpoint: http://orig/v1/chat/completions, model: orig}\n"
            "review: {tests: t}\n"
            "severity_vocab: [Critical, Major, Minor, Nit]\n",
            encoding="utf-8")
        cfgv = rel.build_config(base, {"endpoint": "http://champ/v1/chat/completions",
                                       "model": "Q", "temperature": 0.0})
        assert cfgv.model.endpoint == "http://champ/v1/chat/completions"
        assert cfgv.model.model == "Q"
        assert cfgv.model.temperature == 0.0

    def test_cli_gates_fail_closed(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(rel, "build_config", lambda *a, **k: object())
        monkeypatch.setattr(rel, "run_measurement", lambda *a, **k: {
            "route": {"endpoint": "e", "model": "m", "temperature": 0.2,
                      "seed": None, "max_tokens": 1},
            "determinism": {"mean_agreement": 0.5, "min_agreement": 0.5,
                            "total_runs": 6, "errors": 0, "per_case": {}},
            "recall": {"recall_actionable": 0.9, "recall_any": 0.9,
                       "per_class": {}},
            "false_positives": {"actionable_fp_rate": 0.1, "noise_fp_rate": 0.1,
                                "clean_cases": 5, "flagged_case_ids": []},
            "calibration": {"confidence_field_emitted": False,
                            "proxy": "severity", "labeling": "path-level",
                            "table": {}},
            "call_budget": {"real_calls": 0, "cache_hits": 0, "cache_misses": 0},
        })
        rc = rel.main(["--config", "x.yaml", "--min-determinism", "0.8"])
        assert rc == 1
        assert "determinism" in capsys.readouterr().err

    def test_cli_rejects_single_run(self, capsys):
        with pytest.raises(SystemExit):
            rel.main(["--config", "x.yaml", "--runs", "1"])


# ---------------------------------------------------------------------------
# live lane (opt-in, mirrors test_planted_diffs convention)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(__import__("os").environ.get("FL4WRITE_EVAL") != "1",
                    reason="live eval (needs a reachable configured route)")
class TestLiveMeasurement:
    def test_full_measurement_live(self, tmp_path):
        """One bounded live measurement against the configured route.
        Route overrides come from FL4WRITE_REL_* env vars so no repo
        config file (runner-visible) is added for the eval lane."""
        import os
        overrides = {}
        for env, key in (("FL4WRITE_REL_ENDPOINT", "endpoint"),
                         ("FL4WRITE_REL_MODEL", "model"),
                         ("FL4WRITE_REL_KEY_ENV", "key_env")):
            if os.environ.get(env):
                overrides[key] = os.environ[env]
        config = rel.build_config(os.environ.get(
            "FL4WRITE_EVAL_CONFIG", "fl4write.fl4write.yaml"), overrides or None)
        report = rel.run_measurement(config, runs=int(os.environ.get(
            "FL4WRITE_REL_RUNS", "5")), cache_dir=tmp_path / "cache",
            out_dir=tmp_path / "out")
        det = report["determinism"]
        assert det["errors"] == 0, f"route errors during measurement: {det}"
        assert det["total_runs"] >= 5 * len(corpus.DETERMINISM_SAMPLE)
        print(rel.format_summary(report))
