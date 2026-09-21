"""Docker test isolation with no host output mount or forge credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


class SandboxUnavailable(RuntimeError):
    pass


WORKER = "/opt/fl4write-test-worker.py"
MAX_REPORT = 16 * 1024 * 1024


def container_command(command: list[str], tree: Path, image: str, name: str, timeout: int,
                      *, model_proxy=None) -> list[str]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image or ""):
        raise SandboxUnavailable("test image must be an immutable local image SHA-256")
    if not tree.is_dir() or "," in str(tree.resolve()):
        raise SandboxUnavailable("test tree is unavailable or has an unsupported mount path")
    if not command or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
        raise SandboxUnavailable("test command must be fixed argv")
    if "{junit}" not in command:
        raise SandboxUnavailable("isolated test command requires standalone {junit}")
    argv = ["/evidence/report.xml" if arg == "{junit}" else arg for arg in command]
    uid = os.getuid() or 65534
    gid = os.getgid() if os.getuid() else 65534
    proxy_args = []
    worker_args = []
    if model_proxy is not None:
        socket_path = model_proxy.socket_path
        if socket_path is None or not socket_path.is_socket() or "," in str(socket_path.parent):
            raise SandboxUnavailable("model test proxy is unavailable")
        proxy_args = ["--mount", f"type=bind,src={socket_path.parent},dst=/model-proxy,readonly"]
        # The archived default may use a different provider from --config.
        # Only route settings cross the test boundary; the key stays on the host.
        route = model_proxy.route.model_dump()
        route["key_env"] = ""
        worker_args = ["live-model", json.dumps(route)]
    return [
        "docker", "run", "--detach", "--init", "--name", name, "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "KILL",
        "--security-opt", "no-new-privileges", "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=268435456,mode=1777",
        "--tmpfs", "/evidence:rw,nosuid,noexec,size=16777216,mode=1777",
        "--tmpfs", "/control:rw,nosuid,noexec,size=65536,mode=0700",
        "--mount", f"type=bind,src={tree.resolve()},dst=/work,readonly",
        *proxy_args,
        "--workdir", "/work", "--user", "0:0", "--entrypoint", "/usr/local/bin/python3",
        image, "-I", WORKER, "run", str(uid), str(gid), str(timeout), json.dumps(argv),
        *worker_args,
    ]


def _docker(argv, timeout, *, binary=False):
    try:
        return subprocess.run(argv, timeout=timeout, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=not binary)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxUnavailable("container runtime unavailable or timed out") from exc


def validate_runtime(image: str, *, require_model_proxy=False):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image or ""):
        raise SandboxUnavailable("test image must be an immutable local image SHA-256")
    format_string = '{{.Id}} {{index .Config.Labels "org.fl4write.test-runtime"}}'
    expected = image + " 1"
    if require_model_proxy:
        format_string += ' {{index .Config.Labels "org.fl4write.model-proxy"}}'
        expected += " 2"
    inspected = _docker(["docker", "image", "inspect", "--format", format_string, image], 30)
    if inspected.returncode or inspected.stdout.strip() != expected:
        raise SandboxUnavailable("selected image is not the installed FL4WRITE test runtime")


def run_isolated(command: list[str], tree: Path, evidence: Path, timeout: int, image: str, *, model_proxy=None):
    name = "fl4write-test-" + uuid.uuid4().hex
    argv = container_command(command, tree, image, name, timeout, model_proxy=model_proxy)
    validate_runtime(image, require_model_proxy=model_proxy is not None)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    recovery = evidence.with_name(evidence.name + ".container.json")
    try:
        with recovery.open("x", encoding="utf-8") as stream:
            json.dump({"container": name, "image": image, "state": "cleanup_required"}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise SandboxUnavailable("container recovery record unavailable or prior cleanup unresolved") from exc
    try:
        started = _docker(argv, 30)
        if started.returncode:
            raise SandboxUnavailable("isolated test container could not start")
        finished = _docker(["docker", "exec", name, "python3", "-I", WORKER,
                            "wait", str(timeout + 10)], timeout + 20)
        if finished.returncode:
            raise SandboxUnavailable("isolated test supervisor did not complete")
        try:
            result = json.loads(finished.stdout)
        except (ValueError, TypeError) as exc:
            raise SandboxUnavailable("isolated supervisor returned invalid evidence") from exc
        if not isinstance(result, dict) or result.get("kind") != "completed" or type(result.get("returncode")) is not int:
            raise SandboxUnavailable("isolated test process was unavailable or timed out")
        # 2026-09-16 (adversarial Arch-1): the report arrives EMBEDDED in the
        # root-owned supervisor status whenever the runtime supports it —
        # bytes frozen after the test process group was killed. The separate
        # `report` exec remains only as a compatibility read for older
        # runtimes and re-reads the same hardened worker contract.
        embedded = result.get("report_b64")
        if isinstance(embedded, str) and embedded:
            import base64 as _b64
            try:
                report_bytes = _b64.b64decode(embedded, validate=True)
            except (ValueError, TypeError) as exc:
                raise SandboxUnavailable("isolated supervisor report is not valid base64") from exc
            if not report_bytes or len(report_bytes) > MAX_REPORT:
                raise SandboxUnavailable("isolated test report is missing or invalid")
        else:
            report = _docker(["docker", "exec", name, "python3", "-I", WORKER, "report"], 20, binary=True)
            if report.returncode or len(report.stdout) > MAX_REPORT:
                raise SandboxUnavailable("isolated test report is missing or invalid")
            report_bytes = report.stdout
        evidence.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=evidence.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(report_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.replace(evidence)
        finally:
            temporary.unlink(missing_ok=True)
        return subprocess.CompletedProcess(command, result["returncode"], "", "")
    finally:
        # This generated name belongs only to this invocation.
        original = sys.exception()
        try:
            removed = _docker(["docker", "rm", "--force", name], 30)
            if removed.returncode:
                raise SandboxUnavailable("container cleanup remains unresolved")
            recovery.unlink()
        except (SandboxUnavailable, OSError) as exc:
            reason = "container cleanup remains unresolved; recovery record retained"
            if original is not None:
                reason = str(original) + "; " + reason
            raise SandboxUnavailable(reason) from (original or exc)
