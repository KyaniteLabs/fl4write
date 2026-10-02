"""Portable frozen-denominator regression; authored synthetic evidence only."""
from copy import deepcopy

import pytest

from claim_gate import digest
from test_product_acceptance import corpus


def false_claim(template, index):
    row = deepcopy(template)
    row.update(claim_sha256=digest({'synthetic_false': index}),
               disposition='false_positive', defect_id=None, severity='Minor')
    return row



def test_live_engineering_gate_preserves_original_target_despite_diagnostic_gain():
    from product_acceptance import evaluate
    cases, observations = corpus()
    base = observations[0]['claims'][0]
    for number in range(7):
        paraphrase = deepcopy(base)
        paraphrase['claim_sha256'] = digest({'synthetic_extra_paraphrase': number})
        observations[0]['claims'].append(paraphrase)
    observations[0]['claims'].extend(false_claim(base, number) for number in range(3))
    result = evaluate(cases, observations)
    assert result['metrics']['precision'] == .95  # Corrected claim diagnostic.
    assert result['frozen_gate_precision'] == pytest.approx(50 / 53)
    assert result['metrics']['false_positives'] == 3
    assert result['metrics']['missed_seeded_defects'] == 0
    assert result['status'] == 'failed_engineering_target'
    assert 'frozen_precision_below_target' in result['quality_failures']
    assert result['denominator_change_is_model_gain'] is False
    assert result['deployment_accepted'] is False
