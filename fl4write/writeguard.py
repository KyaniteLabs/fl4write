"""Outbound write authorization — the single choke point for FL4WRITE writes.

Product decision (card t_c7791d3f, 2026-11-11): FL4WRITE writes only to
KyaniteLabs. Every mutating request (POST/PATCH/PUT/DELETE) is checked here
BEFORE a token is minted and BEFORE a socket is opened:

  * ``api.github.com``        owner must be ``KyaniteLabs``
  * ``git.kyanitelabs.tech``  owner ``KyaniteLabs`` or ``simon`` (the org's own
    Forgejo; ``simon/*`` there is an internal namespace, not a personal
    external account). The runner-supplied tailnet ``api_base`` is the same
    server reached over the tailnet, so it carries the same owners.
  * every other host          refused

Reads (GET) are not gated: this guard bounds WRITES, the blast radius the card
names. Refusals are FAIL-CLOSED — a mutating path that does not resolve to a
``/repos/<owner>/<repo>`` target is refused, never allowed by default. Every
refusal emits exactly one ``guard_refusal`` telemetry event with
``{reason, host, repo, method, caller}``.

Do not widen this allowlist from a repo config or a PR. The only value read
from the environment is the runner-owned ``FL4WRITE_FORGEJO_API_BASE`` (the
tailnet base the ``*.fj`` configs already expand from — run-cycle.sh), so the
guard and the configs can never disagree about which server "the org Forgejo"
is. This module is a protected path in card E2: changes to it never auto-merge.
"""

from __future__ import annotations

import os
import re
import urllib.parse

from . import telemetry

MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

GITHUB_API_HOST = "api.github.com"
ORG_FORGEJO_HOST = "git.kyanitelabs.tech"

GITHUB_WRITE_OWNERS = frozenset({"kyanitelabs"})
ORG_FORGEJO_WRITE_OWNERS = frozenset({"kyanitelabs", "simon"})

# Static allowlist. The tailnet Forgejo host is dynamic (runner env) and is
# added by _org_forgejo_hosts(); it carries ORG_FORGEJO_WRITE_OWNERS.
ALLOWLIST = {
    GITHUB_API_HOST: GITHUB_WRITE_OWNERS,
    ORG_FORGEJO_HOST: ORG_FORGEJO_WRITE_OWNERS,
}

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")


class OutOfBoundsWrite(RuntimeError):
    """A write outside the allowlist was refused before any I/O.

    Typed on purpose: call sites and tests assert on this class, and no
    caller may turn a refusal into a retry, a fallback token or a skipped
    write that looks successful.
    """

    def __init__(self, message: str, *, reason: str, host: str, repo: str | None,
                 method: str, caller: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.host = host
        self.repo = repo
        self.method = method
        self.caller = caller


def _org_forgejo_hosts() -> frozenset[str]:
    """The DNS name plus the tailnet base the RUNNER exported.

    ``FL4WRITE_FORGEJO_API_BASE`` is how the tracked ``*.fj`` configs already
    receive the tailnet literal (it never lives in the public repo); the
    guard reads the same runner-owned variable so a config cannot smuggle in
    a different "org" server.
    """
    hosts = {ORG_FORGEJO_HOST}
    raw = os.environ.get("FL4WRITE_FORGEJO_API_BASE", "").strip()
    if raw:
        try:
            parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
            host = parsed.hostname
        except ValueError:
            host = None
        if host:
            hosts.add(host.casefold())
    return frozenset(hosts)


def host_of(base: str) -> str:
    """Hostname of an api_base/URL, or "" when it cannot be parsed."""
    try:
        return urllib.parse.urlsplit(base).hostname or ""
    except ValueError:
        return ""


def repo_from_api_path(path: str) -> str | None:
    """``owner/repo`` from a REST path, or None when the path names no repo.

    Handles ``/repos/<owner>/<repo>/...`` (the only shape either forge client
    writes to) and tolerates a full URL. Anything else is unparseable and the
    caller refuses it: fail closed, never "no repo found, so allow".
    """
    if not isinstance(path, str) or not path:
        return None
    candidate = urllib.parse.urlsplit(path).path if "://" in path else path
    candidate = candidate.split("?", 1)[0].split("#", 1)[0]
    parts = [urllib.parse.unquote(p) for p in candidate.split("/") if p]
    if len(parts) < 3 or parts[0] != "repos":
        return None
    owner, name = parts[1], parts[2]
    if not _OWNER_RE.fullmatch(owner) or not _OWNER_RE.fullmatch(name):
        return None
    return f"{owner}/{name}"


def classify(host: str, repo: str | None) -> tuple[bool, str]:
    """(allowed, reason). Pure: no telemetry, no I/O, no env mutation."""
    h = (host or "").casefold()
    if h == GITHUB_API_HOST:
        owners, denied = GITHUB_WRITE_OWNERS, "github_owner_not_allowlisted"
    elif h and h in _org_forgejo_hosts():
        owners, denied = ORG_FORGEJO_WRITE_OWNERS, "forgejo_owner_not_allowlisted"
    else:
        return False, "host_not_allowlisted"
    if repo is None:
        return False, "unparseable_write_target"
    owner = repo.split("/", 1)[0].casefold()
    if owner not in owners:
        return False, denied
    return True, "allowed"


def write_allowed(base: str, repo: str) -> bool:
    """Planning predicate: would a mutating request through `base` be allowed?

    Used by the tier planner to keep configs whose writes the guard will
    refuse out of the schedule. Emits nothing — planning is not a write.
    """
    return classify(host_of(base), repo)[0]


def refuse(reason: str, host: str, repo: str | None, method: str, caller: str,
           path: str = "") -> OutOfBoundsWrite:
    """Emit ONE ``guard_refusal`` event and return the typed error to raise."""
    telemetry.emit("guard_refusal", reason=reason, host=host or "", repo=repo,
                   method=method, caller=caller)
    target = repo or path or "<no repo>"
    return OutOfBoundsWrite(
        f"out-of-bounds write refused before I/O: {method} {host or '<no host>'} "
        f"{target} ({reason}) — FL4WRITE writes only to KyaniteLabs",
        reason=reason, host=host or "", repo=repo, method=method, caller=caller,
    )


def check_github_write(method: str, path: str,
                       caller: str = "executor._gh_api") -> None:
    """Gate every mutating api.github.com request before the socket opens."""
    m = (method or "").upper()
    if m not in MUTATING_METHODS:
        return
    repo = repo_from_api_path(path)
    allowed, reason = classify(GITHUB_API_HOST, repo)
    if not allowed:
        raise refuse(reason, GITHUB_API_HOST, repo, m, caller, path=path)


def check_forge_write(base: str, method: str, path: str, caller: str) -> None:
    """Gate every mutating forge-adapter request before the socket opens."""
    m = (method or "").upper()
    if m not in MUTATING_METHODS:
        return
    host = host_of(base)
    repo = repo_from_api_path(path)
    allowed, reason = classify(host, repo)
    if not allowed:
        raise refuse(reason, host, repo, m, caller, path=path)


def check_github_installation_repo(repo: str,
                                   caller: str = "appauth.resolve_installation_id") -> None:
    """Refuse to resolve/mint a GitHub App token for a non-KyaniteLabs repo.

    Runs BEFORE ``_make_jwt``: a personal-account installation token must
    never be minted, even for a config that slipped past the file moves.
    """
    target = repo_from_api_path(f"/repos/{repo}") if isinstance(repo, str) else None
    if target != repo:
        target = None  # extra segments, encodings or traversal are not a repo
    allowed, reason = classify(GITHUB_API_HOST, target)
    if not allowed:
        raise refuse(reason, GITHUB_API_HOST, target, "POST", caller,
                     path="/app/installations/access_tokens")
