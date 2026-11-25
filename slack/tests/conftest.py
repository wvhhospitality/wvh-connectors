"""Shared test setup. Every name in these tests is made up."""

import os

# The server reads its owner once, when it starts, so this has to be set before it loads.
os.environ.setdefault("WVH_SLACK_OWNER", "Robin")

import pytest


@pytest.fixture(autouse=True)
def _private_nicknames(tmp_path, monkeypatch):
    """Every test gets its own nickname file, so none ever touches a real one."""
    monkeypatch.setenv("WVH_NICKNAMES", str(tmp_path / "nicknames.yaml"))
    from wvh_slack_mcp import writes

    writes._RECENT_READS.clear()
    yield
    writes._RECENT_READS.clear()
