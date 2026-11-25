"""One-click Slack login through this install's own Slack app.

PKCE means there is no client secret to hold. The browser does the sign-in,
Slack hands back a one-time code, and that code is useless without the verifier
that lives only in this process. Tokens go straight to the Keychain over standard
input. They are never printed, logged, or passed on a command line.

Slack's answer decides identity. The team a person actually approved is the team
that gets recorded, whatever was requested, so one company can't land under another.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from . import keychain
from .config import Config, Workspace, connections_path, owner_name, read_connections, write_connections
from .detect import Detected, detect_workspaces
from .slack_client import _ssl_context

log = logging.getLogger("wvh_slack_mcp.oauth")

AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
API_ROOT = "https://slack.com/api/"
CALLBACK_PORT = 53682
LOGIN_TIMEOUT_SECONDS = 180

# Exactly what the tools use. A login carrying anything else gets revoked on the spot.
# Having a write permission doesn't mean anything writes: see writes.py for that gate.
USER_SCOPES = (
    "channels:read",
    "channels:history",
    "channels:write",
    "groups:read",
    "groups:history",
    "groups:write",
    "im:read",
    "im:history",
    "im:write",
    "mpim:read",
    "mpim:history",
    "mpim:write",
    "users:read",
    "users:read.email",
    "search:read",
    "chat:write",
    "files:read",
    "files:write",
    "reactions:read",
    "reactions:write",
    "emoji:read",
    "canvases:read",
    "canvases:write",
    "lists:read",
    "lists:write",
)
_HARMLESS_SCOPES = {"identify"}

_DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Connected</title>"
    "<body style='font:17px -apple-system,sans-serif;margin:20vh auto;max-width:30rem;padding:0 1rem'>"
    "<h1 style='font-size:1.4rem'>You're all set</h1><p>You can close this tab.</p>"
)

Post = Callable[..., dict]


class ConnectError(Exception):
    """A plain-language reason the login didn't finish."""


@dataclass(frozen=True)
class Connected:
    client_code: str
    name: str
    team_id: str
    team_domain: str | None
    identity: str | None
    is_new: bool


# ─── PKCE and URLs ────────────────────────────────────────────────────────


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    team_id: str | None = None,
    domain: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "scope": "",
        "user_scope": ",".join(USER_SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if team_id:
        params["team"] = team_id  # a hint: Slack may fall back, so the response decides
    base = AUTHORIZE_URL
    if domain and _DOMAIN.fullmatch(domain):
        # The team hint alone loses to whichever workspace the browser used last.
        # Opening the page on the workspace's own address pins it there.
        base = f"https://{domain}.slack.com/oauth/v2/authorize"
    return f"{base}?{urllib.parse.urlencode(params)}"


_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def domain_for(config: Config, team_id: str | None) -> str | None:
    if not team_id:
        return None
    ws = config.by_team(team_id)
    if ws is not None and ws.team_domain:
        return ws.team_domain
    return next((d.domain for d in detect_workspaces() if d.team_id == team_id), None)


def _post_form(method: str, fields: dict, token: str | None = None) -> dict:
    request = urllib.request.Request(
        API_ROOT + method, data=urllib.parse.urlencode(fields).encode(), method="POST"
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
        return json.loads(response.read())


# ─── The single-use local listener ────────────────────────────────────────


class _Listener:
    """Waits on loopback for the one redirect carrying our state value."""

    def __init__(self, state: str, port: int = CALLBACK_PORT) -> None:
        self.state = state
        self.port = port
        self.result: dict | None = None

    def __enter__(self) -> "_Listener":
        listener = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 5  # Chrome opens idle preconnects; don't let one wedge the wait

            def do_GET(self) -> None:
                url = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                if url.path != "/callback" or query.get("state") != listener.state:
                    self.send_error(404)
                    return
                listener.result = query
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_DONE_PAGE.encode())

            def log_message(self, format: str, *args: object) -> None:
                pass  # the request line carries the one-time code; keep it out of every log

        try:
            self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            raise ConnectError(
                f"Port {self.port} is busy, so another Slack connect is probably running. "
                "Finish that one first."
            ) from exc
        self.server.timeout = 1
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.server_close()

    def wait(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.server.handle_request()
            if self.result is not None:
                return self.result
        raise ConnectError("Timed out waiting for Slack. Nothing changed. Run it again when ready.")


# ─── Finishing the login ──────────────────────────────────────────────────


def suggest_code(name: str, taken: set[str]) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in {"llc", "inc", "the", "and"}]
    base = "".join(w[0] for w in words).upper() if len(words) > 1 else "".join(words).upper()[:4]
    base = base or "WS"
    code, n = base, 2
    while code in taken:
        code, n = f"{base}{n}", n + 1
    return code


def _revoke(token: str, post: Post) -> None:
    try:
        post("auth.revoke", {}, token=token)
    except Exception:
        log.warning("Could not revoke a refused token; remove the app from that workspace in Slack.")


def _finish(config: Config, grant: dict, post: Post) -> Connected:
    if not grant.get("ok"):
        raise ConnectError(f"Slack refused the login ({grant.get('error', 'unknown error')}).")

    person = grant.get("authed_user") or {}
    token = person.get("access_token")
    team = grant.get("team") or {}
    if not token or not team.get("id"):
        raise ConnectError("Slack's answer had no user token. Nothing was saved.")

    granted = {s for s in (person.get("scope") or "").split(",") if s}
    extra = granted - set(USER_SCOPES) - _HARMLESS_SCOPES
    if extra:
        _revoke(token, post)
        raise ConnectError(
            f"Refused and revoked: this login came back with access it shouldn't have ({', '.join(sorted(extra))}). "
            "The Slack app's manifest has drifted."
        )

    who = post("auth.test", {}, token=token)
    if not who.get("ok") or who.get("team_id") != team["id"]:
        _revoke(token, post)
        raise ConnectError("Slack couldn't confirm that login, so it was revoked. Nothing was saved.")

    team_id = team["id"]
    domain = (urllib.parse.urlparse(who.get("url") or "").hostname or "").split(".")[0] or None
    name = who.get("team") or team.get("name") or team_id

    ws: Workspace | None = config.by_team(team_id)
    if ws is not None and ws.frozen:
        _revoke(token, post)
        raise ConnectError(f"{ws.label} is frozen by {owner_name()}, so that login was revoked. Nothing was saved.")

    taken = {w.client_code for w in config.workspaces}
    code = ws.client_code if ws else suggest_code(name, taken)

    account = f"{code}-user"
    keychain.store(config.keychain_service, account, token)
    if keychain.read(config.keychain_service, account) != token:
        raise ConnectError(f"The Keychain didn't keep the {code} token. Nothing was recorded.")

    path = connections_path(config.source_path)
    state = read_connections(path)
    state["workspaces"][team_id] = {
        "client_code": code,
        "name": name,
        "team_domain": domain,
        "identity": who.get("user"),
        "scopes": sorted(granted),
        "app_id": grant.get("app_id"),
        "connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_connections(path, state)

    return Connected(code, name, team_id, domain, who.get("user"), is_new=ws is None)


def connect(
    config: Config,
    team_id: str | None = None,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    post: Post = _post_form,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> Connected:
    """Open Slack's Allow page, wait for it, and store the result."""
    if not config.client_id:
        raise ConnectError("The Slack app isn't set up yet. Run: wvh-slack-connect setup")

    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    url = authorize_url(
        config.client_id, config.redirect_uri, state, challenge_for(verifier), team_id, domain_for(config, team_id)
    )

    with _Listener(state) as listener:
        open_browser(url)
        reply = listener.wait(timeout)

    if reply.get("error"):
        reason = "You clicked Cancel" if reply["error"] == "access_denied" else f"Slack said {reply['error']}"
        raise ConnectError(f"{reason}. Nothing changed.")

    grant = post(
        "oauth.v2.access",
        {
            "client_id": config.client_id,
            "code": reply.get("code", ""),
            "redirect_uri": config.redirect_uri,
            "code_verifier": verifier,
        },
    )
    return _finish(config, grant, post)


def disconnect(config: Config, ws: Workspace, post: Post = _post_form) -> None:
    """Kill the token at Slack, then forget it here. Offboarding in one step."""
    account = f"{ws.client_code}-user"
    token = keychain.read(config.keychain_service, account)
    if token:
        _revoke(token, post)
        keychain.delete(config.keychain_service, account)

    path = connections_path(config.source_path)
    state = read_connections(path)
    if ws.team_id:
        state["workspaces"].pop(ws.team_id, None)
    write_connections(path, state)


def resolve_target(config: Config, ref: str) -> str | None:
    """Turn 'Northwind' or 'NB' into a team id for Slack's Allow page."""
    ref = ref.strip()
    if not ref:
        return None

    ws = config.lookup(ref)
    detected = detect_workspaces()
    needle = ref.lower()
    if ws is None:
        hits = [w for w in config.workspaces if needle in w.label.lower()]
        ws = hits[0] if len(hits) == 1 else None
    if ws is not None and ws.frozen:
        raise ConnectError(f"{ws.label} is frozen by {owner_name()}. It stays closed until that hold is lifted.")
    if ws is not None and ws.team_id:
        return ws.team_id

    found: list[Detected] = [d for d in detected if needle in d.name.lower() or needle == (d.domain or "")]
    if len(found) == 1:
        return found[0].team_id
    if len(found) > 1:
        names = ", ".join(d.name for d in found)
        raise ConnectError(f"'{ref}' matches more than one workspace: {names}. Be more specific.")
    raise ConnectError(f"No Slack workspace on this Mac matches '{ref}'. Check you're signed in to it in Slack.")
