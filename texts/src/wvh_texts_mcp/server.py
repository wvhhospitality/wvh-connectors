"""WvH Texts: the connection to Claude. Read-only, with no send tool, ever.

Only the transport lives here; every rule is in tools.py. Logs carry tool names and
error types, never message text.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool, ToolAnnotations

from . import tools
from .config import DEFAULT_INSTALL, ConfigError, install_name, load_config

LOG_LEVEL = os.environ.get("WVH_TEXTS_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wvh_texts_mcp")

# The databases are local files, but a slow read still shouldn't block the event loop.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wvh-texts-io")


def _tool(spec: dict[str, Any]) -> Tool:
    return Tool(
        name=spec["name"],
        description=spec["description"],
        inputSchema={"type": "object", "properties": spec["properties"], "required": spec.get("required", [])},
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )


TOOLS = [_tool(spec) for spec in tools.TOOLS]


def _render(result: dict[str, Any] | tools.Picture) -> list[TextContent | ImageContent]:
    if isinstance(result, tools.Picture):
        return [
            TextContent(type="text", text=json.dumps(result.meta, ensure_ascii=False, indent=2)),
            ImageContent(type="image", data=base64.b64encode(result.data).decode(), mimeType=result.mime),
        ]
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def build_server() -> Server:
    try:
        name = install_name()
        owner = load_config().owner_name
    except ConfigError as exc:
        log.error("Settings problem: %s", exc)
        name, owner = DEFAULT_INSTALL, "the owner"
    server = Server(name, instructions=tools.instructions(owner))

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent | ImageContent]:
        if name not in tools.TOOL_NAMES:
            return [TextContent(type="text", text=json.dumps({"error": "unknown_tool", "detail": f"There is no {name} here. This connector only reads; nothing was sent or changed."}))]
        loop = asyncio.get_running_loop()
        try:
            return _render(await loop.run_in_executor(_executor, tools.call, name, arguments or {}))
        except (ConfigError, tools.TextsError) as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        except Exception as exc:  # noqa: BLE001
            # The type only. An exception's text could quote a message.
            log.error("Unhandled %s in %s", type(exc).__name__, name)
            return [TextContent(type="text", text=json.dumps({"error": f"Internal error in {name}. Check the connector's log."}))]

    return server


def main() -> None:
    async def _run() -> None:
        server = build_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
