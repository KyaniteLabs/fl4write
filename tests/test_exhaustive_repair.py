"""Adversarial regression evidence for PR15 findings."""
import subprocess
import hashlib
import json
import os
from pathlib import Path

import pytest

from fl4write import exhaustive as ex
from fl4write.exhaustive_evidence import seal_bundle, verify_bundle, EvidenceError


def test_git_warning_does_not_become_a_dirty_file(tmp_path):
    binary = tmp_path / "git"
    binary.write_text("#!/bin/sh\necho 'warning: temporary directory fallback' >&2\nexit 0\n")
    binary.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / "check-dirty.sh"
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                                 "FL4WRITE_CHECKOUT": str(tmp_path)})
    assert result.returncode == 0 and result.stdout.strip() == "clean"
    assert "warning:" in result.stderr


@pytest.mark.parametrize("counter", ["failures", "errors", "skipped", "tests"])
def test_root_aggregate_cannot_hide_disagreement(tmp_path, counter):
    path = tmp_path / "suite.xml"
    path.write_text(f'<testsuites {counter}="2"><testsuite tests="1">'
                    '<testcase classname="c" name="n"/></testsuite></testsuites>')
    with pytest.raises(ex.NonGreen, match="aggregate"):
        ex._junit(path)


def test_nested_junit_aggregates_and_red_cases(tmp_path):
    path = tmp_path / "suite.xml"
    path.write_text('<testsuites tests="1" failures="1"><testsuite tests="1" failures="1">'
                    '<testsuite tests="1" failures="1"><testcase classname="c" name="n">'
                    '<failure message="bad"/></testcase></testsuite></testsuite></testsuites>')
    ids, green, digest = ex._junit(path)
    assert ids == {"c::n"} and not green and len(digest) == 64


@pytest.mark.parametrize("xml", [
    '<testsuites xmlns="urn:unknown"><testsuite tests="1"><testcase name="n"/></testsuite></testsuites>',
    '<testsuite tests="+1"><testcase name="n"/></testsuite>',
    '<testsuite tests="1"><testcase name="n"><failure/><skipped/></testcase></testsuite>',
    '<testsuite tests="2"><testcase name="n"/><testcase name="n"/></testsuite>',
])
def test_ambiguous_junit_never_green(tmp_path, xml):
    path = tmp_path / "suite.xml"
    path.write_text(xml)
    with pytest.raises(ex.NonGreen):
        ex._junit(path)


def test_runner_unavailability_stays_infrastructure_deferral(tmp_path):
    with pytest.raises(ex.Deferred, match="process unavailable"):
        ex._test([str(tmp_path / "missing-runner"), "{junit}"], tmp_path, tmp_path / "suite.xml", 2)


def test_runner_timeout_stays_infrastructure_deferral(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("runner", 1)
    monkeypatch.setattr(ex.subprocess, "run", timeout)
    with pytest.raises(ex.Deferred, match="process unavailable"):
        ex._test(["runner", "{junit}"], tmp_path, tmp_path / "suite.xml", 1)


def test_evidence_is_content_addressed_and_rechecked(tmp_path):
    source = tmp_path / "round"
    source.mkdir()
    (source / "head.tar").write_bytes(b"reviewed archive")
    (source / "manifest.json").write_text(json.dumps({
        "head": "a" * 40,
        "archive_sha256": hashlib.sha256(b"reviewed archive").hexdigest(),
    }))
    (source / "full-suite.xml").write_text('<testsuite tests="1"><testcase name="n"/></testsuite>')
    reference = seal_bundle(source, tmp_path / "bundles")
    paths = verify_bundle(reference)
    assert all(not (p.stat().st_mode & 0o222) for p in paths.values())
    paths["full-suite.xml"].chmod(0o600)
    with pytest.raises(EvidenceError, match="mutable"):
        verify_bundle(reference)
    paths["full-suite.xml"].write_text("tampered")
    paths["full-suite.xml"].chmod(0o400)
    with pytest.raises(EvidenceError, match="digest"):
        verify_bundle(reference)
