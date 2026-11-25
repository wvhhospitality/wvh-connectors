"""The one-click login and read path, with Slack faked and a real local listener."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml
from slack_sdk.errors import SlackApiError

from wvh_slack_mcp import connect_cli, oauth, server
from wvh_slack_mcp.config import DEFAULT_REDIRECT_URI, ConfigError, connections_path, load_config, write_connections
from wvh_slack_mcp.detect import Detected, detect_workspaces
from wvh_slack_mcp.oauth import CALLBACK_PORT, USER_SCOPES, ConnectError
from wvh_slack_mcp.slack_client import SlackError, WorkspacePool

PROJECT = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "xoxp-test-not-a-real-token"
REGISTRY = """
workspaces:
  - client_code: NB
    team_id: T_NB
    name: Northwind Builders
    team_domain: northwindbuilders
    enabled: false
  - client_code: CPK
    team_id: T_CPK
    name: Copper Kettle
    enabled: false
    frozen: true
  - client_code: CC
    team_id: T_CC
    name: Copper Lantern Guild
    enabled: false
"""


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def store(self, service, account, value):
        self.items[(service, account)] = value

    def read(self, service, account):
        return self.items.get((service, account))

    def delete(self, service, account):
        self.items.pop((service, account), None)


class FakeSlack:
    """Stands in for the Slack Web API at the HTTP form-post level."""

    def __init__(self, team="T_NB", name="Northwind Builders", scope=",".join(USER_SCOPES)):
        self.team, self.name, self.scope = team, name, scope
        self.calls = []

    def __call__(self, method, fields, token=None):
        self.calls.append((method, fields))
        if method == "oauth.v2.access":
            return {
                "ok": True,
                "app_id": "A1",
                "team": {"id": self.team, "name": self.name},
                "authed_user": {"id": "U1", "scope": self.scope, "access_token": FAKE_TOKEN},
            }
        if method == "auth.test":
            return {"ok": True, "team_id": self.team, "team": self.name, "user": "owner",
                    "url": "https://northwindbuilders.slack.com/"}
        if method == "auth.revoke":
            return {"ok": True, "revoked": True}
        raise AssertionError(f"unexpected call {method}")

    @property
    def revoked(self):
        return any(m == "auth.revoke" for m, _ in self.calls)


@pytest.fixture
def keychain(monkeypatch):
    fake = FakeKeychain()
    monkeypatch.setattr(oauth, "keychain", fake)
    monkeypatch.setattr("wvh_slack_mcp.config.keychain", fake)
    return fake


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "workspaces.yaml"
    path.write_text(REGISTRY)
    write_connections(connections_path(path), {"app": {"client_id": "111.222"}, "workspaces": {}})
    return load_config(path)


def allow_click(reply=None):
    """Plays the browser: Slack redirects back to the local listener."""

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

        def hit():
            for params in ({"code": "stray", "state": "wrong"}, {"state": query["state"], **(reply or {"code": "abc"})}):
                target = f"http://127.0.0.1:{CALLBACK_PORT}/callback?" + urllib.parse.urlencode(params)
                try:
                    urllib.request.urlopen(target, timeout=5).read()
                except urllib.error.HTTPError:
                    pass

        threading.Thread(target=hit, daemon=True).start()

    return browser


# ─── Login ────────────────────────────────────────────────────────────────


def test_pkce_matches_slacks_published_example():
    assert oauth.challenge_for("secretpassword") == "ldMBaaWcQYtSATMV_IG8mf3wp7A6EW80arYoSW80ntU"


def test_allow_page_asks_for_no_bot_and_no_admin():
    url = oauth.authorize_url("1.2", DEFAULT_REDIRECT_URI, "st", "ch", "T_NB")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query, keep_blank_values=True))
    assert query["scope"] == ""  # no bot
    assert query["code_challenge_method"] == "S256"
    assert query["team"] == "T_NB"
    assert not [s for s in query["user_scope"].split(",") if "admin" in s]


def test_manifest_page_and_code_all_agree():
    manifest = yaml.safe_load(connect_cli.manifest_text(DEFAULT_REDIRECT_URI))
    oauth_config = manifest["oauth_config"]
    assert set(oauth_config["scopes"]["user"]) == set(USER_SCOPES)
    assert "bot" not in oauth_config["scopes"] and "features" not in manifest
    assert oauth_config["pkce_enabled"] is True
    assert manifest["settings"]["token_rotation_enabled"] is False
    assert oauth_config["redirect_urls"] == [DEFAULT_REDIRECT_URI]
    assert DEFAULT_REDIRECT_URI.endswith("/slack/pages/slack-connected.html")
    assert f"127.0.0.1:{CALLBACK_PORT}/callback" in (PROJECT / "pages/slack-connected.html").read_text()


def test_each_install_makes_its_own_app_named_for_its_owner(monkeypatch):
    info = yaml.safe_load(connect_cli.manifest_text("https://example.org/done.html"))["display_information"]
    assert info["name"] == "Robin Assistant" and "Robin" in info["description"]
    assert len(info["name"]) <= 35 and len(info["description"]) <= 140

    monkeypatch.delenv("WVH_SLACK_OWNER")
    monkeypatch.setenv("WVH_SLACK_CONFIG", "/nonexistent/workspaces.yaml")
    info = yaml.safe_load(connect_cli.manifest_text("https://example.org/done.html"))["display_information"]
    assert info["name"] == "Slack Assistant", "no name set: a neutral one, never someone else's"


def test_setup_saves_the_client_id_and_the_address_it_was_made_with(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WVH_SLACK_CONFIG", str(tmp_path / "fresh" / "workspaces.yaml"))
    assert connect_cli.cmd_setup(["--client-id", "not-an-id"]) == 1
    assert not (tmp_path / "fresh").exists()

    assert connect_cli.cmd_setup(["--client-id", "123.456"]) == 0
    saved = json.loads((tmp_path / "fresh" / "connections.json").read_text())["app"]
    assert saved == {"client_id": "123.456", "redirect_uri": DEFAULT_REDIRECT_URI}
    assert load_config().client_id == "123.456"


def test_full_login_stores_token_only_in_keychain(cfg, keychain, capsys, caplog):
    slack = FakeSlack()
    done = oauth.connect(cfg, "T_NB", open_browser=allow_click(), post=slack, timeout=10)

    assert (done.client_code, done.team_domain, done.is_new) == ("NB", "northwindbuilders", False)
    assert keychain.items[("WvHSlack", "NB-user")] == FAKE_TOKEN

    exchange = next(f for m, f in slack.calls if m == "oauth.v2.access")
    assert exchange["code"] == "abc" and exchange["code_verifier"]
    assert "client_secret" not in exchange

    on_disk = connections_path(cfg.source_path).read_text()
    assert FAKE_TOKEN not in on_disk
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out + captured.err + caplog.text

    # A connection turns the dormant entry live on the next load, no restart.
    assert cfg.is_stale()
    assert [w.client_code for w in load_config(cfg.source_path).enabled] == ["NB"]


def test_what_you_approved_is_what_gets_recorded(cfg, keychain):
    """Asked for one Copper workspace, approved the other: identity comes from Slack."""
    slack = FakeSlack(team="T_CC", name="Copper Lantern Guild")
    done = oauth.connect(cfg, "T_NB", open_browser=allow_click(), post=slack, timeout=10)
    assert done.client_code == "CC"
    assert ("WvHSlack", "NB-user") not in keychain.items


def test_frozen_workspace_is_revoked_and_nothing_saved(cfg, keychain):
    slack = FakeSlack(team="T_CPK", name="Copper Kettle")
    with pytest.raises(ConnectError, match="frozen"):
        oauth.connect(cfg, None, open_browser=allow_click(), post=slack, timeout=10)
    assert slack.revoked and not keychain.items
    assert load_config(cfg.source_path).enabled == []


def test_any_scope_off_the_list_is_revoked_and_nothing_saved(cfg, keychain):
    slack = FakeSlack(scope=",".join(USER_SCOPES) + ",admin.users:write")
    with pytest.raises(ConnectError, match="admin.users:write"):
        oauth.connect(cfg, "T_NB", open_browser=allow_click(), post=slack, timeout=10)
    assert slack.revoked and not keychain.items


def test_cancel_changes_nothing(cfg, keychain):
    slack = FakeSlack()
    browser = allow_click({"error": "access_denied"})
    with pytest.raises(ConnectError, match="Cancel"):
        oauth.connect(cfg, "T_NB", open_browser=browser, post=slack, timeout=10)
    assert not slack.calls and not keychain.items


def test_unknown_workspace_registers_itself(cfg, keychain):
    slack = FakeSlack(team="T_NEW", name="Silver Birch Foundation")
    done = oauth.connect(cfg, None, open_browser=allow_click(), post=slack, timeout=10)
    assert done.is_new and done.client_code == "SBF"
    assert load_config(cfg.source_path).find("SBF").team_id == "T_NEW"


def test_needs_setup_before_login(tmp_path):
    path = tmp_path / "workspaces.yaml"
    path.write_text(REGISTRY)
    with pytest.raises(ConnectError, match="setup"):
        oauth.connect(load_config(path), open_browser=lambda url: None)


def test_disconnect_revokes_and_forgets(cfg, keychain):
    slack = FakeSlack()
    oauth.connect(cfg, "T_NB", open_browser=allow_click(), post=slack, timeout=10)
    fresh = load_config(cfg.source_path)
    oauth.disconnect(fresh, fresh.find("NB"), post=slack)
    assert slack.revoked and not keychain.items
    assert load_config(cfg.source_path).enabled == []


def test_codes_stay_unique():
    assert oauth.suggest_code("Ember rae", {"ER"}) == "ER2"
    assert oauth.suggest_code("The Quillon LLC", set()) == "QUIL"


def test_resolve_target(cfg, monkeypatch):
    monkeypatch.setattr(oauth, "detect_workspaces", lambda: [Detected("T_A", "Ember rae", "emberrae")])
    assert oauth.resolve_target(cfg, "NB") == "T_NB"
    assert oauth.resolve_target(cfg, "northwind") == "T_NB"
    assert oauth.resolve_target(cfg, "ember") == "T_A"
    assert oauth.resolve_target(cfg, "") is None
    with pytest.raises(ConnectError, match="frozen"):
        oauth.resolve_target(cfg, "CPK")
    with pytest.raises(ConnectError, match="No Slack workspace"):
        oauth.resolve_target(cfg, "nobody")


# ─── Detection ────────────────────────────────────────────────────────────


def test_detect_reads_names_and_survives_missing_files(tmp_path):
    state = tmp_path / "root-state.json"
    state.write_text(json.dumps({"workspaces": {
        "T2": {"name": "Zed", "url": "https://zed.slack.com/", "extra": "ignored"},
        "T1": {"name": "Alpha", "url": "https://alpha.slack.com/"},
    }}))
    found = detect_workspaces((str(tmp_path / "missing.json"), str(state)))
    assert found == [Detected("T1", "Alpha", "alpha"), Detected("T2", "Zed", "zed")]


# ─── Reading ──────────────────────────────────────────────────────────────


class Response(dict):
    headers = {"Retry-After": "42"}


def slack_error(code):
    return SlackApiError(code, Response(ok=False, error=code))


class FakeClient:
    def __init__(self, history=None, search=(), error=None):
        self.history_reply, self.search_matches, self.error = history, list(search), error
        self.search_queries = []

    def conversations_history(self, **kwargs):
        if self.error:
            raise slack_error(self.error)
        return self.history_reply

    def search_messages(self, **kwargs):
        self.search_queries.append(kwargs["query"])
        return {"messages": {"matches": self.search_matches}}

    def conversations_list(self, **kwargs):
        return {"channels": [{"id": "C123456789", "name": "ops"}]}

    def users_list(self, **kwargs):
        return {"members": [{"id": "U1", "profile": {"display_name": "Robin"}}]}

    def conversations_members(self, **kwargs):
        return {"members": ["U1"]}

    def conversations_join(self, **kwargs):
        raise AssertionError("a read must never join a channel: it posts in the client's Slack")


def pool_with(cfg, bot=None, user=None):
    ws = cfg.lookup("NB")
    pool = WorkspacePool(cfg)
    pool._bot_clients["NB"], pool._user_clients["NB"] = bot, user
    return pool, ws


def message(ts):
    return {"ts": str(ts), "text": f"m{ts}", "user": "U1"}


def test_read_falls_from_bot_to_user_without_joining(cfg):
    pool, ws = pool_with(cfg, bot=FakeClient(error="not_in_channel"),
                         user=FakeClient(history={"messages": [message(1000)]}))
    assert [m["text"] for m in pool.history(ws, "#ops")] == ["m1000"]


def test_bot_only_read_reports_instead_of_joining(cfg):
    pool, ws = pool_with(cfg, bot=FakeClient(error="not_in_channel"))
    with pytest.raises(SlackError, match="wvh-slack-connect NB"):
        pool.history(ws, "#ops")


def test_short_history_is_filled_by_search_in_order(cfg):
    capped = {"messages": [message(ts) for ts in (1500, 1400)], "has_more": True}
    older = [
        {**message(1400), "permalink": "x"},                     # overlap, dropped
        {**message(1350), "permalink": "x?thread_ts=1"},         # thread reply, dropped
        {**message(1300), "permalink": "x"},
    ]
    pool, ws = pool_with(cfg, user=FakeClient(history=capped, search=older))
    assert [m["text"] for m in pool.history(ws, "#ops", limit=30)] == ["m1300", "m1400", "m1500"]
    assert pool.user(ws).search_queries[0].startswith("in:#ops")


def test_throttled_read_comes_from_search(cfg):
    client = FakeClient(error="ratelimited", search=[{**message(1300), "permalink": "x"}])
    pool, ws = pool_with(cfg, user=client)
    assert [m["text"] for m in pool.history(ws, "#ops")] == ["m1300"]


def test_throttled_with_nothing_found_says_how_long_to_wait(cfg):
    pool, ws = pool_with(cfg, user=FakeClient(error="ratelimited"))
    with pytest.raises(SlackError, match="42 seconds"):
        pool.history(ws, "#ops")


def test_no_login_at_all_says_how_to_connect(cfg, keychain):
    pool = WorkspacePool(cfg)
    ws = cfg.lookup("NB")
    with pytest.raises(Exception, match="wvh-slack-connect NB"):
        pool.reader(ws)


# ─── Freeze ───────────────────────────────────────────────────────────────


def test_frozen_workspace_cannot_be_read(cfg):
    with pytest.raises(ConfigError, match="frozen"):
        cfg.find("CPK")


def test_writing_stays_off_unless_this_machine_allows_it(cfg, monkeypatch):
    monkeypatch.setattr(server, "WRITES_ENABLED", False)
    monkeypatch.setattr(server.state, "_config", cfg)
    monkeypatch.setattr(server.state, "_pool", None)
    for tool in ("send_message", "confirm_write"):
        with pytest.raises(SlackError, match="off on this machine"):
            server._dispatch(tool, {"workspace": "NB", "to": "#ops", "text": "hi", "ticket": "x", "expect_realm": "y"})


def test_status_board_sorts_everything(cfg, keychain, monkeypatch):
    slack = FakeSlack()
    oauth.connect(cfg, "T_NB", open_browser=allow_click(), post=slack, timeout=10)
    live = load_config(cfg.source_path)
    monkeypatch.setattr(server.state, "_config", live)
    monkeypatch.setattr(server.state, "_pool", None)
    monkeypatch.setattr(server, "detect_workspaces", lambda: [
        Detected("T_CC", "Copper Lantern Guild", "cc"), Detected("T_NEW", "Ember rae", "emberrae"),
    ])
    out = server._dispatch("list_workspaces", {"probe": False})
    by_name = {row["name"]: row["status"] for row in out["workspaces"]}
    assert by_name == {
        "Northwind Builders": "enabled",
        "Copper Kettle": "frozen",
        "Copper Lantern Guild": "available",
        "Ember rae": "available",
    }
    assert "wvh-slack-connect" in out["summary"]["next_step"]


# ─── The real Keychain tool, faked at the process level ───────────────────


def test_a_token_never_goes_on_a_command_line(monkeypatch):
    from wvh_slack_mcp import keychain as real

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(real.subprocess, "run", run)
    real.store("WvHSlack", "NB-user", FAKE_TOKEN)
    assert all(FAKE_TOKEN not in " ".join(args) for args, _ in calls)
    assert any(FAKE_TOKEN in (given or "") for _, given in calls)


def test_a_stuck_keychain_read_times_out_instead_of_freezing(monkeypatch):
    import subprocess

    from wvh_slack_mcp import keychain as real

    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(real.subprocess, "run", run)
    assert real.read("WvHSlack", "MS-bot") is None
    assert seen["timeout"] <= 15


def test_odd_values_are_refused_before_reaching_the_keychain():
    from wvh_slack_mcp import keychain as real

    with pytest.raises(ValueError):
        real.store("WvHSlack", "NB-user", 'x" -T /bin/sh')


def test_a_leftover_user_token_is_not_mistaken_for_the_new_login(cfg, keychain):
    """The second Mac holds an old MS `-user` token with almost no permissions."""
    keychain.items[("WvHSlack", "NB-user")] = "xoxp-old-app-token"
    assert cfg.user_token(cfg.lookup("NB")) is None

    oauth.connect(cfg, "T_NB", open_browser=allow_click(), post=FakeSlack(), timeout=10)
    fresh = load_config(cfg.source_path)
    assert fresh.user_token(fresh.lookup("NB")) == FAKE_TOKEN


def test_allow_page_opens_on_the_workspaces_own_address():
    url = oauth.authorize_url("1.2", DEFAULT_REDIRECT_URI, "st", "ch", "T_JNV", "juniperandvinellc")
    assert url.startswith("https://juniperandvinellc.slack.com/oauth/v2/authorize?")
    assert "team=T_JNV" in url


def test_odd_domain_falls_back_to_the_plain_page():
    url = oauth.authorize_url("1.2", DEFAULT_REDIRECT_URI, "st", "ch", "T_X", "evil.example/x")
    assert url.startswith(oauth.AUTHORIZE_URL + "?")
