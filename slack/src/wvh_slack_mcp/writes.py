"""Every change to Slack lives in this file, behind one door.

The rule: preview first, then confirm by repeating the exact company name. Each
write tool below can only preview. `confirm` is the one function that acts, and it
acts only on a ticket from a preview that passed the discretion filter. Writes go
only through the owner's own login, never an older bot.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from slack_sdk.errors import SlackApiError

from . import nicknames
from .config import Config, Workspace, owner_name, resolve_config_path
from .discretion import Pending, Target, Tickets, normalize, review
from .slack_client import SlackError, WorkspacePool, _friendly

_NEVER_UPLOAD = re.compile(r"(^\.env|\.pem$|\.key$|\.p12$|id_rsa|id_ed25519|credentials|\.keychain)", re.I)
MIN_SCHEDULE_SECONDS, MAX_SCHEDULE_SECONDS = 120, 120 * 86400

_TO = {
    "type": "string",
    "description": (
        "Channel (#name or id) or person (name or id). \"me\" is the owner's own account. "
        "Must exist in this workspace. No fallback."
    ),
}


@dataclass(frozen=True)
class Spec:
    verb: str
    description: str
    properties: dict
    required: tuple[str, ...]
    text_keys: tuple[str, ...]
    target: Callable[[WorkspacePool, Workspace, dict], Target | None]
    run: Callable[..., dict]


# ─── Who it reaches ───────────────────────────────────────────────────────


def _to(pool: WorkspacePool, ws: Workspace, args: dict) -> Target:
    return pool.resolve_target(ws, args["to"])


def _object(kind: str, key: str) -> Callable[[WorkspacePool, Workspace, dict], Target]:
    return lambda pool, ws, args: Target("object", args[key], f"{kind} {args[key]}")


def _scheduled(pool: WorkspacePool, ws: Workspace, args: dict) -> Target:
    wait = int(args["post_at"]) - time.time()
    if not MIN_SCHEDULE_SECONDS <= wait <= MAX_SCHEDULE_SECONDS:
        raise SlackError("Slack schedules between 2 minutes and 120 days out.")
    return _to(pool, ws, args)


def _upload(pool: WorkspacePool, ws: Workspace, args: dict) -> Target:
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        raise SlackError(f"No file at {path}.")
    if _NEVER_UPLOAD.search(path.name):
        raise SlackError(f"{path.name} looks like a credentials file. It won't be uploaded.")
    args["_filename"] = path.name
    return _to(pool, ws, args)


def _new_conversation(pool: WorkspacePool, ws: Workspace, args: dict) -> Target:
    people = [pool.resolve_target(ws, person) for person in args.get("people") or []]
    if any(person.kind != "person" for person in people):
        raise SlackError("'people' can only hold people.")
    if not args.get("name") and not people:
        raise SlackError("Give a channel name, or the people for a group message.")
    args["_user_ids"] = [person.id for person in people]
    label = f"new channel #{args['name']}" if args.get("name") else "a new direct message"
    return Target("object", label, label, people=[p.label for p in people], audience=len(people) + 1)


def _new_canvas(pool: WorkspacePool, ws: Workspace, args: dict) -> Target:
    if args.get("to"):
        return _to(pool, ws, args)
    return Target("object", "new canvas", "a new canvas")


def _new_list(pool: WorkspacePool, ws: Workspace, args: dict) -> Target:
    return Target("object", "new list", f"a new list called {args['name']}")


# ─── What it does. Only confirm() reaches these. ──────────────────────────


def _open(client: Any, target: Target) -> str:
    if target.kind == "person":
        return client.conversations_open(users=target.id)["channel"]["id"]
    return target.id


def _link(client: Any, channel: str, ts: str | None) -> str | None:
    try:
        return client.chat_getPermalink(channel=channel, message_ts=ts).get("permalink")
    except SlackApiError:
        return None


def _send(client, pool, ws, args, target) -> dict:
    channel = _open(client, target)
    resp = client.chat_postMessage(
        channel=channel,
        markdown_text=args["text"],
        thread_ts=args.get("thread_ts"),
        reply_broadcast=args.get("reply_broadcast"),
    )
    return {"ts": resp.get("ts"), "permalink": _link(client, channel, resp.get("ts"))}


def _schedule(client, pool, ws, args, target) -> dict:
    resp = client.chat_scheduleMessage(
        channel=_open(client, target),
        post_at=int(args["post_at"]),
        text=args["text"],
        thread_ts=args.get("thread_ts"),
    )
    return {"scheduled_id": resp.get("scheduled_message_id"), "post_at": resp.get("post_at")}


def _react(client, pool, ws, args, target) -> dict:
    client.reactions_add(channel=_open(client, target), name=args["emoji"].strip(":"), timestamp=args["message_ts"])
    return {"reacted": args["emoji"].strip(":")}


def _send_file(client, pool, ws, args, target) -> dict:
    path = Path(args["path"]).expanduser()
    resp = client.files_upload_v2(
        channel=_open(client, target),
        file=str(path),
        filename=path.name,
        title=args.get("title") or path.name,
        initial_comment=args.get("comment"),
        thread_ts=args.get("thread_ts"),
    )
    info = resp.get("file") or {}
    return {"file_id": info.get("id"), "permalink": info.get("permalink")}


def _create_conversation(client, pool, ws, args, target) -> dict:
    ids = args.get("_user_ids") or []
    if not args.get("name"):
        return {"channel_id": client.conversations_open(users=",".join(ids))["channel"]["id"]}
    channel = client.conversations_create(name=args["name"], is_private=bool(args.get("is_private")))["channel"]["id"]
    pool.forget_channels(ws)
    if ids:
        client.conversations_invite(channel=channel, users=",".join(ids))
    return {"channel_id": channel}


def _create_canvas(client, pool, ws, args, target) -> dict:
    extra = {"channel_id": target.id} if target and target.kind == "channel" else {}
    resp = client.canvases_create(
        title=args["title"],
        document_content={"type": "markdown", "markdown": args["content"]},
        **extra,
    )
    return {"canvas_id": resp.get("canvas_id")}


def _update_canvas(client, pool, ws, args, target) -> dict:
    change: dict = {"operation": args["operation"]}
    if args.get("section_id"):
        change["section_id"] = args["section_id"]
    if args.get("content"):
        change["document_content"] = {"type": "markdown", "markdown": args["content"]}
    client.canvases_edit(canvas_id=args["canvas_id"], changes=[change])
    return {"canvas_id": args["canvas_id"]}


def _blocks(text: str | None) -> list | None:
    if not text:
        return None
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}]}]


def _create_list(client, pool, ws, args, target) -> dict:
    resp = client.slackLists_create(
        name=args["name"], description_blocks=_blocks(args.get("description")), schema=args.get("columns")
    )
    return {"list_id": resp.get("list_id")}


def _update_list(client, pool, ws, args, target) -> dict:
    client.slackLists_update(id=args["list_id"], name=args.get("name"), description_blocks=_blocks(args.get("description")))
    return {"list_id": args["list_id"]}


def _add_record(client, pool, ws, args, target) -> dict:
    resp = client.slackLists_items_create(list_id=args["list_id"], initial_fields=args["fields"])
    return {"record": (resp.get("item") or {}).get("id") or resp.get("id")}


def _update_record(client, pool, ws, args, target) -> dict:
    client.slackLists_items_update(list_id=args["list_id"], cells=args["cells"])
    return {"list_id": args["list_id"]}


_THREAD = {"type": "string", "description": "Reply in this thread (the parent message's ts)."}

SPECS: dict[str, Spec] = {
    "send_message": Spec(
        "Send a message", "Preview a message to a channel or person.",
        {"to": _TO, "text": {"type": "string", "description": "The message, in standard markdown."},
         "thread_ts": _THREAD,
         "reply_broadcast": {"type": "boolean", "description": "Also show a thread reply in the channel."}},
        ("to", "text"), ("text",), _to, _send,
    ),
    "schedule_message": Spec(
        "Schedule a message", "Preview a message for later delivery.",
        {"to": _TO, "text": {"type": "string"}, "thread_ts": _THREAD,
         "post_at": {"type": "integer", "description": "Unix time, 2 minutes to 120 days out."}},
        ("to", "text", "post_at"), ("text",), _scheduled, _schedule,
    ),
    "add_reaction": Spec(
        "Add a reaction", "Preview an emoji reaction on a message.",
        {"to": _TO, "message_ts": {"type": "string"}, "emoji": {"type": "string", "description": "Name, no colons."}},
        ("to", "message_ts", "emoji"), (), _to, _react,
    ),
    "upload_file": Spec(
        "Attach a file", "Preview attaching a file from this Mac to a channel or person.",
        {"to": _TO, "path": {"type": "string", "description": "Local file path."},
         "title": {"type": "string"}, "comment": {"type": "string", "description": "Message to go with it."},
         "thread_ts": _THREAD},
        ("to", "path"), ("title", "comment", "_filename"), _upload, _send_file,
    ),
    "create_conversation": Spec(
        "Start a conversation", "Preview creating a channel, or opening a group message.",
        {"name": {"type": "string", "description": "New channel name. Leave out for a group message."},
         "is_private": {"type": "boolean"},
         "people": {"type": "array", "items": {"type": "string"}, "description": "People in this workspace."}},
        (), ("name",), _new_conversation, _create_conversation,
    ),
    "create_canvas": Spec(
        "Create a canvas", "Preview a new canvas. Free Slack plans need `to`, a channel to hold it.",
        {"title": {"type": "string"}, "content": {"type": "string", "description": "Markdown."}, "to": _TO},
        ("title", "content"), ("title", "content"), _new_canvas, _create_canvas,
    ),
    "update_canvas": Spec(
        "Change a canvas", "Preview one edit to a canvas.",
        {"canvas_id": {"type": "string"},
         "operation": {"type": "string", "enum": ["insert_after", "insert_before", "insert_at_start", "insert_at_end", "replace", "delete"]},
         "section_id": {"type": "string", "description": "From read_canvas with `find`."},
         "content": {"type": "string", "description": "Markdown."}},
        ("canvas_id", "operation"), ("content",), _object("canvas", "canvas_id"), _update_canvas,
    ),
    "create_list": Spec(
        "Create a list", "Preview a new Slack list. Paid Slack plans only.",
        {"name": {"type": "string"}, "description": {"type": "string"},
         "columns": {"type": "array", "items": {"type": "object"}, "description": "Slack's schema: key, name, type, is_primary_column."}},
        ("name",), ("name", "description"), _new_list, _create_list,
    ),
    "update_list": Spec(
        "Change a list", "Preview renaming a list or changing its description. Slack's API can't edit columns.",
        {"list_id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}},
        ("list_id",), ("name", "description"), _object("list", "list_id"), _update_list,
    ),
    "add_list_record": Spec(
        "Add a list row", "Preview adding a row. Get column ids from read_list.",
        {"list_id": {"type": "string"},
         "fields": {"type": "array", "items": {"type": "object"}, "description": "Slack's initial_fields: column_id plus a typed value."}},
        ("list_id", "fields"), ("fields",), _object("list", "list_id"), _add_record,
    ),
    "update_list_record": Spec(
        "Change a list row", "Preview changing cells. Get row and column ids from read_list.",
        {"list_id": {"type": "string"},
         "cells": {"type": "array", "items": {"type": "object"}, "description": "Slack's cells: row_id, column_id, typed value."}},
        ("list_id", "cells"), ("cells",), _object("list", "list_id"), _update_record,
    ),
}


# ─── The door ─────────────────────────────────────────────────────────────


def _audit(cfg: Config, event: str, **fields: Any) -> None:
    """Append-only record of every write and refusal. This Mac only."""
    path = (cfg.source_path or resolve_config_path()).with_name("writes.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with path.open("a") as log:
        log.write(json.dumps(row, ensure_ascii=False) + "\n")


FOLLOW_UP_SECONDS = 12 * 3600  # how long a conversation counts as "already going"

# Conversations Claude read in this server process: (client_code, channel ref, channel id, when).
_RECENT_READS: list[tuple[str, str, str, float]] = []


def note_read(client_code: str, channel_ref: str, channel_id: str = "") -> None:
    """Remember that the owner had Claude read this conversation, for "tell him yes" a moment later."""
    _RECENT_READS.append((client_code, channel_ref or "", channel_id or "", time.time()))
    del _RECENT_READS[:-50]


def _same_place(target: Target, ref: str, ref_id: str) -> bool:
    if ref_id and ref_id == target.id:
        return True
    a, b = normalize(ref).lstrip("#@"), normalize(target.label).lstrip("#@")
    return bool(a) and a == b


def _continuing(cfg: Config, ws: Workspace, target: Target | None) -> str:
    """Is this the conversation already going? The owner's last confirmed post there, or a read just now."""
    if target is None or target.kind == "object":
        return ""
    now = time.time()
    path = (cfg.source_path or resolve_config_path()).with_name("writes.jsonl")
    try:
        lines = path.read_text().splitlines()[-500:]
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(row["at"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if now - when > FOLLOW_UP_SECONDS:
            break
        if row.get("event") != "done" or row.get("client_code") != ws.client_code:
            continue
        if _same_place(target, row.get("to") or "", row.get("target_id") or ""):
            local = datetime.fromtimestamp(when).strftime("%-I:%M %p")
            return f"your conversation with {target.label} in {ws.label} (you last wrote at {local})"
    for code, ref, ref_id, when in reversed(_RECENT_READS):
        if now - when > FOLLOW_UP_SECONDS:
            break
        if code == ws.client_code and _same_place(target, ref, ref_id):
            return f"the conversation with {target.label} in {ws.label} that Claude just read"
    return ""


def _content(args: dict, spec: Spec) -> str:
    return " | ".join(str(args[k]) for k in spec.text_keys if args.get(k))


def _context(pool: WorkspacePool, ws: Workspace, target: Target | None) -> list[dict] | None:
    """The last few messages there, so a wrong room is obvious before anything goes out."""
    if target is None or target.kind == "object":
        return None
    try:
        recent = pool.history(ws, target.id, limit=3)
    except SlackError:
        return None
    return [{"from": m.get("user"), "when": m.get("time"), "text": (m.get("text") or "")[:160]} for m in recent]


def prepare(cfg: Config, pool: WorkspacePool, tickets: Tickets, tool: str, raw: dict) -> dict:
    """Preview only. Nothing here touches Slack except to read."""
    spec = SPECS[tool]
    args = {k: v for k, v in raw.items() if k not in ("workspace", "instruction", "said_as")}
    instruction = (raw.get("instruction") or "").strip()
    said_as = (raw.get("said_as") or "").strip()
    ws = cfg.find(raw["workspace"])  # refuses frozen and unconnected workspaces

    try:
        if not ws.team_id:
            raise SlackError(f"[{ws.client_code}] {ws.label} has no verified Slack id, so it can't be written to.")
        if pool.user(ws) is None:
            raise SlackError(
                f"[{ws.client_code}] Writing needs {owner_name()}'s login in {ws.label}, and older bots never post. "
                f"Use connect_workspace, or run: wvh-slack-connect {ws.client_code}"
            )
        live = pool.verify_realm(ws, fresh=True)
        target = spec.target(pool, ws, args)
        if target is not None and target.kind == "person" and target.id == live.get("owner_id"):
            target.to_self = True   # checked against the login's own id, live, not against a name
            target.audience = 1
    except SlackError as exc:
        _audit(cfg, "refused", tool=tool, client_code=ws.client_code, instruction=instruction, reason=str(exc))
        raise

    others = [w for w in cfg.workspaces if w.client_code != ws.client_code]
    texts = [str(args[k]) for k in spec.text_keys if args.get(k)]
    found = review(
        ws=ws, others=others, instruction=instruction, target=target, texts=texts,
        said_as=said_as, continuing=_continuing(cfg, ws, target), nicknames=nicknames.load(),
    )
    if tool == "upload_file":
        found.flags.append(f"The filter can't read inside {args['_filename']}. Confirm it's the right file.")

    company = live["team"]
    base = {
        "dry_run": True,
        "nothing_sent": True,
        "realm": {"client_code": ws.client_code, "company_name": company, "team_id": live["team_id"]},
        "action": spec.verb,
        "to": target.label if target else None,
        "audience": target.audience if target else None,
    }
    if found.blocks:
        _audit(cfg, "refused", tool=tool, client_code=ws.client_code, company=company,
               to=base["to"], instruction=instruction, reason=found.blocks)
        return {**base, "blocked": True, "reasons": found.blocks, "flags": found.flags}

    ticket = tickets.issue(
        Pending(tool, ws.client_code, live["team_id"], args, target, instruction, found.read_back, time.monotonic(),
                remember=found.remember)
    )
    confirm_with: dict[str, Any] = {"ticket": ticket, "expect_realm": company}
    if found.read_back:
        confirm_with["owner_reply"] = f"<{owner_name()}'s own yes, in their words>"
    return {
        **base,
        "content": _content(args, spec)[:2000],
        "recent_context": _context(pool, ws, target),
        "flags": found.flags,
        "read_back_required": found.read_back,
        "say_this": f"{spec.verb} to {base['to']} in {company}.",
        "confirm_with": confirm_with,
    }


def confirm(
    cfg: Config, pool: WorkspacePool, tickets: Tickets, ticket: str, expect_realm: str, owner_reply: str = ""
) -> dict:
    """The only path that changes Slack."""
    pending = tickets.peek(ticket)
    spec = SPECS[pending.tool]
    ws = cfg.find(pending.client_code)
    live = pool.verify_realm(ws, fresh=True)

    allowed = {normalize(live["team"] or ""), normalize(live["team_id"] or "")}
    if live["team_id"] != pending.team_id or normalize(expect_realm) not in allowed:
        tickets.spend(ticket)
        reason = f"Expected '{expect_realm}', but this would land in {live['team']}. Nothing was written."
        _audit(cfg, "refused", tool=pending.tool, client_code=ws.client_code, company=live["team"], reason=reason)
        raise SlackError(reason)

    if pending.read_back and not owner_reply.strip():
        raise SlackError(
            f"This one needs reading back. Say the realm, recipient, and words, then pass {owner_name()}'s yes."
        )

    tickets.spend(ticket)  # spent before acting, so a retry can never double-post
    content = _content(pending.args, spec)
    record = {
        "tool": pending.tool,
        "client_code": ws.client_code,
        "company": live["team"],
        "to": pending.target.label if pending.target else None,
        "target_id": pending.target.id if pending.target else None,
        "thread_ts": pending.args.get("thread_ts"),
        "instruction": pending.instruction,
        "owner_reply": owner_reply,
        "preview": content[:200],
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    try:
        result = spec.run(pool.user(ws), pool, ws, pending.args, pending.target)
    except SlackApiError as exc:
        error = _friendly(exc, ws)
        _audit(cfg, "failed", **record, reason=str(error))
        raise error from exc
    _audit(cfg, "done", **record, result=result)
    reply = {"done": True, "realm": live["team"], "client_code": ws.client_code, "to": record["to"], **result}
    if pending.remember:
        nick, full = pending.remember
        status = nicknames.remember(nick, full)
        reply["nickname"] = {
            "saved": f"From now on, {nick} means {full} in Slack, mail and texts.",
            "known": f"{nick} already meant {full}.",
            "conflict": f"{nick} already means someone else, so it wasn't saved. Edit nicknames.yaml to change it.",
        }[status]
    return reply
