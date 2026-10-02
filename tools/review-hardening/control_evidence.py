"""Grade each request control independently from saved transport receipts.

Backend-reported settings are evidence of what the backend reported, not proof
of token use or adequate reasoning. This helper grants no quality acceptance.
It does not consult startup defaults or retain prompts, output, or credentials.
"""

import math
import struct


def _valid(value, kind):
    if kind == "positive_integer":
        return type(value) is int and value > 0
    if kind == "integer":
        return type(value) is int
    if kind == "nonnegative_integer":
        return type(value) is int and value >= 0
    if kind == "boolean":
        return type(value) is bool
    if kind == "reasoning_format":
        return type(value) is str and value in ("none", "deepseek", "deepseek-legacy", "auto")
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def control_ledger(receipt):
    """Return a sanitized ledger without changing the original receipt."""
    receipt = receipt if isinstance(receipt, dict) else {}
    requested = receipt.get("request_controls")
    observed = receipt.get("observed_generation_settings")
    requested = requested if isinstance(requested, dict) else {}
    observed = observed if isinstance(observed, dict) else {}
    controls = {}
    specifications = (
        ("output_limit", "max_tokens", "n_predict", "positive_integer"),
        ("seed", "seed", "seed", "integer"),
        ("temperature", "temperature", "temperature", "number"),
        ("reasoning_format", "reasoning_format", "reasoning_format", "reasoning_format"),
        ("reasoning_budget", "reasoning_budget_tokens", "reasoning_budget_tokens", "nonnegative_integer"),
    )
    for name, request_key, observed_key, kind in specifications:
        want, actual = requested.get(request_key), observed.get(observed_key)
        valid_request, valid_observation = _valid(want, kind), _valid(actual, kind)
        if request_key not in requested:
            status = "not_requested"
        elif not valid_request:
            status = "invalid_request_evidence"
        elif observed_key not in observed or actual is None:
            status = "unknown"
        elif not valid_observation:
            status = "invalid_backend_evidence"
        else:
            matches = want == actual
            # Permit exactly the float32 representation, not a broad epsilon
            # that could turn a genuine small temperature change into a match.
            if kind == "number" and not matches:
                try:
                    matches = struct.unpack('!f', struct.pack('!f', want))[0] == actual
                except (OverflowError, struct.error):
                    matches = False
            status = "backend_reported_match" if matches else "backend_reported_mismatch"
        controls[name] = {
            "requested": want if valid_request else None,
            "backend_reported": actual if valid_observation else None,
            "status": status,
            "evidence_field": "observed_generation_settings." + observed_key,
        }

    kwargs = requested.get("chat_template_kwargs")
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    requested_thinking = kwargs.get("enable_thinking")
    thinking = {key: observed[key] for key in ("enable_thinking", "thinking_enabled")
                if key in observed}
    conflict = (len(thinking) == 2 and thinking['enable_thinking'] != thinking['thinking_enabled'])
    actual_thinking = next(iter(thinking.values()), None)
    if conflict:
        thinking_status = "conflicting_backend_evidence"
    elif any(type(value) is not bool for value in thinking.values()):
        thinking_status = "invalid_backend_evidence"
    elif 'enable_thinking' not in kwargs:
        thinking_status = "not_requested"
    elif type(requested_thinking) is not bool:
        thinking_status = "invalid_request_evidence"
    elif not thinking:
        thinking_status = "unknown"
    else:
        thinking_status = ("backend_reported_match" if requested_thinking == actual_thinking
                           else "backend_reported_mismatch")
    controls['thinking_enabled'] = {
        'requested': requested_thinking if type(requested_thinking) is bool else None,
        'backend_reported': actual_thinking if type(actual_thinking) is bool and not conflict else None,
        'status': thinking_status,
        'evidence_field': 'observed_generation_settings.enable_thinking/thinking_enabled',
    }
    measurements = {}
    tokens = receipt.get('tokens')
    tokens = tokens if isinstance(tokens, dict) else {}
    for key, kind in (("server_input_truncated", "boolean"),
                      ("reasoning_tokens", "integer"),
                      ("visible_tokens", "integer"),
                      ("budget_exhausted", "boolean")):
        nested = key in ('reasoning_tokens', 'visible_tokens') and key in tokens
        value = tokens.get(key) if nested else receipt.get(key)
        valid = _valid(value, kind) and (kind != "integer" or value >= 0)
        conflict = nested and key in receipt and receipt[key] != value
        measurements[key] = {
            "value": value if valid and not conflict else None,
            "status": "conflicting_evidence" if conflict else "recorded" if valid else "unknown",
            "evidence_field": 'tokens.' + key if nested else key,
        }
    return {
        "schema_version": 1,
        "controls": controls,
        "measurements": measurements,
        "interpretation": "Backend-reported settings do not prove actual token use or reasoning adequacy.",
        "quality_acceptance_granted": False,
        "merge_or_deployment_authorized": False,
    }
