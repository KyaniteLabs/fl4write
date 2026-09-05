import pytest
from pathlib import Path
from fl4write.exhaustive import _validated

@pytest.mark.parametrize("name", ["exhaustive_transaction.py", "exhaustive_sandbox.py", "exhaustive_budget.py", "exhaustive_publication.py"])
def test_validated_finding_preserves_tracked_source_path(name):
    root = Path(__file__).resolve().parents[1]
    path = "fl4write/" + name
    source = (root / path).read_text()
    evidence = source.splitlines()[0]
    rows = _validated({"findings": [{"path": path, "line": 1, "evidence": evidence, "severity": "Major", "description": "Identity regression"}]}, path, 1, len(source.splitlines()), source)
    assert rows[0]["path"] == path
    assert (root / rows[0]["path"]).is_file()
