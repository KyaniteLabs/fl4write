"""GitHub App authentication — generates installation tokens.

Uses the org's `kyanitelabs` GitHub App (ID 3592379) so every fl4write
interaction shows as `fl4write[bot]` — its own badge, avatar, and identity,
separate from any personal account.

The app has TWO installations (org-wide on KyaniteLabs + all-repos on
simongonzalezdc). A token minted from one installation has NO access to the
other account's repos — the installation must be resolved PER REPO, never
hardcoded (the hardcoded org ID 404'd every personal repo for days).

Resolution: GET /repos/{owner}/{repo}/installation with the app JWT returns
the installation covering that exact repo, whichever account owns it.

The private key lives at ~/.sinter/forgejo/github-app-key.pem (never committed).
Token generation: JWT from the app key → installation token → use as Bearer.
Tokens expire in 1 hour; minted once per installation per process, refreshed
after 50 minutes if a process lives that long.
"""

from __future__ import annotations

import json
import time
from typing import Any
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = 3592379
DEFAULT_INSTALLATION_ID = 129423294  # KyaniteLabs org — used when no repo given
KEY_PATH = Path.home() / ".sinter" / "forgejo" / "github-app-key.pem"

# Cycle-6: bound every app-auth body read (a compromised endpoint or
# MITM must not OOM the cycle) — mirrors forges._bounded_read at API-JSON
# scale. Oversize reads fail the call like any transport error.
_MAX_API_BODY = 1024 * 1024


def _bounded_json_body(resp: Any, what: str) -> Any:
    raw = resp.read(_MAX_API_BODY + 1)
    if len(raw) > _MAX_API_BODY:
        raise RuntimeError(f"github app {what}: response exceeds {_MAX_API_BODY} bytes")
    return json.loads(raw.decode())


# installation_id -> (token, minted_at, key_fingerprint). Tokens last 1h;
# 50min refresh margin. Scoped single-repo tokens share the cache under
# ("scoped", id, repo, bot) keys. The fingerprint binds entries to the
# signing key on disk (review follow-up): a key rotation or same-slug
# App swap inside the TTL window invalidates instead of serving stale.
_TOKEN_CACHE: dict = {}
_TOKEN_TTL = 50 * 60


def _key_fingerprint() -> str:
    """sha256 of the signing key, "" when unreadable (fail-open to mint:
    a missing key fails loudly at _make_jwt, never serves foreign cache)."""
    import hashlib

    try:
        return hashlib.sha256(KEY_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


def _cache_hit(key: object) -> str | None:
    entry = _TOKEN_CACHE.get(key)
    if not entry or len(entry) != 3:
        return None
    token, minted, fingerprint = entry
    if time.time() - minted >= _TOKEN_TTL:
        return None
    if fingerprint != _key_fingerprint():
        return None
    return token


def _make_jwt() -> str:
    """Create a JWT signed with the app's private key (RS256)."""
    import jwt  # PyJWT

    private_key = KEY_PATH.read_text()
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": str(APP_ID)}
    return jwt.encode(payload, private_key, algorithm="RS256")


def _api(url: str, method: str = "GET") -> dict:
    jwt_token = _make_jwt()
    req = urllib.request.Request(

        url,
        method=method,
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:

        return _bounded_json_body(resp, 'api')


def resolve_installation_id(repo: str) -> int:
    """Return the installation ID covering `owner/repo` (org OR user account)."""
    try:
        data = _api(f"https://api.github.com/repos/{repo}/installation")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"GitHub App is not installed on any account covering {repo} "
                f"(or the repo does not exist)"
            ) from exc
        raise
    # F10-D006: envelope + id validation — a malformed/empty response must
    # fail the resolution, never cache or export an unusable installation
    if not isinstance(data, dict):
        raise RuntimeError(
            f"installation resolution for {repo}: malformed response envelope "
            f"({type(data).__name__})"
        )
    iid = data.get("id")
    if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0:
        raise RuntimeError(
            f"installation resolution for {repo}: no usable installation id"
        )
    return iid


def get_installation_token(repo: str | None = None) -> str:
    """Generate an installation token for the installation covering `repo`.

    Without `repo`, falls back to the org installation (legacy behavior).
    """
    installation_id = resolve_installation_id(repo) if repo else DEFAULT_INSTALLATION_ID
    hit = _cache_hit(installation_id)
    if hit is not None:
        return hit
    jwt_token = _make_jwt()
    req = urllib.request.Request(

        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:

        data = _bounded_json_body(resp, 'installation token')
    # F10-D006: envelope + non-empty-token validation BEFORE cache/export —
    # an empty token used to be cached and exported while the CLI still
    # selected fl4write[bot], producing unauthenticated app-identity ops
    if not isinstance(data, dict):
        raise RuntimeError(
            f"app token response: malformed envelope ({type(data).__name__})"
        )
    token = data.get("token")
    # F14-D013 (reopened F10-D006): whitespace/control-bearing tokens are
    # unusable — "   " used to be cached and exported, failing auth for the
    # whole cache lifetime while the CLI still selected the bot identity
    if not isinstance(token, str) or not token or token != token.strip() \
            or any(ord(c) < 0x20 for c in token):
        raise RuntimeError("app token response: unusable token — refusing to cache/export")
    _TOKEN_CACHE[installation_id] = (token, time.time(), _key_fingerprint())
    return token


def install_token_to_env(repo: str | None = None) -> None:
    """Generate a token for `repo`'s installation; set as CODESITTER_GITHUB_TOKEN."""
    import os

    token = get_installation_token(repo)
    os.environ["CODESITTER_GITHUB_TOKEN"] = token
    # Also set GH_TOKEN so gh CLI uses it for any subsidiary calls
    os.environ["GH_TOKEN"] = token


def verified_app_login() -> str:
    """Resolve the signing App's live bot identity without a user-token endpoint."""
    data = _api("https://api.github.com/app")
    if (not isinstance(data, dict) or data.get("id") != APP_ID
            or not isinstance(data.get("slug"), str) or not data["slug"]
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in data["slug"])):
        raise RuntimeError("GitHub App identity could not be verified")
    return data["slug"] + "[bot]"


def get_repository_token(repo: str, bot_login: str) -> str:
    """Mint a fresh single-repository token with only exhaustive-workflow rights.

    Cycle-3: scoped tokens are cached like installation tokens (same 50m
    TTL). Zero-findings review: the cache check sits after resolve but
    the bot-identity verify moved after the hit (bot_login joins the key),
    so a cached repeat costs 1 HTTPS call (resolve) instead of 3.
    """
    import re

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise RuntimeError("invalid repository for scoped GitHub App token")
    installation_id = resolve_installation_id(repo)
    cache_key = ("scoped", installation_id, repo, bot_login)
    hit = _cache_hit(cache_key)
    if hit is not None:
        return hit
    # Zero-findings review: verify AFTER the cache hit, not before — the
    # cached token was verified at mint, and bot_login joins the cache
    # key so cross-bot confusion still re-verifies. Saves 2 HTTPS calls
    # per cached repeat (verify + the resolve above stays: 1 call).
    if verified_app_login() != bot_login:
        raise RuntimeError("configured bot does not match the signing GitHub App")
    permissions = {"contents": "write", "pull_requests": "write", "issues": "write",
                   "actions": "read", "statuses": "read"}
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        data=json.dumps({"repositories": [repo.split("/", 1)[1]],
                         "permissions": permissions}).encode(),
        method="POST", headers={"Authorization": f"Bearer {_make_jwt()}",
                                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        data = _bounded_json_body(response, 'scoped token')
    token = data.get("token") if isinstance(data, dict) else None
    repositories = data.get("repositories") if isinstance(data, dict) else None
    if (not isinstance(token, str) or not token or token != token.strip()
            or any(ord(c) < 0x20 for c in token)
            or not isinstance(repositories, list) or len(repositories) != 1
            or not isinstance(repositories[0], dict) or repositories[0].get("full_name") != repo
            or data.get("permissions") != permissions):
        # Cycle-6: EXACT grant match, not subset — a server returning extra
        # grants (e.g. administration:write) previously passed and widened
        # the token past least privilege without a word. The received set
        # rides the error for fast triage (review follow-up).
        raise RuntimeError(
            "GitHub App token scope could not be verified: "
            f"got {data.get('permissions')!r}, want {permissions!r}")
    _TOKEN_CACHE[cache_key] = (token, time.time(), _key_fingerprint())
    return token
