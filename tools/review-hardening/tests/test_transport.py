import json
import stat
from types import SimpleNamespace

import pytest

from review_transport import private_json, request_once, strict_envelope


class Response:
    def __init__(self, data):
        self.body = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, bound):
        return self.body[:bound]


class FakeOpener:
    def __init__(self, data=None, failure=None):
        self.data, self.failure, self.calls = data, failure, []
    def open(self, req, timeout):
        self.calls.append((json.loads(req.data), timeout))
        if self.failure:
            raise self.failure
        return Response(self.data)


def response(content='{"findings": []}', finish='stop'):
    return {'model': 'LOCAL_TEST_MODEL', 'choices': [{'finish_reason': finish,
             'message': {'content': content, 'reasoning_content': 'DO_NOT_RETAIN_HIDDEN_THINKING'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20,
                      'completion_tokens_details': {'reasoning_tokens': 7}}}


def call(tmp_path, data, opener=None):
    route = SimpleNamespace(model='LOCAL_TEST_MODEL', key_env='', endpoint='http://127.0.0.1:46399/v1/chat/completions')
    payload = {'model': route.model, 'max_tokens': 4000, 'messages': [{'role': 'user', 'content': 'public fixture'}]}
    target = tmp_path / 'capture.json'
    return request_once(route, payload, target, opener or FakeOpener(data)), target


def test_receipt_preserves_controls_and_visible_output_not_reasoning(tmp_path):
    content = '<think>DO_NOT_RETAIN_INLINE_THINKING</think>{"findings": []}'
    (text, receipt), target = call(tmp_path, response(content))
    saved = target.read_text()
    assert 'DO_NOT_RETAIN' not in saved
    assert text == '{"findings": []}'
    assert receipt['tokens']['reasoning_tokens'] == 7 and receipt['tokens']['visible_tokens'] is None
    assert receipt['finish_reason'] == 'stop' and receipt['effective_controls'] is None
    assert receipt['budget_exhausted'] is None
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.with_suffix('.json.attempt').stat().st_mode) == 0o600


@pytest.mark.parametrize('literal', ['<think>source</think>', '<think>', '</think>',
                                     '<think kind="literal">source</think>'])
def test_reasoning_boundary_preserves_literal_tags_inside_json_evidence(tmp_path, literal):
    findings = [{'message': literal, 'evidence': [{'quote': 'return ' + repr(literal)}]}]
    content = json.dumps({'findings': findings})
    (text, receipt), _ = call(tmp_path, response('<think>DO_NOT_RETAIN</think>' + content))
    assert text == content
    assert json.loads(text)['findings'] == findings
    assert receipt['provider_content_sha256'] != receipt['visible_content_sha256']
    assert 'DO_NOT_RETAIN' not in (tmp_path / 'capture.json').read_text()


def test_trailing_reasoning_boundary_is_ambiguous_and_not_saved(tmp_path):
    with pytest.raises(RuntimeError):
        call(tmp_path, response('{"findings": []}<think>DO_NOT_RETAIN</think>'))
    saved = (tmp_path / 'capture.json').read_text()
    assert 'visible_content' not in json.loads(saved)
    assert 'DO_NOT_RETAIN' not in saved


@pytest.mark.parametrize('finish', ['length', None, 'content_filter', 'tool_calls', 'unknown'])
def test_unknown_and_partial_finish_abstains_with_preserved_visible_content(tmp_path, finish):
    with pytest.raises(RuntimeError):
        call(tmp_path, response(finish=finish))
    saved = json.loads((tmp_path / 'capture.json').read_text())
    assert saved['status'] == 'failed' and saved['visible_content'] == '{"findings": []}'


@pytest.mark.parametrize('content', ['{"findings": null}', '{"findings": [], "findings": []}',
                                   '{"findings": []} trailing', '{"findings": [], "score": NaN}',
                                   '<think>unterminated secret', None])
def test_schema_drift_and_unclosed_thinking_do_not_certify_clean(tmp_path, content):
    with pytest.raises(RuntimeError):
        call(tmp_path, response(content))
    saved = (tmp_path / 'capture.json').read_text()
    assert 'unterminated secret' not in saved
    assert json.loads(saved)['status'] == 'failed'


@pytest.mark.parametrize('failed', [True, False])
def test_completed_failed_and_uncertain_attempts_cannot_retry(tmp_path, failed):
    opener = FakeOpener(response(), RuntimeError('https://user:secret@host') if failed else None)
    if failed:
        with pytest.raises(RuntimeError):
            call(tmp_path, response(), opener)
    else:
        call(tmp_path, response(), opener)
    with pytest.raises(FileExistsError):
        call(tmp_path, response(), opener)
    assert len(opener.calls) == 1
    assert 'user:secret' not in (tmp_path / 'capture.json').read_text()


def test_provider_model_mismatch_is_preserved_and_refused(tmp_path):
    data = response()
    data['model'] = 'OTHER_MODEL'
    with pytest.raises(RuntimeError):
        call(tmp_path, data)
    assert json.loads((tmp_path / 'capture.json').read_text())['model_identity_match'] is False


def test_malicious_usage_is_never_echoed(tmp_path):
    data = response()
    data['usage'] = {'prompt_tokens': 'credential secret', 'completion_tokens': True}
    (_, receipt), _ = call(tmp_path, data)
    assert receipt['tokens']['prompt_tokens'] is None and receipt['tokens']['completion_tokens'] is None


def test_atomic_private_artifacts_refuse_symlink_and_do_not_modify_hardlink(tmp_path):
    original = tmp_path / 'original.json'
    original.write_text('preserve')
    link = tmp_path / 'symlink.json'
    link.symlink_to(original)
    with pytest.raises(ValueError):
        private_json(link, {'new': True})
    assert original.read_text() == 'preserve'
    hardlink = tmp_path / 'hardlink.json'
    hardlink.hardlink_to(original)
    private_json(hardlink, {'new': True})
    assert original.read_text() == 'preserve'
    assert stat.S_IMODE(hardlink.stat().st_mode) == 0o600


def test_strict_envelope_refuses_reasoning_as_final():
    with pytest.raises(ValueError):
        strict_envelope('<think>{"findings": []}</think>')


def test_outer_duplicate_finish_reason_cannot_hide_truncation(tmp_path):
    class RawResponse(Response):
        def __init__(self):
            self.body = b'{"choices":[{"finish_reason":"length","finish_reason":"stop","message":{"content":"{\\"findings\\":[]}"}}]}'
    class RawOpener:
        def open(self, *args, **kwargs):
            return RawResponse()
    with pytest.raises(RuntimeError):
        call(tmp_path, None, RawOpener())
    assert json.loads((tmp_path/'capture.json').read_text())['status'] == 'failed'


def test_exponent_overflow_is_not_valid_json_evidence():
    with pytest.raises(ValueError):
        strict_envelope('{"findings": [], "score": 1e309}')


def test_verbose_content_is_discarded_and_observed_controls_are_allowlisted(tmp_path):
    data = response()
    data['__verbose'] = {'prompt': 'DO_NOT_RETAIN_PROMPT', 'content': 'DO_NOT_RETAIN_REASONING',
                         'truncated': False, 'id_slot': 0, 'generation_settings': {
                         'n_ctx': 262144, 'n_predict': 8192, 'reasoning_budget_tokens': 2048,
                         'enable_thinking': True, 'reasoning_format': 'deepseek',
                         'grammar': 'DO_NOT_RETAIN_GRAMMAR'}}
    (_, receipt), target = call(tmp_path, data)
    assert 'DO_NOT_RETAIN' not in target.read_text()
    assert receipt['effective_controls']['reasoning_budget_tokens'] == 2048
    assert receipt['effective_controls']['evidence_origin'] == 'backend_response'
    assert receipt['server_input_truncated'] is False and receipt['id_slot'] == 0


def test_partial_verbose_metadata_does_not_invent_effective_budget(tmp_path):
    data = response()
    data['__verbose'] = {'generation_settings': {'n_ctx': 262144, 'n_predict': 8192,
                                                'reasoning_format': 'deepseek'}}
    (_, receipt), _ = call(tmp_path, data)
    assert receipt['effective_controls'] is None


def test_untagged_reasoning_after_json_is_hash_only(tmp_path):
    with pytest.raises(RuntimeError):
        call(tmp_path, response('{"findings": []}\nReasoning draft: DO_NOT_RETAIN_UNTAGGED'))
    saved = (tmp_path/'capture.json').read_text()
    assert 'DO_NOT_RETAIN' not in saved
    assert 'visible_content' not in json.loads(saved)
    assert 'provider_content_sha256' in json.loads(saved)


def test_conflicting_thinking_aliases_cannot_fabricate_effective_controls(tmp_path):
    data = response()
    data['__verbose'] = {'generation_settings': {'n_ctx': 262144, 'n_predict': 8192,
                          'reasoning_budget_tokens': 2048, 'enable_thinking': False,
                          'thinking_enabled': True}}
    (_, receipt), _ = call(tmp_path, data)
    assert receipt['effective_controls'] is None
    assert receipt['effective_control_conflicts'] == ['thinking_enabled']


def test_marker_and_receipt_entries_are_synced_before_dispatch(tmp_path, monkeypatch):
    import os
    import review_transport
    events = []
    real_sync = os.fsync
    def observed_sync(fd):
        events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        real_sync(fd)
    monkeypatch.setattr(review_transport.os, 'fsync', observed_sync)
    class CheckedOpener(FakeOpener):
        def open(self, req, timeout):
            assert events[:4] == ['file', 'directory', 'file', 'directory']
            events.append('dispatch')
            return super().open(req, timeout)
    opener = CheckedOpener(response())
    (_, receipt), _ = call(tmp_path, response(), opener)
    assert receipt['status'] == 'response_validated'
    assert events[-2:] == ['file', 'directory']
    assert len(opener.calls) == 1


@pytest.mark.parametrize('failure', ['marker_file', 'marker_directory', 'receipt_directory'])
def test_sync_failure_abstains_before_http_and_blocks_retry(tmp_path, monkeypatch, failure):
    import review_transport
    count = 0
    real_sync = review_transport.os.fsync
    fail_at = {'marker_file': 1, 'marker_directory': 2, 'receipt_directory': 4}[failure]
    def failing_sync(fd):
        nonlocal count
        count += 1
        if count == fail_at:
            raise OSError('synthetic durability failure')
        real_sync(fd)
    monkeypatch.setattr(review_transport.os, 'fsync', failing_sync)
    opener = FakeOpener(response())
    with pytest.raises(OSError):
        call(tmp_path, response(), opener)
    assert opener.calls == []
    with pytest.raises(FileExistsError):
        call(tmp_path, response(), opener)
    assert opener.calls == []
