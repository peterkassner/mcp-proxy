#!/usr/bin/env python3
"""
Smoke-test each mcp-proxy hub route over Streamable HTTP .../servers/<key>/mcp (what Cursor uses).

Flow per key: initialize -> notifications/initialized -> tools/list.

Supports both hub modes:
- stateful: initialize returns Mcp-Session-Id and follow-ups send it
- stateless: no session header is expected; follow-ups run without it

If tools/list is not implemented (-32601), fall back to resources/list so resource-only servers
(e.g. motion) still count as healthy mounts.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _post(url: str, payload: dict, *, session: str | None = None, timeout: int = 90) -> tuple[str | None, str]:
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        body = r.read().decode("utf-8", errors="replace")
    return sid, body


def _parse_json_message(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # last line wins for NDJSON-ish blobs
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _rpc_result(msg: dict | None) -> tuple[dict | None, dict | None]:
    if not msg:
        return None, {"code": -1, "message": "empty response"}
    if "error" in msg:
        return None, msg["error"]
    return msg.get("result"), None


def probe_route(base: str, key: str) -> tuple[bool, str]:
    url = f"{base.rstrip('/')}/servers/{key}/mcp"
    try:
        sid, raw = _post(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "verify_hub_routes_with_mcp", "version": "1"},
                },
            },
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        return False, f"HTTP {e.code} on initialize: {body}"
    except Exception as e:
        return False, f"{type(e).__name__} on initialize: {e}"

    session_mode = "stateful" if sid else "stateless"

    try:
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session=sid)
        _, raw_tl = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session=sid)
    except Exception as e:
        return False, f"after initialize ({session_mode}): {type(e).__name__}: {e}"

    msg = _parse_json_message(raw_tl)
    res, err = _rpc_result(msg)
    if err is None and isinstance(res, dict) and "tools" in res:
        n = len(res["tools"] or [])
        return True, f"tools={n} ({session_mode})"

    code = (err or {}).get("code")
    if code == -32601:
        try:
            _, raw_rl = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}, session=sid)
        except Exception as e:
            return False, f"tools/list N/A and resources/list failed: {e}"
        msg2 = _parse_json_message(raw_rl)
        res2, err2 = _rpc_result(msg2)
        if err2 is None and isinstance(res2, dict) and "resources" in res2:
            n = len(res2["resources"] or [])
            return True, f"tools=n/a (method not found); resources={n} (resource-only MCP, {session_mode})"
        return False, f"tools/list N/A; resources/list error={err2!r}"

    return False, f"tools/list error={err!r}"


def main() -> int:
    hub_json = os.environ.get("MCP_PROXY_HUB_JSON") or str(Path.home() / ".mcp" / "mcp-proxied-servers.json")
    base = os.environ.get("MCP_HUB_BASE_URL", "http://127.0.0.1:8096")

    path = Path(hub_json)
    if not path.is_file():
        print(f"[verify-mcp] hub JSON not found: {hub_json}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[verify-mcp] cannot read hub JSON: {e}", file=sys.stderr)
        return 1

    servers = data.get("mcpServers") or {}
    keys = sorted(servers.keys())
    if not keys:
        print("[verify-mcp] no mcpServers keys", file=sys.stderr)
        return 1

    fail = 0
    for k in keys:
        ok, detail = probe_route(base, k)
        url = f"{base.rstrip('/')}/servers/{k}/mcp"
        if ok:
            print(f"[verify-mcp] OK  {k:22} {detail:50} ({url})")
        else:
            print(f"[verify-mcp] FAIL {k:22} {detail} ({url})", file=sys.stderr)
            fail = 1
    return fail


if __name__ == "__main__":
    raise SystemExit(main())
