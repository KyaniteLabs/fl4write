"""Explicit outcome vocabularies for the review and fix lanes.

These used to be free-text strings passed between modules: the executor's
fix result ("pr_opened", "testfail", ...), the per-PR review mark persisted
in state ("reviewed:3", "shadow:0", "dependency-skip", ...) and the review
step results the sweeps branch on ("fix-deferred", "model-unavailable", ...).
A typo on either side silently fell into an "unknown" branch. Each set is now
an enum, writers can only produce a member, and every reader dispatches over
the whole set (tests assert the dispatch tables are exhaustive).

Members are str subclasses, so persisted JSON and existing comparisons with
plain strings keep working byte-for-byte.
"""

from __future__ import annotations

from enum import Enum


class FixStatus(str, Enum):
    """Terminal result of one executor.attempt_fix call."""

    PR_OPENED = "pr_opened"
    BLOCKED = "blocked"     # a fix-lane rail refused (fork, bot PR, disabled, depth)
    SKIPPED = "skipped"     # dependency PR policy
    ERROR = "error"         # infrastructure or integrity refusal
    TESTFAIL = "testfail"   # the patched tree failed its tests
    NOFIX = "nofix"         # the model produced no usable change


FIX_FAILURES = frozenset({FixStatus.ERROR, FixStatus.TESTFAIL, FixStatus.NOFIX})


class ReviewMark(str, Enum):
    """What state records as a PR head's review outcome (last_outcome)."""

    REVIEWED = "reviewed"                  # posted (count = findings)
    SHADOW = "shadow"                      # shadow-only: NOT reviewed for live
    RETRO = "retro"                        # retro audit (count = findings)
    MODEL_FAILED_CAP = "model-failed-cap"  # gave up at this SHA; human look
    DEPENDENCY_SKIP = "dependency-skip"    # policy skip of a bot dependency PR


_COUNTED_MARKS = frozenset({ReviewMark.REVIEWED, ReviewMark.SHADOW, ReviewMark.RETRO})


def format_mark(kind: ReviewMark, count: int | None = None) -> str:
    """The persisted last_outcome string. Counted kinds need a count >= 0;
    the others must not carry one."""
    kind = ReviewMark(kind)
    if kind in _COUNTED_MARKS:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"review mark {kind.value!r} needs a finding count, got {count!r}")
        return f"{kind.value}:{count}"
    if count is not None:
        raise ValueError(f"review mark {kind.value!r} takes no count, got {count!r}")
    return kind.value


def mark_is_shadow(raw: object) -> bool:
    """A shadow mark never counts as reviewed for live posting. Legacy state
    wrote 'shadow' and 'shadow-posted'; every shadow form starts 'shadow'."""
    return str(raw if raw is not None else "").startswith(ReviewMark.SHADOW.value)


class ReviewStep(str, Enum):
    """Result of reviewing one PR inside a sweep (_review_pr and the retro
    variant). The sweeps decide terminal vs. retry from this alone."""

    REVIEWED = "reviewed"
    SHADOW = "shadow"
    FIX_DEFERRED = "fix-deferred"          # reviewed; fixes still pending
    MODEL_FAILED_CAP = "model-failed-cap"  # terminal give-up at this SHA
    MODEL_UNAVAILABLE = "model-unavailable"
    DIFF_UNAVAILABLE = "diff-unavailable"
    DEFERRED = "deferred"                  # budget or retryable trouble


TERMINAL_STEPS = frozenset({ReviewStep.REVIEWED, ReviewStep.MODEL_FAILED_CAP})


class FixResume(str, Enum):
    """Result of resuming a PR's pending fixes."""

    GATED_OFF = "gated-off"   # the fix lane cannot run for this repo/config
    DROPPED = "dropped"       # stale or malformed pending record cleared
    ATTEMPTED = "attempted"
