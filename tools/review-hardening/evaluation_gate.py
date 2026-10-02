"""Evidence validity for model comparisons, independent of claim dispositions."""
import re


def known(value):
    return (isinstance(value, str) and bool(value.strip())
            and value.strip().lower() not in ('unknown', 'unverified', 'none', 'n/a', 'placeholder'))


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def validity(record):
    missing = []
    mismatches = []
    if not isinstance(record, dict):
        record = {}
    for field in ('actual_model', 'cost_basis'):
        if not known(record.get(field)):
            missing.append(field)
    for field in ('input_sha256', 'case_set_sha256'):
        if not sha256(record.get(field)):
            missing.append(field)
    weights = record.get('weights_identity')
    if not isinstance(weights, dict) or weights.get('evidence_origin') not in ('runtime_file_observation', 'runtime_digest'):
        missing.append('weights_identity')
    elif weights.get('kind') == 'sha256':
        if not sha256(weights.get('sha256')):
            missing.append('weights_digest')
    elif weights.get('kind') == 'installed_file_metadata':
        if (not known(weights.get('path')) or type(weights.get('size_bytes')) is not int
                or weights['size_bytes'] <= 0 or not known(weights.get('modified_at'))):
            missing.append('weights_file_metadata')
    else:
        missing.append('weights_identity_kind')
    runtime = record.get('runtime_identity')
    if (not isinstance(runtime, dict) or runtime.get('evidence_origin') != 'runtime_process_observation'
            or not known(runtime.get('host')) or not known(runtime.get('build'))
            or type(runtime.get('pid')) is not int or runtime['pid'] <= 0):
        missing.append('runtime_identity')
    availability = record.get('telemetry_availability')
    if not isinstance(availability, dict) or availability.get('reasoning_split') not in ('returned', 'unsupported'):
        missing.append('telemetry_availability')
    for field in ('context_limit', 'output_limit', 'prompt_tokens', 'completion_tokens'):
        value = record.get(field)
        if type(value) is not int or value < (1 if field.endswith('limit') else 0):
            missing.append(field)
    latency = record.get('latency_s')
    if type(latency) not in (int, float) or not 0 <= latency < float('inf'):
        missing.append('latency_s')
    if record.get('finish_reason') != 'stop':
        missing.append('complete_finish_reason')
    if record.get('input_truncated') is not False or record.get('output_truncated') is not False:
        missing.append('truncation_evidence')
    requested = record.get('requested_controls')
    effective = record.get('effective_controls')
    if not isinstance(requested, dict) or not isinstance(effective, dict):
        missing.append('effective_reasoning_controls')
    else:
        if effective.get('evidence_origin') not in ('backend_response', 'backend_task_log'):
            missing.append('effective_controls_evidence_origin')
        for key in ('thinking_enabled', 'reasoning_budget_tokens', 'max_tokens', 'context_limit'):
            if key not in effective:
                missing.append('effective_' + key)
            elif key not in requested:
                missing.append('requested_' + key)
            elif type(effective[key]) is not type(requested[key]) or effective[key] != requested[key]:
                mismatches.append(key)
        if type(effective.get('thinking_enabled')) is not bool:
            missing.append('effective_thinking_enabled')
        budget = effective.get('reasoning_budget_tokens')
        if type(budget) is not int or budget < 0:
            missing.append('effective_budget')
        for control, field in (('max_tokens', 'output_limit'), ('context_limit', 'context_limit')):
            if effective.get(control) != record.get(field):
                mismatches.append(field)
    if type(record.get('prompt_tokens')) is int and type(record.get('completion_tokens')) is int:
        if type(record.get('output_limit')) is int and record['completion_tokens'] > record['output_limit']:
            mismatches.append('completion_tokens_exceed_output_limit')
        if (type(record.get('context_limit')) is int
                and record['prompt_tokens'] + record['completion_tokens'] > record['context_limit']):
            mismatches.append('token_usage_exceeds_context_limit')
    return {'status': 'valid' if not missing and not mismatches else 'inconclusive',
            'missing_evidence': sorted(set(missing)), 'configured_effective_mismatches': sorted(set(mismatches)),
            'comparative_quality_conclusion_allowed': not missing and not mismatches,
            'individual_claim_failures_remain_valid': True}


def comparison(records):
    if not isinstance(records, list):
        records = []
    runs = [validity(record) for record in records]
    reasons = []
    if len(records) < 2:
        reasons.append('requires_two_runs')
    if any(run['status'] != 'valid' for run in runs):
        reasons.append('run_evidence_incomplete_or_mismatched')
    for field in ('input_sha256', 'case_set_sha256', 'output_limit', 'context_limit'):
        normalized = [record if isinstance(record, dict) else {} for record in records]
        if normalized and any(record.get(field) != normalized[0].get(field) for record in normalized[1:]):
            reasons.append('unmatched_' + field)
    return {'status': 'comparable' if not reasons else 'inconclusive', 'runs': runs,
            'reasons': reasons, 'comparative_quality_conclusion_allowed': not reasons,
            'individual_claim_failures_remain_valid': True}
