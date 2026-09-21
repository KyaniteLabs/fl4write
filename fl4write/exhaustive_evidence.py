"""Content-addressed evidence bundles for exhaustive rounds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


class EvidenceError(ValueError):
    pass


def _canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def recon_ledger_context(ledger: dict) -> dict:
    """Summarize test identities for inference only; preserve the evidence ledger."""
    if "ledger" not in ledger:
        return ledger
    rows = []
    for row in ledger["ledger"]:
        projected = {key: value for key, value in row.items() if key != "test_ids"}
        if "test_ids" in row:
            projected["test_ids_summary"] = {
                "count": len(row["test_ids"]),
                "sha256": hashlib.sha256(_canonical(row["test_ids"])).hexdigest(),
            }
        rows.append(projected)
    return {**ledger, "ledger": rows}


def seal_bundle(source: Path, destination: Path) -> dict[str, str]:
    """Copy completed evidence; the archive is the authority for the source tree."""
    destination.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".sealing-", dir=destination))
    entries = {}
    try:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if relative.parts[0] == "tree":
                continue
            if path.is_symlink():
                raise EvidenceError("evidence contains a symlink")
            if path.is_dir():
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise EvidenceError("evidence contains a nonregular file")
            raw = path.read_bytes()
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o400)
            entries[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        if not {"head.tar", "manifest.json"} <= entries.keys():
            raise EvidenceError("evidence lacks its reviewed archive")
        raw_manifest = _canonical({"version": 1, "files": entries})
        digest = hashlib.sha256(raw_manifest).hexdigest()
        manifest = temporary / "bundle.json"
        manifest.write_bytes(raw_manifest)
        manifest.chmod(0o400)
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o500)
        target = destination / digest
        if target.exists():
            verify_bundle({"path": str(target), "sha256": digest})
        else:
            temporary.chmod(0o500)
            temporary.rename(target)
        return {"path": str(target), "sha256": digest}
    finally:
        if temporary.exists():
            for path in temporary.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            temporary.chmod(0o700)
            shutil.rmtree(temporary)


def verify_bundle(reference: dict) -> dict[str, Path]:
    try:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise EvidenceError("invalid evidence reference")
        root = Path(reference["path"])
        digest = reference["sha256"]
        if (not isinstance(digest, str) or len(digest) != 64 or root.name != digest
                or root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o222):
            raise EvidenceError("evidence bundle is mutable or misidentified")
        manifest = root / "bundle.json"
        if manifest.is_symlink() or manifest.stat().st_mode & 0o222:
            raise EvidenceError("evidence manifest is mutable")
        raw = manifest.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise EvidenceError("evidence manifest digest changed")
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {"version", "files"}
                or data["version"] != 1 or not isinstance(data["files"], dict)
                or not {"head.tar", "manifest.json"} <= data["files"].keys()):
            raise EvidenceError("invalid evidence manifest")
        actual = {}
        for path in root.rglob("*"):
            if path.is_symlink() or path.stat().st_mode & 0o222:
                raise EvidenceError("evidence contains mutable or linked entries")
            if path.is_file() and path != manifest:
                if not stat.S_ISREG(path.stat().st_mode):
                    raise EvidenceError("nonregular evidence")
                actual[path.relative_to(root).as_posix()] = path
        if actual.keys() != data["files"].keys():
            raise EvidenceError("evidence bundle contents changed")
        for name, path in actual.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != data["files"][name]:
                raise EvidenceError("evidence content digest changed")
        reviewed = json.loads(actual["manifest.json"].read_bytes())
        if reviewed.get("archive_sha256") != data["files"]["head.tar"]:
            raise EvidenceError("reviewed archive identity changed")
        return actual
    except EvidenceError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise EvidenceError("evidence unavailable or malformed") from exc
