"""Find the Slack workspaces this Mac is signed in to.

Slack has no public API that lists a person's workspaces, but the desktop app
keeps a plain list of them. This reads only each workspace's id, name, and URL
from that file. It never opens Slack's cookie or token stores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

STATE_FILES = (
    # Mac App Store build, then the direct-download build.
    "~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack/storage/root-state.json",
    "~/Library/Application Support/Slack/storage/root-state.json",
)


@dataclass(frozen=True)
class Detected:
    team_id: str
    name: str
    domain: str | None


def detect_workspaces(paths: tuple[str, ...] = STATE_FILES) -> list[Detected]:
    found: dict[str, Detected] = {}
    for path in paths:
        try:
            data = json.loads(Path(path).expanduser().read_text())
        except (OSError, ValueError):
            continue  # not installed here, or macOS said no: detection is a convenience
        for team_id, ws in (data.get("workspaces") or {}).items():
            if not isinstance(ws, dict):
                continue
            host = urlparse(ws.get("url") or "").hostname or ""
            found[team_id] = Detected(
                team_id=team_id,
                name=ws.get("name") or team_id,
                domain=host.split(".")[0] or None,
            )
    return sorted(found.values(), key=lambda d: d.name.lower())
