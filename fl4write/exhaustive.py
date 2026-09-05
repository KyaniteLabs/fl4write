"""Evidence-bound, default-off exhaustive bug-resolution loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from . import scrub
from .config import ModelRoute, load_config
from .executor import _sandbox_env_for
from .exhaustive_evidence import EvidenceError, seal_bundle, verify_bundle
from .forges import ForgeAdapter, adapter_for
from .state import CycleLock, CycleLockHeld

VERSION, GREEN_REQUIRED, DEFAULT_CHUNK_CHARS = 3, 3, 48_000


class Deferred(RuntimeError):
    pass


class NonGreen(RuntimeError):
    def __init__(self, reason: str, evidence: dict[str, Any] | None = None):
        super().__init__(reason)
        self.evidence = evidence or {}


def _run(argv: list[str], cwd: Path, timeout: int, env: dict[str, str]):
    try:
        return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Deferred(f"process unavailable: {exc}") from exc


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if result.returncode:
        raise Deferred(f"git {' '.join(args)} failed: {scrub.inline(result.stderr, 160)}")
    return result.stdout.strip()


def _identity(repo: Path) -> tuple[Path, str]:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return root, hashlib.sha256(f"{root}\0{common}".encode()).hexdigest()[:24]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".exhaustive-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
    except BaseException:
        Path(raw).unlink(missing_ok=True)
        raise


def _valid_sha(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 40 and all(c in "0123456789abcdef" for c in v)


def _fresh_state(identity: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "repo_identity": identity,
        "head": None,
        "round": 0,
        "consecutive_green": 0,
        "green_baseline": [],
        "green_sha": None,
        "certified_sha": None,
        "pending_round": None,
        "ledger": [],
    }


def _valid_ledger(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    for expected, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("round") != expected:
            return False
        if not _valid_sha(row.get("reviewed_head")) or not isinstance(row.get("green"), bool):
            return False
        if row["green"]:
            ids = row.get("test_ids")
            if (not isinstance(ids, list) or not ids
                    or any(not isinstance(v, str) or not v for v in ids)
                    or len(set(ids)) != len(ids)
                    or not isinstance(row.get("junit_sha256"), str)
                    or len(row["junit_sha256"]) != 64):
                return False
        elif not isinstance(row.get("reason"), str) or not row["reason"]:
            return False
    return True


def _valid_pending(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or not _valid_sha(value.get("reviewed_head")):
        return False
    count = value.get("finding_count")
    return not isinstance(count, bool) and isinstance(count, int) and count >= 0


def _load_state(path: Path, identity: str) -> dict[str, Any]:
    if not path.exists():
        return _fresh_state(identity)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Deferred(f"state unreadable; refusing unsafe recovery: {exc}") from exc
    keys = {
        "version",
        "repo_identity",
        "head",
        "round",
        "consecutive_green",
        "green_baseline",
        "green_sha",
        "certified_sha",
        "pending_round",
        "ledger",
    }
    ints = (data.get("round"), data.get("consecutive_green")) if isinstance(data, dict) else ()
    bad = (
        not isinstance(data, dict)
        or set(data) != keys
        or data.get("version") != VERSION
        or data.get("repo_identity") != identity
        or data.get("head") is not None and not _valid_sha(data.get("head"))
        or len(ints) != 2
        or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in ints)
        or data.get("consecutive_green", 0) > GREEN_REQUIRED
        or not _valid_ledger(data.get("ledger"))
        or data.get("round") != len(data.get("ledger", []))
        or not isinstance(data.get("green_baseline"), list)
        or any(not isinstance(item, str) or not item.strip() for item in data.get("green_baseline", []))
        or len(set(data.get("green_baseline", []))) != len(data.get("green_baseline", []))
        or data.get("green_sha") is not None
        and not _valid_sha(data.get("green_sha"))
        or not _valid_pending(data.get("pending_round"))
        or data.get("certified_sha") is not None
        and not _valid_sha(data.get("certified_sha"))
        or data.get("certified_sha") is not None
        and (data.get("consecutive_green") != GREEN_REQUIRED or data.get("green_sha") != data.get("certified_sha"))
    )
    if bad:
        raise Deferred("state shape, counters, or repository identity invalid")
    trailing = 0
    for row in reversed(data["ledger"]):
        if not row["green"] or row["reviewed_head"] != data["head"]:
            break
        trailing += 1
    if data["pending_round"] and data["pending_round"]["finding_count"]:
        trailing = 0
    baseline = next((row["test_ids"] for row in reversed(data["ledger"]) if row["green"]), [])
    if (trailing != data["consecutive_green"]
            or data["green_sha"] != (data["head"] if trailing else None)
            or sorted(data["green_baseline"]) != sorted(baseline)):
        raise Deferred("state counters disagree with verified ledger history")
    try:
        for row in data["ledger"]:
            paths = verify_bundle(row.get("evidence_bundle"))
            manifest = json.loads(paths["manifest.json"].read_bytes())
            if manifest.get("head") != row["reviewed_head"]:
                raise EvidenceError("round archive does not match reviewed HEAD")
            if row["green"]:
                if row.get("tested_head") != row["reviewed_head"] or row.get("finding_count") != 0:
                    raise EvidenceError("green round lacks matching tested HEAD or zero findings")
                ids, green, digest = _junit(paths["full-suite.xml"])
                if not green or sorted(ids) != sorted(row["test_ids"]) or digest != row["junit_sha256"]:
                    raise EvidenceError("green round disagrees with its full-suite evidence")
    except (EvidenceError, NonGreen, OSError, ValueError, KeyError) as exc:
        raise Deferred(f"round evidence invalid: {exc}") from exc
    return data


def _bind_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Bind a completed round to sealed copies, never mutable working artifacts."""
    if "evidence_bundle" in evidence:
        verify_bundle(evidence["evidence_bundle"])
        return evidence
    try:
        source = Path(evidence["pack_manifest"]).parent
        reference = seal_bundle(source, source.parent.parent / "bundles")
        paths = verify_bundle(reference)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise Deferred("round evidence cannot be sealed") from exc
    return {**evidence, "evidence_bundle": reference,
            "pack_manifest": str(paths["manifest.json"]),
            "coverage_manifest": str(paths["coverage-manifest.json"])}


def _pack(repo: Path, head: str, output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "head.tar"
    with archive.open("wb") as stream:
        result = subprocess.run(
            ["git", "archive", "--format=tar", head], cwd=repo, stdout=stream, stderr=subprocess.PIPE
        )
    if result.returncode:
        raise Deferred("git archive failed")
    tree = output / "tree"
    tree.mkdir()
    with tarfile.open(archive, "r:") as tf:
        tf.extractall(tree, filter="data")
    manifest = output / "manifest.json"
    _atomic_json(
        manifest, {"version": VERSION, "head": head, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    )
    for p in tree.rglob("*"):
        p.chmod(0o500 if p.is_dir() else 0o400)
    tree.chmod(0o500)
    archive.chmod(0o400)
    manifest.chmod(0o400)
    return tree, manifest


def _text_sources(tree: Path):
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        rel = path.relative_to(tree).as_posix()
        try:
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise Deferred(f"tracked text candidate unreadable: {rel}") from exc
        yield rel, text, hashlib.sha256(raw).hexdigest()


def _chunks(text: str, limit: int):
    lines, start, buf = text.splitlines(keepends=True) or [""], 1, ""
    for number, line in enumerate(lines, 1):
        if len(line) > limit:
            raise Deferred(f"source line {number} exceeds chunk limit")
        if buf and len(buf) + len(line) > limit:
            yield start, number - 1, buf
            start, buf = number, ""
        buf += line
    yield start, len(lines), buf


def _validated(value: Any, path: str, start: int, end: int, source: str):
    if not isinstance(value, dict) or set(value) != {"findings"} or not isinstance(value["findings"], list):
        raise Deferred("model returned malformed recon JSON")
    lines, out = source.splitlines(), []
    for row in value["findings"]:
        line, evidence = (
            row.get("line") if isinstance(row, dict) else None,
            row.get("evidence") if isinstance(row, dict) else None,
        )
        if (
            not isinstance(row, dict)
            or row.get("path") != path
            or isinstance(line, bool)
            or not isinstance(line, int)
            or line < start
            or line > end
            or not isinstance(evidence, str)
            or not evidence
            or evidence not in lines[line - 1]
        ):
            raise Deferred("model finding is not grounded at its claimed archived line")
        clean = {str(k): scrub.redact_credentials(scrub.scrub(str(v))) for k, v in row.items()}
        clean["line"] = line
        out.append(clean)
    return out


def _worker(request_path: Path, caller: Callable | None = None) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    route = ModelRoute.model_validate(request["route"])
    tree, ledger = Path(request["tree"]), json.loads(Path(request["ledger"]).read_text())
    fake = request.get("fake_responses")
    if fake:
        queue = list(json.loads(Path(fake).read_text()))

        def call(*_args):
            if not queue:
                raise RuntimeError("fake response queue exhausted")
            return json.dumps(queue.pop(0))
    else:
        if caller is None:
            from .analyzer import _call_model

            caller = _call_model
        call = caller
    findings, coverage, calls, reserved = [], [], 0, 0
    for path, source, digest in _text_sources(tree):
        for start, end, body in _chunks(source, request["chunk_chars"]):
            if calls >= request["max_calls"] or reserved + route.max_tokens > request["max_tokens"]:
                raise Deferred("model call or reserved output-token budget exhausted before full coverage")
            calls += 1
            reserved += route.max_tokens
            prompt = json.dumps(
                {"ledger": ledger, "path": path, "start_line": start, "end_line": end, "content": body}, sort_keys=True
            )
            system = 'Audit archived source. Return only {"findings": []}; findings require path, absolute line, exact single-line evidence, severity, message.'
            try:
                value = json.loads(call(route, prompt, "file", system))
            except Exception as exc:
                raise Deferred(f"model unavailable or returned unusable output: {exc}") from exc
            findings += _validated(value, path, start, end, source)
            coverage.append(
                {"path": path, "sha256": digest, "bytes": len(source.encode()), "start_line": start, "end_line": end}
            )
    Path(request["result"]).write_text(
        json.dumps({"findings": findings, "coverage": coverage, "calls": calls, "reserved_output_tokens": reserved})
    )
    return 0


def _recon(
    tree: Path,
    ledger: Path,
    route: ModelRoute,
    max_calls: int,
    max_tokens: int,
    timeout: int,
    artifact_dir: Path,
    chunk_chars: int,
    fake_responses: Path | None = None,
):
    request, result = artifact_dir / "worker-request.json", artifact_dir / "worker-result.json"
    payload = {
        "route": route.model_dump(),
        "tree": str(tree),
        "ledger": str(ledger),
        "max_calls": max_calls,
        "max_tokens": max_tokens,
        "chunk_chars": chunk_chars,
        "result": str(result),
    }
    if fake_responses:
        payload["fake_responses"] = str(fake_responses)
    _atomic_json(request, payload)
    home = tempfile.mkdtemp(prefix="fl4write-exhaustive-worker-")
    try:
        env = _sandbox_env_for(home)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if route.key_env:
            if not os.environ.get(route.key_env):
                raise Deferred(f"configured model credential {route.key_env} is unavailable")
            env[route.key_env] = os.environ[route.key_env]
        done = _run([sys.executable, "-m", "fl4write.exhaustive", "--trusted-worker", str(request)], tree, timeout, env)
        if done.returncode or not result.is_file():
            raise Deferred(f"trusted recon worker failed ({done.returncode}): {scrub.inline(done.stderr, 200)}")
        value = json.loads(result.read_text())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    coverage = artifact_dir / "coverage-manifest.json"
    _atomic_json(coverage, {"version": VERSION, "entries": value["coverage"]})
    return (
        value["findings"],
        coverage,
        {"calls": value["calls"], "reserved_output_tokens": value["reserved_output_tokens"]},
    )


def _number(v: str | None, name: str) -> int:
    if not isinstance(v, str) or not re.fullmatch(r"[0-9]+", v):
        raise NonGreen(f"JUnit {name} is not a nonnegative integer")
    return int(v)


def _junit(path: Path):
    try:
        raw = path.read_bytes()
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise NonGreen("JUnit document declarations are unsupported")
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as exc:
        raise NonGreen(f"JUnit evidence unreadable: {exc}") from exc
    ids: set[str] = set()

    def walk(node):
        if node.tag not in {"testsuites", "testsuite"}:
            raise NonGreen("JUnit unsupported root, namespace, or suite")
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for child in node:
            if child.tag in {"testsuite", "testsuites"}:
                subtotal = walk(child)
                for key in counts:
                    counts[key] += subtotal[key]
            elif child.tag == "testcase" and node.tag == "testsuite":
                name = child.get("name", "")
                identity = f"{child.get('classname', '')}::{name}"
                if not name.strip() or identity in ids:
                    raise NonGreen("JUnit missing or duplicate test ID")
                ids.add(identity)
                counts["tests"] += 1
                outcomes = []
                for detail in child:
                    if detail.tag in {"failure", "error", "skipped"}:
                        outcomes.append(detail.tag)
                    elif detail.tag not in {"properties", "system-out", "system-err"}:
                        raise NonGreen("JUnit unsupported testcase shape")
                if len(outcomes) > 1:
                    raise NonGreen("JUnit contradictory testcase outcomes")
                for outcome in outcomes:
                    counts[{"failure": "failures", "error": "errors", "skipped": "skipped"}[outcome]] += 1
            elif child.tag not in {"properties", "system-out", "system-err"}:
                raise NonGreen("JUnit unsupported suite child")
        for key, actual in counts.items():
            if key in node.attrib and _number(node.get(key), key) != actual:
                raise NonGreen(f"JUnit contradictory {key} aggregate")
        return counts

    counts = walk(root)
    if not counts["tests"]:
        raise NonGreen("JUnit has no test cases")
    green = not any(counts[k] for k in ("failures", "errors", "skipped"))
    return ids, green, hashlib.sha256(raw).hexdigest()


def _test(command: list[str], tree: Path, evidence: Path, timeout: int):
    argv = [str(evidence) if x == "{junit}" else x for x in command]
    if str(evidence) not in argv:
        raise NonGreen("test command requires standalone {junit}")
    home = tempfile.mkdtemp(prefix="fl4write-exhaustive-test-")
    try:
        done = _run(argv, tree, timeout, _sandbox_env_for(home))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    ids, green, digest = _junit(evidence)
    if done.returncode or not green:
        raise NonGreen(
            f"full suite not green (exit={done.returncode})", {"test_ids": sorted(ids), "junit_sha256": digest}
        )
    return ids, digest


def _escalate(path: Path, reason: str, state: dict[str, Any]):
    _atomic_json(
        path,
        {
            "status": "human_action_required",
            "reason": scrub.inline(reason, 300),
            "round": state["round"],
            "consecutive_green": state["consecutive_green"],
            "ledger": state["ledger"],
        },
    )


def _ledger_body(state: dict[str, Any], certification: bool = False, repo: str = "fixture/repo") -> str:
    from .exhaustive_publication import ledger_body

    return ledger_body(state, repo, certification)


def _publish(adapter: ForgeAdapter, repo: str, issue: int, state: dict[str, Any], certification: bool = False) -> None:
    from .exhaustive_publication import PublicationError, publish_owned

    try:
        publish_owned(adapter, repo, issue, adapter.bot_login, _ledger_body(state, certification, repo))
    except PublicationError as exc:
        raise Deferred(str(exc)) from exc


def _primary(config, injected: ForgeAdapter | None = None) -> tuple[Any, ForgeAdapter]:
    binding = next(b for b in config.forges.values() if b.role == "primary")
    adapter = injected or adapter_for(binding)
    adapter.bot_login = config.bot_login
    return binding, adapter


def _request_owned_fixes(repo, config, head, findings, args, evidence_dir):
    """Enter the atomic executor only after explicit runtime capability checks."""
    if config.shadow or not config.fix.enabled or not config.fix.merge_own_prs:
        raise Deferred("exhaustive fixes require non-shadow config, fix.enabled and merge_own_prs")
    from .exhaustive_fix import attempt_fix_with_regression_pin

    result = attempt_fix_with_regression_pin(
        repo, config, head, findings, args.test_command, evidence_dir,
        verify_suite=_test,
    )
    if result.get("status") != "merged" or not _valid_sha(result.get("merged_head")):
        raise Deferred(f"atomic fix {result.get('status', 'error')}: {result.get('reason', 'unproven')}")
    return result


def _non_green(state, state_path, head, reason, evidence=None):
    evidence = _bind_evidence(evidence or {})
    state["round"] += 1
    state["consecutive_green"] = 0
    state["green_sha"] = None
    state["certified_sha"] = None
    state["ledger"].append(
        {
            "round": state["round"],
            "reviewed_head": head,
            "green": False,
            "reason": scrub.inline(reason, 300),
            "timestamp": int(time.time()),
            **(evidence or {}),
        }
    )
    state["pending_round"] = None
    _atomic_json(state_path, state)


def run(args: argparse.Namespace) -> int:
    repo, identity = _identity(args.repo.resolve())
    state_dir = args.state_dir.resolve() / identity
    state_path = state_dir / "state.json"
    state = _fresh_state(identity)
    try:
        with CycleLock(state_dir / "loop.lock"):
            state = _load_state(state_path, identity)
            config = load_config(args.config or repo / ".fl4write.yaml")
            ledger_issue = getattr(args, "ledger_issue", None)
            binding, forge = _primary(config, getattr(args, "_forge_adapter", None))
            from .exhaustive_transaction import publish_round, replay_publication

            if replay_publication(repo, state_path, forge, config, ledger_issue):
                state = _load_state(state_path, identity)
                if state["certified_sha"]:
                    return 0
                raise Deferred("publication recovered; next invocation starts a fresh round")
            current = _git(repo, "rev-parse", "HEAD")
            if state["head"] != current:
                if state["certified_sha"]:
                    _atomic_json(
                        state_dir / "certification.json",
                        {"status": "invalidated", "reason": "HEAD moved",
                         "previous_sha": state["certified_sha"]},
                    )
                state["head"] = current
                state["consecutive_green"] = 0
                state["green_sha"] = None
                state["certified_sha"] = None
                if state_path.exists():
                    _atomic_json(state_path, state)
            if state["certified_sha"]:
                if state["certified_sha"] == current:
                    return 0
                old = state["certified_sha"]
                state["certified_sha"] = None
                state["consecutive_green"] = 0
                state["green_sha"] = None
                _atomic_json(
                    state_dir / "certification.json",
                    {"status": "invalidated", "reason": "HEAD moved", "previous_sha": old},
                )
                _atomic_json(state_path, state)
            if state["consecutive_green"] == GREEN_REQUIRED and state["green_sha"] == current:
                if ledger_issue is None:
                    return 0
                publish_state = dict(state)
                publish_state["certified_sha"] = current
                _publish(forge, config.repo, ledger_issue, publish_state, certification=True)
                state["certified_sha"] = current
                _atomic_json(state_path, state)
                _atomic_json(
                    state_dir / "certification.json",
                    {"status": "forge_published", "scope": "archived text recon and configured full test command",
                     "sha": current, "ledger_issue": ledger_issue},
                )
                return 0
            for _ in range(args.max_rounds):
                if state["round"] >= args.round_cap:
                    raise Deferred("round cap reached")
                if _git(repo, "status", "--porcelain"):
                    raise Deferred("repository is dirty")
                head = _git(repo, "rev-parse", "HEAD")
                if state["green_sha"] is not None and state["green_sha"] != head:
                    state["consecutive_green"] = 0
                    state["green_sha"] = None
                    _atomic_json(state_path, state)
                state["head"] = head
                rd = state_dir / "artifacts" / f"round-{state['round'] + 1:04d}-{head[:12]}-{time.time_ns()}"
                tree, manifest = _pack(repo, head, rd)
                ledger = rd / "ledger-input.json"
                _atomic_json(ledger, {"ledger": state["ledger"]})
                pending = state["pending_round"]
                if pending:
                    try:
                        paths = verify_bundle(pending.get("evidence_bundle"))
                        if pending["reviewed_head"] != head:
                            _non_green(state, state_path, pending["reviewed_head"],
                                       "HEAD changed during pending round", pending)
                            continue
                        old_manifest = json.loads(paths["manifest.json"].read_bytes())
                        if old_manifest != json.loads(manifest.read_bytes()):
                            raise EvidenceError("pending archive differs from current HEAD archive")
                        for name in ("worker-result.json", "worker-request.json", "coverage-manifest.json"):
                            shutil.copyfile(paths[name], rd / name)
                        result = json.loads(paths["worker-result.json"].read_bytes())
                        findings = result["findings"]
                        if findings != pending["findings"] or len(findings) != pending["finding_count"]:
                            raise EvidenceError("pending findings disagree with sealed recon")
                        usage = pending["model_usage"]
                        coverage = rd / "coverage-manifest.json"
                    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
                        raise Deferred("pending recon evidence invalid; retained for recovery") from exc
                else:
                    findings, coverage, usage = _recon(
                        tree,
                        ledger,
                        config.model,
                        args.max_model_calls,
                        args.max_output_tokens,
                        args.process_timeout,
                        rd,
                        args.chunk_chars,
                        getattr(args, "_fake_responses", None),
                    )
                common = {
                    "pack_manifest": str(manifest),
                    "coverage_manifest": str(coverage),
                    "model_usage": usage,
                    "finding_count": len(findings),
                    "findings": findings,
                }
                if _git(repo, "rev-parse", "HEAD") != head:
                    _non_green(state, state_path, head, "HEAD changed during recon", common)
                    raise Deferred("HEAD changed during recon")
                state["pending_round"] = {"reviewed_head": head, **_bind_evidence(common)}
                if findings:
                    state["consecutive_green"] = 0
                    state["green_sha"] = None
                    _atomic_json(state_path, state)
                    try:
                        if not getattr(args, "enable_fixes", False):
                            raise Deferred("findings recorded; authenticated owned-PR exhaustive fixes are disabled")
                        fix = _request_owned_fixes(repo, config, head, findings, args,
                                                   state_dir / "fixes" / head)
                        if _git(repo, "rev-parse", "HEAD") != head or _git(repo, "status", "--porcelain"):
                            raise Deferred("local checkout changed during atomic fix")
                        _git(repo, "fetch", "origin", fix["merged_head"])
                        _git(repo, "merge", "--ff-only", fix["merged_head"])
                        if _git(repo, "rev-parse", "HEAD") != fix["merged_head"]:
                            raise Deferred("local refresh did not reach verified merged HEAD")
                        refreshed, _ = _pack(repo, fix["merged_head"], rd / "post-fix")
                        _test(args.test_command, refreshed, rd / "post-fix.xml", args.test_timeout)
                        _non_green(state, state_path, head, "findings fixed and merged; fresh recon required",
                                   {**common, "fix": fix})
                        continue
                    except Deferred as exc:
                        if not getattr(args, "enable_fixes", False):
                            _non_green(state, state_path, head, str(exc), common)
                            if ledger_issue is not None:
                                _publish(forge, config.repo, ledger_issue, state)
                        raise
                _atomic_json(state_path, state)
                try:
                    test_ids, junit_hash = _test(args.test_command, tree, rd / "full-suite.xml", args.test_timeout)
                except NonGreen as exc:
                    _non_green(state, state_path, head, str(exc), {**common, **exc.evidence})
                    raise Deferred(str(exc)) from exc
                if _git(repo, "rev-parse", "HEAD") != head:
                    _non_green(state, state_path, head, "HEAD changed during tests", common)
                    raise Deferred("HEAD changed during tests")
                regressions = sorted(set(state["green_baseline"]) - test_ids)
                if regressions:
                    _non_green(
                        state,
                        state_path,
                        head,
                        "prior green test IDs disappeared",
                        {
                            **common,
                            "regressions": regressions,
                            "test_ids": sorted(test_ids),
                            "junit_sha256": junit_hash,
                        },
                    )
                    continue
                common = _bind_evidence(common)
                state["pending_round"] = None
                state["round"] += 1
                state["consecutive_green"] += 1
                state["green_sha"] = head
                state["green_baseline"] = sorted(test_ids)
                state["ledger"].append(
                    {
                        "round": state["round"],
                        "reviewed_head": head,
                        "tested_head": head,
                        "green": True,
                        "test_ids": sorted(test_ids),
                        "junit_sha256": junit_hash,
                        "timestamp": int(time.time()),
                        **common,
                    }
                )
                locally_complete = state["consecutive_green"] == GREEN_REQUIRED
                ready_to_certify = locally_complete and ledger_issue is not None
                if locally_complete:
                    _atomic_json(
                        state_dir / "certification.json",
                        {
                            "status": "forge_certification_pending" if ready_to_certify else "local_evidence_green",
                            "scope": "archived text recon and configured full test command",
                            "sha": head,
                        },
                    )
                if ledger_issue is not None:
                    if ready_to_certify:
                        state["certified_sha"] = head
                    publish_round(repo, state_path, forge, config, ledger_issue, state, ready_to_certify)
                else:
                    if locally_complete:
                        state["certified_sha"] = head
                    _atomic_json(state_path, state)
                if state["certified_sha"]:
                    return 0
            raise Deferred("invocation round limit reached")
    except CycleLockHeld as exc:
        print(scrub.inline(str(exc)), file=sys.stderr)
        return 3
    except Deferred as exc:
        _escalate(state_dir / "escalation.json", str(exc), state)
        print(scrub.inline(str(exc)), file=sys.stderr)
        return 2


def _parser():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--test-command", type=shlex.split, required=True)
    p.add_argument("--max-rounds", type=int, default=1)
    p.add_argument("--round-cap", type=int, default=12)
    p.add_argument("--max-model-calls", type=int, default=64)
    p.add_argument("--max-output-tokens", type=int, default=100_000)
    p.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    p.add_argument("--process-timeout", type=int, default=1800)
    p.add_argument("--test-timeout", type=int, default=3600)
    p.add_argument("--ledger-issue", type=int)
    p.add_argument("--enable-fixes", action="store_true")
    return p


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--trusted-worker":
        try:
            return _worker(Path(sys.argv[2]))
        except Exception as exc:
            print(scrub.inline(str(exc), 300), file=sys.stderr)
            return 2
    p = _parser()
    args = p.parse_args()
    for name in (
        "max_rounds",
        "round_cap",
        "max_model_calls",
        "max_output_tokens",
        "chunk_chars",
        "process_timeout",
        "test_timeout",
    ):
        if getattr(args, name) <= 0:
            p.error(f"--{name.replace('_', '-')} must be positive")
    if args.ledger_issue is not None and args.ledger_issue <= 0:
        p.error("--ledger-issue must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
