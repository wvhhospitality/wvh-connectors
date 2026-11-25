"""Sending: the owner's words, a preview, their yes, then exactly that message, once.

Every name and address here is made up. The mail server is a fake: nothing leaves.
"""

import asyncio
import email
import json
import os
import time
from pathlib import Path

import pytest
import yaml
from mcp import types

from wvh_mail_mcp import guard
from wvh_mail_mcp import server as srv

from conftest import TWO_MAILBOXES, raw_message, settings_folder, write_settings

ALL_MAIL = "[Gmail]/All Mail"


def run(tool: str, **args):
    return asyncio.run(srv._DISPATCH[tool](args))


def call(name: str, arguments: dict) -> dict:
    server = srv.build_server()
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    return json.loads(asyncio.run(handler(req)).root.content[0].text)


@pytest.fixture
def ready(isolated, fake_imap, fake_smtp):
    """Robin's two mailboxes, a login for Studio, and a mailbox that has heard from a few people."""
    write_settings(TWO_MAILBOXES)
    isolated.set_password("robin-mail", "robin@example.com", "abcdabcdabcdabcd")
    seen = [
        ("Dana Northwind <dana@example.org>", "<d1@example.org>"),
        ("Sam Rivera <sam@example.com>", "<s1@example.com>"),
        ("Nico Reyes <nico@example.org>", "<n1@example.org>"),
    ]
    for uid, (sender, mid) in enumerate(seen, start=1):
        fake_imap.folders[ALL_MAIL][uid] = (raw_message(sender=sender, message_id=mid, subject=f"Hello {uid}"), (b"\\Seen",))
    return fake_imap


def send(**args):
    base = {"account": "Studio", "subject": "The deck", "body": "Here it is."}
    return run("send_message", **{**base, **args})


def confirm(preview, reply="yes, send it"):
    return run("confirm_send", ticket=preview["confirm_with"]["ticket"],
               expect_account=preview["confirm_with"]["expect_account"], owner_reply=reply)


def records():
    path = settings_folder() / "sent.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ─── A preview never sends ───────────────────────────────────────────────────


def test_a_preview_sends_nothing_and_says_what_would_go(ready, fake_smtp):
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    assert out["preview"] and out["nothing_sent"] and not out.get("blocked")
    assert fake_smtp.sent == [] and not [c for c in ready.calls if c[0] == "append"]
    assert out["say_this"].startswith("From Studio <robin@example.com> to sam@example.com; subject 'The deck'")
    assert out["read_back_required"] is False, "a colleague, named, inside the company: no read-back needed"
    assert set(out["confirm_with"]) == {"ticket", "expect_account"}


def test_confirm_sends_exactly_the_previewed_message_once(ready, fake_smtp):
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    done = confirm(out)
    assert done["sent"] and done["to"] == "sam@example.com"
    (msg,) = fake_smtp.sent
    assert msg["Subject"] == "The deck" and msg["From"] == "robin@example.com"
    assert fake_smtp.logins == [("robin@example.com", "smtp.gmail.com", 465)]
    again = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "robin@example.com"})
    assert again["error"] == "confirmation" and "already used" in again["detail"]
    assert len(fake_smtp.sent) == 1


def test_a_confirmation_expires(ready, fake_smtp, monkeypatch):
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    monkeypatch.setattr(guard, "TICKET_SECONDS", -1)
    res = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "robin@example.com"})
    assert res["error"] == "confirmation" and "expired" in res["detail"]
    assert fake_smtp.sent == []


def test_a_confirmation_is_bound_to_the_exact_message(ready, fake_smtp):
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    pending = srv.state.tickets._open[out["confirm_with"]["ticket"]]
    pending.msg.replace_header("To", "someone@example.net")
    res = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "robin@example.com"})
    assert res["error"] == "confirmation" and "changed" in res["detail"]
    assert fake_smtp.sent == []


def test_the_wrong_mailbox_in_the_confirmation_spends_it(ready, fake_smtp):
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    res = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "Personal"})
    assert res["error"] == "refused" and fake_smtp.sent == []
    res = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "robin@example.com"})
    assert res["error"] == "confirmation"


def test_the_label_works_as_the_expected_mailbox(ready, fake_smtp):
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    assert run("confirm_send", ticket=out["confirm_with"]["ticket"], expect_account="studio")["sent"]


# ─── Who the owner's words name ──────────────────────────────────────────────


def test_an_address_in_the_owner_s_words_names_it(ready):
    out = send(to="brand.new@example.com", instruction="send the deck to brand.new@example.com")
    assert not out.get("blocked")


def test_a_name_the_mailbox_knows_names_the_address(ready):
    out = send(to="dana@example.org", instruction="Email Dana the deck")
    assert not out.get("blocked")
    assert "Goes outside: dana@example.org." in out["flags"] and out["read_back_required"]


def test_nobody_named_is_blocked_and_recorded(ready, fake_smtp):
    out = send(to="nico@example.org", instruction="Email Dana the deck")
    assert out["blocked"] and "don't name anyone this goes to" in out["reasons"][0]
    assert "confirm_with" not in out
    (row,) = records()
    assert row["event"] == "refused" and row["to"] == "nico@example.org"


def test_no_instruction_is_blocked(ready):
    out = send(to="sam@example.com", instruction="  ")
    assert out["blocked"] and "No instruction from Robin" in out["reasons"][0]


def test_me_means_the_owner_s_own_mailbox_only(ready):
    out = send(to="robin@example.com", instruction="send me the notes")
    assert not out.get("blocked") and out["read_back_required"] is False
    out = send(to="nico@example.org", instruction="send me the notes")
    assert out["blocked"], "'me' never names anyone else"


def test_a_name_resolves_to_the_address_the_mailbox_knows(ready, fake_smtp):
    out = send(to="Dana Northwind", instruction="Email Dana Northwind the deck")
    assert out["to"] == "Dana Northwind <dana@example.org>" and not out.get("blocked")


def test_a_name_that_fits_two_people_asks_which(ready):
    ready.folders[ALL_MAIL][9] = (raw_message(sender="Dana Lark <dana.lark@example.net>", message_id="<x9@example.net>"), ())
    res = call("send_message", {"account": "Studio", "to": "Dana", "subject": "x", "body": "y", "instruction": "email Dana"})
    assert res["error"] == "refused" and "more than one address" in res["detail"]
    assert "dana.lark@example.net" in res["detail"] and "dana@example.org" in res["detail"]


def test_a_name_nobody_has_asks_for_the_address(ready):
    res = call("send_message", {"account": "Studio", "to": "Quinn", "subject": "x", "body": "y", "instruction": "email Quinn"})
    assert res["error"] == "refused" and "Ask the owner for it" in res["detail"]


def test_an_address_nobody_knows_is_flagged(ready):
    out = send(to="new.person@example.com", instruction="send it to new.person@example.com")
    assert not any("Nothing in this mailbox" in f for f in out["flags"]), "written out in full by the owner"
    out = send(to="new.person@example.com", said_as="Pat", instruction="send it to Pat")
    assert any("Nothing in this mailbox or Contacts shows who new.person@example.com is" in f for f in out["flags"])


# ─── Nicknames ───────────────────────────────────────────────────────────────


def test_a_new_nickname_is_read_back_and_remembered_only_after_yes(ready, fake_smtp, nicknames_file):
    out = send(to="dana@example.org", said_as="Danny", instruction="email Danny the deck")
    assert not out.get("blocked")
    assert "Robin said Danny; this goes to Dana Northwind <dana@example.org>. Confirm that's them." in out["flags"]
    assert out["will_remember"] == "Danny = Dana Northwind" and out["read_back_required"]
    assert not nicknames_file.exists(), "nothing is remembered from a preview"
    missing = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "robin@example.com"})
    assert missing["error"] == "refused" and "needs reading back" in missing["detail"]
    assert not nicknames_file.exists()
    done = confirm(out, reply="yes that's Danny")
    assert done["nickname"] == "Remembered: Danny = Dana Northwind."
    assert yaml.safe_load(nicknames_file.read_text())["nicknames"] == {"Danny": "Dana Northwind"}
    assert "@" not in nicknames_file.read_text()


def test_a_remembered_nickname_names_them_next_time(ready, nicknames_file):
    nicknames_file.write_text("nicknames:\n  Danny: Dana Northwind\n")
    out = send(to="dana@example.org", said_as="Danny", instruction="email Danny the deck")
    assert not any("Confirm that's them" in f for f in out["flags"]) and "will_remember" not in out
    out = send(to="dana@example.org", instruction="email Danny the deck")
    assert not out.get("blocked")


def test_said_as_must_be_the_owner_s_own_word(ready):
    out = send(to="dana@example.org", said_as="Danny", instruction="email her the deck")
    assert out["blocked"] and any("isn't in their words" in r for r in out["reasons"])


def test_a_nickname_that_already_means_someone_else_is_not_overwritten(ready, fake_smtp, nicknames_file):
    nicknames_file.write_text("nicknames:\n  Danny: Nico Reyes\n")
    out = send(to="dana@example.org", said_as="Danny", instruction="email Danny the deck")
    assert any("which is saved as Nico Reyes" in f for f in out["flags"])
    assert "will_remember" not in out
    done = confirm(out)
    assert done["sent"] and "nickname" not in done
    assert yaml.safe_load(nicknames_file.read_text())["nicknames"] == {"Danny": "Nico Reyes"}


def test_remember_reports_a_conflict_at_confirm(ready, fake_smtp, nicknames_file):
    out = send(to="dana@example.org", said_as="Danny", instruction="email Danny the deck")
    nicknames_file.write_text("nicknames:\n  Danny: Nico Reyes\n")  # changed between preview and yes
    done = confirm(out)
    assert done["sent"] and done["nickname"].startswith("Not saved: Danny already means someone else")


# ─── Conversations already under way ─────────────────────────────────────────


def test_replying_to_what_the_owner_just_read_continues_it(ready, fake_smtp):
    ready.folders["INBOX"][7] = (raw_message(sender="Dana Northwind <dana@example.org>"), ())
    run("read_message", account="Studio", uid=7)
    out = run("reply_message", account="Studio", uid=7, body="Yes, Friday works.", instruction="tell her yes")
    assert not out.get("blocked") and out["read_back_required"]
    assert any("continues the email from dana@example.org" in f for f in out["flags"])
    done = confirm(out, reply="yes")
    assert done["sent"] and done["subject"] == "Re: Friday works"


def test_a_reply_without_a_name_or_a_conversation_is_blocked(ready):
    ready.folders["INBOX"][7] = (raw_message(sender="Dana Northwind <dana@example.org>"), ())
    out = run("reply_message", account="Studio", uid=7, body="Yes.", instruction="tell her yes")
    assert out["blocked"]


def test_a_follow_up_to_the_last_send_continues_it(ready, fake_smtp):
    first = send(to="sam@example.com", instruction="Email Sam the deck")
    confirm(first)
    out = send(to="sam@example.com", subject="One more", instruction="and send the other one too")
    assert not out.get("blocked") and out["read_back_required"]
    assert any("continues the email to sam@example.com" in f for f in out["flags"])


def test_a_conversation_stops_counting_after_twelve_hours(ready, fake_smtp, monkeypatch):
    confirm(send(to="sam@example.com", instruction="Email Sam the deck"))
    later = time.time() + guard.CONTINUE_SECONDS + 60
    srv.state.memory._clock = lambda: later
    assert send(to="sam@example.com", instruction="and the other one")["blocked"]


def test_the_last_send_is_remembered_across_a_restart(ready, fake_smtp):
    confirm(send(to="sam@example.com", instruction="Email Sam the deck"))
    srv.state._memory = None  # as if the server had just started
    out = send(to="sam@example.com", instruction="and the other one")
    assert not out.get("blocked")


# ─── Flags that need the owner's yes ─────────────────────────────────────────


def test_outside_recipients_are_read_back(ready):
    out = send(to="dana@example.org", instruction="Email Dana the deck")
    assert "Goes outside: dana@example.org." in out["flags"]
    assert out["confirm_with"]["owner_reply"].startswith("<Robin's own yes")


def test_internal_domains_can_be_set(ready):
    write_settings(TWO_MAILBOXES + "internal_domains: [example.org, example.com]\n")
    out = send(to="dana@example.org", instruction="Email Dana the deck")
    assert not any(f.startswith("Goes outside") for f in out["flags"])


def test_sensitive_subjects_to_a_crowd_are_read_back(ready):
    out = send(to=["sam@example.com", "robin@example.com", "pat@example.com"], body="Payroll runs Friday.",
               instruction="email Sam and the team that payroll runs Friday")
    assert any("Sensitive subject (payroll) going to 3 people" in f for f in out["flags"])


# Built at run time so this file never holds anything shaped like a real secret.
SECRET_SHAPES = ["pass" + "word: " + "hunter2" * 2, "my SSN is " + "-".join(["123", "45", "6789"]),
                 "-----BEGIN RSA " + "PRIVATE KEY-----"]


@pytest.mark.parametrize("body", SECRET_SHAPES)
def test_secrets_never_go_out(ready, fake_smtp, body):
    out = send(to="sam@example.com", body=body, instruction="Email Sam")
    assert out["blocked"] and "Secrets never go out" in " ".join(out["reasons"])


def test_the_record_has_no_body_and_no_password(ready, fake_smtp):
    long_body = "Here is the plan. " * 30
    confirm(send(to="sam@example.com", body=long_body, instruction="Email Sam the plan"))
    (row,) = records()
    assert row["event"] == "sent" and row["recipients"] == ["sam@example.com"]
    assert row["body_start"] == long_body[:200] and len(row["body_sha256"]) == 64
    text = (settings_folder() / "sent.jsonl").read_text()
    assert "abcdabcdabcdabcd" not in text and long_body not in text


# ─── Reply-all, threading, files ─────────────────────────────────────────────


def test_reply_all_copies_everyone_else_never_this_mailbox(ready, fake_smtp):
    ready.folders["INBOX"][7] = (raw_message(
        sender="Dana Northwind <dana@example.org>", to="robin@example.com, sam@example.com",
        cc="Nico Reyes <nico@example.org>, robin@example.com", references="<older@example.org>"), ())
    out = run("reply_message", account="Studio", uid=7, reply_all=True, body="Works for me.", instruction="reply all to Dana yes")
    confirm(out)
    (msg,) = fake_smtp.sent
    assert msg["To"] == "dana@example.org"
    assert msg["Cc"] == "sam@example.com, nico@example.org"
    assert msg["In-Reply-To"] == "<abc123@example.org>"
    assert msg["References"] == "<older@example.org> <abc123@example.org>"
    assert msg["Subject"] == "Re: Friday works"


def test_files_attach_from_downloads_and_are_read_back(ready, fake_smtp, home):
    downloads = home / "Downloads"
    downloads.mkdir()
    (downloads / "deck.pdf").write_bytes(b"%PDF-1.4 made up")
    out = send(to="sam@example.com", attachments=[str(downloads / "deck.pdf")], instruction="Email Sam the deck")
    assert any(f.startswith("Attaches: deck.pdf") for f in out["flags"]) and out["read_back_required"]
    confirm(out)
    (msg,) = fake_smtp.sent
    assert [p.get_filename() for p in msg.iter_attachments()] == ["deck.pdf"]


@pytest.mark.parametrize("where, name", [(".ssh", "id_rsa"), ("Documents", "deck.pdf"), ("Downloads", "server.pem")])
def test_files_from_anywhere_else_are_refused(ready, home, where, name):
    folder = home / where
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"x")
    res = call("send_message", {"account": "Studio", "to": "sam@example.com", "subject": "x", "body": "y",
                                "attachments": [str(folder / name)], "instruction": "Email Sam"})
    assert res["error"] == "attach_refused"


# ─── Identities and signatures ───────────────────────────────────────────────

IDENTITIES = """\
identities:
  - name: Robin
    signature: robin.html
  - name: Helper
    signature: helper.html
    drafts_only: true
default_identity: Robin
"""


def signatures():
    folder = settings_folder() / "signatures"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "robin.html").write_text("<p><b>Robin Vale</b> | Northwind Builders</p>")
    (folder / "robin.txt").write_text("Robin Vale\nNorthwind Builders")
    (folder / "helper.html").write_text("<p>Helper, for Robin Vale</p>")


def test_sends_carry_the_writer_s_signature(ready, fake_smtp):
    write_settings(TWO_MAILBOXES + IDENTITIES)
    signatures()
    out = send(to="sam@example.com", instruction="Email Sam the deck")
    assert out["identity"] == "Robin" and out["signature"] == "robin.html" and "signed as Robin" in out["say_this"]
    confirm(out)
    (msg,) = fake_smtp.sent
    assert "Robin Vale\nNorthwind Builders" in msg.get_body(("plain",)).get_content()
    assert "<b>Robin Vale</b>" in msg.get_body(("html",)).get_content()


def test_a_drafts_only_identity_can_draft_but_never_send(ready, fake_smtp):
    write_settings(TWO_MAILBOXES + IDENTITIES)
    signatures()
    out = send(to="sam@example.com", identity="Helper", instruction="Email Sam the deck")
    assert out["blocked"] and "Helper drafts but never sends" in out["reasons"][0]
    draft = run("append_draft", account="Studio", to="sam@example.com", subject="x", body="y", identity="Helper")
    assert draft["ok"] and draft["identity"] == "Helper" and draft["signature"] == "helper.html"
    assert fake_smtp.sent == []


def test_a_signature_is_added_once_not_twice(ready, fake_smtp):
    write_settings(TWO_MAILBOXES + IDENTITIES)
    signatures()
    sig = "<p><b>Robin Vale</b> | Northwind Builders</p>"
    out = send(to="sam@example.com", html_body="<p>Hi</p>" + sig, instruction="Email Sam the deck")
    confirm(out)
    assert fake_smtp.sent[0].get_body(("html",)).get_content().count("Robin Vale") == 1


# ─── Limits, other servers ───────────────────────────────────────────────────


def test_the_rate_limit_stops_a_runaway(ready, fake_smtp):
    write_settings(TWO_MAILBOXES + "send_limits:\n  max_per_window: 1\n  window_seconds: 300\n")
    confirm(send(to="sam@example.com", instruction="Email Sam the deck"))
    out = send(to="sam@example.com", subject="Again", instruction="Email Sam again")
    res = call("confirm_send", {"ticket": out["confirm_with"]["ticket"], "expect_account": "robin@example.com", "owner_reply": "yes"})
    assert res["error"] == "rate_limited" and len(fake_smtp.sent) == 1


def test_a_server_that_doesn_t_file_sent_mail_gets_a_copy(isolated, fake_imap, fake_smtp):
    write_settings("owner_name: Robin\naccounts:\n  - email: robin@example.net\n    imap_host: imap.mail.me.com\n")
    isolated.set_password("robin-mail", "robin@example.net", "pw")
    out = run("send_message", to="robin@example.net", subject="Note", body="x", instruction="send me a note")
    done = confirm(out)
    assert done["sent_copy"] == "[Gmail]/Sent Mail"
    assert fake_smtp.logins[0] == ("starttls", "smtp.mail.me.com")
    (copy,) = fake_imap.folders["[Gmail]/Sent Mail"].values()
    assert copy[1] == (b"\\Seen",)


def test_gmail_files_its_own_sent_copy(ready, fake_smtp):
    done = confirm(send(to="sam@example.com", instruction="Email Sam the deck"))
    assert "sent_copy" not in done and not ready.folders["[Gmail]/Sent Mail"]


# ─── Changes the owner asks for ──────────────────────────────────────────────


def test_flag_and_mark_read_only_when_asked(ready):
    ready.folders["INBOX"][7] = (raw_message(), ())
    run("read_message", account="Studio", uid=7)
    assert ready.folders["INBOX"][7][1] == ()
    assert run("flag_message", account="Studio", uid=7, flag="star")["added"] == "flagged"
    run("mark_read", account="Studio", uid=7)
    assert set(ready.folders["INBOX"][7][1]) == {b"\\Flagged", b"\\Seen"}


def test_save_attachment_goes_to_downloads_under_the_install(ready, home):
    ready.folders["INBOX"][7] = (raw_message(attachments=[("call sheet.pdf", "application/pdf", b"%PDF-1.4 x")]), ())
    out = run("save_attachment", account="Studio", uid=7)
    (saved,) = out["saved"]
    path = Path(saved["path"])
    assert path.parent == (home / "Downloads" / "robin-mail").resolve()
    assert path.name.endswith("_example.org_call-sheet.pdf") and path.read_bytes() == b"%PDF-1.4 x"
    assert not os.access(path, os.X_OK)
