"""Mutation-testing pins for fl4write/forges.py.

A mutmut run over forges.py found whole Forgejo write paths (comment create
and update, issue open and update, head_sha) with no test at all, and the
shared transport (_call/_call_text retry, throttle and error wrapping) mostly
unguarded. These adapters post as the bot and feed every engine decision, so
each contract is pinned against BOTH forges.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from fl4write import config as cfg
from fl4write import forges
from fl4write.forges import ForgeError, ForgejoAdapter, GitHubAdapter, is_own_identity

MARK = "<!-- fl4write:v1:"


def _gh():
    return GitHubAdapter(cfg.ForgeBinding(
        role="primary", api_base="https://api.github.com/", token_env="GHT"))


def _fj():
    return ForgejoAdapter(cfg.ForgeBinding(
        role="primary", api_base="https://forgejo.test/api/v1", token_env="FJT"))


@pytest.fixture(params=["github", "forgejo"])
def adapter(request):
    return _gh() if request.param == "github" else _fj()


class Calls:
    """Stands in for adapter._call: routes by (method, path prefix)."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list = []

    def __call__(self, method, path, payload=None, _retry=True):
        self.seen.append((method, path, payload))
        for (m, prefix), reply in self.routes.items():
            if m == method and path.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply(path) if callable(reply) else reply
        raise AssertionError(f"unrouted {method} {path}")


def _route(monkeypatch, adapter, routes):
    calls = Calls(routes)
    monkeypatch.setattr(adapter, "_call", calls)
    return calls


# ---------------------------------------------------------------- identity
@pytest.mark.parametrize("author,bot,own", [
    ("fl4write[bot]", "fl4write[bot]", True),
    ("SimonG", "simong", True),                      # logins are case-insensitive
    ("simong", "SimonG", True),
    ("KyaniteLabs[bot]", "someone-else", True),      # legacy slug, any case
    ("FL4WRITE[BOT]", "someone-else", True),
    ("mallory", "fl4write[bot]", False),
    ("fl4write", "fl4write[bot]", False),
    (None, "fl4write[bot]", False),
    ("fl4write[bot]", None, False),                  # no configured identity: fail closed
])
def test_is_own_identity(author, bot, own):
    assert is_own_identity(author, bot) is own


def test_mixed_case_bot_login_still_finds_its_own_comment(monkeypatch, adapter):
    # the bug: author was lower-cased but bot_login was not, so a PAT
    # personal account with capitals never matched and re-posted every cycle
    adapter.bot_login = "SimonGonzalez"
    rows = [{"id": 11, "body": MARK + "x -->", "user": {"login": "SimonGonzalez"}}]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: rows})
    assert adapter.get_persistent_comment("o/r", 4) == (11, MARK + "x -->")


# ---------------------------------------------------------------- persistent comment
def test_persistent_comment_pages_with_the_forges_size_and_a_deep_cap(monkeypatch, adapter):
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: []})
    assert adapter.get_persistent_comment("o/r", 4) is None
    size = 100 if adapter.name == "github" else 50
    param = "per_page" if adapter.name == "github" else "limit"
    assert calls.seen == [("GET", f"/repos/o/r/issues/4/comments?page=1&{param}={size}", None)]


def test_persistent_comment_is_findable_past_page_ten(monkeypatch, adapter):
    size = adapter.comment_page_size
    filler = [{"id": 1000 + i, "body": "hi", "user": {"login": "dev"}} for i in range(size)]
    ours = {"id": 77, "body": MARK + "y -->", "user": {"login": "fl4write[bot]"}}

    def page(path):
        n = int(path.split("page=")[1].split("&")[0])
        return filler if n < 15 else [ours]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): page})
    assert adapter.get_persistent_comment("o/r", 4) == (77, MARK + "y -->")


def test_persistent_comment_needs_marker_and_authorship(monkeypatch, adapter):
    rows = [
        {"id": 1, "body": MARK + "spoof -->", "user": {"login": "mallory"}},   # hijack attempt
        {"id": 2, "body": "plain", "user": {"login": "fl4write[bot]"}},        # ours, no marker
        "junk",
        {"id": 3, "body": None, "user": {"login": "fl4write[bot]"}},
        {"id": 4, "body": "x", "user": "fl4write[bot]"},
        {"id": 5, "body": "x", "user": {"login": 5}},
        {"id": 9, "body": MARK + "real -->", "user": {"login": "fl4write[bot]"}},
    ]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: rows})
    assert adapter.get_persistent_comment("o/r", 4) == (9, MARK + "real -->")


@pytest.mark.parametrize("cid", [None, True, 0, -1, "9"])
def test_own_marked_comment_with_unusable_id_is_uncertain_not_absent(monkeypatch, adapter, cid):
    rows = [{"id": cid, "body": MARK + "z -->", "user": {"login": "fl4write[bot]"}}]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: rows})
    with pytest.raises(ForgeError, match="unusable id"):
        adapter.get_persistent_comment("o/r", 4)


def test_foreign_marked_comment_with_bad_id_is_just_skipped(monkeypatch, adapter):
    rows = [{"id": None, "body": MARK + "z -->", "user": {"login": "mallory"}}]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: rows})
    assert adapter.get_persistent_comment("o/r", 4) is None


# ---------------------------------------------------------------- comment + issue writes
def test_create_comment_posts_the_body_and_returns_the_id(monkeypatch, adapter):
    calls = _route(monkeypatch, adapter, {("POST", "/repos/o/r/issues/4/comments"): {"id": 55}})
    assert adapter.create_comment("o/r", 4, "hello") == 55
    assert calls.seen == [("POST", "/repos/o/r/issues/4/comments", {"body": "hello"})]


@pytest.mark.parametrize("reply", [{}, {"id": None}, {"id": True}, {"id": 0}, {"id": "5"}, [], None])
def test_create_comment_without_a_usable_id_is_an_uncertain_write(monkeypatch, adapter, reply):
    _route(monkeypatch, adapter, {("POST", "/repos/o/r/issues/4/comments"): reply})
    with pytest.raises(ForgeError, match="no usable id"):
        adapter.create_comment("o/r", 4, "hello")


def test_update_comment_patches_by_comment_id(monkeypatch, adapter):
    calls = _route(monkeypatch, adapter, {("PATCH", "/repos/o/r/issues/comments/55"): {}})
    assert adapter.update_comment("o/r", 4, 55, "new") is None
    assert calls.seen == [("PATCH", "/repos/o/r/issues/comments/55", {"body": "new"})]


def test_open_issue_returns_the_number(monkeypatch, adapter):
    calls = _route(monkeypatch, adapter, {("POST", "/repos/o/r/issues"): {"number": 8}})
    assert adapter.open_issue("o/r", "T", "B") == 8
    assert calls.seen == [("POST", "/repos/o/r/issues", {"title": "T", "body": "B"})]


@pytest.mark.parametrize("reply", [{}, {"number": None}, {"number": True}, {"number": 0},
                                   {"number": "8"}, [], ForgeError("boom")])
def test_open_issue_uncertain_write_is_none(monkeypatch, adapter, reply):
    _route(monkeypatch, adapter, {("POST", "/repos/o/r/issues"): reply})
    assert adapter.open_issue("o/r", "T", "B") is None


def test_update_issue_reports_success_and_failure(monkeypatch, adapter):
    calls = _route(monkeypatch, adapter, {("PATCH", "/repos/o/r/issues/8"): {}})
    assert adapter.update_issue("o/r", 8, "B2") is True
    assert calls.seen == [("PATCH", "/repos/o/r/issues/8", {"body": "B2"})]
    _route(monkeypatch, adapter, {("PATCH", "/repos/o/r/issues/8"): ForgeError("x")})
    assert adapter.update_issue("o/r", 8, "B2") is False


# ---------------------------------------------------------------- open PR listing
def _pr_row(n, **over):
    row = {"number": n, "title": f"t{n}", "body": "b",
           "head": {"sha": f"{n:040d}", "repo": {"full_name": "o/r"}},
           "user": {"login": "dev", "type": "User"}}
    row.update(over)
    return row


def test_list_open_prs_translates_rows(monkeypatch, adapter):
    rows = [_pr_row(1), _pr_row(2, user={"login": "renovate[bot]", "type": "Bot"}, body=None)]
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/pulls?state=open"): lambda p: rows})
    prs = adapter.list_open_prs("o/r")
    assert [(p.number, p.title, p.head_sha, p.is_fork, p.is_bot_author, p.body, p.forge)
            for p in prs] == [
        (1, "t1", f"{1:040d}", False, False, "b", adapter.name),
        (2, "t2", f"{2:040d}", False, True, "", adapter.name)]
    param = "per_page" if adapter.name == "github" else "limit"
    assert calls.seen[0][1] == f"/repos/o/r/pulls?state=open&page=1&{param}=50"


@pytest.mark.parametrize("bad", ["junk", {"number": True}, {"number": 3, "head": {"sha": 7}}])
def test_list_open_prs_refuses_an_incomplete_listing(monkeypatch, adapter, bad):
    _route(monkeypatch, adapter,
           {("GET", "/repos/o/r/pulls?state=open"): lambda p: [_pr_row(1), bad]})
    with pytest.raises(ForgeError, match=r"listing incomplete \(1 malformed"):
        adapter.list_open_prs("o/r")


# ---------------------------------------------------------------- transport
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("u", code, "m", headers, None)


class Net:
    def __init__(self, monkeypatch, replies):
        self.replies = list(replies)
        self.reqs: list = []
        self.sleeps: list = []
        monkeypatch.setattr(forges.urllib.request, "urlopen", self)
        monkeypatch.setattr(forges.time, "sleep", self.sleeps.append)

    def __call__(self, req, timeout=None):
        self.reqs.append((req, timeout))
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return FakeResp(r)


def test_call_sends_auth_json_and_parses(monkeypatch):
    monkeypatch.setenv("GHT", "tok")
    net = Net(monkeypatch, [b'{"a": 1}'])
    assert _gh()._call("POST", "/x", {"k": "v"}) == {"a": 1}
    (req, timeout), = net.reqs
    assert req.full_url == "https://api.github.com/x" and req.get_method() == "POST"
    assert req.data == b'{"k": "v"}' and timeout == 30
    assert req.get_header("Authorization") == "token tok"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("User-agent") == forges.USER_AGENT


def test_call_without_token_sends_no_auth_and_empty_body_is_empty_dict(monkeypatch):
    monkeypatch.delenv("GHT", raising=False)
    net = Net(monkeypatch, [b""])
    assert _gh()._call("GET", "/x") == {}
    req = net.reqs[0][0]
    assert req.get_header("Authorization") is None and req.data is None


def test_call_non_json_success_is_a_forge_error(monkeypatch):
    Net(monkeypatch, [b"<html>proxy</html>"])
    with pytest.raises(ForgeError, match="github GET /x: non-JSON response"):
        _gh()._call("GET", "/x")


@pytest.mark.parametrize("code", [403, 429, 500, 502, 503, 504])
def test_call_get_retries_once_on_throttle(monkeypatch, code):
    net = Net(monkeypatch, [_http_error(code, "2"), b'{"ok": true}'])
    assert _gh()._call("GET", "/x") == {"ok": True}
    assert net.sleeps == [2.0] and len(net.reqs) == 2


@pytest.mark.parametrize("retry_after,wait", [
    (None, 0.0), ("99", 30.0), ("-1", 1.0), ("nan", 1.0), ("soon", 1.0), ("0", 0.0)])
def test_call_retry_after_is_bounded(monkeypatch, retry_after, wait):
    net = Net(monkeypatch, [_http_error(503, retry_after), b"{}"])
    _gh()._call("GET", "/x")
    assert net.sleeps == [wait]


def test_call_second_failure_is_a_named_forge_error(monkeypatch):
    net = Net(monkeypatch, [_http_error(503), _http_error(503)])
    with pytest.raises(ForgeError, match="github GET /x: HTTP 503"):
        _gh()._call("GET", "/x")
    assert len(net.reqs) == 2


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT"])
def test_call_writes_never_blind_retry(monkeypatch, method):
    net = Net(monkeypatch, [_http_error(503)])
    with pytest.raises(ForgeError, match="HTTP 503"):
        _gh()._call(method, "/x", {})
    assert len(net.reqs) == 1 and net.sleeps == []


def test_call_404_is_not_retried(monkeypatch):
    net = Net(monkeypatch, [_http_error(404)])
    with pytest.raises(ForgeError, match="HTTP 404"):
        _gh()._call("GET", "/x")
    assert len(net.reqs) == 1


@pytest.mark.parametrize("exc", [urllib.error.URLError("dns"), TimeoutError("slow"),
                                 ConnectionResetError("rst"), ValueError("bad url")])
def test_call_transport_errors_retry_get_once_then_wrap(monkeypatch, exc):
    net = Net(monkeypatch, [exc, exc])
    with pytest.raises(ForgeError, match="github GET /x: "):
        _gh()._call("GET", "/x")
    assert len(net.reqs) == 2 and net.sleeps == [1]
    net = Net(monkeypatch, [exc])
    with pytest.raises(ForgeError):
        _gh()._call("POST", "/x", {})
    assert len(net.reqs) == 1 and net.sleeps == []


def test_call_text_returns_raw_text_with_the_same_retry_rules(monkeypatch):
    monkeypatch.setenv("FJT", "fj")
    net = Net(monkeypatch, [_http_error(429, "3"), b"diff --git a/x b/x\n"])
    assert _fj()._call_text("GET", "/d.diff") == "diff --git a/x b/x\n"
    req, timeout = net.reqs[1]
    assert req.full_url == "https://forgejo.test/api/v1/d.diff" and timeout == 30
    assert req.get_header("Authorization") == "token fj" and net.sleeps == [3.0]
    for exc in (urllib.error.URLError("x"), OSError("y")):
        net = Net(monkeypatch, [exc, exc])
        with pytest.raises(ForgeError, match="forgejo GET /d.diff"):
            _fj()._call_text("GET", "/d.diff")
        assert net.sleeps == [1]
    net = Net(monkeypatch, [_http_error(500), _http_error(500)])
    with pytest.raises(ForgeError, match="HTTP 500"):
        _fj()._call_text("GET", "/d.diff")
    net = Net(monkeypatch, [_http_error(410)])
    with pytest.raises(ForgeError, match="HTTP 410"):
        _fj()._call_text("GET", "/d.diff")
    assert net.sleeps == []


# ---------------------------------------------------------------- pagination
def test_paginated_stops_on_a_short_page_and_refuses_a_capped_one(monkeypatch, adapter):
    calls = _route(monkeypatch, adapter, {("GET", "/l"): lambda p: [{}] * (2 if "page=1&" in p else 1)})
    assert len(adapter._paginated("/l?x=1", page_size=2)) == 3
    assert [c[1].split("?")[1] for c in calls.seen] == [
        f"x=1&page=1&{adapter.page_size_param}=2", f"x=1&page=2&{adapter.page_size_param}=2"]
    _route(monkeypatch, adapter, {("GET", "/l"): lambda p: [{}, {}]})
    with pytest.raises(ForgeError, match="stopped at 3 FULL pages"):
        adapter._paginated("/l", page_size=2, max_pages=3)
    _route(monkeypatch, adapter, {("GET", "/l"): {"not": "a list"}})
    with pytest.raises(ForgeError, match="unexpected shape"):
        adapter._paginated("/l", page_size=2)


# ---------------------------------------------------------------- forgejo head + diff
def test_forgejo_head_sha_follows_the_quoted_default_branch(monkeypatch):
    fj = _fj()
    calls = _route(monkeypatch, fj, {("GET", "/repos/o/r/commits/"): {"sha": "abc"},
                                     ("GET", "/repos/o/r"): {"default_branch": "release/1.x"}})
    assert fj.head_sha("o/r") == "abc"
    assert calls.seen[1][1] == "/repos/o/r/commits/release%2F1.x"


@pytest.mark.parametrize("repo_info,commit,expect", [
    ({"default_branch": 7}, {"sha": "s"}, "s"),          # non-string branch -> main
    ([], {"sha": "s"}, None),
    ({}, {"sha": ""}, None),
    ({}, ["s"], None),
    (ForgeError("x"), None, None),
])
def test_forgejo_head_sha_degrades(monkeypatch, repo_info, commit, expect):
    fj = _fj()
    calls = _route(monkeypatch, fj, {("GET", "/repos/o/r/commits/"): commit,
                                     ("GET", "/repos/o/r"): repo_info})
    assert fj.head_sha("o/r") == expect
    if expect:
        assert calls.seen[1][1] == "/repos/o/r/commits/main"


def test_forgejo_get_pr_diff(monkeypatch):
    fj = _fj()
    seen = []
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setattr(fj, "_call_text", lambda m, p: seen.append((m, p)) or diff)
    assert fj.get_pr_diff("o/r", 6) == ({"x.py"}, diff)
    assert seen == [("GET", "/repos/o/r/pulls/6.diff")]
    for raw in ("", "<html>error</html>", "diff --git nonsense"):
        monkeypatch.setattr(fj, "_call_text", lambda m, p, raw=raw: raw)
        assert fj.get_pr_diff("o/r", 6) is None
    monkeypatch.setattr(fj, "_call_text", lambda m, p: (_ for _ in ()).throw(ForgeError("x")))
    assert fj.get_pr_diff("o/r", 6) is None


def test_forgejo_has_no_check_runs():
    assert _fj().head_check_runs("o/r") is None


def test_adapter_routing_and_base_trim():
    assert isinstance(forges.adapter_for(_gh().binding), GitHubAdapter)
    assert isinstance(forges.adapter_for(_fj().binding), ForgejoAdapter)
    assert _gh().base == "https://api.github.com"


# ---------------------------------------------------------------- merged PR listing
def _merged(n, at, **over):
    row = _pr_row(n, merged_at=at, merged=True)
    row.update(over)
    return row


def test_list_merged_prs_window_order_and_paths(monkeypatch, adapter):
    rows = [
        _merged(1, "2026-01-01T00:00:05Z"),
        _merged(2, "2026-01-01T00:00:00Z"),       # same second as the watermark: kept
        _merged(3, "2025-12-31T23:59:59Z"),       # before: dropped
        _merged(4, "2026-01-01T01:00:00+01:00"),  # same instant, other offset: kept
        _merged(5, None),                          # closed, never merged
        _merged(6, "yesterday"),                   # unparseable: not a merge event
        _merged(7, 12345),
    ]
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/pulls?state=closed"): lambda p: rows})
    prs = adapter.list_merged_prs("o/r", "2026-01-01T00:00:00Z")
    assert [p.number for p in prs] in ([2, 4, 1], [4, 2, 1])
    assert {p.number for p in prs} == {1, 2, 4} and prs[-1].number == 1
    assert prs[-1].merged_at == "2026-01-01T00:00:05Z"
    expect = ("/repos/o/r/pulls?state=closed&sort=updated&direction=desc"
              if adapter.name == "github" else "/repos/o/r/pulls?state=closed")
    param = "per_page" if adapter.name == "github" else "limit"
    assert calls.seen[0][1] == f"{expect}&page=1&{param}=50"


def test_list_merged_prs_without_a_watermark_lists_everything(monkeypatch, adapter):
    rows = [_merged(1, "2020-01-01T00:00:00Z"), _merged(2, "2019-01-01T00:00:00Z")]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/pulls?state=closed"): lambda p: rows})
    assert [p.number for p in adapter.list_merged_prs("o/r", "")] == [2, 1]


def test_forgejo_needs_merged_true_not_just_a_timestamp(monkeypatch):
    fj = _fj()
    rows = [_merged(1, "2026-01-02T00:00:00Z", merged="true"),
            _merged(2, "2026-01-02T00:00:00Z", merged=False),
            _merged(3, "2026-01-02T00:00:00Z")]
    _route(monkeypatch, fj, {("GET", "/repos/o/r/pulls?state=closed"): lambda p: rows})
    assert [p.number for p in fj.list_merged_prs("o/r", "2026-01-01T00:00:00Z")] == [3]


@pytest.mark.parametrize("bad", ["junk", _merged(9, "2026-01-02T00:00:00Z", number=True),
                                 _merged(9, "2026-01-02T00:00:00Z", head={"sha": ""})])
def test_list_merged_prs_refuses_an_incomplete_listing(monkeypatch, adapter, bad):
    rows = [_merged(1, "2026-01-02T00:00:00Z"), bad]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/pulls?state=closed"): lambda p: rows})
    with pytest.raises(ForgeError, match=r"list_merged_prs on o/r: listing incomplete \(1 "):
        adapter.list_merged_prs("o/r", "2026-01-01T00:00:00Z")


# ---------------------------------------------------------------- row translation
@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"number": 0}, False), ({"number": -3}, False), ({"number": "4"}, False),
    ({"number": True}, False),
    ({"head": None}, False), ({"head": {"sha": 5}}, False), ({"head": {"sha": ""}}, False),
    ({"title": 5}, False), ({"body": ["x"]}, False),
    ({"user": {"login": 3}}, False),
    ({"title": None, "body": None, "user": None}, True),
])
def test_row_pr_validates_scalars_before_construction(adapter, over, ok):
    row = _pr_row(4)
    row.update(over)
    assert (adapter._row_pr("o/r", row) is not None) is ok


def test_row_pr_defaults_and_bot_type(adapter):
    # time-rot-safe: merged_at is only echoed back, never compared to the clock
    row = _pr_row(4, title=None, body=None, user={"type": "BOT"}, merged_at="2026-01-01T00:00:00Z")
    pr = adapter._row_pr("o/r", row)
    assert (pr.title, pr.body, pr.author, pr.is_bot_author, pr.merged_at, pr.repo) == \
        ("", "", "", True, "2026-01-01T00:00:00Z", "o/r")
    pr = adapter._row_pr("o/r", _pr_row(4, head={"sha": "s", "repo": {"full_name": "fork/r"}}))
    assert pr.is_fork is True and pr.author == "dev" and pr.is_bot_author is False


# ---------------------------------------------------------------- GitHub check runs
def _ci_routes(runs_pages, repo_info=None, commit=None):
    def runs(path):
        page = int(path.rsplit("page=", 1)[1])
        return runs_pages[page - 1] if page <= len(runs_pages) else {"check_runs": []}
    return {("GET", "/repos/o/r/commits/sha1/check-runs"): runs,
            ("GET", "/repos/o/r/commits/"): commit if commit is not None else {"sha": "sha1"},
            ("GET", "/repos/o/r"): repo_info if repo_info is not None else
            {"default_branch": "rel/2"}}


def test_head_check_runs_reads_every_page_of_the_quoted_branch_head(monkeypatch):
    gh = _gh()
    full = {"check_runs": [{"id": i} for i in range(100)]}
    calls = _route(monkeypatch, gh, _ci_routes([full, {"check_runs": [{"id": "last"}]}]))
    sha, runs = gh.head_check_runs("o/r")
    assert sha == "sha1" and len(runs) == 101 and runs[-1] == {"id": "last"}
    assert [c[1] for c in calls.seen] == [
        "/repos/o/r", "/repos/o/r/commits/rel%2F2",
        "/repos/o/r/commits/sha1/check-runs?per_page=100&page=1",
        "/repos/o/r/commits/sha1/check-runs?per_page=100&page=2"]


def test_head_check_runs_cap_is_exactly_one_hundred_pages(monkeypatch):
    gh = _gh()
    full = {"check_runs": [{"id": i} for i in range(100)]}
    _route(monkeypatch, gh, _ci_routes([full] * 99))
    assert len(gh.head_check_runs("o/r")[1]) == 9900
    _route(monkeypatch, gh, _ci_routes([full] * 100))
    assert gh.head_check_runs("o/r") is None              # capped evidence is incomplete


@pytest.mark.parametrize("routes", [
    _ci_routes([{"check_runs": []}], repo_info=[]),
    _ci_routes([{"check_runs": []}], commit=["x"]),
    _ci_routes([{"check_runs": []}], commit={"sha": ""}),
    _ci_routes([["not", "a", "dict"]]),
    _ci_routes([{"check_runs": "nope"}]),
    _ci_routes([{"check_runs": [{"id": 1}, "junk"]}]),
])
def test_head_check_runs_untrustworthy_evidence_is_none(monkeypatch, routes):
    gh = _gh()
    _route(monkeypatch, gh, routes)
    assert gh.head_check_runs("o/r") is None


def test_head_check_runs_odd_branch_falls_back_to_main(monkeypatch):
    gh = _gh()
    calls = _route(monkeypatch, gh, _ci_routes([{"check_runs": []}], repo_info={"default_branch": 1}))
    assert gh.head_check_runs("o/r") == ("sha1", [])
    assert calls.seen[1][1] == "/repos/o/r/commits/main"


# ---------------------------------------------------------------- recursive tree (GitHub/base)
def _tree_routes(tree, commit=None, repo_info=None):
    return {("GET", "/repos/o/r/git/trees/"): tree,
            ("GET", "/repos/o/r/commits/"): commit if commit is not None else {"sha": "t1"},
            ("GET", "/repos/o/r"): repo_info if repo_info is not None else
            {"default_branch": "a/b"}}


def test_recursive_tree_lists_blobs_and_flags_truncation(monkeypatch):
    gh = _gh()
    tree = {"truncated": False, "tree": [
        {"type": "blob", "path": "a.py", "size": 3},
        {"type": "tree", "path": "pkg", "sha": "x"},
        {"type": "blob", "path": "pkg/b.py", "size": 0}]}
    calls = _route(monkeypatch, gh, _tree_routes(tree))
    assert gh.list_tree_files("o/r") == ([("a.py", 3), ("pkg/b.py", 0)], False)
    assert [c[1] for c in calls.seen] == ["/repos/o/r", "/repos/o/r/commits/a%2Fb",
                                          "/repos/o/r/git/trees/t1?recursive=1"]
    _route(monkeypatch, gh, _tree_routes(dict(tree, truncated=True)))
    assert gh.list_tree_files("o/r")[1] is True


@pytest.mark.parametrize("row", [
    "junk", {"type": "blob", "path": "", "size": 1}, {"type": "blob", "path": 3, "size": 1},
    {"type": "blob", "path": "x", "size": True}, {"type": "blob", "path": "x", "size": -1},
    {"type": "blob", "path": "x", "size": "1"}])
def test_recursive_tree_malformed_rows_mark_truncated(monkeypatch, row):
    gh = _gh()
    _route(monkeypatch, gh, _tree_routes({"tree": [{"type": "blob", "path": "ok", "size": 1}, row]}))
    assert gh.list_tree_files("o/r") == ([("ok", 1)], True)


@pytest.mark.parametrize("routes", [
    _tree_routes({"tree": []}, repo_info=[]),
    _tree_routes({"tree": []}, commit=[]),
    _tree_routes({"tree": []}, commit={"sha": ""}),
    _tree_routes({"tree": "nope"}),
    _tree_routes(["nope"]),
])
def test_recursive_tree_unqueryable_is_none(monkeypatch, routes):
    gh = _gh()
    _route(monkeypatch, gh, routes)
    assert gh.list_tree_files("o/r") is None


# ---------------------------------------------------------------- Forgejo manual tree walk
def _walk(monkeypatch, trees, repo_info=None):
    fj = _fj()

    def tree(path):
        return trees[path.rsplit("/", 1)[1]]
    calls = _route(monkeypatch, fj, {("GET", "/repos/o/r/git/trees/"): tree,
                                     ("GET", "/repos/o/r"): repo_info or {"default_branch": "dev/x"}})
    return fj.list_tree_files("o/r"), calls


def _b(path, size=1):
    return {"type": "blob", "path": path, "size": size}


def _t(path, sha):
    return {"type": "tree", "path": path, "sha": sha}


def test_forgejo_walk_descends_subtrees_with_prefixes(monkeypatch):
    trees = {"dev%2Fx": {"tree": [_b("top.py", 5), _t("src", "S")]},
             "S": {"tree": [_b("a.py", 2), _t("deep", "D")]},
             "D": {"tree": [_b("z.py", 7)]}}
    (files, truncated), calls = _walk(monkeypatch, trees)
    assert sorted(files) == [("src/a.py", 2), ("src/deep/z.py", 7), ("top.py", 5)]
    assert truncated is False
    assert calls.seen[1][1] == "/repos/o/r/git/trees/dev%2Fx"


def test_forgejo_walk_replays_a_shared_subtree_under_each_prefix(monkeypatch):
    trees = {"main": {"tree": [_t("a", "S"), _t("b", "S")]}, "S": {"tree": [_b("f")]}}
    (files, truncated), calls = _walk(monkeypatch, trees, {"default_branch": None})
    assert sorted(files) == [("a/f", 1), ("b/f", 1)] and truncated is False
    assert [c[1] for c in calls.seen].count("/repos/o/r/git/trees/S") == 1   # cached


def test_forgejo_walk_ancestor_cycle_truncates(monkeypatch):
    trees = {"dev%2Fx": {"tree": [_t("a", "A")]}, "A": {"tree": [_b("f"), _t("loop", "A")]}}
    (files, truncated), _ = _walk(monkeypatch, trees)
    assert files == [("a/f", 1)] and truncated is True


@pytest.mark.parametrize("trees", [
    {"dev%2Fx": {"tree": [_b("ok"), "junk"]}},
    {"dev%2Fx": {"tree": [_b("ok"), {"type": "tree", "path": "t", "sha": ""}]}},
    {"dev%2Fx": {"tree": [_b("ok"), _b("bad", -1)]}},
    {"dev%2Fx": {"tree": [_b("ok")], "truncated": True}},
    {"dev%2Fx": {"tree": [_b("ok"), _t("s", "S")]}, "S": {"tree": "nope"}},
    {"dev%2Fx": {"tree": [_b("ok"), _t("s", "S")]}, "S": {"tree": [], "truncated": True}},
    {"dev%2Fx": {"tree": [_b("ok"), _t("s", "S")]}, "S": {"tree": ["junk"]}},
    {"dev%2Fx": {"tree": [_b("ok"), _t("s", "S")]}, "S": {"tree": [_t("u", None)]}},
])
def test_forgejo_walk_any_doubt_marks_truncated(monkeypatch, trees):
    (files, truncated), _ = _walk(monkeypatch, trees)
    assert ("ok", 1) in files and truncated is True


def test_forgejo_walk_bad_root_is_truncated_empty_and_errors_are_none(monkeypatch):
    assert _walk(monkeypatch, {"dev%2Fx": ["x"]})[0] == ([], True)
    fj = _fj()
    _route(monkeypatch, fj, {("GET", "/repos/o/r"): ForgeError("down")})
    assert fj.list_tree_files("o/r") is None
    _route(monkeypatch, fj, {("GET", "/repos/o/r"): ["x"]})
    assert fj.list_tree_files("o/r") is None


# ---------------------------------------------------------------- contents + annotations
@pytest.mark.parametrize("reply,exists,is_file", [
    ({"type": "file", "encoding": "base64", "content": ""}, True, True),   # zero-byte file
    ({"sha": "x", "encoding": "base64", "content": "eA=="}, True, True),
    ({"content": None, "encoding": "base64"}, True, False),
    ({"type": "file", "encoding": "none", "content": ""}, True, False),
    ([{"name": "a"}], True, False),                                          # directory
    ({"message": "weird"}, None, False),
    (None, None, None),
    (ForgeError("x HTTP 404"), False, False),
    (ForgeError("x HTTP 500"), None, None),
])
def test_path_exists_and_is_file(monkeypatch, adapter, reply, exists, is_file):
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/contents/"): reply})
    assert adapter.path_exists("o/r", "a b/c.py", "rel/1") is exists
    assert adapter.path_is_file("o/r", "a b/c.py", "rel/1") is is_file
    assert calls.seen[0][1] == "/repos/o/r/contents/a%20b%2Fc.py?ref=rel%2F1"
    calls.seen.clear()
    adapter.path_exists("o/r", "x", None)
    adapter.path_is_file("o/r", "x")
    assert [c[1] for c in calls.seen] == ["/repos/o/r/contents/x"] * 2


@pytest.mark.parametrize("reply,out", [
    ({"encoding": "base64", "content": "eD0x\nCg==\n"}, "x=1\n"),
    ({"encoding": "base64", "content": " \n "}, None),
    ({"encoding": "base64", "content": "!!!"}, None),
    ({"encoding": "base64", "content": "//4="}, None),           # not utf-8
    ({"encoding": "none", "content": "eA=="}, None),
    ({"encoding": "base64", "content": 5}, None),
    ({"encoding": "base64", "content": ""}, None),
    (["x"], None),
    (ForgeError("x"), None),
])
def test_get_file_contract(monkeypatch, adapter, reply, out):
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/contents/"): reply})
    assert adapter.get_file("o/r", "d/f.py", "feat/1") == out
    assert calls.seen[0][1] == "/repos/o/r/contents/d%2Ff.py?ref=feat%2F1"


def test_check_annotations_normalizes_rows(monkeypatch, adapter):
    rows = [{"path": "a.py", "start_line": 4, "message": "m", "annotation_level": "failure"},
            {"line": 9}, None, "junk"]
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/check-runs/7/annotations"):
                                          lambda p: rows})
    assert adapter.check_annotations("o/r", 7) == [
        {"path": "a.py", "start_line": 4, "message": "m", "level": "failure"},
        {"path": "", "start_line": 9, "message": "", "level": ""}]
    assert calls.seen[0][1].endswith(f"page=1&{adapter.page_size_param}=50")
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/check-runs/7/annotations"): ForgeError("x")})
    assert adapter.check_annotations("o/r", 7) is None


def test_reaction_summary_counts_valid_rows_only(monkeypatch, adapter):
    rows = [{"content": "+1", "user": {"login": "a"}}, {"content": "+1", "user": {"login": "b"}},
            {"content": "+1", "user": {"login": "a"}}, {"content": "", "user": {"login": "c"}},
            {"content": "-1", "user": "d"}, {"content": "-1", "user": {"login": ""}},
            "junk", {"content": "heart", "user": {"login": "e"}}]
    calls = _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/comments/3/reactions"):
                                          lambda p: rows})
    assert adapter.reaction_summary("o/r", 3) == {"+1": {"a": 1, "b": 1}, "heart": {"e": 1}}
    assert calls.seen[0][1].endswith(f"page=1&{adapter.page_size_param}=100")
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/comments/3/reactions"):
                                  ForgeError("disabled")})
    assert adapter.reaction_summary("o/r", 3) is None


@pytest.mark.parametrize("base,github", [
    ("https://api.github.com", True), ("https://api.github.com:443/", True),
    ("http://api.github.com", False), ("https://api.github.com.evil.io", False),
    ("https://api.github.com:8443", False), ("https://api.github.com:bad", False),
    ("https://git.example/api/v1", False)])
def test_github_route_needs_exact_https_host(base, github):
    assert forges._is_github_base(base) is github


def test_log_row_redacts_before_truncating():
    out = forges._log_row("github", "bad row", {"t": "token=" + "A" * 40})
    assert out.startswith("github bad row: ") and "A" * 40 not in out
    assert len(out) <= len("github bad row: ") + 120


# ---------------------------------------------------------------- round-3 gaps
@pytest.mark.parametrize("code", [403, 429, 500, 502, 503, 504])
def test_call_text_retries_every_throttle_code(monkeypatch, code):
    net = Net(monkeypatch, [_http_error(code), b"ok"])
    assert _fj()._call_text("GET", "/d") == "ok" and len(net.reqs) == 2


@pytest.mark.parametrize("retry_after,wait", [
    (None, 0.0), ("", 0.0), ("2.5", 2.5), ("30", 30.0), ("31", 30.0), ("-0.5", 1.0),
    ("nan", 1.0), ("inf", 30.0), ("soon", 1.0)])
def test_retry_wait_is_bounded(retry_after, wait):
    assert forges._retry_wait(_http_error(503, retry_after)) == wait


def test_retry_wait_without_headers_is_immediate():
    assert forges._retry_wait(urllib.error.HTTPError("u", 503, "m", None, None)) == 0.0


def test_own_comment_with_id_one_is_found(monkeypatch, adapter):
    rows = [{"id": 1, "body": MARK + "a -->", "user": {"login": "fl4write[bot]"}}]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: rows})
    assert adapter.get_persistent_comment("o/r", 4) == (1, MARK + "a -->")


def test_own_unmarked_comment_with_bad_id_is_skipped_not_raised(monkeypatch, adapter):
    rows = [{"id": None, "body": "plain note", "user": {"login": "fl4write[bot]"}}]
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/issues/4/comments"): lambda p: rows})
    assert adapter.get_persistent_comment("o/r", 4) is None


def test_a_closed_unmerged_row_does_not_hide_later_merges(monkeypatch, adapter):
    rows = [_merged(5, None), _merged(6, "not a time"),
            _merged(1, "2026-01-02T00:00:00Z")]  # time-rot-safe: no clock involved
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/pulls?state=closed"): lambda p: rows})
    assert [p.number for p in adapter.list_merged_prs("o/r", "")] == [1]


def test_annotation_without_any_line_is_line_zero(monkeypatch, adapter):
    _route(monkeypatch, adapter, {("GET", "/repos/o/r/check-runs/7/annotations"):
                                  lambda p: [{"path": "a", "start_line": 0}]})
    assert adapter.check_annotations("o/r", 7)[0]["start_line"] == 0


def test_recursive_tree_odd_default_branch_falls_back_to_main(monkeypatch):
    gh = _gh()
    calls = _route(monkeypatch, gh, _tree_routes({"tree": []}, repo_info={"default_branch": ""}))
    assert gh.list_tree_files("o/r") == ([], False)
    assert calls.seen[1][1] == "/repos/o/r/commits/main"


def test_forgejo_walk_keeps_zero_byte_files(monkeypatch):
    trees = {"dev%2Fx": {"tree": [_b("empty", 0), _t("s", "S")]}, "S": {"tree": [_b("e2", 0)]}}
    (files, truncated), _ = _walk(monkeypatch, trees)
    assert sorted(files) == [("empty", 0), ("s/e2", 0)] and truncated is False


def test_forgejo_walk_finished_subtree_is_not_a_cycle_later(monkeypatch):
    # S is fully walked under b/ first, then reached again under a/s/; only an
    # OPEN ancestor is a cycle, so the second visit must not mark truncated
    trees = {"dev%2Fx": {"tree": [_t("a", "A"), _t("b", "S")]},
             "A": {"tree": [_t("s", "S")]}, "S": {"tree": [_b("f")]}}
    (files, truncated), _ = _walk(monkeypatch, trees)
    assert sorted(files) == [("a/s/f", 1), ("b/f", 1)] and truncated is False
