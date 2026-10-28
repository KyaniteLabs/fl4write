"""diff_texts: split a unified diff into per-path text blocks.

Leaf module (Cycle-2 fix): analyzer and forges share this parsing without
forges importing analyzer.
"""

from __future__ import annotations

import re



def _diff_block_path(header: str, lines: list[str]) -> str | None:
    """Prefer unambiguous destination metadata before the diff body starts."""
    for line in lines:
        if line.startswith(("@@", "GIT binary patch", "Binary files ")):
            break
        rename = line.startswith(("rename to ", "copy to "))
        if rename:
            raw = line.split(" to ", 1)[1]
        elif line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0]
        else:
            continue
        if raw.startswith('"'):
            if not raw.endswith('"'):
                continue
            import codecs

            try:
                raw = codecs.escape_decode(raw[1:-1].encode("utf-8"))[0].decode("utf-8")
            except (ValueError, UnicodeError):
                continue
        if rename and raw:
            return raw
        if raw.startswith("b/"):
            return raw[2:]
    return _git_diff_path(header)




def _git_diff_path(line: str) -> str | None:
    """New-file path from a `diff --git a/.. b/..` header, handling git's
    C-quoted paths for spaces/specials (MECE round-2, luna F2-002: quoted
    headers used to yield no path, disabling grounding and per-path secret
    anchoring for files with spaces)."""
    # F13-A11 (reopened F4-002): decode ONE complete quoted pathname token —
    # rstrip('"') trimmed real content characters (a file named foo\" lost
    # its quote and its secrets finding was demoted). Scan for the closing
    # quote honoring backslash escapes, then decode the whole token's bytes.
    # Quoted token boundaries take precedence over spaces inside filenames.
    # This matters for deletion/binary blocks without destination metadata.
    destination = None
    if line.startswith('diff --git "'):
        match = re.match(r'diff --git "(?:\\.|[^"\\])*" (.*)', line)
        if match is None:
            return None
        destination = match.group(1)
    elif ' "b/' in line:
        destination = line[line.index(' "b/') + 1:]
    if destination is not None:
        quoted = destination.startswith('"b/')
        if not quoted and not destination.startswith('b/'):
            return None
        tok = destination[3:] if quoted else destination[2:]
    else:
        # Unquoted header fallback: complete blocks prefer destination metadata.
        i = line.rfind(" b/")
        if line.startswith("diff --git a/"):
            # Same-path equal halves disambiguate literal ' b/' in both names.
            old_start = len("diff --git a/")
            for match in re.finditer(" b/", line):
                if line[old_start:match.start()] == line[match.end():]:
                    i = match.start()
                    break
        if i == -1:
            return None
        quoted = False
        tok = line[i + 3:]
    if quoted:
        out = []
        backslashes = 0
        for ch in tok:
            if ch == "\\":
                backslashes += 1
                out.append(ch)
                continue
            if ch == '"' and backslashes % 2 == 0:
                tok = "".join(out)
                break
            backslashes = 0
            out.append(ch)
        else:
            return None  # unterminated quoted token
    else:
        # Git leaves ordinary spaces unquoted. The header has already
        # selected the new-file side; whitespace here belongs to the path.
        tok = tok.rstrip("\r\n")
        if not tok:
            return None
        # F21-001: unquoted literal Unicode paths are already decoded text.
        # Return them directly; only the quoted branch decodes C escapes.
        return tok
    try:
        import codecs as _codecs
        # C escapes encode bytes; literal Unicode must contribute its UTF-8
        # bytes without passing through a lossy Latin-1 conversion.
        return _codecs.escape_decode(tok.encode("utf-8"))[0].decode("utf-8")
    except (ValueError, UnicodeError):
        pass
    return tok


def _diff_path_texts(diff_text: str) -> dict[str, str]:
    """Split a unified diff into {path: its hunks} for per-path grounding."""
    out: dict[str, str] = {}
    header: str | None = None
    parts: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if header is not None and (path := _diff_block_path(header, parts)) is not None:
                out[path] = "\n".join(parts)
            header = line
            parts = []
        elif header is not None:
            parts.append(line)
    if header is not None and (path := _diff_block_path(header, parts)) is not None:
        out[path] = "\n".join(parts)
    return out
