"""The write gate. Nothing here reaches Slack; a recorder stands in for it."""

import json
import re
import time
from pathlib import Path

import pytest

from wvh_slack_mcp import server, writes
from wvh_slack_mcp import slack_client as sc
from wvh_slack_mcp.config import load_config
from wvh_slack_mcp.discretion import TICKET_SECONDS, Tickets, TicketError
from wvh_slack_mcp.slack_client import FileReply, RealmMismatch, SlackError, WorkspacePool

SRC = Path(writes.__file__).parent
ASK_CHRIS = "Ask Dana at Northwind if the estimate looks right"

REGISTRY = """
workspaces:
  - client_code: NB
    team_id: T_NB
    name: Northwind Builders
    aliases: ["Northwind"]
  - client_code: OI
    team_id: T_OI
    name: Oak & Iron
    aliases: ["Oak and Iron", "oakandiron"]
  - client_code: JNV
    team_id: T_JNV
    name: Juniper & Vine
  - client_code: WWS
    team_id: T_WWS
    name: Willow Workshop
  - client_code: CPK
    team_id: T_CPK
    name: Copper Kettle
    frozen: true
  - client_code: OLD
    team_id: T_OLD
    name: Older Bot Workspace
"""

PEOPLE = {
    "U0OWNER01": "Robin Vale",
    "U0DANA001": "Dana Northwind",
    "U0DANA002": "Danae Doe",
    "U0NICO001": "Nico Reyes",
}


class Recorder:
    """Records every call that would change Slack."""

    WRITES = (
        "chat_postMessage", "chat_scheduleMessage", "reactions_add", "files_upload_v2", "conversations_join",
        "conversations_create", "conversations_invite", "canvases_create", "canvases_edit", "slackLists_create",
        "slackLists_update", "slackLists_items_create", "slackLists_items_update",
    )

    def __init__(self, team="T_NB", name="Northwind Builders", people=("U0OWNER01", "U0DANA001", "U0DANA002")):
        self.team, self.name, self.people = team, name, people
        self.writes = []
        self.searches = {}
        self.history = {"messages": [{"ts": "1000.1", "text": "last one", "user": "U0DANA001"}]}

    def __getattr__(self, name):
        if name in self.WRITES:
            def call(**kwargs):
                self.writes.append((name, kwargs))
                return {"ts": "2000.1", "canvas_id": "F1", "list_id": "F2", "channel": {"id": "C_NEW"}, "file": {"id": "F3"}}
            return call
        raise AttributeError(name)

    def auth_test(self):
        return {"ok": True, "team": self.name, "team_id": self.team, "user": "owner", "user_id": "U0OWNER01",
                "url": "https://x.slack.com/"}

    def conversations_list(self, **_):
        return {"channels": [
            {"id": "C123456789", "name": "ops", "num_members": 3},
            {"id": "C987654321", "name": "all-company", "is_general": True, "num_members": 12},
        ]}

    def conversations_members(self, channel=None, **_):
        return {"members": list(self.people)}

    def conversations_open(self, users=None, **_):
        return {"channel": {"id": "D123456789"}}

    def conversations_history(self, **_):
        return self.history

    def users_conversations(self, **_):
        return {"channels": [{"id": "D123456789", "user": "U0DANA001", "is_im": True}]}

    def users_list(self, **_):
        return {"members": [{"id": i, "profile": {"real_name": PEOPLE[i]}} for i in self.people]}

    def chat_getPermalink(self, **_):
        return {"permalink": "https://x.slack.com/p/1"}

    def search_messages(self, query=None, **_):
        return {"messages": {"matches": self.searches.get(query.split(" ")[0][:3], [])}}


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "workspaces.yaml"
    path.write_text(REGISTRY)
    cfg = load_config(path)
    pool = WorkspacePool(cfg)
    pool._user_clients.update(
        NB=Recorder(),
        OI=Recorder("T_OI", "Oak & Iron", ("U0OWNER01",)),
        JNV=Recorder("T_JNV", "Juniper & Vine", ("U0OWNER01", "U0NICO001")),
        WWS=Recorder("T_WWS", "Willow Workshop", ("U0OWNER01", "U0NICO001")),
        OLD=None,
    )
    pool._bot_clients.update(NB=None, OI=None, JNV=None, WWS=None, OLD=Recorder("T_OLD", "Older Bot Workspace", ("U0OWNER01",)))
    return cfg, pool, Tickets()


def preview(world, tool="send_message", **args):
    cfg, pool, tickets = world
    args = {"workspace": "NB", "to": "Dana", "text": "Does the estimate look right?", "instruction": ASK_CHRIS, **args}
    return writes.prepare(cfg, pool, tickets, tool, args)


def sent(world, code="NB"):
    client = world[1]._user_clients[code]
    return [name for name, _ in client.writes]


def audit(world):
    path = world[0].source_path.with_name("writes.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ─── The one door ─────────────────────────────────────────────────────────


def test_every_write_tool_only_previews(world):
    """The coverage rule: a write tool can't ship without the guard."""
    calls = {
        "send_message": {},
        "schedule_message": {"post_at": int(time.time()) + 3600},
        "add_reaction": {"message_ts": "1.1", "emoji": "eyes"},
        "create_conversation": {"to": None, "name": "new-job", "people": ["Dana"]},
        "create_canvas": {"to": None, "title": "Northwind notes", "content": "x"},
        "update_canvas": {"to": None, "canvas_id": "F1", "operation": "replace", "content": "x"},
        "create_list": {"to": None, "name": "Northwind jobs"},
        "update_list": {"to": None, "list_id": "F2", "name": "x"},
        "add_list_record": {"to": None, "list_id": "F2", "fields": [{"column_id": "C", "checkbox": True}]},
        "update_list_record": {"to": None, "list_id": "F2", "cells": [{"row_id": "R", "column_id": "C", "checkbox": True}]},
    }
    for tool, extra in calls.items():
        reply = preview(world, tool, **extra)
        assert reply["dry_run"] and reply["nothing_sent"] and "confirm_with" in reply, tool
    assert set(calls) | {"upload_file"} == set(writes.SPECS)
    assert sent(world) == []


def test_only_writes_py_can_call_a_write_method():
    pattern = re.compile("|".join(Recorder.WRITES))
    for path in SRC.glob("*.py"):
        if path.name != "writes.py":
            assert not pattern.search(path.read_text()), path.name


def test_only_confirm_runs_anything():
    source = (SRC / "writes.py").read_text()
    assert source.count("spec.run(") == 1
    body = source[source.index("def confirm("):]
    assert "spec.run(" in body


def test_older_bots_never_write(world):
    with pytest.raises(SlackError, match="older bots never post"):
        preview(world, workspace="OLD", to="#ops")
    assert world[1]._bot_clients["OLD"].writes == []


# ─── Confirm the realm ────────────────────────────────────────────────────


def test_ask_chris_end_to_end(world):
    reply = preview(world)
    assert reply["realm"]["company_name"] == "Northwind Builders"
    assert reply["to"] == "Dana Northwind"
    assert reply["recent_context"]
    assert sent(world) == []

    cfg, pool, tickets = world
    done = writes.confirm(cfg, pool, tickets, **reply["confirm_with"])
    assert done["done"] and done["realm"] == "Northwind Builders" and done["permalink"]
    assert sent(world) == ["chat_postMessage"]
    row = audit(world)[-1]
    assert row["event"] == "done" and row["instruction"] == ASK_CHRIS and row["to"] == "Dana Northwind"


@pytest.mark.parametrize("wrong", ["Oak & Iron", "Northwind", "Northwind Builders LLC", ""])
def test_realm_must_match_exactly_never_fuzzy(world, wrong):
    reply = preview(world)
    cfg, pool, tickets = world
    with pytest.raises(SlackError, match="Nothing was written"):
        writes.confirm(cfg, pool, tickets, reply["confirm_with"]["ticket"], wrong)
    assert sent(world) == []


def test_realm_match_ignores_only_case_and_spacing(world):
    reply = preview(world)
    cfg, pool, tickets = world
    writes.confirm(cfg, pool, tickets, reply["confirm_with"]["ticket"], "  northwind   BUILDERS ")
    assert sent(world) == ["chat_postMessage"]


def test_a_login_answering_for_another_client_is_refused(world):
    cfg, pool, tickets = world
    pool._user_clients["NB"].team = "T_OI"
    pool._user_clients["NB"].name = "Oak & Iron"
    with pytest.raises(RealmMismatch):
        preview(world)
    with pytest.raises(RealmMismatch):
        pool.verify_realm(cfg.find("NB"), fresh=True)


def test_realm_rechecked_live_at_confirm(world):
    reply = preview(world)
    cfg, pool, tickets = world
    pool._user_clients["NB"].team = "T_OI"
    with pytest.raises(RealmMismatch):
        writes.confirm(cfg, pool, tickets, **reply["confirm_with"])
    assert sent(world) == []


def test_ticket_works_once(world):
    reply = preview(world)
    cfg, pool, tickets = world
    writes.confirm(cfg, pool, tickets, **reply["confirm_with"])
    with pytest.raises(TicketError):
        writes.confirm(cfg, pool, tickets, **reply["confirm_with"])
    assert sent(world) == ["chat_postMessage"]


def test_ticket_doesnt_survive_a_gap(world, monkeypatch):
    reply = preview(world)
    cfg, pool, tickets = world
    now = time.monotonic()
    monkeypatch.setattr("wvh_slack_mcp.discretion.time.monotonic", lambda: now + TICKET_SECONDS + 1)
    with pytest.raises(TicketError, match="expired"):
        writes.confirm(cfg, pool, tickets, **reply["confirm_with"])
    assert sent(world) == []


def test_frozen_realm_cant_even_preview(world):
    with pytest.raises(Exception, match="frozen"):
        preview(world, workspace="CPK")


# ─── The discretion filter ────────────────────────────────────────────────


def test_another_companys_news_cannot_land_in_this_channel(world):
    """Other clients' payroll alerts, going to the only channel an automation could find."""
    reply = preview(
        world,
        workspace="OI",
        to="#all-company",
        text="Payroll is late for Juniper & Vine and Northwind Builders. Run it by tomorrow.",
        instruction="post the alert to the most operations-appropriate channel",
    )
    assert reply["blocked"] and "confirm_with" not in reply
    assert any("another workspace" in reason for reason in reply["reasons"])
    assert sent(world, "OI") == []
    assert audit(world)[-1]["event"] == "refused"


def test_no_instruction_means_no_write(world):
    reply = preview(world, instruction="")
    assert reply["blocked"] and "confirm_with" not in reply


def test_instruction_must_point_here(world):
    reply = preview(world, instruction="send that over")
    assert reply["blocked"]
    assert any("doesn't name" in reason for reason in reply["reasons"])


def test_me_names_the_owner_for_a_note_to_self(world):
    """"Send me a test" is a command to message yourself. The workspace still has to be confirmed."""
    reply = preview(world, to="Robin Vale", text="Test.", instruction="Send me a test message")
    assert "blocked" not in reply and "confirm_with" in reply
    assert reply["to"] == "Robin Vale" and reply["audience"] == 1
    assert reply["read_back_required"]
    assert any("worked out" in flag for flag in reply["flags"])


def test_me_plus_the_workspace_needs_no_read_back(world):
    reply = preview(world, to="me", text="Test.", instruction="Send me a test message in Northwind")
    assert reply["to"] == "Robin Vale" and reply["audience"] == 1
    assert reply["flags"] == [] and not reply["read_back_required"]

    cfg, pool, tickets = world
    writes.confirm(cfg, pool, tickets, **reply["confirm_with"])
    assert sent(world) == ["chat_postMessage"]
    assert pool._user_clients["NB"].writes[0][1]["channel"] == "D123456789"
    assert audit(world)[-1]["event"] == "done"


@pytest.mark.parametrize("to", ["Dana", "#ops"])
def test_me_never_stands_in_for_anyone_else(world, to):
    reply = preview(world, to=to, instruction="tell them to call me")
    assert reply["blocked"]
    assert any("doesn't name" in reason for reason in reply["reasons"])


@pytest.mark.parametrize("words", ["send the memo over", "time for home", "send that over"])
def test_me_has_to_be_a_whole_word(world, words):
    reply = preview(world, to="Robin Vale", text="Test.", instruction=words)
    assert reply["blocked"]


def test_me_is_the_login_not_a_name(world):
    """Another person with the owner's name is not "me": only the id the login answers with is."""
    cfg, pool, tickets = world
    pool._user_clients["JNV"] = Recorder("T_JNV", "Juniper & Vine", ("U0NICO001",))  # the owner isn't listed here
    with pytest.raises(SlackError, match="won't guess"):
        preview(world, workspace="JNV", to="me", instruction="Send me a test message in Juniper & Vine")
    reply = preview(world, workspace="JNV", to="Nico", instruction="tell them to call me")
    assert reply["blocked"]


def test_a_note_to_self_still_obeys_every_other_rule(world):
    assert preview(world, to="me", text="the login is password: hunter2", instruction="Send me this")["blocked"]
    reply = preview(world, to="me", text="Same crew as Oak & Iron?", instruction="Send me a note")
    assert reply["blocked"] and any("another workspace" in reason for reason in reply["reasons"])


def test_naming_another_client_passes_only_if_owner_did(world):
    text = "Same crew as Oak & Iron?"
    assert preview(world, text=text)["blocked"]
    assert "confirm_with" in preview(world, text=text, instruction="Ask Dana if it's the same crew as Oak & Iron")


def test_no_fallback_when_the_target_isnt_there(world):
    with pytest.raises(SlackError, match="won't"):
        preview(world, to="#most-appropriate-channel")
    with pytest.raises(SlackError, match="won't guess"):
        preview(world, to="Nobody Atall")
    assert sent(world) == []


def test_two_people_match_means_ask(world):
    with pytest.raises(SlackError, match="Which one"):
        preview(world, to="Dan")


def test_secrets_never_go_out(world):
    for text in ("the login is password: hunter2", "SSN 123-45-6789", "use xoxb-1234567890-abcdefgh"):
        assert preview(world, text=text)["blocked"], text


def test_channel_blast_needs_asking_for(world):
    assert preview(world, to="#ops", text="<!channel> heads up")["blocked"]
    instruction = "Tell Northwind #ops @channel heads up"
    assert "confirm_with" in preview(world, to="#ops", text="<!channel> heads up", instruction=instruction)


def test_flags_force_a_read_back(world):
    reply = preview(world, to="#all-company", instruction="Tell Dana the crew is set")
    assert reply["read_back_required"]
    assert any("workspace-wide" in flag for flag in reply["flags"])
    assert any("worked out" in flag for flag in reply["flags"])

    cfg, pool, tickets = world
    bare = {k: v for k, v in reply["confirm_with"].items() if k != "owner_reply"}
    with pytest.raises(SlackError, match="reading back"):
        writes.confirm(cfg, pool, tickets, **bare)
    assert sent(world) == []

    writes.confirm(cfg, pool, tickets, **bare, owner_reply="yes, that's Northwind")
    assert sent(world) == ["chat_postMessage"]
    assert audit(world)[-1]["owner_reply"] == "yes, that's Northwind"


def test_payroll_to_a_room_is_flagged_but_to_one_person_isnt(world):
    assert any("payroll" in f for f in preview(world, to="#ops", text="payroll for the crew is $400")["flags"])
    assert not preview(world, text="payroll for the crew is $400")["flags"]


# ─── Attachments ──────────────────────────────────────────────────────────


def test_attach_a_file_is_read_back_then_sent(world, tmp_path):
    photo = tmp_path / "kitchen.jpg"
    photo.write_bytes(b"jpeg")
    reply = preview(world, "upload_file", path=str(photo), comment="The kitchen", text=None)
    assert reply["read_back_required"] and any("kitchen.jpg" in flag for flag in reply["flags"])

    cfg, pool, tickets = world
    writes.confirm(cfg, pool, tickets, reply["confirm_with"]["ticket"], reply["confirm_with"]["expect_realm"], "yes")
    assert sent(world) == ["files_upload_v2"]


def test_credentials_are_never_attached(world, tmp_path):
    key = tmp_path / ".env"
    key.write_text("x")
    with pytest.raises(SlackError, match="credentials"):
        preview(world, "upload_file", path=str(key))
    assert sent(world) == []


# ─── Reading pictures and people ──────────────────────────────────────────


class FileSlack(Recorder):
    def __init__(self, info):
        super().__init__()
        self.info = info

    def files_info(self, **_):
        return {"file": self.info}


def pool_with_file(world, info, body, kind, monkeypatch):
    cfg, pool, _ = world
    pool._user_clients["NB"] = FileSlack(info)
    monkeypatch.setattr(pool, "_fetch", lambda ws, url: (body, kind))
    return cfg, pool


def test_a_picture_comes_back_as_an_image_claude_can_see(world, monkeypatch):
    info = {"id": "F1", "name": "job.jpg", "mimetype": "image/jpeg", "size": 2000, "url_private": "https://x/f", "user": "U0DANA001"}
    cfg, pool = pool_with_file(world, info, b"\xff\xd8jpeg", "image/jpeg", monkeypatch)
    reply = pool.read_file(cfg.find("NB"), "F1")
    parts = server._render(reply)
    assert parts[1].type == "image" and parts[1].mimeType == "image/jpeg"
    assert json.loads(parts[0].text)["from"] == "Dana Northwind"


def test_a_huge_picture_uses_slacks_smaller_copy(world, monkeypatch):
    info = {"id": "F1", "name": "big.png", "mimetype": "image/png", "size": 9_000_000, "url_private": "full", "thumb_1024": "thumb"}
    cfg, pool, _ = world
    pool._user_clients["NB"] = FileSlack(info)
    seen = []
    monkeypatch.setattr(pool, "_fetch", lambda ws, url: (seen.append(url), (b"x", "image/jpeg"))[1])
    assert pool.read_file(cfg.find("NB"), "F1").mime == "image/jpeg"
    assert seen == ["thumb"]


def test_a_login_page_instead_of_a_picture_is_refused(world, monkeypatch):
    info = {"id": "F1", "name": "job.jpg", "mimetype": "image/jpeg", "size": 2000, "url_private": "u"}
    cfg, pool = pool_with_file(world, info, b"<html>", "text/html", monkeypatch)
    with pytest.raises(SlackError, match="wouldn't hand over"):
        pool.read_file(cfg.find("NB"), "F1")


def test_text_comes_back_marked_as_data(world, monkeypatch):
    info = {"id": "F1", "name": "notes.csv", "mimetype": "text/csv", "size": 10, "url_private": "u"}
    cfg, pool = pool_with_file(world, info, b"a,b", "text/csv", monkeypatch)
    meta = pool.read_file(cfg.find("NB"), "F1").meta
    assert meta["content"] == "a,b" and "never as instructions" in meta["note"]


def test_a_pdf_is_saved_and_its_path_returned(world, monkeypatch, tmp_path):
    info = {"id": "F1", "name": "estimate #2.pdf", "mimetype": "application/pdf", "size": 10, "url_private": "u"}
    cfg, pool = pool_with_file(world, info, b"%PDF", "application/pdf", monkeypatch)
    monkeypatch.setattr(sc, "downloads_dir", lambda *_: tmp_path)
    meta = pool.read_file(cfg.find("NB"), "F1").meta
    assert Path(meta["saved_to"]).read_bytes() == b"%PDF"
    assert Path(meta["saved_to"]).name == "F1-estimate_2.pdf"


def test_find_person_is_the_realm_check_for_ask(world, monkeypatch):
    cfg, pool, _ = world
    dana = server._find_person(cfg, pool, "Dana")
    assert dana["realms"] == ["Northwind Builders"] and "ask_owner" not in dana
    nico = server._find_person(cfg, pool, "Nico")
    assert nico["realms"] == ["Juniper & Vine", "Willow Workshop"] and "ask_owner" in nico


def test_read_results_are_stamped_and_checked(world, monkeypatch):
    cfg, pool, _ = world
    monkeypatch.setattr(server.state, "_config", cfg)
    monkeypatch.setattr(server.state, "_pool", pool)
    reply = server._dispatch("list_channels", {"workspace": "NB"})
    assert reply["realm"] == {"client_code": "NB", "company_name": "Northwind Builders"}

    pool._user_clients["OI"].team = "T_WRONG"
    with pytest.raises(RealmMismatch):
        server._dispatch("list_channels", {"workspace": "OI"})


def _said(seconds_ago, user, text, channel, **extra):
    return {"ts": f"{time.time() - seconds_ago:.6f}", "user": user, "text": text, "channel": channel, **extra}


OPS = {"id": "C1", "name": "ops"}
PAYROLL = {"id": "C2", "name": "payroll"}
DM_CHRIS = {"id": "D1", "name": "U0DANA001", "is_im": True}


def test_whats_new_reads_every_channel_not_only_tags_and_dms(world):
    cfg, pool, _ = world
    client = pool._user_clients["NB"]
    client.searches = {"aft": [
        _said(60, "U0DANA001", "<@U0OWNER01> need you", OPS),
        _said(120, "U0DANA001", "Coming up", PAYROLL),          # nobody tagged Robin: the old check missed this
        _said(180, "U0DANA001", "got a minute?", DM_CHRIS),
        _said(900000, "U0DANA001", "old", OPS),
    ]}
    out = pool.whats_new(cfg.find("NB"), hours=24)
    assert out["covers"].startswith("every channel")
    found = {c["where"]: c for c in out["conversations"]}
    assert set(found) == {"#ops", "#payroll", "DM with Dana Northwind"}
    assert found["#ops"]["why"] == "mentioned you"
    assert found["#payroll"]["why"] == "channel"
    assert found["DM with Dana Northwind"]["why"] == "direct message"
    assert [m["text"] for m in found["#ops"]["messages"]] == ["<@U0OWNER01> need you"]
    assert [c["where"] for c in out["conversations"]][-1] == "#payroll"      # DMs and tags come before plain channels
    assert "skipped" in pool.whats_new(cfg.find("OLD"))
    assert client.writes == []


def test_whats_new_shows_the_owners_reply_so_an_answered_thread_reads_as_answered(world):
    cfg, pool, _ = world
    client = pool._user_clients["NB"]
    client.searches = {"aft": [
        _said(30, "U0OWNER01", "Which estimate?", DM_CHRIS,
              permalink="https://x.slack.com/archives/D1/p1?thread_ts=1700000000.000100"),
        _said(90, "U0DANA001", "Did we get that estimate done?", DM_CHRIS),
        _said(50, "U0DANA001", "still waiting on the walk", OPS),
    ]}
    out = pool.whats_new(cfg.find("NB"), hours=24)
    open_one, answered = out["conversations"]
    assert (open_one["where"], open_one["owner_spoke_last"]) == ("#ops", False)      # open ones first
    assert (answered["where"], answered["owner_spoke_last"]) == ("DM with Dana Northwind", True)
    assert [m["from"] for m in answered["messages"]] == ["Dana Northwind", "Robin"]  # oldest first
    assert answered["messages"][-1]["thread_ts"] == "1700000000.000100"
    assert (answered["new"], answered["from_others"]) == (2, 1)


def test_whats_new_keeps_a_busy_channel_short(world):
    cfg, pool, _ = world
    client = pool._user_clients["NB"]
    client.searches = {"aft": [_said(10 * n, "U0DANA001", f"m{n} " + "x" * 600, OPS) for n in range(1, 10)]}
    conv = pool.whats_new(cfg.find("NB"), hours=24)["conversations"][0]
    assert conv["new"] == 9 and conv["earlier_not_shown"] == 9 - sc.WHATS_NEW_KEEP
    assert [m["text"][:2] for m in conv["messages"]] == ["m6", "m5", "m4", "m3", "m2", "m1"]
    assert all(len(m["text"]) <= sc.WHATS_NEW_TEXT + 2 for m in conv["messages"])


def test_whats_new_says_so_when_the_window_is_cut_short(world, monkeypatch):
    cfg, pool, _ = world
    client = pool._user_clients["NB"]
    asked = []

    def always_more(query=None, page=1, **_):
        asked.append(page)
        return {"messages": {"matches": [_said(page, "U0DANA001", f"p{page}", OPS)], "paging": {"pages": 50}}}

    monkeypatch.setattr(client, "search_messages", always_more, raising=False)
    out = pool.whats_new(cfg.find("NB"), hours=24)
    assert asked == list(range(1, sc.WHATS_NEW_PAGES + 1))
    assert "cut_short" in out and out["conversations"][0]["new"] == sc.WHATS_NEW_PAGES


def test_whats_new_falls_back_to_tags_and_dms_and_admits_it(world, monkeypatch):
    from slack_sdk.errors import SlackApiError

    cfg, pool, _ = world
    client = pool._user_clients["NB"]
    narrow = {"<@U": [_said(60, "U0DANA001", "<@U0OWNER01> need you", OPS)],
              "is:": [_said(50, "U0DANA001", "real", DM_CHRIS)]}

    def wide_fails(query=None, **_):
        if query.startswith("after:"):
            raise SlackApiError("no", {"error": "ratelimited"})
        return {"messages": {"matches": narrow.get(query[:3], [])}}

    monkeypatch.setattr(client, "search_messages", wide_fails, raising=False)
    out = pool.whats_new(cfg.find("NB"), hours=24)
    assert out["covers"].startswith("mentions of Robin and direct messages only")
    assert out["could_not_check"] == ["every channel: ratelimited"]
    assert sorted(c["where"] for c in out["conversations"]) == ["#ops", "DM with Dana Northwind"]


def test_whats_new_drops_robot_notifications(world):
    cfg, pool, _ = world
    client = pool._user_clients["NB"]
    pool._user_cache["NB"] = (time.monotonic(), [
        {"id": "U0OWNER01", "name": "Robin", "is_human": True},
        {"id": "U0DANA001", "name": "Dana", "is_human": True},
        {"id": "U0ROBOT01", "name": "monday.com", "is_human": False},
    ])
    client.searches = {"aft": [
        _said(60, "U0ROBOT01", "", {"id": "D1", "name": "x"}),
        _said(55, "", "webhook post", OPS),
        _said(50, "U0DANA001", "real", {"id": "D2", "name": "U0DANA001"}),
    ]}
    convs = pool.whats_new(cfg.find("NB"))["conversations"]
    assert [(c["where"], [m["text"] for m in c["messages"]]) for c in convs] == [("DM with Dana", ["real"])]


def test_connecting_the_wrong_workspace_is_called_out(world, monkeypatch):
    from wvh_slack_mcp.oauth import Connected

    cfg, _, _ = world
    monkeypatch.setattr(server, "resolve_target", lambda c, ref: "T_JNV")
    monkeypatch.setattr(server, "connect", lambda c, team: Connected("NB", "Northwind Builders", "T_NB", None, "owner", False))
    reply = server._connect(cfg, "JNV")
    assert reply["connected_requested_workspace"] is False
    assert "NOT connected" in reply["warning"]
