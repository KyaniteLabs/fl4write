"""Malformed numeric annotations stay inside the CI watch containment boundary."""
import json

import pytest

from test_ciwatch import CIRedForge, _run


@pytest.mark.parametrize("literal", ["1e309", "-1e309"])
def test_overflow_annotation_uses_fallback_line(tmp_path, monkeypatch, literal):
    annotations = json.loads(
        '[{"path":"tests/test_x.py","message":"failing assertion",'
        '"start_line":' + literal + '}]'
    )
    forge = CIRedForge(annotations=annotations)
    _run(tmp_path, forge, monkeypatch, fix_result={"status": "nofix"})
    assert len(forge.fix_attempts) == 1
    assert forge.fix_attempts[0][1].line == 1
