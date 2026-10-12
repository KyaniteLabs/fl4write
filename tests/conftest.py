"""Suite-wide test seams."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _host_test_isolation(monkeypatch, request):
    """The executor's mechanics tests (junit evidence, fix integrity, verify
    outcomes) drive mocked or trusted-fixture test runs through the HOST path.
    Production defaults to the Docker runtime and fails closed without an
    image; tests/test_test_isolation.py covers that default by opting out
    with the `default_isolation` marker."""
    if request.node.get_closest_marker("default_isolation") is None:
        monkeypatch.setenv("FL4WRITE_TEST_ISOLATION", "host")
    else:
        monkeypatch.delenv("FL4WRITE_TEST_ISOLATION", raising=False)
    monkeypatch.delenv("FL4WRITE_TEST_IMAGE", raising=False)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "default_isolation: run with production test-isolation defaults")
