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
