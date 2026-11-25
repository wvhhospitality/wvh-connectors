#!/usr/bin/env python3
"""Tell Claude about a connector, safely, even while Claude is open.

The Claude desktop app keeps its settings file in memory and writes its own copy
back while it runs and when it quits. A change made to that file while the app
is open is quietly undone. So this tool changes the file only while Claude is
closed: if Claude is open, it waits in the background, makes the change the
moment you quit Claude, and opens Claude again for you.

    python3 claude_register.py add --name robin-slack \\
        --command ~/wvh-connectors/slack/.venv/bin/wvh-slack-mcp \\
        [--env WVH_SLACK_ALLOW_WRITES=1] [--ask confirm_write]
        [--arg=-m --arg some.module]   (arguments for the program, if it takes any)
    python3 claude_register.py status --name robin-slack
    python3 claude_register.py remove --name robin-slack

What it changes, and nothing else:
  * the Claude desktop app's list of connectors (claude_desktop_config.json)
  * Claude Code's list of connectors for this user (~/.claude.json)
  * with --ask, Claude Code's "ask me first" list (~/.claude/settings.json)
Every other entry is kept exactly as it was, and each file gets a dated .bak
copy beside it before the first change. It needs nothing beyond Python.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
APP_BINARY = os.environ.get("WVH_REGISTER_APP") or "/Applications/Claude.app/Contents/MacOS/Claude"  # override is for tests
WAIT_SECONDS = 60 * 60  # give up after an hour if Claude is never quit


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def desktop_config() -> Path:
    return home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def code_config() -> Path:
    return home() / ".claude.json"


def code_settings() -> Path:
    return home() / ".claude" / "settings.json"


def log_path() -> Path:
    return home() / "Library" / "Logs" / "wvh-connectors-register.log"


def pending_dir() -> Path:
    return home() / "Library" / "Application Support" / "wvh-connectors" / "pending"


def _log(message: str) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')} {message}\n")


# ─── Reading and writing the settings files ───────────────────────────────


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    data = json.loads(text)  # a broken file stops everything: never guess at someone's settings
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a settings file this tool understands")
    return data


def _backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = _dt.date.today().strftime("%-m.%-d.%y")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    if not target.exists():  # keep the first copy of the day: it is the one from before any change
        shutil.copy2(path, target)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def entry(command: str, env: dict[str, str], args: list[str] | None = None) -> dict:
    item: dict = {"command": command, "args": list(args or [])}
    if env:
        item["env"] = dict(env)
    return item


def apply_change(change: dict) -> list[str]:
    """Make one change to the files. Returns what was done, in plain words."""
    name, action = change["name"], change["action"]
    done: list[str] = []
    targets = []
    if change.get("desktop", True):
        targets.append(("the Claude desktop app", desktop_config(), "desktop"))
    if change.get("code", True):
        targets.append(("Claude Code", code_config(), "code"))
    for label, path, kind in targets:
        if kind == "desktop" and not path.parent.exists():
            done.append(f"{label}: not installed on this Mac, skipped")
            continue
        if kind == "code" and not path.exists():
            done.append(f"{label}: not set up on this Mac, skipped")
            continue
        data = _read_json(path)
        servers = data.setdefault("mcpServers", {})
        if action == "add":
            new = entry(change["command"], change.get("env") or {}, change.get("args"))
            if servers.get(name) == new:
                done.append(f"{label}: {name} was already there")
                continue
            _backup(path)
            servers[name] = new
            _write_json(path, data)
            done.append(f"{label}: {name} added")
        elif action == "remove":
            if name not in servers:
                done.append(f"{label}: {name} was not there")
                continue
            _backup(path)
            del servers[name]
            _write_json(path, data)
            done.append(f"{label}: {name} removed")
    return done


def set_ask(name: str, tools: list[str], remove: bool = False) -> list[str]:
    """Claude Code asks before each use of these tools. This file is safe to change while Claude is open."""
    if not tools:
        return []
    path = code_settings()
    data = _read_json(path)
    perms = data.setdefault("permissions", {})
    ask = perms.setdefault("ask", [])
    wanted = [f"mcp__{name}__{tool}" for tool in tools]
    before = list(ask)
    if remove:
        ask[:] = [item for item in ask if item not in wanted]
    else:
        ask.extend(item for item in wanted if item not in ask)
    if ask == before:
        return [f"ask-first list: already {'clear of' if remove else 'has'} {', '.join(wanted)}"]
    _backup(path)
    _write_json(path, data)
    return [f"ask-first list: {'removed' if remove else 'added'} {', '.join(wanted)}"]


# ─── Waiting for Claude to close ──────────────────────────────────────────


def claude_is_open() -> bool:
    # ps, not pgrep: pgrep -f skips processes whose arguments it can't read, and the app is one.
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(line.strip() == APP_BINARY for line in out.stdout.splitlines())


def _apply_pending() -> bool:
    """Make every waiting change, oldest first, while Claude is closed. Returns True if any were made.

    Several connectors may be waiting for the same quit (Slack, mail and texts set up in one
    sitting). One waiting step makes all the changes, under a lock, so no change is lost to
    another's write, and Claude is reopened once, after the last one.
    """
    import fcntl

    folder = pending_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiting = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not waiting or claude_is_open():
            return False  # nothing left, or Claude is already back: the next quit will do
        for change_file in waiting:
            try:
                for line in apply_change(json.loads(change_file.read_text())):
                    _log(line)
            except Exception as exc:  # recorded in the log; there is no screen to show it on
                _log(f"could not make the change in {change_file.name}: {exc}")
            finally:
                change_file.unlink(missing_ok=True)
        return True


def _wait_then_apply(change_file: Path) -> None:
    """Runs on its own, in the background, after the person's chat has gone."""
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if not change_file.exists():
            return  # another waiting step already made this change
        if not claude_is_open():
            time.sleep(2)  # let the app finish writing its own copy before ours goes in
            if _apply_pending():
                if os.environ.get("WVH_REGISTER_NO_REOPEN") != "1":
                    subprocess.run(["open", "-a", "Claude"], check=False)
                    _log("Claude reopened")
                return
            if not change_file.exists():
                return
        time.sleep(1)
    if change_file.exists():
        _log(f"Claude was not quit within an hour; nothing changed ({change_file.name})")
        change_file.unlink(missing_ok=True)


def _detach(change_file: Path) -> None:
    """Start the waiting step so it outlives this chat, and the app, when the person quits Claude."""
    script = str(Path(__file__).resolve())
    python = sys.executable or "/usr/bin/python3"
    subprocess.Popen(
        [python, "-I", script, "_apply-when-closed", str(change_file)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True, cwd=str(home()),
    )


def schedule(change: dict) -> str:
    folder = pending_dir()
    folder.mkdir(parents=True, exist_ok=True)
    change_file = folder / f"{change['name']}-{int(time.time())}.json"
    change_file.write_text(json.dumps(change))
    change_file.chmod(0o600)
    _detach(change_file)
    _log(f"waiting for Claude to close: {change['action']} {change['name']}")
    return str(change_file)


# ─── Commands ─────────────────────────────────────────────────────────────


def _parse_env(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not ENV_KEY.match(key):
            raise SystemExit(f"--env takes NAME=value with a capitalised NAME, not {pair!r}")
        if re.search(r"(?i)(token|secret|password|key)", key):
            raise SystemExit("A login never goes in Claude's settings. The connectors keep logins in the Keychain.")
        env[key] = value
    return env


def _status(name: str) -> int:
    for label, path in (("Claude desktop app", desktop_config()), ("Claude Code", code_config())):
        try:
            servers = _read_json(path).get("mcpServers", {})
        except Exception as exc:
            print(f"{label}: could not read its settings ({exc})")
            continue
        item = servers.get(name)
        print(f"{label}: {'registered, ' + item['command'] if item else 'not registered'}")
    ask = [i for i in _read_json(code_settings()).get("permissions", {}).get("ask", []) if i.startswith(f"mcp__{name}__")]
    print(f"Ask first: {', '.join(ask) if ask else 'nothing'}")
    waiting = sorted(pending_dir().glob(f"{name}-*.json")) if pending_dir().exists() else []
    print(f"Waiting for Claude to close: {'yes' if waiting else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_apply-when-closed"] and len(argv) == 2:
        _wait_then_apply(Path(argv[1]))
        return 0

    parser = argparse.ArgumentParser(description="Tell Claude about a connector, safely, even while Claude is open.")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("add", "remove", "status"):
        p = sub.add_parser(action)
        p.add_argument("--name", required=True, help="what the connector is called in Claude, e.g. robin-slack")
        if action == "add":
            p.add_argument("--command", required=True, help="full path to the connector's program")
            p.add_argument("--env", action="append", default=[], help="NAME=value, repeatable")
            p.add_argument("--arg", action="append", default=[], help="one argument for the program, repeatable (write --arg=-m for one that starts with a dash)")
        if action in ("add", "remove"):
            p.add_argument("--ask", action="append", default=[], help="a tool Claude must ask before using, repeatable")
            p.add_argument("--desktop-only", action="store_true")
            p.add_argument("--code-only", action="store_true")
            p.add_argument("--now", action="store_true", help="change the files now even if Claude is open (not recommended)")
    args = parser.parse_args(argv)

    if not NAME.match(args.name):
        print("The name may use lowercase letters, numbers and hyphens only, for example robin-slack.")
        return 2
    if args.action == "status":
        return _status(args.name)

    change: dict = {"action": args.action, "name": args.name,
                    "desktop": not args.code_only, "code": not args.desktop_only}
    if args.action == "add":
        command = str(Path(os.path.expanduser(args.command)))
        if not Path(command).is_absolute() or not os.access(command, os.X_OK):
            print(f"Can't find a program at {command}. Install the connector first, then run this again.")
            return 1
        change["command"] = command
        change["env"] = _parse_env(args.env)
        change["args"] = list(args.arg)

    for line in set_ask(args.name, args.ask, remove=args.action == "remove"):
        print(line)

    if claude_is_open() and not args.now:
        schedule(change)
        print(
            "Claude is open, so this change will wait until you quit it. Claude rewrites its own settings "
            "while it is open and would undo the change.\n\n"
            "Next: quit Claude completely (Claude menu, Quit Claude, or Command-Q). A few seconds later the "
            f"change is made and Claude opens again by itself, with {args.name} in place. "
            "Then start a new chat to try it.\n"
            f"A record of what happened is kept in {log_path()}."
        )
        return 0

    for line in apply_change(change):
        print(line)
    print("Done. The next time Claude opens, it loads the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
