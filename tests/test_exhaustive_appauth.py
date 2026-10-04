import json

import pytest

from fl4write import appauth


def test_repository_token_requests_only_one_repo_and_minimum_grants(monkeypatch):
    appauth._TOKEN_CACHE.clear()
    monkeypatch.setattr(appauth, "verified_app_login", lambda: "fl4write[bot]")
    monkeypatch.setattr(appauth, "resolve_installation_id", lambda repo: 7)
    monkeypatch.setattr(appauth, "_make_jwt", lambda: "synthetic-jwt")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n=-1): return json.dumps(self.payload).encode()
    def open_request(request, timeout):
        payload = json.loads(request.data)
        assert payload["repositories"] == ["widget"]
        assert payload["permissions"] == {
            "contents": "write", "pull_requests": "write", "issues": "write",
            "actions": "read", "statuses": "read",
        }
        response = Response()
        response.payload = {"token": "synthetic-token", "repositories": [{"full_name": "acme/widget"}],
                            "permissions": payload["permissions"]}
        return response
    monkeypatch.setattr(appauth.urllib.request, "urlopen", open_request)
    assert appauth.get_repository_token("acme/widget", "fl4write[bot]") == "synthetic-token"


def test_app_identity_mismatch_stops_before_mint(monkeypatch):
    appauth._TOKEN_CACHE.clear()
    monkeypatch.setattr(appauth, "verified_app_login", lambda: "other[bot]")
    monkeypatch.setattr(appauth, "resolve_installation_id", lambda repo: 7)
    monkeypatch.setattr(appauth, "_make_jwt", lambda: "synthetic-jwt")

    def no_mint(*a, **k):
        pytest.fail("minted despite identity mismatch")

    monkeypatch.setattr(appauth.urllib.request, "urlopen", no_mint)
    with pytest.raises(RuntimeError, match="does not match"):
        appauth.get_repository_token("acme/widget", "fl4write[bot]")


def test_scoped_token_cached_with_ttl(monkeypatch):
    appauth._TOKEN_CACHE.clear()
    verified_calls = {"n": 0}

    def fake_verify():
        verified_calls["n"] += 1
        return "fl4write[bot]"

    monkeypatch.setattr(appauth, "verified_app_login", fake_verify)
    monkeypatch.setattr(appauth, "resolve_installation_id", lambda repo: 7)
    monkeypatch.setattr(appauth, "_make_jwt", lambda: "synthetic-jwt")
    calls = {"n": 0}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n=-1):
            return json.dumps({"token": "t", "repositories": [{"full_name": "acme/widget"}],
                               "permissions": {"contents": "write", "pull_requests": "write",
                                               "issues": "write", "actions": "read",
                                               "statuses": "read"}}).encode()

    def open_request(request, timeout):
        calls["n"] += 1
        return Response()

    monkeypatch.setattr(appauth.urllib.request, "urlopen", open_request)
    assert appauth.get_repository_token("acme/widget", "fl4write[bot]") == "t"
    assert appauth.get_repository_token("acme/widget", "fl4write[bot]") == "t"
    assert calls["n"] == 1, "second call must serve cache, not re-mint"
    assert verified_calls["n"] == 1, "cache hit must skip re-verification"
    appauth._TOKEN_CACHE.clear()


def test_verified_app_login_requires_exact_signing_app_id(monkeypatch):
    monkeypatch.setattr(appauth, "_api", lambda *a: {"id": appauth.APP_ID + 1, "slug": "fl4write"})
    with pytest.raises(RuntimeError, match="identity"):
        appauth.verified_app_login()


def test_publication_app_token_stays_out_of_environment_and_is_unbound_afterward(monkeypatch):
    import os
    from fl4write.config import RepoConfig
    from fl4write.exhaustive_transaction import _publication_adapter

    config = RepoConfig.model_validate({
        "repo": "acme/widget", "forges": {"origin": {"role": "primary",
            "api_base": "https://api.github.com", "token_env": "TEST_PUBLICATION_TOKEN"}},
        "model": {"endpoint": "https://model.invalid", "model": "test"},
    })
    monkeypatch.setenv("TEST_PUBLICATION_TOKEN", "original-token")
    monkeypatch.setattr(appauth, "get_repository_token", lambda *a: "scoped-publication-token")
    monkeypatch.setattr(appauth, "verified_app_login", lambda: config.bot_login)
    with _publication_adapter(None, config) as adapter:
        assert adapter._headers()["Authorization"] == "Bearer scoped-publication-token"
        assert adapter._call("GET", "/user") == {"login": config.bot_login}
        assert os.environ["TEST_PUBLICATION_TOKEN"] == "original-token"
    assert "scoped-publication-token" not in str(adapter._headers())


def test_oversize_api_body_refused(monkeypatch):
    from fl4write import appauth as _appauth

    class _Big:
        def read(self, n=-1):
            return b"x" * (_appauth._MAX_API_BODY + 1)

    with pytest.raises(RuntimeError, match="exceeds"):
        _appauth._bounded_json_body(_Big(), "probe")


def test_superset_token_grants_refused(monkeypatch):
    import json as _json

    from fl4write import appauth as _appauth

    _appauth._TOKEN_CACHE.clear()
    monkeypatch.setattr(_appauth, "verified_app_login", lambda: "fl4write[bot]")
    monkeypatch.setattr(_appauth, "resolve_installation_id", lambda repo: 7)
    monkeypatch.setattr(_appauth, "_make_jwt", lambda: "synthetic-jwt")
    grants = {"contents": "write", "pull_requests": "write", "issues": "write",
              "actions": "read", "statuses": "read", "administration": "write"}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n=-1):
            return _json.dumps({"token": "t", "repositories": [{"full_name": "acme/widget"}],
                                "permissions": grants}).encode()

    monkeypatch.setattr(_appauth.urllib.request, "urlopen", lambda req, timeout: Response())
    with pytest.raises(RuntimeError, match="scope"):
        _appauth.get_repository_token("acme/widget", "fl4write[bot]")
    _appauth._TOKEN_CACHE.clear()
