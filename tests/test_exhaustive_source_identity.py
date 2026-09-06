"""Grounded source identities survive recon, evidence transfer, and repair replay."""
import hashlib
import json
import tarfile

import pytest

from fl4write import analyzer, exhaustive, exhaustive_fix, scrub
from fl4write.exhaustive_evidence import seal_bundle, verify_bundle
from fl4write.exhaustive_publication import ledger_body
from test_exhaustive_fix import _config


@pytest.mark.parametrize('name', [
    'exhaustive_transaction.py', 'exhaustive_sandbox.py',
    'exhaustive_budget.py', 'exhaustive_publication.py',
])
def test_offline_recon_evidence_and_cached_repair_keep_source_identity(tmp_path, monkeypatch, name):
    relative = 'fl4write/' + name
    artifact = tmp_path / 'artifacts'
    tree = artifact / 'tree'
    (tree / 'fl4write').mkdir(parents=True)
    source = 'VALUE = 1\n'
    (tree / relative).write_text(source)
    archive = artifact / 'head.tar'
    with tarfile.open(archive, 'w') as bundle:
        bundle.add(tree / relative, arcname=relative)
    head = 'a' * 40
    (artifact / 'manifest.json').write_text(json.dumps({
        'version': 1, 'head': head, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
    }))
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{}')
    responses = tmp_path / 'responses.json'
    responses.write_text(json.dumps([{'findings': [{
        'path': relative, 'line': 1, 'evidence': 'VALUE = 1',
        'severity': 'Major', 'message': 'Incorrect value in ' + relative,
    }]}]))
    config = _config()
    findings, coverage, usage = exhaustive._recon(
        tree, ledger, config.model, 1, config.model.max_tokens, 30, artifact, 48000, responses,
    )
    reference = seal_bundle(artifact, tmp_path / 'sealed')
    sealed = verify_bundle(reference)
    transferred = json.loads(sealed['worker-result.json'].read_bytes())['findings']
    assert transferred == findings
    assert json.loads(coverage.read_bytes())['entries'][0]['path'] == relative

    calls = []

    def repair_model(route, prompt, **kwargs):
        request = json.loads(prompt)
        calls.append(request)
        assert request['sources'] == {relative: source}
        assert request['findings'][0]['path'] == relative
        return json.dumps({'files': [
            {'path': relative, 'edits': [{'old': 'VALUE = 1', 'new': 'VALUE = 2'}], 'regression': False},
            {'path': 'tests/test_value.py', 'content': 'assert 1 + 1 == 2\n', 'regression': True},
        ]})

    monkeypatch.setattr(analyzer, '_call_model', repair_model)
    cache = tmp_path / 'repair'
    cache.mkdir()
    args = (config, head, transferred, tree, cache, ['pytest'])
    first = exhaustive_fix._prepared_patch(*args)
    assert first == exhaustive_fix._prepared_patch(*args)
    assert first[0][relative] == 'VALUE = 2\n'
    assert len(calls) == 1
    assert transferred[0]['message'] == scrub.redact_credentials(scrub.scrub('Incorrect value in ' + relative))
    assert transferred[0]['path'] == relative

    public = ledger_body({'round': 1, 'consecutive_green': 0, 'ledger': [{
        'round': 1, 'green': False, 'reviewed_head': head, 'finding_count': 1,
        'findings': transferred, 'model_usage': usage,
    }]}, config.repo)
    assert relative not in public
    assert '"findings"' not in public


def test_internal_path_still_requires_exact_grounding():
    with pytest.raises(exhaustive.Deferred, match='not grounded'):
        exhaustive._validated({'findings': [{
            'path': 'fl4write/exhaustive_sandbox.py', 'line': 1, 'evidence': 'VALUE = 1',
        }]}, 'fl4write/exhaustive_budget.py', 1, 1, 'VALUE = 1\n')


@pytest.mark.parametrize('fenced', [False, True])
def test_recon_displays_absolute_line_numbers_across_chunks_and_preserves_source(tmp_path, fenced):
    tree = tmp_path / 'tree'
    tree.mkdir()
    source = 'alpha\nbeta\ngamma\n'
    (tree / 'value.txt').write_text(source)
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{}')
    result = tmp_path / 'result.json'
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({
        'tree': str(tree), 'ledger': str(ledger), 'result': str(result),
        'route': _config().model.model_dump(), 'chunk_chars': 6,
        'max_calls': 3, 'max_tokens': 3 * _config().model.max_tokens,
    }))
    inputs = []

    def model(route, prompt, mode, system):
        value = json.loads(prompt)
        inputs.append(value)
        if value['start_line'] == 2:
            assert value['content'] == '2: beta\n'
            response = json.dumps({'findings': [{
                'path': 'value.txt', 'line': 2, 'evidence': 'beta',
                'severity': 'Major', 'message': 'fixture finding',
            }]})
            return f'```json\n{response}\n```' if fenced else response
        return '```json\n{"findings": []}\n```' if fenced else '{"findings": []}'

    assert exhaustive._worker(request, caller=model) == 0
    assert [v['start_line'] for v in inputs] == [1, 2, 3]
    value = json.loads(result.read_text())
    assert value['findings'][0]['line'] == 2
    assert value['findings'][0]['evidence'] == 'beta'
    assert {row['sha256'] for row in value['coverage']} == {hashlib.sha256(source.encode()).hexdigest()}
    assert (tree / 'value.txt').read_text() == source


@pytest.mark.parametrize('line,evidence', [(1, 'beta'), (2, '2: beta')])
def test_recon_numbering_does_not_relax_original_line_grounding(line, evidence):
    with pytest.raises(exhaustive.Deferred, match='not grounded'):
        exhaustive._validated({'findings': [{
            'path': 'value.txt', 'line': line, 'evidence': evidence,
        }]}, 'value.txt', 1, 3, 'alpha\nbeta\ngamma\n')


def test_recon_projects_large_test_history_without_changing_evidence(tmp_path):
    from fl4write.model_proxy import MAX_REQUEST, _encode

    ids = [f'tests/test_case.py::test_{i}[unicode-\u03bb-"-\\]' for i in range(6000)]
    row = {'round': 1, 'green': True, 'test_ids': ids, 'junit_sha256': 'a' * 64,
           'regressions': ['retain me'], 'evidence': {'path': 'bundle.json'}}
    ledger = {'ledger': [row, {**row, 'round': 2}], 'other': 'preserved'}
    archive = tmp_path / 'ledger-input.json'
    archive.write_text(json.dumps(ledger))
    original = archive.read_bytes()
    source = 'alpha\nbeta\ngamma\n'
    route = _config().model
    restored = []
    prompts = list(exhaustive._recon_prompts(source, 6, ledger, 'value.txt', route))
    assert len(prompts) == 3
    for start, end, prompt in prompts:
        projected = json.loads(prompt)
        assert len(prompt) < 2000
        assert projected['ledger']['other'] == 'preserved'
        for index, actual in enumerate(projected['ledger']['ledger']):
            summary = actual.pop('test_ids_summary')
            canonical_ids = (json.dumps(ids, sort_keys=True, separators=(',', ':')) + '\n').encode()
            assert summary == {'count': len(ids), 'sha256': hashlib.sha256(canonical_ids).hexdigest()}
            assert actual == {k: v for k, v in ledger['ledger'][index].items() if k != 'test_ids'}
        encoded = _encode({'endpoint': route.endpoint,
                           'payload': analyzer._model_payload(route, prompt, 'file', exhaustive._RECON_SYSTEM)},
                          MAX_REQUEST)
        assert len(encoded) <= MAX_REQUEST
        assert start == end
        restored.append(projected['content'].removeprefix(f'{start}: '))
    assert ''.join(restored) == source
    assert archive.read_bytes() == original
    assert ledger == json.loads(original)


@pytest.mark.parametrize('rows', [[], [{'round': 1}], [{'test_ids': []}]])
def test_recon_test_history_handles_absent_and_empty_ids(rows):
    from fl4write.exhaustive_evidence import recon_ledger_context

    ledger = {'ledger': rows}
    original = json.dumps(ledger)
    projected = recon_ledger_context(ledger)
    assert json.dumps(ledger) == original
    if rows and 'test_ids' in rows[0]:
        assert projected['ledger'][0]['test_ids_summary']['count'] == 0
    else:
        assert projected == ledger


def test_recon_test_history_digest_binds_identity_and_order():
    from fl4write.exhaustive_evidence import recon_ledger_context

    def digest(ids):
        return recon_ledger_context({'ledger': [{'test_ids': ids}]})['ledger'][0]['test_ids_summary']['sha256']

    assert digest(['\u03bb', '"\\']) == digest(['\u03bb', '"\\'])
    assert len({digest(['a', 'b']), digest(['a', 'c']), digest(['b', 'a'])}) == 3


@pytest.mark.parametrize('source,ledger', [
    ('x\n' * 48000, {}),
    ('\U0001f600\n' * 24000, {}),
    ('x\n' * 24000, {'history': '\\"' * 20000}),
], ids=['short-lines', 'unicode', 'large-ledger'])
def test_numbered_recon_requests_fit_real_transport_and_cover_every_source_line(source, ledger):
    from fl4write.model_proxy import MAX_REQUEST, _encode

    route = _config().model
    route.seed = 7
    requests = list(exhaustive._recon_prompts(source, 48000, ledger, 'value.txt', route))
    assert len(requests) > 1
    next_line = 1
    restored = []
    for start, end, prompt in requests:
        assert start == next_line
        next_line = end + 1
        raw = _encode({'endpoint': route.endpoint,
                       'payload': analyzer._model_payload(route, prompt, 'file', exhaustive._RECON_SYSTEM)}, MAX_REQUEST)
        assert len(raw) <= MAX_REQUEST
        content = json.loads(prompt)['content']
        for line, numbered in enumerate(content.splitlines(keepends=True), start):
            assert numbered.startswith(f'{line}: ')
            restored.append(numbered.removeprefix(f'{line}: '))
    assert next_line == len(source.splitlines()) + 1
    assert ''.join(restored) == source


@pytest.mark.parametrize('source,ledger', [('\U0001f600' * 48000, {}), ('x', {'history': 'x' * 262144})],
                         ids=['long-unicode-line', 'oversized-ledger'])
def test_unsplittable_recon_request_defers_before_inference(source, ledger):
    with pytest.raises(exhaustive.Deferred, match='exceeds transport limit'):
        list(exhaustive._recon_prompts(source, 48000, ledger, 'value.txt', _config().model))


@pytest.mark.parametrize('response', [
    '{"findings": [], "findings": []}',
    '```json\n{"findings": []}\n```\n{"findings": [{"line": 1}]}',
])
def test_recon_worker_rejects_duplicate_or_ambiguous_fenced_envelopes(tmp_path, response):
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / 'value.txt').write_text('alpha\n')
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{}')
    result = tmp_path / 'result.json'
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({
        'tree': str(tree), 'ledger': str(ledger), 'result': str(result),
        'route': _config().model.model_dump(), 'chunk_chars': 48000,
        'max_calls': 1, 'max_tokens': _config().model.max_tokens,
    }))
    with pytest.raises(exhaustive.Deferred, match='unusable output'):
        exhaustive._worker(request, caller=lambda *_: response)
    assert not result.exists()
