"""Evidence-bound, default-off exhaustive bug-resolution loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from .forges import ForgeAdapter, _is_github_base, adapter_for
from .models import Finding
from .state import CycleLock, CycleLockHeld

VERSION, GREEN_REQUIRED, DEFAULT_CHUNK_CHARS = 2, 3, 48_000


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
            if (not isinstance(ids, list)
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
    return data


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
    try:
        n = int(v or "0")
    except (TypeError, ValueError) as exc:
        raise NonGreen(f"JUnit {name} is not an integer") from exc
    if n < 0:
        raise NonGreen(f"JUnit {name} is negative")
    return n


def _junit(path: Path):
    try:
        raw = path.read_bytes()
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as exc:
        raise NonGreen(f"JUnit evidence unreadable: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise NonGreen("JUnit has no test suites")
    aggregate_bad = any(_number(s.get(k), k) for s in suites for k in ("failures", "errors", "skipped"))
    declared = sum(_number(s.get("tests"), "tests") for s in suites)
    cases = list(root.iter("testcase"))
    ids = {f"{c.get('classname', '')}::{c.get('name', '')}" for c in cases if c.get("name")}
    child_bad = any(c.find(k) is not None for c in cases for k in ("failure", "error", "skipped"))
    if declared <= 0 or len(cases) != declared or len(ids) != len(cases):
        raise NonGreen("JUnit empty, inconsistent, or duplicate IDs")
    return ids, not aggregate_bad and not child_bad, hashlib.sha256(raw).hexdigest()


def _test(command: list[str], tree: Path, evidence: Path, timeout: int):
    argv = [str(evidence) if x == "{junit}" else x for x in command]
    if str(evidence) not in argv:
        raise NonGreen("test command requires standalone {junit}")
    home = tempfile.mkdtemp(prefix="fl4write-exhaustive-test-")
    try:
        done = _run(argv, tree, timeout, _sandbox_env_for(home))
    except Deferred as exc:
        raise NonGreen(str(exc)) from exc
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


def _public_value(value: Any) -> Any:
    """Recursively scrub values before they cross the forge boundary."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            clean_key = scrub.inline(str(key), 80)
            if clean_key in {"pack_manifest", "coverage_manifest"} and isinstance(item, str):
                out[clean_key] = Path(item).name
            else:
                out[clean_key] = _public_value(item)
        return out
    if isinstance(value, list):
        return [_public_value(v) for v in value]
    if isinstance(value, str):
        return scrub.redact_credentials(scrub.scrub(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub.inline(str(value), 300)


def _ledger_body(state: dict[str, Any], certification: bool = False) -> str:
    """Deterministic issue body: PATCH retries repeat the identical write."""
    rows = _public_value(state["ledger"])
    payload = json.dumps(rows, indent=2, sort_keys=True)
    heading = "FL4WRITE exhaustive certification" if certification else "FL4WRITE exhaustive round ledger"
    status = (
        f"Certified: exhaustively flushed @ `{state['certified_sha']}`"
        if certification
        else f"Rounds: {state['round']}; consecutive clean: {state['consecutive_green']}/{GREEN_REQUIRED}"
    )
    return f"## {heading}\n\n{status}\n\n```json\n{payload}\n```\n"


def _publish(adapter: ForgeAdapter, repo: str, issue: int, state: dict[str, Any], certification: bool = False) -> None:
    raise Deferred("draft quarantined: forge publication requires unresolved safety repairs and independent approval")


def _primary(config, injected: ForgeAdapter | None = None) -> tuple[Any, ForgeAdapter]:
    binding = next(b for b in config.forges.values() if b.role == "primary")
    return binding, injected or adapter_for(binding)


def _request_owned_fixes(config, binding, adapter: ForgeAdapter, head: str, findings: list[dict[str, Any]]):
    """Enter the authenticated fix lane, failing closed at its missing batch API.

    executor.attempt_fix can only replace one finding's source file. Feature 13
    requires one tested PR containing both the fix and a regression pin, so
    opening its current one-file PR would create an artifact that cannot meet
    the contract. Keep this integration boundary explicit until executor grows
    the API described in the returned blocker.
    """
    if not _is_github_base(binding.api_base):
        raise Deferred("Forgejo exhaustive fix adapter unsupported; human escalation required")
    if config.shadow or not config.fix.enabled:
        raise Deferred("GitHub exhaustive fixes require non-shadow config with fix.enabled")
    from .appauth import install_token_to_env

    install_token_to_env(repo=config.repo)
    token = os.environ.get("CODESITTER_GITHUB_TOKEN", "")
    if not token:
        raise Deferred("GitHub App installation token unavailable")
    if binding.token_env:
        os.environ[binding.token_env] = token
    # Validate finding construction now, before any future external mutation.
    for row in findings:
        try:
            Finding(
                rule_id=str(row.get("rule_id") or "general"), severity=str(row["severity"]),
                path=str(row["path"]), line=row["line"], message=str(row["message"]),
                proposal=str(row.get("proposal") or ""),
            )
        except Exception as exc:
            raise Deferred(f"finding cannot enter fix lane: {type(exc).__name__}") from exc
    raise Deferred(
        "GitHub App authentication established, but executor lacks an atomic "
        "attempt_fix_with_regression_pin(pr, findings, config) API returning "
        "PR number, author, fork flag, base SHA, changed paths, test IDs, and "
        "JUnit hash; refusing one-file PRs that cannot prove regression pins"
    )


def _non_green(state, state_path, head, reason, evidence=None):
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
        if getattr(args, "ledger_issue", None) is not None:
            raise Deferred("draft quarantined: forge publication is disabled")
        with CycleLock(state_dir / "loop.lock"):
            state = _load_state(state_path, identity)
            current = _git(repo, "rev-parse", "HEAD")
            if state["pending_round"]:
                pending = state["pending_round"]
                _non_green(
                    state,
                    state_path,
                    pending.get("reviewed_head", current),
                    "recovered interrupted pending round",
                    pending,
                )
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
            config = load_config(args.config or repo / ".fl4write.yaml")
            ledger_issue = getattr(args, "ledger_issue", None)
            binding, forge = _primary(config, getattr(args, "_forge_adapter", None))
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
                    state["green_baseline"] = []
                    state["green_sha"] = None
                    _atomic_json(state_path, state)
                rd = state_dir / "artifacts" / f"round-{state['round'] + 1:04d}-{head[:12]}-{time.time_ns()}"
                tree, manifest = _pack(repo, head, rd)
                ledger = rd / "ledger-input.json"
                _atomic_json(ledger, {"ledger": state["ledger"]})
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
                if findings:
                    state["pending_round"] = {"reviewed_head": head, **common}
                    state["consecutive_green"] = 0
                    state["green_sha"] = None
                    _atomic_json(state_path, state)
                    try:
                        if not getattr(args, "enable_fixes", False):
                            raise Deferred("findings recorded; authenticated owned-PR exhaustive fixes are disabled")
                        _request_owned_fixes(config, binding, forge, head, findings)
                    except Deferred as exc:
                        _non_green(state, state_path, head, str(exc), common)
                        if ledger_issue is not None:
                            _publish(forge, config.repo, ledger_issue, state)
                        raise
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
                _atomic_json(state_path, state)
                if ledger_issue is not None:
                    publish_state = dict(state)
                    if ready_to_certify:
                        publish_state["certified_sha"] = head
                    _publish(forge, config.repo, ledger_issue, publish_state, certification=ready_to_certify)
                    if ready_to_certify:
                        state["certified_sha"] = head
                        _atomic_json(state_path, state)
                        _atomic_json(
                            state_dir / "certification.json",
                            {
                                "status": "forge_published",
                                "scope": "archived text recon and configured full test command",
                                "sha": head,
                                "ledger_issue": ledger_issue,
                            },
                        )
                elif locally_complete:
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
