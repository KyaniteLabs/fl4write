"""Fork detection fails closed (2026-10-12 review).

Every fork rail — no test execution on the runner (engine verify gate), fix
lane comment-only, forced fork tone — keys on PullRequest.is_fork. A PR row
whose head repo is missing, null or malformed must count as a fork, never as
same-repo."""

from __future__ import annotations

import pytest

from fl4write import config as cfg
from fl4write.forges import ForgejoAdapter, GitHubAdapter


def _row(head_repo):
    head = {"sha": "a" * 40, "ref": "feature"}
    if head_repo is not _MISSING:
        head["repo"] = head_repo
    return {"number": 7, "title": "t", "body": "", "head": head,
            "user": {"login": "someone", "type": "User"}, "merged_at": ""}


_MISSING = object()


@pytest.fixture(params=["github", "forgejo"])
def adapter(request):
    if request.param == "github":
        return GitHubAdapter(cfg.ForgeBinding(
            role="primary", api_base="https://api.github.com", token_env="GHT"))
    return ForgejoAdapter(cfg.ForgeBinding(
        role="primary", api_base="https://forgejo.test/api/v1", token_env="FJT"))


@pytest.mark.parametrize("head_repo", [
    None,                        # GitHub: fork deleted after the PR opened
    _MISSING,                    # key absent
    {},                          # repo object without full_name
    {"full_name": None},
    {"full_name": 123},          # wrong type
    {"full_name": "attacker/r"},  # ordinary fork
])
def test_unknown_or_foreign_head_repo_is_a_fork(adapter, head_repo):
    pr = adapter._row_pr("o/r", _row(head_repo))
    assert pr is not None
    assert pr.is_fork is True


def test_same_repo_head_is_not_a_fork(adapter):
    pr = adapter._row_pr("o/r", _row({"full_name": "o/r"}))
    assert pr is not None
    assert pr.is_fork is False
