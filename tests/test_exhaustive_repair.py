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
    '<testsuites xmlns:t="urn:unknown"><testsuite tests="1"><t:testcase name="n"/></testsuite></testsuites>',
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


def _sealable_source(base, extra):
    import hashlib as _hl
    import json as _j
    base.mkdir()
    (base / "head.tar").write_bytes(b"reviewed archive")
    (base / "manifest.json").write_text(_j.dumps({
        "head": "a" * 40,
        "archive_sha256": _hl.sha256(b"reviewed archive").hexdigest()}))
    extra(base)
    return base


def test_extra_bundle_directory_refused(tmp_path):
    """Cycle-6: directories were checked for writability but never
    compared — an extra (even mode-locked) dir passed silently."""
    from fl4write.exhaustive_evidence import EvidenceError, seal_bundle, verify_bundle
    source = tmp_path / "round"
    source.mkdir()
    (source / "head.tar").write_bytes(b"reviewed archive")
    (source / "manifest.json").write_text("{}")
    ref = seal_bundle(source, tmp_path / "bundles")
    bundle = Path(ref["path"])
    # Simulate a writer planting a dir, then restoring canonical perms:
    # verify must compare directory sets, not just writability.
    bundle.chmod(0o700)
    extra = bundle / "extra_dir"
    extra.mkdir(mode=0o500)
    bundle.chmod(0o500)
    with pytest.raises(EvidenceError, match="directories changed"):
        verify_bundle(ref)


def test_nested_bundle_directories_verify(tmp_path):
    """Review round 2 (PR46): parents-only derivation refused
    legitimate nested bundles — ancestors must all be expected."""
    from fl4write.exhaustive_evidence import seal_bundle, verify_bundle
    import hashlib as _hl
    import json as _j
    source = tmp_path / "round"
    source.mkdir()
    (source / "head.tar").write_bytes(b"reviewed archive")
    (source / "manifest.json").write_text(_j.dumps({
        "head": "a" * 40,
        "archive_sha256": _hl.sha256(b"reviewed archive").hexdigest()}))
    deep = source / "sub" / "inner"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("deep evidence")
    ref = seal_bundle(source, tmp_path / "bundles")
    sealed = verify_bundle(ref)
    assert "sub/inner/deep.txt" in sealed


def test_empty_archive_refused_at_seal(tmp_path):
    """Cycle-6: an empty head.tar is meaningless evidence — refuse it
    instead of sealing and verifying vacuity."""
    from fl4write.exhaustive_evidence import EvidenceError, seal_bundle
    source = tmp_path / "round"
    source.mkdir()
    (source / "head.tar").write_bytes(b"")
    (source / "manifest.json").write_text("{}")
    with pytest.raises(EvidenceError, match="empty"):
        seal_bundle(source, tmp_path / "bundles")


def test_top_level_tree_file_seals_while_tree_dir_skipped(tmp_path):
    """Cycle-6: a regular file literally named "tree" is evidence and
    must seal; the archived-tree directory stays out of scope."""
    from fl4write.exhaustive_evidence import seal_bundle, verify_bundle
    src_file = _sealable_source(
        tmp_path / "r1", lambda b: (b / "tree").write_text("evidence file"))
    assert "tree" in verify_bundle(seal_bundle(src_file, tmp_path / "b1"))
    src_dir = _sealable_source(tmp_path / "r2", lambda b: (
        b / "tree").mkdir() or (b / "tree" / "x").write_text("x"))
    sealed = verify_bundle(seal_bundle(src_dir, tmp_path / "b2"))
    assert "tree" not in sealed and "tree/x" not in sealed
