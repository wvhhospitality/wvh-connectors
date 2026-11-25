"""Nicknames and follow-ups: smarter about who was meant, with every guardrail kept.

Every name here is made up. Nothing reaches Slack; the recorder from test_writes stands in.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from wvh_slack_mcp import nicknames, server, writes
from tests.test_writes import ASK_CHRIS, audit, preview, sent, world  # noqa: F401  (fixtures)


def _nicks(**table):
    path = nicknames.path()
    path.write_text(yaml.safe_dump({"nicknames": table}))


def _confirm(world, reply, yes="yes, send it"):
    cfg, pool, tickets = world
    return writes.confirm(cfg, pool, tickets, reply["confirm_with"]["ticket"],
                          reply["confirm_with"]["expect_realm"], owner_reply=yes)


# ─── Nicknames ────────────────────────────────────────────────────────────


def test_a_known_nickname_names_the_person(world):
    _nicks(Danny="Dana Northwind")
    reply = preview(world, instruction="Ask Danny at Northwind if the estimate looks right")
    assert not reply.get("blocked") and "confirm_with" in reply


def test_a_new_nickname_is_read_back_then_remembered_only_after_yes(world):
    reply = preview(world, instruction="Slack Danny that the estimate looks right", said_as="Danny")
    assert not reply.get("blocked") and reply["read_back_required"]
    assert any("Danny" in f and "teaches" in f for f in reply["flags"])
    assert nicknames.lookup("Danny") is None, "nothing is learned from a preview alone"
    done = _confirm(world, reply)
    assert done["done"] and "Danny means Dana Northwind" in done["nickname"]
    assert nicknames.lookup("danny") == "Dana Northwind"


def test_a_learned_nickname_works_next_time_without_said_as(world):
    reply = preview(world, instruction="Slack Danny at Northwind the estimate", said_as="Danny")
    _confirm(world, reply)
    again = preview(world, instruction="Ask Danny at Northwind when the crew starts", text="When does the crew start?")
    assert not again.get("blocked")


def test_said_as_has_to_be_the_owners_own_word(world):
    reply = preview(world, instruction="Ask about the estimate", said_as="Danny")
    assert reply["blocked"] and any("isn't in" in r for r in reply["reasons"])


def test_a_nickname_that_means_someone_else_is_refused(world):
    _nicks(Danny="Nico Reyes")
    reply = preview(world, instruction="Slack Danny the estimate", said_as="Danny")
    assert reply["blocked"] and any("Nico Reyes" in r for r in reply["reasons"])


def test_a_nickname_already_taken_is_never_overwritten(world):
    _nicks(Danny="Nico Reyes")
    assert nicknames.remember("Danny", "Dana Northwind") == "conflict"
    assert nicknames.lookup("Danny") == "Nico Reyes"


def test_nicknames_hold_names_only(world):
    assert nicknames.remember("Danny", "dana@example.com") == "conflict"
    assert nicknames.remember("Danny", "Dana 555 0100") == "conflict"
    assert nicknames.load() == {}


def test_a_nickname_never_skips_any_other_rule(world):
    _nicks(Danny="Dana Northwind")
    assert preview(world, instruction="Slack Danny", text="the login is password: hunter2")["blocked"]
    assert preview(world, instruction="Slack Danny", text="Same crew as Oak & Iron?")["blocked"]
    with pytest.raises(Exception):
        preview(world, workspace="CPK", instruction="Slack Danny")  # a hold stays absolute


def test_find_person_knows_nicknames(world):
    _nicks(Danny="Dana Northwind")
    cfg, pool, _ = world
    found = server._find_person(cfg, pool, "Danny")
    assert any(m["name"] == "Dana Northwind" for m in found["matches"])
    assert "said_as" in found["known_as"]


# ─── Follow-ups in a conversation already going ──────────────────────────


def test_a_follow_up_continues_the_conversation_with_a_read_back(world):
    _confirm(world, preview(world))
    reply = preview(world, instruction="tell her yes", text="Yes.")
    assert not reply.get("blocked") and reply["read_back_required"]
    assert any(f.startswith("Continuing your conversation with Dana Northwind") for f in reply["flags"])


def test_a_follow_up_after_reading_the_conversation(world):
    cfg, pool, _ = world
    target = pool.resolve_target(cfg.find("NB"), "Dana")
    writes.note_read("NB", "Dana", target.id)
    reply = preview(world, instruction="tell her yes", text="Yes.")
    assert not reply.get("blocked") and any("just read" in f for f in reply["flags"])


def test_without_a_conversation_going_a_pronoun_is_still_refused(world):
    assert preview(world, instruction="tell her yes", text="Yes.")["blocked"]


def test_a_follow_up_never_crosses_into_another_workspace(world):
    _confirm(world, preview(world))
    reply = preview(world, workspace="JNV", to="Nico", instruction="tell him yes", text="Yes.")
    assert reply["blocked"]


def test_a_follow_up_goes_stale(world):
    _confirm(world, preview(world))
    path = world[0].source_path.with_name("writes.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(timespec="seconds")
    path.write_text("".join(json.dumps({**r, "at": old}) + "\n" for r in rows))
    assert preview(world, instruction="tell her yes", text="Yes.")["blocked"]


def test_the_record_names_the_conversation(world):
    _confirm(world, preview(world))
    row = audit(world)[-1]
    assert row["event"] == "done" and row["target_id"]
