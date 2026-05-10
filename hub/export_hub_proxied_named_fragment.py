#!/usr/bin/env python3
"""
Build or extend ``hub-proxied.named-servers.fragment.json`` from ``deploy/mcp_servers_registry.json``.

Maps each selected server's ``instructions.cursor`` entry to an mcp-proxy **named child** definition
(stdio passthrough, or ``npx mcp-remote`` for headerless HTTP URLs).

**Removed:** registry ``mcp_hub.proxied`` and ``deploy/sync_mcp_servers.py --write-mcp-hub``. Pass explicit
``--server`` names (repeatable) and/or ``--names-file`` (one name per line).

Uses ``normalize_server`` from ``deploy/sync_mcp_servers.py`` only for Cursor-shaped JSON normalization.
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[3]
DEFAULT_REGISTRY = REPO / "deploy" / "mcp_servers_registry.json"
DEFAULT_OUT = SCRIPT.parent / "hub-proxied.named-servers.fragment.json"


def canonical_to_mcp_proxy_named_entry(_name: str, canon: dict[str, Any]) -> dict[str, Any] | None:
    """Project merged canonical server dict to mcp-proxy named-server child shape."""
    if canon.get("headers"):
        return None
    if canon.get("command"):
        out: dict[str, Any] = {"command": canon["command"]}
        if canon.get("args"):
            out["args"] = list(canon["args"])
        if canon.get("env") and isinstance(canon["env"], dict):
            out["env"] = {str(k): str(v) for k, v in canon["env"].items()}
        return out
    url = canon.get("url")
    if url:
        return {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                str(url),
                "--transport",
                "http-only",
            ],
        }
    return None


def collect_names(args: argparse.Namespace) -> list[str]:
    names: list[str] = []
    for n in args.server or []:
        n = str(n).strip()
        if n:
            names.append(n)
    if args.names_file is not None:
        p = args.names_file.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"names file not found: {p}")
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    # stable unique order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    p.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help=f"Registry JSON (default: {DEFAULT_REGISTRY})",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Write flat name→child object (default: {DEFAULT_OUT})",
    )
    p.add_argument(
        "--server",
        action="append",
        dest="server",
        default=[],
        metavar="NAME",
        help="Registry server name to export (repeatable).",
    )
    p.add_argument(
        "--names-file",
        type=Path,
        default=None,
        help="Path with one server name per line (# comments allowed).",
    )
    p.add_argument("--stdout", action="store_true", help="Print JSON to stdout instead of writing a file.")
    args = p.parse_args()

    try:
        names = collect_names(args)
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not names:
        print(
            "error: no server names — use --server NAME (repeatable) and/or --names-file",
            file=sys.stderr,
        )
        return 1

    reg_path = args.registry.expanduser().resolve()
    if not reg_path.is_file():
        print(f"Registry not found: {reg_path}", file=sys.stderr)
        return 1

    sync = runpy.run_path(str(REPO / "deploy" / "sync_mcp_servers.py"))
    normalize_server = sync["normalize_server"]

    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    servers = reg.get("servers") or {}

    out: dict[str, object] = {
        "_readme": (
            "hub-only backends for mcp-proxy. Merge into ~/.mcp/mcp-proxied-servers.json with "
            "deploy/mcp/mcp-proxy-hub/merge_hub_named_fragments.py (or maintain by hand). "
            "Omit _readme if mcp-proxy rejects unknown keys."
        )
    }
    skipped: list[tuple[str, str]] = []

    for name in names:
        spec = servers.get(name)
        if not isinstance(spec, dict):
            skipped.append((name, "no spec"))
            continue
        inst = spec.get("instructions") or {}
        raw = inst.get("cursor")
        if not isinstance(raw, dict):
            skipped.append((name, "no instructions.cursor"))
            continue
        try:
            canon = normalize_server(name, raw, "registry.cursor")
        except ValueError as e:
            skipped.append((name, f"normalize: {e}"))
            continue
        child = canonical_to_mcp_proxy_named_entry(name, canon)
        if child is None:
            skipped.append((name, "skipped (HTTP with headers?)"))
            continue
        out[name] = child

    if skipped:
        print("export: skipped:", skipped, file=sys.stderr)

    body = {k: v for k, v in out.items() if not str(k).startswith("_")}
    if not body:
        print("export: no hub children produced", file=sys.stderr)
        return 1

    text = json.dumps(out, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)

    if args.stdout:
        sys.stdout.write(text)
        return 0

    out_path = args.output.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"wrote {out_path} ({len(body)} servers, plus optional _readme)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
