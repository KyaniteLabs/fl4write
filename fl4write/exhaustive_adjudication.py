"""Explicit local desk decisions, bound to immutable recon evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .exhaustive_evidence import EvidenceError, verify_bundle


class AdjudicationError(RuntimeError):
    pass


def fingerprint(finding: dict) -> str:
    return hashlib.sha256(json.dumps(finding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise AdjudicationError("duplicate desk JSON key")
    return value


def read_decision(path: Path) -> tuple[dict, bytes]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise AdjudicationError("desk decision is not a bounded regular file")
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise AdjudicationError("desk decision exceeds size limit")
        return json.loads(raw, object_pairs_hook=_unique), raw
    except (OSError, ValueError) as exc:
        raise AdjudicationError("desk decision is unreadable or malformed") from exc


def actionable(value: dict, head: str, recon: dict, findings: list[dict]) -> list[dict]:
    """A reviewer label is provenance; choosing this input is the operator's trust decision."""
    if (not isinstance(value, dict)
            or set(value) != {"version", "reviewed_head", "recon_sha256", "reviewer", "decisions"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["reviewed_head"] != head or value["recon_sha256"] != recon["sha256"]
            or not isinstance(value["reviewer"], str) or not value["reviewer"].strip()
            or not isinstance(value["decisions"], list)):
        raise AdjudicationError("desk decision identity or shape is invalid")
    verdicts = {}
    for row in value["decisions"]:
        if (not isinstance(row, dict) or set(row) != {"finding_id", "verdict", "rationale"}
                or not isinstance(row["finding_id"], str) or row["finding_id"] in verdicts
                or row["verdict"] not in ("valid", "invalid", "duplicate")
                or not isinstance(row["rationale"], str) or not row["rationale"].strip()):
            raise AdjudicationError("desk disposition is incomplete or ambiguous")
        verdicts[row["finding_id"]] = row["verdict"]
    if set(verdicts) != {fingerprint(row) for row in findings}:
        raise AdjudicationError("desk dispositions do not exactly cover recon findings")
    # A duplicate label alone never suppresses an unresolved valid defect.
    return [row for row in findings if verdicts[fingerprint(row)] != "invalid"]


def verified_findings(row: dict, paths: dict[str, Path] | None = None) -> list[dict]:
    """Recompute disposition counts from sealed raw evidence, never a state counter."""
    try:
        paths = paths or verify_bundle(row["evidence_bundle"])
        raw_findings = json.loads(paths["worker-result.json"].read_bytes())["findings"]
        if (not isinstance(raw_findings, list) or row.get("findings") != raw_findings
                or type(row.get("finding_count")) is not int or row["finding_count"] != len(raw_findings)
                or json.loads(paths["manifest.json"].read_bytes()).get("head") != row["reviewed_head"]
                or json.loads(paths["selected-request.json"].read_bytes()) != {"sha256": row["request_sha256"]}):
            raise AdjudicationError("raw finding state differs from sealed recon")
        if "adjudication_sha256" not in row:
            count = row.get("valid_finding_count", len(raw_findings))
            if type(count) is not int or count != len(raw_findings):
                raise AdjudicationError("valid count lacks desk evidence")
            return raw_findings
        recon = row["recon_evidence_bundle"]
        original = verify_bundle(recon)
        if (json.loads(original["manifest.json"].read_bytes()).get("head") != row["reviewed_head"]
                or original["worker-result.json"].read_bytes() != paths["worker-result.json"].read_bytes()
                or original["selected-request.json"].read_bytes() != paths["selected-request.json"].read_bytes()
                or json.loads(original["desk-mode.json"].read_bytes()) != {"enabled": True}):
            raise AdjudicationError("desk decision is bound to different recon evidence")
        value, raw = read_decision(paths["desk-adjudication.json"])
        if hashlib.sha256(raw).hexdigest() != row["adjudication_sha256"]:
            raise AdjudicationError("desk decision digest changed")
        result = actionable(value, row["reviewed_head"], recon, raw_findings)
        count = row.get("valid_finding_count")
        if type(count) is not int or count != len(result):
            raise AdjudicationError("valid finding count differs from sealed desk decisions")
        return result
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        raise AdjudicationError("desk evidence is unavailable or malformed") from exc
