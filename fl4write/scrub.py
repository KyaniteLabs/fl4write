"""Untrusted-text scrubbing — the lethal-trifecta first defense.

EVERYTHING crossing the trust boundary (PR bodies, commit messages, finding
text, review comments, file names) is scrubbed before it enters model prompts
or is echoed into output. Defense classes, from the Lane E research:

- control/invisible characters (RLM/LRM/ZWSP, bidi overrides, ANSI escapes)
- markdown/link exfiltration vectors (data: URLs, base64 img/src in any form)
- hidden HTML (details/summary collapses hiding instructions), HTML comments
- our own marker protocol (fl4write-sitter must never be spoofable)

This is deliberately allow-list-flavored: strip by category, then assert the
result contains none of the categories. Injection payloads become inert text
the model sees as data.
"""

from __future__ import annotations

import re
import unicodedata

_CONTROL_CATEGORIES = {"Cc", "Cf"}
# Keep \n and \t — they're structural, not hostile.
_WHITELIST_CODEPOINTS = {0x09, 0x0A}

_DATA_URL_RE = re.compile(r"data:[^\s\"')]+", re.IGNORECASE)
_BASE64_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*base64[^)]*\)", re.IGNORECASE)
_REMOTE_SRC_RE = re.compile(r"<\s*(img|source|script|iframe)[^>]*src\s*=", re.IGNORECASE)
_REMOTE_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*https?://[^)]*\)", re.IGNORECASE)  # exfil beacon
# F13-A7 (reopened F11-A6/F12-A4): alt text may contain ESCAPED brackets
# ('![a\\]](https://…)') — the plain class above cannot match them
_ESC_ALT_IMG_RE = re.compile(
    r"!\[(?:[^\[\]]|\\.)*\]\(\s*(?:https?:)?//[^)]*\)", re.IGNORECASE)
# F11-A6 (round 11, luna-max DOM-A): remote images also ride in as REFERENCE
# links ("![x][id]" + "[id]: https://evil/...") and protocol-relative URLs
# ("![x](//host/pixel)") — both are attacker-controlled loads in the posted
# comment, none of which may survive scrub
_REMOTE_IMG_REF_DEF_RE = re.compile(
    r"^ {0,3}\[[^\]\n]+\]:\s*(?:[a-zA-Z][a-zA-Z0-9+.\-]*:)?(?://)?\S+.*$",
    re.IGNORECASE | re.MULTILINE)
_PROTOCOL_RELATIVE_IMG_RE = re.compile(
    r"!\[[^\]]*\]\(\s*//[^)]*\)", re.IGNORECASE)
_IMG_REF_USAGE_RE = re.compile(r"!\[[^\]]*\]\[[^\]]*\]", re.IGNORECASE)
# F12-A4 (reopened F11-A6): angle-bracket image destinations ('![x](<https://…>)')
# and <img srcset="…"> survive the inline-URL scrub
_ANGLE_IMG_RE = re.compile(
    r"!\[[^\]]*\]\(\s*<[^>]*(?:https?:)?//[^>]*>\s*\)", re.IGNORECASE)  # F14-A03: incl protocol-relative
_SRCSET_RE = re.compile(
    r"<\s*(img|source)[^>]*\bsrcset\s*=[^>]*>", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HIDDEN_TAG_RE = re.compile(
    r"</?\s*(details|summary|script|style|iframe|h[1-6]|table|thead|tbody|tr|th|td|"
    r"div|section|article|blockquote|pre|hr|svg|math|object|embed|form)\b[^>]*>",
    re.IGNORECASE)
# F13-A8: scrub and assert_clean must speak the same language — inline
# display/visibility declarations are neutralized HERE so posted prose that
# legitimately mentions them does not crash the render assert
_HIDDEN_STYLE_RE = re.compile(
    r"(?:display|visibility)\s*:\s*(?:none|hidden)", re.IGNORECASE)
# Our persistent-comment marker must be minted only by the renderer.
_MARKER_RE = re.compile(r"(?:fl4write|codesitter):v\d+:[0-9a-fA-F]+")


def controls(text: str) -> str:
    """Strip ONLY control/format characters (keep \n/\t) — no content
    rewriting. The canonical form used BEFORE semantic gates and as the
    identity basis, so a zero-width char can never hide a refutation from
    the contradiction scan while the posted text normalizes later (F13-A5)."""
    if not isinstance(text, str):
        return ""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp in _WHITELIST_CODEPOINTS:
            out.append(ch)
            continue
        if unicodedata.category(ch) in _CONTROL_CATEGORIES:
            continue  # drop bidi overrides, zero-widths, ANSI, etc.
        out.append(ch)
    return "".join(out)


def scrub(text: str) -> str:
    """Category-strip untrusted text. Idempotent. Never raises."""
    if not isinstance(text, str):
        return ""
    s = controls(text)
    s = _DATA_URL_RE.sub("[scrubbed-data-url]", s)
    s = _BASE64_IMG_RE.sub("[scrubbed-image]", s)
    s = _REMOTE_IMG_RE.sub("[image removed]", s)
    s = _ESC_ALT_IMG_RE.sub("[image removed]", s)
    s = _PROTOCOL_RELATIVE_IMG_RE.sub("[image removed]", s)
    s = _IMG_REF_USAGE_RE.sub("[image removed]", s)
    s = _ANGLE_IMG_RE.sub("[image removed]", s)
    s = _SRCSET_RE.sub("&lt;remote-srcset ", s)
    # reference DEFINITIONS feed the reference-style images above; removing
    # the usage alone leaves the URL live for other renderers
    s = _REMOTE_IMG_REF_DEF_RE.sub("", s)
    s = _REMOTE_SRC_RE.sub("&lt;remote-src ", s)
    s = _HTML_COMMENT_RE.sub("", s)
    if "<!--" in s:
        # MECE round-1 (terra F1-07): an UNCLOSED comment opener can swallow
        # the remainder of the rendered comment — remove any leftover opener
        s = s.split("<!--", 1)[0] + s.split("<!--", 1)[1].replace("<!--", "")
    s = _HIDDEN_TAG_RE.sub("", s)
    s = _HIDDEN_STYLE_RE.sub("hidden-style", s)
    s = _MARKER_RE.sub("[scrubbed-marker]", s)
    return s


# High-entropy or prefixed runs that look like credentials (MECE round-1,
# terra F1-013): redacted at RENDER/posting time so a model-quoted literal is
# never duplicated onto a more public surface. Kept out of analyzer grounding
# (L1-B3 needs the literal before posting decisions).
_SECRET_PREFIX = ("ghp_", "gho_", "github_pat_", "sk-", "sk_", "AKIA",
                  "xoxb-", "xoxp-", "glpat-", "AIza")
_REDACT_RUN_RE = re.compile(r"[A-Za-z0-9_\-+=]{16,}")
# Long camelCase identifiers that look high-entropy but are code, not secrets
_KNOWN_IDENTIFIERS = {"documentQuerySelector", "getElementById", "getElementByClassName"}


def _entropy(s: str) -> float:
    import math
    if not s:
        return 0.0
    freq = {c: s.count(c) for c in set(s)}
    return -sum((n / len(s)) * math.log2(n / len(s)) for n in freq.values())


def redact_credentials(text: str) -> str:
    """Replace credential-shaped strings with [redacted]. Prefix runs always;
    16+ char runs always (a real secret, not an identifier).
    Apply at posting surfaces, never on analyzer grounding paths."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    import re as _re
    # D4: ANY 16+ char alphanumeric run is redacted unless it is a known
    # code identifier. The old entropy gate let low-entropy credentials
    # (e.g. 'aaaaaaaaaaaaaaaa') leak when not in an assignment context.
    # F13-A1 (CRITICAL, reopened F1-013): credential ASSIGNMENT values are
    # redacted regardless of entropy — 'password=aaaaaaaaaaaaaaaa' is a real
    # hard-coded credential even though its Shannon entropy is ~0
    _ASSIGN_KEY = (
        r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|auth(?:orization)?|private[_-]?key)\b\s*[:=]\s*"
        r"['\"]?([A-Za-z0-9_\-./+]{1,})['\"]?")
    def _assign_sub(m: re.Match) -> str:
        # Preserve a trailing quote char if the match consumed one, so
        # 'password = "abcdef"' -> 'password = "[redacted]"' not '...[redacted]'
        tail = m.group(0)[-1] if m.group(0) else ""
        closing = tail if tail in ("'", '"') else ""
        return out[m.start():m.start(1)] + "[redacted]" + closing
    out = _re.sub(_ASSIGN_KEY, _assign_sub, out)

    # D4: secrets split by '.' (JWT) or '/' (AWS secret key) defeat the 16+
    # contiguous-run rule and leak partial credential material. A JWT is
    # exactly 3 base64url segments joined by '.'; an AWS secret key is base64
    # with '/' separators. Redact the WHOLE dotted/slash-delimited token as
    # one unit when it is long enough to be a real secret (>=24 chars total,
    # >=2 segments) — this catches the short middle fragments the 16+ rule
    # leaves behind. Legitimate dotted identifiers (com.example.Foo) are
    # short per-segment and stay under the 24-char floor.
    _SPLIT_TOKEN_RE = _re.compile(r"[A-Za-z0-9_\-]+(?:[./][A-Za-z0-9_\-]+)+")
    def _split_sub(m) -> str:
        tok = m.group(0)
        if len(tok) >= 24 and ("." in tok or "/" in tok):
            return "[redacted]"
        return tok
    out = _SPLIT_TOKEN_RE.sub(_split_sub, out)
    for m in _REDACT_RUN_RE.finditer(text):
        tok = m.group(0)
        if tok not in _KNOWN_IDENTIFIERS:
            out = out.replace(tok, "[redacted]", 1)
    # prefix-marked tokens not caught by the 16+ run rule (shorter prefixes)
    for p in _SECRET_PREFIX:
        out = _re.sub(re.escape(p) + r"[A-Za-z0-9_\-]{4,}", "[redacted]", out)
    return out


def inline(text: str, limit: int | None = None) -> str:
    """Scrub + collapse to ONE line for list/title renderings (issue bodies,
    escalation bullets, PR titles): finding text with newlines must never
    break a bullet list or mint fake entries (UltraQA round 1, ADV-04/P3 —
    scrub keeps \n structural, which is right for prose but wrong here)."""
    s = redact_credentials(scrub(text))
    s = " ".join(s.split()) if s else ""
    return s[:limit] if limit else s


def assert_clean(text: str) -> None:
    """Fail loudly if scrub() output still contains a defense category."""
    for ch in text:
        cp = ord(ch)
        if cp in _WHITELIST_CODEPOINTS:
            continue
        if unicodedata.category(ch) in _CONTROL_CATEGORIES:
            raise ValueError(f"unscrubbed control char U+{cp:04X} in output")
    for pattern in (_DATA_URL_RE, _BASE64_IMG_RE, _REMOTE_IMG_RE, _ESC_ALT_IMG_RE,
                    _PROTOCOL_RELATIVE_IMG_RE, _IMG_REF_USAGE_RE,
                    _REMOTE_IMG_REF_DEF_RE, _ANGLE_IMG_RE, _SRCSET_RE,
                    _REMOTE_SRC_RE, _MARKER_RE):
        if pattern.search(text):
            raise ValueError(f"unscrubbed pattern {pattern.pattern[:30]} in output")
    # F9-A10: HTML comments and hidden-content tags can restructure a posted
    # review even when every regex pattern above is clean (markers are
    # STRUCTURAL: bare words like 'hidden' or diff arrows must not trip)
    low = text.lower()
    if "<!--" in low or re.search(r"<\s*/?\s*(style|script|template|iframe|svg|math|object|embed)", low) \
            or re.search(r"(?:display|visibility)\s*:\s*(?:none|hidden)", low) \
            or re.search(r"<\s*[a-z]+[^>]*\bhidden\b", low):
        raise ValueError("unscrubbed html comment/hidden structure in output")
