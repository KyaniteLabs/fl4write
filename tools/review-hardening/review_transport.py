"""One bounded Chat Completions attempt with private, content-safe receipts.

No retries, fallback, credentials, service changes or model discovery. Reasoning
text is never saved. Unknown effective controls/counters stay unknown.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request

from claim_gate import digest
from control_evidence import control_ledger
from presentation_normalization import json_presentation


def sync_directory(path):
    """Persist entry creation/replacement on supported POSIX filesystems."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('artifact_symlink_refused')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def strict_json(content):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate_key')
            out[key] = value
        return out
    def refuse_constant(value):
        raise ValueError('nonfinite_json')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite_json')
        return number
    return json.loads(content, object_pairs_hook=unique, parse_constant=refuse_constant, parse_float=finite_float)


def strict_envelope(content, normalize_presentation=False):
    if normalize_presentation:
        content, _ = json_presentation(content)
    parsed = strict_json(content)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('findings'), list):
        raise ValueError('invalid_envelope')
    return parsed


def visible_only(content, allow_json_fence=False):
    if not isinstance(content, str):
        raise ValueError('nonstring_content')
    # Only closed leading blocks are reasoning boundaries. Tags inside JSON
    # strings are source/evidence data and must never be removed or rejected.
    clean = content.strip()
    while True:
        prefix = re.match(r'^<think\b[^>]*>.*?</think\s*>\s*', clean, flags=re.I | re.S)
        if prefix is None:
            break
        clean = clean[prefix.end():]
    if re.match(r'</?think\b', clean, re.I):
        raise ValueError('unclosed_reasoning')
    candidate, repairs = json_presentation(clean) if allow_json_fence else (clean, [])
    if not clean.startswith('{') and not (repairs and candidate.startswith('{')):
        # No reliable final boundary in arbitrary prose from reasoning_format
        # none. Keep a digest only; do not capture a possible reasoning draft.
        raise ValueError('no_final_json_boundary')
    return clean


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect_refused')


def counter(value):
    return value if type(value) is int and value >= 0 else None


def request_once(route, payload, artifact_path, opener=None, normalize_presentation=False):
    """Attempt marker survives errors/crashes; same run can never silently retry.

    The caller must have coordinated the existing resource gate before calling.
    Tests inject a fake opener; this function never grants that authorization.
    """
    path = Path(artifact_path)
    marker = path.with_suffix(path.suffix + '.attempt')
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(digest(payload) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)
    if route.key_env:
        raise ValueError('credential_route_refused')
    started = time.monotonic()
    receipt = {'schema': 1, 'status': 'started', 'attempt_count': 1,
               'model_requested': route.model, 'payload_sha256': digest(payload),
               'prompt_sha256': digest(payload.get('messages')),
               'request_controls': {k: payload[k] for k in
                   ('model', 'temperature', 'max_tokens', 'seed', 'reasoning_budget_tokens',
                    'chat_template_kwargs', 'reasoning_format', 'verbose') if k in payload},
               'effective_controls': None, 'budget_exhausted': None,
               'cost': {'new_spend': 0, 'basis': 'existing local route; not a billing receipt'}}
    receipt['control_evidence'] = control_ledger(receipt)
    private_json(path, receipt)
    try:
        opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        req = urllib.request.Request(route.endpoint, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with opener.open(req, timeout=180) as response:
            body = response.read(2_000_001)
        receipt['provider_response_sha256'] = hashlib.sha256(body).hexdigest()
        if len(body) > 2_000_000:
            raise ValueError('response_size_limit')
        data = strict_json(body)
        if not isinstance(data, dict):
            raise ValueError('invalid_provider_envelope')
        choices = data.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError('invalid_choices')
        choice = choices[0]
        finish = choice.get('finish_reason')
        receipt['finish_reason'] = finish if finish in ('stop', 'length', 'tool_calls', 'content_filter') else None
        returned_model = data.get('model')
        receipt['model_identity_match'] = returned_model == route.model if isinstance(returned_model, str) else None
        usage = data.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get('completion_tokens_details')
        details = details if isinstance(details, dict) else {}
        receipt['tokens'] = {key: counter(usage.get(key)) for key in
                             ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        receipt['tokens']['reasoning_tokens'] = counter(details.get('reasoning_tokens'))
        receipt['tokens']['visible_tokens'] = counter(details.get('visible_tokens'))
        verbose = data.get('__verbose')
        verbose = verbose if isinstance(verbose, dict) else {}
        settings = verbose.get('generation_settings')
        settings = settings if isinstance(settings, dict) else {}
        observed = {}
        for key in ('n_ctx', 'n_predict', 'seed', 'reasoning_budget_tokens'):
            if type(settings.get(key)) is int:
                observed[key] = settings[key]
        for key in ('enable_thinking', 'thinking_enabled'):
            if type(settings.get(key)) is bool:
                observed[key] = settings[key]
        if settings.get('reasoning_format') in ('none', 'deepseek', 'deepseek-legacy'):
            observed['reasoning_format'] = settings['reasoning_format']
        if type(settings.get('temperature')) in (int, float) and math.isfinite(settings['temperature']):
            observed['temperature'] = settings['temperature']
        receipt['observed_generation_settings'] = observed
        receipt['server_input_truncated'] = verbose.get('truncated') if type(verbose.get('truncated')) is bool else None
        for key in ('id_slot', 'tokens_predicted', 'tokens_evaluated'):
            receipt[key] = counter(verbose.get(key))
        thinking = observed.get('thinking_enabled', observed.get('enable_thinking'))
        conflicting = (type(observed.get('thinking_enabled')) is bool
                       and type(observed.get('enable_thinking')) is bool
                       and observed['thinking_enabled'] != observed['enable_thinking'])
        receipt['effective_control_conflicts'] = ['thinking_enabled'] if conflicting else []
        if (not conflicting and type(thinking) is bool and counter(observed.get('reasoning_budget_tokens')) is not None
                and counter(observed.get('n_ctx')) is not None and counter(observed.get('n_predict')) is not None):
            receipt['effective_controls'] = {
                'thinking_enabled': thinking, 'reasoning_budget_tokens': observed['reasoning_budget_tokens'],
                'context_limit': observed['n_ctx'], 'max_tokens': observed['n_predict'],
                'evidence_origin': 'backend_response'}
        # Never copy __verbose wholesale: it contains prompt and full generation.
        message = choice.get('message')
        if not isinstance(message, dict):
            raise ValueError('invalid_message')
        content = message.get('content')
        if isinstance(content, str):
            receipt['provider_content_sha256'] = hashlib.sha256(content.encode()).hexdigest()
        content = visible_only(content, allow_json_fence=normalize_presentation)
        original_final = content
        if normalize_presentation:
            content, repairs = json_presentation(content)
            receipt['presentation_normalizations'] = repairs
        # A brace prefix alone cannot establish a final boundary. Prose after
        # JSON may be an untagged reasoning draft. Preserve only hashes for
        # malformed/ambiguous final content; complete JSON shape failures can
        # still be retained safely for parser diagnosis.
        parsed_content = strict_json(content)
        if not isinstance(parsed_content, dict):
            raise ValueError('no_final_object_boundary')
        receipt['visible_content'] = content  # pre-redaction final output only; 0600
        if normalize_presentation and content != original_final:
            receipt['original_visible_content'] = original_final
        receipt['visible_content_sha256'] = hashlib.sha256(content.encode()).hexdigest()
        receipt['reasoning_content_retained'] = False
        private_json(path, receipt)  # preserve visible failures before parser checks
        if receipt['model_identity_match'] is False:
            raise ValueError('model_identity_mismatch')
        if finish != 'stop':
            raise ValueError('incomplete_or_unknown_finish')
        strict_envelope(content)
        receipt['status'] = 'response_validated'
        return content, receipt
    except Exception as exc:
        receipt.update(status='failed', error_type=type(exc).__name__)
        # Exception strings can contain URL credentials or echoed content.
        raise RuntimeError('review_request_failed; inspect private receipt') from None
    finally:
        receipt['latency_s'] = round(time.monotonic() - started, 3)
        receipt['control_evidence'] = control_ledger(receipt)
        private_json(path, receipt)
