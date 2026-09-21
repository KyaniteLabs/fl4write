"""Execute the runner's aggregate against literal worker-result filenames."""
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('filename', [
    'configs/repo with spaces.yml',
    'configs/repo\nwith newline.yml',
    'configs/repo[ab]*?.yml',
])
@pytest.mark.parametrize('result', ['0\n', '1\n', None], ids=['success', 'failure', 'missing'])
def test_aggregate_reads_exact_result_path(tmp_path, filename, result):
    source = (Path(__file__).resolve().parents[1] / 'run-cycle.sh').read_text()
    aggregate = source.split('# aggregate: MISSING result file = ERR,', 1)[1]
    aggregate = aggregate.split('\n', 1)[1].split('# Cap the log', 1)[0]
    logs = tmp_path / 'logs'
    logs.mkdir()
    slug = filename.replace('/', '_')
    if result is not None:
        (logs / f'{slug}.result').write_text(result)
    # A glob expansion must not substitute an unrelated worker's result.
    (logs / 'configs_repoax.yml.result').write_text('1\n')
    script = 'set -uo pipefail\nDUE_FILES=("$@")\nLOG=runner.log\n' + aggregate
    completed = subprocess.run(
        ['bash', '-c', script, 'aggregate-test', filename],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    log = (tmp_path / 'runner.log').read_text()
    expected = '1 ok / 0 errors' if result == '0\n' else '0 ok / 1 errors'
    assert f'cycle: {expected}' in log
    assert ('NO RESULT FILE (worker died)' in log) == (result is None)
    assert completed.stderr == ''
