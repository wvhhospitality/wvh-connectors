"""The read-only door: for a Mac that runs scheduled jobs, a phone, or a shared door.

    wvh-texts-remote                 streamable HTTP on 127.0.0.1 (port 27128, or WVH_TEXTS_PORT)
    wvh-texts-remote --stdio         the same rules over stdio, for a Claude app on that Mac
    python -m wvh_texts_mcp.remote   the same as wvh-texts-remote

Whatever the settings say, the door is clients-only and can't send: every tool reaches
only the people and group chats on the list, and nobody else can even be confirmed to
exist. It listens on this Mac only. Reaching it from elsewhere is up to you, and should
sit behind its own sign-in. Each request stands alone, so there are no sessions to keep.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

from mcp.server.stdio import stdio_server

from .server import build_server

HOST = "127.0.0.1"
PORT = 27128


def port() -> int:
    try:
        return int(os.environ.get("WVH_TEXTS_PORT") or PORT)
    except ValueError:
        return PORT


def build_app(listen_port: int | None = None):
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Mount

    listen_port = listen_port or port()
    manager = StreamableHTTPSessionManager(
        app=build_server(remote=True),
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{HOST}:{listen_port}"],
            allowed_origins=[],  # a door in front sends none; a web page always does
        ),
    )

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)


def _stdio() -> None:
    async def _run() -> None:
        server = build_server(remote=True)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(_run())


def main() -> None:
    if "--stdio" in sys.argv[1:]:
        _stdio()
        return
    import uvicorn

    uvicorn.run(build_app(), host=HOST, port=port(), log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
