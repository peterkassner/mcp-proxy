"""Create a local SSE server that proxies requests to a stdio MCP server."""

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server as MCPServerSDK  # Renamed to avoid conflict
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Receive, Scope, Send

from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)

# Vendor overlay for Mac hub diagnostics (mcp-proxy fork). See hub/apply_mcp_hub_diag_patch.py.

DEFAULT_EXPOSE_HEADERS: Final[tuple[str, ...]] = ("mcp-session-id",)


def _default_expose_headers() -> list[str]:
    return list(DEFAULT_EXPOSE_HEADERS)


@dataclass
class MCPServerSettings:
    """Settings for the MCP server."""

    bind_host: str
    port: int
    stateless: bool = False
    allow_origins: list[str] | None = None
    expose_headers: list[str] = field(default_factory=_default_expose_headers)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


_global_status: dict[str, Any] = {
    "api_last_activity": datetime.now(timezone.utc).isoformat(),
    "server_instances": {},
}


def _update_global_activity() -> None:
    _global_status["api_last_activity"] = datetime.now(timezone.utc).isoformat()


class _ASGIEndpointAdapter:
    """Wrap a coroutine function into an ASGI application."""

    def __init__(self, endpoint: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._endpoint = endpoint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._endpoint(scope, receive, send)


HTTP_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]


async def _handle_status(_: Request) -> Response:
    """Global health check and service usage monitoring endpoint."""
    return JSONResponse(_global_status)


def create_single_instance_routes(
    mcp_server_instance: MCPServerSDK[object],
    *,
    stateless_instance: bool,
) -> tuple[list[BaseRoute], StreamableHTTPSessionManager]:
    """Create Starlette routes and the HTTP session manager for a single MCP server instance."""
    logger.debug(
        "Creating routes for a single MCP server instance (stateless: %s)",
        stateless_instance,
    )

    sse_transport = SseServerTransport("/messages/")
    http_session_manager = StreamableHTTPSessionManager(
        app=mcp_server_instance,
        event_store=None,
        json_response=True,
        stateless=stateless_instance,
    )

    async def handle_sse_instance(request: Request) -> Response:
        async with sse_transport.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            _update_global_activity()
            await mcp_server_instance.run(
                read_stream,
                write_stream,
                mcp_server_instance.create_initialization_options(),
            )
        return Response()

    async def handle_streamable_http_instance(scope: Scope, receive: Receive, send: Send) -> None:
        _update_global_activity()
        updated_scope = scope
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path and path.rstrip("/") == "/mcp" and not path.endswith("/"):
                updated_scope = dict(scope)
                normalized_path = path + "/"
                logger.debug(
                    "Normalized request path from '%s' to '%s' without redirect",
                    path,
                    normalized_path,
                )
                updated_scope["path"] = normalized_path

                raw_path = scope.get("raw_path")
                if raw_path:
                    if b"?" in raw_path:
                        path_part, query_part = raw_path.split(b"?", 1)
                        updated_scope["raw_path"] = path_part.rstrip(b"/") + b"/?" + query_part
                    else:
                        updated_scope["raw_path"] = raw_path.rstrip(b"/") + b"/"

        await http_session_manager.handle_request(updated_scope, receive, send)

    routes = [
        Route(
            "/mcp",
            endpoint=_ASGIEndpointAdapter(handle_streamable_http_instance),
            methods=HTTP_METHODS,
            include_in_schema=False,
        ),
        Mount("/mcp", app=handle_streamable_http_instance),
        Route("/sse", endpoint=handle_sse_instance),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
    return routes, http_session_manager


async def run_mcp_server(
    mcp_settings: MCPServerSettings,
    default_server_params: StdioServerParameters | None = None,
    named_server_params: dict[str, StdioServerParameters] | None = None,
) -> None:
    """Run stdio client(s) and expose an MCP server with multiple possible backends."""
    if named_server_params is None:
        named_server_params = {}

    all_routes: list[BaseRoute] = [
        Route("/status", endpoint=_handle_status),
    ]

    async with contextlib.AsyncExitStack() as stack:

        @contextlib.asynccontextmanager
        async def combined_lifespan(_app: Starlette) -> AsyncIterator[None]:
            logger.info("Main application lifespan starting...")
            yield
            logger.info("Main application lifespan shutting down...")

        if default_server_params:
            logger.info(
                "_%s_ [default] Setting up default server: %s %s",
                os.getpid(),
                default_server_params.command,
                " ".join(default_server_params.args),
            )
            stdio_streams = await stack.enter_async_context(
                stdio_client(default_server_params, server_label="default"),
            )
            session = await stack.enter_async_context(ClientSession(*stdio_streams))
            proxy = await create_proxy_server(session)

            instance_routes, http_manager = create_single_instance_routes(
                proxy,
                stateless_instance=mcp_settings.stateless,
            )
            await stack.enter_async_context(http_manager.run())
            all_routes.extend(instance_routes)
            _global_status["server_instances"]["default"] = "configured"

        # -- 2026-04-27, CC2: named-server fault isolation. One bad child (e.g. Qdrant import
        # error) used to abort the whole hub; now only that name is skipped, /status still comes
        # up, other MCP routes stay wired.
        named_runtime_stacks: list[contextlib.AsyncExitStack] = []
        wired_named: list[str] = []
        for name, params in named_server_params.items():
            logger.info(
                "_%s_ [%s] Setting up named server: %s %s",
                os.getpid(),
                name,
                params.command,
                " ".join(params.args),
            )
            srv_stack = contextlib.AsyncExitStack()
            await srv_stack.__aenter__()
            try:
                stdio_streams_named = await srv_stack.enter_async_context(
                    stdio_client(params, server_label=name),
                )
                session_named = await srv_stack.enter_async_context(ClientSession(*stdio_streams_named))
                proxy_named = await create_proxy_server(session_named)

                instance_routes_named, http_manager_named = create_single_instance_routes(
                    proxy_named,
                    stateless_instance=mcp_settings.stateless,
                )
                await srv_stack.enter_async_context(http_manager_named.run())

                server_mount = Mount(f"/servers/{name}", routes=instance_routes_named)
                all_routes.append(server_mount)
                _global_status["server_instances"][name] = "configured"
                wired_named.append(name)
                named_runtime_stacks.append(srv_stack)
            except Exception:
                logger.exception(
                    "_%s_ [%s] setup failed; skipping (hub continues with other servers)",
                    os.getpid(),
                    name,
                )
                _global_status["server_instances"][name] = "setup_failed"
                try:
                    await srv_stack.aclose()
                except Exception:
                    logger.exception(
                        "_%s_ [%s] cleanup after failed setup raised again",
                        os.getpid(),
                        name,
                    )
                continue

        if not default_server_params and not wired_named:
            if not named_server_params:
                logger.error("No servers configured to run.")
            else:
                logger.error("No named servers started successfully (all stdio setups failed).")
            return

        middleware: list[Middleware] = []
        if mcp_settings.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=mcp_settings.allow_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                    expose_headers=mcp_settings.expose_headers,
                ),
            )

        starlette_app = Starlette(
            debug=(mcp_settings.log_level == "DEBUG"),
            routes=all_routes,
            middleware=middleware,
            lifespan=combined_lifespan,
        )

        starlette_app.router.redirect_slashes = False

        config = uvicorn.Config(
            starlette_app,
            host=mcp_settings.bind_host,
            port=mcp_settings.port,
            log_level=mcp_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)

        base_url = f"http://{mcp_settings.bind_host}:{mcp_settings.port}"
        sse_urls = []

        if default_server_params:
            sse_urls.append(f"{base_url}/sse")

        sse_urls.extend([f"{base_url}/servers/{name}/sse" for name in wired_named])

        if sse_urls:
            logger.info("Serving MCP Servers via SSE:")
            for url in sse_urls:
                logger.info("  - %s", url)

        logger.debug(
            "Serving incoming MCP requests on %s:%s",
            mcp_settings.bind_host,
            mcp_settings.port,
        )

        try:
            await http_server.serve()
        finally:
            # CC2 2026-04-27: LIFO shutdown of per-name stacks after uvicorn stops
            for ns in reversed(named_runtime_stacks):
                try:
                    await ns.aclose()
                except Exception:
                    logger.exception("Error while closing a named server AsyncExitStack")
