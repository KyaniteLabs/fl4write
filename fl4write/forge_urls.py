"""forge_urls: URL classification that config and the forge adapters share.

Leaf module (Cycle-2 fix, L6 backlog item 4): config must not reach into
forges, and forges must not reach back, so the pure URL predicate lives
here and both import it.
"""

from __future__ import annotations



def is_github_base(api_base: str) -> bool:
    """MECE round-7 (sol F7-D007) + round-8 (terra F8-001): the GitHub route
    carries the App credential — EXACT hostname equality AND https required.
    F14-D005: EVERY urlsplit property access is guarded — a malformed port
    used to raise raw ValueError out of CLI routing."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(api_base)
        return (parts.scheme == "https"
                and parts.hostname == "api.github.com"
                and parts.port in (None, 443))
    except (ValueError, TypeError):
        return False
