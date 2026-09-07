"""Bounded, single-route model transport for credential-free test containers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_FRAME = MAX_RESPONSE + 64
MAX_CONNECTIONS = 8
FRAME_TIMEOUT_S = 5
_ERROR_MESSAGES = {
    "proxy_error": "model proxy rejected request or provider unavailable",
    "provider_http": "provider returned an HTTP error",
    "provider_timeout": "provider deadline exceeded",
    "transport_error": "provider connection failed",
    "invalid_response": "provider returned an invalid response",
    "response_too_large": "provider response exceeds limit",
    "request_timeout": "model request frame deadline exceeded",
}


class ProxyError(RuntimeError):
    def __init__(self, message=None, *, code="proxy_error", http_status=None, retry_after=None):
        self.code = code if isinstance(code, str) and code in _ERROR_MESSAGES else "proxy_error"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.retry_after = retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        description = message if message is not None else _ERROR_MESSAGES[self.code]
        if self.http_status is not None:
            description += f" (HTTP {self.http_status})"
        super().__init__(description)


def _error_response(exc):
    # Only allowlisted metadata crosses process/socket boundaries, never exception text.
    error = exc if isinstance(exc, ProxyError) else ProxyError()
    return {"ok": False, "error": {"code": error.code, "http_status": error.http_status,
                                   "retry_after": error.retry_after}}


def _unwrap(response):
    if not isinstance(response, dict) or response.get("ok") is not True:
        value = response.get("error", {}) if isinstance(response, dict) else {}
        value = value if isinstance(value, dict) else {}
        raise ProxyError(code=value.get("code"), http_status=value.get("http_status"),
                         retry_after=value.get("retry_after"))
    if not isinstance(response.get("data"), dict):
        raise ProxyError(code="invalid_response")
    return response["data"]


def _read(stream, size, deadline=None):
    chunks = []
    while size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProxyError(code="request_timeout")
            stream.settimeout(remaining)
        try:
            data = stream.recv(size)
        except TimeoutError as exc:
            if deadline is not None:
                raise ProxyError(code="request_timeout") from exc
            raise
        if not data:
            raise ProxyError("incomplete model transport frame")
        chunks.append(data)
        size -= len(data)
    return b"".join(chunks)


def _receive(stream, limit, deadline=None):
    size = struct.unpack("!I", _read(stream, 4, deadline))[0]
    if size > limit:
        raise ProxyError("model transport frame exceeds limit")
    return json.loads(_read(stream, size, deadline))


def _encode(value, limit):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > limit:
        raise ProxyError("model transport response exceeds limit")
    return raw


def _send(stream, value, limit):
    raw = _encode(value, limit)
    stream.sendall(struct.pack("!I", len(raw)) + raw)


def request(socket_path: str, endpoint: str, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(190)
        stream.connect(socket_path)
        _send(stream, {"endpoint": endpoint, "payload": payload}, MAX_REQUEST)
        response = _receive(stream, MAX_FRAME)
    return _unwrap(response)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _provider(value):
    """Run only in the owned subprocess so timeout/cleanup can stop HTTP I/O."""
    headers = {"Content-Type": "application/json", "User-Agent": "fl4write/model-test-proxy"}
    if value["key"]:
        headers["Authorization"] = "Bearer " + value["key"]
    req = urllib.request.Request(value["endpoint"], data=json.dumps(value["payload"]).encode(),
                                 headers=headers, method="POST")
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=180) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ProxyError(code="response_too_large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProxyError(code="invalid_response") from exc
    if not isinstance(data, dict):
        raise ProxyError(code="invalid_response")
    return _encode(data, MAX_RESPONSE)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        self.admissions = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.admissions.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.admissions.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.admissions.release()


class ModelProxy:
    """Host-owned context; only its Unix socket directory is mounted in tests.

    Reservations are not refunded on failed or abandoned requests. The server
    forwards only the configured model/endpoint/settings and never client headers.
    """

    def __init__(self, route, *, max_calls: int, max_output_tokens: int, reserve=None):
        if any(type(v) is not int or v <= 0 for v in (max_calls, max_output_tokens)):
            raise ProxyError("model proxy budgets must be positive integers")
        self.route = route.model_copy(deep=True)
        self.key = os.environ.get(route.key_env, "") if route.key_env else ""
        if route.key_env and not self.key:
            raise ProxyError("configured model credential unavailable")
        self.max_calls, self.max_output_tokens = max_calls, max_output_tokens
        self.reserve = reserve
        self.calls = self.reserved_output_tokens = self.completed = self.failed = 0
        self.lock = threading.Lock()
        self.socket_path = None
        self.closed = False
        self.children = set()
        self.streams = set()
        self.handlers = set()

    def _forward(self, payload):
        with self.lock:
            if self.closed:
                raise ProxyError("model proxy closed")
            body = json.dumps({"endpoint": self.route.endpoint, "key": self.key, "payload": payload}).encode()
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--provider"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env={k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ})
            self.children.add(child)
        try:
            try:
                raw, _ = child.communicate(body, timeout=180)
            except subprocess.TimeoutExpired as exc:
                child.kill()
                child.communicate()
                raise ProxyError(code="provider_timeout") from exc
            if child.returncode or len(raw) > MAX_FRAME:
                raise ProxyError(code="transport_error")
            try:
                response = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ProxyError(code="invalid_response") from exc
            return _unwrap(response)
        finally:
            with self.lock:
                self.children.discard(child)

    def _dispatch(self, value):
        if not isinstance(value, dict) or set(value) != {"endpoint", "payload"}:
            raise ProxyError("invalid model request")
        payload = value["payload"]
        expected = {"model": self.route.model, "temperature": self.route.temperature,
                    "max_tokens": self.route.max_tokens}
        if self.route.seed is not None:
            expected["seed"] = self.route.seed
        if self.route.thinking is not None:
            expected["thinking"] = {"type": self.route.thinking}
        if (value["endpoint"] != self.route.endpoint or not isinstance(payload, dict)
                or set(payload) != set(expected) | {"messages"}
                or any(payload[k] != v or type(payload[k]) is not type(v) for k, v in expected.items())):
            raise ProxyError("model request differs from selected route")
        messages = payload["messages"]
        if (not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                       or m["role"] != role or not isinstance(m["content"], str)
                       for m, role in zip(messages, ("system", "user")))):
            raise ProxyError("invalid model messages")
        with self.lock:
            if (self.closed or self.calls >= self.max_calls
                    or self.reserved_output_tokens + self.route.max_tokens > self.max_output_tokens):
                raise ProxyError("model test budget exhausted")
            if self.reserve is not None:
                self.reserve(self.route.max_tokens)
            self.calls += 1
            self.reserved_output_tokens += self.route.max_tokens
        try:
            result = self._forward(payload)
            _encode(result, MAX_RESPONSE)
        except Exception:
            with self.lock:
                self.failed += 1
            raise
        with self.lock:
            self.completed += 1
        return result

    def snapshot(self):
        with self.lock:
            return {"calls": self.calls, "reserved_output_tokens": self.reserved_output_tokens,
                    "completed": self.completed, "failed": self.failed,
                    "active": self.calls - self.completed - self.failed}

    def __enter__(self):
        owner = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                with owner.lock:
                    if owner.closed:
                        return
                    owner.streams.add(self.request)
                    owner.handlers.add(threading.current_thread())
                try:
                    value = _receive(self.request, MAX_REQUEST, time.monotonic() + FRAME_TIMEOUT_S)
                    response = {"ok": True, "data": owner._dispatch(value)}
                    self.request.settimeout(FRAME_TIMEOUT_S)
                    _send(self.request, response, MAX_FRAME)
                except Exception as exc:
                    try:
                        self.request.settimeout(FRAME_TIMEOUT_S)
                        _send(self.request, _error_response(exc), MAX_FRAME)
                    except OSError:
                        pass
                finally:
                    with owner.lock:
                        owner.streams.discard(self.request)
                        owner.handlers.discard(threading.current_thread())
        self.directory = tempfile.TemporaryDirectory(prefix="fl4write-model-proxy-", dir="/tmp")
        self.socket_path = Path(self.directory.name) / "model.sock"
        self.server = _Server(str(self.socket_path), Handler)
        Path(self.directory.name).chmod(0o711)
        self.socket_path.chmod(0o600)
        if os.getuid() == 0:
            os.chown(self.socket_path, 65534, 65534)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        with self.lock:
            self.closed = True
            children, streams, handlers = list(self.children), list(self.streams), list(self.handlers)
            self.key = ""
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        for stream in streams:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.directory.cleanup()
        for handler in handlers:
            handler.join(timeout=1)
        if any(handler.is_alive() for handler in handlers):
            raise ProxyError("model proxy handler cleanup incomplete")


if __name__ == "__main__" and sys.argv[1:] == ["--provider"]:
    try:
        result = {"ok": True, "data": json.loads(_provider(json.load(sys.stdin)))}
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("Retry-After", "") if exc.headers else ""
        retry = int(retry) if retry.isascii() and retry.isdecimal() and len(retry) <= 5 else None
        result = _error_response(ProxyError(code="provider_http", http_status=exc.code, retry_after=retry))
    except TimeoutError:
        result = _error_response(ProxyError(code="provider_timeout"))
    except urllib.error.URLError as exc:
        code = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "transport_error"
        result = _error_response(ProxyError(code=code))
    except Exception as exc:
        result = _error_response(exc)
    sys.stdout.buffer.write(_encode(result, MAX_FRAME))
