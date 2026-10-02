"""Source-verified local llama.cpp control construction; no network calls.

Installed source: 2e0816267d27b9f751e7cdaf92d8f2620a799add,
tools/server/server-common.cpp:1279-1303,1337-1348. Request honoring still
requires a separately coordinated integration experiment on the actual binary.
"""
from copy import deepcopy

SOURCE_REVISION = '2e0816267d27b9f751e7cdaf92d8f2620a799add'


def budget_payload(payload, budget, source_revision):
    if source_revision != SOURCE_REVISION:
        raise ValueError('backend_controls_unverified')
    # -1 delegates to opt.reasoning_budget (512 in the audited server).
    # Positive overrides have actual parser/sampler support, unlike effort labels.
    if type(budget) is not int or not 0 <= budget <= 8192:
        raise ValueError('invalid_reasoning_budget')
    maximum = payload.get('max_tokens')
    if type(maximum) is not int or maximum < budget + 512:
        raise ValueError('insufficient_final_output_allowance')
    if any(key in payload for key in ('thinking', 'reasoning_effort', 'thinking_budget_tokens')):
        raise ValueError('ambiguous_reasoning_controls')
    result = deepcopy(payload)
    result['reasoning_budget_tokens'] = budget
    result['chat_template_kwargs'] = {'enable_thinking': budget > 0}
    # Separate reasoning extraction is supported by installed request parser.
    result['reasoning_format'] = 'deepseek'
    return result
