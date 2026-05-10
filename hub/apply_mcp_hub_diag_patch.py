#!/usr/bin/env python3
"""
Copy vendor overlay modules into the active ``uv tool`` mcp-proxy site-packages.

Overlays add:
  - ``_<hub-pid>_ [<json-key>] Setting up named server: …`` hub log lines
  - ``_<child-pid>_ [<json-key>] …`` prefixes on each named child's stderr (POSIX)
  - skip a named server if stdio spawn fails so the hub can still bind and serve others

Re-run after ``uv tool upgrade mcp-proxy`` if ``--check`` fails (version mismatch stops apply).

Usage::

  python3 deploy/mcp/mcp-proxy-hub/apply_mcp_hub_diag_patch.py
  python3 deploy/mcp/mcp-proxy-hub/apply_mcp_hub_diag_patch.py --check
  python3 deploy/mcp/mcp-proxy-hub/apply_mcp_hub_diag_patch.py --force
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Overlays were produced against this mcp-proxy release; mismatch risks API skew.
EXPECTED_MCP_PROXY_VERSION = "0.11.0"


def _mcp_proxy_tool_root() -> Path:
    which = shutil.which("mcp-proxy")
    if not which:
        raise FileNotFoundError("mcp-proxy not on PATH (install with: uv tool install mcp-proxy)")
    return Path(which).resolve().parent.parent


def _tool_python() -> Path:
    root = _mcp_proxy_tool_root()
    py = root / "bin" / "python"
    if not py.is_file():
        raise FileNotFoundError(f"expected tool venv python at {py}")
    return py


def _installed_mcp_proxy_version() -> str:
    py = _tool_python()
    out = subprocess.check_output(
        [str(py), "-c", "import importlib.metadata as m; print(m.version('mcp-proxy'))"],
        text=True,
    ).strip()
    return out


def _site_packages() -> Path:
    root = _mcp_proxy_tool_root()
    lib = root / "lib"
    if not lib.is_dir():
        raise FileNotFoundError(f"expected {lib} (uv tool layout)")
    cands = sorted(lib.glob("python*/site-packages"))
    if not cands:
        raise FileNotFoundError(f"no python*/site-packages under {lib}")
    return cands[0]


def _overlay_dir() -> Path:
    return Path(__file__).resolve().parent / "vendor-patch-overlays"


def apply_overlay(*, force: bool) -> None:
    ver = _installed_mcp_proxy_version()
    if ver != EXPECTED_MCP_PROXY_VERSION and not force:
        raise SystemExit(
            f"mcp-proxy is {ver!r}, overlays target {EXPECTED_MCP_PROXY_VERSION!r}. "
            "Upgrade overlays or pass --force after verifying MCP APIs still match."
        )
    site = _site_packages()
    overlay = _overlay_dir()
    pairs = [
        (overlay / "mcp" / "client" / "stdio" / "__init__.py", site / "mcp" / "client" / "stdio" / "__init__.py"),
        (overlay / "mcp_proxy" / "mcp_server.py", site / "mcp_proxy" / "mcp_server.py"),
    ]
    for src, dst in pairs:
        if not src.is_file():
            raise FileNotFoundError(f"overlay missing: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"patched: {dst}")


def check_overlay() -> int:
    try:
        ver = _installed_mcp_proxy_version()
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"check: FAIL ({e})")
        return 1
    site = _site_packages()
    stdio = (site / "mcp" / "client" / "stdio" / "__init__.py").read_text(encoding="utf-8", errors="replace")
    server = (site / "mcp_proxy" / "mcp_server.py").read_text(encoding="utf-8", errors="replace")
    ok = ver == EXPECTED_MCP_PROXY_VERSION
    ok = ok and "server_label: str | None = None" in stdio
    ok = ok and "wired_named: list[str] = []" in server
    if ok:
        print(f"check: OK (mcp-proxy {ver}, hub diag overlays present)")
        return 0
    print(
        f"check: FAIL (mcp-proxy {ver}; expected {EXPECTED_MCP_PROXY_VERSION}, "
        "or overlays not applied — run apply_mcp_hub_diag_patch.py)",
    )
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify version + overlay markers only")
    ap.add_argument(
        "--force",
        action="store_true",
        help=f"apply even if mcp-proxy version != {EXPECTED_MCP_PROXY_VERSION}",
    )
    args = ap.parse_args()
    if args.check:
        raise SystemExit(check_overlay())
    apply_overlay(force=args.force)
    raise SystemExit(check_overlay())


if __name__ == "__main__":
    main()
