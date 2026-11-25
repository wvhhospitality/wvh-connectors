"""Copy Slack logins from this Mac to a second, always-on Mac, so nobody clicks Allow twice.

Optional: most installs have one Mac and never use this. It needs a `second_mac:`
block in workspaces.yaml (see the README) and SSH keys already set up between the two.

The second Mac's Keychain only opens inside its logged-in session, and SSH can't enter
that. So a one-shot agent starts there, in that session, and reads from a private
pipe. Tokens travel over SSH's standard input into that pipe: never on a command
line, never in a file, and never printed. The pipe and agent are gone afterward.

Copying a login doesn't make it readable from a phone. That's a separate setting,
WVH_SLACK_REMOTE_REALMS on the second Mac.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from . import keychain
from .config import ConfigError, connections_path, load_config, read_connections, second_mac, write_connections
from .slack_client import WorkspacePool

LABEL = "com.wvh.slack-sync-once"
_HOST = re.compile(r"[A-Za-z0-9._@-]+")
_PATH = re.compile(r"/[A-Za-z0-9._/-]+")


def _target() -> tuple[str, str, str]:
    """The second Mac's SSH name, its home folder, and the Python that runs this app there."""
    mac = second_mac()
    host, home = str(mac.get("host") or ""), str(mac.get("home") or "").rstrip("/")
    if not host or not home:
        raise ConfigError(
            "No second Mac is set up. Add a `second_mac:` block with `host` and `home` "
            "to workspaces.yaml. The README shows how."
        )
    python = str(mac.get("python") or f"{home}/wvh-connectors/slack/.venv/bin/python")
    if not _HOST.fullmatch(host) or not _PATH.fullmatch(home) or not _PATH.fullmatch(python):
        raise ConfigError("`second_mac` needs a plain host name and full, plain paths (no spaces).")
    return host, home, python


def drop_folder(home: str) -> str:
    """A full path on purpose: a cleanup command must never depend on a variable being set."""
    return f"{home}/.wvh-slack-sync"


def clear_script(home: str) -> str:
    # Cleanup names each file it made, then removes the folder only if that left it empty.
    drop = drop_folder(home)
    return (
        f'launchctl bootout "gui/$(id -u)/{LABEL}" 2>/dev/null; '
        f"rm -f {drop}/in {drop}/once.plist {drop}/result.tmp {drop}/result.json; "
        f"rmdir {drop} 2>/dev/null; true"
    )


def start_script(home: str, python: str) -> str:
    drop = drop_folder(home)
    return f"""{clear_script(home)}
set -e
mkdir -m 700 {drop}
mkfifo -m 600 {drop}/in
cat > {drop}/once.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string>
    <string>-m</string><string>wvh_slack_mcp.sync</string>
    <string>receive</string><string>{drop}</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
launchctl bootstrap "gui/$(id -u)" {drop}/once.plist
"""


def _ssh(host: str, script: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, script],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def sync_logins(refs: list[str]) -> int:
    """Runs on the Mac the owner sits at."""
    config = load_config()
    app = read_connections(connections_path())
    lines = []
    for ref in refs:
        ws = config.find(ref)  # refuses a frozen or unknown realm
        token = config.user_token(ws)
        if token is None:
            print(f"{ws.label} isn't connected on this Mac yet. Run: wvh-slack-connect {ws.client_code}")
            return 1
        lines.append(json.dumps({
            "code": ws.client_code,
            "team_id": ws.team_id,
            "token": token,
            "connection": app["workspaces"][ws.team_id],
        }))

    host, home, python = _target()
    drop = drop_folder(home)
    started = _ssh(host, start_script(home, python))
    if started.returncode != 0:
        print("Couldn't start the receiver on the second Mac. Nothing was copied.")
        return 1
    try:
        fed = _ssh(host, f"cat > {drop}/in", stdin="\n".join(lines) + "\n")
        if fed.returncode != 0:
            print("Couldn't reach the receiver on the second Mac. Nothing was copied.")
            return 1
        for _ in range(30):
            reply = _ssh(host, f"cat {drop}/result.json")
            if reply.returncode == 0 and reply.stdout.strip():
                break
            time.sleep(2)
        else:
            print("The second Mac didn't answer in time. Run this again.")
            return 1
    finally:
        _ssh(host, clear_script(home))

    failed = 0
    for row in json.loads(reply.stdout):
        print(f"{row['code']}: {'copied, and Slack confirmed it from the second Mac' if row['ok'] else row['why']}")
        failed += 0 if row["ok"] else 1
    return 1 if failed else 0


def _receive_one(item: dict, config, state: dict, path: Path) -> dict:
    code, team_id = item["code"], item["team_id"]
    ws = config.by_team(team_id)
    if ws is None or ws.client_code != code or ws.frozen:
        return {"code": code, "ok": False, "why": "not in the second Mac's registry, or frozen"}

    account = f"{code}-user"
    previous = keychain.read(config.keychain_service, account)
    keychain.store(config.keychain_service, account, item["token"])
    if keychain.read(config.keychain_service, account) != item["token"]:
        return {"code": code, "ok": False, "why": "the second Mac's Keychain didn't take it"}

    prior_entry = state["workspaces"].get(team_id)
    state["workspaces"][team_id] = item["connection"]
    write_connections(path, state)
    try:
        fresh = load_config()
        WorkspacePool(fresh).verify_realm(fresh.by_team(team_id), fresh=True)
    except Exception as exc:  # anything at all: undo, and keep the reason free of details
        if prior_entry is None:
            state["workspaces"].pop(team_id, None)
        else:
            state["workspaces"][team_id] = prior_entry
        write_connections(path, state)
        if previous:
            keychain.store(config.keychain_service, account, previous)
        else:
            keychain.delete(config.keychain_service, account)
        return {"code": code, "ok": False, "why": f"Slack didn't confirm it ({type(exc).__name__}). Undone."}
    return {"code": code, "ok": True}


def receive(folder: str) -> None:
    """Runs on the second Mac, in its logged-in session. Prints nothing."""
    base = Path(folder)
    with open(base / "in") as pipe:
        items = [json.loads(line) for line in pipe.read().splitlines() if line.strip()]
    config = load_config()
    path = connections_path()
    state = read_connections(path)
    results = [_receive_one(item, config, state, path) for item in items]
    tmp = base / "result.tmp"
    tmp.write_text(json.dumps(results))
    tmp.rename(base / "result.json")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "receive":
        receive(sys.argv[2])
