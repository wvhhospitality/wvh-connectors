"""Slack over the door. Real HTTP on localhost, Slack and the Keychain faked."""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from wvh_slack_mcp import remote, server, slack_client, sync
from wvh_slack_mcp.config import Config, ConfigError, Workspace
from wvh_slack_mcp.server import REMOTE_TOOL_NAMES, _dispatch, state
from wvh_slack_mcp.slack_client import FileReply, SlackError, WorkspacePool

REGISTRY = """
workspaces:
  - {client_code: TWS, name: Tidewater Studio, team_id: T1, enabled: false}
  - {client_code: MS, name: Meridian Services, team_id: T2, enabled: false}
  - {client_code: JNV, name: Juniper & Vine, team_id: T3, enabled: false}
  - {client_code: CPK, name: Copper Kettle, team_id: T4, frozen: true}
"""


class FakePool:
    def __init__(self):
        self.saves = []

    def probe(self, ws):
        return {"client_code": ws.client_code, "name": ws.label, "status": "ok"}

    def verify_realm(self, ws, fresh=False):
        return {}

    def list_channels(self, ws):
        return [{"name": "general"}]

    def find_people(self, ws, name):
        return [{"id": "U1", "real_name": name, "name": name, "title": "", "email": ""}]

    def whats_new(self, ws, hours):
        return {"mentions": [], "dms": []}

    def read_file(self, ws, file_id, save=True):
        self.saves.append(save)
        return FileReply({"id": file_id})


@pytest.fixture
def realms(tmp_path, monkeypatch):
    (tmp_path / "workspaces.yaml").write_text(REGISTRY)
    (tmp_path / "connections.json").write_text(json.dumps({
        "app": {"client_id": "x"},
        "workspaces": {t: {"client_code": c} for t, c in (("T1", "TWS"), ("T2", "MS"), ("T3", "JNV"))},
    }))
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    monkeypatch.setenv("WVH_SLACK_REMOTE_REALMS", "TWS, MS")
    monkeypatch.setattr(server, "detect_workspaces", lambda: pytest.fail("remote must not look at this Mac's Slack"))
    pool = FakePool()
    state._config, state._pool, state.only = None, None, remote.remote_realms()
    _ = state.config
    state._pool = pool
    yield pool
    state._config, state._pool, state.only = None, None, None


def test_empty_setting_means_no_realms(monkeypatch):
    monkeypatch.delenv("WVH_SLACK_REMOTE_REALMS", raising=False)
    assert remote.remote_realms() == set()


def test_only_the_named_realms_exist(realms):
    rows = _dispatch("list_workspaces", {})["workspaces"]
    assert sorted(r["client_code"] for r in rows) == ["MS", "TWS"]
    assert "Juniper" not in json.dumps(rows) and "Copper" not in json.dumps(rows)


@pytest.mark.parametrize("ref", ["JNV", "Juniper & Vine", "CPK", "Copper Kettle"])
def test_another_client_cant_be_read_or_named(realms, ref):
    with pytest.raises(ConfigError) as caught:
        _dispatch("list_channels", {"workspace": ref})
    message = str(caught.value).replace(ref, "")
    assert "Vine" not in message and "Copper" not in message and "frozen" not in message


def test_find_person_and_whats_new_stay_inside(realms):
    assert _dispatch("find_person", {"name": "Nico"})["realms"] == ["Meridian Services", "Tidewater Studio"]
    assert sorted(_dispatch("whats_new", {})) == ["Meridian Services", "Tidewater Studio"]
    with pytest.raises(ConfigError):
        _dispatch("whats_new", {"workspace": "JNV"})


@pytest.mark.parametrize("tool", ["connect_workspace", "send_message", "confirm_write", "upload_file", "add_reaction"])
def test_writes_and_connect_refused_even_with_the_switch_on(realms, monkeypatch, tool):
    monkeypatch.setattr(server, "WRITES_ENABLED", True)
    with pytest.raises(SlackError, match="only reads"):
        _dispatch(tool, {"workspace": "TWS", "ticket": "t", "expect_realm": "Tidewater Studio"})


def test_list_has_no_writes_or_connect():
    names = {t.name for t in remote.remote_tools()}
    assert names == set(REMOTE_TOOL_NAMES)
    assert "connect_workspace" not in names
    assert not names & set(server.WRITE_TOOLS)
    assert "confirm_write" not in names


def test_files_never_land_on_disk(realms):
    _dispatch("read_file", {"workspace": "TWS", "file_id": "F1"})
    assert realms.saves == [False]


def test_read_file_without_save_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(slack_client, "downloads_dir", lambda *_: tmp_path / "downloads")
    ws = Workspace(client_code="TWS", team_id="T1")
    pool = WorkspacePool(Config(workspaces=[ws]))
    monkeypatch.setattr(pool, "_file_info", lambda w, fid: {
        "id": fid, "name": "a.pdf", "mimetype": "application/pdf", "size": 5,
        "url_private": "https://x/a.pdf", "permalink": "https://slack/a",
    })
    monkeypatch.setattr(pool, "_names", lambda w: {})
    monkeypatch.setattr(pool, "_fetch", lambda w, url: (b"%PDF-", "application/pdf"))
    reply = pool.read_file(ws, "F1", save=False)
    assert reply.meta["permalink"] == "https://slack/a"
    assert "saved_to" not in reply.meta
    assert not (tmp_path / "downloads").exists()


@pytest.fixture
def http_server(realms):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = remote.build_app(port)
    state._pool = realms
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    while not instance.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp/"
    instance.should_exit = True
    thread.join(5)


HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(url, method, params=None, headers=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return httpx.post(url, json=body, headers={**HEADERS, **(headers or {})}, timeout=10)


def test_over_real_http(http_server):
    init = rpc(http_server, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"},
    })
    assert init.status_code == 200
    assert "read Slack" in init.json()["result"]["instructions"]

    listed = rpc(http_server, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed} == set(REMOTE_TOOL_NAMES)

    called = rpc(http_server, "tools/call", {"name": "list_channels", "arguments": {"workspace": "MS"}}).json()
    assert "general" in called["result"]["content"][0]["text"]
    assert "Meridian Services" in called["result"]["content"][0]["text"]

    refused = rpc(http_server, "tools/call", {"name": "send_message", "arguments": {"workspace": "MS"}}).json()
    assert "only reads" in refused["result"]["content"][0]["text"]


def test_a_web_page_cant_reach_it(http_server):
    assert rpc(http_server, "tools/list", headers={"Origin": "https://evil.example"}).status_code == 403


def test_a_wrong_host_cant_reach_it(http_server):
    assert rpc(http_server, "tools/list", headers={"Host": "evil.example"}).status_code in (403, 421)


# ── Copying a login ───────────────────────────────────────────────────────


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def read(self, service, account):
        return self.items.get(account)

    def store(self, service, account, secret):
        self.items[account] = secret

    def delete(self, service, account):
        self.items.pop(account, None)


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    (tmp_path / "workspaces.yaml").write_text(REGISTRY)
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    fake = FakeKeychain()
    monkeypatch.setattr(sync, "keychain", fake)
    monkeypatch.setattr("wvh_slack_mcp.config.keychain", fake)
    monkeypatch.setattr(sync.WorkspacePool, "verify_realm", lambda self, ws, fresh=False: {})
    return tmp_path, fake


def _run(tmp_path, *items):
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "in").write_text("\n".join(json.dumps(i) for i in items) + "\n")
    sync.receive(str(folder))
    return json.loads((folder / "result.json").read_text())


SECOND_MAC = {"host": "mini", "home": "/Users/robin"}
GOOD = {"code": "TWS", "team_id": "T1", "token": "xoxp-fake", "connection": {"client_code": "TWS", "name": "Tidewater Studio"}}


def test_receive_stores_and_records(receiver):
    tmp_path, fake = receiver
    assert _run(tmp_path, GOOD) == [{"code": "TWS", "ok": True}]
    assert fake.items["TWS-user"] == "xoxp-fake"
    assert "T1" in json.loads((tmp_path / "connections.json").read_text())["workspaces"]


def test_receive_keeps_no_token_in_files(receiver):
    tmp_path, _ = receiver
    _run(tmp_path, GOOD)
    assert "xoxp-fake" not in (tmp_path / "connections.json").read_text()
    assert "xoxp-fake" not in (tmp_path / "drop" / "result.json").read_text()


def test_receive_refuses_frozen_and_mismatched(receiver):
    tmp_path, fake = receiver
    frozen = {**GOOD, "code": "CPK", "team_id": "T4"}
    crossed = {**GOOD, "code": "MS", "team_id": "T1"}
    rows = _run(tmp_path, frozen, crossed)
    assert [r["ok"] for r in rows] == [False, False]
    assert fake.items == {}


def test_receive_undoes_when_slack_disagrees(receiver, monkeypatch):
    tmp_path, fake = receiver
    fake.items["TWS-user"] = "older"

    def refuse(self, ws, fresh=False):
        raise SlackError("wrong team")

    monkeypatch.setattr(sync.WorkspacePool, "verify_realm", refuse)
    rows = _run(tmp_path, GOOD)
    assert rows[0]["ok"] is False
    assert fake.items["TWS-user"] == "older"
    assert "T1" not in json.loads((tmp_path / "connections.json").read_text())["workspaces"]
    assert "xoxp-fake" not in rows[0]["why"]


def test_sync_refuses_a_frozen_realm_before_touching_anything(tmp_path, monkeypatch):
    (tmp_path / "workspaces.yaml").write_text(REGISTRY)
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    monkeypatch.setattr(sync, "second_mac", lambda: SECOND_MAC)
    monkeypatch.setattr(sync, "_ssh", lambda *a, **k: pytest.fail("must not reach the second Mac"))
    with pytest.raises(ConfigError, match="frozen"):
        sync.sync_logins(["CPK"])


def test_sync_needs_a_second_mac_and_says_so(tmp_path, monkeypatch):
    (tmp_path / "workspaces.yaml").write_text(REGISTRY)
    (tmp_path / "connections.json").write_text(json.dumps({"app": {}, "workspaces": {"T1": {"client_code": "TWS"}}}))
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    fake = FakeKeychain()
    fake.items["TWS-user"] = "xoxp-secret"
    monkeypatch.setattr("wvh_slack_mcp.config.keychain", fake)
    monkeypatch.setattr(sync, "_ssh", lambda *a, **k: pytest.fail("there is nowhere to send it"))
    with pytest.raises(ConfigError, match="No second Mac"):
        sync.sync_logins(["TWS"])


@pytest.mark.parametrize("bad", [{"host": "mini", "home": "/Users/robin; rm -rf /"}, {"host": "mini x", "home": "/Users/robin"}])
def test_second_mac_settings_must_be_plain(monkeypatch, bad):
    monkeypatch.setattr(sync, "second_mac", lambda: bad)
    with pytest.raises(ConfigError, match="plain"):
        sync._target()


def test_tokens_only_travel_on_stdin(tmp_path, monkeypatch):
    (tmp_path / "workspaces.yaml").write_text(REGISTRY)
    (tmp_path / "connections.json").write_text(json.dumps({"app": {}, "workspaces": {"T1": {"client_code": "TWS"}}}))
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "workspaces.yaml"))
    fake = FakeKeychain()
    fake.items["TWS-user"] = "xoxp-secret"
    monkeypatch.setattr("wvh_slack_mcp.config.keychain", fake)
    calls = []

    class Done:
        returncode = 0
        stdout = '[{"code": "TWS", "ok": true}]'

    def fake_run(args, input=None, **kwargs):
        calls.append((args, input))
        return Done()

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    monkeypatch.setattr(sync, "second_mac", lambda: SECOND_MAC)
    assert sync.sync_logins(["TWS"]) == 0
    assert all(args[:4] == ["ssh", "-o", "BatchMode=yes", "mini"] for args, _ in calls)
    assert all("xoxp-secret" not in " ".join(args) for args, _ in calls)
    assert sum("xoxp-secret" in (stdin or "") for _, stdin in calls) == 1


def test_cleanup_only_touches_named_files():
    home = SECOND_MAC["home"]
    clear, start = sync.clear_script(home), sync.start_script(home, "/Users/robin/app/.venv/bin/python")
    assert "rm -r" not in clear and "rm -f " in clear
    assert "$HOME" not in clear and "$HOME" not in start and "~" not in clear
    assert "rmdir " + sync.drop_folder(home) in clear
    assert sync.drop_folder(home) == "/Users/robin/.wvh-slack-sync"


def test_stdio_mode_uses_the_same_restrictions(realms):
    import asyncio
    from mcp import types
    server = remote.restricted_server()
    assert state.only == {"TWS", "MS"}
    handler = server.request_handlers[types.ListToolsRequest]
    result = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    names = {t.name for t in result.root.tools}
    assert names == set(REMOTE_TOOL_NAMES)
    assert "connect_workspace" not in names and "send_message" not in names
