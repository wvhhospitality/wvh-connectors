"""Set up WvH Texts: who this install works for, and who is on the allow-list.

    wvh-texts-setup --install robin-texts status
    wvh-texts-setup --install robin-texts owner Robin [--timezone America/Chicago]
    wvh-texts-setup --install robin-texts allow "Dana Northwind" [--only "+1 312 555 0101"]
    wvh-texts-setup --install robin-texts allow "+1 312 555 0199" --name "Front desk"
    wvh-texts-setup --install robin-texts groups "Dana Northwind"       (find group chats)
    wvh-texts-setup --install robin-texts allow-group "Harbor crew"   or   allow-group --id chat123 --name "Harbor crew"
    wvh-texts-setup --install robin-texts nickname Dee "Dana Northwind"   (--remove to take one off)
    wvh-texts-setup --install robin-texts remove "Dana Northwind"
    wvh-texts-setup --install robin-texts list

This is the only way the allow-list changes. The connector's tools can read it but
never change it, so nothing a text says can widen it. Every change makes a dated
copy of the settings file first. Nothing here needs a password.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .config import (
    Config,
    ConfigError,
    contacts_root,
    home_dir,
    install_name,
    is_handle,
    load_config,
    mask,
    messages_db,
    normalize_handle,
    read_raw,
    settings_path,
)
from .contacts import Directory, read_cards
from .db import FULL_DISK_ACCESS_FIX, NoAccess, access
from .messages import Store


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
        "# WvH Texts settings. Only threads on this allow-list can be read.\n"
        "# Change it with wvh-texts-setup, not by hand.\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".people-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def _load(install: str) -> tuple[Path, dict]:
    path = settings_path(install)
    raw = read_raw(path)
    load_config(path, install)  # refuse to build on a file that doesn't read cleanly
    return path, raw


def _cards():
    try:
        return read_cards(contacts_root())
    except NoAccess:
        print("Contacts couldn't be opened.\n\n" + FULL_DISK_ACCESS_FIX)
        return None


def _store():
    try:
        return Store()
    except NoAccess:
        path = messages_db()
        if access(path.parent, path) == "missing":
            print("There is no Messages history on this Mac.")
        else:
            print("Messages couldn't be opened.\n\n" + FULL_DISK_ACCESS_FIX)
        return None


def _shown(handles) -> str:
    return ", ".join(sorted(mask(h) for h in handles))


# ─── Commands ───────────────────────────────────────────────────────────────


def cmd_status(install: str) -> int:
    folder = home_dir(install)
    print(f"Install:          {install}")
    print(f"Settings folder:  {folder}" + ("" if folder.exists() else "  (not created yet)"))
    cfg = load_config(install=install)
    print(f"Owner:            {cfg.owner_name}")
    print("Time zone:        " + (cfg.timezone or "this Mac's own"))
    print(f"Allow-list:       {len(cfg.allow)} people, {len(cfg.groups)} group chats")
    path = messages_db()
    messages = access(path.parent, path)
    try:
        cards = read_cards(contacts_root())
        contacts = f"ok ({len(cards)} cards with a number or address)"
    except NoAccess:
        contacts = "blocked"
    print("Messages:         " + {"ok": "ok", "blocked": "blocked", "missing": "no Messages history on this Mac"}[messages])
    print(f"Contacts:         {contacts}")
    if messages == "blocked" or contacts == "blocked":
        print("\n" + FULL_DISK_ACCESS_FIX)
    if cfg.empty:
        print("\nNo one is on the allow-list yet, so the connector reads nothing.")
        if not folder.exists():
            print("Nothing has been created on this Mac.")
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
    path, data = _load(install)
    data["owner_name"] = name
    if timezone:
        data["timezone"] = timezone
    write_settings(path, data)
    print(f"Saved. This install works for {name}" + (f", in {timezone}." if timezone else "."))
    return 0


def _add_person(data: dict, name: str, handles: set[str]) -> tuple[str, set[str]]:
    entries = data.setdefault("allow", [])
    for entry in entries:
        if str(entry.get("name", "")).strip().lower() == name.lower():
            merged = {normalize_handle(h) for h in entry.get("handles") or []} | handles
            entry["handles"] = sorted(merged)
            return entry["name"], merged
    entries.append({"name": name, "handles": sorted(handles)})
    return name, handles


def cmd_allow(install: str, query: str, only: list[str], label: str | None) -> int:
    path, data = _load(install)
    cards = _cards()
    if cards is None:
        return 1
    plain = Directory(cards, Config(install=install, nicknames=load_config(path, install).nicknames))

    if is_handle(query):
        handle = normalize_handle(query)
        card = plain.by_handle.get(handle)
        name = card.name if card else (label or query.strip())
        handles = {handle}
    else:
        found = plain.resolve(query)
        if not found.people:
            print(f"No one called {query!r} is in this Mac's Contacts. Check the spelling, or allow their number instead.")
            return 1
        if found.one is None:
            print(f"{query!r} could mean more than one person. Nothing was added. Run it again with one of these full names, or the number:")
            for p in found.people[:15]:
                extra = f", {p.organization}" if p.organization else ""
                print(f"  {p.name}{extra}  ({_shown(p.handles)})")
            return 1
        person = found.one
        handles = set(person.handles)
        if only:
            wanted = {normalize_handle(h) for h in only}
            unknown = wanted - handles
            if unknown:
                print(f"{_shown(unknown)} isn't saved on {person.name}'s card. Nothing was added.")
                return 1
            handles = wanted
        name = person.name

    name, total = _add_person(data, name, handles)
    write_settings(path, data)
    print(f"Added {name}: {_shown(total)}.")
    print("Their direct texts with those numbers and addresses can now be read.")
    print("Group chats they are in stay closed. Add a group on its own with allow-group.")
    return 0


def _describe_group(chat, directory: Directory | None, store: Store, allowed: set[str]) -> str:
    names = []
    for handle in sorted(chat.handles):
        known = directory.name_for(handle) if directory else None
        names.append(known or mask(handle))
    last = store.last_message(chat.id)
    when = last.astimezone().strftime("%Y-%m-%d") if last else "no messages"
    title = chat.display_name or "(no name)"
    mark = "  [on the allow-list]" if chat.identifier in allowed else ""
    return f"  {title}  id {chat.identifier}  {len(chat.handles)} others: {', '.join(names)}  last {when}{mark}"


def cmd_groups(install: str, query: str) -> int:
    cfg = load_config(install=install)
    store = _store()
    if store is None:
        return 1
    try:
        cards = _cards()
        directory = Directory(cards or [], cfg)
        found = {c.id: c for c in store.groups_named(query)}
        person = directory.resolve(query).one if not is_handle(query) else None
        handles = {normalize_handle(query)} if is_handle(query) else (person.handles if person else set())
        for chat in store.groups_with(handles):
            found[chat.id] = chat
        if not found:
            print(f"No group chats found for {query!r}.")
            return 1
        allowed = {g.chat for g in cfg.groups}
        print(f"Group chats for {query!r}. To allow one: allow-group --id <id> --name \"<what you call it>\"")
        for chat in sorted(found.values(), key=lambda c: c.display_name.lower()):
            print(_describe_group(chat, directory, store, allowed))
        return 0
    finally:
        store.close()


def cmd_allow_group(install: str, name: str | None, chat_id: str | None, label: str | None) -> int:
    path, data = _load(install)
    store = _store()
    if store is None:
        return 1
    try:
        if chat_id:
            chat = store.group_by_identifier(chat_id.strip())
            if chat is None:
                print(f"No group chat with id {chat_id!r} on this Mac. Find it with: groups \"<a member's name>\"")
                return 1
        else:
            matches = store.groups_named(name or "")
            if not matches:
                print(f"No group chat is called {name!r}. Find it with: groups \"<a member's name>\"")
                return 1
            if len(matches) > 1:
                print(f"More than one group chat matches {name!r}. Nothing was added. Run it again with --id:")
                for chat in matches:
                    print(_describe_group(chat, None, store, set()))
                return 1
            chat = matches[0]
        title = (label or chat.display_name or "").strip()
        if not title:
            print("That group has no name in Messages. Give it one here with --name \"<what you call it>\".")
            return 1
        groups = data.setdefault("groups", [])
        for entry in groups:
            if entry.get("chat") == chat.identifier:
                entry["name"] = title
                break
        else:
            groups.append({"name": title, "chat": chat.identifier})
        write_settings(path, data)
        print(f"Added the group chat {title!r} ({len(chat.handles)} others in it). Every message in it can now be read.")
        return 0
    finally:
        store.close()


def cmd_remove(install: str, query: str) -> int:
    path, data = _load(install)
    low = query.strip().lower()
    handle = normalize_handle(query) if is_handle(query) else None
    people, groups = data.get("allow") or [], data.get("groups") or []

    for entry in list(groups):
        if str(entry.get("name", "")).lower() == low or entry.get("chat") == query.strip():
            groups.remove(entry)
            write_settings(path, data)
            print(f"Removed the group chat {entry.get('name')!r}. It can no longer be read.")
            return 0
    for entry in list(people):
        stored = {normalize_handle(h) for h in entry.get("handles") or []}
        if handle and handle in stored:
            stored.discard(handle)
            if stored:
                entry["handles"] = sorted(stored)
                print(f"Removed {mask(handle)} from {entry['name']}. Their other numbers stay allowed: {_shown(stored)}.")
            else:
                people.remove(entry)
                print(f"Removed {entry['name']}. Their texts can no longer be read.")
            write_settings(path, data)
            return 0
        if not handle and str(entry.get("name", "")).lower() == low:
            people.remove(entry)
            write_settings(path, data)
            print(f"Removed {entry['name']}. Their texts can no longer be read.")
            return 0
    print(f"{query!r} isn't on the allow-list. Nothing changed.")
    return 1


def cmd_nickname(install: str, nick: str, name: str | None, remove: bool) -> int:
    path, data = _load(install)
    nicknames = data.setdefault("nicknames", {}) or {}
    data["nicknames"] = nicknames
    key = next((k for k in nicknames if str(k).lower() == nick.strip().lower()), None)
    if remove:
        if key is None:
            print(f"No nickname {nick!r}. Nothing changed.")
            return 1
        del nicknames[key]
        write_settings(path, data)
        print(f"Removed the nickname {nick!r}.")
        return 0
    if not name:
        print('Give the full name too, such as: nickname Dee "Dana Northwind"')
        return 2
    nicknames[key or nick.strip()] = name.strip()
    write_settings(path, data)
    print(f"Saved: {nick.strip()!r} means {name.strip()}. A nickname never adds anyone to the allow-list.")
    return 0


def cmd_list(install: str) -> int:
    cfg = load_config(install=install)
    print(f"Install {install}, for {cfg.owner_name}.")
    if cfg.empty:
        print("No one is on the allow-list yet.")
    for person in sorted(cfg.allow, key=lambda p: p.name.lower()):
        print(f"  person  {person.name}  ({_shown(person.handles)})")
    for group in sorted(cfg.groups, key=lambda g: g.name.lower()):
        print(f"  group   {group.name}  (id {group.chat})")
    for nick, name in sorted(cfg.nicknames.items()):
        print(f"  nickname  {nick} = {name}")
    return 0


# ─── Entry point ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wvh-texts-setup", description="Set up WvH Texts and its allow-list.")
    parser.add_argument("--install", help="the install name, for example robin-texts (default: WVH_TEXTS_INSTALL, else wvh-texts)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("owner")
    p.add_argument("name")
    p.add_argument("--timezone")
    p = sub.add_parser("allow")
    p.add_argument("who", help="a name from Contacts, a phone number, or an email address")
    p.add_argument("--only", action="append", default=[], help="allow only this number or address of theirs, repeatable")
    p.add_argument("--name", help="for a number that isn't in Contacts: what to call it")
    p = sub.add_parser("groups")
    p.add_argument("who", help="a group chat's name, or someone in it")
    p = sub.add_parser("allow-group")
    p.add_argument("name", nargs="?")
    p.add_argument("--id", dest="chat_id")
    p.add_argument("--name", dest="label")
    p = sub.add_parser("remove")
    p.add_argument("who")
    p = sub.add_parser("nickname")
    p.add_argument("nick")
    p.add_argument("name", nargs="?")
    p.add_argument("--remove", action="store_true")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    try:
        install = install_name(args.install)
        if args.command == "status":
            return cmd_status(install)
        if args.command == "owner":
            return cmd_owner(install, args.name, args.timezone)
        if args.command == "allow":
            return cmd_allow(install, args.who, args.only, args.name)
        if args.command == "groups":
            return cmd_groups(install, args.who)
        if args.command == "allow-group":
            if not args.name and not args.chat_id:
                print('Give the group\'s name, or --id from the groups command.')
                return 2
            return cmd_allow_group(install, args.name, args.chat_id, args.label)
        if args.command == "remove":
            return cmd_remove(install, args.who)
        if args.command == "nickname":
            return cmd_nickname(install, args.nick, args.name, args.remove)
        return cmd_list(install)
    except ConfigError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
