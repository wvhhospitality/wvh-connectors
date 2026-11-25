"""Settings, the mailbox list, and Keychain logins.

Everything personal lives in one settings folder, outside the app, so updating the
app never touches it. Each install has its own name, chosen at setup (for example
`robin-mail`), and that name decides where its settings and logins are:

    ~/Library/Application Support/<install-name>/     (or wherever WVH_MAIL_HOME points)
        accounts.yaml   your name, time zone, and your mailboxes (no passwords)
        drafts.jsonl    kept by the connector: a record of every draft it saved (no bodies)

    macOS Keychain      one app password per mailbox: service <install-name>, account <address>

The server learns its install name from WVH_MAIL_INSTALL, set when it is registered
with Claude. Without it, the name is `wvh-mail`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import keyring
import yaml

DEFAULT_INSTALL = "wvh-mail"
DEFAULT_OWNER = "the owner"
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
SETTINGS_FILE = "accounts.yaml"
DRAFTS_LOG = "drafts.jsonl"

# Same rule Claude uses for a connector's name: lowercase letters, numbers, hyphens.
INSTALL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


class ConfigError(Exception):
    """A settings file is malformed, or a mailbox can't be found."""


class CredentialError(Exception):
    """No login is stored in the Keychain for a mailbox."""


# ─── Which install, and where its settings live ─────────────────────────────


def install_name(explicit: str | None = None) -> str:
    name = (explicit or os.environ.get("WVH_MAIL_INSTALL") or DEFAULT_INSTALL).strip()
    if not INSTALL_NAME.fullmatch(name):
        raise ConfigError(
            f"{name!r} can't be an install name. Use lowercase letters, numbers and hyphens, "
            "for example robin-mail."
        )
    return name


def keychain_service(install: str | None = None) -> str:
    """The Keychain service holding this install's app passwords: the install name itself."""
    return install_name(install)


def home_dir(install: str | None = None) -> Path:
    """The settings folder. WVH_MAIL_HOME moves it; nothing personal lives beside the code."""
    explicit = os.environ.get("WVH_MAIL_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / "Library" / "Application Support" / install_name(install)


def settings_path(install: str | None = None) -> Path:
    return home_dir(install) / SETTINGS_FILE


def drafts_log_path(install: str | None = None) -> Path:
    return home_dir(install) / DRAFTS_LOG


# ─── The settings file ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Account:
    email: str
    label: str = ""
    imap_host: str = DEFAULT_IMAP_HOST
    imap_port: int = DEFAULT_IMAP_PORT
    ssl: bool = True

    @property
    def name(self) -> str:
        """How to say which mailbox: its label, or its address."""
        return self.label or self.email

    def describe(self) -> dict:
        d = {"email": self.email}
        if self.label:
            d["label"] = self.label
        return d


@dataclass(frozen=True)
class Config:
    install: str
    owner_name: str
    timezone: str | None
    accounts: tuple[Account, ...]

    def find(self, ref: str) -> Account:
        """A mailbox by address or label, ignoring case."""
        needle = (ref or "").strip().lower()
        for acct in self.accounts:
            if acct.email == needle or (acct.label and acct.label.lower() == needle):
                return acct
        known = ", ".join(a.name for a in self.accounts) or "none yet"
        raise ConfigError(f"No mailbox called {ref!r}. Set up: {known}.")

    def zone(self) -> ZoneInfo | None:
        if not self.timezone:
            return None
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def __iter__(self) -> Iterator[Account]:
        return iter(self.accounts)


def read_raw(path: Path) -> dict:
    """The settings file as written, or nothing at all: a new install has none."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} should hold settings, not a bare list or value.")
    return raw


def _account(entry: dict, path: Path) -> Account:
    email = str(entry.get("email") or "").strip().lower()
    if not EMAIL.fullmatch(email):
        raise ConfigError(f"A mailbox in {path} has no usable email address: {entry!r}")
    try:
        port = int(entry.get("imap_port") or DEFAULT_IMAP_PORT)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"imap_port for {email} in {path} isn't a number.") from exc
    return Account(
        email=email,
        label=str(entry.get("label") or "").strip(),
        imap_host=str(entry.get("imap_host") or DEFAULT_IMAP_HOST).strip(),
        imap_port=port,
        ssl=bool(entry.get("ssl", True)),
    )


def load_config(path: Path | None = None, install: str | None = None) -> Config:
    """Parse the settings. A missing file is an install with no mailboxes yet, not an error."""
    name = install_name(install)
    cfg_path = path or settings_path(name)
    raw = read_raw(cfg_path)

    accounts: list[Account] = []
    for entry in raw.get("accounts") or []:
        if isinstance(entry, dict):
            accounts.append(_account(entry, cfg_path))

    seen: set[str] = set()
    for acct in accounts:
        for key in filter(None, (acct.email, acct.label.lower())):
            if key in seen:
                raise ConfigError(f"Two mailboxes in {cfg_path} are both called {key!r}.")
            seen.add(key)

    owner = os.environ.get("WVH_MAIL_OWNER") or raw.get("owner_name")
    owner = str(owner).strip() if owner and str(owner).strip() else DEFAULT_OWNER
    tz = raw.get("timezone")
    tz = str(tz).strip() if tz and str(tz).strip() else None
    return Config(install=name, owner_name=owner, timezone=tz, accounts=tuple(accounts))


# ─── Logins ──────────────────────────────────────────────────────────────────


def get_password(account: Account, install: str | None = None) -> str:
    """The mailbox's app password from the Keychain. Never logged, never returned by a tool."""
    service = keychain_service(install)
    pw = keyring.get_password(service, account.email)
    if not pw:
        raise CredentialError(
            f"No login is stored for {account.email}. Run setup for it again: "
            f"wvh-mail-setup --install {service} add {account.email}"
        )
    return pw


def has_password(account: Account, install: str | None = None) -> bool:
    try:
        return bool(keyring.get_password(keychain_service(install), account.email))
    except Exception:  # noqa: BLE001 - a locked or missing Keychain reads as "not stored"
        return False
