"""Set up WvH Mail: add a mailbox, check them, take one away.

    wvh-mail-setup --install robin-mail status
    wvh-mail-setup --install robin-mail owner Robin [--timezone America/Chicago]
    wvh-mail-setup --install robin-mail add robin@example.com [--label Work]
    wvh-mail-setup --install robin-mail password robin@example.com
    wvh-mail-setup --install robin-mail remove robin@example.com

`add` asks for the mailbox's app password itself. In a terminal it is a hidden
prompt. When Claude runs it, there is no terminal, so a small Mac window opens
with a hidden box to paste it into. Either way the password goes straight to the
Mac's Keychain: never into a chat, a command, a file, a log, or the screen. It is
checked with the mail server first, and kept only if the server accepts it.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import keyring
import yaml

from . import imap_client as mail
from .config import (
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    EMAIL,
    Account,
    ConfigError,
    has_password,
    home_dir,
    install_name,
    keychain_service,
    load_config,
    read_raw,
    settings_path,
)

# Under two minutes, so a Claude running this with its usual command time limit sees the answer.
DIALOG_SECONDS = 100


# ─── Asking for the password ────────────────────────────────────────────────


def _applescript_text(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask_in_dialog(address: str) -> str | None:
    """A Mac window with a hidden answer box. Returns None if it was cancelled or timed out."""
    prompt = (
        f"Paste the app password for {address}, then click Save.\n\n"
        "It goes straight into this Mac's Keychain. It won't show here, in Claude, or anywhere else."
    )
    script = (
        "activate\n"
        f"set reply to display dialog {_applescript_text(prompt)} default answer \"\" with hidden answer "
        f"with title \"Mail setup\" buttons {{\"Cancel\", \"Save\"}} default button \"Save\" "
        f"cancel button \"Cancel\" giving up after {DIALOG_SECONDS}\n"
        "if gave up of reply then return \"\"\n"
        "return text returned of reply"
    )
    try:
        # The script goes in on stdin and the answer comes back on stdout: the password
        # is never part of any command line.
        done = subprocess.run(["/usr/bin/osascript", "-"], input=script, capture_output=True, text=True, timeout=DIALOG_SECONDS + 10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n")


def ask_in_terminal(address: str) -> str | None:
    try:
        return getpass.getpass(f"Paste the app password for {address}, then press Return (it won't show): ")
    except (EOFError, KeyboardInterrupt):
        return None


def ask_password(address: str) -> str | None:
    raw = ask_in_terminal(address) if sys.stdin.isatty() else ask_in_dialog(address)
    if raw is None:
        return None
    # Google shows app passwords in groups of four with spaces; the spaces aren't part of it.
    return "".join(raw.split()) or None


# ─── Writing the settings file ──────────────────────────────────────────────


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    copy = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, copy)
    return copy


def write_settings(path: Path, data: dict) -> Path | None:
    """Replace the file in one step, after a dated copy of the old one. Private to this user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    backup = _backup(path)
    text = (
        "# WvH Mail settings. No passwords here: those are in the Mac's Keychain.\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".accounts-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def _entry(email: str, label: str | None, host: str | None, port: int | None) -> dict:
    entry: dict = {"email": email}
    if label:
        entry["label"] = label
    if host and host != DEFAULT_IMAP_HOST:
        entry["imap_host"] = host
    if port and port != DEFAULT_IMAP_PORT:
        entry["imap_port"] = port
    return entry


# ─── Commands ───────────────────────────────────────────────────────────────


def cmd_status(install: str, probe: bool = True) -> int:
    folder = home_dir(install)
    print(f"Install:          {install}")
    print(f"Settings folder:  {folder}" + ("" if folder.exists() else "  (not created yet)"))
    print(f"Keychain service: {keychain_service(install)}")
    if not settings_path(install).exists():
        print("\nNo mailboxes yet. Nothing has been created on this Mac.")
        print(f"Add one with: wvh-mail-setup --install {install} add <address>")
        return 0
    cfg = load_config(install=install)
    print(f"Owner:            {cfg.owner_name}")
    print("Time zone:        " + (cfg.timezone or "this Mac's own"))
    if not cfg.accounts:
        print("\nNo mailboxes yet.")
        return 0
    print()
    pool = mail.IMAPPool(install)
    try:
        for acct in cfg.accounts:
            stored = has_password(acct, install)
            line = f"  {acct.email}" + (f" ({acct.label})" if acct.label else "")
            line += f"  login {'stored' if stored else 'MISSING'}"
            if probe and stored:
                line += f"  sign-in: {mail.probe(pool, acct).status}"
            print(line)
    finally:
        pool.close_all()
    return 0


def cmd_owner(install: str, name: str, timezone: str | None) -> int:
    name = name.strip()
    if not name:
        print("Give a name, such as: owner Robin")
        return 2
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            print(f"{timezone!r} isn't a time zone name. Use one like America/Los_Angeles.")
            return 2
    path = settings_path(install)
    data = read_raw(path)
    data["owner_name"] = name
    if timezone:
        data["timezone"] = timezone
    write_settings(path, data)
    print(f"Saved. This install works for {name}" + (f", in {timezone}." if timezone else "."))
    return 0


def _store_and_check(install: str, account: Account) -> int:
    """Ask, check with the server, and keep the password only if the server accepts it."""
    typed = ask_password(account.email)
    if not typed:
        print("No password was entered. Nothing was saved.")
        return 1
    print("Checking it with the mail server...")
    result = mail.check_login(account, typed)
    if result.status == "auth_error":
        del typed
        print(f"The mail server said no, so nothing was saved. {result.error}")
        return 1
    keyring.set_password(keychain_service(install), account.email, typed)
    del typed
    if result.status == "ok":
        print(f"OK: {account.email} signs in and its inbox opens. The password is in this Mac's Keychain.")
        return 0
    print(f"Saved in the Keychain, but it couldn't be checked just now ({result.status}: {result.error}). Run status later.")
    return 0


def cmd_add(install: str, email: str, label: str | None, host: str | None, port: int | None) -> int:
    email = email.strip().lower()
    if not EMAIL.fullmatch(email):
        print(f"{email!r} isn't an email address.")
        return 2
    path = settings_path(install)
    data = read_raw(path)
    accounts = [a for a in (data.get("accounts") or []) if isinstance(a, dict)]
    existing = next((a for a in accounts if str(a.get("email", "")).strip().lower() == email), None)
    new = _entry(email, label or (existing or {}).get("label"), host or (existing or {}).get("imap_host"), port or (existing or {}).get("imap_port"))
    if label and any(str(a.get("label", "")).lower() == label.lower() and a is not existing for a in accounts):
        print(f"Another mailbox already has the label {label!r}. Pick a different one.")
        return 2
    account = Account(email=email, label=new.get("label", ""), imap_host=new.get("imap_host", DEFAULT_IMAP_HOST),
                      imap_port=int(new.get("imap_port", DEFAULT_IMAP_PORT)))
    status = _store_and_check(install, account)
    if status != 0:
        return status
    if existing is not None:
        accounts[accounts.index(existing)] = {**existing, **new}
    else:
        accounts.append(new)
    data["accounts"] = accounts
    write_settings(path, data)
    print(f"{'Updated' if existing else 'Added'} {email} in {path}.")
    return 0


def cmd_password(install: str, email: str) -> int:
    try:
        account = load_config(install=install).find(email)
    except ConfigError as exc:
        print(exc)
        return 1
    return _store_and_check(install, account)


def cmd_remove(install: str, email: str) -> int:
    email = email.strip().lower()
    path = settings_path(install)
    data = read_raw(path)
    accounts = [a for a in (data.get("accounts") or []) if isinstance(a, dict)]
    kept = [a for a in accounts if str(a.get("email", "")).strip().lower() != email]
    found_entry = len(kept) != len(accounts)
    if found_entry:
        data["accounts"] = kept
        write_settings(path, data)
    forgot = False
    try:
        keyring.delete_password(keychain_service(install), email)
        forgot = True
    except Exception:  # noqa: BLE001 - nothing stored is fine
        pass
    if not found_entry and not forgot:
        print(f"{email} wasn't set up here. Nothing changed.")
        return 1
    print(f"Removed {email}" + (" and its Keychain login." if forgot else "."))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wvh-mail-setup", description="Set up WvH Mail on this Mac.")
    parser.add_argument("--install", help="this install's name, such as robin-mail (default: WVH_MAIL_INSTALL, else wvh-mail)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="what is set up, and whether each mailbox signs in")
    p.add_argument("--no-probe", action="store_true", help="don't sign in to check")
    p = sub.add_parser("owner", help="whose mail this is, and their time zone")
    p.add_argument("name")
    p.add_argument("--timezone")
    p = sub.add_parser("add", help="add a mailbox and store its app password")
    p.add_argument("email")
    p.add_argument("--label", help="a short name, such as Work or Personal")
    p.add_argument("--imap-host", help=f"default {DEFAULT_IMAP_HOST} (Gmail and Google Workspace)")
    p.add_argument("--imap-port", type=int, help=f"default {DEFAULT_IMAP_PORT}")
    p = sub.add_parser("password", help="store a new app password for a mailbox already set up")
    p.add_argument("email")
    p = sub.add_parser("remove", help="take a mailbox away and forget its login")
    p.add_argument("email")
    args = parser.parse_args(argv)
    try:
        install = install_name(args.install)
        if args.command == "status":
            return cmd_status(install, probe=not args.no_probe)
        if args.command == "owner":
            return cmd_owner(install, args.name, args.timezone)
        if args.command == "add":
            return cmd_add(install, args.email, args.label, args.imap_host, args.imap_port)
        if args.command == "password":
            return cmd_password(install, args.email)
        if args.command == "remove":
            return cmd_remove(install, args.email)
    except ConfigError as exc:
        print(exc)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
