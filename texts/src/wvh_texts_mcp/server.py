"""WvH Texts: the connection to Claude.

Only the transport lives here; every rule is in tools.py and sending.py. Logs carry
tool names and error types, never message text.

    wvh-texts-mcp                 the connector on the owner's Mac (stdio)
    python -m wvh_texts_mcp.server   the same thing
    wvh-texts-remote              the read-only door for a Mac that runs scheduled jobs (remote.py)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool, ToolAnnotations

from . import tools
from .config import DEFAULT_INSTALL, Config, ConfigError, install_name, load_config

LOG_LEVEL = os.environ.get("WVH_TEXTS_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wvh_texts_mcp")

# The databases are local files, but a slow read still shouldn't block the event loop.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wvh-texts-io")


def _tool(spec: dict[str, Any]) -> Tool:
    sends = spec["name"] == "confirm_send"
    return Tool(
        name=spec["name"],
        description=spec["description"],
        inputSchema={"type": "object", "properties": spec["properties"], "required": spec.get("required", [])},
        annotations=ToolAnnotations(readOnlyHint=not sends, destructiveHint=False, openWorldHint=sends),
    )


def _render(result: dict[str, Any] | tools.Picture) -> list[TextContent | ImageContent]:
    if isinstance(result, tools.Picture):
        return [
            TextContent(type="text", text=json.dumps(result.meta, ensure_ascii=False, indent=2)),
            ImageContent(type="image", data=base64.b64encode(result.data).decode(), mimeType=result.mime),
        ]
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def _config(remote: bool) -> Config:
    config = load_config()
    if remote and not config.clients_only:
        from dataclasses import replace

        config = replace(config, scope="clients")
    return config


def build_server(remote: bool = False) -> Server:
    """remote=True: the read-only door. Clients-only scope and no sending, whatever the settings say."""
    try:
        name = install_name()
        config = _config(remote)
        owner, can_send, narrow = config.owner_name, config.can_send and not remote, config.clients_only
    except ConfigError as exc:
        log.error("Settings problem: %s", exc)
        name, owner, can_send, narrow = DEFAULT_INSTALL, "the owner", False, True
    server = Server(name, instructions=tools.instructions(owner, can_send, narrow))

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        try:
            specs = tools.tool_specs(_config(remote), remote)
        except ConfigError:
            specs = tools.READ_TOOLS
        return [_tool(spec) for spec in specs]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent | ImageContent]:
        loop = asyncio.get_running_loop()
        try:
            return _render(await loop.run_in_executor(_executor, partial(tools.call, name, arguments or {}, remote)))
        except (ConfigError, tools.TextsError) as exc:
            unknown = str(exc).startswith("There is no ")
            return [TextContent(type="text", text=json.dumps({"error": "unknown_tool" if unknown else str(exc), **({"detail": str(exc)} if unknown else {})}))]
        except Exception as exc:  # noqa: BLE001
            # The type only. An exception's text could quote a message.
            log.error("Unhandled %s in %s", type(exc).__name__, name)
            return [TextContent(type="text", text=json.dumps({"error": f"Internal error in {name}. Check the connector's log."}))]

    return server


def create_server(remote: bool = False) -> Server:
    """The older name, kept so existing launchers keep working."""
    return build_server(remote)


def main() -> None:
    async def _run() -> None:
        server = build_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
