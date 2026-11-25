"""Names for addresses from the Mac's Contacts, read-only and best effort.

Used only to tell who an address belongs to, so the owner's words ("email Max") can be
matched to the address a message would go to. If Contacts can't be opened (no
permission, no Contacts at all), this answers nothing and mail carries on with what the
mailbox itself shows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_QUERY = """
SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZNICKNAME, r.ZORGANIZATION, e.ZADDRESS
FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK
WHERE e.ZADDRESS IS NOT NULL
"""


def db_paths() -> list[Path]:
    root = Path.home() / "Library" / "Application Support" / "AddressBook"
    found = sorted(root.glob("Sources/*/AddressBook-v22.abcddb"))
    main = root / "AddressBook-v22.abcddb"
    return found + ([main] if main.exists() else [])


def _rows() -> list[tuple]:
    rows: list[tuple] = []
    for path in db_paths():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            continue
        try:
            rows += con.execute(_QUERY).fetchall()
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return rows


def names_for(address: str) -> set[str]:
    """Every name Contacts has for this address: full name, first name, nickname."""
    want = (address or "").strip().lower()
    names: set[str] = set()
    for first, last, nick, org, addr in _rows():
        if str(addr or "").strip().lower() != want:
            continue
        full = " ".join(p for p in (first, last) if p)
        names.update(n for n in (full, first, nick) if n)  # not the company: too many people share one
    return names


def addresses_for(name: str) -> set[str]:
    """Addresses on every card whose full name, first name or nickname is this name."""
    want = " ".join((name or "").split()).lower()
    if not want:
        return set()
    found: set[str] = set()
    for first, last, nick, _org, addr in _rows():
        full = " ".join(p for p in (first, last) if p).lower()
        if want in {full, str(first or "").lower(), str(nick or "").lower()} and addr:
            found.add(str(addr).strip().lower())
    return found
