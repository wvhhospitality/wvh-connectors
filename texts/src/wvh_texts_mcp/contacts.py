"""Turn names and nicknames into people, and say who is on the allow-list.

Names come from the Mac's Contacts databases, read directly and read-only, plus the
nickname list in settings. Being in Contacts never makes anyone readable: only the
numbers and addresses saved on the allow-list at setup do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, contacts_root, is_handle, normalize_handle
from .db import NoAccess, open_readonly

MIN_PARTIAL = 2  # a one-letter partial match would just list the address book


@dataclass
class Person:
    name: str
    handles: set[str] = field(default_factory=set)  # every number and address known for them
    allowed: set[str] = field(default_factory=set)  # the ones on the allow-list: what may be read
    nicknames: set[str] = field(default_factory=set)  # lowercase
    organization: str = ""
    in_contacts: bool = True

    @property
    def on_allow_list(self) -> bool:
        return bool(self.allowed)

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


class Directory:
    """Contacts, merged by name, with the allow-list laid over it."""

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
            merged[key].allowed |= set(entry.handles)
            merged[key].handles |= set(entry.handles)

        self._nicknames = {nick: name.lower() for nick, name in config.nicknames.items()}
        for nick, target in self._nicknames.items():
            if target in merged:
                merged[target].nicknames.add(nick)

        self.people = sorted(merged.values(), key=lambda p: p.name.lower())
        self.by_handle: dict[str, Person] = {}
        # People on the allow-list first, so a number shared with another card keeps the allowed name.
        for person in sorted(self.people, key=lambda p: not p.on_allow_list):
            for handle in person.handles:
                self.by_handle.setdefault(handle, person)

    @classmethod
    def load(cls, config: Config, root: Path | None = None) -> tuple["Directory", bool]:
        """The directory, and whether Contacts could be read. Without Contacts, the allow-list still works."""
        try:
            return cls(read_cards(root), config), True
        except NoAccess:
            return cls([], config), False

    def allowed_people(self) -> list[Person]:
        return [person for person in self.people if person.on_allow_list]

    def name_for(self, handle: str) -> str | None:
        person = self.by_handle.get(normalize_handle(handle))
        return person.name if person else None

    def resolve(self, query: str) -> Resolution:
        """Best match first. The owner's own nickname list outranks everything."""
        text = query.strip()
        low = text.lower()
        if not low:
            return Resolution([])
        if is_handle(text):
            handle = normalize_handle(text)
            known = self.by_handle.get(handle)
            if known is None:
                return Resolution([Person(name=text, handles={handle}, in_contacts=False)], "number or email")
            if handle not in known.allowed:
                # The card may be allowed for other numbers, but not this one.
                return Resolution([Person(known.name, {handle}, set(), organization=known.organization)], "number or email")
            return Resolution([Person(known.name, {handle}, {handle}, organization=known.organization)], "number or email")

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
