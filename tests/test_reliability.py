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
    confidence_calibration,
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

    def test_outage_runs_do_not_score_stability(self):
        recs = [_rec("c", "cls", SIG_C, "clean", False, {}) for _ in range(5)]
        for r in recs:
            r.update(flag="error", signature=[], severity_counts={},
                     error="route down")
        m = determinism_metrics(recs)
        assert m["per_case"]["c"]["agreement_rate"] == 0.0
        assert m["mean_agreement"] == 0.0
        assert m["errors"] == 5

    def test_partial_outage_excluded_from_agreement(self):
        recs = ([_rec("c", "cls", SIG_A, "defect", True, {"Major": 1})] * 4)
        err = _rec("c", "cls", SIG_C, "clean", False, {})
        err.update(flag="error", signature=[], severity_counts={},
                   error="route down")
        m = determinism_metrics(recs + [err])
        assert m["per_case"]["c"]["agreement_rate"] == 1.0
        assert m["per_case"]["c"]["errors"] == 1


class TestRecallMetrics:
    def test_trend_row_uses_case_counts(self):
        report = {
            "route": {"model": "test", "endpoint": "http://test.invalid"},
            "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0},
            "recall": recall_metrics([
                _rec("hit", "cls", SIG_A, "defect", True, {"Major": 1}),
                _rec("miss", "cls", SIG_C, "clean", False, {}),
            ]),
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0},
            "calibration": {"table": {}},
        }
        row = rel.trend_row(report, "report.json")
        assert row["recall_any"] == "1/2"
        assert row["recall_actionable"] == "1/2"

    def test_trend_row_reads_legacy_boolean_reports_as_lower_bound(self):
        report = {
            "route": {"model": "test", "endpoint": "http://test.invalid"},
            "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0},
            "recall": {"per_class": {
                "cls": {"cases": 2, "caught_any": True, "caught_actionable": False},
            }},
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0},
            "calibration": {"table": {}},
        }
        row = rel.trend_row(report, "legacy.json")
        # boolean True only warrants "at least one caught", never the class
        assert row["recall_any"] == "1/2"
        assert row["recall_actionable"] == "0/2"

    def test_trend_row_falls_back_per_bar_for_partial_entries(self):
        row = rel.trend_row({
            "route": {"model": "t", "endpoint": "e"},
            "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0},
            "recall": {"per_class": {
                "cls": {"cases": 3, "caught_any_cases": 2, "caught_actionable": True},
            }},
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0},
            "calibration": {"table": {}},
        }, "partial.json")
        assert row["recall_any"] == "2/3"
        assert row["recall_actionable"] == "1/3"

    def test_trend_row_handles_empty_recall(self):
        row = rel.trend_row({
            "route": {"model": "t", "endpoint": "e"},
            "determinism": {"mean_agreement": 0.0, "min_agreement": 0.0},
            "recall": {"per_class": {}},
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0},
            "calibration": {"table": {}},
        }, "empty.json")
        assert row["recall_any"] == "0/0"
        assert row["severity_precision"] is None
        assert row["worst_severity_precision"] is None

    def test_trend_row_exposes_worst_severity(self):
        row = rel.trend_row({
            "route": {"model": "t", "endpoint": "e"},
            "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0},
            "recall": {"per_class": {}},
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0},
            "calibration": {"table": {
                "Critical": {"n": 1, "tp": 1, "fp": 0},
                "Nit": {"n": 10, "tp": 0, "fp": 10},
            }},
        }, "skew.json")
        assert row["severity_precision"] == round(1 / 11, 4)
        assert row["worst_severity_precision"] == 0.0

    def test_trend_row_clamps_corrupt_counts(self):
        row = rel.trend_row({
            "route": {"model": "t", "endpoint": "e"},
            "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0},
            "recall": {"per_class": {
                "inflated": {"cases": 2, "caught_any_cases": 3,
                             "caught_actionable_cases": 9},
                "sizeless": {"caught_any": True, "caught_actionable": True},
            }},
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0},
            "calibration": {"table": {}},
        }, "rot.json")
        assert row["recall_any"] == "2/2"
        assert row["recall_actionable"] == "2/2"

    def test_format_summary_renders_legacy_boolean_reports(self):
        text = rel.format_summary({
            "route": {"model": "t", "endpoint": "e", "temperature": 0.0,
                      "seed": None, "max_tokens": 1},
            "determinism": {"mean_agreement": 1.0, "min_agreement": 1.0,
                            "total_runs": 2, "errors": 0, "per_case": {}},
            "recall": {"recall_any": 0.5, "recall_actionable": 0.0,
                       "per_class": {"cls": {"cases": 2, "caught_any": True,
                                             "caught_actionable": False}}},
            "false_positives": {"actionable_fp_rate": 0.0, "noise_fp_rate": 0.0,
                                "clean_cases": 1, "flagged_case_ids": []},
            "calibration": {"confidence_field_emitted": False, "proxy": "severity",
                            "labeling": "path-level", "table": {}},
            "call_budget": {},
        })
        assert "cls: any=1/2 actionable=0/2" in text

    def test_cadence_script_delegates_to_shared_trend_row(self):
        script = (Path(__file__).resolve().parents[1] / "tools/reliability-cadence.sh").read_text()
        assert "from fl4write.reliability import trend_row" in script
        assert "caught_any_cases" not in script
        assert "fcntl.flock" in script
        assert "MIN_DET=0.80" in script

    def test_cadence_script_preflights_env_before_state_capture(self):
        """Config env preflight names missing variables before any capture."""
        script = (Path(__file__).resolve().parents[1] / "tools/reliability-cadence.sh").read_text()
        # the env default + missing-var scan happen before the BEFORE capture
        assert script.index("FL4WRITE_MODEL_ENDPOINT") < \
            script.index("BEFORE_STATE_FILE=")
        assert "unset environment variable" in script

    def test_cadence_script_pins_serving_host_and_endpoint_semantics(self):
        """--serving-host '' forces local; derivation resolves to an
        authorized ssh alias or local collection; endpoint preflight runs
        before any state capture."""
        script = (Path(__file__).resolve().parents[1] / "tools/reliability-cadence.sh").read_text()
        # explicit flag (even empty) is authoritative
        assert "SERVING_HOST_GIVEN" in script
        # derivation maps to an ssh alias, never a bare tailnet IP
        assert 'FL4WRITE_SERVING_SSH:-nucbox' in script
        assert "cannot derive an authorized ssh target" in script
        # endpoint reachability is preflighted before state capture
        assert script.index("_ep_code=") < script.index("BEFORE_STATE_FILE=")
        # remote ssh target is preflighted before state capture
        assert script.index("BatchMode=yes") < script.index("BEFORE_STATE_FILE=")

    def test_missed_case_is_not_hidden_by_same_class_hit(self):
        recs = [
            _rec("hit", "cls", SIG_A, "defect", True, {"Major": 1}),
            _rec("miss", "cls", SIG_C, "clean", False, {}),
        ]
        m = recall_metrics(recs)
        assert m["recall_any"] == 0.5
        assert m["recall_actionable"] == 0.5
        assert m["per_class"]["cls"]["caught_any_cases"] == 1
        assert m["per_class"]["cls"]["caught_actionable_cases"] == 1

    def test_cases_have_equal_weight_despite_class_size_and_repeats(self):
        recs = [
            _rec("hit", "cls", SIG_A, "defect", True, {"Major": 1}),
            _rec("hit", "cls", SIG_A, "defect", True, {"Major": 1}),
            _rec("minor", "cls", SIG_B, "defect", False, {"Minor": 1}),
            _rec("miss", "other", SIG_C, "clean", False, {}),
        ]
        m = recall_metrics(recs)
        assert m["recall_any"] == pytest.approx(2 / 3)
        assert m["recall_actionable"] == pytest.approx(1 / 3)
        assert m["per_class"]["cls"]["cases"] == 2

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

    def test_outage_is_missing_data_not_clean(self):
        recs = [_rec("k1", "", SIG_C, "clean", False, {}) for _ in range(3)]
        for r in recs:
            r.update(flag="error", error="route down")
        m = fp_metrics(recs)
        assert m["clean_cases"] == 0
        assert m["errors"] == 3
        assert m["actionable_fp_rate"] == 0.0


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

    def test_confidence_field_optional_and_bounded(self):
        """Honest-uncertainty surface (reliability queue 2026-09-17 item 2).
        REGRESSION pin — FAILS PRE-FIX: the Finding schema carried NO
        confidence field. Optional: absent means "no calibrated claim",
        never an invented default; bounds [0,1] enforced by the model."""
        from fl4write.models import Finding
        assert "confidence" in Finding.model_fields
        f = Finding(rule_id="general", severity="Major", path="a.py", line=1,
                    message="m")
        assert f.confidence is None  # absent, not 0.0 / 0.5 / 1.0
        f2 = Finding(rule_id="general", severity="Major", path="a.py", line=1,
                     message="m", confidence=0.9)
        assert f2.confidence == pytest.approx(0.9)
        with pytest.raises(Exception):
            Finding(rule_id="general", severity="Major", path="a.py", line=1,
                    message="m", confidence=1.5)
        with pytest.raises(Exception):
            Finding(rule_id="general", severity="Major", path="a.py", line=1,
                    message="m", confidence=-0.1)

    def test_confidence_not_part_of_verdict_identity(self):
        """Confidence is claim STRENGTH, not verdict identity — the
        signature-vote must not treat a confidence flip as a different
        verdict (that would re-introduce instability the vote exists to
        kill)."""
        from fl4write.models import Finding
        f1 = [Finding(rule_id="general", severity="Major", path="a.py", line=2,
                      message="m", confidence=0.9)]
        f2 = [Finding(rule_id="general", severity="Major", path="a.py", line=2,
                      message="m", confidence=0.4)]
        f3 = [Finding(rule_id="general", severity="Major", path="a.py", line=2,
                      message="m")]
        assert verdict_signature(f1) == verdict_signature(f2) == verdict_signature(f3)


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
        # single-shot here: this test pins the BUDGET LAW arithmetic
        # (runs x samples + cases), which the vote multiplies — the
        # vote-aware version is pinned in test_vote_budget_and_slot_cache
        config = _offline_config().model_copy(update={"signature_vote_n": 1})
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
        budget = report["call_budget"]
        assert budget["failed_calls"] == budget["real_calls"] > 0

    def test_stale_determinism_id_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rel, "_guarded_run",
                            lambda case, config: _rec(case["id"], "", SIG_C,
                                                      "clean", False, {}))
        report = rel.run_measurement(_offline_config(), runs=2,
                                     determinism_ids=["no-such-case"],
                                     cache_dir=tmp_path / "c", out_dir=tmp_path / "o")
        det = report["determinism"]
        assert det["errors"] == 2
        assert det["mean_agreement"] == 0.0
        rec = report["records"]["determinism"][0]
        assert rec["flag"] == "error" and "no-such-case" in rec["error"]


# ---------------------------------------------------------------------------
# gates + config building
# ---------------------------------------------------------------------------

class TestConfigAndGates:
    def test_cli_recall_gate_catches_miss_within_class(self, monkeypatch, tmp_path, capsys):
        cases = [dict(case, defect_class="shared") for case in corpus.DEFECT_CASES[:2]]
        monkeypatch.setattr(corpus, "DEFECT_CASES", cases)
        monkeypatch.setattr(corpus, "DETERMINISM_SAMPLE", [cases[0]["id"]])
        monkeypatch.setattr(rel, "build_config", lambda *a, **k: _offline_config())

        def sample(case, config):
            cls = case.get("defect_class", "")
            caught = bool(cls) and case["id"] != cases[1]["id"]
            return _rec(case["id"], cls, SIG_A if caught else SIG_C,
                        "defect" if caught else "clean", caught, {"Major": 1} if caught else {})

        monkeypatch.setattr(rel, "_guarded_run", sample)
        rc = rel.main(["--config", "unused.yaml", "--runs", "2", "--out", str(tmp_path),
                       "--cache-dir", "none", "--min-recall", "0.75"])
        assert rc == 1
        assert "recall 50.0% < 75.0%" in capsys.readouterr().err

    @pytest.mark.parametrize("failed_lane", ["determinism", "recall", "clean", None])
    def test_cli_rejects_measurement_errors_despite_passing_metrics(
            self, monkeypatch, tmp_path, capsys, failed_lane):
        monkeypatch.setattr(rel, "build_config", lambda *a, **k: _offline_config())
        calls = 0
        det_calls = 2 * len(corpus.DETERMINISM_SAMPLE)

        def sample(case, config):
            nonlocal calls
            calls += 1
            cls = case.get("defect_class", "")
            lane = "determinism" if calls <= det_calls else ("recall" if cls else "clean")
            record = _rec(case["id"], cls, SIG_A if cls else SIG_C,
                          "defect" if cls else "clean", bool(cls),
                          {"Major": 1} if cls else {})
            # Determinism errors hit only the first run of each pair, so the
            # surviving runs still agree (metrics stay green, isolating the
            # error gate); one failed recall/clean case likewise leaves the
            # configured numeric bars green.
            det_err = lane == "determinism" and (calls % 2 == 1)
            if lane == failed_lane and (det_err or calls == det_calls + 1
                                        or calls == det_calls + len(corpus.DEFECT_CASES) + 1):
                record.update(flag="error", actionable=False, signature=[],
                              severity_counts={}, error="route unavailable")
            return record

        monkeypatch.setattr(rel, "_guarded_run", sample)
        rc = rel.main(["--config", "unused.yaml", "--runs", "2",
                       "--out", str(tmp_path), "--cache-dir", "none",
                       "--min-determinism", "0.9", "--min-recall", "0.9",
                       "--max-actionable-fp", "0.1"])
        report = json.loads((tmp_path / "reliability-report.json").read_text())
        assert report["determinism"]["mean_agreement"] >= 0.9
        assert report["recall"]["recall_actionable"] >= 0.9
        assert report["false_positives"]["actionable_fp_rate"] <= 0.1
        assert rc == (1 if failed_lane else 0)
        assert ("measurement errors" in capsys.readouterr().err) == bool(failed_lane)

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
            "records": {"determinism": [], "recall": [], "clean": []},
        })
        rc = rel.main(["--config", "x.yaml", "--min-determinism", "0.8"])
        assert rc == 1
        assert "determinism" in capsys.readouterr().err

    def test_cli_rejects_single_run(self, capsys):
        with pytest.raises(SystemExit):
            rel.main(["--config", "x.yaml", "--runs", "1"])


# ---------------------------------------------------------------------------
# signature-vote (reliability queue item 1) — anchor-flip class killed
# ---------------------------------------------------------------------------

_VOTE_DIFF = (
    "diff --git a/impl.py b/impl.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def f(x):\n"
    "+    return x\n"
    "+\n"
    "diff --git a/test_impl.py b/test_impl.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def test_f():\n"
    "+    assert f(1) == 1\n"
    "+\n"
)
_VOTE_FILES = {"impl.py", "test_impl.py"}


def _anchor_canned(path):
    return _CANNED.format(path=path)


class TestSignatureVote:
    """REGRESSION pins — FAIL PRE-FIX (analyze() was single-shot and posted
    whatever sample came first; there was no vote_n). The measured 09-17
    anchor-flip class: same rule+severity, path flipping between impl and
    test files at T=0.0 (4/5 vs 1/5). Best-of-N modal posting must kill it
    deterministically."""

    @staticmethod
    def _scripted(script):
        state = {"i": 0}

        def fake(route, prompt, mode="pr", system=None):
            resp = script[state["i"] % len(script)]
            state["i"] += 1
            if isinstance(resp, Exception):
                raise resp
            return resp

        return fake

    @staticmethod
    def _pr():
        from fl4write.models import PullRequest
        return PullRequest(forge="forgejo", number=1, repo="eval/reliability",
                           head_sha="0" * 40)

    def test_modal_anchor_wins_over_flip_class(self, monkeypatch, tmp_path):
        """The measured 4/5-vs-1/5 flip, replayed: every posted verdict must
        carry the MODAL anchor (4/5 = the test file), across repeated
        voted calls. Single-shot analyze alternates and fails this."""
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", tmp_path / "tel.jsonl")
        script = [_anchor_canned("test_impl.py")] * 4 + [_anchor_canned("impl.py")]
        monkeypatch.setattr(an, "_call_model", self._scripted(script))
        config = _offline_config().model_copy(update={"signature_vote_n": 5})
        sigs = []
        for _ in range(5):
            doc = an.analyze(self._pr(), _VOTE_FILES, _VOTE_DIFF, config)
            sigs.append(verdict_signature(doc.findings))
        assert all(s == sigs[0] for s in sigs), "voted posting must be deterministic"
        assert [s[0][2] for s in sigs] == ["test_impl.py"] * 5  # modal anchor

    def test_tie_breaks_deterministically(self, monkeypatch, tmp_path):
        """2-vs-2 split inside one vote (vote_n=4): the tie must break the
        same way every time (lexicographically smallest signature) — a
        non-deterministic tie-break would move the instability, not kill
        it."""
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        script = [_anchor_canned("impl.py"), _anchor_canned("test_impl.py")]
        monkeypatch.setattr(an, "_call_model", self._scripted(script))
        config = _offline_config().model_copy(update={"signature_vote_n": 4})
        sigs = []
        for _ in range(3):
            doc = an.analyze(self._pr(), _VOTE_FILES, _VOTE_DIFF, config)
            sigs.append(verdict_signature(doc.findings))
        assert all(s == sigs[0] for s in sigs)
        assert sigs[0][0][2] == "impl.py"  # lexicographic tie-break

    def test_vote_n_1_is_byte_for_byte_single_shot(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        calls = []
        script = [_anchor_canned("impl.py"), _anchor_canned("test_impl.py")]
        scripted = self._scripted(script)

        def fake(route, prompt, mode="pr", system=None):
            calls.append(1)
            return scripted(route, prompt, mode, system)

        monkeypatch.setattr(an, "_call_model", fake)
        config = _offline_config().model_copy(update={"signature_vote_n": 1})
        d1 = an.analyze(self._pr(), _VOTE_FILES, _VOTE_DIFF, config)
        d2 = an.analyze(self._pr(), _VOTE_FILES, _VOTE_DIFF, config)
        assert len(calls) == 2  # exactly one sample per analyze
        assert d1.findings[0].path == "impl.py"
        assert d2.findings[0].path == "test_impl.py"  # raw sample passes through

    def test_partial_sample_failure_votes_over_survivors(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        script = [RuntimeError("route down"), RuntimeError("route down"),
                  _anchor_canned("impl.py"), _anchor_canned("impl.py"),
                  _anchor_canned("impl.py")]
        monkeypatch.setattr(an, "_call_model", self._scripted(script))
        config = _offline_config().model_copy(update={"signature_vote_n": 5})
        doc = an.analyze(self._pr(), _VOTE_FILES, _VOTE_DIFF, config)
        assert verdict_signature(doc.findings) == [("general", "Major", "impl.py", 1)]

    def test_all_samples_failing_fails_closed(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        from fl4write.analyzer import ModelUnavailable
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(an, "_call_model",
                            self._scripted([RuntimeError("route down")]))
        config = _offline_config().model_copy(update={"signature_vote_n": 3})
        with pytest.raises(ModelUnavailable):
            an.analyze(self._pr(), _VOTE_FILES, _VOTE_DIFF, config)

    def test_vote_budget_and_slot_cache(self, monkeypatch, tmp_path):
        """Vote-aware call budget + PER-SLOT cache replay: every vote sample
        is a real call (determinism = runs x samples x vote_n), and a
        cache re-measure replays N INDEPENDENT recorded samples per case —
        slot-blind caching would serve sample 1 N times and fabricate a
        unanimous vote."""
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        calls = []

        def fake(route, prompt, mode="pr", system=None):
            calls.append(1)
            m = re.search(r"\+\+\+ b/(\S+)", prompt)
            return _CANNED.format(path=m.group(1))

        monkeypatch.setattr(an, "_call_model", fake)
        config = _offline_config().model_copy(update={"signature_vote_n": 3})
        cache_dir = tmp_path / "rel-cache"
        det_ids = corpus.DETERMINISM_SAMPLE
        n_cases = len(corpus.DEFECT_CASES) + len(corpus.CLEAN_CASES)
        per_case_calls = (2 * len(det_ids) + n_cases) * 3
        r1 = rel.run_measurement(config, runs=2, determinism_ids=det_ids,
                                 cache_dir=cache_dir, out_dir=tmp_path / "out")
        assert len(calls) == per_case_calls
        assert r1["call_budget"]["real_calls"] == per_case_calls
        assert r1["call_budget"]["cache_hits"] == 0
        calls.clear()
        r2 = rel.run_measurement(config, runs=2, determinism_ids=det_ids,
                                 cache_dir=cache_dir, out_dir=tmp_path / "out2")
        assert len(calls) == 2 * len(det_ids) * 3  # determinism stays fresh
        assert r2["call_budget"]["cache_hits"] == n_cases * 3  # per-slot replay
        assert r2["call_budget"]["real_calls"] == 2 * len(det_ids) * 3


# ---------------------------------------------------------------------------
# honest uncertainty (queue item 2): confidence contract + ECE
# ---------------------------------------------------------------------------

def _conf_canned(path, conf_json):
    return ('{"findings": [{"rule_id": "general", "severity": "Major", '
            f'"path": "{path}", "line": 1, "category": "General", '
            '"message": "Arbitrary SQL executes from the unsanitized input.", '
            f'"proposal": "", "confidence": {conf_json}}}]}}')


class TestConfidenceContract:
    def test_prompt_carries_confidence_contract(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        prompts = []

        def fake(route, prompt, mode="pr", system=None):
            prompts.append(prompt)
            return '{"findings": []}'

        monkeypatch.setattr(an, "_call_model", fake)
        an.analyze(TestSignatureVote._pr(), _VOTE_FILES, _VOTE_DIFF,
                   _offline_config(), vote_n=1)
        assert any("CONFIDENCE CONTRACT" in p for p in prompts)
        assert any("honest probability" in p for p in prompts)

    def test_valid_confidence_preserved(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(an, "_call_model",
                            lambda r, p, mode="pr", system=None:
                            _conf_canned("impl.py", "0.85"))
        doc = an.analyze(TestSignatureVote._pr(), _VOTE_FILES, _VOTE_DIFF,
                         _offline_config(), vote_n=1)
        assert doc.findings[0].confidence == pytest.approx(0.85)

    @pytest.mark.parametrize("bad", ['"high"', "true", "1.5", "-0.2", "null"])
    def test_malformed_confidence_stripped_not_coerced(self, monkeypatch, tmp_path, bad):
        """A malformed confidence claim degrades to ABSENT — never coerced
        (bool True would silently become 1.0; '"high"' would crash or
        invent). The finding itself stays, judged on grounding alone."""
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(an, "_call_model",
                            lambda r, p, mode="pr", system=None:
                            _conf_canned("impl.py", bad))
        doc = an.analyze(TestSignatureVote._pr(), _VOTE_FILES, _VOTE_DIFF,
                         _offline_config(), vote_n=1)
        assert len(doc.findings) == 1  # grounding kept it
        assert doc.findings[0].confidence is None

    def test_run_case_records_confidence(self, monkeypatch, tmp_path):
        import fl4write.analyzer as an
        import fl4write.telemetry as telemetry
        monkeypatch.setenv("FL4WRITE_TELEMETRY", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(telemetry, "_STREAM", str(tmp_path / "tel.jsonl"))
        monkeypatch.setattr(an, "_call_model",
                            lambda r, p, mode="pr", system=None:
                            _conf_canned("users_query.py", "0.7"))
        rec = run_case(corpus.case_by_id("sql-string-concat"), _offline_config())
        assert rec["findings"][0]["confidence"] == pytest.approx(0.7)


class TestConfidenceCalibration:
    """ECE math, pinned on synthetic records (queue item 2: the CEO's
    honest-uncertainty bar becomes a NUMBER with a regression gate)."""

    def _rec(self, kind, confs):
        return {"kind": kind, "case_id": "x", "flag": "defect",
                "severity_counts": {"Major": len(confs)},
                "findings": [{"rule_id": "general", "severity": "Major",
                              "path": "a.py", "line": 1, "message": "m",
                              "confidence": c} for c in confs]}

    def test_ece_math(self):
        # two claims at 0.9: one true, one false -> bin precision 0.5
        # ECE = |0.5 - 0.9| = 0.4
        cc = confidence_calibration([self._rec("defect", [0.9]),
                                     self._rec("clean", [0.9])])
        assert cc["ece"] == pytest.approx(0.4)
        assert cc["n_with_confidence"] == 2 and cc["n_findings"] == 2
        assert cc["emitted_rate"] == 1.0
        top = cc["bins"][-1]
        assert top["n"] == 2 and top["mean_confidence"] == pytest.approx(0.9)
        assert top["empirical_precision"] == pytest.approx(0.5)

    def test_calibrated_claims_score_low_ece(self):
        # 0.9-confident claims true 9/10 times, 0.2-confident claims true
        # 1/5 times -> near-zero ECE (the definition of honest uncertainty)
        recs = [self._rec("defect", [0.9] * 9), self._rec("clean", [0.9]),
                self._rec("defect", [0.2]), self._rec("clean", [0.2] * 4)]
        cc = confidence_calibration(recs)
        assert cc["ece"] == pytest.approx(0.0)

    def test_absent_confidence_is_honest_gap_not_default(self):
        cc = confidence_calibration([self._rec("defect", [None, None])])
        assert cc["ece"] is None  # no data — never invented
        assert cc["emitted_rate"] == 0.0
        assert cc["n_findings"] == 2

    def test_confidence_one_lands_in_top_bin(self):
        cc = confidence_calibration([self._rec("defect", [1.0])])
        assert cc["bins"][-1]["n"] == 1
        # one claim at 1.0 that is true: precision 1/1 = 1.0, conf 1.0 -> ECE 0.0
        assert cc["ece"] == pytest.approx(0.0)
        assert cc["bins"][-1]["empirical_precision"] == 1.0

    def test_severity_calibration_reports_emission(self):
        recs = [self._rec("defect", [0.9])]
        assert severity_calibration(recs)["confidence_field_emitted"] is True
        assert severity_calibration([self._rec("defect", [None])])[
            "confidence_field_emitted"] is False


class TestFpLawNoteSplit:
    """Queue item 3 — actionable_fp_rate must measure DEFECT-FINDING ERROR;
    coverage-only testing-law claims ride their own bar (both actionable
    FPs in the 09-17 baseline were coverage claims on law-dirty fixtures)."""

    def _clean_rec(self, findings):
        return {"case_id": "k", "defect_class": "", "kind": "clean",
                "signature": [], "flag": "defect",
                "actionable": any(f.get("severity") in ("Critical", "Major")
                                  for f in findings),
                "severity_counts": {}, "findings": findings, "error": ""}

    def test_coverage_law_note_rides_own_bar(self):
        note = {"rule_id": "tests", "severity": "Major", "path": "a.py",
                "line": 1, "message": "No tests exercise this new logic."}
        m = fp_metrics([self._clean_rec([note])])
        assert m["actionable_fp_rate"] == 0.0   # not defect-finding error
        assert m["law_note_rate"] == 1.0
        assert m["noise_fp_rate"] == 1.0        # still a finding (any-severity bar)

    def test_defect_claim_still_counts(self):
        defect = {"rule_id": "general", "severity": "Major", "path": "a.py",
                  "line": 1,
                  "message": "Arbitrary SQL executes from the unsanitized input."}
        note = {"rule_id": "tests", "severity": "Major", "path": "a.py",
                "line": 1, "message": "No tests exercise this new logic."}
        m = fp_metrics([self._clean_rec([defect, note])])
        assert m["actionable_fp_rate"] == 1.0
        assert m["law_note_rate"] == 1.0

    def test_failing_test_claim_is_a_defect_claim(self):
        claim = {"rule_id": "tests", "severity": "Major", "path": "a.py",
                 "line": 1, "message": "The new test fails when the helper is called."}
        m = fp_metrics([self._clean_rec([claim])])
        assert m["actionable_fp_rate"] == 1.0
        assert m["law_note_rate"] == 0.0

    def test_legacy_records_without_findings_keep_old_bar(self):
        legacy = {"case_id": "k", "defect_class": "", "kind": "clean",
                  "signature": [], "flag": "defect", "actionable": True,
                  "severity_counts": {"Major": 1}, "findings": [], "error": ""}
        m = fp_metrics([legacy])
        assert m["actionable_fp_rate"] == 1.0


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
