"""Settings, the mailbox list, who signs what, and Keychain logins.

Everything personal lives in one settings folder, outside the app, so updating the
app never touches it. Each install has its own name, chosen at setup (for example
`robin-mail`), and that name decides where its settings and logins are:

    ~/Library/Application Support/<install-name>/     (or wherever WVH_MAIL_HOME points)
        accounts.yaml   your name, time zone, mailboxes, identities, and this Mac's switches
        signatures/     signature files (HTML, with an optional .txt beside each)
        sent.jsonl      kept by the connector: every send, refusal and failure (no bodies)
        drafts.jsonl    kept by the connector: every draft it saved (no bodies)

    macOS Keychain      one app password per mailbox: service <keychain_service>, account <address>
                        (keychain_service is the install name unless the settings say otherwise)

The server learns its install name from WVH_MAIL_INSTALL, set when it is registered
with Claude. Without it, the name is `wvh-mail`. WVH_MAIL_CONFIG names one settings
file directly, for a Mac that keeps it somewhere else.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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
SENT_LOG = "sent.jsonl"
SIGNATURES = "signatures"
DEFAULT_REMOTE_PORT = 27129

# Same rule Claude uses for a connector's name: lowercase letters, numbers, hyphens.
INSTALL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_SIGNATURE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.html?$")

# Where a mail server's sending side usually is, by its reading side.
_SMTP_BY_IMAP = {
    "imap.gmail.com": ("smtp.gmail.com", 465, False),
    "imap.mail.me.com": ("smtp.mail.me.com", 587, True),
    "imap.fastmail.com": ("smtp.fastmail.com", 465, False),
    "imap.mail.yahoo.com": ("smtp.mail.yahoo.com", 465, False),
}


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


def home_dir(install: str | None = None) -> Path:
    """The settings folder. WVH_MAIL_HOME moves it; nothing personal lives beside the code."""
    explicit = os.environ.get("WVH_MAIL_HOME")
    if explicit:
        return Path(explicit).expanduser()
    explicit_file = os.environ.get("WVH_MAIL_CONFIG")
    if explicit_file:
        return Path(explicit_file).expanduser().parent
    return Path.home() / "Library" / "Application Support" / install_name(install)


def settings_path(install: str | None = None) -> Path:
    explicit_file = os.environ.get("WVH_MAIL_CONFIG")
    if explicit_file and not os.environ.get("WVH_MAIL_HOME"):
        return Path(explicit_file).expanduser()
    return home_dir(install) / SETTINGS_FILE


def drafts_log_path(install: str | None = None) -> Path:
    return home_dir(install) / DRAFTS_LOG


def sent_log_path(install: str | None = None) -> Path:
    return home_dir(install) / SENT_LOG


def signatures_dir(install: str | None = None) -> Path:
    return home_dir(install) / SIGNATURES


def downloads_dir(install: str | None = None) -> Path:
    """Where saved attachments go by default: ~/Downloads/<install-name>/."""
    return Path.home() / "Downloads" / install_name(install)


# ─── The settings file ──────────────────────────────────────────────────────


def default_smtp(imap_host: str) -> tuple[str, int, bool]:
    """(host, port, starttls) for a reading host. Unknown hosts guess smtp.<rest>, port 465."""
    host = imap_host.strip().lower()
    if host in _SMTP_BY_IMAP:
        return _SMTP_BY_IMAP[host]
    if host.startswith("imap."):
        return "smtp." + host[5:], 465, False
    return host, 465, False


@dataclass(frozen=True)
class Account:
    email: str
    label: str = ""
    imap_host: str = DEFAULT_IMAP_HOST
    imap_port: int = DEFAULT_IMAP_PORT
    ssl: bool = True
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_starttls: bool = False
    save_sent: bool = False       # append a copy to Sent after sending (Gmail does it by itself)
    identity: str | None = None   # who signs mail from this mailbox unless told otherwise
    signature: str | None = None  # a signature file for this mailbox when no identity is named

    @property
    def name(self) -> str:
        """How to say which mailbox: its label, or its address."""
        return self.label or self.email

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1]

    def describe(self) -> dict:
        d = {"email": self.email}
        if self.label:
            d["label"] = self.label
        return d


@dataclass(frozen=True)
class Identity:
    """Who is writing: a person, or an assistant writing for them. Signs with its own signature."""

    name: str
    signature: str | None = None                 # file in signatures/
    by_domain: dict = field(default_factory=dict)  # sending domain or address -> signature file
    drafts_only: bool = False                    # may save drafts, never send

    def signature_for(self, account: Account) -> str | None:
        table = {str(k).lower(): v for k, v in (self.by_domain or {}).items()}
        return table.get(account.email) or table.get(account.domain) or self.signature


@dataclass(frozen=True)
class SendLimits:
    max_per_window: int = 10     # sends per mailbox per window: stops a runaway loop
    window_seconds: int = 300
    max_recipients: int = 20     # To + Cc + Bcc per message


@dataclass(frozen=True)
class Config:
    install: str
    owner_name: str
    timezone: str | None
    accounts: tuple[Account, ...]
    keychain_service: str = ""
    sending: bool = True          # this Mac may send (each send still needs the owner's yes)
    drafts: bool = True           # this Mac may save drafts and remove its own
    remote_drafts: bool = False   # the read-only remote may save drafts (never send)
    remote_port: int = DEFAULT_REMOTE_PORT
    internal_domains: tuple[str, ...] = ()
    identities: tuple[Identity, ...] = ()
    default_identity: str | None = None
    send_limits: SendLimits = field(default_factory=SendLimits)

    def find(self, ref: str) -> Account:
        """A mailbox by address or label, ignoring case."""
        needle = (ref or "").strip().lower()
        for acct in self.accounts:
            if acct.email == needle or (acct.label and acct.label.lower() == needle):
                return acct
        known = ", ".join(a.name for a in self.accounts) or "none yet"
        raise ConfigError(f"No mailbox called {ref!r}. Set up: {known}.")

    def identity(self, name: str | None) -> Identity | None:
        if not name:
            return None
        for ident in self.identities:
            if ident.name.lower() == str(name).strip().lower():
                return ident
        known = ", ".join(i.name for i in self.identities) or "none set up"
        raise ConfigError(f"No identity called {name!r}. Set up: {known}.")

    def identity_for(self, account: Account, name: str | None = None) -> Identity | None:
        """The named identity, else the mailbox's own, else the install's default, else nobody."""
        return self.identity(name or account.identity or self.default_identity)

    @property
    def own_addresses(self) -> set[str]:
        return {a.email for a in self.accounts}

    @property
    def inside_domains(self) -> set[str]:
        return set(self.internal_domains) or {a.domain for a in self.accounts}

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


def is_legacy(raw: dict) -> bool:
    """The older WvH Mail format: accounts keyed by client_code with a secret_ref."""
    accounts = raw.get("accounts") or []
    return any(isinstance(a, dict) and ("secret_ref" in a or "client_code" in a) for a in accounts)


def _switch(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("on", "yes", "true", "1"):
        return True
    if text in ("off", "no", "false", "0"):
        return False
    return default


def _env_switch(name: str, current: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return current
    return _switch(value, current)


def _signature_name(value, where: str) -> str | None:
    if value in (None, ""):
        return None
    name = str(value).strip()
    if not _SIGNATURE_FILE.fullmatch(name) or "/" in name or ".." in name:
        raise ConfigError(f"{where}: {name!r} isn't a signature file name. Use a plain name like robin.html.")
    return name


def _int(value, default: int, what: str, path: Path) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} in {path} isn't a number.") from exc


def _account(entry: dict, path: Path) -> Account:
    email = str(entry.get("email") or "").strip().lower()
    if not EMAIL.fullmatch(email):
        raise ConfigError(f"A mailbox in {path} has no usable email address: {entry!r}")
    imap_host = str(entry.get("imap_host") or DEFAULT_IMAP_HOST).strip()
    smtp_host, smtp_port, starttls = default_smtp(imap_host)
    if entry.get("smtp_host"):
        smtp_host = str(entry["smtp_host"]).strip()
    smtp_port = _int(entry.get("smtp_port"), smtp_port, f"smtp_port for {email}", path)
    starttls = _switch(entry.get("smtp_starttls"), starttls if not entry.get("smtp_port") else smtp_port == 587)
    return Account(
        email=email,
        label=str(entry.get("label") or "").strip(),
        imap_host=imap_host,
        imap_port=_int(entry.get("imap_port"), DEFAULT_IMAP_PORT, f"imap_port for {email}", path),
        ssl=bool(entry.get("ssl", True)),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_starttls=starttls,
        save_sent=_switch(entry.get("save_sent"), not imap_host.lower().endswith("gmail.com")),
        identity=(str(entry["identity"]).strip() or None) if entry.get("identity") else None,
        signature=_signature_name(entry.get("signature"), f"signature for {email}"),
    )


def _identity(entry: dict, path: Path) -> Identity:
    name = str(entry.get("name") or "").strip()
    if not name:
        raise ConfigError(f"An identity in {path} has no name: {entry!r}")
    by_domain = entry.get("by_domain") or {}
    if not isinstance(by_domain, dict):
        raise ConfigError(f"by_domain for identity {name} in {path} should map a domain to a signature file.")
    return Identity(
        name=name,
        signature=_signature_name(entry.get("signature"), f"identity {name}"),
        by_domain={str(k).strip().lower(): _signature_name(v, f"identity {name}") for k, v in by_domain.items()},
        drafts_only=_switch(entry.get("drafts_only"), False),
    )


def load_config(path: Path | None = None, install: str | None = None) -> Config:
    """Parse the settings. A missing file is an install with no mailboxes yet, not an error."""
    name = install_name(install)
    cfg_path = path or settings_path(name)
    raw = read_raw(cfg_path)
    if is_legacy(raw):
        raise ConfigError(
            f"{cfg_path} is in the older WvH Mail format. Convert it once with: "
            f"wvh-mail-setup --install {name} import {cfg_path}"
        )

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

    identities = tuple(_identity(e, cfg_path) for e in (raw.get("identities") or []) if isinstance(e, dict))
    names = [i.name.lower() for i in identities]
    if len(names) != len(set(names)):
        raise ConfigError(f"Two identities in {cfg_path} share a name.")
    default_identity = str(raw.get("default_identity") or "").strip() or None
    for ref in [default_identity] + [a.identity for a in accounts]:
        if ref and ref.lower() not in names:
            raise ConfigError(f"{cfg_path} names an identity {ref!r} that isn't set up under identities.")

    limits_raw = raw.get("send_limits") or {}
    limits = SendLimits(
        max_per_window=_int(limits_raw.get("max_per_window"), 10, "send_limits.max_per_window", cfg_path),
        window_seconds=_int(limits_raw.get("window_seconds"), 300, "send_limits.window_seconds", cfg_path),
        max_recipients=_int(limits_raw.get("max_recipients"), 20, "send_limits.max_recipients", cfg_path),
    )

    owner = os.environ.get("WVH_MAIL_OWNER") or raw.get("owner_name")
    owner = str(owner).strip() if owner and str(owner).strip() else DEFAULT_OWNER
    tz = raw.get("timezone")
    tz = str(tz).strip() if tz and str(tz).strip() else None
    service = str(raw.get("keychain_service") or "").strip() or name
    internal = tuple(sorted({str(d).strip().lower().lstrip("@") for d in (raw.get("internal_domains") or []) if str(d).strip()}))
    return Config(
        install=name,
        owner_name=owner,
        timezone=tz,
        accounts=tuple(accounts),
        keychain_service=service,
        sending=_env_switch("WVH_MAIL_SENDING", _switch(raw.get("sending"), True)),
        drafts=_env_switch("WVH_MAIL_DRAFTS", _switch(raw.get("drafts"), True)),
        remote_drafts=_env_switch("WVH_MAIL_REMOTE_DRAFTS", _switch(raw.get("remote_drafts"), False)),
        remote_port=_int(os.environ.get("WVH_MAIL_REMOTE_PORT") or raw.get("remote_port"), DEFAULT_REMOTE_PORT, "remote_port", cfg_path),
        internal_domains=internal,
        identities=identities,
        default_identity=default_identity,
        send_limits=limits,
    )


# ─── Logins ──────────────────────────────────────────────────────────────────


def keychain_service(install: str | None = None) -> str:
    """The Keychain service holding this install's app passwords: the settings' keychain_service,
    else the install name. Read fresh, so an import that sets it is used at once."""
    name = install_name(install)
    try:
        raw = read_raw(settings_path(name))
    except ConfigError:
        raw = {}
    return str(raw.get("keychain_service") or "").strip() or name


def get_password(account: Account, install: str | None = None) -> str:
    """The mailbox's app password from the Keychain. Never logged, never returned by a tool."""
    service = keychain_service(install)
    pw = keyring.get_password(service, account.email)
    if not pw:
        raise CredentialError(
            f"No login is stored for {account.email}. Run setup for it again: "
            f"wvh-mail-setup --install {install_name(install)} password {account.email}"
        )
    return pw


def has_password(account: Account, install: str | None = None) -> bool:
    try:
        return bool(keyring.get_password(keychain_service(install), account.email))
    except Exception:  # noqa: BLE001 - a locked or missing Keychain reads as "not stored"
        return False
