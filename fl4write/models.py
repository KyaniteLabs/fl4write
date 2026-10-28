"""Normalized entities — the engine speaks only these; forges are adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class PullRequest(BaseModel):
    forge: str
    number: int
    repo: str  # owner/name as the forge sees it
    title: str = ""
    body: str = ""
    head_sha: str
    is_fork: bool = False
    author: str = ""
    is_bot_author: bool = False
    state: str = "open"
    merged_at: str = ""  # ISO timestamp; "" = not merged (post-merge sweep key)


class Finding(BaseModel):
    """A review finding. Severity/tone are renderer concerns; this is data."""

    rule_id: str  # must exist in config.review (grounding) or "general"
    severity: str  # must exist in config.severity_vocab (grounding)
    path: str
    line: int
    category: str = "General"
    message: str
    proposal: str = ""  # suggested fix, tone-invariant
    # Honest-uncertainty surface (reliability queue 2026-09-17): the model's
    # own probability [0,1] that this finding is a real defect. OPTIONAL —
    # absent means "the model did not claim calibrated confidence", never 0.5
    # or any invented default. Invalid values are stripped upstream (never
    # coerced), so an out-of-range or non-numeric claim degrades to "absent".
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ReviewDoc(BaseModel):
    pr: PullRequest
    findings: list[Finding] = Field(default_factory=list)
    digest: dict[str, int] = Field(default_factory=dict)  # severity -> count



@dataclass
class CycleReport:
    repo: str
    scanned: int = 0
    reviewed: int = 0
    mirror_degraded: int = 0
    skipped_dependency: int = 0
    already_done: int = 0
    mirror_dedup: int = 0
    fix_escalations: int = 0
    model_unavailable: int = 0
    shadow_only: bool = False
    gatekeeper_dropped: int = 0
    fix_prs_opened: int = 0
    fix_prs_merged: int = 0
    issues_triaged: int = 0
    acceptance: dict[str, Any] = field(default_factory=dict)
    skipped_diff_unavailable: int = 0
    postmerge_reviewed: int = 0
    retro_reviewed: int = 0
    retro_zombies: int = 0
    ci_red_heads: int = 0
    ci_fix_prs_opened: int = 0
    ci_escalations: int = 0
    omni_scanned: int = 0
    omni_findings: int = 0
    gatekeeper_failed: int = 0
    fix_attempts: int = 0
    fix_failures: int = 0
    _fix_failure_notes: list = field(default_factory=list)
    _merged_listing_incomplete: bool = False
    alerts: list[str] = field(default_factory=list)
