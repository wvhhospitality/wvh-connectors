"""Offline tests: settings, the registry, and workspace resolution. No network, no Keychain."""

from pathlib import Path

import pytest

from wvh_slack_mcp import config as settings_module
from wvh_slack_mcp.config import Config, ConfigError, Workspace, load_config

PROJECT = Path(__file__).resolve().parents[1]


def _write(tmp_path, text):
    p = tmp_path / "workspaces.yaml"
    p.write_text(text)
    return p


def test_example_config_parses(tmp_path):
    example = (PROJECT / "workspaces.example.yaml").read_text()
    cfg = load_config(_write(tmp_path, example))
    assert cfg.keychain_service == "WvHSlack"
    assert {w.client_code for w in cfg.workspaces} == {"JNV", "KL"}


def test_this_macs_own_settings_parse():
    live = settings_module.resolve_config_path()
    if not live.exists():
        pytest.skip("no settings on this Mac")
    codes = [w.client_code for w in load_config(live).workspaces]
    assert len(codes) == len(set(codes)), "two workspaces share a code in this Mac's settings"


def test_settings_live_outside_the_app(monkeypatch, tmp_path):
    monkeypatch.delenv("WVH_SLACK_CONFIG", raising=False)
    monkeypatch.delenv("WVH_SLACK_HOME", raising=False)
    default = settings_module.resolve_config_path()
    assert default == Path.home() / "Library/Application Support/wvh-slack/workspaces.yaml"
    assert PROJECT not in default.parents, "settings must never sit beside the code"

    monkeypatch.setenv("WVH_SLACK_HOME", str(tmp_path))
    assert settings_module.resolve_config_path() == tmp_path / "workspaces.yaml"
    assert settings_module.connections_path() == tmp_path / "connections.json"
    assert settings_module.downloads_dir() == tmp_path / "downloads"

    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "elsewhere" / "w.yaml"))
    assert settings_module.resolve_config_path() == tmp_path / "elsewhere" / "w.yaml"


def test_owner_name_comes_from_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("WVH_SLACK_OWNER", raising=False)
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    assert settings_module.owner_name() == "the owner" and settings_module.owner_cap() == "The owner"

    _write(tmp_path, "owner_name: Sam\n")
    assert settings_module.owner_name() == "Sam"

    monkeypatch.setenv("WVH_SLACK_OWNER", "Jo")
    assert settings_module.owner_name() == "Jo", "the environment setting wins"


def test_time_zone_setting_and_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("WVH_SLACK_TIMEZONE", raising=False)
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    _write(tmp_path, "timezone: America/Chicago\n")
    assert settings_module.local_timezone().key == "America/Chicago"

    monkeypatch.setenv("WVH_SLACK_TIMEZONE", "Not/AZone")
    assert settings_module.local_timezone().key, "a bad value falls back to this Mac's zone, never an error"


def test_no_one_persons_name_is_written_into_the_tools():
    """Every description names the configured owner, so the same app serves anyone."""
    from wvh_slack_mcp import remote, server

    text = server.INSTRUCTIONS + remote.REMOTE_INSTRUCTIONS
    text += " ".join(t.description + str(t.inputSchema) for t in server.READ_TOOLS + server.WRITE_TOOL_DEFS + [server.CONFIRM_TOOL])
    assert "Robin" in text, "the test owner's name should appear"
    assert "owner_reply" in str(server.CONFIRM_TOOL.inputSchema)


def test_find_by_code_name_and_domain(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
workspaces:
  - client_code: JNV
    name: Juniper & Vine
    team_domain: junipernvine
""",
        )
    )
    assert cfg.find("jnv").client_code == "JNV"
    assert cfg.find("Juniper & Vine").client_code == "JNV"
    assert cfg.find("junipernvine").client_code == "JNV"
    with pytest.raises(ConfigError):
        cfg.find("nope")


def test_dormant_registry_loads_but_resolves_with_a_hint(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
workspaces:
  - client_code: JNV
    name: Juniper & Vine
    enabled: false
""",
        )
    )
    assert cfg.workspaces and not cfg.enabled  # known, but nothing live yet
    with pytest.raises(ConfigError, match="isn't connected yet"):
        cfg.find("JNV")


def test_dispatch_reports_dormant_workspaces_without_network(tmp_path, monkeypatch):
    from wvh_slack_mcp import server

    cfg = load_config(
        _write(
            tmp_path,
            """
workspaces:
  - client_code: JNV
    name: Juniper & Vine
    enabled: false
""",
        )
    )
    monkeypatch.setattr(server.state, "_config", cfg)
    monkeypatch.setattr(server.state, "_pool", None)
    monkeypatch.setattr(server, "detect_workspaces", lambda: [])
    out = server._dispatch("list_workspaces", {})
    assert out["summary"]["reachable"] == 0
    assert out["summary"]["dormant"] == 1
    assert out["workspaces"][0]["status"] == "dormant"


def test_registry_hot_reloads_when_file_changes(tmp_path, monkeypatch):
    import os

    from wvh_slack_mcp import server

    p = _write(
        tmp_path,
        """
workspaces:
  - client_code: CPK
    name: Copper Kettle
    enabled: false
""",
    )
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(p))
    monkeypatch.setattr(server.state, "_config", None)
    monkeypatch.setattr(server.state, "_pool", None)
    monkeypatch.setattr(server, "detect_workspaces", lambda: [])

    assert server._dispatch("list_workspaces", {})["workspaces"][0]["status"] == "dormant"

    # Enable it on disk, bump mtime, and dispatch again — no restart.
    p.write_text(p.read_text().replace("enabled: false", "enabled: true"))
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 10))

    row = server._dispatch("list_workspaces", {"probe": False})["workspaces"][0]
    assert row["status"] == "enabled", "registry change should be picked up without a restart"


def test_a_new_install_starts_empty_and_says_what_to_do(tmp_path, monkeypatch):
    from wvh_slack_mcp import server
    from wvh_slack_mcp.detect import Detected

    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "wvh-slack" / "workspaces.yaml"))
    monkeypatch.setattr(server.state, "_config", None)
    monkeypatch.setattr(server.state, "_pool", None)
    monkeypatch.setattr(server, "detect_workspaces", lambda: [Detected("T_K", "KiteLoop", "kiteloop")])

    cfg = load_config()
    assert cfg.workspaces == [] and cfg.client_id is None, "no settings file is a fresh start, not an error"

    reply = server._dispatch("list_workspaces", {})
    assert reply["workspaces"] == [
        {"client_code": None, "name": "KiteLoop", "team_domain": "kiteloop", "status": "available", "detail": "Say: connect KiteLoop"}
    ]
    assert "wvh-slack-connect setup" in reply["summary"]["next_step"]
    assert not (tmp_path / "wvh-slack").exists(), "looking must not create anything"


def test_a_broken_settings_file_is_reported(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "workspaces: [unclosed"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "- just\n- a list\n"))


def test_workspace_label():
    assert Workspace(client_code="X").label == "X"
    assert Workspace(client_code="X", name="Xylo").label == "Xylo"


def test_no_write_tool_is_offered_by_default():
    from wvh_slack_mcp import server, writes

    names = {t.name for t in server.TOOLS}
    assert "read_file" in names and "find_person" in names
    assert not names & set(writes.SPECS) and "confirm_write" not in names
