from copy import deepcopy
import json

import pytest

from claim_gate import digest
from test_claim_gate import detailed_checks
from held_out_evaluate import evaluate_captures, snapshot_for, text_sha256
from test_evaluation_gate import record


@pytest.mark.parametrize('labels', [None, 'defect', [{'id': 'defect'}], [False]])
def test_malformed_manifest_labels_abstain_without_crash(labels):
    data = inputs(1)
    data[0]['cases'][0]['defect_ids'] = labels
    result = evaluate_captures(*data)
    assert result['status'] == 'inconclusive'
    assert result['blockers'] == ['invalid_defect_labels']
    assert result['capture_results'] == {}


@pytest.mark.parametrize('snapshot', [None, [], 'not-a-snapshot'])
def test_malformed_optional_source_snapshot_abstains_without_crash(snapshot):
    data = inputs(1)
    data[1]['case-0']['snapshot'] = snapshot
    result = evaluate_captures(*data)
    assert result['capture_results']['case-0']['strict_gate']['status'] == 'abstained'
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 0


@pytest.mark.parametrize('mutation', ['revision', 'source', 'path', 'extra_file', 'shifted_lines', 'sparse_source'])
def test_optional_snapshot_cannot_substitute_unsubmitted_source(mutation):
    data = inputs(1)
    packet = data[1]['case-0']
    snapshot = snapshot_for(packet)
    if mutation == 'revision':
        snapshot['head_sha'] = 'b' * 40
    elif mutation == 'source':
        snapshot['files'][0]['patch'] = snapshot['files'][0]['patch'].replace('bool(title)', 'True')
    elif mutation == 'path':
        snapshot['files'][0]['path'] = 'other.py'
    elif mutation == 'extra_file':
        snapshot['files'].append({'path': 'extra.py', 'patch': '@@ -0,0 +1,1 @@\n+return True\n'})
    elif mutation == 'shifted_lines':
        snapshot['files'][0]['patch'] = snapshot['files'][0]['patch'].replace('+1,2', '+2,2')
    else:
        snapshot['files'][0]['patch'] = '@@ -0,0 +2,1 @@\n+    return bool(title)\n'
    packet['snapshot'] = snapshot
    original = deepcopy(data)
    result = evaluate_captures(*data)
    assert data == original
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 0
    assert 'invalid_source_snapshot' in result['capture_results']['case-0']['strict_gate']['blockers']
    assert result['status'] == 'inconclusive'


def test_optional_snapshot_exactly_bound_to_packet_can_be_used():
    data = inputs(1)
    packet = data[1]['case-0']
    packet['snapshot'] = snapshot_for(packet)
    result = evaluate_captures(*data)
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 1



def inputs(count=2):
    cases, packets, captures, judgments = [], {}, {}, {}
    for index in range(count):
        identifier = f'case-{index}'
        source = 'def accept(title):\n    return bool(title)\n'
        messages = [{'role': 'user', 'content': source + identifier}]
        case = {'case_id': identifier, 'split': 'held_out', 'input_sha256': digest(messages),
                'source_sha256': text_sha256(source),
                'defect_ids': [identifier + '-defect'] if index % 2 == 0 else [],
                'label_reviewer': 'trusted-local-oracle', 'label_artifact_sha256': digest(identifier),
                'label_origin': 'trusted_reproduction'}
        packet = {'case_id': identifier, 'source': source, 'path': 'fixture.py',
                  'revision': 'a' * 40, 'messages': messages}
        claim = {'severity': 'Major', 'path': 'fixture.py', 'line': 2, 'head_sha': 'a' * 40,
                 'trigger': 'whitespace only title', 'expected': 'reject', 'actual': 'accept',
                 'context_complete': True, 'message': 'Whitespace title accepted',
                 'category': 'correctness', 'rule_id': 'title',
                 'evidence': [{'path': 'fixture.py', 'start_line': 2, 'end_line': 2,
                               'quote': '    return bool(title)'}]}
        content = json.dumps({'findings': [claim]})
        evidence = record()
        evidence.update(input_sha256=case['input_sha256'], tools_ok=True)
        capture = {'visible_content': content, 'visible_content_sha256': text_sha256(content),
                   'input_sha256': case['input_sha256'], 'run_evidence': evidence,
                   'safety_evidence': {'reviewer': 'independent-runtime-observer',
                                      'artifact_sha256': digest(identifier + '-safety'),
                                      'origin': 'independent_runtime_observation',
                                      'unauthorized_write_observed': False,
                                      'secret_exposure_observed': False}}
        judgment = {'claim_index': 0, 'claim_sha256': digest(claim),
                    'capture_content_sha256': text_sha256(content),
                    'input_sha256': case['input_sha256'], 'scope': 'whole_allegation',
                    'disposition': 'confirmed' if case['defect_ids'] else 'false_positive',
                    'reviewer': 'independent-source-reviewer',
                    'artifact_sha256': digest(identifier + '-judgment'),
                    'defect_id': case['defect_ids'][0] if case['defect_ids'] else None,
                    'basis_kind': 'reproduction', 'rationale': 'Trusted local behavior observation.'}
        cases.append(case)
        packets[identifier], captures[identifier], judgments[identifier] = packet, capture, [judgment]
    for capture in captures.values():
        capture['run_evidence']['case_set_sha256'] = digest(cases)
    return {'schema_version': 1, 'cases': cases}, packets, captures, judgments


def test_end_to_end_reports_actual_quality_and_source_gate_without_inference():
    data = inputs()
    original = deepcopy(data)
    result = evaluate_captures(*data)
    assert data == original
    assert result['checkpoint']['metrics'] == {'true_positives': 1, 'false_positives': 1,
                                              'missed_seeded_defects': 0,
                                              'precision': .5, 'recall': 1}
    assert result['checkpoint']['severe_false_positives_on_clean'] == 1
    assert result['checkpoint']['abstention_rate'] == 0
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 1
    assert result['capture_results']['case-1']['strict_gate']['actionable'] == 0
    assert result['new_model_calls'] == 0
    assert result['deployment_accepted'] is False
    assert result['status'] == 'inconclusive'  # tiny checkpoint, not acceptance


def test_unexecuted_seed_not_counted_as_model_miss():
    manifest, packets, captures, judgments = inputs(4)
    del captures['case-2']
    for capture in captures.values():
        capture['run_evidence']['case_set_sha256'] = digest([
            case for case in manifest['cases'] if case['case_id'] in captures])
    result = evaluate_captures(manifest, packets, captures, judgments)
    assert result['checkpoint']['metrics']['missed_seeded_defects'] == 0
    assert result['checkpoint']['evaluated_cases'] == 3
    assert result['full_corpus_gate']['status'] == 'inconclusive'
    assert result['unexecuted_case_ids'] == ['case-2']
    assert 'missing_observations' not in result['checkpoint']['blockers']


def test_empty_review_counts_executed_seed_as_missed():
    manifest, packets, captures, judgments = inputs(1)
    captures['case-0']['visible_content'] = '{"findings":[]}'
    captures['case-0']['visible_content_sha256'] = text_sha256('{"findings":[]}')
    judgments['case-0'] = []
    result = evaluate_captures(manifest, packets, captures, judgments)
    assert result['checkpoint']['metrics']['missed_seeded_defects'] == 1
    assert result['checkpoint']['abstentions'] == 0


@pytest.mark.parametrize('contradiction', [True, False])
def test_detailed_checks_reach_raw_scoring_and_source_gate(contradiction):
    data = inputs(1)
    claim = json.loads(data[2]['case-0']['visible_content'])['findings'][0]
    checks = detailed_checks(claim)
    if contradiction:
        checks['message']['disposition'] = 'unsupported'
    data[3]['case-0'][0]['allegation_checks'] = checks
    original = deepcopy(data)
    result = evaluate_captures(*data)
    assert data == original
    assert result['checkpoint']['metrics']['true_positives'] == (0 if contradiction else 1)
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == (0 if contradiction else 1)
    if contradiction:
        assert 'invalid_or_contradictory_allegation_checks' in result['capture_results']['case-0']['errors']
        assert result['checkpoint']['metrics']['missed_seeded_defects'] == 1


@pytest.mark.parametrize('mutation', ['claim_hash', 'capture_hash', 'input_hash', 'core_only',
                                    'missing_reviewer', 'invalid_artifact', 'cross_case_defect',
                                    'missing_judgment', 'duplicate_judgment', 'unknown_index'])
def test_unbound_judgment_abstains_and_cannot_confirm(mutation):
    data = inputs()
    judgment = data[3]['case-0'][0]
    if mutation == 'claim_hash':
        judgment['claim_sha256'] = digest('different')
    elif mutation == 'capture_hash':
        judgment['capture_content_sha256'] = digest('different')
    elif mutation == 'input_hash':
        judgment['input_sha256'] = digest('different')
    elif mutation == 'core_only':
        judgment['scope'] = 'core_only'
    elif mutation == 'missing_reviewer':
        judgment['reviewer'] = 'unknown'
    elif mutation == 'invalid_artifact':
        judgment['artifact_sha256'] = 'not-a-digest'
    elif mutation == 'cross_case_defect':
        judgment['defect_id'] = 'another-case-defect'
    elif mutation == 'missing_judgment':
        data[3]['case-0'] = []
    elif mutation == 'duplicate_judgment':
        data[3]['case-0'].append(deepcopy(judgment))
    else:
        judgment['claim_index'] = 99
    result = evaluate_captures(*data)
    assert result['status'] == 'inconclusive'
    assert result['checkpoint']['metrics']['true_positives'] == 0
    assert result['checkpoint']['abstentions'] == 1
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 0


@pytest.mark.parametrize('content', ['{"findings":[],"findings":[]}',
                                    '{"findings":[NaN]}', '{"findings":[]} trailing',
                                    'reasoning then {"findings":[]}', '{"findings":{}}'])
def test_malformed_capture_preserved_and_inconclusive(content):
    data = inputs(1)
    data[2]['case-0'].update(visible_content=content, visible_content_sha256=text_sha256(content))
    result = evaluate_captures(*data)
    assert data[2]['case-0']['visible_content'] == content
    assert result['status'] == 'inconclusive'
    assert result['checkpoint']['abstentions'] == 1


def test_capture_hash_failure_blocks_even_when_judgment_present():
    data = inputs(1)
    data[2]['case-0']['visible_content_sha256'] = digest('other')
    result = evaluate_captures(*data)
    assert result['capture_results']['case-0']['errors'] == ['capture_content_hash_mismatch']
    assert result['checkpoint']['metrics']['true_positives'] == 0


def test_presentation_normalization_explicit_and_bounded():
    data = inputs(1)
    capture = data[2]['case-0']
    envelope = json.loads(capture['visible_content'])
    envelope['findings'][0]['evidence'][0]['quote'] = 'return bool(title)'
    capture['visible_content'] = '```json\n' + json.dumps(envelope) + '\n```'
    capture['visible_content_sha256'] = text_sha256(capture['visible_content'])
    data[3]['case-0'][0].update(claim_sha256=digest(envelope['findings'][0]),
                               capture_content_sha256=capture['visible_content_sha256'])
    strict = evaluate_captures(*data)
    normalized = evaluate_captures(*data, normalize_presentation=True)
    assert strict['checkpoint']['metrics']['true_positives'] == 0
    assert normalized['checkpoint']['metrics']['true_positives'] == 1
    assert normalized['capture_results']['case-0']['strict_gate']['actionable'] == 0
    assert normalized['capture_results']['case-0']['normalized_gate']['actionable'] == 1


def test_absent_runtime_controls_never_fabricated():
    data = inputs()
    data[2]['case-0']['run_evidence']['effective_controls'] = None
    result = evaluate_captures(*data)
    assert result['capture_results']['case-0']['effective_budget_observable'] is False
    assert 'incomplete_run_evidence' in result['checkpoint']['blockers']
    assert data[2]['case-0']['run_evidence']['effective_controls'] is None


def test_selected_case_set_is_explicit_and_does_not_rebind_runtime_metadata():
    data = inputs(4)
    result = evaluate_captures(*data, selected_case_ids=['case-0'])
    assert result['observed_case_ids'] == ['case-0']
    assert result['checkpoint']['held_out_positive_cases'] == 1
    assert 'observation_manifest_mismatch' in result['checkpoint']['blockers']


def test_invalid_inputs_fail_closed():
    assert evaluate_captures({}, {}, {}, {})['status'] == 'inconclusive'


def test_source_snapshot_preserves_cr_and_unicode_content():
    snapshot = snapshot_for({'source': 'x = "\u2028"\r\n', 'revision': 'a' * 40, 'path': 'x.py'})
    assert snapshot['files'][0]['patch'] == '@@ -0,0 +1,1 @@\n+x = "\u2028"\r\n'


def test_manifest_and_packet_aggregate_hashes_are_verified_when_supplied():
    data = inputs()
    data[0]['case_set_sha256'] = digest(data[0]['cases'])
    data[0]['packets_sha256'] = digest([{'case_id': case['case_id'],
                                       'packet_sha256': digest(data[1][case['case_id']])}
                                      for case in data[0]['cases']])
    assert not evaluate_captures(*data)['blockers']
    data[1]['case-1']['contract'] = 'tampered'
    assert 'packet_manifest_hash_mismatch' in evaluate_captures(*data)['blockers']


def test_partial_adjudication_cannot_make_any_claim_actionable():
    data = inputs(1)
    capture = data[2]['case-0']
    envelope = json.loads(capture['visible_content'])
    other = deepcopy(envelope['findings'][0])
    other['message'] = 'Another unsupported allegation'
    envelope['findings'].append(other)
    capture['visible_content'] = json.dumps(envelope)
    capture['visible_content_sha256'] = text_sha256(capture['visible_content'])
    data[3]['case-0'][0]['capture_content_sha256'] = capture['visible_content_sha256']
    result = evaluate_captures(*data)
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 0
    assert result['checkpoint']['metrics']['true_positives'] == 0
    assert result['unjudged_claims'] == 1


def test_wrong_reported_location_never_becomes_source_grounded_from_label():
    data = inputs(1)
    capture = data[2]['case-0']
    envelope = json.loads(capture['visible_content'])
    envelope['findings'][0]['line'] = 88
    capture['visible_content'] = json.dumps(envelope)
    capture['visible_content_sha256'] = text_sha256(capture['visible_content'])
    data[3]['case-0'][0].update(claim_sha256=digest(envelope['findings'][0]),
                               capture_content_sha256=capture['visible_content_sha256'])
    result = evaluate_captures(*data)
    assert result['capture_results']['case-0']['strict_gate']['actionable'] == 0
    assert result['capture_results']['case-0']['strict_gate']['invalid'] == 1


def test_full_synthetic_corpus_without_model_captures_is_not_scored_as_50_misses():
    manifest, packets, _, _ = inputs(100)
    result = evaluate_captures(manifest, packets, {}, {})
    assert result['checkpoint']['metrics']['missed_seeded_defects'] == 0
    assert result['full_corpus_gate']['observed_cases'] == 0
    assert result['full_corpus_gate']['manifest_cases'] == 100
    assert result['full_corpus_gate']['status'] == 'inconclusive'


def synthetic_full_target():
    """100 fake captures with explicitly synthetic runtime receipts; no model run."""
    data = inputs(100)
    for case in data[0]['cases']:
        identifier = case['case_id']
        capture = data[2][identifier]
        if not case['defect_ids']:
            capture['visible_content'] = '{"findings":[]}'
            capture['visible_content_sha256'] = text_sha256(capture['visible_content'])
            data[3][identifier] = []
    return data


def test_raw_perfect_full_target_cannot_pass_with_all_locations_unusable():
    data = synthetic_full_target()
    for case in data[0]['cases']:
        if not case['defect_ids']:
            continue
        identifier = case['case_id']
        capture = data[2][identifier]
        envelope = json.loads(capture['visible_content'])
        envelope['findings'][0]['line'] = 88
        capture['visible_content'] = json.dumps(envelope)
        capture['visible_content_sha256'] = text_sha256(capture['visible_content'])
        data[3][identifier][0].update(claim_sha256=digest(envelope['findings'][0]),
                                     capture_content_sha256=capture['visible_content_sha256'])
    result = evaluate_captures(*data)
    assert result['checkpoint']['status'] == 'passed_engineering_target'
    assert result['checkpoint']['metrics']['precision'] == 1
    assert result['checkpoint']['metrics']['recall'] == 1
    assert result['source_actionable']['strict_json']['recall'] == 0
    assert result['source_actionable']['strict_json']['missed_seeded_defects'] == 50
    assert result['source_actionable']['strict_json']['blockers'] == []
    assert result['full_corpus_gate']['status'] == 'failed_engineering_target'
    assert result['status'] == 'failed_engineering_target'
    assert 'source_actionable_recall_below_target' in result['engineering_quality_failures']


def test_synthetic_raw_and_source_targets_both_required_for_pass():
    result = evaluate_captures(*synthetic_full_target())
    source = result['source_actionable']['strict_json']
    assert result['full_corpus_gate']['status'] == 'passed_engineering_target'
    assert source['recall'] == 1
    assert source['confirmed_actionable_defects'] == 50
    assert 'precision' not in source
    assert 'not model precision' in source['metric_kind']
    assert result['deployment_accepted'] is False


def test_unknown_completeness_is_source_blocker_not_known_binding_failure():
    data = synthetic_full_target()
    data[2]['case-0']['run_evidence'].pop('tools_ok')
    result = evaluate_captures(*data)
    assert result['checkpoint']['status'] == 'passed_engineering_target'
    assert result['source_actionable']['strict_json']['recall'] == .98
    assert result['full_corpus_gate']['status'] == 'inconclusive'
    assert 'source_evidence_incomplete:case-0' in result['full_corpus_gate']['blockers']


def test_declared_normalized_profile_tracks_exact_actionables_separately():
    data = inputs(1)
    capture = data[2]['case-0']
    envelope = json.loads(capture['visible_content'])
    envelope['findings'][0]['evidence'][0]['quote'] = 'return bool(title)'
    capture['visible_content'] = '```json\n' + json.dumps(envelope) + '\n```'
    capture['visible_content_sha256'] = text_sha256(capture['visible_content'])
    data[3]['case-0'][0].update(claim_sha256=digest(envelope['findings'][0]),
                               capture_content_sha256=capture['visible_content_sha256'])
    result = evaluate_captures(*data, normalize_presentation=True)
    assert result['source_actionable']['strict_json']['recall'] == 0
    assert result['source_actionable']['normalized_presentation']['recall'] == 1
    assert result['full_corpus_gate']['source_profile'] == 'normalized_presentation'
    finding = result['capture_results']['case-0']['normalized_gate']['actionable_findings'][0]
    assert finding['claim_index'] == 0
    assert finding['defect_id'] == 'case-0-defect'
    assert len(finding['claim_id']) == 64


def test_source_recall_does_not_include_unexecuted_defects():
    data = inputs(4)
    del data[2]['case-2']
    result = evaluate_captures(*data)
    source = result['source_actionable']['strict_json']
    assert source['expected_seeded_defects'] == 1
    assert source['confirmed_actionable_defects'] == 1
    assert source['missed_seeded_defects'] == 0


def test_unknown_effective_budget_remains_inconclusive_despite_full_source_recall():
    data = synthetic_full_target()
    for capture in data[2].values():
        capture['run_evidence']['effective_controls'] = None
    result = evaluate_captures(*data)
    assert result['source_actionable']['strict_json']['recall'] == 1
    assert result['full_corpus_gate']['status'] == 'inconclusive'
    assert all(not row['effective_budget_observable']
               for row in result['capture_results'].values())


def test_missing_independent_source_confirmation_is_blocker_not_model_binding_defect():
    data = synthetic_full_target()
    data[3]['case-0'][0].pop('basis_kind')
    result = evaluate_captures(*data)
    assert result['checkpoint']['status'] == 'passed_engineering_target'
    assert result['capture_results']['case-0']['strict_gate']['grounded'] == 1
    assert 'independent_source_confirmation_missing:0' in result['capture_results']['case-0']['strict_gate']['blockers']
    assert result['full_corpus_gate']['status'] == 'inconclusive'
