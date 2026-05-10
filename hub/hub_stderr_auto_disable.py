#!/usr/bin/env python3
"""
Scan recent mcp-proxy stderr for per-server failure blocks and remove failing names
from hub ``mcpServers`` in ``~/.mcp/mcp-proxied-servers.json``. Hub child definitions also live in
``hub-proxied.named-servers.fragment.json`` — remove failing names there by hand so they are not restored
on the next merge.

mcp-proxy wires named servers before ``uvicorn.serve()`` (with optional vendor overlays
that skip a name when **stdio spawn** fails so the hub can still listen). A **hang**
during setup (no exception) or a failure **after** stdio is entered can still block
the hub; this script trims names from recent log **failure blocks** so the next restart
can proceed.

Usage (repo root or absolute paths):

  python3 deploy/mcp/mcp-proxy-hub/hub_stderr_auto_disable.py --dry-run
  python3 deploy/mcp/mcp-proxy-hub/hub_stderr_auto_disable.py --kickstart
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()

# mcp_proxy.mcp_server logs one of these before stdio_client() for each named backend
# (legacy quoted name, or hub-diag ``_<pid>_ [<key>] Setting up named server``).
SETUP_RE_LEGACY = re.compile(r"Setting up named server '([^']+)'")
SETUP_RE_DIAG = re.compile(r"_\d+_ \[([^\]]+)\] Setting up named server")

# Lines inside a server block that indicate the hub should drop this name.
FAIL_LINE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bERROR\b"),
    re.compile(r"\bCRITICAL\b"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"ExceptionGroup:"),
    re.compile(r"\bValueError\b"),
    re.compile(r"BrokenPipeError"),
    re.compile(r"Missing required environment"),
    re.compile(r"Failed to load server configurations"),
    re.compile(r"ECONNREFUSED"),
    re.compile(r"Fatal error", re.IGNORECASE),
    re.compile(r"unhandled errors in a TaskGroup", re.IGNORECASE),
)


def default_stderr_path() -> Path:
    env = os.environ.get("MCP_PROXY_STDERR_LOG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for name in ("mcp-proxy-hub.log", "stderr.log"):
        cand = SCRIPT.parent / "logs" / name
        if cand.is_file():
            return cand.resolve()
    return (
        Path.home()
        / "Library"
        / "Logs"
        / "com.peterkjackson.mcp-proxy-bridge"
        / "mcp-proxy-hub.log"
    ).resolve()


def default_hub_path() -> Path:
    env = os.environ.get("MCP_PROXY_HUB_JSON", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".mcp" / "mcp-proxied-servers.json").expanduser().resolve()


def read_tail_text(path: Path, *, max_lines: int) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"stderr log not found: {path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    if len(lines) <= max_lines:
        return raw
    return "\n".join(lines[-max_lines:]) + ("\n" if lines else "")


def _setup_anchor_matches(log_text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for m in SETUP_RE_LEGACY.finditer(log_text):
        out.append((m.start(), m.group(1)))
    for m in SETUP_RE_DIAG.finditer(log_text):
        out.append((m.start(), m.group(1)))
    out.sort(key=lambda t: t[0])
    return out


def failed_server_names(log_text: str) -> set[str]:
    """
    Split log into blocks starting at each named-server setup line (legacy or hub-diag).
    If a block contains a failure signature, that server name is flagged.
    """
    matches = _setup_anchor_matches(log_text)
    if not matches:
        return set()
    failed: set[str] = set()
    for i, (start, name) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(log_text)
        block = log_text[start:end]
        if not _block_has_failure(block):
            continue
        # Ignore global config errors that are not tied to a specific named child.
        if "Missing 'mcpServers' key" in block or "Invalid config file format" in block:
            continue
        failed.add(name)
    return failed


def _block_has_failure(block: str) -> bool:
    for line in block.splitlines():
        for rx in FAIL_LINE_RES:
            if rx.search(line):
                return True
    return False


def load_hub(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "mcpServers" not in data:
        raise ValueError(f"{path}: expected JSON object with top-level 'mcpServers'")
    if not isinstance(data["mcpServers"], dict):
        raise ValueError(f"{path}: mcpServers must be an object")
    return data


def write_json_atomic(path: Path, obj: Any, *, dry_run: bool) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)
    if dry_run:
        print(f"  [dry-run] would write {path} ({len(text)} bytes)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        suffix=".json.tmp", prefix="mcp-hub-", dir=str(path.parent), text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp).replace(path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_file(path: Path, *, dry_run: bool) -> None:
    if not path.is_file():
        return
    bak = path.with_suffix(path.suffix + ".bak")
    if dry_run:
        print(f"  [dry-run] would copy {path} -> {bak}")
        return
    shutil.copy2(path, bak)


def patch_hub_remove_servers(hub_path: Path, remove: set[str], *, dry_run: bool) -> list[str]:
    data = load_hub(hub_path)
    servers: dict[str, Any] = data["mcpServers"]
    removed: list[str] = []
    for name in sorted(remove):
        if name in servers:
            del servers[name]
            removed.append(name)
    if not removed:
        return []
    backup_file(hub_path, dry_run=dry_run)
    write_json_atomic(hub_path, data, dry_run=dry_run)
    return removed


def maybe_kickstart(*, dry_run: bool) -> None:
    if dry_run:
        print("  [dry-run] would launchctl kickstart mcp-proxy-bridge")
        return
    uid = os.getuid()
    label = "com.peterkjackson.mcp-proxy-bridge"
    cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
    try:
        subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError as e:
        print(f"  warning: kickstart failed: {e}", file=sys.stderr)
        return
    print(f"  kickstart: {' '.join(cmd)}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Remove failing MCP names from hub JSON using recent mcp-proxy stderr.",
    )
    p.add_argument(
        "--stderr",
        type=Path,
        default=None,
        help=f"Hub stderr log (default: MCP_PROXY_STDERR_LOG or logs/stderr.log symlink or ~/Library/Logs/.../stderr.log)",
    )
    p.add_argument(
        "--hub",
        type=Path,
        default=None,
        help="Hub JSON path (default: MCP_PROXY_HUB_JSON or ~/.mcp/mcp-proxied-servers.json)",
    )
    p.add_argument(
        "--tail-lines",
        type=int,
        default=12_000,
        help="Only scan the last N lines of stderr (default: 12000).",
    )
    p.add_argument("--dry-run", action="store_true", help="Print actions without writing files.")
    p.add_argument(
        "--kickstart",
        action="store_true",
        help="Run launchctl kickstart after a successful hub write (ignored with --dry-run).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    stderr_p = (args.stderr or default_stderr_path()).expanduser().resolve()
    hub_p = (args.hub or default_hub_path()).expanduser().resolve()

    try:
        text = read_tail_text(stderr_p, max_lines=args.tail_lines)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    failed = failed_server_names(text)
    if not failed:
        print(f"No failing server blocks found in last {args.tail_lines} lines of {stderr_p}")
        return 0

    print(f"Detected failing server names from {stderr_p}:")
    for n in sorted(failed):
        print(f"  - {n}")

    if not hub_p.is_file():
        print(f"Hub JSON missing: {hub_p}", file=sys.stderr)
        return 1

    removed = patch_hub_remove_servers(hub_p, failed, dry_run=args.dry_run)
    if removed:
        print(f"Removed from hub mcpServers ({hub_p}): {', '.join(removed)}")
    else:
        print("No matching keys in hub mcpServers (already absent or names differ).")

    if args.kickstart and removed and not args.dry_run:
        maybe_kickstart(dry_run=False)
    elif args.kickstart and args.dry_run:
        maybe_kickstart(dry_run=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
