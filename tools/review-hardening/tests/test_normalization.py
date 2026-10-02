from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from claim_gate import assess, bind_claim, digest, source_index
from presentation_normalization import pinned_quote
from review_transport import request_once, strict_envelope
from test_transport import FakeOpener, response


def case():
    snapshot = {'repository': 'fixture/repo', 'head_sha': 'a' * 40,
                'files': [{'path': 'a.py', 'patch': '@@ -0,0 +1,2 @@\n+def allow(owner, signed_in):\n+    return owner or signed_in'}]}
    claim = {'head_sha': snapshot['head_sha'], 'path': 'a.py', 'line': 2,
             'rule_id': 'correctness', 'category': 'auth', 'severity': 'Major',
             'message': 'Nonowner allowed.', 'trigger': 'owner=False, signed_in=True',
             'expected': 'False', 'actual': 'True', 'context_complete': True,
             'evidence': [{'path': 'a.py', 'start_line': 2, 'end_line': 2,
                           'quote': 'return owner or signed_in'}]}
    run = {'repository': snapshot['repository'], 'head_sha': snapshot['head_sha'],
           'snapshot_sha256': digest(snapshot), 'input_complete': True,
           'response_complete': True, 'tools_ok': True}
    return snapshot, claim, run


@pytest.mark.parametrize('fence', ['```', '````'])
def test_one_complete_json_fence_is_only_presentation(fence):
    raw = fence + 'json\n{"findings": []}\n' + fence
    assert strict_envelope(raw, normalize_presentation=True) == {'findings': []}
    with pytest.raises(ValueError):
        strict_envelope(raw)


@pytest.mark.parametrize('raw', [
    'preface\n```json\n{"findings": []}\n```',
    '```json\n{"findings": []}\n```\nreasoning draft',
    '```json\n{"findings": []}\n```\n```json\n{"findings": []}\n```',
    '```python\n{"findings": []}\n```', '```json\n{"findings": []}\n````',
    '```json\n{"findings": [], "findings": []}\n```',
    '```json\n{"findings": []}{"findings": []}\n```',
    '```json\n{"findings": [], "score": 1e309}\n```'])
def test_ambiguous_malicious_fences_remain_refused(raw):
    with pytest.raises(ValueError):
        strict_envelope(raw, normalize_presentation=True)


def test_transport_preserves_original_and_records_lossless_fence_repair(tmp_path):
    raw = '```json\n{"findings": []}\n```'
    route = SimpleNamespace(model='LOCAL_TEST_MODEL', key_env='', endpoint='http://127.0.0.1/v1/chat/completions')
    text, record = request_once(route, {'model': route.model}, tmp_path/'capture.json',
                                FakeOpener(response(raw)), normalize_presentation=True)
    assert text == '{"findings": []}' and record['original_visible_content'] == raw
    assert record['presentation_normalizations'][0]['kind'] == 'single_complete_json_fence'
    assert json.loads((tmp_path/'capture.json').read_text())['status'] == 'response_validated'


def test_unique_missing_indentation_is_bound_without_claim_mutation_or_self_approval():
    snapshot, claim, run = case()
    original = deepcopy(claim)
    baseline = assess({'findings': [claim]}, snapshot, run)
    normalized = assess({'findings': [claim]}, snapshot, run, normalize_indent=True)
    assert baseline['claims'][0]['reason'] == 'quote_mismatch'
    assert normalized['claims'][0]['status'] == 'grounded_unvalidated'
    assert normalized['claims'][0]['presentation_normalizations'][0]['kind'] == 'missing_ascii_space_indent'
    assert not normalized['actionable'] and claim == original


@pytest.mark.parametrize('quote', ['return owner and signed_in', 'return owner or signed_in ',
                                 '\treturn owner or signed_in', 'return OWNER or signed_in',
                                 '        return owner or signed_in', 'return owner or signed_in\nextra'])
def test_semantic_trailing_tab_or_extra_indent_edits_are_never_normalized(quote):
    with pytest.raises(ValueError):
        pinned_quote({2: '    return owner or signed_in'}, 2, 2, quote)


def test_ambiguous_indentation_binding_refused_even_when_model_supplies_line():
    with pytest.raises(ValueError, match='ambiguous_indent_normalization'):
        pinned_quote({2: '    return True', 8: '    return True'}, 2, 2, 'return True')


def test_revision_and_anchor_checks_survive_normalization():
    snapshot, claim, run = case()
    claim['head_sha'] = 'b' * 40
    assert assess({'findings': [claim]}, snapshot, run, normalize_indent=True)['claims'][0]['reason'] == 'revision_mismatch'
    claim['head_sha'] = snapshot['head_sha']
    claim['line'] = 999
    assert assess({'findings': [claim]}, snapshot, run, normalize_indent=True)['claims'][0]['reason'] == 'unknown_location'


@pytest.mark.parametrize('scope', [None, 'core_only', 'whole_allegation'])
def test_core_only_confirmation_never_elevates_unsupported_whole_message(scope):
    snapshot, claim, run = case()
    identifier = bind_claim(claim, snapshot, source_index(snapshot), normalize_indent=True)
    proof = {'claim_id': identifier, 'snapshot_sha256': digest(snapshot), 'head_sha': snapshot['head_sha'],
             'reviewer': 'trusted-fixture-test', 'disposition': 'confirmed', 'verified_severity': 'Major',
             'scope': scope, 'basis': {'kind': 'reproduction', 'artifact_sha256': 'b'*64,
                                     'rationale': 'Independent whole allegation check.'}}
    result = assess({'findings': [claim]}, snapshot, run, {identifier: proof}, normalize_indent=True)
    assert bool(result['actionable']) is (scope == 'whole_allegation')
