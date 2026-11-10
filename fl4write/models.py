"""Normalized entities — the engine speaks only these; forges are adapters."""

from __future__ import annotations

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


# A credential the analyzer proved in the anchored source. Owner decision
# (2026-11-10): such a finding is always Critical, so the model nit-filter
# (gatekeeper) may neither drop nor demote it. Only the analyzer sets this
# category, and it strips it from any finding the source does not prove.
VERIFIED_SECRET_CATEGORY = "secret (verified in source)"


class Finding(BaseModel):
    """A review finding. Severity/tone are renderer concerns; this is data."""

    rule_id: str  # must exist in config.review (grounding) or "general"
    severity: str  # must exist in config.severity_vocab (grounding)
    path: str
    line: int
    category: str = "General"
    message: str
    proposal: str = ""  # suggested fix, tone-invariant


class ReviewDoc(BaseModel):
    pr: PullRequest
    findings: list[Finding] = Field(default_factory=list)
    digest: dict[str, int] = Field(default_factory=dict)  # severity -> count
