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
