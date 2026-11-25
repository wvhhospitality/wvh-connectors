"""Settings, the workspace registry, and Keychain credential resolution.

Everything personal lives in one settings folder, outside the app, so updating
the app never touches it:

    ~/Library/Application Support/wvh-slack/      (or wherever WVH_SLACK_HOME points)
        workspaces.yaml    hand-kept, optional: your name, time zone, workspace codes, holds
        connections.json   machine-kept: what `wvh-slack-connect` linked (no secrets)
        writes.jsonl       machine-kept: the record of every write and refusal
        downloads/         files opened with read_file

    macOS Keychain         the tokens, service "WvHSlack", never in a file

Keychain entries per workspace:
    <CODE>-user  -> xoxp-... the owner's login, from the Slack app this install created
    <CODE>-bot   -> xoxb-... an older per-workspace app, if one was ever set up
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field

from . import keychain

APP_FOLDER = "wvh-slack"

# Slack needs an HTTPS return address for an app that serves more than one workspace.
# This static page only forwards the one-time code to the local listener; no secret
# ever passes through it. An install records the address it was set up with, so
# changing this default never breaks an app that already exists.
DEFAULT_REDIRECT_URI = "https://wvhhospitality.github.io/wvh-connectors/slack/pages/slack-connected.html"

DEFAULT_OWNER = "the owner"


class ConfigError(Exception):
    """A settings file is malformed, or a workspace can't be used as asked."""


class CredentialError(Exception):
    """Keychain entry missing for a configured workspace."""


# ─── Where settings live ──────────────────────────────────────────────────


def home_dir() -> Path:
    """The settings folder. WVH_SLACK_HOME moves it; nothing personal lives beside the code."""
    explicit = os.environ.get("WVH_SLACK_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_FOLDER


def resolve_config_path(path: str | os.PathLike | None = None) -> Path:
    """The registry file. WVH_SLACK_CONFIG names one file outright; otherwise it's in home_dir()."""
    chosen = path or os.environ.get("WVH_SLACK_CONFIG")
    return Path(chosen).expanduser() if chosen else home_dir() / "workspaces.yaml"


def connections_path(cfg_path: str | os.PathLike | None = None) -> Path:
    return resolve_config_path(cfg_path).with_name("connections.json")


def downloads_dir(cfg_path: str | os.PathLike | None = None) -> Path:
    return resolve_config_path(cfg_path).with_name("downloads")


def _read_registry(cfg_path: Path) -> dict:
    """The hand-kept file, or nothing at all: a new install has none and doesn't need one."""
    if not cfg_path.exists():
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path} should hold settings, not a bare list or value.")
    return raw


_settings_cache: tuple[Path, float, dict] | None = None


def settings(path: str | os.PathLike | None = None) -> dict:
    """The hand-kept settings, re-read only when the file changes. Never raises."""
    global _settings_cache
    cfg_path = resolve_config_path(path)
    try:
        stamp = cfg_path.stat().st_mtime
    except OSError:
        return {}
    if _settings_cache is not None and _settings_cache[:2] == (cfg_path, stamp):
        return _settings_cache[2]
    try:
        raw = _read_registry(cfg_path)
    except ConfigError:
        raw = {}
    _settings_cache = (cfg_path, stamp, raw)
    return raw


def owner_name() -> str:
    """Who this install works for. Used in every message so Claude addresses the right person."""
    name = os.environ.get("WVH_SLACK_OWNER") or settings().get("owner_name")
    return str(name).strip() if name and str(name).strip() else DEFAULT_OWNER


def owner_cap() -> str:
    """owner_name(), fit to start a sentence."""
    name = owner_name()
    return name[:1].upper() + name[1:]


def local_timezone() -> ZoneInfo:
    """The `timezone` setting, else this Mac's own zone, else UTC."""
    wanted = os.environ.get("WVH_SLACK_TIMEZONE") or settings().get("timezone")
    candidates = [str(wanted)] if wanted else []
    try:
        system = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in system:
            candidates.append(system.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def second_mac() -> dict:
    """Optional: an always-on Mac that logins can be copied to. Empty when there isn't one."""
    raw = settings().get("second_mac")
    return raw if isinstance(raw, dict) else {}


# ─── The registry ─────────────────────────────────────────────────────────


class Workspace(BaseModel):
    client_code: str
    name: str | None = None          # human label, e.g. "Northwind Bakery"
    team_domain: str | None = None   # the <team_domain>.slack.com subdomain
    team_id: str | None = None       # Slack's own id; the identity that decides everything
    aliases: list[str] = Field(default_factory=list)  # other names, used by the discretion filter
    enabled: bool = True
    frozen: bool = False             # the owner's hold: unreadable and unconnectable until lifted

    @property
    def label(self) -> str:
        return self.name or self.client_code


class Config(BaseModel):
    keychain_service: str = "WvHSlack"
    workspaces: list[Workspace] = Field(default_factory=list)
    """Every registry entry, enabled or not. Filter with `.enabled` for live ones."""

    connected: set[str] = Field(default_factory=set)  # team ids signed in through this install's app
    client_id: str | None = None     # the Slack app's public id; not a secret
    redirect_uri: str = DEFAULT_REDIRECT_URI

    source_path: Path | None = None
    source_stamp: tuple[float, float] | None = None

    def is_stale(self) -> bool:
        """True if the registry or connections changed on disk since this load."""
        if self.source_path is None:
            return False
        try:
            return _stamp(self.source_path) != self.source_stamp
        except OSError:
            return False

    @property
    def enabled(self) -> list[Workspace]:
        return [w for w in self.workspaces if w.enabled and not w.frozen]

    def _match(self, pool: list[Workspace], needle: str) -> Workspace | None:
        for ws in pool:
            if ws.client_code.lower() == needle:
                return ws
        for ws in pool:
            if (ws.name and ws.name.lower() == needle) or (
                ws.team_domain and ws.team_domain.lower() == needle
            ):
                return ws
        return None

    def lookup(self, ref: str) -> Workspace | None:
        """Any registry entry, live or not, by code, name, or domain."""
        return self._match(self.workspaces, ref.strip().lstrip("#").lower())

    def by_team(self, team_id: str) -> Workspace | None:
        return next((w for w in self.workspaces if w.team_id == team_id), None)

    def find(self, ref: str) -> Workspace:
        """Resolve a *live* workspace by code (preferred), name, or team_domain."""
        needle = ref.strip().lstrip("#").lower()
        hit = self._match(self.enabled, needle)
        if hit is not None:
            return hit

        known = self._match(self.workspaces, needle)
        if known is not None and known.frozen:
            raise ConfigError(
                f"Workspace '{known.client_code}' ({known.label}) is frozen by {owner_name()}. "
                "It stays closed until that hold is lifted."
            )
        if known is not None:
            raise ConfigError(
                f"Workspace '{known.client_code}' ({known.label}) isn't connected yet. "
                f"Use connect_workspace, or run: wvh-slack-connect {known.client_code}"
            )

        live = ", ".join(w.client_code for w in self.enabled) or "(none connected yet)"
        raise ConfigError(f"Unknown workspace '{ref}'. Connected: {live}")

    # ─── Credentials ──────────────────────────────────────────────────────

    def bot_token(self, ws: Workspace) -> str | None:
        """Older per-workspace app token. Fast history reads, but limited to invited channels."""
        return keychain.read(self.keychain_service, f"{ws.client_code}-bot")

    def user_token(self, ws: Workspace) -> str | None:
        """The owner's login. Sees what the owner sees; the only token that may write.

        Only a login this install's app recorded counts. Older per-workspace apps can
        leave `-user` tokens behind with unknown scopes, and those must never be
        mistaken for it, for reading or for writing.
        """
        if ws.team_id not in self.connected:
            return None
        return keychain.read(self.keychain_service, f"{ws.client_code}-user")


def read_connections(path: Path) -> dict:
    if not path.exists():
        return {"app": {}, "workspaces": {}}
    try:
        raw = json.loads(path.read_text())
    except ValueError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    return {"app": raw.get("app") or {}, "workspaces": raw.get("workspaces") or {}}


def write_connections(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _stamp(cfg_path: Path) -> tuple[float, float]:
    return (_mtime(cfg_path), _mtime(connections_path(cfg_path)))


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = resolve_config_path(path)
    raw = _read_registry(cfg_path)

    workspaces = [Workspace(**w) for w in raw.get("workspaces") or []]
    state = read_connections(connections_path(cfg_path))

    by_team = {w.team_id: w for w in workspaces if w.team_id}
    for team_id, conn in state["workspaces"].items():
        ws = by_team.get(team_id)
        if ws is None:
            workspaces.append(
                Workspace(
                    client_code=conn["client_code"],
                    name=conn.get("name"),
                    team_domain=conn.get("team_domain"),
                    team_id=team_id,
                )
            )
        else:
            ws.enabled = True
            ws.team_domain = ws.team_domain or conn.get("team_domain")

    app = state["app"]
    # An empty registry is how every install starts. list_workspaces shows what this
    # Mac is signed in to and what to do next, so nothing here treats it as an error.
    return Config(
        keychain_service=raw.get("keychain_service", "WvHSlack"),
        workspaces=workspaces,
        connected=set(state["workspaces"]),
        client_id=app.get("client_id"),
        redirect_uri=app.get("redirect_uri") or raw.get("redirect_uri") or DEFAULT_REDIRECT_URI,
        source_path=cfg_path,
        source_stamp=_stamp(cfg_path),
    )
