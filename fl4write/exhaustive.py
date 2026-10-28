"""Evidence-bound, default-off exhaustive bug-resolution loop."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from . import scrub
from .config import ModelRoute, assert_credential_endpoints_trusted, load_config
from .executor import _sandbox_env_for
from .exhaustive_evidence import EvidenceError, seal_bundle, verify_bundle
from .exhaustive_adjudication import AdjudicationError, apply_decision, verified_findings
from .model_proxy import ProxyError
from .forges import ForgeAdapter, adapter_for
from .state import CycleLock, CycleLockHeld
# Cycle-1: the shared core lives in exhaustive_core (leaf); re-imported here
# so every existing reference -- internal callers, tests, satellites -- keeps
# resolving through this module unchanged.
from .exhaustive_core import (  # noqa: F401
    DEFAULT_CHUNK_CHARS, GREEN_REQUIRED, VERSION, Deferred, NonGreen, _run, _git, _identity, _atomic_json, _valid_sha,
    _fresh_state, _valid_ledger, _valid_pending, _load_state, _text_sources,
    _chunks, _validated, _RECON_SYSTEM, _SCREEN_SYSTEM, _recon_prompts,
    _number, _junit,
)

log = logging.getLogger("fl4write.exhaustive")



def _request_identity(args, config):
    isolation = getattr(args, "isolation", "docker")
    if getattr(args, "live_model_tests", False) and isolation != "docker":
        raise Deferred("live model tests require Docker isolation")
    test_image = getattr(args, "test_image", None)
    if isolation == "docker":
        from .exhaustive_sandbox import SandboxUnavailable, validate_runtime

        try:
            validate_runtime(test_image, require_model_proxy=getattr(args, "live_model_tests", False))
        except SandboxUnavailable as exc:
            raise Deferred(str(exc)) from exc
    # P2e: under Docker the container resolves the command with the image's own
    # interpreter (the argv carries the image entrypoint and the raw command),
    # so the HOST binary is not part of what is tested and must not enter the
    # request identity — a host toolchain upgrade mid-run would otherwise
    # invalidate a round for a change the tested environment never sees.
    executable = None
    if isolation != "docker":
        found = shutil.which(args.test_command[0]) if args.test_command else None
        if found is None:
            raise Deferred("selected test executable is unavailable")
        executable = Path(found).resolve()
    value = {
        "test_command": args.test_command, "executable": str(executable) if executable else None,
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest() if executable else None,
        "config": config.model_dump(mode="json"),
        "test_timeout": args.test_timeout, "process_timeout": args.process_timeout,
        "max_model_calls": args.max_model_calls, "max_output_tokens": args.max_output_tokens,
        "chunk_chars": args.chunk_chars,
        "isolation": isolation, "test_image": test_image,
        "live_model_tests": getattr(args, "live_model_tests", False),
        "base_branch": getattr(args, "base_branch", None),
        "environment": {k: os.environ.get(k) for k in ("FL4WRITE_EVAL", "FL4WRITE_EVAL_CONFIG", "PYTHONPATH")},
    }
    desk = getattr(args, "desk_adjudications", None)
    if desk is not None:
        value["desk_adjudications"] = str(desk.resolve())
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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
        executable = p.is_dir() or bool(p.stat().st_mode & 0o111)
        p.chmod(0o500 if executable else 0o400)
    tree.chmod(0o500)
    archive.chmod(0o400)
    manifest.chmod(0o400)
    return tree, manifest


def _disappeared_regressions(state: dict, head: str, test_ids: Sequence[str]) -> list:
    """Test IDs present in the last green round but absent now — enforced ONLY
    when that baseline was measured at the CURRENT head. Two different heads
    have different test sets by construction, so comparing across a head move
    reported ordinary upstream renames as regressions and wedged certification
    permanently (review P0, 2026-09-26). The baseline itself is kept intact
    (the ledger invariant still holds); it is simply not cross-head evidence.
    """
    if state.get("green_baseline_head") != head:
        return []
    return sorted(set(state["green_baseline"]) - set(test_ids))


def _worker(request_path: Path, caller: Callable | None = None) -> int:
    from .exhaustive_recon import worker

    return worker(request_path, caller)


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
    checkpoint: Path | None = None,
    screen_route: ModelRoute | None = None,
    concurrency: int = 1,
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
    if screen_route is not None:
        payload["screen_route"] = screen_route.model_dump()
    # Only carried when it changes behavior: at the default 1 the request
    # stays byte-identical to the serial format, so existing checkpoints
    # keep their identity (the key rides ReconProgress identity when set).
    if concurrency > 1:
        payload["concurrency"] = concurrency
    if fake_responses:
        payload["fake_responses"] = str(fake_responses)
    if checkpoint is not None:
        payload["checkpoint"] = str(checkpoint)
        payload["binding"] = {
            "round": checkpoint.name, "repository_state": str(checkpoint.parent.resolve()),
            "manifest": json.loads((artifact_dir / "manifest.json").read_bytes()),
            "selected_request": json.loads((artifact_dir / "selected-request.json").read_bytes()),
        }
    _atomic_json(request, payload)
    home = tempfile.mkdtemp(prefix="fl4write-exhaustive-worker-")
    try:
        env = _sandbox_env_for(home)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proxy = os.environ.get("FL4WRITE_MODEL_PROXY_SOCKET")
        if proxy:
            env["FL4WRITE_MODEL_PROXY_SOCKET"] = proxy
        else:
            for _route in (route, screen_route):
                if _route is None or not _route.key_env:
                    continue
                if not os.environ.get(_route.key_env):
                    raise Deferred(f"configured model credential {_route.key_env} is unavailable")
                env[_route.key_env] = os.environ[_route.key_env]
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


def _test(command: list[str], tree: Path, evidence: Path, timeout: int, *, isolation="process", image=None,
          model_proxy=None):
    # Cycle-1: the evidence token may also ride embedded in an argument
    # (e.g. --junit-xml={junit}.xml); it must still appear exactly once in
    # the substituted argv so the runner knows where evidence lands.
    argv = [x.replace("{junit}", str(evidence)) if "{junit}" in x else x for x in command]
    if sum(a.count(str(evidence)) for a in argv) != 1:
        raise NonGreen("test command requires {junit} exactly once")
    cleanup_note = ""
    if isolation == "docker":
        from .exhaustive_sandbox import SandboxCleanupPending, SandboxUnavailable, run_isolated

        try:
            options = {"model_proxy": model_proxy} if model_proxy is not None else {}
            done = run_isolated(command, tree, evidence, timeout, image, **options)
        except SandboxCleanupPending as exc:
            # P2c: the report is already written, so the run's own verdict
            # stands — an unresolved container cleanup must not reclassify a
            # red suite as a deferral. The reason rides alongside the verdict.
            done, cleanup_note = exc.result, str(exc)
        except SandboxUnavailable as exc:
            raise Deferred(str(exc)) from exc
    elif isolation == "process":
        home = tempfile.mkdtemp(prefix="fl4write-exhaustive-test-")
        try:
            done = _run(argv, tree, timeout, _sandbox_env_for(home),
                            new_session=True)
        finally:
            shutil.rmtree(home, ignore_errors=True)
    else:
        raise Deferred("unknown test isolation mode")
    ids, green, digest = _junit(evidence)
    if done.returncode or not green:
        reason = f"full suite not green (exit={done.returncode})"
        if cleanup_note:
            reason = f"{reason}; {cleanup_note}"
        raise NonGreen(reason, {"test_ids": sorted(ids), "junit_sha256": digest})
    if cleanup_note:
        # A green run whose container was never cleaned up cannot certify.
        raise Deferred(cleanup_note)
    return ids, digest


def _suite_runner(args):
    if getattr(args, "isolation", "docker") == "process":
        return _test
    from functools import partial

    options = {"isolation": "docker", "image": getattr(args, "test_image", None)}
    if getattr(args, "live_model_tests", False):
        options["model_proxy"] = getattr(args, "_model_proxy", None)
        if options["model_proxy"] is None:
            raise Deferred("live model tests require the active round transport")
    return partial(_test, **options)


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
    if getattr(args, "isolation", "docker") != "docker":
        raise Deferred("automatic fixes require the isolated container test runtime")
    from .exhaustive_fix import attempt_fix_with_regression_pin

    selection = {}
    if getattr(args, "base_branch", None) is not None:
        selection["base_branch"] = args.base_branch
    result = attempt_fix_with_regression_pin(
        repo, config, head, findings, args.test_command, evidence_dir,
        verify_suite=_suite_runner(args),
        test_timeout=args.test_timeout,
        **selection,
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


def _repo_supplied_config(config_path: Path, repo: Path) -> bool:
    """P1: a config that lives inside the reviewed tree is supplied by that
    tree, so its model route must not be allowed to aim a host credential at
    an endpoint of the repository's choosing."""
    try:
        return config_path.resolve().is_relative_to(repo.resolve())
    except (OSError, ValueError, RuntimeError):
        return True  # unresolvable path: treat as untrusted, never as trusted


def run(args: argparse.Namespace) -> int:
    repo, identity = _identity(args.repo.resolve())
    state_dir = args.state_dir.resolve() / identity
    state_path = state_dir / "state.json"
    state = _fresh_state(identity)
    try:
        desk = getattr(args, "desk_adjudications", None)
        if desk is not None:
            desk = desk.resolve()
            if desk.is_relative_to(repo) or desk.exists() and not desk.is_dir():
                raise Deferred("desk decisions must be in an external directory")
        with CycleLock(state_dir / "loop.lock"), ExitStack() as transports:
            state = _load_state(state_path, identity)
            config_path = args.config or repo / ".fl4write.yaml"
            config = load_config(config_path)
            if _repo_supplied_config(config_path, repo):
                try:
                    assert_credential_endpoints_trusted(config, source=str(config_path))
                except ValueError as exc:
                    raise Deferred(str(exc)) from exc
            request_sha = _request_identity(args, config)
            request_path = state_dir / "request-identity.json"
            if request_path.exists():
                try:
                    if json.loads(request_path.read_bytes()) != {"sha256": request_sha}:
                        raise Deferred("selected test command or configuration changed; existing round cannot be reused")
                except (OSError, ValueError, TypeError) as exc:
                    raise Deferred("request identity evidence is invalid") from exc
            elif state["ledger"] or state["pending_round"]:
                raise Deferred("existing rounds lack selected request identity")
            else:
                _atomic_json(request_path, {"sha256": request_sha})
            if (state["pending_round"] and state["pending_round"].get("request_sha256") != request_sha
                    or any(row.get("request_sha256") != request_sha
                           for row in state["ledger"][-state["consecutive_green"]:])
                    and state["consecutive_green"]):
                raise Deferred("selected request differs from pending or clean-round evidence")
            ledger_issue = getattr(args, "ledger_issue", None)
            binding, forge = _primary(config, getattr(args, "_forge_adapter", None))
            from .exhaustive_transaction import publish_round, replay_publication

            if replay_publication(repo, state_path, forge, config, ledger_issue):
                state = _load_state(state_path, identity)
                if state["certified_sha"]:
                    return 0
                # NEW-2 (2026-09-16): the transaction COMPLETED — persisting
                # state then raising Deferred produced a spurious
                # "human_action_required" escalation for a success. Fall
                # through and start the fresh round below instead.
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
                    raise Deferred("local rounds complete; owned ledger publication is required for certification")
                publish_state = dict(state)
                publish_state["certified_sha"] = current
                publish_round(repo, state_path, forge, config, ledger_issue, publish_state, True)
                state["certified_sha"] = current
                _atomic_json(state_path, state)
                _atomic_json(
                    state_dir / "certification.json",
                    {"status": "forge_published", "scope": "archived text recon and configured full test command",
                     "sha": current, "ledger_issue": ledger_issue},
                )
                return 0
            for _ in range(args.max_rounds):
                transports.close()
                if state["round"] >= args.round_cap:
                    raise Deferred("round cap reached")
                if _git(repo, "status", "--porcelain"):
                    raise Deferred("repository is dirty")
                head = _git(repo, "rev-parse", "HEAD")
                from .exhaustive_budget import round_transport
                budget = transports.enter_context(round_transport(
                    args, config, state_dir / "budgets" / f"round-{state['round'] + 1:04d}.json", request_sha))
                if state["green_sha"] is not None and state["green_sha"] != head:
                    state["consecutive_green"] = 0
                    state["green_sha"] = None
                    _atomic_json(state_path, state)
                state["head"] = head
                rd = state_dir / "artifacts" / f"round-{state['round'] + 1:04d}-{head[:12]}-{time.time_ns()}"
                tree, manifest = _pack(repo, head, rd)
                _atomic_json(rd / "selected-request.json", {"sha256": request_sha})
                if desk is not None:
                    _atomic_json(rd / "desk-mode.json", {"enabled": True})
                ledger = rd / "ledger-input.json"
                _atomic_json(ledger, {"ledger": state["ledger"]})
                pending = state["pending_round"]
                if pending:
                    try:
                        paths = verify_bundle(pending.get("evidence_bundle"))
                        if json.loads(paths["selected-request.json"].read_bytes()) != {"sha256": request_sha}:
                            raise EvidenceError("pending selected request changed")
                        if pending["reviewed_head"] != head:
                            _non_green(state, state_path, pending["reviewed_head"],
                                       "HEAD changed during pending round", pending)
                            continue
                        old_manifest = json.loads(paths["manifest.json"].read_bytes())
                        if old_manifest != json.loads(manifest.read_bytes()):
                            raise EvidenceError("pending archive differs from current HEAD archive")
                        for name in ("worker-result.json", "worker-request.json", "coverage-manifest.json"):
                            shutil.copyfile(paths[name], rd / name)
                        if "desk-adjudication.json" in paths:
                            shutil.copyfile(paths["desk-adjudication.json"], rd / "desk-adjudication.json")
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
                        state_dir / "recon-checkpoints" / f"round-{state['round']+1:04d}-{head}.json",
                        screen_route=config.screen_model,
                        concurrency=getattr(config, "concurrency", 1),
                    )
                common = {
                    "request_sha256": request_sha,
                    "pack_manifest": str(manifest),
                    "coverage_manifest": str(coverage),
                    "model_usage": budget.usage if budget is not None else usage,
                    "finding_count": len(findings),
                    "findings": findings,
                }
                if pending and pending.get("desk_adjudication") is True:
                    common["recon_evidence_bundle"] = pending.get("recon_evidence_bundle", pending["evidence_bundle"])
                if pending and "adjudication_sha256" in pending:
                    common.update({key: pending[key] for key in
                                   ("recon_evidence_bundle", "adjudication_sha256", "valid_finding_count")})
                if _git(repo, "rev-parse", "HEAD") != head:
                    _non_green(state, state_path, head, "HEAD changed during recon", common)
                    raise Deferred("HEAD changed during recon")
                state["pending_round"] = {"reviewed_head": head, **_bind_evidence(common)}
                active_findings = findings
                if desk is not None:
                    state["pending_round"]["desk_adjudication"] = True
                    if findings:
                        common.setdefault("recon_evidence_bundle", state["pending_round"]["evidence_bundle"])
                        state["pending_round"]["recon_evidence_bundle"] = common["recon_evidence_bundle"]
                    _atomic_json(state_path, state)
                    if findings:
                        active_findings = apply_decision(desk, head, common, rd, state["pending_round"])
                        state["pending_round"] = {"reviewed_head": head, "desk_adjudication": True,
                                                  **_bind_evidence(common)}
                        verified_findings(state["pending_round"])
                        if active_findings:
                            state["consecutive_green"], state["green_sha"] = 0, None
                        _atomic_json(state_path, state)
                if active_findings:
                    state["consecutive_green"] = 0
                    state["green_sha"] = None
                    _atomic_json(state_path, state)
                    try:
                        if not getattr(args, "enable_fixes", False):
                            raise Deferred("findings recorded; authenticated owned-PR exhaustive fixes are disabled")
                        fix = _request_owned_fixes(repo, config, head, active_findings, args,
                                                   state_dir / "fixes" / head)
                        if _git(repo, "rev-parse", "HEAD") != head or _git(repo, "status", "--porcelain"):
                            raise Deferred("local checkout changed during atomic fix")
                        from .exhaustive_fix import FixError, fetch_merged_head

                        try:
                            fetch_merged_head(repo, config, fix["merged_head"])
                        except FixError as exc:
                            raise Deferred(str(exc)) from exc
                        _git(repo, "merge", "--ff-only", fix["merged_head"])
                        if _git(repo, "rev-parse", "HEAD") != fix["merged_head"]:
                            raise Deferred("local refresh did not reach verified merged HEAD")
                        refreshed, _ = _pack(repo, fix["merged_head"], rd / "post-fix")
                        try:
                            _suite_runner(args)(args.test_command, refreshed, rd / "post-fix.xml", args.test_timeout)
                        except NonGreen as exc:
                            _non_green(state, state_path, head, "merged repair failed refreshed full suite",
                                       {**common, **exc.evidence, "fix": fix,
                                        "tested_head": fix["merged_head"]})
                            raise Deferred("merged repair failed refreshed full suite") from exc
                        transports.close()
                        _non_green(state, state_path, head, "findings fixed and merged; fresh recon required",
                                   {**common, "fix": fix})
                        continue
                    except Deferred as exc:
                        if not getattr(args, "enable_fixes", False):
                            _non_green(state, state_path, head, str(exc), common)
                            if ledger_issue is not None:
                                publish_round(repo, state_path, forge, config, ledger_issue, state)
                        raise
                _atomic_json(state_path, state)
                try:
                    test_ids, junit_hash = _suite_runner(args)(args.test_command, tree, rd / "full-suite.xml", args.test_timeout)
                except NonGreen as exc:
                    _non_green(state, state_path, head, str(exc), {**common, **exc.evidence})
                    raise Deferred(str(exc)) from exc
                transports.close()
                if _git(repo, "rev-parse", "HEAD") != head:
                    _non_green(state, state_path, head, "HEAD changed during tests", common)
                    raise Deferred("HEAD changed during tests")
                regressions = _disappeared_regressions(state, head, test_ids)
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
                state["green_baseline_head"] = head
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
                    _atomic_json(state_path, state)
                    if locally_complete:
                        raise Deferred("local rounds complete; owned ledger publication is required for certification")
                if state["certified_sha"]:
                    return 0
            raise Deferred("invocation round limit reached")
    except CycleLockHeld as exc:
        print(scrub.inline(str(exc)), file=sys.stderr)
        return 3
    except (Deferred, ProxyError, AdjudicationError) as exc:
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
    p.add_argument("--desk-adjudications", type=Path,
                   help="explicitly trust source-bound desk decision files in this external directory")
    p.add_argument("--base-branch", help="explicit existing repair target; defaults to the repository default branch")
    p.add_argument("--isolation", choices=("docker", "process"), default="docker",
                   help="Docker is required for automatic fixes; process is for trusted local diagnostics")
    p.add_argument("--test-image", help="immutable image SHA-256 of the installed test runtime")
    p.add_argument("--live-model-tests", action="store_true",
                   help="allow live tests through the shared bounded model transport (Docker only)")
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
