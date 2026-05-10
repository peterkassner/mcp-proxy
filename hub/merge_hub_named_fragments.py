#!/usr/bin/env python3
"""
Merge hub fragment JSON files into ~/.mcp/mcp-proxied-servers.json for mcp-proxy --named-server-config.

Previously ``deploy/sync_mcp_servers.py --write-mcp-hub`` performed this merge; that pathway was removed so
MCP app sync never touches hub disk state.

Typical:

  python3 deploy/mcp/mcp-proxy-hub/merge_hub_named_fragments.py --dry-run
  python3 deploy/mcp/mcp-proxy-hub/merge_hub_named_fragments.py

Overlapping server names between the two fragments are an error (except ignored keys starting with '_').
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[3]

DEFAULT_HUB_PX = REPO / "deploy" / "mcp" / "mcp-proxy-hub" / "hub-proxied.named-servers.fragment.json"
DEFAULT_BRIDGE = REPO / "deploy" / "mcp" / "Mac<>PC-Docker-bridge" / "bridge-only.named-servers.fragment.json"
DEFAULT_OUT = Path.home() / ".mcp" / "mcp-proxied-servers.json"


def load_fragment(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: root must be object")
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


def merge_named_maps(a: dict[str, object], b: dict[str, object], *, label_a: str, label_b: str) -> dict[str, object]:
    overlap = set(a) & set(b)
    if overlap:
        raise ValueError(
            f"duplicate server name(s) in fragments: {sorted(overlap)!r} "
            f"({label_a} vs {label_b})"
        )
    return {**a, **b}


def write_json_atomic(path: Path, obj: object, *, dry_run: bool) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)
    if dry_run:
        print(f"  [dry-run] would write {path} ({len(text)} bytes)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        suffix=".json.tmp", prefix="mcp-hub-merge-", dir=str(path.parent), text=True
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    p.add_argument(
        "--hub-proxied",
        type=Path,
        default=DEFAULT_HUB_PX,
        help=f"Hub-only fragment (default: {DEFAULT_HUB_PX})",
    )
    p.add_argument(
        "--bridge",
        type=Path,
        default=DEFAULT_BRIDGE,
        help=f"Docker bridge fragment (default: {DEFAULT_BRIDGE} if present)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path (default: {DEFAULT_OUT})",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--skip-missing-bridge",
        action="store_true",
        help="If the bridge fragment path does not exist, merge hub-proxied only.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    hub_path = args.hub_proxied.expanduser().resolve()
    out_path = args.output.expanduser().resolve()

    try:
        hub_map = load_fragment(hub_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Error loading hub fragment: {e}", file=sys.stderr)
        return 1

    bridge_path = args.bridge.expanduser().resolve()
    if bridge_path.is_file():
        try:
            bridge_map = load_fragment(bridge_path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Error loading bridge fragment: {e}", file=sys.stderr)
            return 1
        try:
            merged = merge_named_maps(
                hub_map,
                bridge_map,
                label_a=str(hub_path.name),
                label_b=str(bridge_path.name),
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    elif args.skip_missing_bridge:
        merged = dict(hub_map)
        print(f"  note: bridge fragment missing; using {hub_path.name} only", file=sys.stderr)
    else:
        print(
            f"Error: bridge fragment not found: {bridge_path}\n"
            "  Use --skip-missing-bridge to write hub-only, or copy the example beside it.",
            file=sys.stderr,
        )
        return 1

    obj = {"mcpServers": merged}
    write_json_atomic(out_path, obj, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"wrote {out_path} ({len(merged)} named server(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
