"""Interrupted recon keeps validated coverage without reusing another round."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from fl4write import exhaustive
from fl4write.exhaustive_recon import _digest
from test_exhaustive_fix import _config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fixture(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / 'value.txt').write_text('alpha\nbeta\ngamma\n')
    ledger = tmp_path / 'ledger.json'
    ledger.write_text('{"ledger": []}')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({
        'tree': str(tree), 'ledger': str(ledger), 'result': str(tmp_path / 'result.json'),
        'checkpoint': str(tmp_path / 'checkpoint.json'), 'binding': {'round': 1, 'head': 'a' * 40},
        'route': _config().model.model_dump(), 'chunk_chars': 6,
        'max_calls': 4, 'max_tokens': 4 * _config().model.max_tokens,
    }))
    return request


def _interrupted(request):
    calls = []

    def model(*args):
        calls.append(json.loads(args[1])['start_line'])
        if len(calls) == 2:
            raise TimeoutError('fixture outage')
        return json.dumps({'findings': [{
            'path': 'value.txt', 'line': 1, 'evidence': 'alpha',
            'severity': 'Major', 'message': 'fixture finding',
        }]})

    with pytest.raises(exhaustive.Deferred, match='fixture outage'):
        exhaustive._worker(request, model)
    assert calls == [1, 2]
    assert not request.with_name('result.json').exists()


def test_resume_preserves_findings_and_charges_failed_call(tmp_path):
    request = _fixture(tmp_path)
    _interrupted(request)
    calls = []

    def model(*args):
        calls.append(json.loads(args[1])['start_line'])
        return '{"findings": []}'

    assert exhaustive._worker(request, model) == 0
    result = json.loads((tmp_path / 'result.json').read_bytes())
    assert calls == [2, 3]
    assert result['calls'] == 4
    assert result['reserved_output_tokens'] == 4 * _config().model.max_tokens
    assert [row['start_line'] for row in result['coverage']] == [1, 2, 3]
    assert result['findings'][0]['evidence'] == 'alpha'
    assert exhaustive._worker(request, lambda *_: pytest.fail('completed chunk repeated')) == 0


@pytest.mark.parametrize('change', ['source', 'ledger', 'head', 'round', 'route', 'limit', 'archive', 'selected_request'])
def test_changed_identity_rejects_checkpoint_before_inference(tmp_path, change):
    request = _fixture(tmp_path)
    _interrupted(request)
    value = json.loads(request.read_bytes())
    if change == 'source':
        (tmp_path / 'tree/value.txt').write_text('other\nbeta\ngamma\n')
    elif change == 'ledger':
        (tmp_path / 'ledger.json').write_text('{"ledger": [{"test_ids": ["new"]}]}')
    elif change in ('head', 'round', 'archive', 'selected_request'):
        value['binding'][change] = 'different'
    elif change == 'route':
        value['route']['temperature'] = 0.9
    else:
        value['chunk_chars'] = 12
    request.write_text(json.dumps(value))
    with pytest.raises(exhaustive.Deferred, match='identity changed'):
        exhaustive._worker(request, lambda *_: pytest.fail('inference on changed identity'))


@pytest.mark.parametrize('change', ['checksum', 'prompt', 'grounding', 'attempts'])
def test_corrupt_checkpoint_fails_closed(tmp_path, change):
    request = _fixture(tmp_path)
    _interrupted(request)
    checkpoint = tmp_path / 'checkpoint.json'
    value = json.loads(checkpoint.read_bytes())
    if change == 'checksum':
        value['sha256'] = 'bad'
    else:
        if change == 'prompt':
            value['value']['entries'][0]['prompt_sha256'] = 'bad'
        elif change == 'grounding':
            value['value']['entries'][0]['response']['findings'][0]['evidence'] = 'absent'
        else:
            value['value']['attempts'] = 0
        value['sha256'] = _digest(value['value'])
    checkpoint.write_text(json.dumps(value))
    before = checkpoint.read_bytes()
    with pytest.raises(exhaustive.Deferred):
        exhaustive._worker(request, lambda *_: pytest.fail('inference on corrupt checkpoint'))
    assert checkpoint.read_bytes() == before


def test_new_round_performs_fresh_inference(tmp_path):
    request = _fixture(tmp_path)
    calls = []

    def model(*args):
        calls.append(json.loads(args[1])['start_line'])
        return '{"findings": []}'

    exhaustive._worker(request, model)
    value = json.loads(request.read_bytes())
    value['binding']['round'] = 2
    value['checkpoint'] = str(tmp_path / 'round2.json')
    request.write_text(json.dumps(value))
    exhaustive._worker(request, model)
    assert calls == [1, 2, 3, 1, 2, 3]


@pytest.mark.parametrize('change', ['duplicate', 'reordered', 'excess'])
def test_invalid_completed_prefix_never_calls_model(tmp_path, change):
    request = _fixture(tmp_path)
    exhaustive._worker(request, lambda *_: '{"findings": []}')
    checkpoint = tmp_path / 'checkpoint.json'
    value = json.loads(checkpoint.read_bytes())
    entries = value['value']['entries']
    if change == 'duplicate':
        entries[1] = entries[0]
    elif change == 'reordered':
        entries.reverse()
    else:
        entries.append(entries[-1])
        value['value']['attempts'] = 4
    value['sha256'] = _digest(value['value'])
    checkpoint.write_text(json.dumps(value))
    with pytest.raises(exhaustive.Deferred):
        exhaustive._worker(request, lambda *_: pytest.fail('invalid prefix dispatched'))


def test_crash_before_acceptance_retains_charge(tmp_path, monkeypatch):
    from fl4write.exhaustive_recon import ReconProgress

    request = _fixture(tmp_path)
    calls = []

    def model(*args):
        calls.append(json.loads(args[1])['start_line'])
        return '{"findings": []}'

    with monkeypatch.context() as patch:
        def crash(*_):
            raise OSError('fixture checkpoint interruption')
        patch.setattr(ReconProgress, 'accept', crash)
        with pytest.raises(OSError, match='checkpoint interruption'):
            exhaustive._worker(request, model)
    assert not (tmp_path / 'result.json').exists()
    exhaustive._worker(request, model)
    assert calls == [1, 1, 2, 3]
    assert json.loads((tmp_path / 'result.json').read_bytes())['calls'] == 4


def test_optimized_python_still_rejects_identity_change(tmp_path):
    request = _fixture(tmp_path)
    _interrupted(request)
    value = json.loads(request.read_bytes())
    value['binding']['round'] = 2
    request.write_text(json.dumps(value))
    code = (
        'import sys; from pathlib import Path; from fl4write.exhaustive import _worker, Deferred\n'
        'try: _worker(Path(sys.argv[1]), lambda *_: sys.exit(9))\n'
        'except Deferred: sys.exit(0)\n'
        'sys.exit(8)\n'
    )
    done = subprocess.run([sys.executable, '-O', '-c', code, str(request)], capture_output=True,
                          # The child imports fl4write from CWD when launched with
                          # `-c`; anchor it to this repository so the suite behaves
                          # the same from any invocation directory.
                          cwd=REPO_ROOT)
    assert done.returncode == 0, done.stderr
