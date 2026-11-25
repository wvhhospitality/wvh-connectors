"""The discretion filter: what must be true before anything is written to Slack.

The rule is "confirm the realm before acting." It was built after an automation
posted one client's sensitive alerts into another client's channel. Each rule below
would have stopped that post.

Everything here is a pure function of what the server already knows, so it can't
be argued with by a prompt. Blocks stop a write outright. Flags let it continue
only after the realm, recipient, and words are read back to the owner, who says yes.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Workspace, owner_cap, owner_name

TICKET_SECONDS = 600  # a confirmation doesn't survive a gap

_LEGAL = r"\b(llc|inc|incorporated|corp|co)\.?$"
_FILLER = {"llc", "inc", "the", "and", "all", "for", "general", "channel", "team", "group"}

_SECRETS = (
    (r"xox[abeoprs]-[0-9A-Za-z-]{8,}", "a Slack token"),
    (r"sk-ant-[0-9A-Za-z_-]{8,}", "an API key"),
    (r"\bAKIA[0-9A-Z]{12,}\b", "an API key"),
    (r"\bgh[pousr]_[0-9A-Za-z]{20,}\b", "an access token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"\b(password|passwd|passcode)\s*[:=]\s*\S+", "a password"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "a Social Security number"),
)
_SENSITIVE = (
    "payroll", "salary", "salaries", "wages", "social security", "bank account",
    "account number", "routing number", "w-2", "w-9", "1099", "tax id", "ein",
)
_BROADCAST = re.compile(r"(<!(channel|here|everyone)>|(?<!\w)@(channel|here|everyone)\b)", re.I)


class TicketError(Exception):
    """The confirmation is missing, used, or too old."""


@dataclass
class Target:
    kind: str                        # channel | person | object
    id: str
    label: str                       # "#ops", "Dana Northwind"
    people: list[str] = field(default_factory=list)
    audience: int | None = None
    is_general: bool = False
    is_shared: bool = False
    to_self: bool = False             # the owner's own account: a note to self that nobody else sees


@dataclass
class Finding:
    blocks: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def read_back(self) -> bool:
        return bool(self.flags)


@dataclass
class Pending:
    tool: str
    client_code: str
    team_id: str
    args: dict[str, Any]
    target: Target | None
    instruction: str
    read_back: bool
    created: float


class Tickets:
    """Single-use, short-lived, and bound to the exact action that was previewed."""

    def __init__(self) -> None:
        self._open: dict[str, Pending] = {}

    def issue(self, pending: Pending) -> str:
        ticket = secrets.token_urlsafe(9)
        self._open[ticket] = pending
        return ticket

    def peek(self, ticket: str) -> Pending:
        pending = self._open.get(ticket)
        if pending is None:
            raise TicketError("That confirmation is unknown or already used. Preview it again.")
        if time.monotonic() - pending.created > TICKET_SECONDS:
            del self._open[ticket]
            raise TicketError(f"That confirmation expired. Preview it again and re-confirm with {owner_name()}.")
        return pending

    def spend(self, ticket: str) -> None:
        self._open.pop(ticket, None)


def normalize(text: str) -> str:
    text = text.replace("&", " and ").replace("’", "'").lower()
    return re.sub(r"\s+", " ", text).strip()


def mentions(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    return bool(phrase) and re.search(
        rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalize(text)
    ) is not None


# "Send me a test", "remind myself": the owner naming themself.
_SELF = re.compile(r"(?<![a-z0-9'])(?:me|myself)(?![a-z0-9'])")


def names_self(instruction: str) -> bool:
    """True when the owner's own words say "me" or "myself" as a whole word."""
    return _SELF.search(normalize(instruction)) is not None


def realm_phrases(ws: Workspace) -> list[str]:
    """Every way a person might name this client in a sentence."""
    phrases = list(ws.aliases)
    name = ws.name or ""
    inner = re.findall(r"\(([^)]+)\)", name)
    bare = re.sub(r"\([^)]*\)", "", name).strip()
    phrases += [bare, re.sub(_LEGAL, "", bare, flags=re.I).strip(), *inner]
    if ws.team_domain:
        phrases.append(ws.team_domain)
    return sorted({p for p in phrases if len(p) >= 4})


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", normalize(text)) if w not in _FILLER}


def review(
    *,
    ws: Workspace,
    others: list[Workspace],
    instruction: str,
    target: Target | None,
    texts: list[str],
) -> Finding:
    found = Finding()
    body = "\n".join(t for t in texts if t)

    if not instruction.strip():
        found.blocks.append(
            f"No instruction from {owner_name()}. Slack writes happen only on {owner_name()}'s specific command."
        )

    realm_named = re.search(rf"\b{re.escape(ws.client_code)}\b", instruction) is not None or any(
        mentions(instruction, p) for p in realm_phrases(ws)
    )
    named = _words(instruction)
    if target is not None and instruction.strip():
        wanted = _words(target.label) | {w for person in target.people for w in _words(person)}
        # "Me" names the recipient only when the recipient is the owner's own account.
        # It never stands in for anyone else, and it never names the workspace.
        said_me = target.to_self and names_self(instruction)
        if not realm_named and not (named & wanted) and not said_me:
            found.blocks.append(
                f"{owner_cap()}'s instruction doesn't name {target.label}, anyone who'd see this, "
                f"or {ws.label}. Nothing here says this is the place that was meant."
            )

    for other in others:
        hit = next((p for p in realm_phrases(other) if mentions(body, p)), None)
        if hit and not mentions(instruction, hit):
            found.blocks.append(
                f"This names another workspace, {other.label}, and {owner_name()}'s instruction didn't. "
                "That is how a message lands in the wrong company's channel."
            )

    for pattern, what in _SECRETS:
        if re.search(pattern, body, re.I):
            found.blocks.append(f"This appears to contain {what}. Secrets never go in Slack.")

    if _BROADCAST.search(body) and not _BROADCAST.search(instruction):
        found.blocks.append(f"This would notify a whole channel, and {owner_name()} didn't ask for that.")

    if not realm_named:
        found.flags.append(f"The workspace was worked out, not stated. Confirm it's {ws.label}.")
    if target is not None and target.is_general:
        found.flags.append(f"{target.label} is the workspace-wide channel, so everyone there sees it.")
    if target is not None and target.is_shared:
        found.flags.append(f"{target.label} is shared with people outside this workspace.")
    wide = target is None or target.audience is None or target.audience > 2
    hit = next((word for word in _SENSITIVE if mentions(body, word)), None)
    if hit and wide:
        who = f"{target.audience} people" if target and target.audience else "an audience we can't count"
        found.flags.append(f"Sensitive subject ({hit}) going to {who}.")

    return found
