"""Real transport regressions for safe diagnostics and bounded socket admission."""
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fl4write.config import ModelRoute
from fl4write import model_proxy as mp


@pytest.fixture
def provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            mode = self.path.strip('/')
            status = int(mode) if mode.isdecimal() else 200
            body = {'choices': [{'message': {'content': '{"findings": []}'}}]}
            raw = json.dumps(body).encode()
            if mode == 'bad-json':
                raw = b'not JSON: PRIVATE_PROVIDER_SENTINEL'
            elif mode == 'scalar':
                raw = b'[]'
            elif mode == 'oversize':
                raw = b'x' * (mp.MAX_RESPONSE + 1)
            elif status != 200:
                raw = b'PRIVATE_PROVIDER_SENTINEL'
            self.send_response(status, 'PRIVATE_PROVIDER_SENTINEL')
            self.send_header('Retry-After', '7' if status == 429 else 'PRIVATE_PROVIDER_SENTINEL')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def setup(base, mode):
    route = ModelRoute(endpoint=base + '/' + mode, model='synthetic', key_env='',
                       max_tokens=10, temperature=0.2)
    payload = {'model': route.model, 'max_tokens': 10, 'temperature': 0.2,
               'messages': [{'role': 'system', 'content': 'Review.'},
                            {'role': 'user', 'content': 'synthetic input'}]}
    return route, payload, mp.ModelProxy(route, max_calls=1, max_output_tokens=10)


@pytest.mark.parametrize('status', [401, 429, 503])
def test_http_status_survives_real_transport_without_provider_text(provider, status):
    base, calls = provider
    route, payload, proxy = setup(base, str(status))
    with proxy:
        with pytest.raises(mp.ProxyError) as caught:
            mp.request(str(proxy.socket_path), route.endpoint, payload)
        error = caught.value
        assert getattr(error, 'code', None) == 'provider_http'
        assert error.http_status == status
        assert error.retry_after == (7 if status == 429 else None)
        assert 'PRIVATE_PROVIDER_SENTINEL' not in str(error) + repr(vars(error))
        assert proxy.snapshot() == {'calls': 1, 'reserved_output_tokens': 10,
                                    'completed': 0, 'failed': 1, 'active': 0}
    assert len(calls) == 1


@pytest.mark.parametrize('mode, code', [('bad-json', 'invalid_response'),
                                       ('scalar', 'invalid_response'),
                                       ('oversize', 'response_too_large')])
def test_invalid_provider_output_has_safe_category(provider, mode, code):
    base, _ = provider
    route, payload, proxy = setup(base, mode)
    with proxy:
        with pytest.raises(mp.ProxyError) as caught:
            mp.request(str(proxy.socket_path), route.endpoint, payload)
        assert getattr(caught.value, 'code', None) == code
        assert 'PRIVATE_PROVIDER_SENTINEL' not in str(caught.value)
        assert proxy.snapshot()['failed'] == 1


def test_incomplete_clients_cannot_exceed_host_limit_and_capacity_recovers(provider, monkeypatch):
    monkeypatch.setattr(mp, 'MAX_CONNECTIONS', 2, raising=False)
    base, calls = provider
    route, payload, proxy = setup(base, 'ok')
    peers = []
    try:
        with proxy:
            for _ in range(2):
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                peer.settimeout(0.5)
                peer.connect(str(proxy.socket_path))
                peer.sendall(b'\x00')
                peers.append(peer)
            deadline = time.monotonic() + 1
            while len(proxy.handlers) != 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peers.append(extra)
            extra.settimeout(0.5)
            extra.connect(str(proxy.socket_path))
            try:
                rejected = extra.recv(1) == b''
            except ConnectionResetError:
                rejected = True
            except TimeoutError:
                rejected = False
            assert rejected, 'excess incomplete client was admitted instead of closed'
            assert len(proxy.handlers) <= 2
            assert proxy.snapshot()['calls'] == 0 and not calls
            for peer in peers:
                peer.close()
            deadline = time.monotonic() + 1
            while proxy.handlers and time.monotonic() < deadline:
                time.sleep(0.01)
            assert mp.request(str(proxy.socket_path), route.endpoint, payload)['choices']
    finally:
        for peer in peers:
            peer.close()


def test_slow_frame_uses_total_deadline_not_per_byte_timeout(provider, monkeypatch):
    monkeypatch.setattr(mp, 'FRAME_TIMEOUT_S', 0.1, raising=False)
    base, calls = provider
    _, _, proxy = setup(base, 'ok')
    with proxy, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(0.5)
        peer.connect(str(proxy.socket_path))
        peer.sendall(struct.pack('!I', 1000))
        for _ in range(10):
            try:
                peer.sendall(b' ')
            except BrokenPipeError:
                break
            time.sleep(0.02)
        try:
            result = mp._receive(peer, mp.MAX_FRAME)
        except TimeoutError:
            result = None
        assert result and result.get('error', {}).get('code') == 'request_timeout'
        assert proxy.snapshot()['calls'] == 0 and not calls
