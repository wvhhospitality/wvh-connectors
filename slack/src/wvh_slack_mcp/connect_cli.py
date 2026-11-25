"""wvh-slack-connect: everything a person ever does to log in to Slack.

    wvh-slack-connect              go through every workspace not yet connected
    wvh-slack-connect northwind    connect one, by name or code
    wvh-slack-connect status       see where everything stands
    wvh-slack-connect check NB     test how fast Slack lets this app read
    wvh-slack-connect disconnect NB   revoke and forget, for offboarding
    wvh-slack-connect sync NB HL   copy logins from this Mac to a second, always-on Mac
    wvh-slack-connect setup        one-time: create your own Slack app
    wvh-slack-connect setup --client-id 123.456   save the app's Client ID without a prompt
"""

from __future__ import annotations

import re
import sys
import urllib.parse
import webbrowser
from pathlib import Path

from slack_sdk.errors import SlackApiError

from .config import (
    DEFAULT_OWNER,
    ConfigError,
    connections_path,
    load_config,
    owner_name,
    read_connections,
    settings,
    write_connections,
)
from .detect import Detected, detect_workspaces
from .oauth import ConnectError, connect, disconnect, resolve_target
from .slack_client import WorkspacePool, _error_code
from .sync import sync_logins

MANIFEST = Path(__file__).resolve().parents[2] / "app.manifest.yaml"
CREATE_APP_URL = "https://api.slack.com/apps?new_app=1&manifest_yaml="


def _plan(config) -> tuple[list[tuple[Detected, str, str]], list[str], list[str]]:
    """Sort what's on this Mac into to-do, done, and held."""
    todo, done, held = [], [], []
    for found in detect_workspaces():
        ws = config.by_team(found.team_id)
        if ws is not None and ws.frozen:
            held.append(ws.label)
        elif ws is not None and config.user_token(ws):
            done.append(ws.label)
        elif ws is not None and ws.enabled:
            todo.append((found, ws.client_code, "adds pictures, search, and DMs"))
        else:
            todo.append((found, ws.client_code if ws else "new", ""))
    return todo, done, held


def cmd_walk() -> int:
    config = load_config()
    if not config.client_id:
        print("One-time setup comes first. Run: wvh-slack-connect setup")
        return 1

    todo, done, held = _plan(config)
    print(f"Found {len(todo) + len(done) + len(held)} Slack workspaces on this Mac.")
    print(f"  {len(done)} connected, {len(held)} frozen, {len(todo)} to go.\n")
    if held:
        print(f"  Skipping frozen: {', '.join(held)}\n")

    connected = 0
    for index, (found, code, note) in enumerate(todo, 1):
        suffix = f" ({note})" if note else ""
        answer = input(
            f"[{index}/{len(todo)}] {found.name} [{code}]{suffix}\n"
            "      Enter = connect, s = skip, q = quit: "
        ).strip().lower()
        if answer == "q":
            break
        if answer == "s":
            continue
        print("      Slack is opening in your browser. Click Allow.")
        try:
            result = connect(load_config(), found.team_id)
        except ConnectError as exc:
            print(f"      Not connected. {exc}\n")
            continue
        connected += 1
        if result.team_id != found.team_id:
            print(f"      Heads up: you approved {result.name}, not {found.name}.")
        print(f"      Connected {result.name} as {result.client_code}.\n")

    print(f"Done. {connected} connected this run.")
    return 0


def cmd_one(ref: str) -> int:
    config = load_config()
    if not config.client_id:
        print("One-time setup comes first. Run: wvh-slack-connect setup")
        return 1
    target = resolve_target(config, ref)
    print("Slack is opening in your browser. Click Allow.")
    result = connect(config, target)
    if target and result.team_id != target:
        print(f"Heads up: you asked for {ref}, but Slack approved {result.name}. {ref} is NOT connected.")
        print("Check the app's public distribution, and that this browser is signed in to that workspace.")
        return 1
    print(f"Connected {result.name} as {result.client_code}.")
    return 0


def cmd_status() -> int:
    config = load_config()
    todo, done, held = _plan(config)
    print(f"Connected ({len(done)}): {', '.join(done) or 'none'}")
    print(f"To connect ({len(todo)}): {', '.join(f.name for f, _, _ in todo) or 'none'}")
    print(f"Frozen ({len(held)}): {', '.join(held) or 'none'}")
    print(f"Your Slack app: {'set up' if config.client_id else 'not set up yet. Run: wvh-slack-connect setup'}")
    return 0


def manifest_text(redirect_uri: str) -> str:
    """The Slack app this install creates: named for its owner, and nobody else's."""
    owner = owner_name()
    named = owner != DEFAULT_OWNER
    name = str(settings().get("app_name") or (f"{owner} Assistant" if named else "Slack Assistant"))
    about = (
        f"{owner}'s assistant for Slack. It posts only when {owner} says to."
        if named
        else "A personal assistant for Slack. It posts only when its owner says to."
    )
    clean = lambda text, limit: re.sub(r'["\\\n]', "", text)[:limit]
    return (
        MANIFEST.read_text()
        .replace("{app_name}", clean(name, 35))
        .replace("{app_description}", clean(about, 140))
        .replace("{redirect_uri}", redirect_uri)
    )


def _save_client_id(client_id: str, redirect_uri: str) -> int:
    if not re.fullmatch(r"\d+\.\d+", client_id):
        print("That doesn't look like a Client ID (two numbers with a dot). Nothing saved.")
        return 1
    path = connections_path()
    state = read_connections(path)
    state["app"]["client_id"] = client_id
    # Kept with the app, so a later change to the default address can't break this one.
    state["app"].setdefault("redirect_uri", redirect_uri)
    write_connections(path, state)
    print("\nSaved. Now run: wvh-slack-connect")
    return 0


def cmd_setup(args: list[str]) -> int:
    config = load_config()
    if args[:1] == ["--client-id"] and len(args) == 2:
        return _save_client_id(args[1].strip(), config.redirect_uri)
    if args:
        print("Use: wvh-slack-connect setup    or    wvh-slack-connect setup --client-id 123.456")
        return 1

    url = CREATE_APP_URL + urllib.parse.quote(manifest_text(config.redirect_uri))
    print("Opening Slack with your own app already filled in. This happens once, ever.\n")
    print("  1. Pick the workspace you use most, then Next, Next, Create.")
    print("  2. Only if you have more than one workspace: in the left menu click Manage Distribution,")
    print("     open 'Remove Hard Coded Information', tick the box, click Activate Public Distribution.")
    print("  3. Go to Basic Information and copy the Client ID. It isn't a secret.\n")
    webbrowser.open(url)

    if not sys.stdin.isatty():
        print("Then run: wvh-slack-connect setup --client-id <the Client ID>")
        return 0
    return _save_client_id(input("Paste the Client ID here: ").strip(), config.redirect_uri)


def cmd_disconnect(ref: str) -> int:
    config = load_config()
    ws = config.lookup(ref)
    if ws is None:
        print(f"No workspace matches '{ref}'.")
        return 1
    disconnect(config, ws)
    print(f"Disconnected {ws.label}. The login is revoked at Slack and gone from this Mac.")
    return 0


def cmd_check(ref: str) -> int:
    """Measure Slack's read limits on this app. Prints counts only, never message text."""
    config = load_config()
    ws = config.find(ref)
    pool = WorkspacePool(config)
    client = pool.reader(ws)
    joined = [c for c in pool.list_channels(ws) if c["is_member"]]
    channel, first = None, None
    for candidate in joined:
        first = client.conversations_history(channel=candidate["id"], limit=100)
        if first.get("messages"):
            channel = candidate
            break
    if channel is None:
        print("Inconclusive: no channel here has messages to test against.")
        return 1

    count = len(first.get("messages", []))
    print(f"Read 1: {count} messages, more waiting: {bool(first.get('has_more'))}")
    throttled = False
    for attempt in (2, 3, 4):
        try:
            client.conversations_history(channel=channel["id"], limit=100)
            print(f"Read {attempt}: ok")
        except SlackApiError as exc:
            throttled = _error_code(exc) == "ratelimited"
            print(f"Read {attempt}: {_error_code(exc)}")
            break

    if pool.user(ws):
        try:
            hits = client.search_messages(query=f"in:#{channel['name']}", count=20, sort="timestamp")
            total = (hits.get("messages") or {}).get("total", 0)
            print(f"Search by channel: works, {total} messages found")
        except SlackApiError as exc:
            print(f"Search by channel: {_error_code(exc)}")

    short = count == 15 and first.get("has_more")
    print("\nVerdict:", "Slack is capping history for this app; search will fill in."
          if throttled or short else "No cap seen here.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    command = args[0].lower() if args else ""
    try:
        if not args:
            return cmd_walk()
        if command == "setup":
            return cmd_setup(args[1:])
        if command == "status":
            return cmd_status()
        if command == "check" and len(args) == 2:
            return cmd_check(args[1])
        if command == "disconnect" and len(args) == 2:
            return cmd_disconnect(args[1])
        if command == "sync" and len(args) >= 2:
            return sync_logins(args[1:])
        if command in {"-h", "--help", "help"}:
            print(__doc__)
            return 0
        return cmd_one(" ".join(args))
    except (ConnectError, ConfigError) as exc:
        print(exc)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Everything connected so far is saved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
