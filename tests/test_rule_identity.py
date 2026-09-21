"""Custom rule identities survive the actual persistent-comment lifecycle."""
import pytest

from fl4write import metrics, renderer
from fl4write.models import Finding
from test_fl4write import make_config, make_pr


class CommentForge:
    def __init__(self, body):
        self.body = body

    def get_persistent_comment(self, repo, number):
        return 7, self.body


def _finding(rule):
    return Finding(rule_id=rule, severity="Major", path="api.py", line=2, message="Incorrect result")


def _previous(body):
    return [Finding(severity=severity, path=path, line=line, rule_id=rule, message="previous")
            for severity, path, line, rule in renderer.parse_finding_lines(body)]


@pytest.mark.parametrize("rule", ["api`result", "apiresult", "api``result", "`api", "api`",
                                  "`", "``", "&amp;", "a\\b", "a\u200db",
                                  "api\\u0060result", "a\\u200db", "<!--rule-->", "display:none"])
def test_unchanged_custom_rule_round_trip_is_not_new_or_addressed(rule):
    config, pr = make_config(review={rule: "Check results"}), make_pr()
    finding = _finding(rule)
    first = renderer.render_review(pr, [finding], config, "first")
    previous = _previous(first)
    assert [f.rule_id for f in previous] == [rule]
    repeated = renderer.render_review(pr, [finding], config, "second", previous_findings=previous)
    assert "🆕" not in repeated and "Resolved since last review" not in repeated
    assert [f.rule_id for f in _previous(repeated)] == [rule]
    assert metrics.comment_signals(CommentForge(repeated), pr.repo, pr.number) == {
        "findings": 1, "resolved": 0, "reactions": 0, "addressed": 0}


def test_neighboring_custom_rules_remain_distinct_and_only_missing_rule_resolves():
    rules = ["apiresult", "api`result", "api``result", "`apiresult", "apiresult`"]
    config, pr = make_config(review={rule: "Check results" for rule in rules}), make_pr()
    findings = [_finding(rule) for rule in rules]
    first = renderer.render_review(pr, findings, config, "first")
    previous = _previous(first)
    assert [f.rule_id for f in previous] == rules
    repeat = renderer.render_review(pr, findings, config, "second", previous_findings=previous)
    assert "🆕" not in repeat and "Resolved since last review" not in repeat
    updated = renderer.render_review(pr, findings[1:], config, "third", previous_findings=previous)
    signals = metrics.comment_signals(CommentForge(updated), pr.repo, pr.number)
    assert signals["findings"] == 4 and signals["resolved"] == signals["addressed"] == 1


def test_legacy_rule_escapes_are_not_decoded_as_new_heading_format():
    legacy = "### 🟠 Major — `api.py:2` — `api\\u0060result`"
    assert renderer.parse_finding_lines(legacy) == [("Major", "api.py", 2, "api\\u0060result")]
