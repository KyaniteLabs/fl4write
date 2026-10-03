"""Grounding holes: ungroundable blocks and clipped spans.

Cycle-3 autoloop findings: binary blobs, mode-change-only diffs and pure
deletions have no new-file lines, yet any cited line was accepted; and
span ends came from hunk headers even when truncation cut the hunk short,
so findings past the cut (within slack) cited code never sent.
"""
from __future__ import annotations

import json

from fl4write import config as cfg
from fl4write.analyzer import _diff_line_spans, _line_outside_diff, analyze
from fl4write.models import PullRequest

RAW = {
    "repo": "o/r",
    "forges": {
        "github": {
            "role": "primary", "api_base": "https://api.github.com", "token_env": "GHT",
        }
    },
    "model": {"endpoint": "http://model/v1", "model": "test", "key_env": "MK"},
    "review": {"general": "catch defects"},
    "severity_vocab": ["Critical", "Major", "Minor", "Nit"],
}


def _config():
    return cfg.RepoConfig.model_validate(RAW)


def _pr():
    return PullRequest(forge="github", number=1, repo="o/r", head_sha="a" * 40)


def _item(path, line):
    return {"rule_id": "general", "severity": "Major", "path": path,
            "line": line, "category": "c",
            "message": "Unsanitized input flows into the query in this change."}


def _analyze(monkeypatch, path, line, diff_text):
    item = _item(path, line)
    monkeypatch.setattr(
        "fl4write.analyzer._call_model",
        lambda route, prompt, mode="pr", system=None, **kw: json.dumps({"findings": [item]}),
    )
    return analyze(_pr(), {path}, diff_text, _config())


DELETED = ("diff --git a/gone.py b/gone.py\n"
           "deleted file mode 100644\n"
           "--- a/gone.py\n"
           "+++ /dev/null\n"
           "@@ -1,5 +0,0 @@\n"
           "-dead one\n-dead two\n")

BINARY = ("diff --git a/logo.png b/logo.png\n"
          "new file mode 100644\n"
          "Binary files /dev/null and b/logo.png differ\n")

MODE_ONLY = ("diff --git a/run.sh b/run.sh\n"
             "old mode 100644\n"
             "new mode 100755\n")


class TestUngroundableBlocks:
    def test_deleted_file_has_no_spans(self):
        assert _diff_line_spans(DELETED)["gone.py"] == []

    def test_deleted_file_finding_dropped(self, monkeypatch):
        assert _analyze(monkeypatch, "gone.py", 2, DELETED).findings == []

    def test_binary_finding_dropped(self, monkeypatch):
        assert _analyze(monkeypatch, "logo.png", 1, BINARY).findings == []

    def test_mode_change_only_finding_dropped(self, monkeypatch):
        assert _analyze(monkeypatch, "run.sh", 1, MODE_ONLY).findings == []

    def test_unknown_path_keeps_old_accept(self):
        assert _line_outside_diff("nope.py", 3, DELETED) is False


class TestClippedSpans:
    def _cut_diff(self):
        head = ("diff --git a/big.py b/big.py\n+++ b/big.py\n"
                "@@ -0,0 +1,2000 @@\n" + "+visible\n" * 40)
        return head  # header claims 2000 lines; only 40 present (cut)

    def test_span_clips_to_present_lines(self):
        assert _diff_line_spans(self._cut_diff())["big.py"] == [(1, 40)]

    def test_beyond_cut_finding_dropped(self, monkeypatch):
        assert _analyze(monkeypatch, "big.py", 500, self._cut_diff()).findings == []

    def test_visible_line_still_posts(self, monkeypatch):
        doc = _analyze(monkeypatch, "big.py", 20, self._cut_diff())
        assert [f.path for f in doc.findings] == ["big.py"]
