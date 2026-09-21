"""Trusted container supervisor; repository tests run under a different uid."""
import base64
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

STATUS = Path("/control/status.json")
REPORT = "/evidence/report.xml"
MAX_REPORT = 16 * 1024 * 1024


def _read_report():
    """Snapshot the JUnit report through the hardened open contract.

    Returns base64 of at most MAX_REPORT bytes, or None when the report is
    absent, oversized, or not a regular file. Called by the trusted
    supervisor immediately after the test process group is killed, so the
    bytes the host seals as evidence are frozen before any survivor can
    act (2026-09-16, adversarial Arch-1)."""
    try:
        descriptor = os.open(REPORT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_REPORT + 1)
        if len(payload) > MAX_REPORT:
            return None
        return base64.b64encode(payload).decode("ascii")
    except OSError:
        return None
    finally:
        os.close(descriptor)


def main():
    mode = sys.argv[1]
    if mode == "wait":
        deadline = time.monotonic() + int(sys.argv[2])
        while not STATUS.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not STATUS.is_file():
            return 2
        sys.stdout.write(STATUS.read_text())
        return 0
    if mode == "report":
        embedded = _read_report()
        if embedded is None:
            return 2
        sys.stdout.buffer.write(base64.b64decode(embedded))
        return 0
    if mode != "run":
        return 2
    uid, gid, timeout = map(int, sys.argv[2:5])
    command = json.loads(sys.argv[5])
    if uid <= 0 or gid < 0 or not isinstance(command, list) or not command:
        return 2
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
           "PYTHONPATH": "/work", "PYTHONDONTWRITEBYTECODE": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    if len(sys.argv) > 6:
        if len(sys.argv) != 8 or sys.argv[6] != "live-model":
            return 2
        route = json.loads(sys.argv[7])
        if not isinstance(route, dict) or route.get("key_env") != "":
            return 2
        env.update(FL4WRITE_EVAL="1", FL4WRITE_LIVE_EVAL_PROXY_SOCKET="/model-proxy/model.sock",
                   FL4WRITE_EVAL_MODEL=json.dumps(route))
    result = {"kind": "deferred", "reason": "test process unavailable"}
    process = None
    try:
        process = subprocess.Popen(command, cwd="/work", env=env, user=uid, group=gid,
                                   extra_groups=[], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            result = {"kind": "completed", "returncode": process.wait(timeout=timeout)}
        except subprocess.TimeoutExpired:
            result = {"kind": "deferred", "reason": "test process timed out"}
        finally:
            # 2026-09-16 (adversarial Arch-1): wait() reaps only the direct
            # child — detached grandchildren sharing the process group used
            # to survive into the report readback window and could replace
            # /evidence/report.xml (mode 1777) with an all-green forgery.
            # Kill the whole group on EVERY exit path, then snapshot the
            # report bytes into the ROOT-OWNED /control status document so
            # the host never trusts evidence the test uid can still write.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
    except OSError:
        pass
    embedded = _read_report()
    if embedded is not None:
        result["report_b64"] = embedded
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(STATUS)
    # Keep the bounded tmpfs report available until the host collects it.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError):
        sys.exit(2)
