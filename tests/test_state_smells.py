"""The deterministic implicit-state-machine detector (fl4write/state_smells.py).

Each shape must fire on the bug class and stay quiet on the look-alikes
ordinary code is full of; findings anchor to exact added lines.
"""

from __future__ import annotations

from fl4write import state_smells as sm
from fl4write.capabilities import default_review_rules

RULES = {sm.RULE_ID: "law"}


def _diff(path, lines, start=10):
    body = "".join(f"+{line}\n" for line in lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,1 +{start},{len(lines)} @@\n{body}")


def test_the_rule_ships_in_the_default_review_law():
    assert sm.RULE_ID in default_review_rules()


def test_nothing_without_the_rule_in_the_repo_law():
    d = _diff("a.py", ['if job.status == "done": x()'] * 5)
    assert sm.scan(d, {"other": "x"}) == []


# ---------------------------------------------------------------- status-text
def test_status_free_text_repeated_fires_once_at_the_first_use():
    d = _diff("jobs.py", [
        'if job.status == "queued":', "    start(job)",
        'elif job.status == "running":', "    poll(job)",
        'elif job.status == "dnoe":', "    finish(job)"])
    (f,) = sm.scan(d, RULES)
    assert (f.path, f.line, f.severity, f.rule_id) == ("jobs.py", 10, "Minor", "state-machines")
    assert "job.status" in f.message and "3 places" in f.message


def test_status_comparisons_across_files_and_reversed_order_count_together():
    d = (_diff("a.ts", ['if (order.state === "paid") {}']) +
         _diff("b.ts", ['if ("refunded" === order.state) {}', 'x = order.state !== "void"']))
    assert len(sm.scan(d, RULES)) == 1


def test_one_or_two_status_checks_stay_quiet():
    d = _diff("ci.py", ['runs = [r for r in rs if r["status"] == "completed"]',
                        'ok = r["status"] != "queued"'])
    assert sm.scan(d, RULES) == []


def test_comments_and_non_lifecycle_names_stay_quiet():
    d = _diff("x.py", ['# if status == "a"', '# if status == "b"', '# if status == "c"',
                       'if colour == "red": a()', 'if colour == "blue": b()',
                       'if colour == "green": c()'])
    assert sm.scan(d, RULES) == []


# ---------------------------------------------------------------- flag-soup
def test_three_booleans_on_one_object_fire():
    d = _diff("sweep.py", ['st["complete"] = True', 'st["published"] = False',
                           "st.scope_pending = True", "other = True"])
    (f,) = sm.scan(d, RULES)
    assert f.line == 10 and "3 separate booleans" in f.message and "`st`" in f.message
    assert "complete, published, scope_pending" in f.message


def test_two_flags_or_repeated_writes_of_one_flag_stay_quiet():
    d = _diff("x.py", ['st["done"] = True', 'st["done"] = False', 'st["ok"] = True',
                       'cfg["a"] = True', 'other["b"] = True'])
    assert sm.scan(d, RULES) == []


def test_flags_on_different_objects_do_not_add_up():
    d = _diff("x.py", ['a["x"] = True', 'b["y"] = True', 'c["z"] = True'])
    assert sm.scan(d, RULES) == []


# ---------------------------------------------------------------- copy-drift
BLOCK = ['state["round"] += 1', 'state["streak"] = 0', 'state["green"] = None']


def test_a_block_added_twice_fires_at_the_second_copy():
    d = _diff("loop.py", BLOCK + ["work()", "more()"] + BLOCK)
    (f,) = sm.scan(d, RULES)
    assert f.line == 15 and "also added at loop.py:10" in f.message


def test_copies_across_files_fire_once():
    d = _diff("a.py", BLOCK) + _diff("b.py", BLOCK, start=40)
    (f,) = sm.scan(d, RULES)
    assert (f.path, f.line) == ("b.py", 40) and "a.py:10" in f.message


def test_short_or_non_consecutive_repeats_stay_quiet():
    d = _diff("x.py", BLOCK[:2] + ["x()"] + BLOCK[:2])
    assert sm.scan(d, RULES) == []
    trivial = ["}", "}", "}", "pass", "}", "}", "}"]
    assert sm.scan(_diff("y.ts", trivial), RULES) == []


# ---------------------------------------------------------------- diff parsing
def test_line_numbers_follow_the_new_file_through_context_and_removals():
    d = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -5,4 +7,5 @@\n"
         " ctx\n-gone\n+st['a'] = True\n ctx\n+st['b'] = True\n+st['c'] = True\n")
    (f,) = sm.scan(d, RULES)
    assert f.line == 8


def test_deleted_files_and_removed_lines_are_ignored():
    d = ("diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
         '-st["a"] = True\n-st["b"] = True\n-st["c"] = True\n')
    assert sm.scan(d, RULES) == []


def test_findings_are_capped_and_sorted():
    d = "".join(_diff(f"f{i}.py", [f'o{i}["a"] = True', f'o{i}["b"] = True', f'o{i}["c"] = True'])
                for i in range(8))
    out = sm.scan(d, RULES)
    assert len(out) == sm.MAX_FINDINGS
    assert [f.path for f in out] == sorted(f.path for f in out)


def test_engine_review_carries_the_smells(monkeypatch):
    """End to end: a PR whose diff hides a state machine gets the finding."""
    from fl4write import engine
    from fl4write.models import ReviewDoc
    from test_fl4write import make_config, make_pr

    diff = _diff("sweep.py", ['st["complete"] = True', 'st["published"] = True',
                              'st["pending"] = False'])
    monkeypatch.setattr("fl4write.analyzer.analyze",
                        lambda pr, files, text, config, mode="pr": ReviewDoc(pr=pr, findings=[]))
    posted = []

    class _Forge:
        name = "github"

        def get_persistent_comment(self, repo, n):
            return None

        def create_comment(self, repo, n, body):
            posted.append(body)

    cfg = make_config(shadow=False, gatekeeper=False, verify_tests=False,
                      review={"state-machines": "law"})
    st = {"prs": {}}
    out = engine._review_pr(make_pr(), cfg, _Forge(), lambda pr: ({"sweep.py"}, diff), None, st,
                            engine.CycleReport(repo="o/r"), run_fixes=False)
    assert out == "reviewed" and len(posted) == 1
    assert "state-machines" in posted[0] and "sweep.py:10" in posted[0]
