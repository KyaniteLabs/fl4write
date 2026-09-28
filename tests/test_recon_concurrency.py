"""Concurrent recon dispatch keeps serial semantics: byte-identical default,
exact budget ceilings under parallelism, identical failure surfacing."""
import json
import threading
import time

import pytest

from fl4write import exhaustive
from fl4write.config import RepoConfig
from test_exhaustive_fix import _config


def _request(tmp_path, *, lines=3, max_calls=4, tokens=None, concurrency=None,
             chunk_chars=2, name='request.json'):
    route = _config().model
    tree = tmp_path / 'tree'
    tree.mkdir(exist_ok=True)
    (tree / 'value.txt').write_text(''.join(f'{chr(97 + i)}\n' for i in range(lines)))
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{"ledger": []}')
    value = {
        'tree': str(tree), 'ledger': str(ledger), 'result': str(tmp_path / 'result.json'),
        'checkpoint': str(tmp_path / 'checkpoint.json'), 'binding': {'round': 1, 'head': 'a' * 40},
        'route': route.model_dump(), 'chunk_chars': chunk_chars,
        'max_calls': max_calls, 'max_tokens': tokens or max_calls * route.max_tokens,
    }
    if concurrency is not None:
        value['concurrency'] = concurrency
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return path


def _model(calls, *, fail_at=(), jitter=0.0):
    def call(*args):
        start = json.loads(args[1])['start_line']
        calls.append(start)
        if jitter:
            time.sleep(jitter * (start % 3))
        if start in fail_at:
            raise TimeoutError('fixture outage')
        return '{"findings": []}'
    return call


class _Overlap:
    """Probe how many model calls are in flight at once."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, *args):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        return '{"findings": []}'


def _checkpoint_value(tmp_path):
    return json.loads((tmp_path / 'checkpoint.json').read_bytes())['value']


# ---------------------------------------------------------------- (a) default = serial
def test_default_request_is_deterministic_serial_and_byte_identical(tmp_path):
    left, right = tmp_path / 'left', tmp_path / 'right'
    left.mkdir()
    right.mkdir()
    probe, calls = _Overlap(), []
    def model(*args):
        calls.append(json.loads(args[1])['start_line'])
        return probe(*args)
    assert exhaustive._worker(_request(left, lines=3, max_calls=4), model) == 0
    assert exhaustive._worker(_request(right, lines=3, max_calls=4), model) == 0
    assert calls == [1, 2, 3, 1, 2, 3]  # chunk order, both runs
    assert probe.max_active == 1  # the default dispatches strictly serially
    assert (right / 'checkpoint.json').read_bytes() == (left / 'checkpoint.json').read_bytes()
    assert (right / 'result.json').read_bytes() == (left / 'result.json').read_bytes()


def test_explicit_concurrency_one_keeps_serial_dispatch_and_result_bytes(tmp_path):
    plain, explicit = tmp_path / 'plain', tmp_path / 'explicit'
    plain.mkdir()
    explicit.mkdir()
    exhaustive._worker(_request(plain, lines=3, max_calls=4), _model([]))
    probe = _Overlap()
    assert exhaustive._worker(_request(explicit, lines=3, max_calls=4, concurrency=1), probe) == 0
    assert probe.max_active == 1
    assert (explicit / 'result.json').read_bytes() == (plain / 'result.json').read_bytes()


def test_recon_payload_carries_concurrency_only_when_parallel(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / 'value.txt').write_text('a\nb\nc\n')
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{"ledger": []}')
    route = _config().model
    fakes = tmp_path / 'fakes.json'
    fakes.write_text(json.dumps([{'findings': []}] * 3))
    results = {}
    for concurrency in (1, 4):
        art = tmp_path / f'art-{concurrency}'
        art.mkdir()
        exhaustive._recon(tree, ledger, route, 4, 4 * route.max_tokens, 60, art, 2,
                          fake_responses=fakes, concurrency=concurrency)
        results[concurrency] = art
    serial = json.loads((results[1] / 'worker-request.json').read_bytes())
    assert 'concurrency' not in serial  # byte-identical pre-knob request format
    assert set(serial) == {'route', 'tree', 'ledger', 'max_calls', 'max_tokens',
                           'chunk_chars', 'result', 'fake_responses'}
    parallel = json.loads((results[4] / 'worker-request.json').read_bytes())
    assert parallel['concurrency'] == 4
    # same fixture corpus through the concurrent path yields the serial bytes
    assert (results[4] / 'worker-result.json').read_bytes() \
        == (results[1] / 'worker-result.json').read_bytes()


def test_concurrency_change_rejects_prior_checkpoint(tmp_path):
    request = _request(tmp_path, lines=3, max_calls=4)
    with pytest.raises(exhaustive.Deferred, match='fixture outage'):
        exhaustive._worker(request, _model([], fail_at={2}))
    assert len(_checkpoint_value(tmp_path)['entries']) == 1
    value = json.loads(request.read_bytes())
    value['concurrency'] = 3
    request.write_text(json.dumps(value))
    with pytest.raises(exhaustive.Deferred, match='identity changed'):
        exhaustive._worker(request, lambda *_: pytest.fail('inference on changed identity'))


# ---------------------------------------------------------------- (b) exact ceilings
@pytest.mark.parametrize('concurrency', [None, 3, 5])
def test_call_ceiling_is_exact_under_concurrency(tmp_path, concurrency):
    route = _config().model
    request = _request(tmp_path, lines=6, max_calls=4, tokens=6 * route.max_tokens,
                       concurrency=concurrency)
    calls = []
    with pytest.raises(exhaustive.Deferred, match='budget exhausted before full coverage'):
        exhaustive._worker(request, _model(calls))
    assert sorted(calls) == [1, 2, 3, 4]  # exactly the admissible count dispatched
    value = _checkpoint_value(tmp_path)
    assert value['attempts'] == 4  # one charge per dispatched call, never more
    assert len(value['entries']) == 4  # accepted strictly in coverage order
    assert not (tmp_path / 'result.json').exists()


@pytest.mark.parametrize('concurrency', [None, 4])
def test_token_ceiling_is_exact_under_concurrency(tmp_path, concurrency):
    route = _config().model
    request = _request(tmp_path, lines=5, max_calls=10, tokens=2 * route.max_tokens,
                       concurrency=concurrency)
    calls = []
    with pytest.raises(exhaustive.Deferred, match='budget exhausted before full coverage'):
        exhaustive._worker(request, _model(calls))
    assert sorted(calls) == [1, 2]
    value = _checkpoint_value(tmp_path)
    assert value['attempts'] == 2
    assert value['attempts'] * route.max_tokens <= 2 * route.max_tokens
    assert len(value['entries']) == 2


def test_jittered_parallel_hammer_never_exceeds_either_ceiling(tmp_path):
    route = _config().model
    request = _request(tmp_path, lines=12, max_calls=5, tokens=12 * route.max_tokens,
                       concurrency=8)
    calls = []
    with pytest.raises(exhaustive.Deferred, match='budget exhausted before full coverage'):
        exhaustive._worker(request, _model(calls, jitter=0.02))
    assert sorted(calls) == [1, 2, 3, 4, 5]  # eight workers, still five dispatches
    value = _checkpoint_value(tmp_path)
    assert value['attempts'] == 5 and len(value['entries']) == 5


# ---------------------------------------------------------------- (c) failure semantics
@pytest.mark.parametrize('concurrency', [None, 3])
def test_failing_call_surfaces_serial_error_semantics(tmp_path, concurrency):
    route = _config().model
    request = _request(tmp_path, lines=3, max_calls=6, tokens=6 * route.max_tokens,
                       concurrency=concurrency)
    calls = []
    with pytest.raises(exhaustive.Deferred) as exc:
        exhaustive._worker(request, _model(calls, fail_at={2}))
    assert 'model unavailable or returned unusable output: fixture outage' in str(exc.value)
    value = _checkpoint_value(tmp_path)
    if concurrency is None:
        assert calls == [1, 2]
        assert value['attempts'] == 2  # serial charges only the calls it reached
    else:
        assert sorted(calls) == [1, 2, 3]  # the window was already in flight
        assert value['attempts'] == 3  # every dispatched call charged exactly once
    assert len(value['entries']) == 1  # ordered accept stops at the failed chunk


def test_concurrent_failure_resumes_from_ordered_checkpoint(tmp_path):
    route = _config().model
    request = _request(tmp_path, lines=3, max_calls=6, tokens=6 * route.max_tokens,
                       concurrency=3)
    with pytest.raises(exhaustive.Deferred, match='fixture outage'):
        exhaustive._worker(request, _model([], fail_at={2}))
    calls = []
    assert exhaustive._worker(request, _model(calls)) == 0
    assert sorted(calls) == [2, 3]  # the accepted chunk replays without a call
    result = json.loads((tmp_path / 'result.json').read_bytes())
    assert result['calls'] == 5  # three from the failed window, two on resume
    assert result['reserved_output_tokens'] == 5 * route.max_tokens
    assert [row['start_line'] for row in result['coverage']] == [1, 2, 3]


def test_concurrent_pass_dispatches_chunks_in_parallel(tmp_path):
    request = _request(tmp_path, lines=3, max_calls=4, concurrency=3)
    barrier = threading.Barrier(3, timeout=15)
    def model(*args):
        barrier.wait()  # only passes when all three calls are in flight at once
        return '{"findings": []}'
    assert exhaustive._worker(request, model) == 0
    assert json.loads((tmp_path / 'result.json').read_bytes())['calls'] == 3


# ---------------------------------------------------------------- config knob
def _raw_config():
    return {
        'repo': 'acme/widget',
        'forges': {'origin': {'role': 'primary',
                              'api_base': 'https://api.github.com', 'token_env': 'TEST_FORGE_TOKEN'}},
        'model': {'endpoint': 'https://model.invalid/v1', 'model': 'test'},
    }


def test_concurrency_defaults_to_one():
    assert RepoConfig.model_validate(_raw_config()).concurrency == 1


@pytest.mark.parametrize('value', [1, 4, 8])
def test_concurrency_accepts_bounded_integers(value):
    assert RepoConfig.model_validate({**_raw_config(), 'concurrency': value}).concurrency == value


@pytest.mark.parametrize('value', [0, 9, True, '2', 2.5])
def test_concurrency_refuses_out_of_range_and_non_integers(value):
    with pytest.raises(Exception):
        RepoConfig.model_validate({**_raw_config(), 'concurrency': value})
