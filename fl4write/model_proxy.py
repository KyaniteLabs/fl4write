"""Bounded, single-route model transport for credential-free test containers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import tempfile
import threading
import urllib.request

MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 1024 * 1024


class ProxyError(RuntimeError):
    pass


def _read(stream, size):
    chunks = []
    while size:
        data = stream.recv(size)
        if not data:
            raise ProxyError("incomplete model transport frame")
        chunks.append(data)
        size -= len(data)
    return b"".join(chunks)


def _receive(stream, limit):
    size = struct.unpack("!I", _read(stream, 4))[0]
    if size > limit:
        raise ProxyError("model transport frame exceeds limit")
    return json.loads(_read(stream, size))


def _send(stream, value, limit):
    raw = json.dumps(value).encode()
    if len(raw) > limit:
        raise ProxyError("model transport response exceeds limit")
    stream.sendall(struct.pack("!I", len(raw)) + raw)


def request(socket_path: str, endpoint: str, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(190)
        stream.connect(socket_path)
        _send(stream, {"endpoint": endpoint, "payload": payload}, MAX_REQUEST)
        response = _receive(stream, MAX_RESPONSE)
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ProxyError("model proxy rejected request or provider unavailable")
    if not isinstance(response.get("data"), dict):
        raise ProxyError("model proxy returned invalid data")
    return response["data"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ModelProxy:
    """Host-owned context; only its Unix socket directory is mounted in tests.

    Reservations are not refunded on failed or abandoned requests. The server
    forwards only the configured model/endpoint/settings and never client headers.
    """

    def __init__(self, route, *, max_calls: int, max_output_tokens: int):
        if any(type(v) is not int or v <= 0 for v in (max_calls, max_output_tokens)):
            raise ProxyError("model proxy budgets must be positive integers")
        self.route = route.model_copy(deep=True)
        self.key = os.environ.get(route.key_env, "") if route.key_env else ""
        if route.key_env and not self.key:
            raise ProxyError("configured model credential unavailable")
        self.max_calls, self.max_output_tokens = max_calls, max_output_tokens
        self.calls = self.reserved_output_tokens = self.completed = self.failed = 0
        self.lock = threading.Lock()
        self.socket_path = None

    def _forward(self, payload):
        headers = {"Content-Type": "application/json", "User-Agent": "fl4write/model-test-proxy"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.route.endpoint, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(req, timeout=180) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ProxyError("provider response exceeds limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ProxyError("provider response is not an object")
        return value

    def _dispatch(self, value):
        if not isinstance(value, dict) or set(value) != {"endpoint", "payload"}:
            raise ProxyError("invalid model request")
        payload = value["payload"]
        expected = {"model": self.route.model, "temperature": self.route.temperature,
                    "max_tokens": self.route.max_tokens}
        if self.route.seed is not None:
            expected["seed"] = self.route.seed
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
            if (self.calls >= self.max_calls
                    or self.reserved_output_tokens + self.route.max_tokens > self.max_output_tokens):
                raise ProxyError("model test budget exhausted")
            self.calls += 1
            self.reserved_output_tokens += self.route.max_tokens
        try:
            result = self._forward(payload)
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
                self.request.settimeout(190)
                try:
                    value = _receive(self.request, MAX_REQUEST)
                    response = {"ok": True, "data": owner._dispatch(value)}
                    _send(self.request, response, MAX_RESPONSE)
                except Exception:
                    try:
                        _send(self.request, {"ok": False}, MAX_RESPONSE)
                    except OSError:
                        pass
        self.directory = tempfile.TemporaryDirectory(prefix="fl4write-model-proxy-", dir="/tmp")
        self.socket_path = Path(self.directory.name) / "model.sock"
        self.server = socketserver.UnixStreamServer(str(self.socket_path), Handler)
        Path(self.directory.name).chmod(0o711)
        self.socket_path.chmod(0o600)
        if os.getuid() == 0:
            os.chown(self.socket_path, 65534, 65534)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.directory.cleanup()
        self.key = ""
