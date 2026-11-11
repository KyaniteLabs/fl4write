"""
Scratch self-test fixture for the FL4WRITE failure loop (kanban card E3).

This file exists on a scratch branch only and must never reach main. It is
deliberately red so the failure loop has a real failure to consume, and it
echoes a fake credential from the environment so the loop's scrub step can be
proven on real captured output.
"""

import os


def test_injected_wrong_assertion():
    print("GITHUB_TOKEN=" + os.environ.get("SELFTEST_FAKE_SECRET", ""))
    assert 2 + 2 == 4
