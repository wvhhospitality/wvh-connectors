"""Sending a text: preview first, then the owner's yes, then Messages sends it.

The same habit as the Slack and mail connectors:

  1. send_text only PREVIEWS. Nothing goes out. It needs the owner's own words from
     the conversation (the instruction) and checks them against who it would reach.
  2. Claude reads the preview back: who, which number or group, the words, any files.
  3. confirm_send is the only thing that sends, and only for the exact text previewed.
     A confirmation works once and expires in 10 minutes.

The gate lets a person be named the way people really talk: their name, a nickname
(from Contacts, from this install, or the list shared by every WvH connector), "me"
for the owner, or nothing at all when the owner is clearly carrying on a conversation
Claude just read or just sent to. The looser ways always need the owner's yes read back.

Sending goes through the Messages app with AppleScript handed over on standard input,
never on a command line. A file is copied first into ~/Pictures/<install>/outgoing/,
because Messages can't send most files from anywhere else.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import nicknames as shared_nicknames
from .config import Config, home_dir, mask, outgoing_dir, python_app, sent_log_path

TICKET_SECONDS = 600
CONTINUE_HOURS = 12
MAX_FILES = 10
MAX_FILE_BYTES = 100 * 1024 * 1024
DELIVERY_WAIT = 8.0  # seconds to watch for Messages to record the send
OSASCRIPT = "/usr/bin/osascript"
_spawn = subprocess.run  # the one place a script is run; tests put a stand-in here

_SECRETS = (
    (r"xox[abeoprs]-[0-9A-Za-z-]{8,}", "a Slack token"),
    (r"sk-ant-[0-9A-Za-z_-]{8,}", "an API key"),
    (r"\bAKIA[0-9A-Z]{12,}\b", "an API key"),
    (r"\bgh[pousr]_[0-9A-Za-z]{20,}\b", "an access token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"\b(password|passwd|passcode)\s*[:=]\s*\S+", "a password"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "a Social Security number"),
)
# Words that never identify anyone on their own.
_FILLER = {
    "the", "and", "for", "you", "your", "with", "text", "send", "tell", "him", "her", "them", "say", "that",
    "this", "please", "can", "could", "message", "reply", "back", "yes", "now", "let", "know", "ask", "she",
    "his", "hers", "their", "they", "just", "also", "too", "group", "chat", "thread",
}


class SendError(Exception):
    """A plain-language refusal or failure. Safe to show."""


# ─── Words ──────────────────────────────────────────────────────────────────


def normalize(text: str) -> str:
    text = (text or "").replace("&", " and ").replace("’", "'").lower()
    return re.sub(r"\s+", " ", text).strip()


def mentions(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalize(text)) is not None


_SELF = re.compile(r"(?<![a-z0-9'])(?:me|myself)(?![a-z0-9'])")


def names_self(instruction: str) -> bool:
    return _SELF.search(normalize(instruction)) is not None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", normalize(text)) if w not in _FILLER}


# ─── What a send would reach ────────────────────────────────────────────────


@dataclass
class Target:
    kind: str  # person | group | me
    name: str
    key: str  # what "the same conversation" means: person:<name>, group:<chat id>, or me
    handles: set[str] = field(default_factory=set)
    chat_guid: str = ""  # the Messages chat to send to
    chat_ids: list[int] = field(default_factory=list)  # database ids, for the delivery check
    service: str = ""
    new_thread_handle: str = ""  # no thread yet: start one with this number or address
    nicknames: set[str] = field(default_factory=set)
    in_contacts: bool = True
    audience: int = 1

    @property
    def reached_at(self) -> str:
        if self.kind == "group":
            return f"group chat ({self.audience} others)"
        handle = self.new_thread_handle or next(iter(sorted(self.handles)), "")
        return mask(handle) if handle else ""


@dataclass
class Finding:
    blocks: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    new_nickname: str | None = None

    @property
    def read_back(self) -> bool:
        return bool(self.flags)


@dataclass
class Pending:
    target: Target
    text: str
    files: list[Path]
    instruction: str
    owner: str
    read_back: bool
    new_nickname: str | None
    created: float
    digest: str


class Tickets:
    """Single-use, short-lived, and bound to the exact text and files that were previewed."""

    def __init__(self) -> None:
        self._open: dict[str, Pending] = {}

    def issue(self, pending: Pending) -> str:
        ticket = secrets.token_urlsafe(9)
        self._open[ticket] = pending
        return ticket

    def peek(self, ticket: str) -> Pending:
        pending = self._open.get(str(ticket or ""))
        if pending is None:
            raise SendError("That confirmation is unknown or already used. Preview the text again.")
        if time.monotonic() - pending.created > TICKET_SECONDS:
            del self._open[ticket]
            raise SendError("That confirmation expired. Preview the text again, and read it back again.")
        return pending

    def spend(self, ticket: str) -> None:
        self._open.pop(ticket, None)

    def clear(self) -> None:
        self._open.clear()


TICKETS = Tickets()


# ─── The conversation already going ─────────────────────────────────────────


@dataclass
class Recent:
    key: str
    name: str
    at: datetime


_LAST_READ: dict[str, Recent] = {}


def note_read(key: str, name: str) -> None:
    """read_texts calls this: the thread the owner just had Claude read, in this server process."""
    _LAST_READ.clear()
    _LAST_READ["last"] = Recent(key, name, datetime.now(timezone.utc))


def forget_reads() -> None:
    _LAST_READ.clear()


def last_sent(install: str) -> Recent | None:
    """The most recent text this install sent, from its own log."""
    path = sent_log_path(install)
    if not path.exists():
        return None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines[-500:]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "sent" and row.get("key"):
            try:
                at = datetime.fromisoformat(row["at"])
            except (KeyError, ValueError):
                return None
            return Recent(row["key"], row.get("to") or "", at)
    return None


def continuing(install: str, target: Target) -> Recent | None:
    """The conversation this send carries on, if it's the one just read or just sent to (12 hours)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CONTINUE_HOURS)
    for recent in (_LAST_READ.get("last"), last_sent(install)):
        if recent and recent.key == target.key and recent.at >= cutoff:
            return recent
    return None


# ─── The gate ───────────────────────────────────────────────────────────────


def review(
    *,
    config: Config,
    instruction: str,
    target: Target,
    text: str,
    files: list[Path],
    said_as: str | None,
    nickname_means: Callable[[str], str | None],
    also_could_be: list[str],
) -> Finding:
    found = Finding()
    owner = config.owner_name
    if not instruction.strip():
        found.blocks.append(f"No instruction from {owner}. Texts go out only on {owner}'s own words in this conversation.")
    if not text.strip() and not files:
        found.blocks.append("There's nothing to send: no words and no file.")
    for pattern, what in _SECRETS:
        if re.search(pattern, text, re.I):
            found.blocks.append(f"This appears to contain {what}. Secrets never go out by text.")

    said = (said_as or "").strip()
    said_is_new = False
    if said:
        if not mentions(instruction, said):
            found.blocks.append(f"{said!r} isn't in {owner}'s words. A nickname has to be what {owner} actually said.")
        else:
            means = nickname_means(said)
            if means and normalize(means) != normalize(target.name):
                found.blocks.append(f"{said!r} already means {means}, not {target.name}. Ask {owner} who they mean.")
            elif not means and normalize(said) != normalize(target.name) and normalize(said) not in target.nicknames:
                said_is_new = True

    named = False
    if target.kind == "me":
        named = names_self(instruction)
    elif target.kind == "group":
        named = mentions(instruction, target.name) or bool(_words(instruction) & _words(target.name))
    else:
        phrases = [target.name, *target.nicknames]
        named = any(mentions(instruction, p) for p in phrases) or bool(_words(instruction) & _words(target.name))
    if said and not said_is_new and mentions(instruction, said):
        named = True
    if said_is_new:
        named = True

    carry_on = None
    if not named and instruction.strip():
        carry_on = continuing(config.install, target)
        if carry_on is None:
            what = "anyone in that group" if target.kind == "group" else target.name
            found.blocks.append(
                f"{owner[:1].upper() + owner[1:]}'s words don't name {what}, and this isn't the conversation "
                "just read or just texted. Nothing here says this is who was meant."
            )

    # The looser ways in: each one needs the owner's yes, read back.
    if said_is_new:
        found.new_nickname = said
        found.flags.append(
            f"{owner[:1].upper() + owner[1:]} said {said!r}. This goes to {target.name} ({target.reached_at}). "
            f"Saying yes also remembers {said} = {target.name} for every connector on this Mac."
        )
    if carry_on is not None:
        found.flags.append(f"Their words don't name anyone, so this carries on the conversation with {carry_on.name}. Confirm that's who.")
    if also_could_be and target.kind == "person":
        found.flags.append(f"That name could also mean {', '.join(also_could_be[:4])}. Confirm it's {target.name}.")
    if target.kind == "group":
        found.flags.append(f"This goes to the group chat {target.name}: all {target.audience} others in it see it.")
    if target.kind == "person" and not target.in_contacts:
        found.flags.append(f"{target.reached_at} isn't saved in Contacts. Confirm the number.")
    if target.new_thread_handle:
        found.flags.append(f"There's no thread with {target.reached_at} on this Mac yet, so this starts a new one by iMessage.")
    for path in files:
        found.flags.append(f"Check it's the right file: {path.name}.")
    return found


# ─── Files ──────────────────────────────────────────────────────────────────


def check_files(paths: list[str]) -> list[Path]:
    """Only real, readable, reasonably sized files the owner keeps, never system or login stores."""
    if len(paths) > MAX_FILES:
        raise SendError(f"At most {MAX_FILES} files in one text.")
    home = Path.home().resolve()
    blocked = [home / "Library", home / ".ssh", home / ".gnupg", home / ".config"]
    out = []
    for raw in paths:
        path = Path(os.path.expanduser(str(raw))).resolve()
        if not path.is_file():
            raise SendError(f"No file at {raw}.")
        if any(path == b or b in path.parents for b in blocked) or any(part.startswith(".") for part in path.parts[1:]):
            raise SendError(f"{path.name} is in a private or system folder, so it can't be sent from here.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SendError(f"{path.name} is over 100 MB, too big to text.")
        out.append(path)
    return out


def stage_files(install: str, files: list[Path]) -> list[Path]:
    """Copy each file where Messages can read it: ~/Pictures/<install>/outgoing/<time>/."""
    if not files:
        return []
    folder = outgoing_dir(install) / datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    folder.mkdir(parents=True, exist_ok=True)
    copies = []
    for index, path in enumerate(files):
        target = folder / (f"{index + 1}-{path.name}" if len(files) > 1 else path.name)
        shutil.copy2(path, target)
        copies.append(target)
    return copies


# ─── AppleScript, on standard input ─────────────────────────────────────────


def as_literal(text: str) -> str:
    """An AppleScript string expression for any text: quotes and backslashes escaped,
    line breaks joined with linefeed, so nothing in the text can end the string early."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts = ['"' + line.replace("\\", "\\\\").replace('"', '\\"') + '"' for line in lines]
    return " & linefeed & ".join(parts) if parts else '""'


def script_for(target: Target, text: str, files: list[Path]) -> str:
    """The AppleScript Messages runs. File references are made before the tell block:
    inside it, Messages misreads `POSIX file`."""
    lines = []
    for index, path in enumerate(files):
        lines.append(f"set theFile{index} to POSIX file {as_literal(str(path))}")
    if text.strip():
        lines.append(f"set theText to {as_literal(text)}")
    lines.append('tell application "Messages"')
    if target.chat_guid:
        lines.append(f"\tset theChat to chat id {as_literal(target.chat_guid)}")
    else:
        lines.append("\tset theService to 1st account whose service type = iMessage")
        lines.append(f"\tset theChat to participant {as_literal(target.new_thread_handle)} of theService")
    for index in range(len(files)):
        lines.append(f"\tsend theFile{index} to theChat")
    if text.strip():
        lines.append("\tsend theText to theChat")
    lines.append("end tell")
    return "\n".join(lines) + "\n"


def run_script(script: str) -> tuple[bool, str]:
    """Hand the script to osascript on standard input. Returns (ok, plain error)."""
    try:
        done = _spawn([OSASCRIPT, "-"], input=script, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Messages didn't respond ({type(exc).__name__})."
    if done.returncode == 0:
        return True, ""
    err = (done.stderr or "").strip()
    if "-1743" in err or "Not authorized" in err:
        return False, (
            "macOS hasn't allowed this connector to control Messages. Open System Settings, then Privacy & "
            f"Security, then Automation, find Python ({python_app()}) or Claude, and turn on Messages. "
            "Then preview the text again."
        )
    if "-1728" in err or "Can't get" in err:
        return False, "Messages couldn't find that conversation. Open it once in Messages, then preview again."
    return False, "Messages refused the send. Nothing is known to have gone out; check the thread in Messages."


# ─── The record ─────────────────────────────────────────────────────────────


def audit(config: Config, event: str, **fields: Any) -> None:
    """Every text sent, refused or failed. This Mac only. Never a password: those are refused."""
    folder = home_dir(config.install)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        descriptor = os.open(sent_log_path(config.install), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def remember_nickname(nickname: str, full_name: str) -> str:
    return shared_nicknames.remember(nickname, full_name)
