"""What must be true before mail leaves: the owner's words, a preview, and their yes.

Every send works the same way, for the owner, their team and their clients:

  1. send_message or reply_message PREVIEWS. Nothing is sent. It carries the owner's own
     words from the conversation (`instruction`), and the message is built exactly as it
     would go out: from, to, cc, bcc, subject, body, signature, attachments.
  2. The checks below run on that preview. A block stops it with a plain reason. A flag
     lets it continue only after Claude reads it back and the owner says yes.
  3. confirm_send sends that exact message, once, within ten minutes.

The instruction has to name who it's for: a name or address the mailbox or Contacts ties
to a recipient, a nickname the owner confirmed before, the name the owner used
(`said_as`), "me" for the owner's own mailbox, or a reply in a conversation already under
way. Everything here is a pure function of what the server already knows, so a prompt
or an email can't argue with it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

TICKET_SECONDS = 600              # a confirmation doesn't survive a gap
CONTINUE_SECONDS = 12 * 60 * 60   # how long a conversation counts as "already going"

_FILLER = {
    "the", "and", "for", "with", "from", "that", "this", "email", "mail", "send", "reply", "message",
    "inc", "llc", "team", "info", "admin", "hello", "contact", "support", "noreply", "office",
}
_SECRETS = (
    (r"xox[abeoprs]-[0-9A-Za-z-]{8,}", "a Slack token"),
    (r"sk-ant-[0-9A-Za-z_-]{8,}", "an API key"),
    (r"\bAKIA[0-9A-Z]{12,}\b", "an API key"),
    (r"\bgh[pousr]_[0-9A-Za-z]{20,}\b", "an access token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"\b(password|passwd|passcode|app password)\s*[:=]\s*\S+", "a password"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "a Social Security number"),
)
_SENSITIVE = (
    "payroll", "salary", "salaries", "wages", "social security", "bank account",
    "account number", "routing number", "w-2", "w-9", "1099", "tax id", "ein",
)
_SELF = re.compile(r"(?<![a-z0-9'])(?:me|myself)(?![a-z0-9'])")


class GuardError(Exception):
    """Blocked before anything left. The detail says why, plainly."""


class TicketError(Exception):
    """The confirmation is missing, used, or too old."""


# ─── Words ───────────────────────────────────────────────────────────────────


def normalize(text: str) -> str:
    text = (text or "").replace("&", " and ").replace("’", "'").lower()
    return re.sub(r"\s+", " ", text).strip()


def mentions(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalize(text)) is not None


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", normalize(text)) if w not in _FILLER}


def names_self(instruction: str) -> bool:
    return _SELF.search(normalize(instruction)) is not None


# ─── Who a message goes to ───────────────────────────────────────────────────


@dataclass
class Who:
    address: str
    names: set[str] = field(default_factory=set)       # what the mailbox or Contacts calls them
    nicknames: set[str] = field(default_factory=set)   # nicknames the owner confirmed for those names

    @property
    def full_name(self) -> str | None:
        """The best name on record: first and last if any name has both, else the longest."""
        if not self.names:
            return None
        spaced = [n for n in self.names if " " in n.strip()]
        return max(spaced or list(self.names), key=len)

    def label(self) -> str:
        return f"{self.full_name} <{self.address}>" if self.full_name else self.address


@dataclass
class Finding:
    blocks: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    remember: tuple[str, str] | None = None   # (nickname, full name) to save after the owner's yes
    continuing: bool = False

    @property
    def read_back(self) -> bool:
        return bool(self.flags)


def review(
    *,
    owner: str,
    instruction: str,
    said_as: str | None,
    people: list[Who],
    own_addresses: set[str],
    inside_domains: set[str],
    texts: list[str],
    continuation: str | None,
    attachments: list[str],
    drafts_only_identity: str | None,
    known_nickname: str | None,
) -> Finding:
    found = Finding()
    said = (said_as or "").strip()
    body = "\n".join(t for t in texts if t)

    if not instruction.strip():
        found.blocks.append(f"No instruction from {owner}. Mail goes out only on {owner}'s own word in this conversation.")

    if drafts_only_identity:
        found.blocks.append(
            f"{drafts_only_identity} drafts but never sends. Save it as a draft instead (append_draft), "
            f"and {owner} sends it."
        )

    if said and not mentions(instruction, said):
        found.blocks.append(f"said_as has to be the name {owner} used, as they said it. {said!r} isn't in their words.")

    def named(who: Who) -> bool:
        if who.address in normalize(instruction):
            return True
        if words(" ".join(who.names)) & words(instruction):
            return True
        if any(mentions(instruction, nick) for nick in who.nicknames):
            return True
        return False

    by_name = [p for p in people if named(p)]
    to_self = bool(people) and all(p.address in own_addresses for p in people) and names_self(instruction)
    by_said = bool(said) and mentions(instruction, said)

    if instruction.strip() and not (by_name or to_self or by_said or continuation):
        listed = ", ".join(p.label() for p in people) or "nobody"
        found.blocks.append(
            f"{owner[:1].upper() + owner[1:]}'s words don't name anyone this goes to ({listed}), and it isn't a reply "
            "in a conversation already under way. Nothing says this is who was meant."
        )

    if said and not by_name and by_said:
        target = next((p for p in people if p.full_name), people[0] if people else None)
        if target is not None:
            if known_nickname and target.full_name and known_nickname.lower() == target.full_name.lower():
                pass  # a nickname confirmed before, for this very person
            elif known_nickname:
                found.flags.append(
                    f"{owner[:1].upper() + owner[1:]} said {said}, which is saved as {known_nickname}, "
                    f"but this goes to {target.label()}. Confirm that's who was meant."
                )
            else:
                found.flags.append(f"{owner[:1].upper() + owner[1:]} said {said}; this goes to {target.label()}. Confirm that's them.")
                if target.full_name and len(people) >= 1:
                    found.remember = (said, target.full_name)

    if continuation and not (by_name or to_self or by_said):
        found.continuing = True
        found.flags.append(f"No one was named, so this continues {continuation}. Confirm it's the right conversation.")

    for p in people:
        if not p.names and p.address not in normalize(instruction) and p.address not in own_addresses:
            found.flags.append(f"Nothing in this mailbox or Contacts shows who {p.address} is.")

    outside = sorted({p.address for p in people if p.address.rsplit("@", 1)[-1] not in inside_domains})
    if outside:
        found.flags.append("Goes outside: " + ", ".join(outside) + ".")

    if attachments:
        found.flags.append("Attaches: " + ", ".join(attachments) + ". Confirm they're the right files.")

    for pattern, what in _SECRETS:
        if re.search(pattern, body, re.I):
            found.blocks.append(f"This appears to contain {what}. Secrets never go out by email from here.")

    hit = next((word for word in _SENSITIVE if mentions(body, word)), None)
    if hit and len(people) > 2:
        found.flags.append(f"Sensitive subject ({hit}) going to {len(people)} people.")

    return found


# ─── Tickets ─────────────────────────────────────────────────────────────────


@dataclass
class Pending:
    tool: str
    account: str
    msg: EmailMessage
    instruction: str
    read_back: bool
    created: float
    sha256: str
    summary: dict[str, Any]
    attachments: list[dict] = field(default_factory=list)
    remember: tuple[str, str] | None = None
    thread_ids: list[str] = field(default_factory=list)
    identity: str | None = None


def fingerprint(msg: EmailMessage) -> str:
    return hashlib.sha256(msg.as_bytes()).hexdigest()


class Tickets:
    """Single-use, short-lived, and bound to the exact message that was previewed."""

    def __init__(self, clock=time.monotonic) -> None:
        self._open: dict[str, Pending] = {}
        self._clock = clock

    def issue(self, pending: Pending) -> str:
        ticket = secrets.token_urlsafe(9)
        self._open[ticket] = pending
        return ticket

    def peek(self, ticket: str) -> Pending:
        pending = self._open.get(ticket)
        if pending is None:
            raise TicketError("That confirmation is unknown or already used. Preview the message again.")
        if self._clock() - pending.created > TICKET_SECONDS:
            del self._open[ticket]
            raise TicketError("That confirmation expired (ten minutes). Preview it again and ask again.")
        if fingerprint(pending.msg) != pending.sha256:
            del self._open[ticket]
            raise TicketError("The message changed after it was previewed. Preview it again.")
        return pending

    def spend(self, ticket: str) -> None:
        self._open.pop(ticket, None)


# ─── Conversations already under way ─────────────────────────────────────────


@dataclass
class _Seen:
    when: float
    account: str
    people: set[str]
    thread_ids: set[str]
    what: str
    uid: tuple[str, int] | None = None   # (folder, uid) for a message the owner had read


class Memory:
    """What this server sent, and what the owner had Claude open, in the last twelve hours.
    Sends survive a restart through sent.jsonl; reads last as long as this server does."""

    def __init__(self, log_path: Path | None = None, clock=time.time) -> None:
        self._clock = clock
        self._items: list[_Seen] = []
        self._log_path = log_path
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._log_path is None or not self._log_path.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=CONTINUE_SECONDS)
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()[-300:]
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
                when = datetime.fromisoformat(row["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if row.get("event") != "sent" or when < cutoff:
                continue
            people = {a.lower() for a in row.get("recipients") or []}
            ids = {i for i in row.get("thread_ids") or [] if i}
            self._items.append(_Seen(when.timestamp(), row.get("account", ""), people, ids, "the email to " + ", ".join(sorted(people))))

    def _fresh(self) -> list[_Seen]:
        self._load()
        now = self._clock()
        self._items = [s for s in self._items if now - s.when <= CONTINUE_SECONDS]
        return self._items

    def saw_read(self, account: str, folder: str, uid: int, sender: str, people: set[str], thread_ids: set[str], subject: str) -> None:
        self._items.append(_Seen(self._clock(), account, {p.lower() for p in people}, set(thread_ids),
                                 f"the email from {sender} ({subject!r})", (folder.lower(), int(uid))))

    def saw_send(self, account: str, people: set[str], thread_ids: set[str]) -> None:
        self._items.append(_Seen(self._clock(), account, {p.lower() for p in people}, set(thread_ids),
                                 "the email to " + ", ".join(sorted(people))))

    def continuation(self, account: str, people: set[str], reply_to: tuple[str, int] | None, thread_ids: set[str]) -> str | None:
        """A plain description of the conversation this continues, or None."""
        people = {p.lower() for p in people}
        recent = [s for s in self._fresh() if s.account == account]
        for seen in reversed(recent):
            if reply_to is not None and seen.uid == (reply_to[0].lower(), int(reply_to[1])):
                return seen.what
            if thread_ids and seen.thread_ids & thread_ids:
                return seen.what
        sends = [s for s in recent if s.uid is None]
        if sends and people and people <= sends[-1].people:
            return sends[-1].what
        return None
