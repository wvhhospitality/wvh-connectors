"""Building and sending mail over SMTP (the protocol mail apps use to send).

Nothing in this module decides WHETHER to send: the server's preview and the owner's
yes do that (see guard.py). This module only builds a message exactly as previewed and,
when confirm_send calls it, hands it to the mail server. Guardrails that live here:

  - Every send is rate-limited per mailbox (a sliding window), so a runaway loop stops.
  - Recipients per message are capped.
  - The app password comes from the Keychain at send time and is never logged or shown.
  - Nothing is ever deleted, moved or expunged from here.
"""

from __future__ import annotations

import html as _html
import re
import smtplib
import ssl
import threading
import time
from collections import deque
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formatdate, getaddresses, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi

from .config import Account, SendLimits, get_password, signatures_dir


class SendError(Exception):
    """The message can't be built or sent as asked. Nothing went out."""


class SendRateLimited(SendError):
    """A mailbox sent its allowance for the window."""


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


# ─── Rate limit ──────────────────────────────────────────────────────────────


class SendLimiter:
    """Per-mailbox sliding window. A slot is used before the server is dialled, so a
    failing send still counts: that throttles retry storms too."""

    def __init__(self, limits: SendLimits, now=time.time) -> None:
        self._limits = limits
        self._now = now
        self._sends: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, account_email: str) -> None:
        now = self._now()
        cutoff = now - self._limits.window_seconds
        with self._lock:
            q = self._sends.setdefault(account_email.lower(), deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._limits.max_per_window:
                raise SendRateLimited(
                    f"{account_email} has sent {self._limits.max_per_window} messages in the last "
                    f"{self._limits.window_seconds // 60} minutes, the most this connector allows. Wait a few minutes. "
                    "(send_limits in the settings file changes this.)"
                )
            q.append(now)


# ─── Signatures ──────────────────────────────────────────────────────────────


def load_signature(filename: str | None, install: str | None = None) -> tuple[str, str] | None:
    """(html, plain text) for a signature file in the settings folder's signatures/.
    The plain text is the .txt beside it, or the HTML with its tags taken out."""
    if not filename:
        return None
    folder = signatures_dir(install).resolve()
    path = (folder / filename).resolve()
    if folder not in path.parents or not path.is_file():
        return None
    html = path.read_text(encoding="utf-8")
    txt = path.with_suffix(".txt")
    if txt.is_file():
        text = txt.read_text(encoding="utf-8").strip()
    else:
        text = re.sub(r"<br\s*/?>|</(p|div|tr)>", "\n", html, flags=re.I)
        text = _html.unescape(re.sub(r"<[^>]+>", "", text))
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return html, text


def apply_signature(body: str, html_body: str | None, sig_html: str, sig_text: str) -> tuple[str, str]:
    """Add a signature to both parts. Without an html_body, the plain body is escaped into
    a simple block so the formatted signature can ride along. Pure function."""
    if html_body and sig_html.strip() and sig_html.strip() in html_body:
        return body, html_body  # already signed: never twice
    new_body = body.rstrip() + ("\n\n" + sig_text if sig_text else "")
    if html_body:
        new_html = html_body + "<br>" + sig_html
    else:
        escaped = _html.escape(body).replace("\n", "<br>")
        new_html = (
            '<div style="font-family: Helvetica, Arial, sans-serif; font-size: 13px; line-height: 1.5;">'
            f"{escaped}</div><br>" + sig_html
        )
    return new_body, new_html


# ─── Building ────────────────────────────────────────────────────────────────


def one_line(value) -> str:
    """Line breaks in a header would let text add headers of its own; they become spaces."""
    if value is None:
        return ""
    return re.sub(r"\s*[\r\n]+\s*", " ", str(value)).strip()


def address_list(value) -> list[str]:
    """A string or list of addresses, each possibly 'Name <address>', as clean entries."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for v in value:
        text = one_line(v)
        if not text:
            continue
        for name, addr in getaddresses([text]):
            if addr:
                out.append(f"{name} <{addr}>" if name else addr)
    return out


def bare(entries) -> list[str]:
    """Just the addresses, lowercased."""
    return [addr.lower() for _n, addr in getaddresses([", ".join(address_list(entries))]) if addr]


_RE = re.compile(r"(?i)^\s*re\s*:")


def reply_subject(subject: str) -> str:
    subject = one_line(subject)
    return subject if _RE.match(subject) else f"Re: {subject}".strip()


def build_message(
    from_addr: str,
    to,
    subject: str,
    body: str,
    cc=None,
    bcc=None,
    html_body: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    max_recipients: int = 20,
    zone: ZoneInfo | None = None,
    message_id_mark: str | None = None,
) -> EmailMessage:
    """A finished message. Pure function: no network."""
    to, cc, bcc = address_list(to), address_list(cc), address_list(bcc)
    if not to:
        raise SendError("A message needs at least one 'to' address.")
    total = len(to) + len(cc) + len(bcc)
    if total > max_recipients:
        raise SendError(f"{total} recipients is more than {max_recipients} for one message (send_limits.max_recipients).")
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)  # smtplib reads it for the envelope and leaves it out of what is sent
    msg["Subject"] = one_line(subject)
    msg["Date"] = format_datetime(datetime.now(zone)) if zone else formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(idstring=message_id_mark, domain=from_addr.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = one_line(in_reply_to)
    if references:
        msg["References"] = one_line(references)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def reply_headers(ctx: dict, self_email: str, reply_all: bool) -> dict:
    """To, Cc, Subject, In-Reply-To and References for a reply in the same conversation.
    Reply-all copies everyone else on the message, never this mailbox itself, never twice."""
    to = [a for a in (ctx.get("reply_to") or []) if a.lower() != self_email.lower()] or list(ctx.get("reply_to") or [])
    if not to:
        raise SendError("The original message has no address to reply to.")
    cc: list[str] = []
    if reply_all:
        seen = {a.lower() for a in to} | {self_email.lower()}
        for addr in (ctx.get("to") or []) + (ctx.get("cc") or []):
            if addr.lower() not in seen:
                cc.append(addr)
                seen.add(addr.lower())
    original = one_line(ctx.get("message_id") or "")
    refs: list[str] = []
    for part in one_line(ctx.get("references") or "").split() + [original]:
        if part and part not in refs:
            refs.append(part)
    return {
        "to": to,
        "cc": cc,
        "subject": reply_subject(ctx.get("subject") or ""),
        "in_reply_to": original or None,
        "references": " ".join(refs) or None,
    }


def recipients(msg: EmailMessage) -> list[str]:
    found: list[str] = []
    for header in ("To", "Cc", "Bcc"):
        value = msg.get(header)
        if value:
            found += [addr for _n, addr in getaddresses([str(value)]) if addr]
    return found


# ─── Sending ─────────────────────────────────────────────────────────────────


def send(account: Account, msg: EmailMessage, install: str | None = None, limiter: SendLimiter | None = None) -> str:
    """Hand one finished message to the mail server. Returns its Message-ID."""
    if limiter is not None:
        limiter.check(account.email)
    login = get_password(account, install)
    try:
        if account.smtp_starttls:
            with smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=_ssl_context())
                smtp.login(account.email, login)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, context=_ssl_context(), timeout=30) as smtp:
                smtp.login(account.email, login)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise SendError(
            f"The mail server refused the sign-in for sending from {account.email} (code {exc.smtp_code}). "
            "Check its app password with: wvh-mail-setup password " + account.email
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(
            f"Sending from {account.email} failed: {exc}. It most likely didn't go out; check that mailbox's Sent "
            "before trying again."
        ) from exc
    finally:
        del login
    return str(msg["Message-ID"])
