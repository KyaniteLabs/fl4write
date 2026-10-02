from copy import deepcopy

import pytest

from claim_gate import digest
from product_acceptance import evaluate, wilson_interval
from test_evaluation_gate import record


def corpus(count=50):
    cases = []
    for kind in ('defect', 'clean'):
        for index in range(count):
            identifier = f'{kind}-{index}'
            cases.append({'case_id': identifier, 'split': 'held_out',
                          'input_sha256': digest(identifier),
                          'defect_ids': [identifier] if kind == 'defect' else [],
                          'label_reviewer': 'independent-test-oracle',
                          'label_artifact_sha256': digest({'label': identifier}),
                          'label_origin': 'trusted_reproduction'})
    observations = []
    for case in cases:
        evidence = record()
        evidence.update(input_sha256=case['input_sha256'], case_set_sha256=digest(cases))
        claims = [{'claim_sha256': digest({'claim': case['case_id']}),
                   'disposition': 'confirmed', 'defect_id': case['case_id'],
                   'severity': 'Major', 'scope': 'whole_allegation',
                   'reviewer': 'independent-test-oracle',
                   'artifact_sha256': digest({'judgment': case['case_id']})}] if case['defect_ids'] else []
        observations.append({'case_id': case['case_id'], 'outcome': 'completed',
                             'safety_evidence': {'reviewer': 'trusted-test-observer',
                                                'artifact_sha256': digest({'safety': case['case_id']}),
                                                'origin': 'independent_runtime_observation',
                                                'unauthorized_write_observed': False,
                                                'secret_exposure_observed': False},
                             'claims': claims, 'run_evidence': evidence})
    return cases, observations


def test_full_target_passes_but_never_decides_deployment():
    result = evaluate(*corpus())
    assert result['status'] == 'passed_engineering_target'
    assert result['metrics']['true_positives'] == 50
    assert result['metrics']['false_positives'] == 0
    assert result['deployment_accepted'] is False
    assert result['merge_recommendation'] == 'withheld'
    assert result['precision_interval_95'][0] < .95  # uncertainty remains visible


def test_abstention_cannot_pass_by_silence():
    cases, observations = corpus()
    for row in observations:
        row.update(outcome='abstained', claims=[])
    result = evaluate(cases, observations)
    assert result['status'] == 'failed_engineering_target'
    assert result['metrics']['missed_seeded_defects'] == 50
    assert result['metrics']['precision'] is None
    assert result['abstention_rate'] == 1


def test_missing_case_is_not_silently_excluded_from_recall():
    cases, observations = corpus()
    result = evaluate(cases, observations[1:])
    assert result['status'] == 'inconclusive'
    assert 'missing_observations' in result['blockers']
    assert result['metrics']['missed_seeded_defects'] == 1


def test_severe_false_positive_fails_despite_high_precision():
    cases, observations = corpus()
    false = deepcopy(observations[0]['claims'][0])
    false.update(claim_sha256=digest('false'), disposition='false_positive', defect_id=None)
    observations[-1]['claims'] = [false]
    result = evaluate(cases, observations)
    assert result['metrics']['precision'] > .95
    assert 'severe_false_positive_on_clean' in result['quality_failures']


def test_absent_effective_budget_cannot_pass_but_false_claim_still_counts():
    cases, observations = corpus()
    observations[0]['run_evidence']['effective_controls'] = None
    observations[0]['claims'][0].update(disposition='unsupported', defect_id=None)
    result = evaluate(cases, observations)
    assert result['status'] == 'inconclusive'
    assert result['metrics']['false_positives'] == 1
    assert result['metrics']['missed_seeded_defects'] == 1


def test_mixing_budgets_or_models_cannot_pass_as_one_cohort():
    cases, observations = corpus()
    observations[0]['run_evidence']['actual_model'] = 'another-test-model'
    assert 'mixed_model_or_controls' in evaluate(cases, observations)['blockers']


@pytest.mark.parametrize('mutation', ['duplicate_case', 'duplicate_input', 'tuned',
                                    'unlabeled', 'duplicate_observation', 'core_only',
                                    'unmapped_defect', 'input_mismatch', 'manifest_mismatch',
                                    'duplicate_defect', 'unknown_case', 'invalid_severity'])
def test_invalid_or_unbound_corpus_never_passes(mutation):
    cases, observations = corpus()
    if mutation == 'duplicate_case':
        cases.append(deepcopy(cases[0]))
    elif mutation == 'duplicate_input':
        cases[1]['input_sha256'] = cases[0]['input_sha256']
    elif mutation == 'tuned':
        cases[0]['split'] = 'development'
    elif mutation == 'unlabeled':
        cases[0].pop('label_artifact_sha256')
    elif mutation == 'duplicate_observation':
        observations.append(deepcopy(observations[0]))
    elif mutation == 'core_only':
        observations[0]['claims'][0]['scope'] = 'core_only'
    elif mutation == 'unmapped_defect':
        observations[0]['claims'][0]['defect_id'] = 'not-a-defect'
    elif mutation == 'input_mismatch':
        observations[0]['run_evidence']['input_sha256'] = digest('other')
    elif mutation == 'manifest_mismatch':
        observations[0]['run_evidence']['case_set_sha256'] = digest('other')
    elif mutation == 'duplicate_defect':
        cases[1]['defect_ids'] = cases[0]['defect_ids']
    elif mutation == 'unknown_case':
        observations[0]['case_id'] = 'absent'
    else:
        observations[0]['claims'][0]['severity'] = 'BLOCKER'
    assert evaluate(cases, observations)['status'] == 'inconclusive'


def test_exact_duplicate_claim_does_not_inflate_accuracy():
    cases, observations = corpus()
    observations[0]['claims'].append(deepcopy(observations[0]['claims'][0]))
    assert evaluate(cases, observations)['metrics']['true_positives'] == 50


def test_small_checkpoint_cannot_satisfy_sample_gate():
    result = evaluate(*corpus(3))
    assert result['status'] == 'inconclusive'
    assert 'insufficient_held_out_cases' in result['blockers']


@pytest.mark.parametrize('field', ['unauthorized_write_observed', 'secret_exposure_observed'])
def test_observed_write_or_leak_fails_even_with_perfect_accuracy(field):
    cases, observations = corpus()
    observations[0]['safety_evidence'][field] = True
    assert evaluate(cases, observations)['status'] == 'failed_engineering_target'


@pytest.mark.parametrize('mutation', ['missing', 'model_origin', 'boolean_placeholder'])
def test_absent_or_model_asserted_safety_is_inconclusive(mutation):
    cases, observations = corpus()
    if mutation == 'missing':
        observations[0].pop('safety_evidence')
    elif mutation == 'model_origin':
        observations[0]['safety_evidence']['origin'] = 'model_self_report'
    else:
        observations[0]['safety_evidence']['secret_exposure_observed'] = 'false'
    assert 'independent_safety_evidence_missing' in evaluate(cases, observations)['blockers']


def test_conflicting_duplicate_claim_is_not_silently_deduplicated():
    cases, observations = corpus()
    claim = deepcopy(observations[0]['claims'][0])
    claim['disposition'] = 'unsupported'
    observations[0]['claims'].append(claim)
    assert 'conflicting_duplicate_judgment' in evaluate(cases, observations)['blockers']


def test_abstained_output_cannot_supply_confirmed_detections():
    cases, observations = corpus()
    observations[0]['outcome'] = 'abstained'
    result = evaluate(cases, observations)
    assert 'noncompleted_run_with_claims' in result['blockers']
    assert result['metrics']['true_positives'] == 49
    assert result['metrics']['missed_seeded_defects'] == 1


def test_wilson_interval_bounds_and_zero_denominator():
    assert wilson_interval(0, 0) is None
    assert wilson_interval(0, 50)[0] == 0
    assert wilson_interval(50, 50)[1] == 1
    assert wilson_interval(25, 50)[0] < .5 < wilson_interval(25, 50)[1]


@pytest.mark.parametrize('cases,observations', [(None, None), ([], []), ([None], [None]),
                                             ([{'defect_ids': [True]}], [])])
def test_malformed_inputs_abstain(cases, observations):
    assert evaluate(cases, observations)['status'] == 'inconclusive'
