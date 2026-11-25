"""Reading mail and saving drafts over IMAP (the protocol mail apps use to read a mailbox).

What this module does NOT do is the point of it:
  - It cannot send. There is no SMTP (the sending protocol) anywhere in this connector.
  - It never marks mail read: every fetch is BODY.PEEK, and folders being read are
    opened read-only (EXAMINE).
  - It never erases anything. No \\Deleted flag, no EXPUNGE, no flags of any kind on
    received or sent mail.
  - The one change it makes besides saving a draft: when a new draft replaces one this
    connector saved earlier, the old one moves to Trash, where Gmail keeps it 30 days.
    A draft the owner wrote themselves is never touched.

Each mailbox gets a connection on demand, reused for a few minutes, then closed. A
mailbox that fails is reported as such; it never stops the others.
"""

from __future__ import annotations

import email
import email.header
import logging
import re
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from email.message import Message
from email.utils import getaddresses
from typing import Iterator
from zoneinfo import ZoneInfo

import certifi
from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from .attachments import is_attachment, list_attachments
from .config import Account, get_password


def _ssl_context() -> ssl.SSLContext:
    """python.org Python on macOS doesn't trust the system's certificates; certifi's bundle works everywhere."""
    return ssl.create_default_context(cafile=certifi.where())


log = logging.getLogger(__name__)

INBOX = "INBOX"
# Gmail's usual names, used when a server doesn't say which folder is which.
FALLBACK_FOLDERS = {
    "sent": "[Gmail]/Sent Mail",
    "all": "[Gmail]/All Mail",
    "drafts": "[Gmail]/Drafts",
    "trash": "[Gmail]/Trash",
}
_SPECIAL_FLAGS = {"sent": b"\\Sent", "all": b"\\All", "drafts": b"\\Drafts", "trash": b"\\Trash"}

# Every draft this connector saves gets a Message-ID ending in this word, so it can tell
# its own drafts from ones the owner wrote in Gmail, Apple Mail or on a phone.
DRAFT_MARK = "wvhmail"
_OUR_MESSAGE_ID = re.compile(r"^<\d+\.\d+\.\d+\." + DRAFT_MARK + r"@[^<>@\s]+>$")
_APPENDUID = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")

IDLE_TIMEOUT_SECONDS = 300
MAX_BODY_CHARS = 64 * 1024
_SNIPPET_LEN = 180


class DraftRefused(Exception):
    """The old draft wasn't saved by this connector, so it was left where it is."""


# ─── Models ──────────────────────────────────────────────────────────────────


@dataclass
class MessageSummary:
    account: str
    label: str
    uid: int
    from_: str
    to: list[str]
    subject: str
    date: str | None
    snippet: str
    unread: bool

    def to_dict(self) -> dict:
        d = {"account": self.account}
        if self.label:
            d["label"] = self.label
        d.update(
            uid=self.uid, **{"from": self.from_}, to=self.to, subject=self.subject,
            date=self.date, snippet=self.snippet, unread=self.unread,
        )
        return d


@dataclass
class MessageDetail:
    summary: MessageSummary
    headers: dict[str, str]
    body_text: str
    body_truncated: bool
    attachments: list[dict]

    def to_dict(self) -> dict:
        d = self.summary.to_dict()
        d.update(headers=self.headers, body_text=self.body_text, body_truncated=self.body_truncated, attachments=self.attachments)
        return d


@dataclass
class AccountStatus:
    email: str
    label: str
    status: str  # ok | auth_error | unreachable | no_login | error
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"email": self.email}
        if self.label:
            d["label"] = self.label
        d["status"] = self.status
        if self.error:
            d["error"] = self.error
        return d


# ─── Connections ─────────────────────────────────────────────────────────────


def open_client(account: Account, password: str) -> IMAPClient:
    client = IMAPClient(
        host=account.imap_host,
        port=account.imap_port,
        ssl=account.ssl,
        ssl_context=_ssl_context() if account.ssl else None,
        timeout=30,
    )
    try:
        client.login(account.email, password)
    except Exception:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
        raise
    return client


@dataclass
class _Pooled:
    client: IMAPClient
    last_used: float = field(default_factory=time.time)
    folders: dict[str, str] = field(default_factory=dict)


class IMAPPool:
    """One connection per mailbox, opened on demand and closed when idle."""

    def __init__(self, install: str) -> None:
        self.install = install
        self._connections: dict[str, _Pooled] = {}
        self._lock = threading.Lock()

    @contextmanager
    def connection(self, account: Account) -> Iterator[IMAPClient]:
        pooled = self._acquire(account)
        try:
            yield pooled.client
        except (OSError, socket.error) as exc:
            log.warning("Connection problem for %s: %s", account.email, exc)
            self._drop(account.email)
            raise

    def folder(self, account: Account, client: IMAPClient, name: str | None) -> str:
        """inbox / sent / all / drafts / trash, or a folder's own name passed through."""
        key = (name or "inbox").strip().lower()
        if key == "inbox":
            return INBOX
        if key not in _SPECIAL_FLAGS:
            return name  # a folder's real name, as the server spells it
        pooled = self._connections.get(account.email)
        if pooled is not None and key in pooled.folders:
            return pooled.folders[key]
        found = None
        try:
            found = client.find_special_folder(_SPECIAL_FLAGS[key])
        except Exception:  # noqa: BLE001 - not every server says; Gmail's names are the fallback
            pass
        resolved = found or FALLBACK_FOLDERS[key]
        if pooled is not None:
            pooled.folders[key] = resolved
        return resolved

    def _acquire(self, account: Account) -> _Pooled:
        with self._lock:
            self._reap_idle()
            existing = self._connections.get(account.email)
            if existing is not None:
                existing.last_used = time.time()
                return existing
            pooled = _Pooled(client=open_client(account, get_password(account, self.install)))
            self._connections[account.email] = pooled
            return pooled

    def _drop(self, address: str) -> None:
        with self._lock:
            pooled = self._connections.pop(address, None)
        if pooled is not None:
            _quiet_logout(pooled.client)

    def _reap_idle(self) -> None:
        now = time.time()
        for key in [k for k, v in self._connections.items() if now - v.last_used > IDLE_TIMEOUT_SECONDS]:
            pooled = self._connections.pop(key, None)
            if pooled:
                _quiet_logout(pooled.client)

    def close_all(self) -> None:
        with self._lock:
            for pooled in self._connections.values():
                _quiet_logout(pooled.client)
            self._connections.clear()


def _quiet_logout(client: IMAPClient) -> None:
    try:
        client.logout()
    except Exception:  # noqa: BLE001
        pass


# ─── Reading ─────────────────────────────────────────────────────────────────


def _unfold(value) -> str:
    """One line: a long header's folding line breaks become spaces."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = re.sub(r"\r?\n[ \t]+", " ", str(value))
    return re.sub(r"[\r\n]+", " ", value).strip()


def decode_header(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = _unfold(raw)
    try:
        return _unfold(str(email.header.make_header(email.header.decode_header(raw))))
    except Exception:  # noqa: BLE001
        return raw


def addresses(raw) -> list[str]:
    if not raw:
        return []
    return [addr for _name, addr in getaddresses([decode_header(raw)]) if addr]


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _when(value: datetime | None, zone: ZoneInfo | None) -> str | None:
    """The message's date, in the owner's time zone if one is set, else this Mac's."""
    if value is None:
        return None
    try:
        # imapclient gives a naive time already moved to this Mac's local time.
        aware = value.astimezone() if value.tzinfo is None else value
        return (aware.astimezone(zone) if zone else aware).isoformat()
    except (ValueError, OverflowError, OSError):
        return value.isoformat()


def _snippet(raw) -> str:
    text = _text(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:_SNIPPET_LEN]


def _summary(account: Account, uid: int, fetched: dict, zone: ZoneInfo | None) -> MessageSummary:
    env = fetched.get(b"ENVELOPE")
    flags = [_text(f) for f in fetched.get(b"FLAGS") or []]
    snippet = _snippet(fetched.get(b"BODY[TEXT]<0>") or fetched.get(b"BODY[TEXT]") or b"")
    subject, when, sender, to = "", None, "", []
    if env is not None:
        subject = decode_header(env.subject) if env.subject else ""
        when = _when(env.date, zone)
        if env.from_:
            f = env.from_[0]
            box, host = _text(f.mailbox), _text(f.host)
            sender = f"{box}@{host}" if box and host else (box or host)
        for t in env.to or []:
            box, host = _text(t.mailbox), _text(t.host)
            if box and host:
                to.append(f"{box}@{host}")
    return MessageSummary(
        account=account.email, label=account.label, uid=int(uid), from_=sender, to=to,
        subject=subject, date=when, snippet=snippet, unread="\\Seen" not in flags,
    )


SUMMARY_ITEMS = [b"ENVELOPE", b"FLAGS", b"BODY.PEEK[TEXT]<0.512>"]


def _since(value: str | None):
    """Accept 2026-05-01 or the mail world's 01-May-2026."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{value!r} isn't a date. Use 2026-05-01.")


def list_unread(pool: IMAPPool, account: Account, limit: int = 25, since: str | None = None,
                zone: ZoneInfo | None = None) -> list[MessageSummary]:
    criteria: list = ["UNSEEN"]
    day: date | None = _since(since)
    if day:
        criteria += ["SINCE", day]
    with pool.connection(account) as client:
        client.select_folder(INBOX, readonly=True)
        uids = sorted(client.search(criteria))[-limit:][::-1]
        if not uids:
            return []
        fetched = client.fetch(uids, SUMMARY_ITEMS)
        return [_summary(account, uid, fetched[uid], zone) for uid in uids if uid in fetched]


def search(pool: IMAPPool, account: Account, query: str, limit: int = 25, folder: str = "inbox",
           zone: ZoneInfo | None = None) -> list[MessageSummary]:
    """Gmail's own search syntax where the server has it (from:, newer_than:), else a text search."""
    with pool.connection(account) as client:
        client.select_folder(pool.folder(account, client, folder), readonly=True)
        try:
            uids = client.gmail_search(query)
        except Exception:  # noqa: BLE001 - not Gmail
            uids = client.search(["TEXT", query])
        uids = sorted(uids)[-limit:][::-1]
        if not uids:
            return []
        fetched = client.fetch(uids, SUMMARY_ITEMS)
        return [_summary(account, uid, fetched[uid], zone) for uid in uids if uid in fetched]


def fetch_message(pool: IMAPPool, account: Account, uid: int, folder: str = "inbox") -> tuple[Message, dict]:
    """The whole message. Read-only folder and BODY.PEEK[]: nothing about it changes, unread included."""
    with pool.connection(account) as client:
        target = pool.folder(account, client, folder)
        client.select_folder(target, readonly=True)
        fetched = client.fetch([uid], [b"ENVELOPE", b"FLAGS", b"BODY.PEEK[]"])
        if uid not in fetched:
            raise LookupError(f"No message {uid} in {target} for {account.email}.")
        raw = fetched[uid].get(b"BODY[]") or b""
    return email.message_from_bytes(raw), fetched[uid]


def read_message(pool: IMAPPool, account: Account, uid: int, folder: str = "inbox",
                 zone: ZoneInfo | None = None) -> MessageDetail:
    msg, fetched = fetch_message(pool, account, uid, folder)
    body, truncated, attachments = _body_and_attachments(msg)
    headers = {k: decode_header(v) for k, v in msg.items()}
    return MessageDetail(_summary(account, uid, fetched, zone), headers, body, truncated, attachments)


def _body_and_attachments(msg: Message) -> tuple[str, bool, list[dict]]:
    plain: list[str] = []
    html: list[str] = []
    attachments = [a.describe() for a in list_attachments(msg)]
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            (plain if ctype == "text/plain" else html).append(text)
    if plain:
        body = "\n\n".join(plain).strip()
    elif html:
        try:
            import html2text

            h = html2text.HTML2Text()
            h.body_width = 0
            h.ignore_images = True
            body = h.handle("\n\n".join(html)).strip()
        except Exception:  # noqa: BLE001
            body = re.sub(r"<[^>]+>", " ", "\n\n".join(html)).strip()
    else:
        body = ""
    truncated = len(body) > MAX_BODY_CHARS
    return body[:MAX_BODY_CHARS], truncated, attachments


def reply_context(pool: IMAPPool, account: Account, uid: int, folder: str = "inbox") -> dict:
    """The headers a reply draft needs to sit in the same conversation. BODY.PEEK: stays unread."""
    with pool.connection(account) as client:
        target = pool.folder(account, client, folder)
        client.select_folder(target, readonly=True)
        fetched = client.fetch([uid], [b"BODY.PEEK[HEADER]"])
        if uid not in fetched:
            raise LookupError(f"No message {uid} in {target} for {account.email}.")
        head = email.message_from_bytes(fetched[uid].get(b"BODY[HEADER]") or b"")
    return {
        "message_id": _unfold(head.get("Message-ID")),
        "references": _unfold(head.get("References")),
        "subject": decode_header(head.get("Subject")),
        "reply_to": addresses(head.get("Reply-To")) or addresses(head.get("From")),
        "to": addresses(head.get("To")),
        "cc": addresses(head.get("Cc")),
    }


# ─── Drafts ──────────────────────────────────────────────────────────────────


def append_draft(pool: IMAPPool, account: Account, msg_bytes: bytes) -> tuple[int | None, str]:
    """Save a finished message in Drafts. It is not sent. Returns its uid there, if the server says."""
    with pool.connection(account) as client:
        drafts = pool.folder(account, client, "drafts")
        response = client.append(drafts, msg_bytes, flags=[b"\\Draft"])
    if isinstance(response, str):
        response = response.encode("utf-8", "replace")
    found = _APPENDUID.search(response or b"")
    return (int(found.group(1)) if found else None), drafts


def made_by_this_tool(message_id: str) -> bool:
    return bool(_OUR_MESSAGE_ID.match((message_id or "").strip()))


def trash_own_draft(pool: IMAPPool, account: Account, uid: int) -> dict:
    """Move one draft this connector saved from Drafts to Trash. The uid is looked up in
    Drafts only, so nothing in the inbox or Sent can be reached from here, and a draft
    the owner wrote is refused."""
    with pool.connection(account) as client:
        drafts = pool.folder(account, client, "drafts")
        client.select_folder(drafts, readonly=False)
        try:
            fetched = client.fetch([uid], [b"BODY.PEEK[HEADER]"])
            if uid not in fetched:
                raise LookupError(f"No draft {uid} in {drafts} for {account.email}.")
            head = email.message_from_bytes(fetched[uid].get(b"BODY[HEADER]") or b"")
            message_id = (head.get("Message-ID") or "").strip()
            if not made_by_this_tool(message_id):
                raise DraftRefused("That draft wasn't saved by this connector, so it was left where it is.")
            trash = pool.folder(account, client, "trash")
            client.move([uid], trash)
        finally:
            try:
                client.select_folder(INBOX, readonly=True)
            except Exception:  # noqa: BLE001
                pass
    return {"uid": int(uid), "subject": decode_header(head.get("Subject")), "moved_to": trash}


# ─── Checking a login ────────────────────────────────────────────────────────


def check_login(account: Account, password: str) -> AccountStatus:
    """Sign in with a password held only in memory, open the inbox read-only, sign out."""
    try:
        client = open_client(account, password)
        try:
            client.select_folder(INBOX, readonly=True)
        finally:
            _quiet_logout(client)
        return AccountStatus(account.email, account.label, "ok")
    except LoginError as exc:
        return AccountStatus(account.email, account.label, "auth_error", _login_hint(exc))
    except (OSError, socket.error) as exc:
        return AccountStatus(account.email, account.label, "unreachable", str(exc))
    except Exception as exc:  # noqa: BLE001
        return AccountStatus(account.email, account.label, "error", str(exc))


def probe(pool: IMAPPool, account: Account) -> AccountStatus:
    """Is this mailbox reachable with the login stored for it?"""
    from .config import CredentialError

    try:
        with pool.connection(account) as client:
            client.noop()
        return AccountStatus(account.email, account.label, "ok")
    except CredentialError as exc:
        return AccountStatus(account.email, account.label, "no_login", str(exc))
    except LoginError as exc:
        return AccountStatus(account.email, account.label, "auth_error", _login_hint(exc))
    except (OSError, socket.error) as exc:
        return AccountStatus(account.email, account.label, "unreachable", str(exc))
    except Exception as exc:  # noqa: BLE001
        return AccountStatus(account.email, account.label, "error", str(exc))


def _login_hint(exc: Exception) -> str:
    return (
        f"The mail server refused the sign-in ({exc}). For Google, this needs an app password, "
        "not the account's normal password, and IMAP turned on."
    )


# Deliberately absent: sending, replying, forwarding, deleting, expunging, flagging,
# marking read, moving or copying received or sent mail.
