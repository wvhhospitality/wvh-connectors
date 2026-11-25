"""Settings: who this install works for, and the allow-list.

Everything personal lives in one settings folder, outside the app, so updating the
app never touches it. Each install has its own name, chosen at setup (for example
`robin-texts`), and that name decides where its settings live:

    ~/Library/Application Support/<install-name>/     (or wherever WVH_TEXTS_HOME points)
        people.yaml    your name, time zone, the allow-list, group chats, nicknames
        reads.jsonl    kept by the connector: who was read and how much (never any text)

The allow-list starts empty. Only the setup command changes it, never a Claude tool,
so nothing a text says can widen it. With an empty list, nothing is read at all.

The server learns its install name from WVH_TEXTS_INSTALL, set when it is registered
with Claude. Without it, the name is `wvh-texts`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_INSTALL = "wvh-texts"
DEFAULT_OWNER = "the owner"
SETTINGS_FILE = "people.yaml"
READS_LOG = "reads.jsonl"

# Same rule Claude uses for a connector's name: lowercase letters, numbers, hyphens.
INSTALL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ConfigError(Exception):
    """A settings file is malformed. The message is safe to show."""


# ─── Which install, and where things live ───────────────────────────────────


def install_name(explicit: str | None = None) -> str:
    name = (explicit or os.environ.get("WVH_TEXTS_INSTALL") or DEFAULT_INSTALL).strip()
    if not INSTALL_NAME.fullmatch(name):
        raise ConfigError(
            f"{name!r} can't be an install name. Use lowercase letters, numbers and hyphens, "
            "for example robin-texts."
        )
    return name


def home_dir(install: str | None = None) -> Path:
    """The settings folder. WVH_TEXTS_HOME moves it; nothing personal lives beside the code."""
    explicit = os.environ.get("WVH_TEXTS_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / "Library" / "Application Support" / install_name(install)


def settings_path(install: str | None = None) -> Path:
    return home_dir(install) / SETTINGS_FILE


def reads_log_path(install: str | None = None) -> Path:
    return home_dir(install) / READS_LOG


def _path(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else default


def messages_dir() -> Path:
    return Path.home() / "Library" / "Messages"


def messages_db() -> Path:
    return _path("WVH_TEXTS_CHAT_DB", messages_dir() / "chat.db")


def attachments_root() -> Path:
    return _path("WVH_TEXTS_ATTACHMENTS", messages_dir() / "Attachments")


def contacts_root() -> Path:
    return _path("WVH_TEXTS_CONTACTS", Path.home() / "Library" / "Application Support" / "AddressBook")


# ─── Phone numbers and addresses ────────────────────────────────────────────

_LOOKS_LIKE_NUMBER = re.compile(r"^[+()\d\s.\-]{7,}$")


def normalize_handle(handle: str) -> str:
    """Phones become their last 10 digits. Emails and short codes stay as they are, lowercased."""
    value = str(handle).strip().lower()
    if "@" in value or re.search(r"[a-z]", value):
        return value
    digits = re.sub(r"\D", "", value)
    return digits[-10:] if len(digits) >= 10 else digits or value


def is_handle(text: str) -> bool:
    """Does this look like a phone number or an email address, rather than a name?"""
    text = text.strip()
    return ("@" in text and " " not in text) or bool(_LOOKS_LIKE_NUMBER.match(text))


def mask(handle: str) -> str:
    """Enough of a number or address to tell two apart, not enough to copy."""
    if "@" in handle:
        user, _, domain = handle.partition("@")
        return f"{user[:1]}…@{domain}"
    return f"…{handle[-4:]}"


# ─── The settings file ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class AllowedPerson:
    name: str
    handles: frozenset[str]  # normalized: only direct threads with these are readable


@dataclass(frozen=True)
class AllowedGroup:
    name: str  # what the owner calls it
    chat: str  # the group's own id in Messages (chat_identifier), which does not change


@dataclass(frozen=True)
class Config:
    install: str
    owner_name: str = DEFAULT_OWNER
    timezone: str | None = None
    allow: tuple[AllowedPerson, ...] = ()
    groups: tuple[AllowedGroup, ...] = ()
    nicknames: dict[str, str] = field(default_factory=dict)  # lowercase nickname -> name

    @property
    def empty(self) -> bool:
        return not self.allow and not self.groups

    def zone(self) -> ZoneInfo | None:
        if not self.timezone:
            return None
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return None


def read_raw(path: Path) -> dict:
    """The settings file as written, or nothing at all: a new install has none."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not read {path.name}: it isn't valid settings.") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} should hold settings, not a bare list or value.")
    return raw


def _person(entry, path: Path) -> AllowedPerson:
    if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
        raise ConfigError(f"Every entry under allow in {path.name} needs a name.")
    handles = entry.get("handles") or []
    if not isinstance(handles, list) or not handles:
        raise ConfigError(f"{entry['name']} in {path.name} needs at least one number or address under handles.")
    return AllowedPerson(
        name=str(entry["name"]).strip(),
        handles=frozenset(normalize_handle(h) for h in handles if str(h).strip()),
    )


def _group(entry, path: Path) -> AllowedGroup:
    if not isinstance(entry, dict) or not str(entry.get("chat") or "").strip():
        raise ConfigError(f"Every entry under groups in {path.name} needs its chat id.")
    chat = str(entry["chat"]).strip()
    return AllowedGroup(name=str(entry.get("name") or chat).strip(), chat=chat)


def load_config(path: Path | None = None, install: str | None = None) -> Config:
    """Parse the settings. A missing file is an install with an empty allow-list, not an error."""
    name = install_name(install)
    cfg_path = path or settings_path(name)
    raw = read_raw(cfg_path)

    allow = tuple(_person(entry, cfg_path) for entry in raw.get("allow") or [])
    groups = tuple(_group(entry, cfg_path) for entry in raw.get("groups") or [])
    nicknames_raw = raw.get("nicknames") or {}
    if not isinstance(nicknames_raw, dict):
        raise ConfigError(f"nicknames in {cfg_path.name} should pair each nickname with a name.")
    nicknames = {str(k).strip().lower(): str(v).strip() for k, v in nicknames_raw.items() if str(k).strip() and str(v).strip()}

    owner = os.environ.get("WVH_TEXTS_OWNER") or raw.get("owner_name")
    owner = str(owner).strip() if owner and str(owner).strip() else DEFAULT_OWNER
    tz = raw.get("timezone")
    tz = str(tz).strip() if tz and str(tz).strip() else None
    return Config(install=name, owner_name=owner, timezone=tz, allow=allow, groups=groups, nicknames=nicknames)


def setup_command(install: str) -> str:
    """The command the owner runs to change the allow-list, as Claude should quote it."""
    return f"~/wvh-connectors/texts/.venv/bin/wvh-texts-setup --install {install}"
