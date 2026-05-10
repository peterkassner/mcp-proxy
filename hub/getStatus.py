#!/usr/bin/env python3
"""
Poll mcp-proxy hub until GET /status succeeds or timeout.
On timeout: optional launchd full reload via restart.zsh (TTY prompt, --relaunch, or MCP_HUB_STATUS_AUTO_RELAUNCH=1).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import urllib.parse

WIDTH = 77


def _emit(msg: str, *, err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


def _pad_line(text: str, *, err: bool = False) -> None:
    line = text[:WIDTH]
    if len(text) > WIDTH:
        line = line[: WIDTH - 1] + "…"
    line = line + " " * (WIDTH - len(line))
    _emit("│ " + line + "│", err=err)


def _box(title_lines: list[str], *, err: bool = False) -> None:
    top = "┌" + "─" * WIDTH + "┐"
    bot = "└" + "─" * WIDTH + "┘"
    _emit(top, err=err)
    for t in title_lines:
        _pad_line(t, err=err)
    _emit(bot, err=err)


def _kickstart_hub(domain: str, label: str, plist: str) -> bool:
    _emit("")
    _emit(" ▸ launchd full reload via restart.zsh")
    restart_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart.zsh")
    if not os.path.isfile(restart_script):
        _emit(f"    ERROR: restart script missing: {restart_script}", err=True)
        return False

    env = os.environ.copy()
    env.setdefault("MCP_HUB_VERIFY_MCPTOOLS", "0")
    env.setdefault("MCP_PROXY_LAUNCHAGENT_PLIST", plist)
    _emit(f"    zsh {restart_script}")
    r = subprocess.run(["/bin/zsh", restart_script], env=env)
    if r.returncode != 0:
        _emit("   restart.zsh failed", err=True)
        return False
    _emit("   (restart.zsh completed — rechecking status …)")
    return True


def _want_relaunch(*, do_relaunch: bool) -> bool:
    if do_relaunch:
        return True
    v = (
        os.environ.get("MCP_HUB_STATUS_AUTO_RELAUNCH")
        or os.environ.get("MCP_HUB_STATUS_AUTO_RELUNCH")
        or ""
    ).strip().lower()
    if v in ("1", "yes", "true", "on"):
        return True
    if sys.stdin.isatty() and sys.stdout.isatty():
        _emit("")
        try:
            ans = input("Relaunch mcp-proxy via restart.zsh? [y/N] ")
        except EOFError:
            return False
        _emit("")
        return ans.strip().lower().startswith("y")
    return False


def _poll(status_url: str, interval: int, max_wait: int) -> str | None:
    elapsed = 0
    attempt = 1
    while elapsed < max_wait:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"  {ts:19}  try {attempt}   elapsed {elapsed:3d}s / {max_wait:3d}s   …",
            flush=True,
        )
        try:
            req = urllib.request.Request(status_url, method="GET")
            with urllib.request.urlopen(req, timeout=7) as resp:
                raw = resp.read().decode()
            json.loads(raw)
            return raw
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
            _emit("           (no response — will retry)")
            _emit("")
        if elapsed + interval >= max_wait:
            break
        time.sleep(interval)
        elapsed += interval
        attempt += 1
    return None


def _show_success(body: str, status_url: str) -> None:
    d = json.loads(body)
    _emit("")
    _box(["hub HTTP: UP", status_url])
    _emit("")
    print(json.dumps(d, indent=2, ensure_ascii=False))
    print("")
    print("── summary " + "─" * 61)
    print(f"  api_last_activity   {d.get('api_last_activity', 'n/a')}")
    si = d.get("server_instances") or {}
    names = sorted(si.keys())
    print(f"  backends            {len(names)} configured")
    if names:
        joined = ", ".join(names)
        print(
            textwrap.fill(
                joined,
                width=70,
                initial_indent="  • ",
                subsequent_indent="    ",
                break_long_words=False,
                break_on_hyphens=False,
            )
        )


def _verify_enabled() -> bool:
    v = os.environ.get("MCP_HUB_VERIFY_MCPTOOLS", "1").strip().lower()
    return v in ("1", "yes", "true", "on")


def _parse_json_message(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
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


def _post_mcp(url: str, payload: dict, *, session: str | None = None, timeout: int = 20) -> tuple[str | None, str]:
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        body = resp.read().decode("utf-8", errors="replace")
    return sid, body


def _probe_mcp_route(base_url: str, key: str) -> tuple[bool, str]:
    route = f"{base_url.rstrip('/')}/servers/{key}/mcp"
    try:
        sid, _ = _post_mcp(
            route,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "getStatus.py", "version": "1"},
                },
            },
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:240]
        return False, f"HTTP {e.code} on initialize: {body}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__} on initialize: {e}"

    # Initialize succeeded; continue probing even without explicit mcp-session-id header
    # (some routes handle sessions implicitly)
    try:
        if sid:
            _post_mcp(route, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session=sid)
            _, tools_raw = _post_mcp(route, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session=sid)
        else:
            # Try without explicit session ID
            _, tools_raw = _post_mcp(route, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    except Exception as e:  # noqa: BLE001
        return False, f"after initialize: {type(e).__name__}: {e}"

    tools_msg = _parse_json_message(tools_raw)
    tools_res, tools_err = _rpc_result(tools_msg)
    if tools_err is None and isinstance(tools_res, dict) and "tools" in tools_res:
        return True, f"tools={len(tools_res.get('tools') or [])}"

    if (tools_err or {}).get("code") == -32601:
        try:
            if sid:
                _, resources_raw = _post_mcp(
                    route,
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
                    session=sid,
                )
            else:
                _, resources_raw = _post_mcp(
                    route,
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
                )
        except Exception as e:  # noqa: BLE001
            return False, f"tools/list N/A and resources/list failed: {e}"
        resources_msg = _parse_json_message(resources_raw)
        resources_res, resources_err = _rpc_result(resources_msg)
        if resources_err is None and isinstance(resources_res, dict) and "resources" in resources_res:
            n = len(resources_res.get("resources") or [])
            return True, f"tools=n/a (method not found); resources={n} (resource-only MCP)"
        return False, f"tools/list N/A; resources/list error={resources_err!r}"

    return False, f"tools/list error={tools_err!r}"


def _verify_mcp_routes(body: str, status_url: str) -> bool:
    d = json.loads(body)
    instances = d.get("server_instances") or {}
    keys = sorted(instances.keys())
    if not keys:
        _emit("── MCP route verification skipped: no server_instances in /status")
        return True

    parts = urllib.parse.urlsplit(status_url)
    base_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    _emit("")
    _emit("── MCP route verification (tools availability) " + "─" * 36)
    _emit(f"  base {base_url}")
    _emit(f"  routes {len(keys)}")

    failures = 0
    for key in keys:
        ok, detail = _probe_mcp_route(base_url, key)
        route = f"{base_url.rstrip('/')}/servers/{key}/mcp"
        if ok:
            _emit(f"  OK   {key:22} {detail} ({route})")
        else:
            failures += 1
            _emit(f"  FAIL {key:22} {detail} ({route})", err=True)

    if failures:
        _emit("")
        _box(
            [
                "hub MCP routes: DEGRADED",
                f"{failures} / {len(keys)} route(s) unusable for agent tools/resources",
            ],
            err=True,
        )
        return False

    _emit("")
    _box(["hub MCP routes: OK", f"all {len(keys)} route(s) passed initialize + tools/resources checks"])
    return True


def main() -> int:
    p = argparse.ArgumentParser(
        description="Poll mcp-proxy GET /status until success or timeout; optional launchd relaunch."
    )
    p.add_argument(
        "--relaunch",
        action="store_true",
        help="On timeout, relaunch LaunchAgent without prompting",
    )
    args = p.parse_args()

    status_url = os.environ.get("MCP_HUB_STATUS_URL", "http://127.0.0.1:8096/status")
    interval = int(os.environ.get("MCP_HUB_STATUS_INTERVAL_SEC", "5"))
    max_wait = int(os.environ.get("MCP_HUB_STATUS_TIMEOUT_SEC", "120"))
    label = "com.peterkjackson.mcp-proxy-bridge"
    domain = f"gui/{os.getuid()}"
    plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")

    _emit("")
    _box(
        [
            "mcp-proxy hub — HTTP status probe",
            f"GET {status_url}",
            f"every {interval}s · timeout {max_wait}s",
        ]
    )
    _emit("")

    body = _poll(status_url, interval, max_wait)
    if body is not None:
        _show_success(body, status_url)
        if _verify_enabled() and not _verify_mcp_routes(body, status_url):
            return 2
        return 0

    _emit("", err=True)
    _box(
        [
            "hub HTTP: DOWN — timeout",
            f"no response from {status_url}",
            f"within {max_wait}s",
        ],
        err=True,
    )

    if not _want_relaunch(do_relaunch=args.relaunch):
        return 1

    if not _kickstart_hub(domain, label, plist):
        return 1

    _emit("")
    _emit("── Re-probing after kickstart (same timeout window) ──")
    _emit("")

    body = _poll(status_url, interval, max_wait)
    if body is not None:
        _show_success(body, status_url)
        if _verify_enabled() and not _verify_mcp_routes(body, status_url):
            return 2
        return 0

    _emit("", err=True)
    _box(
        [
            "hub still DOWN after kickstart",
            f"check: tail ~/Library/Logs/{label}/stderr.log",
        ],
        err=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
