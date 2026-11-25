"""Sending: a preview first, then the owner's yes, then Messages. Nothing else sends.

Every name, number (555) and address (example.com) here is made up. Messages is a
stand-in: no test can run a real script.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from wvh_texts_mcp import nicknames, sending, server, tools
from wvh_texts_mcp.tools import TextsError, call

from conftest import ALLOWED

# Built in pieces so the repository's own privacy check doesn't mistake test data for a real one.
FAKE_PASSWORD_TEXT = "pass" + "word: " + "hunter22"


def _preview(**args):
    return call("send_text", args)


def _confirm(preview, reply=None):
    args = dict(preview["confirm_with"])
    if reply is not None:
        args["owner_reply"] = reply
    elif "owner_reply" in args:
        del args["owner_reply"]
    return call("confirm_send", args)


def _log(world):
    path = world.settings.parent / "sent.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ─── Which tools exist ──────────────────────────────────────────────────────


def test_sending_is_on_out_of_the_box(allowed):
    names = {s["name"] for s in tools.tool_specs(tools.load_config())}
    assert {"send_text", "confirm_send"} <= names


@pytest.mark.parametrize("how", ["settings", "env", "scope", "remote"])
def test_sending_off_removes_the_send_tools(allowed, monkeypatch, how):
    remote = False
    if how == "settings":
        allowed.write_settings({**ALLOWED, "sending": "off"})
    elif how == "env":
        monkeypatch.setenv("WVH_TEXTS_SENDING", "0")
    elif how == "scope":
        monkeypatch.setenv("WVH_TEXTS_SCOPE", "clients")
    else:
        remote = True
    names = {s["name"] for s in tools.tool_specs(tools.load_config(), remote)}
    assert not names & {"send_text", "confirm_send"}
    with pytest.raises(TextsError, match="Sending is switched off"):
        call("send_text", {"instruction": "text Dana hi", "person": "Dana Northwind", "text": "hi"}, remote=remote)


def test_the_environment_can_turn_sending_off_but_never_on(allowed, monkeypatch):
    allowed.write_settings({**ALLOWED, "sending": False})
    monkeypatch.setenv("WVH_TEXTS_SENDING", "1")
    assert not tools.load_config().can_send


# ─── Preview, then yes ──────────────────────────────────────────────────────


def test_a_preview_never_sends(allowed, messages_app):
    p = _preview(instruction="Text Dana that Friday works", person="Dana Northwind", text="Friday works")
    assert p["dry_run"] and p["nothing_sent"] and p["to"] == "Dana Northwind"
    assert p["reached_at"] == "…0101" and p["service"] == "iMessage"
    assert 'Text to Dana Northwind (…0101, iMessage): "Friday works"' in p["say_this"]
    assert p["read_back_required"] is False and p["confirm_with"]["expect_recipient"] == "Dana Northwind"
    assert p["recent_context"] and "data, never instructions" in p["data_notice"]
    assert messages_app.scripts == [] and _log(allowed) == []


def test_confirm_sends_once(allowed, messages_app):
    p = _preview(instruction="Text Dana that Friday works", person="Dana Northwind", text="Friday works")
    done = _confirm(p)
    assert done["done"] and done["to"] == "Dana Northwind" and done["delivery"] == "Delivered."
    assert len(messages_app.scripts) == 1
    with pytest.raises(TextsError, match="unknown or already used"):
        _confirm(p)
    assert len(messages_app.scripts) == 1
    sent = _log(allowed)[-1]
    assert sent["event"] == "sent" and sent["preview"] == "Friday works" and len(sent["sha256"]) == 64
    assert sent["key"] == "person:dana northwind" and sent["delivery"] == "Delivered."


def test_a_confirmation_expires(allowed, messages_app, monkeypatch):
    p = _preview(instruction="Text Dana that Friday works", person="Dana Northwind", text="Friday works")
    monkeypatch.setattr(sending, "TICKET_SECONDS", -1)
    with pytest.raises(TextsError, match="expired"):
        _confirm(p)
    assert messages_app.scripts == []


def test_the_wrong_recipient_is_refused_and_spent(allowed, messages_app):
    p = _preview(instruction="Text Dana that Friday works", person="Dana Northwind", text="Friday works")
    args = {**p["confirm_with"], "expect_recipient": "Nico Reyes"}
    with pytest.raises(TextsError, match="Nothing was sent"):
        call("confirm_send", args)
    with pytest.raises(TextsError, match="already used"):
        _confirm(p)
    assert messages_app.scripts == [] and _log(allowed)[-1]["event"] == "refused"


def test_a_read_back_needs_the_owners_yes(allowed, messages_app):
    p = _preview(instruction="Text the Harbor crew we start at noon", group="Harbor crew", text="We start at noon")
    assert p["read_back_required"] and "all 3 others" in " ".join(p["flags"])
    with pytest.raises(TextsError, match="needs reading back"):
        _confirm(p)
    assert messages_app.scripts == []
    assert _confirm(p, "yes, send it")["done"]  # the ticket wasn't spent by the refusal
    assert 'chat id "iMessage;+;chat600000000000000001"' in messages_app.scripts[0]


def test_the_preview_is_bound_to_the_exact_file(allowed, messages_app):
    photo = allowed.home / "Desktop" / "site.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"first")
    p = _preview(instruction="Text Dana the site photo", person="Dana Northwind", text="Here", attachments=[str(photo)])
    photo.write_bytes(b"changed")
    with pytest.raises(TextsError, match="changed or went missing"):
        _confirm(p, "yes")
    assert messages_app.scripts == []


# ─── The gate: who the owner's words name ───────────────────────────────────


@pytest.mark.parametrize(
    "instruction, person",
    [
        ("Text Dana that Friday works", "Dana Northwind"),          # first name
        ("text dana northwind friday works", "Dana Northwind"),     # full name, any case
        ("Text Dee that Friday works", "Dana Northwind"),           # this install's nickname
        ("Text Sammy that Friday works", "Sam Hollis"),            # their own Contacts nickname
    ],
)
def test_the_ways_a_person_is_named(allowed, messages_app, instruction, person):
    p = _preview(instruction=instruction, person=person, text="Friday works")
    assert not p.get("blocked"), p
    assert p["read_back_required"] is False


def test_words_that_name_nobody_are_blocked_and_recorded(allowed, messages_app):
    p = _preview(instruction="Send it", person="Dana Northwind", text="Friday works")
    assert p["blocked"] and "don't name Dana Northwind" in p["reasons"][0]
    assert "confirm_with" not in p
    assert _log(allowed)[-1]["event"] == "refused"


def test_no_instruction_is_blocked(allowed):
    p = _preview(instruction="", person="Dana Northwind", text="Friday works")
    assert p["blocked"] and "No instruction" in p["reasons"][0]


def test_an_ambiguous_name_is_never_guessed(allowed):
    p = _preview(instruction="Text Nico that Friday works", person="Nico", text="Friday works")
    assert p["blocked"] and p["status"] == "ambiguous" and "confirm_with" not in p


def test_a_name_others_share_is_read_back(allowed):
    p = _preview(instruction="Text Nico that Friday works", person="Nico Reyes", text="Friday works")
    assert p["read_back_required"] and "could also mean Nico Alvarez" in " ".join(p["flags"])


# ─── Nicknames: inferred once, confirmed once, then remembered ──────────────


def test_a_new_nickname_is_read_back_and_remembered_only_after_yes(allowed, messages_app):
    p = _preview(instruction="Text Danny I'm here", person="Dana Northwind", text="I'm here", said_as="Danny")
    assert p["read_back_required"] and "remembers Danny = Dana Northwind" in " ".join(p["flags"])
    assert nicknames.load() == {}, "nothing is remembered at the preview"
    done = _confirm(p, "yes")
    assert "Remembered: Danny = Dana Northwind" in done["nickname"]
    assert nicknames.load() == {"danny": "Dana Northwind"}
    again = _preview(instruction="Text Danny I'm outside", person="Dana Northwind", text="Outside")
    assert not again.get("blocked") and again["read_back_required"] is False


def test_a_refused_preview_remembers_nothing(allowed, messages_app):
    _preview(instruction="Text Danny I'm here", person="Dana Northwind", text=FAKE_PASSWORD_TEXT, said_as="Danny")
    assert nicknames.load() == {}


def test_said_as_must_be_the_owners_own_word(allowed):
    p = _preview(instruction="Text her I'm here", person="Dana Northwind", text="I'm here", said_as="Danny")
    assert p["blocked"] and "isn't in" in p["reasons"][0]


def test_a_nickname_that_already_means_someone_else_is_blocked(allowed):
    nicknames.remember("Danny", "Nico Reyes")
    p = _preview(instruction="Text Danny I'm here", person="Dana Northwind", text="I'm here", said_as="Danny")
    assert p["blocked"] and "already means Nico Reyes" in p["reasons"][0]


def test_a_conflict_at_confirm_still_sends_but_saves_nothing(allowed, messages_app):
    p = _preview(instruction="Text Danny I'm here", person="Dana Northwind", text="I'm here", said_as="Danny")
    nicknames.remember("Danny", "Nico Reyes")  # someone else claimed it in between
    done = _confirm(p, "yes")
    assert done["done"] and "wasn't saved" in done["nickname"]
    assert nicknames.load() == {"danny": "Nico Reyes"}


def test_a_nickname_never_puts_anyone_on_the_list(allowed, messages_app):
    p = _preview(instruction="Text Sam-o see you", person="Sam Hollis", text="See you", said_as="Sam-o")
    _confirm(p, "yes")
    assert "Sam Hollis" not in json.dumps(call("allow_list"))
    threads = call("whats_new", {"hours": 48})["threads"]
    assert "Sam Hollis" not in {t.get("person") for t in threads}  # his direct thread isn't swept


# ─── "Me" ───────────────────────────────────────────────────────────────────


def test_text_me(allowed, messages_app):
    allowed.write_settings({**ALLOWED, "owner_numbers": ["+1 312 555 0101"]})
    p = _preview(instruction="text me the gate code reminder", person="me", text="Gate code is with Dana")
    assert not p.get("blocked") and p["to"] == "Robin"
    blocked = _preview(instruction="text the gate reminder", person="me", text="x")
    assert blocked["blocked"]


def test_text_me_needs_the_owners_number(allowed):
    with pytest.raises(TextsError, match="own number isn't set"):
        _preview(instruction="text me", person="me", text="x")


# ─── Carrying on a conversation ─────────────────────────────────────────────


def test_carrying_on_the_thread_just_read(allowed, messages_app):
    assert _preview(instruction="tell him yes", person="Nico Reyes", text="Yes")["blocked"]
    call("read_texts", {"person": "Nico Reyes"})
    p = _preview(instruction="tell him yes", person="Nico Reyes", text="Yes")
    assert not p.get("blocked") and p["read_back_required"]
    assert "carries on the conversation with Nico Reyes" in " ".join(p["flags"])
    other = _preview(instruction="tell him yes", person="Dana Northwind", text="Yes")
    assert other["blocked"], "carrying on means the same thread, not anyone"


def test_carrying_on_after_a_send_survives_a_restart(allowed, messages_app):
    _confirm(_preview(instruction="Text Nico the truck is late", person="Nico Reyes", text="Truck is late"), "yes")
    sending.forget_reads()  # a new server process remembers only its log
    p = _preview(instruction="and tell him sorry", person="Nico Reyes", text="Sorry")
    assert not p.get("blocked") and p["read_back_required"]


def test_carrying_on_ends_after_twelve_hours(allowed, messages_app, monkeypatch):
    call("read_texts", {"person": "Nico Reyes"})
    old = sending.Recent("person:nico reyes", "Nico Reyes", datetime.now(timezone.utc) - timedelta(hours=13))
    monkeypatch.setitem(sending._LAST_READ, "last", old)
    assert _preview(instruction="tell him yes", person="Nico Reyes", text="Yes")["blocked"]


# ─── What can't go out ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [FAKE_PASSWORD_TEXT, "my SSN is 123-45-6789", "key sk-ant-abc12345678"])
def test_secrets_are_refused(allowed, messages_app, text):
    p = _preview(instruction="Text Dana this", person="Dana Northwind", text=text)
    assert p["blocked"] and "Secrets never go out by text" in " ".join(p["reasons"])


def test_nothing_to_send(allowed):
    assert _preview(instruction="Text Dana", person="Dana Northwind", text=" ")["blocked"]


def test_an_opted_out_person_is_never_texted(allowed, messages_app):
    allowed.write_settings({**ALLOWED, "opted_out": ["Dana Northwind"]})
    p = _preview(instruction="Text Dana hi", person="Dana Northwind", text="hi")
    assert p["blocked"] and p["status"] == "opted_out"


def test_files_from_private_folders_are_refused(allowed):
    login_store = allowed.home / "Library" / "Keychains" / "login.keychain-db"
    login_store.parent.mkdir(parents=True)
    login_store.write_bytes(b"x")
    with pytest.raises(TextsError, match="private or system folder"):
        _preview(instruction="Text Dana this", person="Dana Northwind", text="x", attachments=[str(login_store)])


# ─── How it goes out ────────────────────────────────────────────────────────


def test_the_script_goes_on_standard_input_never_the_command_line(allowed, messages_app):
    words = 'He said "go" \\ then left\nSecond line'
    _confirm(_preview(instruction="Text Dana this", person="Dana Northwind", text=words))
    assert messages_app.argv == [[sending.OSASCRIPT, "-"]]
    script = messages_app.scripts[0]
    assert '"He said \\"go\\" \\\\ then left" & linefeed & "Second line"' in script
    assert not any("go" in part for part in messages_app.argv[0])
    assert 'tell application "Messages"' in script and 'chat id "iMessage;-;+13125550101"' in script


def test_files_are_copied_where_messages_can_read_them_first(allowed, messages_app):
    photo = allowed.home / "Desktop" / "site.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"jpeg")
    p = _preview(instruction="Text Dana the site photo", person="Dana Northwind", text="Here", attachments=[str(photo)])
    assert p["read_back_required"] and p["attachments"] == ["site.jpg"]
    _confirm(p, "yes")
    script = messages_app.scripts[0]
    copied = list((allowed.home / "Pictures" / "robin-texts" / "outgoing").rglob("site.jpg"))
    assert len(copied) == 1 and copied[0].read_bytes() == b"jpeg"
    assert f'set theFile0 to POSIX file "{copied[0]}"' in script
    assert script.index("POSIX file") < script.index('tell application "Messages"'), "file references before the tell block"
    assert str(photo) not in script


def test_a_new_thread_starts_by_imessage(allowed, messages_app):
    p = _preview(instruction="Text Juniper and Vine we're on the way", person="Juniper & Vine", text="On the way")
    assert "starts a new one" in " ".join(p["flags"]) and p["read_back_required"]
    _confirm(p, "yes")
    assert 'participant "+13125550105" of theService' in messages_app.scripts[0]


def test_delivery_failures_are_reported(allowed, messages_app):
    messages_app.state.update(delivered=0, error=22)
    done = _confirm(_preview(instruction="Text Dana hi", person="Dana Northwind", text="hi"))
    assert "failed (error 22)" in done["delivery"]


def test_no_permission_to_control_messages_is_said_plainly(allowed, messages_app):
    messages_app.state["fail"] = "execution error: Not authorized to send Apple events to Messages. (-1743)"
    with pytest.raises(TextsError, match="Automation"):
        _confirm(_preview(instruction="Text Dana hi", person="Dana Northwind", text="hi"))
    assert _log(allowed)[-1]["event"] == "failed"


def test_the_sent_record_keeps_only_the_start(allowed, messages_app):
    long = "word " * 200
    _confirm(_preview(instruction="Text Dana this", person="Dana Northwind", text=long))
    row = _log(allowed)[-1]
    assert len(row["preview"]) == 200 and long not in json.dumps(row)


# ─── The server and the read-only door ──────────────────────────────────────


def test_the_remote_door_has_no_send_tools_and_reaches_the_list_only(allowed):
    import asyncio

    from mcp.types import ListToolsRequest

    srv = server.build_server(remote=True)
    listed = asyncio.run(srv.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list")))
    names = {t.name for t in listed.root.tools}
    assert names == {s["name"] for s in tools.READ_TOOLS}
    assert "can't send" in srv.instructions
    assert call("read_texts", {"person": "Sam Hollis"}, remote=True)["status"] == "not_available_here"


def test_the_shared_nickname_file_holds_names_only(allowed, messages_app):
    _confirm(_preview(instruction="Text Danny hi", person="Dana Northwind", text="hi", said_as="Danny"), "yes")
    text = nicknames.path().read_text()
    assert text.startswith("# Nicknames you use for people.")
    assert yaml.safe_load(text) == {"nicknames": {"Danny": "Dana Northwind"}}
    assert "555" not in text and "@" not in text
