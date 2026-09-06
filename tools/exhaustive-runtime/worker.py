"""Trusted container supervisor; repository tests run under a different uid."""
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
        descriptor = os.open(REPORT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT:
                return 2
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                sys.stdout.buffer.write(stream.read(MAX_REPORT + 1))
        finally:
            os.close(descriptor)
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
    try:
        process = subprocess.Popen(command, cwd="/work", env=env, user=uid, group=gid,
                                   extra_groups=[], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            result = {"kind": "completed", "returncode": process.wait(timeout=timeout)}
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            result = {"kind": "deferred", "reason": "test process timed out"}
    except OSError:
        pass
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
