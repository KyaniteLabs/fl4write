from copy import deepcopy

import pytest

from evaluation_gate import comparison, validity
from ornith_adapter import SOURCE_REVISION, budget_payload


def record():
    controls = {'thinking_enabled': True, 'reasoning_budget_tokens': 2048,
                'max_tokens': 8192, 'context_limit': 262144}
    return {'actual_model': 'SYNTHETIC_TEST_MODEL', 'weights_identity': {
            'kind': 'sha256', 'sha256': 'd' * 64, 'evidence_origin': 'runtime_digest'},
            'runtime_identity': {'host': 'synthetic', 'build': 'fake-test-build', 'pid': 1,
                                 'evidence_origin': 'runtime_process_observation'},
            'input_sha256': 'a' * 64, 'case_set_sha256': 'b' * 64,
            'cost_basis': 'offline unit test', 'telemetry_availability': {'reasoning_split': 'unsupported'},
            'prompt_tokens': 10, 'completion_tokens': 20, 'latency_s': 1.0,
            'context_limit': 262144, 'output_limit': 8192, 'finish_reason': 'stop',
            'input_truncated': False, 'output_truncated': False,
            'requested_controls': controls, 'effective_controls': {**controls, 'evidence_origin': 'backend_response'}}


def test_complete_evidence_is_comparable_but_never_computes_accuracy():
    assert validity(record())['status'] == 'valid'
    assert comparison([record(), deepcopy(record())])['status'] == 'comparable'


@pytest.mark.parametrize('field', ['actual_model', 'weights_identity', 'runtime_identity', 'context_limit',
                                 'output_limit', 'finish_reason', 'effective_controls', 'prompt_tokens',
                                 'completion_tokens', 'latency_s', 'cost_basis', 'telemetry_availability'])
def test_missing_evidence_forces_inconclusive_individual_failures_survive(field):
    value = record()
    value.pop(field)
    result = validity(value)
    assert result['status'] == 'inconclusive' and not result['comparative_quality_conclusion_allowed']
    assert result['individual_claim_failures_remain_valid']


def test_requested_2048_effective_512_is_not_budget_comparison():
    value = record()
    value['effective_controls']['reasoning_budget_tokens'] = 512
    result = validity(value)
    assert result['configured_effective_mismatches'] == ['reasoning_budget_tokens']
    assert comparison([record(), value])['status'] == 'inconclusive'


@pytest.mark.parametrize('field', ['input_truncated', 'output_truncated'])
def test_truncation_is_inconclusive(field):
    value = record()
    value[field] = True
    assert validity(value)['status'] == 'inconclusive'


def test_unmatched_input_or_case_set_is_not_comparable():
    value = record()
    value['case_set_sha256'] = 'c' * 64
    assert 'unmatched_case_set_sha256' in comparison([record(), value])['reasons']


def test_supported_positive_budget_controls_leave_original_payload_unchanged():
    payload = {'max_tokens': 8192, 'messages': []}
    changed = budget_payload(payload, 2048, SOURCE_REVISION)
    assert changed['reasoning_budget_tokens'] == 2048
    assert changed['chat_template_kwargs'] == {'enable_thinking': True}
    assert changed['reasoning_format'] == 'deepseek'
    assert payload == {'max_tokens': 8192, 'messages': []}


@pytest.mark.parametrize('budget', [-1, True, 8193, 'high'])
def test_unsupported_budget_does_not_silently_use_server_default(budget):
    with pytest.raises(ValueError):
        budget_payload({'max_tokens': 10000}, budget, SOURCE_REVISION)


def test_unknown_source_effort_labels_and_inadequate_outer_budget_are_refused():
    with pytest.raises(ValueError):
        budget_payload({'max_tokens': 8192}, 2048, 'unknown')
    with pytest.raises(ValueError):
        budget_payload({'max_tokens': 8192, 'reasoning_effort': 'high'}, 2048, SOURCE_REVISION)
    with pytest.raises(ValueError):
        budget_payload({'max_tokens': 2048}, 2048, SOURCE_REVISION)


@pytest.mark.parametrize('origin', ['request_only', 'source_supported', 'unknown'])
def test_source_or_request_intent_is_never_effective_evidence(origin):
    value = record()
    value['effective_controls']['evidence_origin'] = origin
    assert validity(value)['status'] == 'inconclusive'


@pytest.mark.parametrize('field', ['actual_model', 'weights_identity', 'runtime_identity',
                                 'input_sha256', 'case_set_sha256'])
def test_unknown_identity_placeholders_do_not_count_as_evidence(field):
    value = record()
    value[field] = 'unknown'
    assert not validity(value)['comparative_quality_conclusion_allowed']


@pytest.mark.parametrize('change', ['context', 'output'])
def test_counters_exceeding_limits_are_inconclusive(change):
    value = record()
    value['completion_tokens' if change == 'output' else 'prompt_tokens'] = 999999
    assert validity(value)['status'] == 'inconclusive'


def test_malformed_comparison_records_abstain():
    assert comparison([None, None])['status'] == 'inconclusive'
    assert comparison(None)['status'] == 'inconclusive'
