"""GUI dashboard router for the mcp-proxy hub.

Mounted at /gui by run_mcp_server in mcp_server.py.
All /gui/api/* endpoints are also the Raycast extension API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

GUI_STATIC_DIR = pathlib.Path(__file__).parent / "gui_static"
CONFIG_PATH = pathlib.Path.home() / ".mcp" / "mcp-proxied-servers.json"
# gui_router.py → src/mcp_proxy/ → src/ → repo root → hub/
HUB_DIR = pathlib.Path(__file__).parent.parent.parent / "hub"


# ---------------------------------------------------------------------------
# MCP probe helpers
# ---------------------------------------------------------------------------

_MCP_TIMEOUT = httpx.Timeout(10.0)
_INIT_PAYLOAD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "gui-dashboard", "version": "0.1"},
    },
}
_INITIALIZED_PAYLOAD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {},
}
_TOOLS_LIST_PAYLOAD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
}
_RESOURCES_LIST_PAYLOAD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "resources/list",
    "params": {},
}


async def _probe_server_tools(hub_base_url: str, server_name: str) -> dict[str, Any]:
    """MCP initialize + tools/list (or resources/list fallback) for one named server."""
    mcp_url = f"{hub_base_url}/servers/{server_name}/mcp"
    headers = {"Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_MCP_TIMEOUT) as client:
            # 1. initialize
            r = await client.post(mcp_url, json=_INIT_PAYLOAD, headers=headers)
            if r.status_code not in (200, 201, 202):
                return {"error": f"initialize HTTP {r.status_code}"}

            session_id = r.headers.get("mcp-session-id", "")
            if session_id:
                headers = {**headers, "mcp-session-id": session_id}

            # 2. notifications/initialized
            await client.post(mcp_url, json=_INITIALIZED_PAYLOAD, headers=headers)

            # 3. tools/list
            r2 = await client.post(mcp_url, json=_TOOLS_LIST_PAYLOAD, headers=headers)
            body = r2.json()
            error_code = (body.get("error") or {}).get("code")

            if error_code == -32601:
                # Method not found — try resources/list
                r3 = await client.post(
                    mcp_url, json=_RESOURCES_LIST_PAYLOAD, headers=headers
                )
                body = r3.json()
                resources = (body.get("result") or {}).get("resources", [])
                return {"tools": [], "resources": resources, "resource_count": len(resources)}

            tools = (body.get("result") or {}).get("tools", [])
            return {"tools": tools, "tool_count": len(tools)}

    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------


def _read_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def create_gui_router(global_status: dict[str, Any], hub_base_url: str) -> Router:
    """Return a Starlette Router ready to be mounted at /gui."""

    async def index(request: Request) -> Response:
        html_file = GUI_STATIC_DIR / "dashboard.html"
        if html_file.exists():
            return FileResponse(str(html_file), media_type="text/html")
        return Response("<h1>GUI not found</h1>", media_type="text/html", status_code=404)

    async def api_status(request: Request) -> JSONResponse:
        instances: dict[str, str] = global_status.get("server_instances", {})
        counts: dict[str, int] = {"configured": 0, "setup_failed": 0, "other": 0}
        for state in instances.values():
            if state == "configured":
                counts["configured"] += 1
            elif state == "setup_failed":
                counts["setup_failed"] += 1
            else:
                counts["other"] += 1
        return JSONResponse(
            {
                "hub_url": hub_base_url,
                "api_last_activity": global_status.get("api_last_activity"),
                "server_instances": instances,
                "summary": counts,
            }
        )

    async def api_servers(request: Request) -> JSONResponse:
        config = _read_config()
        instances: dict[str, str] = global_status.get("server_instances", {})
        named = config.get("mcpServers", {}) if isinstance(config, dict) else {}
        servers = []
        seen = set()
        for name, cfg in named.items():
            seen.add(name)
            servers.append(
                {
                    "name": name,
                    "status": instances.get(name, "unknown"),
                    "config": cfg,
                }
            )
        # Include any runtime-known servers not in config
        for name, state in instances.items():
            if name not in seen:
                servers.append({"name": name, "status": state, "config": None})
        return JSONResponse({"servers": servers, "count": len(servers)})

    async def api_server_tools(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        result = await _probe_server_tools(hub_base_url, name)
        return JSONResponse({"server": name, **result})

    async def api_server_restart(request: Request) -> JSONResponse:
        """Fire-and-forget restart via hub/restart.zsh (runs in background)."""
        restart_script = HUB_DIR / "restart.zsh"
        if not restart_script.exists():
            return JSONResponse(
                {"error": f"restart.zsh not found at {restart_script}"}, status_code=500
            )

        async def _run_restart() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "/bin/zsh",
                    str(restart_script),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await proc.wait()
            except Exception as exc:  # noqa: BLE001
                logger.error("restart.zsh failed: %s", exc)

        # Schedule restart after response is sent
        asyncio.ensure_future(_run_restart())
        return JSONResponse(
            {
                "status": "restarting",
                "message": "Restart initiated via restart.zsh. Hub will be unavailable briefly.",
            }
        )

    async def api_config_get(request: Request) -> JSONResponse:
        return JSONResponse(_read_config())

    async def api_config_put(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"invalid JSON: {exc}"}, status_code=400)

        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(body, indent=2))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"write failed: {exc}"}, status_code=500)

        # Run fragment merge if script exists
        merge_script = HUB_DIR / "merge_hub_named_fragments.py"
        if merge_script.exists():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python3",
                    str(merge_script),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode != 0:
                    logger.warning(
                        "merge_hub_named_fragments.py exit %s: %s",
                        proc.returncode,
                        stdout.decode(errors="replace"),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("merge_hub_named_fragments.py failed: %s", exc)

        return JSONResponse({"status": "saved", "path": str(CONFIG_PATH)})

    routes = [
        Route("/", endpoint=index),
        Route("/api/status", endpoint=api_status),
        Route("/api/servers", endpoint=api_servers),
        Route("/api/servers/{name}/tools", endpoint=api_server_tools),
        Route("/api/servers/{name}/restart", endpoint=api_server_restart, methods=["POST"]),
        Route("/api/config", endpoint=api_config_get),
        Route("/api/config", endpoint=api_config_put, methods=["PUT"]),
        Mount("/static", app=StaticFiles(directory=str(GUI_STATIC_DIR)), name="gui_static"),
    ]
    return Router(routes=routes)
