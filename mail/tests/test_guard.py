"""The checks themselves, as plain functions. Every name here is made up."""

import json
import time
from datetime import datetime, timezone

from wvh_mail_mcp import guard


def who(address, *names, nicks=()):
    return guard.Who(address=address, names=set(names), nicknames=set(nicks))


def review(instruction, people, said_as=None, continuation=None, attachments=(), known=None, ident=None, texts=("hi",)):
    return guard.review(
        owner="Robin", instruction=instruction, said_as=said_as, people=people,
        own_addresses={"robin@example.com"}, inside_domains={"example.com"}, texts=list(texts),
        continuation=continuation, attachments=list(attachments), drafts_only_identity=ident, known_nickname=known,
    )


def test_first_names_and_nicknames_name_the_recipient():
    dana = who("dana@example.org", "Dana Northwind", "Dana")
    assert not review("email Dana the deck", [dana]).blocks
    assert not review("email Danny the deck", [who("dana@example.org", "Dana Northwind", nicks=["Danny"])]).blocks
    assert review("email Sam the deck", [dana]).blocks


def test_filler_words_never_count_as_a_name():
    team = who("team@example.org", "Team Inbox")
    assert review("email the team", [team]).blocks


def test_full_name_prefers_first_and_last():
    assert who("x@example.org", "Dana", "Dana Northwind").full_name == "Dana Northwind"


def test_tickets_expire_and_are_single_use():
    import email.message

    now = [0.0]
    tickets = guard.Tickets(clock=lambda: now[0])
    msg = email.message.EmailMessage()
    msg["To"] = "dana@example.org"
    msg.set_content("x")
    pending = guard.Pending("send_message", "robin@example.com", msg, "email Dana", False, 0.0, guard.fingerprint(msg), {})
    t = tickets.issue(pending)
    assert tickets.peek(t) is pending
    tickets.spend(t)
    try:
        tickets.peek(t)
        raise AssertionError("a spent ticket worked")
    except guard.TicketError:
        pass
    t = tickets.issue(pending)
    now[0] = guard.TICKET_SECONDS + 1
    try:
        tickets.peek(t)
        raise AssertionError("an old ticket worked")
    except guard.TicketError as exc:
        assert "expired" in str(exc)


def test_memory_reads_recent_sends_from_the_record(tmp_path):
    log = tmp_path / "sent.jsonl"
    old = {"ts": "2020-01-01T00:00:00+00:00", "event": "sent", "account": "robin@example.com", "recipients": ["old@example.org"]}
    new = {"ts": datetime.now(timezone.utc).isoformat(), "event": "sent", "account": "robin@example.com", "recipients": ["sam@example.com"]}
    refused = {**new, "event": "refused", "recipients": ["nico@example.org"]}
    log.write_text("\n".join(json.dumps(r) for r in (old, new, refused)) + "\nnot json\n")
    memory = guard.Memory(log)
    assert memory.continuation("robin@example.com", {"sam@example.com"}, None, set())
    assert memory.continuation("robin@example.com", {"old@example.org"}, None, set()) is None
    assert memory.continuation("robin@example.com", {"nico@example.org"}, None, set()) is None
    assert memory.continuation("other@example.com", {"sam@example.com"}, None, set()) is None
