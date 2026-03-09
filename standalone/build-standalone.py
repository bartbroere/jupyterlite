#!/usr/bin/env python3
"""
Build a fully standalone JupyterLite HTML file with inlined Pyodide WASM.

This script downloads Pyodide assets and embeds them (base64-encoded)
directly into the HTML file, producing a single file that works
completely offline - no network requests needed.

Usage:
    python build-standalone.py [--output notebook-standalone.html] [--pyodide-version 0.27.5]

The resulting file will be large (~30-50MB) due to the embedded WASM
and Python stdlib, but will work by simply opening it in any browser,
even from the local filesystem (file://).
"""

import argparse
import base64
import io
import os
import re
import sys
import tarfile
import urllib.request
from pathlib import Path


PYODIDE_VERSION = "0.27.5"
CDN_BASE = "https://cdn.jsdelivr.net/pyodide/v{version}/full"

ASSETS = [
    "pyodide.js",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
    "pyodide-lock.json",
    "pyodide.asm.js",
]


def download(url: str, desc: str) -> bytes:
    """Download a URL with progress indication."""
    print(f"  Downloading {desc}...")
    req = urllib.request.Request(url, headers={"User-Agent": "JupyterLite-Standalone-Builder/1.0"})
    resp = urllib.request.urlopen(req)
    total = int(resp.headers.get("Content-Length", 0))
    data = bytearray()
    block_size = 1 << 16  # 64KB

    while True:
        chunk = resp.read(block_size)
        if not chunk:
            break
        data.extend(chunk)
        if total:
            pct = len(data) * 100 // total
            size_mb = len(data) / (1024 * 1024)
            print(f"\r  {desc}: {size_mb:.1f} MB ({pct}%)", end="", flush=True)

    if total:
        print()
    print(f"  {desc}: {len(data) / (1024*1024):.1f} MB")
    return bytes(data)


def build(
    input_html: str = "notebook.html",
    output_html: str = "notebook-standalone.html",
    pyodide_version: str = PYODIDE_VERSION,
    cache_dir: str = ".pyodide-cache",
):
    script_dir = Path(__file__).parent
    input_path = script_dir / input_html
    output_path = script_dir / output_html
    cache = script_dir / cache_dir / pyodide_version

    print(f"Building standalone JupyterLite notebook")
    print(f"  Pyodide version: {pyodide_version}")
    print(f"  Input: {input_path}")
    print(f"  Output: {output_path}")
    print()

    # Download assets
    cdn = CDN_BASE.format(version=pyodide_version)
    cache.mkdir(parents=True, exist_ok=True)

    assets = {}
    for name in ASSETS:
        cached = cache / name
        if cached.exists():
            print(f"  Using cached {name} ({cached.stat().st_size / (1024*1024):.1f} MB)")
            assets[name] = cached.read_bytes()
        else:
            assets[name] = download(f"{cdn}/{name}", name)
            cached.write_bytes(assets[name])

    print()

    # Read the HTML template
    html = input_path.read_text(encoding="utf-8")

    # Encode assets to base64
    print("Encoding assets to base64...")
    wasm_b64 = base64.b64encode(assets["pyodide.asm.wasm"]).decode("ascii")
    stdlib_b64 = base64.b64encode(assets["python_stdlib.zip"]).decode("ascii")
    print(f"  WASM: {len(wasm_b64) / (1024*1024):.1f} MB (base64)")
    print(f"  stdlib: {len(stdlib_b64) / (1024*1024):.1f} MB (base64)")

    # Prepare the Pyodide JS runtime
    pyodide_js = assets["pyodide.js"].decode("utf-8")
    pyodide_asm_js = assets["pyodide.asm.js"].decode("utf-8")
    lock_json = assets["pyodide-lock.json"].decode("utf-8")

    # Escape the lock JSON for embedding in a JS string literal
    # (it may contain characters that break script tags)
    lock_json_escaped = lock_json.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${").replace("</script>", "<\\/script>")

    # Build the inlined data block to inject before the main script
    inline_block = f"""<script>
// === INLINED PYODIDE ASSETS ===
// These were embedded by build-standalone.py
// Pyodide version: {pyodide_version}

const __PYODIDE_WASM_BASE64__ = "{wasm_b64}";
const __PYODIDE_STDLIB_BASE64__ = "{stdlib_b64}";
const __PYODIDE_LOCK_JSON__ = `{lock_json_escaped}`;
</script>
<script>
// === PYODIDE ASM.JS RUNTIME ===
{pyodide_asm_js}
</script>
<script>
// === PYODIDE MAIN RUNTIME ===
{pyodide_js}
</script>
"""

    # Inject before the main <script> tag
    # We replace the CDN loading logic with the inlined version
    html = html.replace(
        "<script>\n// ===========================================================================",
        inline_block + "\n<script>\n// ===========================================================================",
    )

    # Write output
    print(f"\nWriting standalone HTML...")
    output_path.write_text(html, encoding="utf-8")
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Done! Output: {output_path} ({size_mb:.1f} MB)")
    print(f"\nOpen in browser: file://{output_path.resolve()}")


def main():
    parser = argparse.ArgumentParser(
        description="Build a standalone JupyterLite HTML file with inlined Pyodide WASM"
    )
    parser.add_argument(
        "-o", "--output",
        default="notebook-standalone.html",
        help="Output HTML filename (default: notebook-standalone.html)",
    )
    parser.add_argument(
        "-i", "--input",
        default="notebook.html",
        help="Input HTML template (default: notebook.html)",
    )
    parser.add_argument(
        "--pyodide-version",
        default=PYODIDE_VERSION,
        help=f"Pyodide version to embed (default: {PYODIDE_VERSION})",
    )
    parser.add_argument(
        "--cache-dir",
        default=".pyodide-cache",
        help="Directory to cache downloaded assets (default: .pyodide-cache)",
    )
    args = parser.parse_args()
    build(args.input, args.output, args.pyodide_version, args.cache_dir)


if __name__ == "__main__":
    main()
