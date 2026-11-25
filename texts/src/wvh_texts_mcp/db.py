"""The one way this package opens a database: read-only, twice over. And whether it can.

Nothing in this package ever writes to Messages or Contacts. Sending goes through the
Messages app itself (see sending.py), never through its database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

def full_disk_access_fix() -> str:
    """What to switch on, naming the exact program macOS checks on this Mac."""
    from .config import python_app

    return (
        "macOS hasn't given this connector permission to read Messages and Contacts. Claude starts the "
        "connector as its own program, so macOS checks the Python that runs it, not only Claude. To allow it: "
        "open System Settings, then Privacy & Security, then Full Disk Access. Click +, press Command-Shift-G, "
        f"paste {python_app()} , press Return, click Open, and turn on Python. Turn on Claude too (click + and "
        "choose Claude in Applications if it isn't listed). Then quit Claude completely and open it again."
    )


# Kept for anything that imported the old name; the text is worked out when it's used.
FULL_DISK_ACCESS_FIX = "Full Disk Access is missing. Run the setup command's status to see exactly what to switch on."


class NoAccess(Exception):
    """macOS refused to let this process read a database. The message is safe to show."""


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open `path` so that neither SQLite nor this code can change it.

    mode=ro keeps Messages' write-ahead log readable, so the newest texts show up.
    immutable=1 would hide them. query_only is the second lock.
    """
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, PermissionError) as exc:
        raise NoAccess(str(exc)) from exc
    return conn


def access(folder: Path, database: Path) -> str:
    """'ok', 'blocked' (macOS says no: Full Disk Access is missing), or 'missing'."""
    try:
        os.listdir(folder)
    except PermissionError:
        return "blocked"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "blocked"
    try:
        if not database.exists():
            return "missing"
    except PermissionError:
        return "blocked"
    try:
        open_readonly(database).close()
    except NoAccess:
        return "blocked"
    return "ok"
