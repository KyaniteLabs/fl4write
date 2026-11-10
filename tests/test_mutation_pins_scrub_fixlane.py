"""Mutation-testing pins for scrub.py and fixlane.py (round 1).

Each test here kills one or more surviving mutants from a mutmut run of
fl4write/scrub.py + fl4write/fixlane.py: logic changes the suite used to
let through unnoticed. The behaviors are safety rails (scrub before
posting, the final assert_clean gate, credential redaction, dependency-PR
policy), so "nothing noticed" meant "a regression could ship".
"""

from __future__ import annotations

import pytest

from fl4write import fixlane, scrub
from test_fl4write import make_config, make_pr


# ---------------------------------------------------------------- controls
def test_controls_keeps_text_after_every_stripped_char():
    # continue -> break: stopped at the first dropped control char
    assert scrub.controls("a‮b​c⁦d") == "abcd"


# ---------------------------------------------------------------- unclosed comments
def test_unclosed_comment_keeps_prefix_and_every_tail_segment():
    # [0] -> [1], rsplit, maxsplit drop: lost the prefix or a segment
    out = scrub.scrub("keep A <!-- B <!-- C")
    assert "<!--" not in out
    assert out.startswith("keep A ")
    assert "B" in out and "C" in out


# ---------------------------------------------------------------- assert_clean
@pytest.mark.parametrize("text", [
    "ok\n‮override",       # control char AFTER a whitelisted char (continue -> break)
    "<!-- hidden -->",
    "<style>x</style>",
    "<SCRIPT>x</SCRIPT>",        # lower() -> upper() made tag checks case-blind
    "<p style='display: none'>x</p>",
    "<span style='visibility:hidden'>x</span>",
    "<div hidden>x</div>",
])
def test_assert_clean_rejects_each_hidden_structure_alone(text):
    # or -> and: every branch must fire on its own
    with pytest.raises(ValueError):
        scrub.assert_clean(text)


@pytest.mark.parametrize("text", [
    "plain review text, mentions hidden and display without markup",
    "line one\n\tindented line two",
])
def test_assert_clean_accepts_plain_text(text):
    scrub.assert_clean(text)


def test_assert_clean_messages_name_the_problem():
    with pytest.raises(ValueError, match="U\\+202E"):
        scrub.assert_clean("x‮")
    with pytest.raises(ValueError, match="unscrubbed pattern"):
        scrub.assert_clean("![x](data:image/png;base64,AAAA)")


# ---------------------------------------------------------------- redact_credentials
@pytest.mark.parametrize("value", [None, 123, ""])
def test_redact_returns_non_text_and_empty_unchanged(value):
    # `or` -> `and` let 123 fall through into re.sub and crash
    assert scrub.redact_credentials(value) == value


@pytest.mark.parametrize("text,expected", [
    ('password = "abcdef"', 'password = "[redacted]"'),
    ("token: 'abcdef'", "token: '[redacted]'"),
    ("secret=abcdef", "secret=[redacted]"),
    # key names are case-insensitive (env/YAML style); values keep any case
    ("API_KEY=abc123", "API_KEY=[redacted]"),
    ('Token: "short1"', 'Token: "[redacted]"'),
    ("password=ABCDEF", "password=[redacted]"),
])
def test_redact_assignment_keeps_its_closing_quote(text, expected):
    assert scrub.redact_credentials(text) == expected


def test_redact_slash_token_floor_is_16_inclusive():
    # >= 16 -> > 16 / >= 17
    sixteen, fifteen = "ab/cdefghijklmno", "ab/cdefghijklmn"
    assert (len(sixteen), len(fifteen)) == (16, 15)
    assert scrub.redact_credentials(f"x {sixteen} y") == "x [redacted] y"
    assert scrub.redact_credentials(f"x {fifteen} y") == f"x {fifteen} y"


def test_redact_dotted_token_floor_is_24_inclusive():
    # >= 24 -> > 24 / >= 25
    t24, t23 = "abcde.fghij.klmno.pqrstu", "abcde.fghij.klmno.pqrst"
    assert (len(t24), len(t23)) == (24, 23)
    assert scrub.redact_credentials(f"x {t24} y") == "x [redacted] y"
    assert scrub.redact_credentials(f"x {t23} y") == f"x {t23} y"


def test_redact_every_copy_of_a_repeated_secret():
    secret = "A1b2C3d4E5f6G7h8I9"
    assert secret not in scrub.redact_credentials(f"{secret} and again {secret}")


# ---------------------------------------------------------------- inline
def test_inline_empty_and_limit():
    assert scrub.inline("") == ""
    assert scrub.inline("a\nb   c") == "a b c"
    assert scrub.inline("abcdef", limit=3) == "abc"
    assert scrub.inline("abcdef") == "abcdef"  # no limit: untruncated


# ---------------------------------------------------------------- dependency policy
@pytest.mark.parametrize("title,policy,expected", [
    # whole-word skip triggers
    ("chore(deps): update lockfile", "skip-patch", "skip"),
    ("Lock file maintenance", "skip-patch", "skip"),
    ("chore(deps): pin dependencies", "skip-patch", "skip"),
    ("chore(deps): update node digest to abc123", "skip-patch", "skip"),
    ("chore(deps): bump foo patch", "skip-patch", "skip"),
    # substring false-positives: these are real bumps and must be reviewed
    ("chore(deps): bump typing-extensions from 4.1 to 4.2", "skip-patch", "shallow"),
    ("chore(deps): bump pinia to 3.0", "skip-patch", "shallow"),
    ("chore(deps): bump dispatcher to 2.1", "skip-patch", "shallow"),
    # policy branches
    ("chore(deps): bump foo patch", "shallow-minor", "shallow"),
    ("chore(deps): bump foo from 1 to 2", "skip-all", "skip"),
    ("chore(deps): bump foo from 1 to 2", "skip-patch", "shallow"),
])
def test_dependency_depth_policy(title, policy, expected):
    pr = make_pr(is_bot_author=True, title=title)
    config = make_config(fix={"enabled": True, "dependency_policy": policy})
    assert fixlane.dependency_depth(pr, title, config) == expected


def test_dependency_depth_humans_always_get_full_review():
    pr = make_pr(is_bot_author=False, title="update lockfile")
    assert fixlane.dependency_depth(pr, pr.title, make_config(fix={"dependency_policy": "skip-all"})) == "full"


# ---------------------------------------------------------------- merge gate
def test_merge_gate_refusal_names_the_wrong_author():
    config = make_config(fix={"enabled": True, "merge_own_prs": True})
    with pytest.raises(fixlane.FixLaneBlocked, match="mallory"):
        fixlane.merge_own_pr("mallory", "fl4write[bot]", True, config)


@pytest.mark.parametrize("text,expected", [
    # short prefix-marked tokens, under the 16-char run rule
    ("use sk-abcd now", "use [redacted] now"),
    ("ghp_ab12 here", "[redacted] here"),
    ("AKIAABCD x", "[redacted] x"),
    ("glpat-Ab9_ ok", "[redacted] ok"),
    # a bare prefix with fewer than 4 trailing chars is documentation, not a key
    ("the sk-ab prefix", "the sk-ab prefix"),
])
def test_redact_short_prefixed_tokens(text, expected):
    assert scrub.redact_credentials(text) == expected
