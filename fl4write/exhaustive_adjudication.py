"""Explicit local desk decisions, bound to immutable recon evidence."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path

from .exhaustive_evidence import EvidenceError, verify_bundle
from .state import _atomic_write


class AdjudicationError(RuntimeError):
    pass


def fingerprint(finding: dict) -> str:
    return hashlib.sha256(json.dumps(finding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise AdjudicationError("duplicate desk JSON key")
    return value


# Standing carve-out (Lever 3, 2026-09-27): recon re-flagged the fleet's
# ~100 *.fl4write.yaml model endpoints ("Configuration exposes a private IP
# address...", "Insecure HTTP endpoint...") every round, and the desk
# re-invalidated them every round. Policy, settled once: plain-http to a
# non-routable private/loopback host IS the documented accepted posture for
# LAN self-hosting (config.ModelRoute: "http allowed: BYO-LLM localhost
# routers"), so those rows are adjudicated invalid here instead of returning
# as desk work. Narrow by construction — only an `endpoint:` config line
# whose URL host is loopback or RFC1918/ULA/link-local, with no userinfo and
# no credential query parameter, and whose message does not assert a
# credential leak. Public/routable endpoints and credentials still flag.
_ENDPOINT_LINE = re.compile(r"^\s*endpoint\s*:\s*[\"']?(https?://[^\s\"']+)", re.IGNORECASE)
_CREDENTIAL_QUERY_KEYS = frozenset(
    {"token", "key", "api_key", "apikey", "secret", "password", "access_token", "auth"}
)
_CREDENTIAL_MESSAGE_MARKERS = (
    "credential", "secret", "token", "password", "api key", "api-key", "apikey",
)
_NON_ROUTABLE_V4 = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_NON_ROUTABLE_V6 = tuple(
    ipaddress.ip_network(net) for net in ("fc00::/7", "fe80::/10")  # ULA, link-local
)


def _non_routable_private(host: str) -> bool:
    """Loopback or non-routable private literal: RFC1918, IPv6 ULA/link-local,
    loopback, `localhost`. A public DNS name or routable IP is never private."""
    from .config import _loopback_host

    if _loopback_host(host):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in net for net in (_NON_ROUTABLE_V4 if address.version == 4 else _NON_ROUTABLE_V6))


def accepted_lan_endpoint(finding: dict) -> bool:
    """Does this recon finding target the accepted LAN self-hosting posture?

    True only when the finding's cited evidence is a model-transport
    `endpoint:` line whose URL host is non-routable private/loopback, the URL
    carries no embedded credential (userinfo or credential query parameter),
    and the finding's message does not assert a credential leak. The authority
    is parsed by hand: findings arrive AFTER scrub.redact_credentials, which
    can collapse an URL's port-and-path into `[redacted]` (a bracketed token
    urllib refuses as an IPv6 netloc) — the host itself always survives."""
    match = _ENDPOINT_LINE.match(str(finding.get("evidence", "")))
    if match is None:
        return False
    url = match.group(1)
    _, _, rest = url.partition("://")
    authority = re.split(r"[/?#\s]", rest, maxsplit=1)[0]
    if "@" in authority:
        return False  # credential in userinfo: still flags
    if authority.startswith("["):  # bracketed IPv6 literal
        host = authority[1:authority.find("]")]
    else:
        host = authority.split(":", 1)[0]
    host = host.strip().lower()
    if not host or not _non_routable_private(host):
        return False
    query = url.split("?", 1)[1].split("#", 1)[0] if "?" in url else ""
    if {part.split("=", 1)[0].lower() for part in query.split("&") if part} & _CREDENTIAL_QUERY_KEYS:
        return False
    message = str(finding.get("message", "")).lower()
    return not any(marker in message for marker in _CREDENTIAL_MESSAGE_MARKERS)


def apply_endpoint_carveout(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split recon findings into (actionable, carved). Carved rows are the
    standing-policy invalids above; they stay recorded in the round artifact
    (log-what-you-dropped) — suppressed from repair, never silently erased."""
    actionable_rows, carved_rows = [], []
    for row in findings:
        (carved_rows if accepted_lan_endpoint(row) else actionable_rows).append(row)
    return actionable_rows, carved_rows


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


def apply_decision(directory: Path, head: str, common: dict, artifacts: Path, pending: dict) -> list[dict]:
    """Request or replay source-bound desk decisions before the runner tests them."""
    recon = pending.get("recon_evidence_bundle", pending["evidence_bundle"])
    if "adjudication_sha256" in pending:
        # Accepted bytes are already sealed; external edits cannot change a retry.
        source = verify_bundle(pending["evidence_bundle"])["desk-adjudication.json"]
    else:
        source = directory / (recon["sha256"] + ".json")
        if not source.exists():
            _atomic_write(artifacts / "desk-request.json", {
                "filename": source.name,
                "findings": common["findings"],
                "template": {"version": 1, "reviewed_head": head, "recon_sha256": recon["sha256"],
                             "reviewer": "", "decisions": [
                                 {"finding_id": ident, "verdict": "REVIEW_REQUIRED", "rationale": ""}
                                 for ident in sorted({fingerprint(row) for row in common["findings"]})]},
            })
            raise AdjudicationError(f"desk decisions required for recon {recon['sha256']}; see desk-request.json")
    value, raw = read_decision(source)
    active = actionable(value, head, recon, common["findings"])
    (artifacts / "desk-adjudication.json").write_bytes(raw)
    common.update(recon_evidence_bundle=recon, adjudication_sha256=hashlib.sha256(raw).hexdigest(),
                  valid_finding_count=len(active))
    return active


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
