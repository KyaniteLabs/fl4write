"""Comparable aware timestamps shared by forge intake and sweep state."""
from datetime import datetime


def parse_iso(raw: object) -> datetime | None:
    """Reject malformed/naive values; retain offsets for instant comparison."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else None
