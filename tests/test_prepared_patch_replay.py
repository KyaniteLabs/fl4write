"""Prepared source is data, not another model response to clean or reinterpret."""
import json

import pytest

from fl4write import exhaustive_fix as repair
from test_exhaustive_fix import _config


@pytest.mark.parametrize("source", [
    '# <think>ordinary documentation</think>\ndef value(): return 2\n',
    'DOC = "<think>example text</think>"\ndef value(): return 2\n',
])
def test_cached_complete_source_is_byte_identical_on_retry(tmp_path, monkeypatch, source):
    repo, evidence = tmp_path / "repo", tmp_path / "evidence"
    repo.mkdir()
    evidence.mkdir()
    files = {"module.py": source, "test_module.py": "assert value() == 2\n"}
    pins = {"test_module.py"}
    calls = []
    def generate(*args):
        calls.append(True)
        return dict(files), set(pins)
    monkeypatch.setattr(repair, "_model_patch", generate)
    args = (_config(), "a" * 40, [{"path": "module.py"}], repo, evidence, ["pytest"])
    first = repair._prepared_patch(*args)
    stored = (evidence / "prepared-patch.json").read_bytes()
    second = repair._prepared_patch(*args)
    assert first == second == (files, pins)
    assert len(calls) == 1
    assert (evidence / "prepared-patch.json").read_bytes() == stored


def test_first_compact_generation_preserves_literal_source_text(tmp_path, monkeypatch):
    from fl4write import analyzer
    source = '# <think>ordinary documentation</think>\ndef value(): return 1\n'
    (tmp_path / "module.py").write_text(source)
    response = {"files": [
        {"path": "module.py", "edits": [{"old": "return 1", "new": "return 2"}], "regression": False},
        {"path": "test_module.py", "content": "assert value() == 2\n", "regression": True},
    ]}
    monkeypatch.setattr(analyzer, "_call_model", lambda *args, **kwargs: json.dumps(response))
    files, pins = repair._model_patch(_config(), "a" * 40, [{"path": "module.py"}], tmp_path)
    assert files["module.py"] == source.replace("return 1", "return 2")
    assert pins == {"test_module.py"}


@pytest.mark.parametrize("invalid", [None, {"files": []}, {"files": [
    {"path": "module.py", "content": "x", "regression": "false"},
    {"path": "test_module.py", "content": "y", "regression": True},
]}])
def test_cached_patch_still_requires_valid_schema(tmp_path, monkeypatch, invalid):
    monkeypatch.setattr(repair, "_model_patch", lambda *args: (
        {"module.py": "return_value = 2\n", "test_module.py": "assert True\n"}, {"test_module.py"}))
    args = (_config(), "a" * 40, [{"path": "module.py"}], tmp_path, tmp_path, ["pytest"])
    repair._prepared_patch(*args)
    cache = tmp_path / "prepared-patch.json"
    saved = json.loads(cache.read_bytes())
    saved["patch"] = invalid
    cache.chmod(0o600)
    cache.write_text(json.dumps(saved))
    with pytest.raises(repair.FixError):
        repair._prepared_patch(*args)


def test_first_model_json_preserves_new_regression_literal_text(tmp_path, monkeypatch):
    from fl4write import analyzer
    (tmp_path / "module.py").write_text("def value(): return 1\n")
    regression = "EXPECTED = '<think>ordinary text</think>'\nassert EXPECTED == '<think>ordinary text</think>'\n"
    response = {"files": [
        {"path": "module.py", "edits": [{"old": "return 1", "new": "return 2"}], "regression": False},
        {"path": "test_module.py", "content": regression, "regression": True},
    ]}
    monkeypatch.setattr(analyzer, "_call_model", lambda *args, **kwargs: json.dumps(response))
    files, pins = repair._model_patch(_config(), "a" * 40, [{"path": "module.py"}], tmp_path)
    assert files["test_module.py"] == regression
    assert pins == {"test_module.py"}


def test_patch_response_still_accepts_reasoning_preamble():
    patch = {"files": [
        {"path": "module.py", "content": "VALUE = 2\n", "regression": False},
        {"path": "test_module.py", "content": "assert VALUE == 2\n", "regression": True},
    ]}
    files, pins = repair._parse_patch("<think>Consider the correction.</think>\n" + json.dumps(patch))
    assert files["module.py"] == "VALUE = 2\n" and pins == {"test_module.py"}


@pytest.mark.parametrize("duplicate", [
    '{"files":[],"files":[]}',
    '{"files":[{"path":"module.py","content":"x","content":"y","regression":false},'
    '{"path":"test_module.py","content":"z","regression":true}]}',
])
def test_strict_model_patch_rejects_duplicate_keys(duplicate):
    with pytest.raises(ValueError):
        repair._parse_patch(duplicate)


@pytest.mark.parametrize("wrapper", [
    "<think>Consider correction.</think>\n{}",
    "```json\n{}\n```",
    "<think>Consider correction.</think>\n```json\n{}\n```",
])
@pytest.mark.parametrize("compact", [False, True])
def test_wrapped_patch_preserves_literal_source_in_content_and_edits(wrapper, compact):
    old = "<think>ordinary text</think>"
    new = "<think>corrected text</think>"
    regression = f"EXPECTED = {new!r}\nassert EXPECTED == {new!r}\n"
    implementation = ({"edits": [{"old": old, "new": new}]} if compact else
                      {"content": f"VALUE = {new!r}\n"})
    patch = {"files": [
        {"path": "module.py", **implementation, "regression": False},
        {"path": "test_module.py", "content": regression, "regression": True},
    ]}
    files, pins = repair._parse_patch(wrapper.format(json.dumps(patch)),
                                      {"module.py": f"VALUE = {old!r}\n"})
    assert files["module.py"] == f"VALUE = {new!r}\n"
    assert files["test_module.py"] == regression
    assert pins == {"test_module.py"}


@pytest.mark.parametrize("response", [
    '<think>{"files": []}',
    '```json\n{"files": []}',
    '{"files": []}\n{"files": [{"different": true}]}',
    '<think>{"files": []}</think>',
])
def test_incomplete_or_multiple_patch_envelopes_fail_closed(response):
    with pytest.raises((ValueError, repair.FixError)):
        repair._parse_patch(response)
