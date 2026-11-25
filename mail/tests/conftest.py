"""Shared test setup. Every name and address in these tests is made up.

No test touches the real Keychain, a real mail server, or this Mac's real settings:
the Keychain is swapped for one in memory, the mail server for a fake, and each test
gets its own empty home folder.
"""

from __future__ import annotations

import email
import email.utils
import re
import smtplib
from datetime import datetime
from pathlib import Path

import keyring
import keyring.backend
import pytest
from imapclient.response_types import Address, Envelope

from wvh_mail_mcp import imap_client as mail
from wvh_mail_mcp import server as srv


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fresh home folder and an empty Keychain for every test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WVH_MAIL_HOME", raising=False)
    monkeypatch.delenv("WVH_MAIL_OWNER", raising=False)
    for name in ("WVH_MAIL_CONFIG", "WVH_MAIL_SENDING", "WVH_MAIL_DRAFTS", "WVH_MAIL_REMOTE_DRAFTS",
                 "WVH_MAIL_REMOTE_ACCOUNTS", "WVH_MAIL_REMOTE_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WVH_MAIL_INSTALL", "robin-mail")
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    srv.state.__init__()  # forget any install, settings and connections from another test
    yield ring
    srv.state.__init__()


@pytest.fixture
def home(isolated) -> Path:
    return Path.home()


def settings_folder(install: str = "robin-mail") -> Path:
    return Path.home() / "Library" / "Application Support" / install


def write_settings(text: str, install: str = "robin-mail") -> Path:
    folder = settings_folder(install)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "accounts.yaml"
    path.write_text(text)
    return path


TWO_MAILBOXES = """\
owner_name: Robin
timezone: America/Chicago
accounts:
  - email: robin@example.com
    label: Studio
  - email: Robin.Personal@example.net
    label: Personal
"""


# ─── A fake mail server ──────────────────────────────────────────────────────


def raw_message(subject="Friday works", sender="dana@example.org", to="robin@example.com",
                body="See you Friday.", message_id="<abc123@example.org>", attachments=(), cc=None,
                references=None) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if references:
        msg["References"] = references
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 24 Nov 2026 09:15:00 -0800"
    msg.set_content(body)
    for name, ctype, data in attachments:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def envelope(raw: bytes) -> Envelope:
    msg = email.message_from_bytes(raw)

    def addr(value):
        name, address = email.utils.parseaddr(value)
        box, host = address.split("@")
        return (Address(name.encode() if name else None, None, box.encode(), host.encode()),)

    return Envelope(
        datetime(2026, 11, 24, 9, 15), msg["Subject"].encode(), addr(msg["From"]), addr(msg["From"]),
        addr(msg["From"]), addr(msg["To"]), None, None, None, msg["Message-ID"].encode(),
    )


class FakeImap:
    """Records every call. Folders hold {uid: (raw bytes, flags)}."""

    def __init__(self) -> None:
        self.folders: dict[str, dict[int, tuple[bytes, tuple]]] = {
            "INBOX": {}, "[Gmail]/Sent Mail": {}, "[Gmail]/Drafts": {}, "[Gmail]/Trash": {}, "[Gmail]/All Mail": {},
        }
        self.selected: str | None = None
        self.calls: list[tuple] = []
        self.next_uid = 100

    # reading
    def select_folder(self, name, readonly=False):
        self.calls.append(("select_folder", name, readonly))
        self.selected = name

    def search(self, criteria):
        self.calls.append(("search", tuple(map(str, criteria))))
        box = self.folders[self.selected]
        if criteria and criteria[0] == "UNSEEN":
            return [uid for uid, (_raw, flags) in box.items() if b"\\Seen" not in flags]
        return list(box)

    def gmail_search(self, query):
        self.calls.append(("gmail_search", query))
        # Plain words match anywhere; from:/to:/cc: terms joined by OR match any one of them.
        terms = [t.split(":", 1)[-1].strip().strip('"') for t in re.split(r"\s+OR\s+", query)] if " OR " in query else [query]
        box = self.folders[self.selected]
        return [uid for uid, (raw, _f) in box.items() if any(t.lower() in raw.decode().lower() for t in terms if t)]

    def fetch(self, uids, items):
        self.calls.append(("fetch", tuple(uids), tuple(items)))
        out = {}
        for uid in uids:
            if uid not in self.folders[self.selected]:
                continue
            raw, flags = self.folders[self.selected][uid]
            row = {b"FLAGS": flags}
            for item in items:
                if item == b"ENVELOPE":
                    row[b"ENVELOPE"] = envelope(raw)
                elif item.startswith(b"BODY.PEEK[TEXT]"):
                    row[b"BODY[TEXT]<0>"] = email.message_from_bytes(raw).get_payload(decode=False).encode()[:512] if not email.message_from_bytes(raw).is_multipart() else b""
                elif item == b"BODY.PEEK[]":
                    row[b"BODY[]"] = raw
                elif item == b"BODY.PEEK[HEADER]":
                    row[b"BODY[HEADER]"] = raw.split(b"\n\n", 1)[0] + b"\n\n"
            out[uid] = row
        return out

    def find_special_folder(self, flag):
        return {b"\\Sent": "[Gmail]/Sent Mail", b"\\Drafts": "[Gmail]/Drafts", b"\\Trash": "[Gmail]/Trash", b"\\All": "[Gmail]/All Mail"}.get(flag)

    # changes the owner can ask for: flags and mark read
    def add_flags(self, uids, flags):
        self.calls.append(("add_flags", tuple(uids), tuple(flags)))
        for uid in uids:
            raw, old = self.folders[self.selected][uid]
            self.folders[self.selected][uid] = (raw, tuple(old) + tuple(f for f in flags if f not in old))

    def add_gmail_labels(self, uids, labels):
        self.calls.append(("add_gmail_labels", tuple(uids), tuple(labels)))

    # saving a draft, filing a sent copy, moving the connector's own draft to Trash
    def append(self, folder, msg, flags=()):
        self.calls.append(("append", folder, tuple(flags)))
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder][uid] = (msg if isinstance(msg, bytes) else msg.encode(), tuple(flags))
        return f"[APPENDUID 7 {uid}] (Success)".encode()

    def move(self, uids, folder):
        self.calls.append(("move", tuple(uids), folder))
        for uid in uids:
            self.folders[folder][uid] = self.folders[self.selected].pop(uid)

    def noop(self):
        self.calls.append(("noop",))

    def logout(self):
        self.calls.append(("logout",))

    def __getattr__(self, name):
        # Anything else (add_flags, delete_messages, expunge...) is a call the connector must never make.
        raise AssertionError(f"the connector called IMAP {name}, which it must never do")


class FakePool(mail.IMAPPool):
    def __init__(self, client: FakeImap) -> None:
        super().__init__("robin-mail")
        self.client = client

    def _acquire(self, account):
        pooled = self._connections.get(account.email)
        if pooled is None:
            pooled = mail._Pooled(client=self.client)
            self._connections[account.email] = pooled
        return pooled


@pytest.fixture
def fake_imap() -> FakeImap:
    client = FakeImap()
    srv.state._pool = FakePool(client)
    return client


# ─── A fake mail server for sending ─────────────────────────────────────────


class FakeSmtp:
    """Stands in for smtplib.SMTP_SSL and smtplib.SMTP. Records every message handed over."""

    sent: list = []
    logins: list = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSmtp.logins.append(("starttls", self.host))

    def login(self, user, password):
        FakeSmtp.logins.append((user, self.host, self.port))

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSmtp.sent, FakeSmtp.logins = [], []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch):
    """No test can reach a real mail server, whatever it does."""
    def refuse(*a, **k):
        raise AssertionError("a test tried to reach a real mail server")
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    monkeypatch.setattr(smtplib, "SMTP", refuse)


@pytest.fixture(autouse=True)
def no_contacts(monkeypatch):
    """Contacts on this Mac are never read in tests; a test can supply its own."""
    from wvh_mail_mcp import contacts
    monkeypatch.setattr(contacts, "_rows", lambda: [])


@pytest.fixture(autouse=True)
def nicknames_file(tmp_path, monkeypatch):
    path = tmp_path / "nicknames.yaml"
    monkeypatch.setenv("WVH_NICKNAMES", str(path))
    return path
