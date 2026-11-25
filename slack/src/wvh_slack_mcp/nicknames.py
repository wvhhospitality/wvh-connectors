"""Nicknames the owner uses for people, shared by every WvH connector on this Mac.

    ~/Library/Application Support/wvh-connectors/nicknames.yaml

    nicknames:
      Danny: Dana Northwind

Slack, mail and texts all read this one file, so a nickname confirmed in one is known
to all three. A connector writes to it only after the owner says yes to a preview that
used a new nickname. It holds names only, never an address, a number or a login, and a
nickname never grants anything by itself: every message is still previewed.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from pathlib import Path

import yaml

HEADER = "# Nicknames you use for people. Written when you confirm one; you can edit it.\n"


def path() -> Path:
    explicit = os.environ.get("WVH_NICKNAMES")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("HOME") or Path.home())
    return home / "Library" / "Application Support" / "wvh-connectors" / "nicknames.yaml"


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def load() -> dict[str, str]:
    """{nickname (lowercase): full name}. A missing or unreadable file is simply no nicknames."""
    try:
        data = yaml.safe_load(path().read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    table = data.get("nicknames") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return {}
    return {_clean(str(k)).lower(): _clean(str(v)) for k, v in table.items() if _clean(str(k)) and _clean(str(v))}


def lookup(nickname: str) -> str | None:
    return load().get(_clean(nickname).lower())


def nicknames_for(full_name: str) -> list[str]:
    want = _clean(full_name).lower()
    return [nick for nick, name in load().items() if name.lower() == want]


def remember(nickname: str, full_name: str) -> str:
    """Save one nickname. Returns 'saved', 'known', or 'conflict' (it already means someone else)."""
    nickname, full_name = _clean(nickname), _clean(full_name)
    if not nickname or not full_name or "@" in nickname + full_name or re.search(r"\d{3}", nickname + full_name):
        return "conflict"  # names only: never an address or a number
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".nicknames.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = (yaml.safe_load(target.read_text()) or {}) if target.exists() else {}
        except yaml.YAMLError:
            return "conflict"  # never overwrite a file we can't read
        if not isinstance(data, dict):
            data = {}
        table = data.get("nicknames") if isinstance(data.get("nicknames"), dict) else {}
        for key, value in table.items():
            if _clean(str(key)).lower() == nickname.lower():
                return "known" if _clean(str(value)).lower() == full_name.lower() else "conflict"
        table[nickname] = full_name
        data["nicknames"] = table
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".nicknames.")
        with os.fdopen(fd, "w") as f:
            f.write(HEADER)
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    return "saved"
