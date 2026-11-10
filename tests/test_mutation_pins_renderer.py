"""Mutation-testing pins for fl4write/renderer.py — the public comment body.

Everything here is posted where anyone can read it, so the contracts that
keep model and repo text from forging structure, the finding-line round
trip and the tone rails are pinned directly.
"""

from __future__ import annotations

import pytest

from fl4write import renderer as r
from fl4write.models import Finding
from test_fl4write import make_config, make_pr


def _f(**over):
    base = dict(rule_id="loc-ceiling", severity="Major", path="src/a.py", line=7,
                message="too long", proposal="")
    base.update(over)
    return Finding(**base)


# ---------------------------------------------------------------- tone rails
@pytest.mark.parametrize("fork,tone,override,expect", [
    (False, "roast", "balanced", "roast"),
    (False, "quiet", "assertive", "quiet"),
    (True, "roast", "assertive", "assertive"),
    (True, "quiet", "quiet", "quiet"),
    (True, "roast", "roast", "balanced"),          # a fork is never roasted
])
def test_tone_rails(fork, tone, override, expect):
    cfg = make_config(tone=tone, tone_fork_override=override)
    assert r._tone_for(make_pr(is_fork=fork), cfg) == expect


def test_fork_with_roast_override_renders_no_roast_banner():
    cfg = make_config(tone="roast", tone_fork_override="roast")
    body = r.render_review(make_pr(is_fork=True), [_f()], cfg, "h1")
    assert "Roast mode" not in body and "tone: balanced" in body
    assert r._TONES["balanced"] in body


# ---------------------------------------------------------------- markdown escaping
@pytest.mark.parametrize("text,expect", [
    ("a ``` b", "a `` b"),
    ("````x````", "``x``"),
    ("~~~\ncode", "\\~~~\ncode"),
    ("   ~~~~", "\\~~~~"),                         # leading spaces drop (still inert)
    ("    ~~~", "    ~~~"),                       # 4 spaces: indented code, not a fence
    ("# h", "\\# h"),
    ("   ###### h", "   \\###### h"),
    ("####### h", "####### h"),                    # 7 hashes is not a heading
    ("#tag", "#tag"),                              # no space: not a heading
    ("> quote", "\\> quote"),
    ("   > q", "\\> q"),
    ("ok\n## two\n> three", "ok\n\\## two\n\\> three"),
])
def test_md_escape_block(text, expect):
    assert r._md_escape_block(text) == expect


# ---------------------------------------------------------------- paths
@pytest.mark.parametrize("raw,shown", [
    ("a/b.py", "a/b.py"),
    ("a\\b", "a\\\\b"),
    ("a\nb", "a\\nb"),
    ("a\rb", "a\\rb"),
    ("a\tb", "a\tb"),
    ("a\u200db", "a\\u200db"),                     # zero-width joiner (Cf)
    ("a\u202eb", "a\\u202eb"),                     # bidi override
    ("a\x07b", "a\\u0007b"),
    ("a_b`c", "a_b`c"),
])
def test_escape_path_is_injective_and_single_line(raw, shown):
    assert r._escape_path(raw) == shown
    assert r.path_display(raw) == shown


def test_distinct_raw_paths_never_share_an_identity():
    raws = ["a b", "a\nb", "a\\nb", "ab", "a\u200db", "a\\u200db"]
    assert len({r.path_key(p) for p in raws}) == len(raws)


def test_credential_shaped_paths_get_a_one_way_key():
    import hashlib
    raw = "conf/token=" + "Z" * 32 + ".env"
    key = r.path_key(raw)
    assert key == "\\sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert "Z" * 32 not in r.path_display(raw)
    assert r.path_key("plain.py") == "plain.py"


def test_path_plain_escapes_backticks():
    assert r.path_plain("a`b") == "a\\`b"


@pytest.mark.parametrize("text,span", [
    ("a", "`a`"), ("a`b", "``a`b``"), ("a``b`c", "```a``b`c```"), ("", "``")])
def test_code_span_widens_past_the_longest_run(text, span):
    assert r._code_span(text) == span


# ---------------------------------------------------------------- finding line contract
@pytest.mark.parametrize("finding", [
    _f(),
    _f(severity="Critical", path="we`ird/p.py", line=1),
    _f(severity="Nit", rule_id="a:b<c>[d]!e`f", line=12345),
    _f(severity="Minor", path="a\nb.py", rule_id="r"),
])
def test_render_then_parse_round_trips(finding):
    body = r.render_finding(finding, "balanced")
    assert r.parse_finding_lines(body) == [
        (finding.severity, r.path_key(finding.path), finding.line, finding.rule_id)]


def test_finding_heading_shape_and_unknown_severity_badge():
    line = r.render_finding(_f(), "balanced").splitlines()[0]
    assert line == "### 🟠 Major — `src/a.py:7` — rule `loc-ceiling`"
    assert r.render_finding(_f(severity="Odd"), "x").startswith("### ⚪ Odd — ")


def test_parse_accepts_new_marker_legacy_ids_and_rejects_bad_v2():
    body = ("🆕 ### 🔴 Critical — `x.py:3` — rule `a\\u003ab`\n"
            "### 🔵 Nit — `y.py:4` — `legacy\\u0041`\n"
            "### 🔵 Nit — `z.py:5` — rule `\\u0000`\n"
            "### 🔵 Nit — `w.py:6` — rule `bad\\q`\n"
            "### 🔵 Nit — `v.py:7` — rule `a\\u0020b`\n"
            "### 🟡 Bogus — `u.py:8` — rule `r`\n")
    assert r.parse_finding_lines(body) == [
        ("Critical", "x.py", 3, "a:b"), ("Nit", "y.py", 4, "legacy\\u0041")]


def test_rule_display_escapes_markdown_bytes():
    assert r._rule_display("a`b<c>:d[e]!f") == \
        "a\\u0060b\\u003cc\\u003e\\u003ad\\u005be\\u005d\\u0021f"
    assert r._rule_display('q"\n') == 'q\\"\\n'


# ---------------------------------------------------------------- finding body
def test_finding_body_scrubs_and_fences_the_proposal():
    f = _f(severity="Critical", message="# fake\nsee ```x```",
           proposal="```\nrm -rf /\n```\n### 🔴 Critical — `z:1` — rule `r`")
    body = r.render_finding(f, "balanced")
    assert "\\# fake" in body and "see ``x``" in body
    assert "<details>\n<summary>💡 How to fix</summary>\n\n````\n" in body
    assert "\n````\n\n</details>" in body
    assert "\\### 🔴 Critical" in body and len(r.parse_finding_lines(body)) == 1
    assert body.endswith("🚨 **Do NOT merge** until this is addressed.")
    post = r.render_finding(f, "balanced", post_merge=True)
    assert post.endswith("🚨 **Landed on main** — fix-forward strongly recommended.")


def test_finding_without_proposal_or_urgency_is_just_heading_and_message():
    assert r.render_finding(_f(), "q") == \
        "### 🟠 Major — `src/a.py:7` — rule `loc-ceiling`\n\ntoo long"


def test_finding_message_and_proposal_redact_credentials():
    secret = "ghp_" + "A" * 36
    body = r.render_finding(_f(message=f"key {secret}", proposal=f"use {secret}"), "q")
    assert secret not in body


# ---------------------------------------------------------------- review body
def test_review_header_table_counts_new_marks_and_resolved():
    cfg = make_config(tone="assertive")
    pr = make_pr(number=9, head_sha="abcdef1234" + "0" * 30)
    cur = [_f(), _f(severity="Critical", path="b.py", line=2, rule_id="r2"),
           _f(severity="Major", path="c.py", line=3)]
    prev = [_f(), _f(severity="Nit", path="gone.py", line=4, rule_id="old")]
    body = r.render_review(pr, cur, cfg, "hash9", previous_findings=prev, gatekeeper_dropped=2)
    assert "| 🔴 Critical | 1 |\n| 🟠 Major | 2 |\n| 🟡 Minor | 0 |\n| 🔵 Nit | 0 |" in body
    assert "`KyaniteLabs/kinocut#9` @ `abcdef12` · 3 findings · 🧹 2 nits filtered" in body
    assert body.count("🆕 ### ") == 2                      # src/a.py:7 is unchanged
    assert "### 🟠 Major — `src/a.py:7`" in body and "🆕 ### 🟠 Major — `src/a.py:7`" not in body
    assert "### ✅ Resolved since last review\n\n- ✅ `~gone.py:4` (`old`)" in body
    assert r._TONES["assertive"] in body and "tone: assertive" in body
    assert body.endswith("<!-- fl4write:v1:hash9 -->\n")
    assert body.startswith("## 🔍 FL4WRITE review\n\n")


def test_review_without_dropped_nits_says_nothing_about_them():
    body = r.render_review(make_pr(), [_f()], make_config(), "h")
    assert "nits filtered" not in body and "· 1 findings\n" in body


def test_only_resolved_findings_still_render_the_table():
    body = r.render_review(make_pr(), [], make_config(), "h", previous_findings=[_f()])
    assert "| 🟠 Major | 0 |" in body and "### ✅ Resolved" in body
    assert "Clean review" not in body and "· 0 findings" in body


@pytest.mark.parametrize("post_merge,truncated,expect,absent", [
    (False, False, "Go merge it. 🎉", "PARTIAL"),
    (True, False, "Merged in good shape. ✅", "Go merge it"),
    (False, True, "Review incomplete (diff truncated) — no merge signal.", "Go merge it"),
    (True, True, "Review incomplete (diff truncated) — no merge signal.", "Merged in good"),
])
def test_clean_and_truncated_states(post_merge, truncated, expect, absent):
    body = r.render_review(make_pr(), [], make_config(tone="quiet"), "h",
                           diff_truncated=truncated, post_merge=post_merge)
    assert expect in body and absent not in body
    assert body.startswith("## 🔍 FL4WRITE review (post-merge)\n\n" if post_merge
                           else "## 🔍 FL4WRITE review\n\n")
    assert ("⚠️ **PARTIAL REVIEW" in body) is truncated


def test_truncated_review_with_findings_keeps_the_banner():
    body = r.render_review(make_pr(), [_f()], make_config(), "h", diff_truncated=True)
    assert "⚠️ **PARTIAL REVIEW — the diff was truncated; the file set below the limit " \
           "is NOT a clean bill**" in body and "Go merge it" not in body


def test_review_refuses_hidden_structure_from_model_text():
    with pytest.raises(Exception):
        r.render_review(make_pr(), [_f(proposal="<!-- hidden -->")], make_config(), "h")


def test_count_severities():
    assert r._count_severities([_f(), _f(), _f(severity="Nit")]) == {"Major": 2, "Nit": 1}


def test_severity_table_counts_every_level_exactly():
    fs = [_f(severity="Critical"), _f(severity="Minor"), _f(severity="Minor"),
          _f(severity="Nit"), _f(severity="Nit"), _f(severity="Nit")]
    assert r._severity_table(fs) == (
        "| Severity | Count |\n|---|---|\n"
        "| 🔴 Critical | 1 |\n| 🟠 Major | 0 |\n| 🟡 Minor | 2 |\n| 🔵 Nit | 3 |")
    assert "| 🔴 Critical | 0 |" in r._severity_table([])


def test_bad_rule_lines_do_not_stop_the_parse():
    body = ("### 🔵 Nit — `w.py:6` — rule `bad\\q`\n"
            "### 🔵 Nit — `v.py:7` — rule `a\\u0020b`\n"
            "### 🟠 Major — `ok.py:8` — rule `fine`\n")
    assert r.parse_finding_lines(body) == [("Major", "ok.py", 8, "fine")]


def test_code_span_with_a_leading_backtick():
    assert r._code_span("`a") == "``" + "`a" + "``"


def test_rule_display_escapes_non_ascii():
    assert r._rule_display("é") == "\\u00e9"


def test_review_sections_and_resolved_lines_are_separated():
    prev = [_f(path="x.py", line=1, rule_id="a"), _f(path="y.py", line=2, rule_id="b")]
    body = r.render_review(make_pr(), [_f()], make_config(tone="quiet"), "h",
                           previous_findings=prev)
    assert "- ✅ `~x.py:1` (`a`)\n- ✅ `~y.py:2` (`b`)" in body
    assert "too long\n\n---\n\n### ✅ Resolved" in body
    assert "| 🔵 Nit | 0 |\n\n`KyaniteLabs" in body


def test_clean_bodies_are_exact():
    cfg = make_config(tone="quiet")
    assert "\n\n## ✨ Clean review — nothing to fix.\n\nGo merge it. 🎉\n\n---\n*Reviewed" in \
        r.render_review(make_pr(), [], cfg, "h")
    assert "\n\n## ✨ Clean review — nothing to fix.\n\nMerged in good shape. ✅\n\n---\n*" in \
        r.render_review(make_pr(), [], cfg, "h", post_merge=True)
    assert "\n\nReview incomplete (diff truncated) — no merge signal.\n\n\n---\n*" in \
        r.render_review(make_pr(), [], cfg, "h", diff_truncated=True)


def test_urgency_and_proposal_are_blank_line_separated():
    body = r.render_finding(_f(severity="Critical", proposal="p"), "q")
    assert "too long\n\n<details>" in body and "</details>\n\n🚨" in body
