"""Building a draft. Pure functions: no network, nothing is sent from here or anywhere."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formatdate, make_msgid
from zoneinfo import ZoneInfo

from .imap_client import DRAFT_MARK

MAX_RECIPIENTS = 50


class DraftError(Exception):
    """The draft can't be built as asked. Nothing was saved."""


def one_line(value) -> str:
    """Line breaks in a header would let text add headers of its own; they become spaces."""
    if value is None:
        return ""
    return re.sub(r"\s*[\r\n]+\s*", " ", str(value)).strip()


def address_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in re.split(r"[,;]", value)]
    return [one_line(v) for v in value if one_line(v)]


_RE = re.compile(r"(?i)^\s*re\s*:")


def reply_subject(subject: str) -> str:
    subject = one_line(subject)
    return subject if _RE.match(subject) else f"Re: {subject}".strip()


def build_draft(
    from_addr: str,
    to,
    subject: str,
    body: str,
    cc=None,
    html_body: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    zone: ZoneInfo | None = None,
) -> EmailMessage:
    to, cc = address_list(to), address_list(cc)
    if not to:
        raise DraftError("A draft needs at least one 'to' address.")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise DraftError(f"{len(to) + len(cc)} recipients is more than {MAX_RECIPIENTS} for one draft.")
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = one_line(subject)
    msg["Date"] = format_datetime(datetime.now(zone)) if zone else formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(idstring=DRAFT_MARK, domain=from_addr.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = one_line(in_reply_to)
        msg["References"] = one_line(f"{references or ''} {in_reply_to}")
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg
