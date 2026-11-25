"""Set up WvH Mail: add a mailbox, check them, take one away.

    wvh-mail-setup --install robin-mail status
    wvh-mail-setup --install robin-mail owner Robin [--timezone America/Chicago]
    wvh-mail-setup --install robin-mail add robin@example.com [--label Work]
    wvh-mail-setup --install robin-mail password robin@example.com
    wvh-mail-setup --install robin-mail remove robin@example.com
    wvh-mail-setup --install robin-mail identity add Robin --signature ~/Desktop/robin.html --default
    wvh-mail-setup --install robin-mail identity list
    wvh-mail-setup --install robin-mail sending on|off
    wvh-mail-setup --install robin-mail drafts on|off
    wvh-mail-setup --install robin-mail import <older accounts.yaml> [--signatures <folder>]

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
import re
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
    is_legacy,
    keychain_service,
    load_config,
    read_raw,
    settings_path,
    signatures_dir,
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
    print(f"Sending:          {'on (every send is previewed and needs a yes)' if cfg.sending else 'off on this Mac'}")
    print(f"Drafts:           {'on' if cfg.drafts else 'off on this Mac'}")
    if cfg.identities:
        print("Identities:       " + ", ".join(i.name + (" (drafts only)" if i.drafts_only else "") for i in cfg.identities))
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


def _copy_signature(install: str, source: str) -> str:
    """Copy a signature file (and the .txt beside it, if any) into the settings folder."""
    src = Path(os.path.expanduser(source))
    if not src.is_file() or src.suffix.lower() not in (".html", ".htm"):
        raise ConfigError(f"{source} isn't an HTML signature file.")
    folder = signatures_dir(install)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder.parent, 0o700)
    name = re.sub(r"[^A-Za-z0-9._ -]", "-", src.name).lstrip(".-") or "signature.html"
    shutil.copy2(src, folder / name)
    txt = src.with_suffix(".txt")
    if txt.is_file():
        shutil.copy2(txt, (folder / name).with_suffix(".txt"))
    return name


def cmd_identity(install: str, action: str, name: str | None, signature: str | None, domains: list[str],
                 drafts_only: bool, default: bool, account: str | None) -> int:
    path = settings_path(install)
    data = read_raw(path)
    idents = [i for i in (data.get("identities") or []) if isinstance(i, dict)]
    if action == "list":
        if not idents:
            print("No identities yet. Mail goes out unsigned unless a mailbox has its own signature.")
            return 0
        for i in idents:
            extra = [f"signature {i['signature']}"] if i.get("signature") else []
            extra += [f"{d}: {f}" for d, f in (i.get("by_domain") or {}).items()]
            extra += ["drafts only"] if i.get("drafts_only") else []
            mark = " (default)" if str(data.get("default_identity", "")).lower() == str(i.get("name", "")).lower() else ""
            print(f"  {i.get('name')}{mark}: " + (", ".join(extra) or "no signature"))
        return 0
    if not name or not name.strip():
        print("Give the identity a name, such as: identity add Robin")
        return 2
    name = name.strip()
    entry = next((i for i in idents if str(i.get("name", "")).lower() == name.lower()), None)
    if entry is None:
        entry = {"name": name}
        idents.append(entry)
    if signature:
        entry["signature"] = _copy_signature(install, signature)
    for pair in domains or []:
        dom, sep, file = pair.partition("=")
        if not sep:
            print(f"--domain takes domain=file.html, not {pair!r}")
            return 2
        entry.setdefault("by_domain", {})[dom.strip().lower()] = _copy_signature(install, file.strip())
    if drafts_only:
        entry["drafts_only"] = True
    data["identities"] = idents
    if default:
        data["default_identity"] = name
    if account:
        accounts = [a for a in (data.get("accounts") or []) if isinstance(a, dict)]
        target = next((a for a in accounts if str(a.get("email", "")).lower() == account.lower()
                       or str(a.get("label", "")).lower() == account.lower()), None)
        if target is None:
            print(f"No mailbox called {account!r}.")
            return 2
        target["identity"] = name
        data["accounts"] = accounts
    write_settings(path, data)
    load_config(install=install)  # refuse to leave a file the server can't read
    print(f"Saved identity {name}" + (" (drafts only)" if entry.get("drafts_only") else "") + ".")
    return 0


def cmd_switch(install: str, which: str, value: str) -> int:
    path = settings_path(install)
    data = read_raw(path)
    data[which] = value == "on"
    write_settings(path, data)
    if which == "sending":
        print("Sending is " + ("on: every send is previewed and goes out only after a yes." if value == "on"
                               else "off on this Mac: no tool can send from here.") + " Restart Claude to apply it.")
    else:
        print("Drafts are " + ("on." if value == "on" else "off on this Mac: no tool can save or remove a draft.")
              + " Restart Claude to apply it.")
    return 0


def convert_legacy(raw: dict) -> dict:
    """The older WvH Mail format into this one. No passwords move: the Keychain service carries over."""
    imap = raw.get("imap") or {}
    services = {str(a.get("secret_ref", "")).split("/", 1)[0] for a in raw.get("accounts") or [] if isinstance(a, dict) and "/" in str(a.get("secret_ref", ""))}
    out: dict = {}
    if len(services) == 1:
        out["keychain_service"] = services.pop()
    elif services:
        raise ConfigError("The older file uses more than one Keychain service; convert it by hand.")
    accounts = []
    for a in raw.get("accounts") or []:
        if not isinstance(a, dict) or not a.get("email"):
            continue
        entry: dict = {"email": str(a["email"]).strip().lower()}
        if a.get("client_code"):
            entry["label"] = str(a["client_code"]).strip()
        host = a.get("imap_host") or imap.get("host")
        if host and host != DEFAULT_IMAP_HOST:
            entry["imap_host"] = host
        port = a.get("imap_port") or imap.get("port")
        if port and int(port) != DEFAULT_IMAP_PORT:
            entry["imap_port"] = int(port)
        if a.get("signature"):
            entry["signature"] = str(a["signature"]).strip()
        accounts.append(entry)
    out["accounts"] = accounts
    send = (raw.get("defaults") or {}).get("send") or {}
    limits = {k: int(send[k]) for k in ("max_per_window", "window_seconds", "max_recipients") if k in send}
    if limits:
        out["send_limits"] = limits
    return out


def cmd_import(install: str, source: str, signatures: str | None) -> int:
    src = Path(os.path.expanduser(source))
    raw = read_raw(src)
    if not is_legacy(raw):
        print(f"{src} doesn't look like the older WvH Mail format. Nothing changed.")
        return 1
    converted = convert_legacy(raw)
    path = settings_path(install)
    current = read_raw(path)
    if current.get("accounts"):
        print(f"{path} already has mailboxes. Import into a new install name, or move that file aside first.")
        return 1
    merged = {**current, **converted}
    copied = []
    if signatures:
        sig_src = Path(os.path.expanduser(signatures))
        for f in sorted(sig_src.glob("*.htm*")):
            copied.append(_copy_signature(install, str(f)))
    write_settings(path, merged)
    load_config(install=install)
    print(f"Imported {len(converted['accounts'])} mailboxes into {path}.")
    if converted.get("keychain_service"):
        print(f"Their app passwords stay where they are, in the Keychain under {converted['keychain_service']}.")
    if copied:
        print(f"Copied {len(copied)} signature files into {signatures_dir(install)}.")
    print("Run status to check each one signs in.")
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
    p = sub.add_parser("identity", help="who signs mail, and with which signature")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("name", nargs="?")
    p.add_argument("--signature", help="an HTML signature file to copy in (a .txt beside it is the plain-text version)")
    p.add_argument("--domain", action="append", default=[], help="domain=file.html: a different signature when sending from that domain")
    p.add_argument("--drafts-only", action="store_true", help="this identity may draft but never send")
    p.add_argument("--default", action="store_true", help="sign with it unless told otherwise")
    p.add_argument("--account", help="sign this one mailbox's mail with it")
    p = sub.add_parser("sending", help="let this Mac send (every send still needs a yes), or not")
    p.add_argument("value", choices=["on", "off"])
    p = sub.add_parser("drafts", help="let this Mac save drafts, or not")
    p.add_argument("value", choices=["on", "off"])
    p = sub.add_parser("import", help="bring in mailboxes from the older WvH Mail settings file")
    p.add_argument("source")
    p.add_argument("--signatures", help="a folder of signature files to copy in")
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
        if args.command == "identity":
            return cmd_identity(install, args.action, args.name, args.signature, args.domain,
                                args.drafts_only, args.default, args.account)
        if args.command in ("sending", "drafts"):
            return cmd_switch(install, args.command, args.value)
        if args.command == "import":
            return cmd_import(install, args.source, args.signatures)
    except ConfigError as exc:
        print(exc)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
