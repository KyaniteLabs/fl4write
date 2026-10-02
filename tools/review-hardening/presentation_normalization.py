"""Explicit, bounded presentation repair; no semantic or revision inference.

Baseline parsing/binding remains strict unless a caller opts into this layer.
Every repair records provenance and preserves the original model output.
"""
import hashlib
import re


def text_sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def json_presentation(content):
    if not isinstance(content, str):
        raise ValueError('nonstring_content')
    stripped = content.strip(' \t\r\n')
    # Exactly one complete outer fence with matching delimiter, optional json
    # language, and no surrounding prose. The strict decoder owns inner syntax.
    match = re.fullmatch(r'(?P<fence>`{3,8})(?:json)?\r?\n(?P<body>.*)\r?\n(?P=fence)',
                         stripped, flags=re.S)
    if match:
        return match['body'], [{'kind': 'single_complete_json_fence',
                               'original_sha256': text_sha256(content),
                               'normalized_sha256': text_sha256(match['body'])}]
    return content, []


def _missing_indent_only(source, quote):
    if not isinstance(quote, str):
        return False
    source_lines, quote_lines = source.split('\n'), quote.split('\n')
    if len(source_lines) != len(quote_lines):
        return False
    changed = False
    for actual, cited in zip(source_lines, quote_lines):
        actual_body, cited_body = actual.lstrip(' '), cited.lstrip(' ')
        if actual_body != cited_body or not actual_body.strip():
            return False
        actual_indent, cited_indent = len(actual) - len(actual_body), len(cited) - len(cited_body)
        if cited_indent > actual_indent or actual_body.startswith('\t'):
            return False
        changed |= actual_indent != cited_indent
    return changed


def pinned_quote(visible, start, end, quote):
    """Repair missing ASCII-space indentation only, uniquely within captured path.

    Location/revision/count checks happen before this function. Uniqueness is
    scoped to captured new-side lines, not unseen whole-file source. Identifiers,
    literal bodies, operators, trailing whitespace and tabs are never changed.
    """
    source = '\n'.join(visible[n] for n in range(start, end + 1))
    if source == quote:
        return source, []
    if not _missing_indent_only(source, quote):
        raise ValueError('quote_mismatch')
    width = end - start + 1
    matches = []
    for candidate in visible:
        if all(n in visible for n in range(candidate, candidate + width)):
            candidate_text = '\n'.join(visible[n] for n in range(candidate, candidate + width))
            if candidate_text == quote or _missing_indent_only(candidate_text, quote):
                matches.append(candidate)
    if matches != [start]:
        raise ValueError('ambiguous_indent_normalization')
    return source, [{'kind': 'missing_ascii_space_indent', 'start_line': start, 'end_line': end,
                     'uniqueness_scope': 'captured_new_side_of_pinned_path',
                     'original_quote_sha256': text_sha256(quote),
                     'pinned_quote_sha256': text_sha256(source)}]
