"""Read threads and attachments out of the Messages database. SELECT only.

How a thread is found:
  * a direct thread, only when every number or address in it belongs to one person;
  * a group thread, by the group's own id in Messages. Someone being in a group never
    opens the group: what they said there is shown only when asked, and only their words.

Nothing here writes. Sending goes through the Messages app (sending.py); after a send,
this module only looks up, read-only, whether Messages delivered it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import messages_db, normalize_handle
from .db import open_readonly
from .decode import message_text

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_NANOSECONDS = 1_000_000_000
STYLE_DIRECT = 45
STYLE_GROUP = 43
_CHUNK = 500  # SQLite's parameter limit, comfortably

_PICTURE_TYPES = ("heic", "heif", "jpg", "jpeg", "png", "gif", "tif", "tiff", "webp", "bmp")


def from_apple_time(raw: int | None) -> datetime | None:
    if not raw:
        return None
    seconds = raw / _NANOSECONDS if raw > 100_000_000_000 else raw  # very old rows hold seconds
    return _APPLE_EPOCH + timedelta(seconds=seconds)


def to_apple_time(moment: datetime) -> int:
    return int((moment - _APPLE_EPOCH).total_seconds() * _NANOSECONDS)


def hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


@dataclass
class Chat:
    id: int
    style: int
    identifier: str
    display_name: str
    handles: set[str] = field(default_factory=set)  # normalized
    guid: str = ""  # what the Messages app calls this chat, for sending

    @property
    def service(self) -> str:
        return self.guid.split(";", 1)[0] if ";" in self.guid else ""


@dataclass
class Delivery:
    found: bool
    error: int = 0
    delivered: bool = False
    sent: bool = False

    def plain(self) -> str:
        if not self.found:
            return "Messages hasn't recorded it yet. Check the thread in a moment."
        if self.error:
            return f"Messages says it failed (error {self.error}). It may not have gone out."
        if self.delivered:
            return "Delivered."
        if self.sent:
            return "Sent. Not marked delivered yet (normal for a text message, or a phone that's off)."
        return "Handed to Messages. Not confirmed sent yet."


@dataclass
class Attachment:
    id: int
    message_id: int
    name: str
    kind: str  # picture, video, audio, or file
    filename: str | None


@dataclass
class Message:
    id: int
    chat_id: int
    when: datetime | None
    from_me: bool
    sender: str | None  # normalized handle of who sent it, for group threads
    text: str
    unsent: bool
    edited: bool
    has_attachments: bool


def _kind(mime: str | None, uti: str | None, name: str) -> str:
    mime = (mime or "").lower()
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mime.startswith("image/") or extension in _PICTURE_TYPES or (uti or "").startswith("public.heic"):
        return "picture"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _marks(values: Iterable) -> str:
    return ",".join("?" for _ in values)


class Store:
    def __init__(self, path: Path | None = None):
        self.conn = open_readonly(path or messages_db())
        self._handles: dict[int, str] = {}
        for row in self.conn.execute("SELECT ROWID, id FROM handle"):
            if row["id"]:
                self._handles[row["ROWID"]] = normalize_handle(row["id"])
        chat_columns = self._columns("chat")
        self._message_columns = self._columns("message")
        guid = "guid" if "guid" in chat_columns else "''"
        self.chats: dict[int, Chat] = {}
        for row in self.conn.execute(f"SELECT ROWID, style, chat_identifier, display_name, {guid} AS guid FROM chat"):
            self.chats[row["ROWID"]] = Chat(
                id=row["ROWID"],
                style=row["style"] or 0,
                identifier=row["chat_identifier"] or "",
                display_name=(row["display_name"] or "").strip(),
                guid=row["guid"] or "",
            )
        for row in self.conn.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
            chat, handle = self.chats.get(row["chat_id"]), self._handles.get(row["handle_id"])
            if chat and handle:
                chat.handles.add(handle)

    def close(self) -> None:
        self.conn.close()

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def handle_rows(self, handles: set[str]) -> list[int]:
        """The database's own row ids for these numbers and addresses."""
        return sorted(rowid for rowid, handle in self._handles.items() if handle in handles)

    # ── Which threads ────────────────────────────────────────────────────────

    def direct_chats(self, allowed: set[str]) -> list[int]:
        """Direct threads made only of these numbers and addresses."""
        if not allowed:
            return []
        return sorted(
            chat.id
            for chat in self.chats.values()
            if chat.style == STYLE_DIRECT and chat.handles and chat.handles <= allowed
        )

    def chat_for_sending(self, handles: set[str]) -> Chat | None:
        """The direct thread to send to: the most recently used one made only of these handles."""
        candidates = [c for c in self.chats.values() if c.style == STYLE_DIRECT and c.handles and c.handles <= handles]
        if not candidates:
            return None
        return max(candidates, key=lambda c: (self.last_message(c.id) or _APPLE_EPOCH, c.id))

    def groups_containing(self, handles: set[str]) -> list[int]:
        """Group threads any of these numbers or addresses is in. Used only to show that person's own words."""
        return sorted(c.id for c in self.chats.values() if c.style == STYLE_GROUP and c.handles & handles)

    def group_chats(self, identifiers: set[str]) -> list[int]:
        """Group threads by their own id, never by who is in them."""
        if not identifiers:
            return []
        return sorted(
            chat.id for chat in self.chats.values() if chat.style == STYLE_GROUP and chat.identifier in identifiers
        )

    def groups_named(self, name: str) -> list[Chat]:
        """For setup only: group threads whose name matches."""
        low = name.strip().lower()
        exact = [c for c in self.chats.values() if c.style == STYLE_GROUP and c.display_name.lower() == low]
        return exact or [c for c in self.chats.values() if c.style == STYLE_GROUP and low and low in c.display_name.lower()]

    def groups_with(self, handles: set[str]) -> list[Chat]:
        """For setup only: group threads that include any of these numbers or addresses."""
        return [c for c in self.chats.values() if c.style == STYLE_GROUP and c.handles & handles]

    def group_by_identifier(self, identifier: str) -> Chat | None:
        for chat in self.chats.values():
            if chat.style == STYLE_GROUP and chat.identifier == identifier:
                return chat
        return None

    # ── Messages ─────────────────────────────────────────────────────────────

    @staticmethod
    def _where(chat_ids: list[int], since: datetime | None, inbound_only: bool, sender_rows: list[int] | None = None):
        # item_type 0 skips "named the group" rows. associated_message_type 0 skips tapbacks.
        clauses = [
            f"cmj.chat_id IN ({_marks(chat_ids)})",
            "m.item_type = 0",
            "COALESCE(m.associated_message_type, 0) = 0",
        ]
        params: list = list(chat_ids)
        if since is not None:
            clauses.append("m.date >= ?")
            params.append(to_apple_time(since))
        if inbound_only:
            clauses.append("m.is_from_me = 0")
        if sender_rows is not None:
            # Only these people's own words: in a group, nobody else's messages are shown.
            clauses.append(f"m.is_from_me = 0 AND m.handle_id IN ({_marks(sender_rows) or 'NULL'})")
            params.extend(sender_rows)
        return " AND ".join(clauses), params

    def fetch(
        self,
        chat_ids: list[int],
        limit: int,
        since: datetime | None = None,
        before_id: int | None = None,
        inbound_only: bool = False,
        sender_rows: list[int] | None = None,
    ) -> list[Message]:
        """Newest first. `sender_rows` keeps only those people's words, for group threads."""
        if not chat_ids:
            return []
        if sender_rows is not None and not sender_rows:
            return []
        where, params = self._where(chat_ids, since, inbound_only, sender_rows)
        if before_id is not None:
            where += " AND m.date < (SELECT date FROM message WHERE ROWID = ?)"
            params.append(before_id)
        rows = self.conn.execute(
            "SELECT m.ROWID AS id, cmj.chat_id, m.text, m.attributedBody, m.date, m.is_from_me, m.handle_id, "
            "m.cache_has_attachments, m.date_retracted, m.date_edited "
            "FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id "
            f"WHERE {where} ORDER BY m.date DESC LIMIT ?",
            [*params, limit],
        )
        messages: dict[int, Message] = {}
        for row in rows:
            unsent = bool(row["date_retracted"])
            messages.setdefault(
                row["id"],
                Message(
                    id=row["id"],
                    chat_id=row["chat_id"],
                    when=from_apple_time(row["date"]),
                    from_me=bool(row["is_from_me"]),
                    sender=self._handles.get(row["handle_id"]),
                    text="" if unsent else message_text(row["text"], row["attributedBody"]),
                    unsent=unsent,
                    edited=bool(row["date_edited"]),
                    has_attachments=bool(row["cache_has_attachments"]),
                ),
            )
        return list(messages.values())

    def summary(
        self, chat_ids: list[int], since: datetime | None = None, inbound_only: bool = False
    ) -> tuple[int, datetime | None]:
        """How many messages, and when the newest one came."""
        if not chat_ids:
            return 0, None
        where, params = self._where(chat_ids, since, inbound_only)
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT m.ROWID), MAX(m.date) "
            f"FROM chat_message_join cmj JOIN message m ON m.ROWID = cmj.message_id WHERE {where}",
            params,
        ).fetchone()
        return row[0], from_apple_time(row[1])

    def last_message(self, chat_id: int) -> datetime | None:
        return self.summary([chat_id])[1]

    def delivery(self, chat_ids: list[int], handles: set[str], since: datetime) -> Delivery:
        """After a send: the newest message from the owner in that thread since `since`. Read-only."""
        cols = self._message_columns
        error = "m.error" if "error" in cols else "0"
        delivered = "m.is_delivered" if "is_delivered" in cols else "0"
        sent = "m.is_sent" if "is_sent" in cols else "0"
        rows = self.handle_rows(handles)
        clauses, params = ["m.is_from_me = 1", "m.date >= ?"], [to_apple_time(since)]
        where = []
        if chat_ids:
            where.append(f"m.ROWID IN (SELECT message_id FROM chat_message_join WHERE chat_id IN ({_marks(chat_ids)}))")
            params.extend(chat_ids)
        if rows:
            where.append(f"m.handle_id IN ({_marks(rows)})")
            params.extend(rows)
        if not where:
            return Delivery(found=False)
        clauses.append("(" + " OR ".join(where) + ")")
        row = self.conn.execute(
            f"SELECT {error} AS error, {delivered} AS delivered, {sent} AS sent FROM message m "
            f"WHERE {' AND '.join(clauses)} ORDER BY m.date DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return Delivery(found=False)
        return Delivery(found=True, error=row["error"] or 0, delivered=bool(row["delivered"]), sent=bool(row["sent"]))

    # ── Attachments ──────────────────────────────────────────────────────────

    @staticmethod
    def _attachment(row) -> Attachment | None:
        name = row["transfer_name"] or (row["filename"] or "").rsplit("/", 1)[-1]
        if row["hide_attachment"] or row["is_sticker"] or name.endswith(".pluginPayloadAttachment"):
            return None  # link previews and stickers, not things anyone sent on purpose
        return Attachment(
            id=row["id"],
            message_id=row["message_id"],
            name=name,
            kind=_kind(row["mime_type"], row["uti"], name),
            filename=row["filename"],
        )

    def attachments_for(self, message_ids: list[int]) -> dict[int, list[Attachment]]:
        found: dict[int, list[Attachment]] = defaultdict(list)
        for start in range(0, len(message_ids), _CHUNK):
            chunk = message_ids[start : start + _CHUNK]
            rows = self.conn.execute(
                "SELECT j.message_id, a.ROWID AS id, a.filename, a.mime_type, a.uti, a.transfer_name, "
                "a.is_sticker, a.hide_attachment "
                "FROM message_attachment_join j JOIN attachment a ON a.ROWID = j.attachment_id "
                f"WHERE j.message_id IN ({_marks(chunk)}) ORDER BY a.ROWID",
                chunk,
            )
            for row in rows:
                attachment = self._attachment(row)
                if attachment:
                    found[attachment.message_id].append(attachment)
        return found

    def attachment_in(
        self, attachment_id: int, chat_ids: list[int], sender_rows: list[int] | None = None
    ) -> tuple[Attachment, int] | None:
        """The attachment and its thread, but only if it sits in one of these threads
        (and, with sender_rows, only if one of those people sent it)."""
        if not chat_ids:
            return None
        extra, params = "", [attachment_id, *chat_ids]
        if sender_rows is not None:
            if not sender_rows:
                return None
            extra = f" AND m.is_from_me = 0 AND m.handle_id IN ({_marks(sender_rows)})"
            params.extend(sender_rows)
        row = self.conn.execute(
            "SELECT j.message_id, cmj.chat_id, a.ROWID AS id, a.filename, a.mime_type, a.uti, a.transfer_name, "
            "a.is_sticker, a.hide_attachment "
            "FROM attachment a "
            "JOIN message_attachment_join j ON j.attachment_id = a.ROWID "
            "JOIN chat_message_join cmj ON cmj.message_id = j.message_id "
            "JOIN message m ON m.ROWID = j.message_id "
            f"WHERE a.ROWID = ? AND cmj.chat_id IN ({_marks(chat_ids)}){extra} LIMIT 1",
            params,
        ).fetchone()
        if not row:
            return None
        attachment = self._attachment(row)
        return (attachment, row["chat_id"]) if attachment else None
