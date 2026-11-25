"""Set up WvH Texts: who this install works for, who is on the list, and the switches.

    wvh-texts-setup --install robin-texts status
    wvh-texts-setup --install robin-texts owner Robin [--timezone America/Chicago] [--number "+1 312 555 0100"]
    wvh-texts-setup --install robin-texts allow "Dana Northwind" [--only "+1 312 555 0101"]
    wvh-texts-setup --install robin-texts allow "+1 312 555 0199" --name "Front desk"
    wvh-texts-setup --install robin-texts groups "Dana Northwind"       (find group chats)
    wvh-texts-setup --install robin-texts allow-group "Harbor crew"   or   allow-group --id chat123 --name "Harbor crew"
    wvh-texts-setup --install robin-texts nickname Dee "Dana Northwind"   (--remove to take one off)
    wvh-texts-setup --install robin-texts remove "Dana Northwind"
    wvh-texts-setup --install robin-texts list
    wvh-texts-setup --install robin-texts sweep-hold "Dana Northwind"   (readable by name, left out of sweeps; --remove)
    wvh-texts-setup --install robin-texts opt-out "Dana Northwind"      (out of every tool; --remove)
    wvh-texts-setup --install robin-texts sending off                   (or on)
    wvh-texts-setup --install robin-texts scope clients                 (or all)
    wvh-texts-setup --install robin-texts import old-people.yaml [--to FILE] [--nicknames-to FILE]

This is the only way the list changes. The connector's tools can read it but never
change it, so nothing a text says can widen it. Every change makes a dated copy of
the settings file first. Nothing here needs a password.
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

from . import nicknames as shared_nicknames
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
    python_app,
    read_raw,
    settings_path,
)
from .contacts import Directory, read_cards
from .db import NoAccess, access, full_disk_access_fix
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
        "# WvH Texts settings. Sweeps reach only the people and group chats on this list.\n"
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
        print("Contacts couldn't be opened.\n\n" + full_disk_access_fix())
        return None


def _store():
    try:
        return Store()
    except NoAccess:
        path = messages_db()
        if access(path.parent, path) == "missing":
            print("There is no Messages history on this Mac.")
        else:
            print("Messages couldn't be opened.\n\n" + full_disk_access_fix())
        return None


def _shown(handles) -> str:
    return ", ".join(sorted(mask(h) for h in handles))


# ─── Commands ───────────────────────────────────────────────────────────────


def cmd_status(install: str) -> int:
    folder = home_dir(install)
    print(f"Install:          {install}")
    print(f"Settings folder:  {folder}" + ("" if folder.exists() else "  (not created yet)"))
    cfg = load_config(install=install)
    print(f"Owner:            {cfg.owner_name}" + (f" ({len(cfg.owner_handles)} number(s) for \"text me\")" if cfg.owner_handles else ""))
    print("Time zone:        " + (cfg.timezone or "this Mac's own"))
    print(f"List:             {len(cfg.allow)} people, {len(cfg.groups)} group chats"
          f" ({len(cfg.sweep_hold)} on hold, {len(cfg.opted_out)} opted out)")
    print(f"Sending:          {'on: every text is previewed and needs your yes' if cfg.can_send else 'off'}")
    print(f"Scope:            {'clients only: every tool reaches the list only' if cfg.clients_only else 'all: reads who you name; sweeps reach the list'}")
    print(f"Shared nicknames: {len(shared_nicknames.load())} in {shared_nicknames.path()}")
    path = messages_db()
    messages = access(path.parent, path)
    try:
        cards = read_cards(contacts_root())
        contacts = f"ok ({len(cards)} cards with a number or address)"
    except NoAccess:
        contacts = "blocked"
    print("Messages:         " + {"ok": "ok", "blocked": "blocked", "missing": "no Messages history on this Mac"}[messages])
    print(f"Contacts:         {contacts}")
    print(f"Python for macOS: {python_app()}")
    print("                  (Full Disk Access and permission to control Messages go to this Python, and to Claude)")
    if messages == "blocked" or contacts == "blocked":
        print("\n" + full_disk_access_fix())
    if cfg.empty:
        print("\nNo one is on the list yet, so sweeps have nothing to look through. Reading someone by name works.")
        if not folder.exists():
            print("Nothing has been created on this Mac.")
    return 0


def cmd_owner(install: str, name: str, timezone: str | None, numbers: list[str] | None = None) -> int:
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
    if numbers:
        bad = [n for n in numbers if not is_handle(n)]
        if bad:
            print(f"{bad[0]!r} isn't a phone number or email address. Nothing changed.")
            return 2
        data["owner_numbers"] = sorted({normalize_handle(n) for n in numbers})
    write_settings(path, data)
    print(f"Saved. This install works for {name}" + (f", in {timezone}" if timezone else "") + ".")
    if numbers:
        print(f"\"Text me\" goes to {', '.join(mask(normalize_handle(n)) for n in numbers)}.")
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
    print("They're on the list now, so what's new and unnamed searches include their direct texts.")
    print("Group chats they're in aren't swept. Add a group on its own with allow-group.")
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
        print(f"Added the group chat {title!r} ({len(chat.handles)} others in it). Sweeps now include every message in it.")
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
            print(f"Removed the group chat {entry.get('name')!r} from the list. Sweeps no longer reach it.")
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
                print(f"Removed {entry['name']} from the list. Sweeps no longer reach them.")
            write_settings(path, data)
            return 0
        if not handle and str(entry.get("name", "")).lower() == low:
            people.remove(entry)
            write_settings(path, data)
            print(f"Removed {entry['name']} from the list. Sweeps no longer reach them. To keep them out of everything, use opt-out.")
            return 0
    print(f"{query!r} isn't on the list. Nothing changed.")
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
    print(f"Saved for this install: {nick.strip()!r} means {name.strip()}. A nickname never puts anyone on the list.")
    return 0


def cmd_list(install: str) -> int:
    cfg = load_config(install=install)
    print(f"Install {install}, for {cfg.owner_name}. Sending {'on' if cfg.can_send else 'off'}, scope {cfg.scope}.")
    if cfg.empty:
        print("No one is on the list yet.")
    for person in sorted(cfg.allow, key=lambda p: p.name.lower()):
        where = _shown(person.handles) if person.handles else "every number on their card"
        label = f"  [{person.client}]" if person.client else ""
        hold = "  (on hold)" if person.name.lower() in cfg.sweep_hold else ""
        print(f"  person  {person.name}{label}  ({where}){hold}")
    for group in sorted(cfg.groups, key=lambda g: g.name.lower()):
        hold = "  (on hold)" if group.name.lower() in cfg.sweep_hold else ""
        print(f"  group   {group.name}  (id {group.chat}){hold}")
    for name in sorted(cfg.opted_out):
        print(f"  opted out  {name if not is_handle(name) else mask(name)}")
    for nick, name in sorted(cfg.nicknames.items()):
        print(f"  nickname  {nick} = {name}  (this install)")
    shared = shared_nicknames.load()
    if shared:
        print(f"  plus {len(shared)} shared nickname(s) in {shared_nicknames.path()}")
    return 0


def _name_list(data: dict, key: str, name: str, remove: bool) -> tuple[bool, list]:
    entries = data.get(key) or []
    low = name.strip().lower()
    present = [e for e in entries if str(e).strip().lower() == low]
    if remove:
        data[key] = [e for e in entries if str(e).strip().lower() != low]
        return bool(present), data[key]
    if not present:
        entries.append(name.strip())
    data[key] = entries
    return not present, entries


def cmd_sweep_hold(install: str, name: str, remove: bool) -> int:
    path, data = _load(install)
    changed, _ = _name_list(data, "sweep_hold", name, remove)
    if not changed:
        print(f"{name!r} {'is not' if remove else 'is already'} on hold. Nothing changed.")
        return 1 if remove else 0
    write_settings(path, data)
    if remove:
        print(f"{name} is off hold. Sweeps include them again.")
    else:
        print(f"{name} is on hold: readable when you name them, left out of what's new and unnamed searches.")
    return 0


def cmd_opt_out(install: str, name: str, remove: bool) -> int:
    path, data = _load(install)
    changed, _ = _name_list(data, "opted_out", name, remove)
    if not changed:
        print(f"{name!r} {'has not' if remove else 'has already'} opted out. Nothing changed.")
        return 1 if remove else 0
    write_settings(path, data)
    if remove:
        print(f"{name} is back in reach.")
    else:
        print(f"{name} has opted out: no tool reads them or texts them through this connector.")
    return 0


def cmd_sending(install: str, state: str) -> int:
    path, data = _load(install)
    data["sending"] = state == "on"
    write_settings(path, data)
    if state == "on":
        print("Sending is on. Every text is previewed first and goes out only after your yes. Restart Claude to load it.")
    else:
        print("Sending is off: the send tools are gone from this Mac. Restart Claude to load it.")
    return 0


def cmd_scope(install: str, scope: str) -> int:
    path, data = _load(install)
    data["scope"] = scope
    write_settings(path, data)
    if scope == "clients":
        print("Scope is clients only: every tool reaches the list only, and sending is off. Restart Claude to load it.")
    else:
        print("Scope is all: Claude reads who you name; sweeps reach the list. Restart Claude to load it.")
    return 0


def cmd_import(install: str, source: str, to: str | None, nicknames_to: str | None, owner: str | None) -> int:
    """Bring an older WvH Texts people file into this format. Prints counts, never names."""
    src = Path(os.path.expanduser(source))
    try:
        old = yaml.safe_load(src.read_text()) or {}
    except (OSError, yaml.YAMLError):
        print(f"Couldn't read {src}. Nothing changed.")
        return 1
    if not isinstance(old, dict):
        print(f"{src.name} isn't a settings file this command knows. Nothing changed.")
        return 1
    target = Path(os.path.expanduser(to)) if to else settings_path(install)
    data = read_raw(target)
    load_config(target, install)  # refuse to build on a file that doesn't read cleanly

    entries = data.setdefault("allow", []) or []
    data["allow"] = entries
    known = {str(e.get("name", "")).strip().lower() for e in entries if isinstance(e, dict)}
    added = 0
    for entry in old.get("allow_list") or []:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            continue
        name = str(entry["name"]).strip()
        if name.lower() in known:
            continue
        item = {"name": name}  # no handles: every number on their Contacts card, as before
        if str(entry.get("client") or "").strip():
            item["client"] = str(entry["client"]).strip()
        entries.append(item)
        known.add(name.lower())
        added += 1

    held = list(old.get("sweep_hold") or [])
    if old.get("approved") is not True:
        held += [e["name"] for e in entries if isinstance(e, dict) and e.get("name")]  # sweeps weren't open yet
    for key, values in (("sweep_hold", held), ("opted_out", old.get("opted_out") or [])):
        current = data.get(key) or []
        lows = {str(v).strip().lower() for v in current}
        for value in values:
            if str(value).strip() and str(value).strip().lower() not in lows:
                current.append(str(value).strip())
                lows.add(str(value).strip().lower())
        if current:
            data[key] = current
    if owner:
        data["owner_name"] = owner.strip()
    data.setdefault("sending", True)
    data.setdefault("scope", "all")

    counts = {"saved": 0, "known": 0, "conflict": 0}
    old_nicks = old.get("nicknames") or {}
    if isinstance(old_nicks, dict) and old_nicks:
        previous = os.environ.get("WVH_NICKNAMES")
        if nicknames_to:
            os.environ["WVH_NICKNAMES"] = str(Path(os.path.expanduser(nicknames_to)))
        try:
            for nick, name in old_nicks.items():
                counts[shared_nicknames.remember(str(nick), str(name))] += 1
        finally:
            if nicknames_to:
                if previous is None:
                    os.environ.pop("WVH_NICKNAMES", None)
                else:
                    os.environ["WVH_NICKNAMES"] = previous
    write_settings(target, data)
    where = Path(os.path.expanduser(nicknames_to)) if nicknames_to else shared_nicknames.path()
    print(f"Imported into {target}:")
    print(f"  {added} people added to the list (each with every number on their Contacts card)")
    print(f"  {len(data.get('sweep_hold') or [])} on hold, {len(data.get('opted_out') or [])} opted out")
    print(f"  nicknames into {where}: {counts['saved']} saved, {counts['known']} already there, {counts['conflict']} skipped (they meant someone else, or held a number)")
    print("  sending on, scope all" if data.get("sending") and data.get("scope") == "all" else f"  sending {data.get('sending')}, scope {data.get('scope')}")
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
    p.add_argument("--number", action="append", default=[], help="your own number or address, for \"text me\"; repeatable")
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
    p = sub.add_parser("sweep-hold")
    p.add_argument("who")
    p.add_argument("--remove", action="store_true")
    p = sub.add_parser("opt-out")
    p.add_argument("who")
    p.add_argument("--remove", action="store_true")
    p = sub.add_parser("sending")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("scope")
    p.add_argument("scope", choices=["all", "clients"])
    p = sub.add_parser("import")
    p.add_argument("source", help="an older WvH Texts people.yaml")
    p.add_argument("--to", help="write the new settings here instead of this install's folder")
    p.add_argument("--nicknames-to", help="write nicknames here instead of the shared nicknames file")
    p.add_argument("--owner", help="the owner's first name")
    args = parser.parse_args(argv)

    try:
        install = install_name(args.install)
        if args.command == "status":
            return cmd_status(install)
        if args.command == "owner":
            return cmd_owner(install, args.name, args.timezone, args.number)
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
        if args.command == "sweep-hold":
            return cmd_sweep_hold(install, args.who, args.remove)
        if args.command == "opt-out":
            return cmd_opt_out(install, args.who, args.remove)
        if args.command == "sending":
            return cmd_sending(install, args.state)
        if args.command == "scope":
            return cmd_scope(install, args.scope)
        if args.command == "import":
            return cmd_import(install, args.source, args.to, args.nicknames_to, args.owner)
        return cmd_list(install)
    except ConfigError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
