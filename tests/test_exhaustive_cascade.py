"""Cascade recon (lever 2): an optional cheap screen pass routes only suspect chunks deep.

Proves with fakes: (a) no screen_model configured -> the single-model dispatch is
unchanged, (b) screen verdicts route only flagged chunks to the primary model,
(c) deep-pass findings carry the same evidence shape as the single-model path
(the ledger cannot tell them apart) — plus budget, resume, config and proxy laws.
"""
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from fl4write import exhaustive
from fl4write.config import ModelRoute, RepoConfig, assert_credential_endpoints_trusted
from fl4write.exhaustive_recon import ReconProgress
from fl4write.model_proxy import ModelProxy, ProxyError, request
from test_exhaustive_fix import _config

_EVIDENCE = {1: 'alpha', 2: 'beta', 3: 'gamma'}


def _screen_route() -> ModelRoute:
    return _config().model.model_copy(update={"model": "cheap-screen", "endpoint": "https://screen.invalid/v1"})


def _fixture(tmp_path: Path, *, screen: bool = False, max_calls: int = 8, max_tokens: int | None = None):
    tree = tmp_path / 'tree'
    tree.mkdir(parents=True)
    (tree / 'value.txt').write_text('alpha\nbeta\ngamma\n')
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{"ledger": []}')
    route = _config().model
    payload = {
        'tree': str(tree), 'ledger': str(ledger), 'result': str(tmp_path / 'result.json'),
        'checkpoint': str(tmp_path / 'checkpoint.json'), 'binding': {'round': 1, 'head': 'a' * 40},
        'route': route.model_dump(), 'chunk_chars': 6,
        'max_calls': max_calls, 'max_tokens': max_tokens or max_calls * route.max_tokens,
    }
    if screen:
        payload['screen_route'] = _screen_route().model_dump()
    req = tmp_path / 'request.json'
    req.write_text(json.dumps(payload))
    return req


def _finding(start: int) -> dict:
    return {'findings': [{'path': 'value.txt', 'line': start, 'evidence': _EVIDENCE[start],
                          'severity': 'Major', 'message': f'fixture finding at line {start}'}]}


def _cascade_model(flags, deeps, calls=None):
    """Fake dispatcher: the screen system prompt gets a flag verdict, the recon
    system prompt gets grounded findings — both keyed by the chunk's start line."""
    def model(route, prompt, mode, system):
        start = json.loads(prompt)['start_line']
        if calls is not None:
            calls.append((route.model, system, start))
        if system == exhaustive._SCREEN_SYSTEM:
            return json.dumps({'flag': bool(flags.get(start, False))})
        return json.dumps(deeps.get(start, {'findings': []}))
    return model


def test_screen_model_defaults_to_none():
    assert _config().screen_model is None


def test_unset_screen_model_keeps_single_model_dispatch(tmp_path):
    req = _fixture(tmp_path, screen=False)
    calls = []
    model = _cascade_model({}, {s: _finding(s) for s in _EVIDENCE}, calls)
    assert exhaustive._worker(req, model) == 0
    assert calls == [('test', exhaustive._RECON_SYSTEM, s) for s in (1, 2, 3)]
    result = json.loads((tmp_path / 'result.json').read_bytes())
    assert result['calls'] == 3
    assert [f['line'] for f in result['findings']] == [1, 2, 3]
    assert result['reserved_output_tokens'] == 3 * _config().model.max_tokens
    # every chunk is checkpoint-cached: a completed round never re-infers
    assert exhaustive._worker(req, lambda *_: pytest.fail('completed chunk repeated')) == 0


def test_screen_flags_route_only_flagged_chunks_deep(tmp_path):
    req = _fixture(tmp_path, screen=True)
    calls = []
    model = _cascade_model({2: True}, {2: _finding(2)}, calls)
    assert exhaustive._worker(req, model) == 0
    screens = [c for c in calls if c[1] == exhaustive._SCREEN_SYSTEM]
    deeps = [c for c in calls if c[1] == exhaustive._RECON_SYSTEM]
    assert [c[2] for c in screens] == [1, 2, 3]  # every chunk is screened on the cheap route
    assert all(c[0] == 'cheap-screen' for c in screens)
    assert [(c[0], c[2]) for c in deeps] == [('test', 2)]  # only the flagged chunk goes deep
    result = json.loads((tmp_path / 'result.json').read_bytes())
    assert result['calls'] == 4  # 3 screens + 1 deep: screening never rides free
    assert [f['line'] for f in result['findings']] == [2]  # findings come only from the deep pass
    assert [row['start_line'] for row in result['coverage']] == [1, 2, 3]  # full coverage retained


def test_cascade_deep_findings_match_single_model_evidence_shape(tmp_path):
    deeps = {s: _finding(s) for s in _EVIDENCE}
    single = _fixture(tmp_path / 'single', screen=False)
    exhaustive._worker(single, _cascade_model({}, deeps))
    cascade = _fixture(tmp_path / 'cascade', screen=True)
    exhaustive._worker(cascade, _cascade_model({s: True for s in _EVIDENCE}, deeps))
    single_result = json.loads((tmp_path / 'single/result.json').read_bytes())
    cascade_result = json.loads((tmp_path / 'cascade/result.json').read_bytes())
    # same findings, same coverage: the ledger cannot tell a cascade round apart
    assert cascade_result['findings'] == single_result['findings']
    assert cascade_result['coverage'] == single_result['coverage']


def test_screen_calls_consume_the_shared_call_budget(tmp_path):
    req = _fixture(tmp_path, screen=True, max_calls=3)
    model = _cascade_model({s: True for s in _EVIDENCE}, {1: _finding(1)})
    with pytest.raises(exhaustive.Deferred, match='budget exhausted'):
        exhaustive._worker(req, model)
    assert not (tmp_path / 'result.json').exists()
    value = json.loads((tmp_path / 'checkpoint.json').read_bytes())['value']
    # screen(1) + deep(1) + screen(2) charged; the second deep pass is refused
    assert value['attempts'] == 3


def test_attempt_reservation_uses_the_largest_route_window(tmp_path):
    req = _fixture(tmp_path, screen=True)
    payload = json.loads(req.read_bytes())
    payload['screen_route']['max_tokens'] = 9000
    req.write_text(json.dumps(payload))
    progress = ReconProgress(payload, list(exhaustive._text_sources(tmp_path / 'tree')), {"ledger": []})
    assert progress.attempt_tokens == 9000
    plain = _fixture(tmp_path / 'plain', screen=False)
    payload = json.loads(plain.read_bytes())
    progress = ReconProgress(payload, list(exhaustive._text_sources(tmp_path / 'plain/tree')), {"ledger": []})
    assert progress.attempt_tokens == _config().model.max_tokens


def test_cascade_resume_replays_only_incomplete_chunks(tmp_path):
    req = _fixture(tmp_path, screen=True)
    calls = []

    def interrupted(route, prompt, mode, system):
        start = json.loads(prompt)['start_line']
        calls.append((system == exhaustive._SCREEN_SYSTEM, start))
        if len(calls) == 3:  # the screen of chunk 2 dies mid-round
            raise TimeoutError('fixture outage')
        if system == exhaustive._SCREEN_SYSTEM:
            return json.dumps({'flag': start != 3})
        return json.dumps(_finding(start))

    with pytest.raises(exhaustive.Deferred, match='fixture outage'):
        exhaustive._worker(req, interrupted)
    assert not (tmp_path / 'result.json').exists()
    resumed = []
    model = _cascade_model({1: True, 2: True}, {1: _finding(1), 2: _finding(2)})
    original = model

    def recorder(route, prompt, mode, system):
        start = json.loads(prompt)['start_line']
        resumed.append((system == exhaustive._SCREEN_SYSTEM, start))
        return original(route, prompt, mode, system)

    assert exhaustive._worker(req, recorder) == 0
    assert resumed == [(True, 2), (False, 2), (True, 3)]  # chunk 1 replays from the checkpoint
    result = json.loads((tmp_path / 'result.json').read_bytes())
    assert result['calls'] == 6  # 3 charged before the outage + 3 after it
    assert [f['line'] for f in result['findings']] == [1, 2]


@pytest.mark.parametrize('verdict', ['{"flag": "maybe"}', '{"flagged": true}', 'not json', '{"flag": 1}'])
def test_malformed_screen_verdict_fails_closed(tmp_path, verdict):
    req = _fixture(tmp_path, screen=True)

    def model(route, prompt, mode, system):
        assert system == exhaustive._SCREEN_SYSTEM, 'the deep pass must not run unflagged'
        return verdict

    with pytest.raises(exhaustive.Deferred, match='unusable output'):
        exhaustive._worker(req, model)
    assert not (tmp_path / 'result.json').exists()


def test_screen_route_is_part_of_the_checkpoint_identity(tmp_path):
    req = _fixture(tmp_path, screen=True)

    def interrupted(route, prompt, mode, system):
        if json.loads(prompt)['start_line'] == 2 and system == exhaustive._SCREEN_SYSTEM:
            raise TimeoutError('fixture outage')
        return json.dumps({'flag': False})

    with pytest.raises(exhaustive.Deferred, match='fixture outage'):
        exhaustive._worker(req, interrupted)
    payload = json.loads(req.read_bytes())
    del payload['screen_route']  # screening switched off mid-round
    req.write_text(json.dumps(payload))
    with pytest.raises(exhaustive.Deferred, match='identity changed'):
        exhaustive._worker(req, lambda *_: pytest.fail('changed identity dispatched'))


def test_recon_hands_the_screen_route_to_the_worker(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir(parents=True)
    (tree / 'value.txt').write_text('alpha\nbeta\ngamma\n')
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{"ledger": []}')
    responses = tmp_path / 'responses.json'
    responses.write_text(json.dumps([{'flag': False}] * 3))
    config = _config()
    config.screen_model = _screen_route()
    findings, _, usage = exhaustive._recon(
        tree, ledger, config.model, 8, 8 * config.model.max_tokens, 60, tmp_path / 'evidence', 6,
        responses, screen_route=config.screen_model,
    )
    payload = json.loads((tmp_path / 'evidence/worker-request.json').read_bytes())
    assert payload['screen_route'] == config.screen_model.model_dump()
    assert findings == [] and usage['calls'] == 3  # screens only; nothing was flagged
    responses.write_text(json.dumps([{'findings': []}] * 3))
    exhaustive._recon(tree, ledger, _config().model, 8, 8 * config.model.max_tokens, 60,
                      tmp_path / 'evidence2', 6, responses)
    payload = json.loads((tmp_path / 'evidence2/worker-request.json').read_bytes())
    assert 'screen_route' not in payload


def _raw_config() -> dict:
    return {
        "repo": "acme/widget",
        "forges": {"origin": {"role": "primary", "api_base": "https://api.github.com",
                              "token_env": "TEST_FORGE_TOKEN"}},
        "model": {"endpoint": "https://model.invalid/v1", "model": "test"},
    }


def test_screen_model_is_optional_and_parseable():
    assert RepoConfig.model_validate(_raw_config()).screen_model is None
    config = RepoConfig.model_validate({**_raw_config(),
                                        "screen_model": {"endpoint": "https://screen.invalid/v1",
                                                         "model": "cheap"}})
    assert config.screen_model.model == "cheap"


def test_identical_screen_model_warns_but_parses(caplog):
    with caplog.at_level(logging.WARNING, logger="fl4write.config"):
        config = RepoConfig.model_validate({**_raw_config(),
                                            "screen_model": {"endpoint": "https://model.invalid/v1",
                                                             "model": "test"}})
    assert config.screen_model is not None
    assert any("saves no calls" in record.message for record in caplog.records)


def test_screen_key_env_colliding_with_forge_token_is_refused():
    raw = {**_raw_config(), "screen_model": {"endpoint": "https://screen.invalid/v1", "model": "cheap",
                                             "key_env": "TEST_FORGE_TOKEN"}}
    with pytest.raises(ValidationError, match="collides"):
        RepoConfig.model_validate(raw)


def test_screen_key_env_cannot_use_reserved_forge_names():
    raw = {**_raw_config(), "screen_model": {"endpoint": "https://screen.invalid/v1", "model": "cheap",
                                             "key_env": "GH_TOKEN"}}
    with pytest.raises(ValidationError, match="reserved"):
        RepoConfig.model_validate(raw)


def test_repo_supplied_screen_route_cannot_aim_credentials_at_untrusted_endpoints(monkeypatch):
    monkeypatch.delenv("FL4WRITE_TRUSTED_MODEL_ENDPOINTS", raising=False)
    config = RepoConfig.model_validate({**_raw_config(),
                                        "screen_model": {"endpoint": "https://collector.invalid/v1",
                                                         "model": "cheap", "key_env": "HOST_SECRET"}})
    with pytest.raises(ValueError, match="screen_model"):
        assert_credential_endpoints_trusted(config, source="fixture/.fl4write.yaml")


def _proxy_payload(route: ModelRoute, prompt: str) -> dict:
    return {"model": route.model, "temperature": route.temperature, "max_tokens": route.max_tokens,
            "messages": [{"role": "system", "content": exhaustive._SCREEN_SYSTEM},
                         {"role": "user", "content": prompt}]}


def _proxy_answer(payload, endpoint, key):
    return {"choices": [{"message": {"content": '{"flag": false}'}, "finish_reason": "stop"}]}


def test_proxy_serves_the_screen_route_under_the_shared_budget(monkeypatch):
    primary = ModelRoute(endpoint="https://model.invalid/v1", model="test", max_tokens=10)
    screen = ModelRoute(endpoint="https://screen.invalid/v1", model="cheap-screen", max_tokens=5)
    seen = []

    def forward(payload, endpoint, key):
        seen.append(endpoint)
        return _proxy_answer(payload, endpoint, key)

    with ModelProxy(primary, max_calls=2, max_output_tokens=20, screen_route=screen) as proxy:
        monkeypatch.setattr(proxy, "_forward", forward)
        assert request(str(proxy.socket_path), screen.endpoint, _proxy_payload(screen, "triage"))["choices"]
        assert request(str(proxy.socket_path), primary.endpoint, _proxy_payload(primary, "audit"))["choices"]
        assert seen == [screen.endpoint, primary.endpoint]  # each call leaves via its own route
        with pytest.raises(ProxyError, match="budget exhausted"):
            proxy._dispatch({"endpoint": screen.endpoint,
                             "payload": _proxy_payload(screen, "more")})
        assert proxy.snapshot() == {"calls": 2, "reserved_output_tokens": 20,
                                    "completed": 2, "failed": 0, "active": 0}


def test_proxy_charges_every_route_the_largest_configured_window(monkeypatch):
    primary = ModelRoute(endpoint="https://model.invalid/v1", model="test", max_tokens=10)
    screen = ModelRoute(endpoint="https://screen.invalid/v1", model="cheap-screen", max_tokens=5)
    with ModelProxy(primary, max_calls=5, max_output_tokens=15, screen_route=screen) as proxy:
        monkeypatch.setattr(proxy, "_forward", _proxy_answer)
        assert request(str(proxy.socket_path), screen.endpoint, _proxy_payload(screen, "a"))["choices"]
        assert proxy.snapshot()["reserved_output_tokens"] == 10  # the cheap route does not ride free
        with pytest.raises(ProxyError):
            request(str(proxy.socket_path), screen.endpoint, _proxy_payload(screen, "b"))


def test_proxy_rejects_routes_it_was_not_configured_with():
    primary = ModelRoute(endpoint="https://model.invalid/v1", model="test", max_tokens=10)
    screen = ModelRoute(endpoint="https://screen.invalid/v1", model="cheap-screen", max_tokens=5)
    stranger = ModelRoute(endpoint="https://stranger.invalid/v1", model="other", max_tokens=5)
    with ModelProxy(primary, max_calls=1, max_output_tokens=10, screen_route=screen) as proxy:
        with pytest.raises(ProxyError, match="differs from selected route"):
            proxy._dispatch({"endpoint": stranger.endpoint, "payload": _proxy_payload(stranger, "x")})
        assert proxy.snapshot()["calls"] == 0  # refused before any reservation


def test_proxy_refuses_unavailable_screen_credentials(monkeypatch):
    primary = ModelRoute(endpoint="https://model.invalid/v1", model="test", max_tokens=10)
    screen = ModelRoute(endpoint="https://screen.invalid/v1", model="cheap",
                        key_env="MISSING_SCREEN_KEY")
    monkeypatch.delenv("MISSING_SCREEN_KEY", raising=False)
    with pytest.raises(ProxyError, match="credential unavailable"):
        ModelProxy(primary, max_calls=1, max_output_tokens=10, screen_route=screen)


def test_fit_loop_validates_each_leg_with_its_own_system(monkeypatch):
    """Review 2026-09-27: the fit loop validated BOTH legs with _RECON_SYSTEM,
    but each leg dispatches with its own system prompt — a gap window at
    MAX_REQUEST let a chunk pass fit() yet bust its real dispatch client-side
    (deterministic deferral on every retry). Pinned two ways, agnostic to
    which system prompt is the longer one on this tree: (a) mechanically, the
    fit loop must encode each endpoint with exactly its own system prompt;
    (b) every emitted chunk must fit BOTH legs' real envelopes."""
    from fl4write import model_proxy
    from fl4write.analyzer import _model_payload
    from fl4write.model_proxy import MAX_REQUEST, _encode

    route = _config().model
    screen = _screen_route()
    ledger = {'ledger': []}

    def fits(target, system, prompt):
        try:
            _encode({'endpoint': target.endpoint,
                     'payload': _model_payload(target, prompt, 'file', system)}, MAX_REQUEST)
            return True
        except ProxyError:
            return False

    # (a) mechanical pairing: spy on the fit loop's transport encodes. The
    # screen endpoint validated with _RECON_SYSTEM (the old bug) shows up
    # here as a cross-pairing no matter which prompt is longer.
    seen, real_encode = [], _encode

    def spy(value, limit):
        seen.append((value['endpoint'], value['payload']['messages'][0]['content']))
        return real_encode(value, limit)

    monkeypatch.setattr(model_proxy, '_encode', spy)
    source = ('y' * 50 + '\n') * 100  # many short lines: several chunks, none un-splittable
    chunks = list(exhaustive._recon_prompts(source, 512, ledger, 'value.txt', route, screen))
    correct = {(route.endpoint, exhaustive._RECON_SYSTEM),
               (screen.endpoint, exhaustive._SCREEN_SYSTEM)}
    assert set(seen) == correct  # each leg validated with ITS system — no cross-pairing

    # (b) size invariant: every emitted chunk fits BOTH legs' real envelopes
    assert chunks
    for _start, _end, prompt in chunks:
        assert fits(route, exhaustive._RECON_SYSTEM, prompt)
        assert fits(screen, exhaustive._SCREEN_SYSTEM, prompt)
