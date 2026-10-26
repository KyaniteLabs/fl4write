"""Behaviour tests for the gatekeeper's parsing and decision rules.

Expected values derived by hand from fl4write/gatekeeper.py:
- _keep_sets: a non-dict or keep-not-a-list is None; rows need a real int
  line (bools are rejected — F12-A2); a row WITH a stripped rule_id keys
  by_rule, without it keeps the whole line; junk rows are skipped.
- _demotions: only rows whose severity is in the vocab survive, keyed
  (path, line, rule_id) with the rule copied verbatim (no strip).
- filter_findings: any model/parse failure — including an empty keep-set
  against findings and a keep-set matching zero findings — fails OPEN
  (all findings, dropped 0, failed True); demotion may only LOWER within
  the vocab, applies per (path,line,rule_id), falls back to a ruleless
  row only when exactly one finding holds the line, and never touches
  dropped findings.
"""
import json

import pytest

from fl4write import gatekeeper
from fl4write.models import Finding
from test_quality_tranche import make_config


def F(path="src/a.py", line=3, rule="general", sev="Major", msg="bug"):
    return Finding(path=path, line=line, rule_id=rule, severity=sev, message=msg)


class TestKeepSets:
    def test_non_dict_and_keep_not_list_are_none(self):
        assert gatekeeper._keep_sets(None) is None
        assert gatekeeper._keep_sets([{"keep": []}]) is None
        assert gatekeeper._keep_sets({"keep": "x"}) is None
        assert gatekeeper._keep_sets({}) is None

    def test_rule_row_and_line_row_split(self):
        line_only, by_rule = gatekeeper._keep_sets({
            "keep": [
                {"path": "a.py", "line": 1},                        # whole line
                {"path": "b.py", "line": 2, "rule_id": " r1 "},     # stripped rule
                {"path": "b.py", "line": 2},                        # whole line at same key
            ]
        })
        assert line_only == {("a.py", 1), ("b.py", 2)}
        assert by_rule == {("b.py", 2): {"r1"}}

    def test_bool_and_non_int_lines_are_skipped_f12a2(self):
        line_only, by_rule = gatekeeper._keep_sets({
            "keep": [
                {"path": "a.py", "line": True, "rule_id": "r"},
                {"path": "a.py", "line": "4"},
                {"path": "a.py", "line": 5},
                "junk",
                {"path": "  ", "line": 6, "rule_id": "r2"},         # stripped path, empty
            ]
        })
        assert line_only == {("a.py", 5)}
        assert by_rule == {("", 6): {"r2"}}

    def test_empty_keep_list_is_valid_empty_not_none(self):
        line_only, by_rule = gatekeeper._keep_sets({"keep": []})
        assert line_only == set() and by_rule == {}

    def test_legacy_keep_set_is_the_union(self):
        ks = gatekeeper._keep_set({"keep": [
            {"path": "a.py", "line": 1},
            {"path": "b.py", "line": 2, "rule_id": "r"},
        ]})
        assert ks == {("a.py", 1), ("b.py", 2)}
        assert gatekeeper._keep_set({"keep": "x"}) is None


VOCAB = ["Critical", "Major", "Minor", "Nit"]


class TestDemotions:
    def test_only_vocab_severities_survive(self):
        out = gatekeeper._demotions({
            "demote": [
                {"path": "a.py", "line": 1, "rule_id": "r", "severity": "Minor"},
                {"path": "a.py", "line": 2, "rule_id": "r", "severity": "NotAWord"},
            ]
        }, VOCAB)
        assert out == {("a.py", 1, "r"): "Minor"}

    def test_rule_id_is_copied_verbatim_no_strip(self):
        out = gatekeeper._demotions({
            "demote": [{"path": " p ", "line": 3, "rule_id": " r ", "severity": "Nit"}]
        }, VOCAB)
        assert out == {("p", 3, " r "): "Nit"}

    def test_bool_line_never_demotes_and_missing_demote_key_is_empty(self):
        assert gatekeeper._demotions({"demote": [{"path": "a", "line": True, "severity": "Nit"}]}, VOCAB) == {}
        assert gatekeeper._demotions({}, VOCAB) == {}
        assert gatekeeper._demotions({"demote": None}, VOCAB) == {}


class TestFilterFindings:
    def run(self, monkeypatch, findings, reply=None, error=None):
        calls = []

        def model(route, prompt, **kw):
            calls.append(prompt)
            if error:
                raise error
            return reply() if callable(reply) else reply

        monkeypatch.setattr(gatekeeper, "_call_model", model)
        kept, dropped, failed = gatekeeper.filter_findings(list(findings), make_config())
        return kept, dropped, failed, calls

    def test_empty_findings_never_call_the_model(self, monkeypatch):
        kept, dropped, failed, calls = self.run(monkeypatch, [], reply='{"keep": []}')
        assert (kept, dropped, failed, calls) == ([], 0, False, [])

    def test_subset_keep_exact_counts(self, monkeypatch):
        fs = [F(line=1), F(line=2), F(line=3)]
        reply = json.dumps({"keep": [{"path": "src/a.py", "line": 1}, {"path": "src/a.py", "line": 2}]})
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert [f.line for f in kept] == [1, 2]
        assert dropped == 1 and failed is False

    def test_line_row_keeps_every_finding_on_the_line(self, monkeypatch):
        fs = [F(line=7, rule="r1"), F(line=7, rule="r2"), F(line=8, rule="r1")]
        reply = json.dumps({"keep": [{"path": "src/a.py", "line": 7}]})
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert [f.rule_id for f in kept] == ["r1", "r2"]
        assert dropped == 1 and failed is False

    def test_rule_row_disambiguates_same_line_findings(self, monkeypatch):
        fs = [F(line=7, rule="r1"), F(line=7, rule="r2")]
        reply = json.dumps({"keep": [{"path": "src/a.py", "line": 7, "rule_id": "r2"}]})
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert [f.rule_id for f in kept] == ["r2"]
        assert dropped == 1 and failed is False

    def test_rule_mismatch_on_a_single_finding_line_keeps_it(self, monkeypatch):
        fs = [F(line=7, rule="r1")]
        reply = json.dumps({"keep": [{"path": "src/a.py", "line": 7, "rule_id": "nope"}]})
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert len(kept) == 1 and dropped == 0 and failed is False

    def test_fail_open_on_unparseable_keep(self, monkeypatch):
        fs = [F()]
        for reply in ('{"keep": {}}', 'not json at all', ''):
            kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
            assert kept == fs and failed is True
            assert dropped == 0

    def test_fail_open_on_model_exception(self, monkeypatch):
        fs = [F()]
        kept, dropped, failed, _ = self.run(monkeypatch, fs, error=RuntimeError("boom"))
        assert kept == fs and dropped == 0 and failed is True

    def test_fail_open_on_empty_keep_and_on_drop_all(self, monkeypatch):
        fs = [F(line=1), F(line=2)]
        for reply in ('{"keep": []}', '{"keep": [{"path": "other.py", "line": 9}]}'):
            kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
            assert len(kept) == 2 and failed is True and dropped == 0

    def test_demotion_only_lowers(self, monkeypatch):
        fs = [F(line=1, sev="Major"), F(line=2, sev="Minor")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}, {"path": "src/a.py", "line": 2}],
            "demote": [
                {"path": "src/a.py", "line": 1, "rule_id": "general", "severity": "Minor"},  # lower: applies
                {"path": "src/a.py", "line": 2, "rule_id": "general", "severity": "Critical"},  # raise: refused
            ],
        })
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert kept[0].severity == "Minor"
        assert kept[1].severity == "Minor"  # unchanged
        assert failed is False

    def test_demotion_to_same_severity_is_refused(self, monkeypatch):
        fs = [F(line=1, sev="Major")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}],
            "demote": [{"path": "src/a.py", "line": 1, "rule_id": "general", "severity": "Major"}],
        })
        kept, _, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert kept[0].severity == "Major" and failed is False

    def test_ruleless_demotion_applies_only_to_a_single_finding_line(self, monkeypatch):
        alone = [F(line=5, sev="Major")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 5}],
            "demote": [{"path": "src/a.py", "line": 5, "severity": "Nit"}],
        })
        kept, _, failed, _ = self.run(monkeypatch, alone, reply=reply)
        assert kept[0].severity == "Nit"

        twins = [F(line=5, rule="r1", sev="Major"), F(line=5, rule="r2", sev="Major")]
        reply2 = json.dumps({
            "keep": [{"path": "src/a.py", "line": 5}, {"path": "src/a.py", "line": 5, "rule_id": "r2"}],
            "demote": [{"path": "src/a.py", "line": 5, "severity": "Nit"}],
        })
        kept2, _, failed2, _ = self.run(monkeypatch, twins, reply=reply2)
        assert [f.severity for f in kept2] == ["Major", "Major"]  # ambiguous: no demotion
        assert failed2 is False

    def test_each_same_line_finding_gets_its_own_demotion(self, monkeypatch):
        fs = [F(line=4, rule="r1", sev="Major"), F(line=4, rule="r2", sev="Critical")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 4, "rule_id": "r1"},
                     {"path": "src/a.py", "line": 4, "rule_id": "r2"}],
            "demote": [
                {"path": "src/a.py", "line": 4, "rule_id": "r1", "severity": "Minor"},
                {"path": "src/a.py", "line": 4, "rule_id": "r2", "severity": "Minor"},
            ],
        })
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert dropped == 0 and failed is False
        assert sorted((f.rule_id, f.severity) for f in kept) == [("r1", "Minor"), ("r2", "Minor")]

    def test_dropped_findings_are_never_demoted(self, monkeypatch):
        fs = [F(line=1, sev="Major"), F(line=2, sev="Major")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}],
            "demote": [{"path": "src/a.py", "line": 2, "rule_id": "general", "severity": "Nit"}],
        })
        kept, dropped, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert dropped == 1
        assert fs[1].severity == "Major"  # the dropped one is untouched

    def test_off_vocab_demotion_is_ignored(self, monkeypatch):
        fs = [F(line=1, sev="Major")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}],
            "demote": [{"path": "src/a.py", "line": 1, "rule_id": "general", "severity": "Whatever"}],
        })
        kept, _, failed, _ = self.run(monkeypatch, fs, reply=reply)
        assert kept[0].severity == "Major"


class TestJunkOrderingAndMissingKeys:
    """junk rows must be skipped (continue), not fatal (break); missing keys
    fall back to empty strings whose exact values the matchers rely on."""

    def test_keep_junk_first_does_not_kill_later_rows(self):
        line_only, by_rule = gatekeeper._keep_sets({
            "keep": ["junk", {"line": True}, {"path": "a.py", "line": 1, "rule_id": "r"}]
        })
        assert line_only == set()
        assert by_rule == {("a.py", 1): {"r"}}

    def test_keep_row_without_path_lands_on_the_empty_path_key(self):
        line_only, _ = gatekeeper._keep_sets({"keep": [{"line": 4}]})
        assert line_only == {("", 4)}

    def test_demote_junk_and_bool_first_do_not_kill_later_rows(self):
        out = gatekeeper._demotions({
            "demote": [
                "junk",
                {"path": "z", "line": True, "severity": "Nit"},
                {"path": "b.py", "line": 2, "rule_id": "r", "severity": "Nit"},
                {"path": "c.py", "line": 3, "severity": "Nit"},
            ]
        }, VOCAB)
        assert out == {("b.py", 2, "r"): "Nit", ("c.py", 3, ""): "Nit"}

    def test_demote_row_without_rule_uses_the_empty_rule_key(self):
        out = gatekeeper._demotions({
            "demote": [{"path": "p.py", "line": 1, "severity": "Minor"}]
        }, VOCAB)
        assert out == {("p.py", 1, ""): "Minor"}


class TestPromptAndCallContract:
    def test_exact_prompt_scrub_limits_and_model_wiring(self, monkeypatch):
        from fl4write.law import SYSTEM_PROMPT_ADDENDUM
        cfg = make_config()
        # redaction-safe fillers: uniform char runs trip the scrubber's
        # defense categories, so alternate characters to probe ONLY the
        # length limits
        # scrub redacts any long UNSPACED run, so the limit probes use spaced
        # text the scrubber passes through (verified against scrub.inline)
        long_path = "src/" + "folder " * 30       # 214 chars -> masked identity
        long_msg = "word " * 26                   # 130 chars -> cut at 120
        long_rule = "rule one two " * 5           # 65 chars -> cut at 60
        fs = [F(path=long_path, line=9, rule=long_rule, msg=long_msg, sev="Major")]
        captured = {}

        def model(route, prompt, **kw):
            captured["route"] = route
            captured["prompt"] = prompt
            captured.update(kw)
            return json.dumps({"keep": [{"path": "__fl4write_path_0__", "line": 9}]})

        monkeypatch.setattr(gatekeeper, "_call_model", model)
        kept, dropped, failed = gatekeeper.filter_findings(fs, cfg)

        assert failed is False and dropped == 0
        # the route is the config's own model object
        assert captured["route"] == cfg.model
        # the gatekeeper's OWN system contract + the law addendum, joined
        assert captured["system"] == (
            gatekeeper._GATEKEEPER_SYSTEM + "\n\n" + SYSTEM_PROMPT_ADDENDUM
        )
        p = captured["prompt"]
        assert p.startswith("REPO SEVERITY VOCAB: ['Critical', 'Major', 'Minor', 'Nit']")
        # finding rows: masked identity, rule cut at 60, message cut at 120
        assert f"- [Major] __fl4write_path_0__:9 ({('rule one two ' * 5)[:60]}): {('word ' * 26)[:120]}\n" in p
        # display block maps the masked key to the path cut at 200 chars
        assert f"__fl4write_path_0__ => {('src/' + 'folder ' * 30)[:200]}\n" in p
        assert p.endswith("JSON keep list:")

    def test_duplicate_paths_do_not_stop_identity_mapping(self, monkeypatch):
        fs = [F(path="a.py", line=1), F(path="a.py", line=2), F(path="b.py", line=3)]
        reply = json.dumps({"keep": [{"path": "b.py", "line": 3}]})
        kept, dropped, failed, _ = TestFilterFindings().run(monkeypatch, fs, reply=reply)
        assert failed is False
        assert [f.path for f in kept] == ["b.py"]

    def test_bare_list_reply_fails_open_not_accidentally_parsing(self, monkeypatch):
        fs = [F(line=1)]
        reply = '[{"keep": [{"path": "src/a.py", "line": 1}]}]'
        kept, dropped, failed, _ = TestFilterFindings().run(monkeypatch, fs, reply=reply)
        assert failed is True and dropped == 0 and len(kept) == 1


class TestLogLines:
    def test_demoted_log_line_is_exact(self, monkeypatch, caplog):
        import logging
        fs = [F(line=1, sev="Major")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}],
            "demote": [{"path": "src/a.py", "line": 1, "rule_id": "general", "severity": "Minor"}],
        })
        with caplog.at_level(logging.INFO, logger="fl4write.gatekeeper"):
            kept, _, failed = None, None, None
            kept, _, failed = TestFilterFindings().run(monkeypatch, fs, reply=reply)[0:3]
        assert failed is False
        msgs = [r.getMessage() for r in caplog.records]
        assert "gatekeeper demoted src/a.py:1 (general) Major->Minor" in msgs

    def test_same_severity_demotion_is_refused_and_unlogged(self, monkeypatch, caplog):
        import logging
        fs = [F(line=1, sev="Major")]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}],
            "demote": [{"path": "src/a.py", "line": 1, "rule_id": "general", "severity": "Major"}],
        })
        with caplog.at_level(logging.INFO, logger="fl4write.gatekeeper"):
            kept, _, failed = TestFilterFindings().run(monkeypatch, fs, reply=reply)[0:3]
        assert failed is False and kept[0].severity == "Major"
        assert not any("demoted" in r.getMessage() for r in caplog.records)

    def test_fail_open_warning_carries_the_reason(self, monkeypatch, caplog):
        import logging
        fs = [F(line=1)]
        with caplog.at_level(logging.WARNING, logger="fl4write.gatekeeper"):
            TestFilterFindings().run(monkeypatch, fs, reply='{"keep": {}}')
        warns = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("gatekeeper unavailable (fail-open, posting all): keep-list unusable: ")
                   for m in warns)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="fl4write.gatekeeper"):
            TestFilterFindings().run(
                monkeypatch, fs,
                reply=json.dumps({"keep": [{"path": "other.py", "line": 9}]}))
        warns2 = [r.getMessage() for r in caplog.records]
        assert any(m.endswith(": keep-list matched zero findings; refusing drop-all as parse failure")
                   for m in warns2)

    def test_duplicate_key3_does_not_stop_later_demotions(self, monkeypatch):
        fs = [
            F(line=1, rule="r", sev="Major"),
            F(line=1, rule="r", sev="Major"),   # duplicate identity
            F(line=2, rule="r3", sev="Major"),
        ]
        reply = json.dumps({
            "keep": [
                {"path": "src/a.py", "line": 1, "rule_id": "r"},
                {"path": "src/a.py", "line": 1, "rule_id": "r"},
                {"path": "src/a.py", "line": 2, "rule_id": "r3"},
            ],
            "demote": [
                {"path": "src/a.py", "line": 1, "rule_id": "r", "severity": "Minor"},
                {"path": "src/a.py", "line": 2, "rule_id": "r3", "severity": "Minor"},
            ],
        })
        kept, dropped, failed, _ = TestFilterFindings().run(monkeypatch, fs, reply=reply)
        assert failed is False
        # the applied-set demotes each identity ONCE: the duplicate finding
        # keeps Major by design; a break at the duplicate would also strand
        # the line-2 demotion, which is what this asserts against
        severities = sorted((f.line, f.severity) for f in kept)
        assert severities == [(1, "Major"), (1, "Minor"), (2, "Minor")]


class TestTelemetryAndDropLogs:
    def _patch(self, monkeypatch, reply):
        import fl4write.telemetry as telemetry
        events = []

        def emit(event, **kw):
            events.append((event, kw))

        monkeypatch.setattr(telemetry, "emit", emit)
        monkeypatch.setattr(gatekeeper._tel, "emit", emit) if hasattr(gatekeeper._tel, "emit") else None
        return events

    def test_telemetry_payload_and_drop_log_lines(self, monkeypatch, caplog):
        import logging
        import fl4write.telemetry as telemetry
        events = []
        monkeypatch.setattr(telemetry, "emit", lambda e, **kw: events.append((e, kw)))
        # gatekeeper does `from . import telemetry as _tel` INSIDE the
        # function, so patching the module attribute is enough.

        cfg = make_config()
        fs = [
            F(line=1, rule="r1", sev="Major", msg="keep me"),
            F(line=2, rule="r2", sev="Major", msg="drop " + "me too " * 10),  # 75 chars, scrubber-safe
        ]
        reply = json.dumps({
            "keep": [{"path": "src/a.py", "line": 1}],
            "demote": [{"path": "src/a.py", "line": 1, "rule_id": "r1", "severity": "Minor"}],
        })
        captured = {}

        def model(route, prompt, **kw):
            return reply

        monkeypatch.setattr(gatekeeper, "_call_model", model)
        with caplog.at_level(logging.INFO, logger="fl4write.gatekeeper"):
            kept, dropped, failed = gatekeeper.filter_findings(fs, cfg)

        assert failed is False and dropped == 1
        # telemetry: one gatekeeper event with exact counts and the exact
        # demoted_ids string (path:line (rule) old->new)
        assert events == [(
            "gatekeeper",
            {"repo": cfg.repo, "kept": 1, "dropped": 1,
             "demoted": ["src/a.py:1 (r1) Major->Minor"]},
        )]
        msgs = [r.getMessage() for r in caplog.records]
        # dropped line carries the 60-char message head
        assert "gatekeeper dropped: src/a.py:2 (r2) " + ("drop " + "me too " * 10)[:60] in msgs
        assert "gatekeeper dropped 1/2 findings (nit filter)" in msgs
        assert not any("dropped: src/a.py:1" in m for m in msgs)  # id-set exactness

    def test_unusable_message_carries_exactly_120_chars_of_the_parse(self, monkeypatch, caplog):
        import logging
        fs = [F()]
        filler = "x" * 200
        with caplog.at_level(logging.WARNING, logger="fl4write.gatekeeper"):
            TestFilterFindings().run(
                monkeypatch, fs, reply=json.dumps({"keep": filler}))
        warn = " ".join(r.getMessage() for r in caplog.records)
        expected_snippet = str({"keep": filler})[:120]
        assert f"keep-list unusable: {expected_snippet}" in warn
        assert expected_snippet + "x" not in warn  # cut at 120, not 121


class TestMaskedIdentityCollisions:
    def test_reserved_identity_name_forces_the_underscore_key(self, monkeypatch):
        """A finding literally named __fl4write_path_0__ reserves that key, so
        the first masked path must take the underscore-prefixed form."""
        weird1 = "src/" + "folder " * 30          # masked -> __fl4write_path_0__ (taken) -> _-prefixed
        fs = [F(path=weird1, line=1), F(path="__fl4write_path_0__", line=2)]
        prompts = []

        def model(route, prompt, **kw):
            prompts.append(prompt)
            # keep via whatever identities the prompt actually used
            import re
            keys = re.findall(r"- \[Major\] (\S+):\d+", prompt)
            return json.dumps({"keep": [{"path": k, "line": i + 1} for i, k in enumerate(keys)]})

        monkeypatch.setattr(gatekeeper, "_call_model", model)
        kept, dropped, failed = gatekeeper.filter_findings(fs, make_config())
        assert failed is False and dropped == 0 and len(kept) == 2
        p = prompts[0]
        assert "- [Major] ___fl4write_path_0__:1 (" in p          # the masked one, _-prefixed
        assert "- [Major] __fl4write_path_1__:2 (" in p           # the reserved name itself got masked too
        # display lines are newline-terminated
        assert "___fl4write_path_0__ => " + weird1[:200] + "\n" in p

    def test_demote_row_without_path_lands_on_the_empty_path_key(self):
        out = gatekeeper._demotions({
            "demote": [{"line": 4, "rule_id": "r", "severity": "Nit"}]
        }, VOCAB)
        assert out == {("", 4, "r"): "Nit"}


class TestFinalKillRound:
    def test_off_vocab_severity_first_does_not_kill_later_rows(self):
        out = gatekeeper._demotions({
            "demote": [
                {"path": "a.py", "line": 1, "rule_id": "r", "severity": "NotAWord"},
                {"path": "b.py", "line": 2, "rule_id": "r", "severity": "Nit"},
            ]
        }, VOCAB)
        assert out == {("b.py", 2, "r"): "Nit"}

    def test_demoted_ids_and_logs_use_full_length_cuts(self, monkeypatch, caplog):
        import logging
        long_path = "src/" + "folder " * 30        # 214 chars
        long_rule = "rule one two " * 5            # 65 chars
        fs = [F(path=long_path, line=1, rule=long_rule, sev="Major", msg="keep me")]
        reply = json.dumps({
            "keep": [{"path": "__fl4write_path_0__", "line": 1}],
            "demote": [{"path": "__fl4write_path_0__", "line": 1,
                        "rule_id": long_rule, "severity": "Minor"}],
        })
        import fl4write.telemetry as telemetry
        events = []
        monkeypatch.setattr(telemetry, "emit", lambda e, **kw: events.append((e, kw)))
        monkeypatch.setattr(
            gatekeeper, "_call_model",
            lambda route, prompt, **kw: reply)
        with caplog.at_level(logging.INFO, logger="fl4write.gatekeeper"):
            kept, dropped, failed = gatekeeper.filter_findings(fs, make_config())
        assert failed is False
        p2, r60 = long_path[:200], long_rule[:60]
        assert events[0][1]["demoted"] == [f"{p2}:1 ({r60}) Major->Minor"]
        msgs = [r.getMessage() for r in caplog.records]
        assert f"gatekeeper demoted {p2}:1 ({r60}) Major->Minor" in msgs

    def test_mask_decision_boundary_at_exactly_201_chars(self, monkeypatch):
        # a 201-char scrub-clean path: [:200] truncates (-> masked identity),
        # [:201] does not (-> original path in the prompt)
        path201 = "src/" + "folder " * 28 + "xy"   # 4 + 196 + ... = 201
        path201 = ("src/" + "folder " * 28)[:199] + "xy"  # ensure exactly 201
        assert len(path201) == 201
        prompts = []
        import re

        def model(route, prompt, **kw):
            prompts.append(prompt)
            keys = re.findall(r"- \[Major\] (\S+):\d+", prompt)
            return json.dumps({"keep": [{"path": k, "line": 1} for k in keys]})

        monkeypatch.setattr(gatekeeper, "_call_model", model)
        kept, dropped, failed = gatekeeper.filter_findings(
            [F(path=path201, line=1)], make_config())
        assert failed is False and len(kept) == 1
        p = prompts[0]
        assert f"- [Major] __fl4write_path_0__:1 (" in p     # masked: 200-cut changed it
        assert f"{path201} => __fl4write_path_0__" not in p  # display goes key => path
        assert f"__fl4write_path_0__ => {path201[:200]}" in p  # display cuts at 200

    def test_dropped_log_uses_the_full_length_cuts(self, monkeypatch, caplog):
        import logging
        long_path = "docs/" + "folder " * 30        # 215 chars
        long_rule = "rule three " * 6               # 66 chars
        fs = [
            F(path="src/keep.py", line=1, sev="Major", msg="keep me"),
            F(path=long_path, line=5, rule=long_rule, sev="Major", msg="drop me"),
        ]
        reply = json.dumps({"keep": [{"path": "src/keep.py", "line": 1}]})
        monkeypatch.setattr(gatekeeper, "_call_model", lambda r, p, **kw: reply)
        with caplog.at_level(logging.INFO, logger="fl4write.gatekeeper"):
            kept, dropped, failed = gatekeeper.filter_findings(fs, make_config())
        assert failed is False and dropped == 1
        msgs = [r.getMessage() for r in caplog.records]
        # the per-finding drop log fires for every finding not in the kept set
        assert (f"gatekeeper dropped: {long_path[:200]}:5 ({long_rule[:60]}) drop me" in msgs)
