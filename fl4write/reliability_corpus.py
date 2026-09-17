"""Reliability measurement corpus (built for the CEO reliability verdict: "not even a
reliable code review bot yet" — determinism, recall, honest uncertainty
become MEASURED properties, not assertions).

Two corpora, both time-rot-free by construction (LEARNINGS #63 discipline:
NO wall-clock dates anywhere in fixture data — every case is pure code):

- DEFECT_CASES: one known defect per case, each labeled with a
  defect_class. Cases 1-3 carry the production lineage of
  tests/test_planted_diffs.py (Epoch #198 median miss + the two predicted
  comorbidity classes) and keep their impl+failing-test shape so numbers
  stay comparable with the 09-16 desk receipt (3/3 recall, Champion).
  Cases 4-10 are new single-file classes with NO test in the diff — they
  measure the model's own reading of code, not test-tracing.

- CLEAN_CASES: plausible, correct feature diffs. Any finding on them is a
  false positive by construction (two bars: any-severity noise, and
  Critical/Major actionable).

build_case_diff() emits a realistic new-file unified diff (diff --git +
@@ hunks) so the analyzer's grounding gates (path in diff, line inside
hunk spans) run exactly as they do on real PRs — the harness measures the
END-TO-END product (model + gates), not the model in isolation.
"""

from __future__ import annotations

# Determinism sample: FIXED case ids (never reordered/renamed without
# invalidating comparability) — 2 defect + 1 clean, measured N>=5 times
# each with FRESH calls (cache off: repeated samples are the measurement).
DETERMINISM_SAMPLE = ["median-unsorted", "sql-string-concat", "clean-pagination"]

DEFECT_CASES = [
    {
        "id": "median-unsorted",
        "defect_class": "unsorted-input-assumption",
        "origin": "Epoch #198 E2E: missed by M3 x2 + deepseek x2 (production)",
        "title": "Add median helper",
        "files": {
            "fl4write-proof/proof_target.py": (
                "def median(values):\n"
                "    n = len(values)\n"
                "    if n == 0:\n"
                "        raise ValueError('empty')\n"
                "    if n % 2 == 1:\n"
                "        return values[n // 2]\n"
                "    return (values[n // 2 - 1] + values[n // 2]) / 2\n"
            ),
            "fl4write-proof/test_proof_target.py": (
                "from proof_target import median\n\n\n"
                "def test_median_odd_unsorted():\n"
                "    assert median([5.0, 1.0, 3.0]) == 3.0\n"
            ),
        },
    },
    {
        "id": "off-by-one-boundary",
        "defect_class": "boundary-indexing",
        "origin": "class predicted by comorbidity #3",
        "title": "Add pagination helper",
        "files": {
            "paginate.py": (
                "def page(items, page_no, size):\n"
                "    'Return slice for 1-based page_no.'\n"
                "    start = page_no * size\n"
                "    return items[start:start + size]\n"
            ),
            "test_paginate.py": (
                "from paginate import page\n\n\n"
                "def test_first_page():\n"
                "    assert page(['a', 'b', 'c'], 1, 2) == ['a', 'b']\n"
            ),
        },
    },
    {
        "id": "inverted-comparison",
        "defect_class": "comparison-polarity",
        "origin": "class predicted by comorbidity #3",
        "title": "Add severity threshold check",
        "files": {
            "threshold.py": (
                "def is_critical(sev_score):\n"
                "    'Critical when score >= 90.'\n"
                "    return sev_score < 90\n"
            ),
            "test_threshold.py": (
                "from threshold import is_critical\n\n\n"
                "def test_critical():\n"
                "    assert is_critical(95) is True\n"
            ),
        },
    },
    {
        "id": "sql-string-concat",
        "defect_class": "sql-injection",
        "origin": "harness class (reliability baseline)",
        "title": "Add user lookup endpoint",
        "files": {
            "users_query.py": (
                "import sqlite3\n\n\n"
                "def find_user(conn: sqlite3.Connection, email: str):\n"
                "    'Look up one user row by email.'\n"
                "    cur = conn.cursor()\n"
                "    cur.execute(\"SELECT id, email, plan FROM users \"\n"
                "                \"WHERE email = '\" + email + \"'\")\n"
                "    return cur.fetchone()\n"
            ),
        },
    },
    {
        "id": "hardcoded-token",
        "defect_class": "hardcoded-credential",
        "origin": "harness class (reliability baseline)",
        "title": "Add deploy notifier",
        "files": {
            "deploy_notify.py": (
                "import urllib.request\n\n"
                "API_KEY = \"sk-proj-9f8e7d6c5b4a3210fedcba9876543210\"\n\n\n"
                "def notify_deploy(target: str) -> None:\n"
                "    'Ping the ops channel after a deploy.'\n"
                "    req = urllib.request.Request(\n"
                "        \"https://ops.example.internal/deployed\",\n"
                "        data=(\"{\\\"target\\\": \\\"\" + target + \"\\\"}\").encode(),\n"
                "        headers={\"Authorization\": \"Bearer \" + API_KEY})\n"
                "    urllib.request.urlopen(req, timeout=30)\n"
            ),
        },
    },
    {
        "id": "file-never-closed",
        "defect_class": "resource-leak",
        "origin": "harness class (reliability baseline)",
        "title": "Add config line counter",
        "files": {
            "count_config.py": (
                "def count_active_lines(path: str) -> int:\n"
                "    'Count non-blank, non-comment lines in a config file.'\n"
                "    fh = open(path, encoding=\"utf-8\")\n"
                "    n = 0\n"
                "    for line in fh:\n"
                "        stripped = line.strip()\n"
                "        if stripped and not stripped.startswith(\"#\"):\n"
                "            n += 1\n"
                "    return n\n"
            ),
        },
    },
    {
        "id": "mutate-while-iterating",
        "defect_class": "mutation-during-iteration",
        "origin": "harness class (reliability baseline)",
        "title": "Add adult-filter helper",
        "files": {
            "filter_users.py": (
                "def drop_minors(users: list[dict]) -> list[dict]:\n"
                "    'Remove underage users in place and return the list.'\n"
                "    for u in users:\n"
                "        if u[\"age\"] < 18:\n"
                "            users.remove(u)\n"
                "    return users\n"
            ),
        },
    },
    {
        "id": "swallowed-error",
        "defect_class": "exception-swallow",
        "origin": "harness class (reliability baseline)",
        "title": "Add metrics flusher",
        "files": {
            "flush_metrics.py": (
                "import json\n"
                "import pathlib\n\n\n"
                "def flush(counters: dict, path: pathlib.Path) -> bool:\n"
                "    'Persist counters to disk; True on success.'\n"
                "    try:\n"
                "        path.write_text(json.dumps(counters))\n"
                "    except Exception:\n"
                "        pass\n"
                "    return True\n"
            ),
        },
    },
    {
        "id": "precedence-bug",
        "defect_class": "operator-precedence",
        "origin": "harness class (reliability baseline)",
        "title": "Add loaded-flag check",
        "files": {
            "ready_check.py": (
                "MASK_READY = 1\n"
                "MASK_LOADED = 2\n\n\n"
                "def is_loaded(flags: int) -> bool:\n"
                "    'True when the LOADED bit (2) is set.'\n"
                "    return flags & MASK_LOADED == 2\n"
            ),
        },
    },
    {
        "id": "none-attr-crash",
        "defect_class": "missing-none-guard",
        "origin": "harness class (reliability baseline)",
        "title": "Add greeting formatter",
        "files": {
            "greet.py": (
                "def greeting(opts: dict) -> str:\n"
                "    'Friendly greeting; name may be absent from opts.'\n"
                "    name = opts.get(\"name\")\n"
                "    return \"Hello, \" + name.upper() + \"!\"\n"
            ),
        },
    },
]

CLEAN_CASES = [
    {
        "id": "clean-median",
        "origin": "correct counterpart of median-unsorted",
        "title": "Add median helper",
        "files": {
            "stats_util.py": (
                "def median(values):\n"
                "    'Median of a numeric sequence (input not modified).'\n"
                "    if not values:\n"
                "        raise ValueError('empty')\n"
                "    s = sorted(values)\n"
                "    n = len(s)\n"
                "    if n % 2 == 1:\n"
                "        return s[n // 2]\n"
                "    return (s[n // 2 - 1] + s[n // 2]) / 2\n"
            ),
        },
    },
    {
        "id": "clean-pagination",
        "origin": "correct counterpart of off-by-one-boundary",
        "title": "Add pagination helper",
        "files": {
            "paging.py": (
                "def page(items, page_no, size):\n"
                "    'Return slice for 1-based page_no.'\n"
                "    start = (page_no - 1) * size\n"
                "    return items[start:start + size]\n"
            ),
        },
    },
    {
        "id": "clean-sql-params",
        "origin": "correct counterpart of sql-string-concat",
        "title": "Add user lookup endpoint",
        "files": {
            "users_query.py": (
                "import sqlite3\n\n\n"
                "def find_user(conn: sqlite3.Connection, email: str):\n"
                "    'Look up one user row by email.'\n"
                "    cur = conn.cursor()\n"
                "    cur.execute(\n"
                "        \"SELECT id, email, plan FROM users WHERE email = ?\",\n"
                "        (email,))\n"
                "    return cur.fetchone()\n"
            ),
        },
    },
    {
        "id": "clean-file-close",
        "origin": "correct counterpart of file-never-closed",
        "title": "Add config line counter",
        "files": {
            "count_config.py": (
                "def count_active_lines(path: str) -> int:\n"
                "    'Count non-blank, non-comment lines in a config file.'\n"
                "    with open(path, encoding=\"utf-8\") as fh:\n"
                "        return sum(\n"
                "            1 for line in fh\n"
                "            if line.strip() and not line.strip().startswith(\"#\"))\n"
            ),
        },
    },
    {
        "id": "clean-threshold",
        "origin": "correct counterpart of inverted-comparison",
        "title": "Add severity threshold check",
        "files": {
            "threshold.py": (
                "def is_critical(sev_score: int) -> bool:\n"
                "    'Critical when score >= 90.'\n"
                "    return sev_score >= 90\n"
            ),
        },
    },
]


def build_case_diff(case: dict) -> tuple[set[str], str]:
    """Render a case as a realistic new-file unified diff.

    Returns (diff_files, diff_text) with proper `diff --git` headers and
    `@@ -0,0 +1,N @@` hunks so the analyzer's grounding gates engage
    exactly as on a real PR.
    """
    parts: list[str] = []
    for path, code in case["files"].items():
        lines = code.splitlines()
        body = "".join(f"+{ln}\n" for ln in lines)
        parts.append(
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n"
            f"{body}"
        )
    return set(case["files"].keys()), "".join(parts)


def case_by_id(case_id: str) -> dict:
    for case in DEFECT_CASES + CLEAN_CASES:
        if case["id"] == case_id:
            return case
    raise KeyError(f"no corpus case with id {case_id!r}")
