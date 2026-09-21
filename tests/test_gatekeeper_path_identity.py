"""Gatekeeper decisions refer to the exact finding, including unusual paths."""
import json
import re

import pytest

from fl4write import gatekeeper
from fl4write.models import Finding
from test_quality_tranche import make_config


@pytest.mark.parametrize('path', ['src/my  file.py', ' leading.py', 'trailing.py ',
                                 'src/my\tfile.py', 'src/' + 'a' * 210 + '.py'],
                         ids=['spaces', 'leading', 'trailing', 'tab', 'long'])
def test_prompt_identity_keeps_and_demotes_exact_path_without_collision(monkeypatch, path):
    paths = [path, 'normal.py', '__fl4write_path_0__', path]
    findings = [Finding(path=p, line=12, severity='Major', rule_id=r, message='bug')
                for p, r in zip(paths, ['general', 'general', 'general', 'other'])]

    def model(route, prompt, **kwargs):
        rows = re.findall(r'^- \[Major\] (.*):12 \((\w+)\):', prompt, re.MULTILINE)
        assert len(rows) == 4
        keep = [{'path': p, 'line': 12, 'rule_id': r} for p, r in rows[:2]]
        return json.dumps({'keep': keep,
                           'demote': [{**keep[0], 'severity': 'Minor'}]})

    monkeypatch.setattr(gatekeeper, '_call_model', model)
    kept, dropped, failed = gatekeeper.filter_findings(findings, make_config())
    assert kept == findings[:2]
    assert dropped == 2 and failed is False
    assert kept[0].path == path and kept[0].severity == 'Minor'
    assert kept[1].severity == 'Major'
