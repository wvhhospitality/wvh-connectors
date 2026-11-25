"""Turn names and nicknames into people, and say who is on the list.

Names come from the Mac's Contacts databases, read directly and read-only, plus two
nickname lists: the one shared by every WvH connector on this Mac (nicknames.py) and
this install's own. Being in Contacts lets the owner read or text someone by name when
they ask. Being on the list is what lets a sweep (what's new, unnamed searches) reach
them. A nickname never puts anyone on the list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import nicknames as shared_nicknames
from .config import Config, contacts_root, is_handle, normalize_handle
from .db import NoAccess, open_readonly

MIN_PARTIAL = 2  # a one-letter partial match would just list the address book


@dataclass
class Person:
    name: str
    handles: set[str] = field(default_factory=set)  # every number and address known for them
    allowed: set[str] = field(default_factory=set)  # the ones a sweep may read (on the list)
    nicknames: set[str] = field(default_factory=set)  # lowercase
    organization: str = ""
    in_contacts: bool = True
    listed: bool = False  # on the list
    client: str | None = None
    opted_out: bool = False
    held: bool = False  # on the list, but left out of sweeps

    @property
    def on_allow_list(self) -> bool:
        return self.listed and bool(self.allowed) and not self.opted_out

    @property
    def words(self) -> set[str]:
        found = set(self.name.lower().split())
        for nickname in self.nicknames:
            found.update(nickname.split())
        return found


@dataclass
class Resolution:
    people: list[Person]  # the best matches, all equally good
    matched_by: str = ""

    @property
    def one(self) -> Person | None:
        return self.people[0] if len(self.people) == 1 else None


def _contact_files(root: Path) -> list[Path]:
    files = sorted(root.glob("Sources/*/AddressBook-v22.abcddb"))
    top = root / "AddressBook-v22.abcddb"
    return files + [top] if top.exists() else files


def read_cards(root: Path | None = None) -> list[Person]:
    """Every Contacts card with a number or address. Raises NoAccess if macOS says no."""
    root = root or contacts_root()
    try:
        os.listdir(root)  # glob alone would hide a refusal behind an empty list
        files = _contact_files(root)
    except FileNotFoundError:
        return []
    except PermissionError as exc:
        raise NoAccess(str(exc)) from exc
    people: list[Person] = []
    for path in files:
        conn = open_readonly(path)
        try:
            found: dict[int, Person] = {}
            for row in conn.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZNAME FROM ZABCDRECORD"
            ):
                full = " ".join(part for part in (row["ZFIRSTNAME"], row["ZLASTNAME"]) if part).strip()
                name = full or row["ZORGANIZATION"] or row["ZNAME"]
                if not name:
                    continue
                found[row["Z_PK"]] = Person(
                    name=name.strip(),
                    nicknames={row["ZNICKNAME"].strip().lower()} if row["ZNICKNAME"] else set(),
                    organization=(row["ZORGANIZATION"] or "").strip(),
                )
            for row in conn.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if row["ZOWNER"] in found and row["ZFULLNUMBER"]:
                    found[row["ZOWNER"]].handles.add(normalize_handle(row["ZFULLNUMBER"]))
            for row in conn.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if row["ZOWNER"] in found and row["ZADDRESS"]:
                    found[row["ZOWNER"]].handles.add(normalize_handle(row["ZADDRESS"]))
            people.extend(person for person in found.values() if person.handles)
        finally:
            conn.close()
    return people


def all_nicknames(config: Config) -> dict[str, str]:
    """This install's own nicknames first, then the shared list fills in the rest."""
    merged = dict(shared_nicknames.load())
    merged.update(config.nicknames)
    return merged


class Directory:
    """Contacts, merged by name, with the list, holds, opt-outs and nicknames laid over it."""

    def __init__(self, cards: list[Person], config: Config):
        merged: dict[str, Person] = {}
        for card in cards:  # the same person is often saved in several accounts
            key = card.name.lower()
            if key in merged:
                merged[key].handles |= card.handles
                merged[key].nicknames |= card.nicknames
                merged[key].organization = merged[key].organization or card.organization
            else:
                merged[key] = Person(card.name, set(card.handles), set(), set(card.nicknames), card.organization)

        for entry in config.allow:
            key = entry.name.lower()
            if key not in merged:
                merged[key] = Person(entry.name, set(entry.handles), in_contacts=False)
            person = merged[key]
            person.listed = True
            person.client = entry.client
            person.handles |= set(entry.handles)
            # A name-only entry means every number and address on their card.
            person.allowed |= set(entry.handles) if entry.handles else set(person.handles)

        for person in merged.values():
            person.opted_out = config.is_opted_out(person.name, person.handles)
            person.held = person.name.lower() in config.sweep_hold

        self._nickname_names = dict(all_nicknames(config))  # nickname -> the full name as written
        self._nicknames = {nick: name.lower() for nick, name in self._nickname_names.items()}
        for nick, target in self._nicknames.items():
            if target in merged:
                merged[target].nicknames.add(nick)

        self.people = sorted(merged.values(), key=lambda p: p.name.lower())
        self.by_handle: dict[str, Person] = {}
        # People on the list first, so a number shared with another card keeps the listed name.
        for person in sorted(self.people, key=lambda p: not p.listed):
            for handle in person.handles:
                self.by_handle.setdefault(handle, person)

    @classmethod
    def load(cls, config: Config, root: Path | None = None) -> tuple["Directory", bool]:
        """The directory, and whether Contacts could be read. Without Contacts, the list still works."""
        try:
            return cls(read_cards(root), config), True
        except NoAccess:
            return cls([], config), False

    def allowed_people(self) -> list[Person]:
        return [person for person in self.people if person.on_allow_list]

    def sweepable(self) -> list[Person]:
        """The list, minus holds and opt-outs: who an automatic sweep may reach."""
        return [person for person in self.allowed_people() if not person.held]

    def name_for(self, handle: str) -> str | None:
        person = self.by_handle.get(normalize_handle(handle))
        return person.name if person else None

    def nickname_target(self, nickname: str) -> str | None:
        """The full name a nickname already means, or None."""
        return self._nickname_names.get(nickname.strip().lower())

    def resolve(self, query: str) -> Resolution:
        """Best match first. The owner's own nicknames outrank everything."""
        text = query.strip()
        low = text.lower()
        if not low:
            return Resolution([])
        if is_handle(text):
            handle = normalize_handle(text)
            known = self.by_handle.get(handle)
            if known is None:
                return Resolution([Person(name=text, handles={handle}, in_contacts=False)], "number or email")
            # Only that one number: asking for a number reads that number's thread.
            narrowed = Person(
                known.name, {handle}, {handle} & known.allowed, set(known.nicknames), known.organization,
                known.in_contacts, known.listed, known.client, known.opted_out, known.held,
            )
            return Resolution([narrowed], "number or email")

        target = self._nicknames.get(low)
        words = low.split()
        tiers = [
            ("nickname", [p for p in self.people if target and p.name.lower() == target]),
            ("full name", [p for p in self.people if p.name.lower() == low]),
            ("contact nickname", [p for p in self.people if low in p.nicknames]),
            ("name", [p for p in self.people if all(word in p.words for word in words)]),
        ]
        for label, found in tiers:
            if found:
                return Resolution(found, label)
        if len(low) >= MIN_PARTIAL:
            found = [p for p in self.people if low in p.name.lower() or low in p.organization.lower()]
            if found:
                return Resolution(found, "partial name")
        return Resolution([])
