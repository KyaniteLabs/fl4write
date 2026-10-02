from copy import deepcopy
import json

import pytest

from control_evidence import control_ledger
from test_transport import call, response


def evidence():
    return {
        'request_controls': {'max_tokens': 8192, 'temperature': .2, 'seed': 17,
                             'reasoning_format': 'deepseek', 'reasoning_budget_tokens': 2048},
        'observed_generation_settings': {'n_predict': 8192, 'temperature': .20000000298023224,
                                         'seed': 17, 'reasoning_format': 'deepseek'},
        'tokens': {'reasoning_tokens': None, 'visible_tokens': None},
        'server_input_truncated': False, 'budget_exhausted': None,
    }


def test_partial_settings_are_visible_without_fabricating_reasoning_budget():
    receipt = evidence()
    original = deepcopy(receipt)
    ledger = control_ledger(receipt)
    for key in ('output_limit', 'temperature', 'seed', 'reasoning_format'):
        assert ledger['controls'][key]['status'] == 'backend_reported_match'
    assert ledger['controls']['reasoning_budget']['status'] == 'unknown'
    assert ledger['measurements']['server_input_truncated']['value'] is False
    assert ledger['measurements']['reasoning_tokens']['value'] is None
    assert ledger['measurements']['budget_exhausted']['value'] is None
    assert ledger['quality_acceptance_granted'] is False
    assert ledger['merge_or_deployment_authorized'] is False
    assert receipt == original


@pytest.mark.parametrize('value', [True, -1, '2048', float('nan'), float('inf')])
def test_invalid_budget_evidence_never_matches(value):
    receipt = evidence()
    receipt['observed_generation_settings']['reasoning_budget_tokens'] = value
    assert control_ledger(receipt)['controls']['reasoning_budget']['status'] == 'invalid_backend_evidence'


def test_explicit_disabled_zero_budget_is_a_valid_backend_setting():
    receipt = evidence()
    receipt['request_controls']['reasoning_budget_tokens'] = 0
    receipt['request_controls']['chat_template_kwargs'] = {'enable_thinking': False}
    receipt['observed_generation_settings'].update(reasoning_budget_tokens=0, enable_thinking=False)
    ledger = control_ledger(receipt)
    assert ledger['controls']['reasoning_budget']['status'] == 'backend_reported_match'
    assert ledger['controls']['thinking_enabled']['status'] == 'backend_reported_match'
    assert ledger['measurements']['budget_exhausted']['value'] is None


@pytest.mark.parametrize('value', [True, -1, '0.2', float('nan'), float('inf'), 10 ** 400])
def test_malicious_numeric_settings_are_rejected_without_crashing(value):
    receipt = evidence()
    receipt['observed_generation_settings']['temperature'] = value
    assert control_ledger(receipt)['controls']['temperature']['status'] == 'invalid_backend_evidence'


@pytest.mark.parametrize('value', [.3, .200001, 0])
def test_real_temperature_mismatch_survives_rounding_tolerance(value):
    receipt = evidence()
    receipt['observed_generation_settings']['temperature'] = value
    assert control_ledger(receipt)['controls']['temperature']['status'] == 'backend_reported_mismatch'


def test_small_temperature_changes_are_not_hidden_by_an_absolute_tolerance():
    receipt = evidence()
    receipt['request_controls']['temperature'] = 0
    receipt['observed_generation_settings']['temperature'] = 1e-9
    assert control_ledger(receipt)['controls']['temperature']['status'] == 'backend_reported_mismatch'


def test_supported_legacy_extraction_format_is_preserved():
    receipt = evidence()
    receipt['request_controls']['reasoning_format'] = 'deepseek-legacy'
    receipt['observed_generation_settings']['reasoning_format'] = 'deepseek-legacy'
    assert control_ledger(receipt)['controls']['reasoning_format']['status'] == 'backend_reported_match'


def test_request_intent_and_startup_defaults_cannot_fill_missing_observations():
    receipt = evidence()
    receipt['effective_controls'] = {'reasoning_budget_tokens': 2048}
    receipt['startup'] = {'reasoning_budget_tokens': 2048}
    receipt['tokens']['completion_tokens'] = 2055
    ledger = control_ledger(receipt)
    assert ledger['controls']['reasoning_budget']['status'] == 'unknown'
    assert ledger['measurements']['reasoning_tokens']['value'] is None


def test_unrequested_setting_does_not_become_a_confirmed_request():
    receipt = evidence()
    del receipt['request_controls']['seed']
    result = control_ledger(receipt)['controls']['seed']
    assert result['status'] == 'not_requested' and result['backend_reported'] == 17


def test_counter_schema_uses_transport_nested_tokens_and_refuses_conflicts():
    receipt = evidence()
    receipt['tokens'].update(reasoning_tokens=7, visible_tokens=13)
    ledger = control_ledger(receipt)
    assert ledger['measurements']['reasoning_tokens']['value'] == 7
    assert ledger['measurements']['visible_tokens']['evidence_field'] == 'tokens.visible_tokens'
    receipt['reasoning_tokens'] = 2048
    item = control_ledger(receipt)['measurements']['reasoning_tokens']
    assert item['value'] is None and item['status'] == 'conflicting_evidence'


@pytest.mark.parametrize('value', [None, True, -1, 'secret-counter'])
def test_invalid_counter_values_remain_unknown(value):
    receipt = evidence()
    receipt['tokens']['reasoning_tokens'] = value
    assert control_ledger(receipt)['measurements']['reasoning_tokens']['value'] is None


@pytest.mark.parametrize('receipt', [None, [], 'secret', {'request_controls': [], 'tokens': []}])
def test_malformed_containers_do_not_raise_or_echo(receipt):
    output = control_ledger(receipt)
    assert 'secret' not in json.dumps(output)
    assert output['quality_acceptance_granted'] is False


def test_conflicting_thinking_aliases_cannot_be_selected_for_a_match():
    receipt = evidence()
    receipt['request_controls']['chat_template_kwargs'] = {'enable_thinking': True}
    receipt['observed_generation_settings'].update(enable_thinking=False, thinking_enabled=True)
    item = control_ledger(receipt)['controls']['thinking_enabled']
    assert item['status'] == 'conflicting_backend_evidence' and item['backend_reported'] is None


def test_provider_metadata_and_arbitrary_format_strings_are_not_echoed():
    receipt = evidence()
    receipt.update(prompt='DO_NOT_RETAIN_PROMPT', content='DO_NOT_RETAIN_CONTENT', credentials='DO_NOT_RETAIN_KEY')
    receipt['observed_generation_settings'].update(reasoning_format='DO_NOT_RETAIN_FORMAT', secret='DO_NOT_RETAIN_SECRET')
    assert 'DO_NOT_RETAIN' not in json.dumps(control_ledger(receipt))


@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_transport_persists_ledger_for_success_and_incomplete_output(tmp_path, finish):
    data = response(finish=finish)
    data['__verbose'] = {'generation_settings': {'n_predict': 4000}, 'truncated': False}
    if finish == 'stop':
        (_, receipt), target = call(tmp_path, data)
        assert receipt['control_evidence']['measurements']['reasoning_tokens']['value'] == 7
    else:
        with pytest.raises(RuntimeError):
            call(tmp_path, data)
        target = tmp_path / 'capture.json'
    ledger = json.loads(target.read_text())['control_evidence']
    assert ledger['controls']['output_limit']['status'] == 'backend_reported_match'
    assert ledger['quality_acceptance_granted'] is False


def test_transport_failure_still_records_unknown_controls_and_never_retries(tmp_path):
    from test_transport import FakeOpener
    opener = FakeOpener(failure=OSError('DO_NOT_RETAIN_FAILURE'))
    with pytest.raises(RuntimeError):
        call(tmp_path, None, opener)
    saved = (tmp_path / 'capture.json').read_text()
    assert 'DO_NOT_RETAIN' not in saved
    assert json.loads(saved)['control_evidence']['controls']['output_limit']['status'] == 'unknown'
    with pytest.raises(FileExistsError):
        call(tmp_path, None, opener)
    assert len(opener.calls) == 1
