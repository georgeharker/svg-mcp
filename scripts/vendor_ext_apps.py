#!/usr/bin/env python3
"""Vendor the ``@modelcontextprotocol/ext-apps`` SDK inline for the ``show_widget`` MCP-App.

We deliberately vendor a copy rather than a git submodule or a runtime CDN load:

* The widget needs the **built** ``app-with-deps`` bundle, not the ext-apps *source*, so a submodule
  of the ext-apps repo would still require a JS build toolchain we don't want in a Python package.
* Loading from a CDN (unpkg/jsdelivr) at render time is blocked by the host's iframe CSP — the
  widget must be a single self-contained inline module.

So this script pins a version, fetches that bundle, rewrites its trailing ``export{…}`` so the
``App`` class is exposed on ``globalThis`` (which lets the bundle run as an inline
``<script type="module">`` with no ``import`` — see :mod:`svg_mcp.widget`), and writes the result to
``src/svg_mcp/_ext_apps_inline.js``. Re-run when bumping ``VERSION``::

    python scripts/vendor_ext_apps.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

VERSION = "0.4.0"
URL = f"https://unpkg.com/@modelcontextprotocol/ext-apps@{VERSION}/app-with-deps"
OUT = Path(__file__).resolve().parent.parent / "src" / "svg_mcp" / "_ext_apps_inline.js"


def main() -> None:
    src = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8")  # noqa: S310 (pinned https)
    match = re.search(r"([A-Za-z0-9_$]+) as App\b", src)
    if not match:
        sys.exit("vendor_ext_apps: could not find `… as App` in the ext-apps export")
    app_local = match.group(1)
    # The bundle ends in one flat `export{a as X, …, <app_local> as App}` (no nested braces).
    transformed = re.sub(
        r"export\{[^}]*\}\s*;?\s*$",
        f";globalThis.__SVGMCP_APP={app_local};",
        src.strip(),
    )
    if "export{" in transformed or "export {" in transformed:
        sys.exit("vendor_ext_apps: an export statement is still present after the rewrite")
    OUT.write_text(transformed, encoding="utf-8")
    print(f"vendored ext-apps@{VERSION} (App={app_local}) -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
