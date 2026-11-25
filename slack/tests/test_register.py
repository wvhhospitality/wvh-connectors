"""tools/claude_register.py: telling Claude about a connector without the app undoing it.

Every name here is made up. Each test runs in an empty, throwaway home folder.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "claude_register.py"


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("claude_register", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = []
    monkeypatch.setattr(module, "_detach", lambda change_file: started.append(change_file))
    monkeypatch.setattr(module, "claude_is_open", lambda: False)
    module.started = started
    return module


@pytest.fixture()
def program(tmp_path):
    path = tmp_path / "wvh-connectors" / "slack" / ".venv" / "bin" / "wvh-slack-mcp"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def _desktop(tmp_path, servers=None, extra=None):
    path = tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"mcpServers": servers or {}, **(extra or {})}
    path.write_text(json.dumps(data))
    return path


def _code(tmp_path, servers=None):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"numStartups": 3, "mcpServers": servers or {}}))
    return path


def test_add_while_closed_changes_both_and_keeps_everything_else(reg, tmp_path, program):
    desk = _desktop(tmp_path, {"notes": {"command": "/bin/true", "args": []}}, {"preferences": {"theme": "dark"}})
    code = _code(tmp_path)
    assert reg.main(["add", "--name", "robin-slack", "--command", program, "--env", "WVH_SLACK_ALLOW_WRITES=1"]) == 0
    d = json.loads(desk.read_text())
    assert d["preferences"] == {"theme": "dark"}
    assert d["mcpServers"]["notes"] == {"command": "/bin/true", "args": []}
    assert d["mcpServers"]["robin-slack"] == {"command": program, "args": [], "env": {"WVH_SLACK_ALLOW_WRITES": "1"}}
    c = json.loads(code.read_text())
    assert c["numStartups"] == 3 and c["mcpServers"]["robin-slack"]["command"] == program
    assert list(desk.parent.glob("claude_desktop_config.json.bak-*")), "a backup is made before the first change"
    assert not reg.started


def test_add_while_open_waits_and_changes_nothing_yet(reg, tmp_path, program, monkeypatch, capsys):
    desk = _desktop(tmp_path)
    monkeypatch.setattr(reg, "claude_is_open", lambda: True)
    assert reg.main(["add", "--name", "robin-slack", "--command", program]) == 0
    assert json.loads(desk.read_text())["mcpServers"] == {}, "nothing is written while the app is open"
    assert len(reg.started) == 1
    pending = json.loads(Path(reg.started[0]).read_text())
    assert pending["name"] == "robin-slack" and pending["command"] == program
    assert "quit Claude" in capsys.readouterr().out


def test_the_waiting_step_applies_once_closed_then_reopens(reg, tmp_path, program, monkeypatch):
    desk = _desktop(tmp_path)
    monkeypatch.setattr(reg, "claude_is_open", lambda: True)
    reg.main(["add", "--name", "robin-slack", "--command", program])
    change_file = Path(reg.started[0])
    states = iter([True, True, False])
    monkeypatch.setattr(reg, "claude_is_open", lambda: next(states, False))
    monkeypatch.setattr(reg.time, "sleep", lambda s: None)
    opened = []
    monkeypatch.setattr(reg.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    reg._wait_then_apply(change_file)
    assert json.loads(desk.read_text())["mcpServers"]["robin-slack"]["command"] == program
    assert opened == [["open", "-a", "Claude"]]
    assert not change_file.exists()
    assert "robin-slack added" in (tmp_path / "Library" / "Logs" / "wvh-connectors-register.log").read_text()


def test_the_waiting_step_gives_up_without_changing_anything(reg, tmp_path, program, monkeypatch):
    desk = _desktop(tmp_path)
    monkeypatch.setattr(reg, "claude_is_open", lambda: True)
    reg.main(["add", "--name", "robin-slack", "--command", program])
    monkeypatch.setattr(reg, "WAIT_SECONDS", 0)
    opened = []
    monkeypatch.setattr(reg.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    reg._wait_then_apply(Path(reg.started[0]))
    assert json.loads(desk.read_text())["mcpServers"] == {}
    assert opened == []


def test_ask_first_list_is_added_and_kept(reg, tmp_path, program):
    _desktop(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"ask": ["mcp__other__thing"], "allow": ["Read"]}, "model": "x"}))
    reg.main(["add", "--name", "robin-slack", "--command", program, "--ask", "confirm_write"])
    reg.main(["add", "--name", "robin-slack", "--command", program, "--ask", "confirm_write"])
    s = json.loads(settings.read_text())
    assert s["permissions"]["ask"] == ["mcp__other__thing", "mcp__robin-slack__confirm_write"]
    assert s["permissions"]["allow"] == ["Read"] and s["model"] == "x"


def test_remove_takes_out_only_that_connector(reg, tmp_path, program):
    desk = _desktop(tmp_path, {"robin-slack": {"command": program, "args": []}, "notes": {"command": "/bin/true", "args": []}})
    reg.main(["remove", "--name", "robin-slack", "--desktop-only"])
    assert list(json.loads(desk.read_text())["mcpServers"]) == ["notes"]


@pytest.mark.parametrize("bad", ["Robin Slack", "robin_slack", "-x", "../x", ""])
def test_names_are_plain(reg, program, bad):
    assert reg.main(["add", f"--name={bad}", "--command", program]) == 2


def test_a_login_never_goes_in_claudes_settings(reg, tmp_path, program):
    _desktop(tmp_path)
    with pytest.raises(SystemExit):
        reg.main(["add", "--name", "robin-slack", "--command", program, "--env", "SLACK_TOKEN=abc"])


def test_missing_program_is_refused(reg, tmp_path):
    _desktop(tmp_path)
    assert reg.main(["add", "--name", "robin-slack", "--command", str(tmp_path / "nothing")]) == 1


def test_a_broken_settings_file_is_never_overwritten(reg, tmp_path, program):
    desk = _desktop(tmp_path)
    desk.write_text("{ not json")
    with pytest.raises(ValueError):
        reg.main(["add", "--name", "robin-slack", "--command", program, "--desktop-only"])
    assert desk.read_text() == "{ not json"


def test_no_claude_installed_skips_quietly(reg, tmp_path, program, capsys):
    assert reg.main(["add", "--name", "robin-slack", "--command", program]) == 0
    out = capsys.readouterr().out
    assert "not installed on this Mac" in out and "not set up on this Mac" in out
    assert not (tmp_path / "Library" / "Application Support" / "Claude").exists()


def test_program_arguments_are_kept_in_order(reg, tmp_path, program):
    desk = _desktop(tmp_path)
    reg.main(["add", "--name", "robin-slack", "--command", program, "--arg=-m", "--arg", "some.module", "--arg=--stdio", "--desktop-only"])
    assert json.loads(desk.read_text())["mcpServers"]["robin-slack"]["args"] == ["-m", "some.module", "--stdio"]


def test_several_waiting_connectors_all_land_and_claude_reopens_once(reg, tmp_path, program, monkeypatch):
    desk = _desktop(tmp_path, {"notes": {"command": "/bin/true", "args": []}})
    monkeypatch.setattr(reg, "claude_is_open", lambda: True)
    for name in ("robin-slack", "robin-mail", "robin-texts"):
        reg.main(["add", "--name", name, "--command", program, "--desktop-only"])
    assert len(reg.started) == 3
    monkeypatch.setattr(reg, "claude_is_open", lambda: False)
    monkeypatch.setattr(reg.time, "sleep", lambda s: None)
    opened = []
    monkeypatch.setattr(reg.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    for change_file in reg.started:  # each waiting step wakes when Claude closes
        reg._wait_then_apply(Path(change_file))
    assert sorted(json.loads(desk.read_text())["mcpServers"]) == ["notes", "robin-mail", "robin-slack", "robin-texts"]
    assert opened == [["open", "-a", "Claude"]], "Claude reopens once, after the last change"
    assert not list((tmp_path / "Library" / "Application Support" / "wvh-connectors" / "pending").glob("*.json"))


def test_a_waiting_step_does_nothing_if_claude_came_back(reg, tmp_path, program, monkeypatch):
    desk = _desktop(tmp_path)
    monkeypatch.setattr(reg, "claude_is_open", lambda: True)
    reg.main(["add", "--name", "robin-slack", "--command", program, "--desktop-only"])
    # Claude closes for a moment, then is back before the change is made: wait for the next quit.
    states = iter([False, True])
    monkeypatch.setattr(reg, "claude_is_open", lambda: next(states, True))
    monkeypatch.setattr(reg.time, "sleep", lambda s: None)
    monkeypatch.setattr(reg, "WAIT_SECONDS", 0.05)
    reg._wait_then_apply(Path(reg.started[0]))
    assert json.loads(desk.read_text())["mcpServers"] == {}
