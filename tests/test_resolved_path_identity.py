import pytest
from fl4write import renderer
from fl4write.engine import _prior_findings
from fl4write.models import Finding
from test_fl4write import make_config, make_pr

@pytest.mark.parametrize(
    "path, identity",
    [(r"dir\name.py", r"dir\\name.py"), ("dir\nname.py", r"dir\nname.py")],
    ids=["literal-backslash", "newline"],
)
def test_resolved_path_preserves_persisted_identity(path, identity):
    config, pr = make_config(), make_pr()
    finding = Finding(rule_id="loc-ceiling", severity="Major", path=path, line=2, message="Finding")
    first = renderer.render_review(pr, [finding], config, "first")
    previous = _prior_findings(first)
    assert previous[0].path == identity
    resolved = renderer.render_review(pr, [], config, "second", previous_findings=previous)
    assert "Resolved since last review" in resolved
    assert "- ✅ `~" + identity + ":2`" in resolved
