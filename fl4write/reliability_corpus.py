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
  measure the model's own reading of code, not test-tracing. Cases 11-20
  (corpus growth) add ten more single-file classes in the same shape; each
  was executed when added to confirm the defect is real, not merely styled.

- CLEAN_CASES: plausible, correct, LAW-CLEAN feature diffs (each ships its
  tests — org law: changes to logic ship with tests). Any finding on them is
  a false positive by construction (two bars: any-severity noise, and
  Critical/Major actionable). Validity fix (post-baseline): the original
  clean cases were test-less, so law-mandated missing-tests notes were
  counted as FPs — both recorded baseline readings of actionable-FP were
  inflated by this; the law-note rate stays derivable from records instead
  of masquerading as noise. Twenty clean cases (one per defect class where a
  correct counterpart exists, plus independent features) so one false
  positive moves the rate 5 points, not 20. "Clean by construction" is a
  CHECKED claim: tests/test_reliability.py runs every clean case's own tests.

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
    {
        "id": "path-join-escape",
        "defect_class": "path-traversal",
        "origin": "harness class (corpus growth)",
        "title": "Add attachment download helper",
        "files": {
            "attachments.py": (
                "from pathlib import Path\n"
                "\n"
                "UPLOAD_ROOT = Path('/srv/uploads')\n"
                "\n"
                "\n"
                "def read_attachment(name: str) -> bytes:\n"
                "    'Return the bytes of an uploaded attachment by file name.'\n"
                "    return (UPLOAD_ROOT / name).read_bytes()\n"
            ),
        },
    },
    {
        "id": "shell-true-grep",
        "defect_class": "shell-injection",
        "origin": "harness class (corpus growth)",
        "title": "Add log search helper",
        "files": {
            "log_search.py": (
                "import subprocess\n"
                "\n"
                "\n"
                "def count_matches(pattern: str, log_path: str) -> int:\n"
                "    'Count log lines matching a user-supplied pattern.'\n"
                '    out = subprocess.run(f"grep -c {pattern} {log_path}", shell=True,\n'
                "                         capture_output=True, text=True)\n"
                "    return int(out.stdout.strip() or 0)\n"
            ),
        },
    },
    {
        "id": "shared-default-list",
        "defect_class": "mutable-default-arg",
        "origin": "harness class (corpus growth)",
        "title": "Add tag builder",
        "files": {
            "tags.py": (
                "def with_tag(tag: str, tags: list[str] = []) -> list[str]:\n"
                "    'Return a tag list that includes tag (new list unless one is given).'\n"
                "    tags.append(tag)\n"
                "    return tags\n"
            ),
        },
    },
    {
        "id": "floor-division-mean",
        "defect_class": "integer-division-truncation",
        "origin": "harness class (corpus growth)",
        "title": "Add latency average",
        "files": {
            "latency.py": (
                "def mean_latency_ms(samples: list[float]) -> float:\n"
                "    'Arithmetic mean of latency samples in milliseconds.'\n"
                "    if not samples:\n"
                "        raise ValueError('no samples')\n"
                "    return sum(samples) // len(samples)\n"
            ),
        },
    },
    {
        "id": "range-skips-last",
        "defect_class": "range-excludes-last",
        "origin": "harness class (corpus growth)",
        "title": "Add invoice total",
        "files": {
            "invoice.py": (
                "def invoice_total(line_amounts: list[int]) -> int:\n"
                "    'Sum of every invoice line amount, in cents.'\n"
                "    total = 0\n"
                "    for i in range(len(line_amounts) - 1):\n"
                "        total += line_amounts[i]\n"
                "    return total\n"
            ),
        },
    },
    {
        "id": "regex-match-prefix",
        "defect_class": "unanchored-regex",
        "origin": "harness class (corpus growth)",
        "title": "Add username validation",
        "files": {
            "usernames.py": (
                "import re\n"
                "\n"
                "_USERNAME = re.compile(r'[a-z0-9_]{3,20}')\n"
                "\n"
                "\n"
                "def is_valid_username(name: str) -> bool:\n"
                "    'True when name is 3-20 chars of lowercase letters, digits, underscore.'\n"
                "    return _USERNAME.match(name) is not None\n"
            ),
        },
    },
    {
        "id": "pickle-request-body",
        "defect_class": "unsafe-deserialization",
        "origin": "harness class (corpus growth)",
        "title": "Add session import endpoint",
        "files": {
            "session_import.py": (
                "import base64\n"
                "import pickle\n"
                "\n"
                "\n"
                "def import_session(body: str) -> dict:\n"
                "    'Decode a client-supplied session blob (base64) into a dict.'\n"
                "    return pickle.loads(base64.b64decode(body))\n"
            ),
        },
    },
    {
        "id": "counter-keyerror",
        "defect_class": "missing-key-default",
        "origin": "harness class (corpus growth)",
        "title": "Add word frequency helper",
        "files": {
            "wordfreq.py": (
                "def word_counts(text: str) -> dict[str, int]:\n"
                "    'Count how often each whitespace-separated word appears.'\n"
                "    counts: dict[str, int] = {}\n"
                "    for word in text.split():\n"
                "        counts[word] += 1\n"
                "    return counts\n"
            ),
        },
    },
    {
        "id": "is-string-compare",
        "defect_class": "identity-vs-equality",
        "origin": "harness class (corpus growth)",
        "title": "Add job status check",
        "files": {
            "jobs.py": (
                "def is_finished(status: str) -> bool:\n"
                "    'True when a job status string reports completion.'\n"
                '    return status is "done"\n'
            ),
        },
    },
    {
        "id": "lambda-late-binding",
        "defect_class": "late-binding-closure",
        "origin": "harness class (corpus growth)",
        "title": "Add per-level log handlers",
        "files": {
            "handlers.py": (
                "LEVELS = ['debug', 'info', 'error']\n"
                "\n"
                "\n"
                "def build_handlers() -> dict:\n"
                "    'Map each level name to a callable returning that level name.'\n"
                "    handlers = {}\n"
                "    for level in LEVELS:\n"
                "        handlers[level] = lambda: level\n"
                "    return handlers\n"
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
            "test_stats_util.py": (
                "from stats_util import median\n\n\n"
                "def test_median_odd():\n"
                "    assert median([5.0, 1.0, 3.0]) == 3.0\n\n\n"
                "def test_median_even():\n"
                "    assert median([4, 1, 3, 2]) == 2.5\n\n\n"
                "def test_median_empty_raises():\n"
                "    try:\n"
                "        median([])\n"
                "        raise AssertionError('expected ValueError')\n"
                "    except ValueError:\n"
                "        pass\n"
            ),
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
            "test_paging.py": (
                "from paging import page\n\n\n"
                "def test_page_first():\n"
                "    assert page([1, 2, 3, 4, 5], 1, 2) == [1, 2]\n\n\n"
                "def test_page_last_partial():\n"
                "    assert page([1, 2, 3, 4, 5], 3, 2) == [5]\n\n\n"
                "def test_page_beyond_is_empty():\n"
                "    assert page([1, 2], 5, 2) == []\n"
            ),
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
            "test_users_query.py": (
                "import sqlite3\n"
                "from users_query import find_user\n\n\n"
                "def test_find_user_parameterized():\n"
                "    conn = sqlite3.connect(':memory:')\n"
                "    conn.execute('CREATE TABLE users (id INT, email TEXT, plan TEXT)')\n"
                "    conn.execute(\"INSERT INTO users VALUES (1, 'a@b.c', 'free')\")\n"
                "    row = find_user(conn, 'a@b.c')\n"
                "    assert row == (1, 'a@b.c', 'free')\n"
                "    assert find_user(conn, 'nobody@x.y') is None\n"
            ),
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
            "test_count_config.py": (
                "from count_config import count_active_lines\n\n\n"
                "def test_counts_active_only(tmp_path):\n"
                "    f = tmp_path / 'c.conf'\n"
                "    f.write_text('# comment\\n\\nreal = 1\\n  \\nother = 2\\n')\n"
                "    assert count_active_lines(str(f)) == 2\n"
            ),
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
            "test_threshold.py": (
                "from threshold import is_critical\n\n\n"
                "def test_boundary_at_90():\n"
                "    assert is_critical(90) is True\n\n\n"
                "def test_below_boundary():\n"
                "    assert is_critical(89) is False\n"
            ),
            "threshold.py": (
                "def is_critical(sev_score: int) -> bool:\n"
                "    'Critical when score >= 90.'\n"
                "    return sev_score >= 90\n"
            ),
        },
    },
    {
        "id": "clean-safe-path",
        "origin": "correct counterpart of path-join-escape",
        "title": "Add attachment download helper",
        "files": {
            "test_attachments_safe.py": (
                "import pytest\n"
                "from attachments_safe import read_attachment\n"
                "\n"
                "\n"
                "def test_reads_file_inside_root(tmp_path):\n"
                "    (tmp_path / 'a.txt').write_bytes(b'hi')\n"
                "    assert read_attachment(tmp_path, 'a.txt') == b'hi'\n"
                "\n"
                "\n"
                "def test_rejects_escape(tmp_path):\n"
                "    with pytest.raises(ValueError):\n"
                "        read_attachment(tmp_path, '../outside.txt')\n"
            ),
            "attachments_safe.py": (
                "from pathlib import Path\n"
                "\n"
                "\n"
                "def read_attachment(root: Path, name: str) -> bytes:\n"
                "    'Return an uploaded file by name; names may not leave root.'\n"
                "    base = root.resolve()\n"
                "    target = (base / name).resolve()\n"
                "    if not target.is_relative_to(base):\n"
                "        raise ValueError('attachment name escapes the upload root')\n"
                "    return target.read_bytes()\n"
            ),
        },
    },
    {
        "id": "clean-subprocess-argv",
        "origin": "correct counterpart of shell-true-grep",
        "title": "Add log search helper",
        "files": {
            "test_log_search_safe.py": (
                "from log_search_safe import count_matches\n"
                "\n"
                "\n"
                "def test_counts_and_treats_pattern_as_data(tmp_path):\n"
                "    log = tmp_path / 'app.log'\n"
                "    log.write_text('ok\\nerror one\\nerror; echo x\\n')\n"
                "    assert count_matches('error', str(log)) == 2\n"
                "    assert count_matches('x; echo PWNED', str(log)) == 0\n"
            ),
            "log_search_safe.py": (
                "import subprocess\n"
                "\n"
                "\n"
                "def count_matches(pattern: str, log_path: str) -> int:\n"
                "    'Count log lines matching pattern (passed as an argument, no shell).'\n"
                '    out = subprocess.run(["grep", "-c", "-e", pattern, "--", log_path],\n'
                "                         capture_output=True, text=True, timeout=30)\n"
                "    if out.returncode not in (0, 1):  # 1 = no lines matched\n"
                "        raise RuntimeError(out.stderr.strip() or 'grep failed')\n"
                "    return int(out.stdout.strip() or 0)\n"
            ),
        },
    },
    {
        "id": "clean-default-none",
        "origin": "correct counterpart of shared-default-list",
        "title": "Add tag builder",
        "files": {
            "test_tags_safe.py": (
                "from tags_safe import with_tag\n"
                "\n"
                "\n"
                "def test_fresh_list_each_call():\n"
                "    assert with_tag('a') == ['a']\n"
                "    assert with_tag('b') == ['b']\n"
                "\n"
                "\n"
                "def test_extends_given_list_without_mutating_it():\n"
                "    given = ['x']\n"
                "    assert with_tag('y', given) == ['x', 'y']\n"
                "    assert given == ['x']\n"
            ),
            "tags_safe.py": (
                "def with_tag(tag: str, tags: list[str] | None = None) -> list[str]:\n"
                "    'Return a new tag list: the given tags (if any) plus tag.'\n"
                "    return [*(tags or []), tag]\n"
            ),
        },
    },
    {
        "id": "clean-true-mean",
        "origin": "correct counterpart of floor-division-mean",
        "title": "Add latency average",
        "files": {
            "test_latency_safe.py": (
                "import pytest\n"
                "from latency_safe import mean_latency_ms\n"
                "\n"
                "\n"
                "def test_mean_keeps_fraction():\n"
                "    assert mean_latency_ms([1.0, 2.0]) == 1.5\n"
                "\n"
                "\n"
                "def test_empty_raises():\n"
                "    with pytest.raises(ValueError):\n"
                "        mean_latency_ms([])\n"
            ),
            "latency_safe.py": (
                "def mean_latency_ms(samples: list[float]) -> float:\n"
                "    'Arithmetic mean of latency samples in milliseconds.'\n"
                "    if not samples:\n"
                "        raise ValueError('no samples')\n"
                "    return sum(samples) / len(samples)\n"
            ),
        },
    },
    {
        "id": "clean-invoice-sum",
        "origin": "correct counterpart of range-skips-last",
        "title": "Add invoice total",
        "files": {
            "test_invoice_safe.py": (
                "from invoice_safe import invoice_total\n"
                "\n"
                "\n"
                "def test_sums_every_line():\n"
                "    assert invoice_total([100, 250]) == 350\n"
                "\n"
                "\n"
                "def test_empty_invoice_is_zero():\n"
                "    assert invoice_total([]) == 0\n"
            ),
            "invoice_safe.py": (
                "def invoice_total(line_amounts: list[int]) -> int:\n"
                "    'Sum of every invoice line amount, in cents.'\n"
                "    return sum(line_amounts)\n"
            ),
        },
    },
    {
        "id": "clean-fullmatch",
        "origin": "correct counterpart of regex-match-prefix",
        "title": "Add username validation",
        "files": {
            "test_usernames_safe.py": (
                "from usernames_safe import is_valid_username\n"
                "\n"
                "\n"
                "def test_accepts_plain_names():\n"
                "    assert is_valid_username('bob_42')\n"
                "\n"
                "\n"
                "def test_rejects_trailing_junk_and_short():\n"
                "    assert not is_valid_username('bob; DROP TABLE users')\n"
                "    assert not is_valid_username('ab')\n"
            ),
            "usernames_safe.py": (
                "import re\n"
                "\n"
                "_USERNAME = re.compile(r'[a-z0-9_]{3,20}')\n"
                "\n"
                "\n"
                "def is_valid_username(name: str) -> bool:\n"
                "    'True when name is 3-20 chars of lowercase letters, digits, underscore.'\n"
                "    return _USERNAME.fullmatch(name) is not None\n"
            ),
        },
    },
    {
        "id": "clean-json-session",
        "origin": "correct counterpart of pickle-request-body",
        "title": "Add session import endpoint",
        "files": {
            "test_session_import_safe.py": (
                "import base64\n"
                "import json\n"
                "\n"
                "import pytest\n"
                "from session_import_safe import import_session\n"
                "\n"
                "\n"
                "def _blob(obj):\n"
                "    return base64.b64encode(json.dumps(obj).encode()).decode()\n"
                "\n"
                "\n"
                "def test_round_trip():\n"
                "    assert import_session(_blob({'user': 'a'})) == {'user': 'a'}\n"
                "\n"
                "\n"
                "def test_non_object_rejected():\n"
                "    with pytest.raises(ValueError):\n"
                "        import_session(_blob([1, 2]))\n"
            ),
            "session_import_safe.py": (
                "import base64\n"
                "import json\n"
                "\n"
                "\n"
                "def import_session(body: str) -> dict:\n"
                "    'Decode a client-supplied session blob (base64 JSON object) into a dict.'\n"
                "    data = json.loads(base64.b64decode(body, validate=True))\n"
                "    if not isinstance(data, dict):\n"
                "        raise ValueError('session blob must be a JSON object')\n"
                "    return data\n"
            ),
        },
    },
    {
        "id": "clean-counter",
        "origin": "correct counterpart of counter-keyerror",
        "title": "Add word frequency helper",
        "files": {
            "test_wordfreq_safe.py": (
                "from wordfreq_safe import word_counts\n"
                "\n"
                "\n"
                "def test_counts_repeats():\n"
                "    assert word_counts('a b a') == {'a': 2, 'b': 1}\n"
                "\n"
                "\n"
                "def test_empty_text():\n"
                "    assert word_counts('') == {}\n"
            ),
            "wordfreq_safe.py": (
                "from collections import Counter\n"
                "\n"
                "\n"
                "def word_counts(text: str) -> dict[str, int]:\n"
                "    'Count how often each whitespace-separated word appears.'\n"
                "    return dict(Counter(text.split()))\n"
            ),
        },
    },
    {
        "id": "clean-status-equality",
        "origin": "correct counterpart of is-string-compare",
        "title": "Add job status check",
        "files": {
            "test_jobs_safe.py": (
                "from jobs_safe import is_finished\n"
                "\n"
                "\n"
                "def test_done_by_value():\n"
                "    assert is_finished(''.join(['do', 'ne']))\n"
                "\n"
                "\n"
                "def test_other_status():\n"
                "    assert not is_finished('running')\n"
            ),
            "jobs_safe.py": (
                "def is_finished(status: str) -> bool:\n"
                "    'True when a job status string reports completion.'\n"
                '    return status == "done"\n'
            ),
        },
    },
    {
        "id": "clean-bound-handlers",
        "origin": "correct counterpart of lambda-late-binding",
        "title": "Add per-level log handlers",
        "files": {
            "test_handlers_safe.py": (
                "from handlers_safe import build_handlers\n"
                "\n"
                "\n"
                "def test_each_handler_keeps_its_level():\n"
                "    h = build_handlers()\n"
                "    assert [h[k]() for k in ('debug', 'info', 'error')] == ['debug', 'info', 'error']\n"
            ),
            "handlers_safe.py": (
                "LEVELS = ['debug', 'info', 'error']\n"
                "\n"
                "\n"
                "def build_handlers() -> dict:\n"
                "    'Map each level name to a callable returning that level name.'\n"
                "    return {level: (lambda level=level: level) for level in LEVELS}\n"
            ),
        },
    },
    {
        "id": "clean-token-compare",
        "origin": "independent clean case (constant-time secret compare)",
        "title": "Add webhook token check",
        "files": {
            "test_webhook_auth.py": (
                "from webhook_auth import token_matches\n"
                "\n"
                "\n"
                "def test_matching_and_mismatching():\n"
                "    assert token_matches('abc123', 'abc123')\n"
                "    assert not token_matches('abc124', 'abc123')\n"
                "    assert not token_matches('', 'abc123')\n"
            ),
            "webhook_auth.py": (
                "import hmac\n"
                "\n"
                "\n"
                "def token_matches(supplied: str, expected: str) -> bool:\n"
                "    'Constant-time comparison of a webhook token against the configured one.'\n"
                "    return hmac.compare_digest(supplied.encode(), expected.encode())\n"
            ),
        },
    },
    {
        "id": "clean-retry",
        "origin": "independent clean case (bounded retry)",
        "title": "Add bounded retry helper",
        "files": {
            "test_retry.py": (
                "import pytest\n"
                "from retry import retry\n"
                "\n"
                "\n"
                "def test_succeeds_after_transient_failures():\n"
                "    calls, sleeps = [], []\n"
                "\n"
                "    def flaky():\n"
                "        calls.append(1)\n"
                "        if len(calls) < 3:\n"
                "            raise ConnectionError('blip')\n"
                "        return 'ok'\n"
                "\n"
                "    assert retry(flaky, attempts=3, sleep=sleeps.append) == 'ok'\n"
                "    assert sleeps == [0.5, 1.0]\n"
                "\n"
                "\n"
                "def test_gives_up_and_reraises():\n"
                "    def always():\n"
                "        raise ConnectionError('down')\n"
                "\n"
                "    with pytest.raises(ConnectionError):\n"
                "        retry(always, attempts=2, sleep=lambda s: None)\n"
            ),
            "retry.py": (
                "import time\n"
                "from typing import Callable, TypeVar\n"
                "\n"
                "T = TypeVar('T')\n"
                "\n"
                "\n"
                "def retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 0.5,\n"
                "          sleep: Callable[[float], None] = time.sleep) -> T:\n"
                "    'Call fn, retrying ConnectionError with doubling delay; re-raise the last.'\n"
                "    if attempts < 1:\n"
                "        raise ValueError('attempts must be >= 1')\n"
                "    for attempt in range(attempts - 1):\n"
                "        try:\n"
                "            return fn()\n"
                "        except ConnectionError:\n"
                "            sleep(base_delay * 2 ** attempt)\n"
                "    return fn()  # last attempt: its error propagates\n"
            ),
        },
    },
    {
        "id": "clean-chunks",
        "origin": "independent clean case (list chunking)",
        "title": "Add batch chunker",
        "files": {
            "test_batching.py": (
                "import pytest\n"
                "from batching import chunks\n"
                "\n"
                "\n"
                "def test_even_and_ragged_tail():\n"
                "    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n"
                "    assert chunks([], 3) == []\n"
                "\n"
                "\n"
                "def test_rejects_nonpositive_size():\n"
                "    with pytest.raises(ValueError):\n"
                "        chunks([1], 0)\n"
            ),
            "batching.py": (
                "def chunks(items: list, size: int) -> list[list]:\n"
                "    'Split items into consecutive batches of at most size elements.'\n"
                "    if size < 1:\n"
                "        raise ValueError('size must be >= 1')\n"
                "    return [items[i:i + size] for i in range(0, len(items), size)]\n"
            ),
        },
    },
    {
        "id": "clean-slugify",
        "origin": "independent clean case (string normalization)",
        "title": "Add URL slug helper",
        "files": {
            "test_slugs.py": (
                "from slugs import slugify\n"
                "\n"
                "\n"
                "def test_basic_and_punctuation():\n"
                "    assert slugify('Hello, World!') == 'hello-world'\n"
                "    assert slugify('  --Already-Slugged--  ') == 'already-slugged'\n"
                "\n"
                "\n"
                "def test_nothing_usable():\n"
                "    assert slugify('!!!') == ''\n"
            ),
            "slugs.py": (
                "import re\n"
                "\n"
                "\n"
                "def slugify(title: str) -> str:\n"
                "    'Lowercase ASCII slug: runs of non-alphanumerics become one hyphen.'\n"
                "    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')\n"
            ),
        },
    },
    {
        "id": "clean-env-flag",
        "origin": "independent clean case (env parsing)",
        "title": "Add feature flag reader",
        "files": {
            "test_flags.py": (
                "import pytest\n"
                "from flags import env_flag\n"
                "\n"
                "\n"
                "def test_truthy_falsy_and_default(monkeypatch):\n"
                "    monkeypatch.setenv('F', ' Yes ')\n"
                "    assert env_flag('F') is True\n"
                "    monkeypatch.setenv('F', '0')\n"
                "    assert env_flag('F') is False\n"
                "    monkeypatch.delenv('F')\n"
                "    assert env_flag('F', default=True) is True\n"
                "\n"
                "\n"
                "def test_garbage_raises(monkeypatch):\n"
                "    monkeypatch.setenv('F', 'maybe')\n"
                "    with pytest.raises(ValueError):\n"
                "        env_flag('F')\n"
            ),
            "flags.py": (
                "import os\n"
                "\n"
                "_TRUE = {'1', 'true', 'yes', 'on'}\n"
                "_FALSE = {'0', 'false', 'no', 'off', ''}\n"
                "\n"
                "\n"
                "def env_flag(name: str, default: bool = False) -> bool:\n"
                "    'Read a boolean feature flag from the environment; unknown values raise.'\n"
                "    raw = os.environ.get(name)\n"
                "    if raw is None:\n"
                "        return default\n"
                "    value = raw.strip().lower()\n"
                "    if value in _TRUE:\n"
                "        return True\n"
                "    if value in _FALSE:\n"
                "        return False\n"
                "    raise ValueError(f'{name} must be a boolean flag, got {raw!r}')\n"
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
