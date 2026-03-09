#!/usr/bin/env python3
"""
Build a fully standalone JupyterLite HTML file with inlined Pyodide WASM.

This script downloads Pyodide assets and embeds them (base64-encoded)
directly into the HTML file, producing a single file that works
completely offline - no network requests needed.

Usage:
    python build-standalone.py [--output notebook-standalone.html] [--pyodide-version 0.27.5]
    python build-standalone.py --packages numpy matplotlib pandas

The resulting file will be large (~30-50MB+) due to the embedded WASM
and Python stdlib, but will work by simply opening it in any browser,
even from the local filesystem (file://).
"""

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path


PYODIDE_VERSION = "0.27.5"
CDN_BASE = "https://cdn.jsdelivr.net/pyodide/v{version}/full"

CORE_ASSETS = [
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


def resolve_dependencies(lock_data: dict, package_names: list[str]) -> list[str]:
    """Resolve the full dependency tree for the requested packages."""
    packages = lock_data["packages"]
    resolved = set()
    queue = list(package_names)

    while queue:
        name = queue.pop(0)
        if name in resolved:
            continue
        if name not in packages:
            print(f"  Warning: package '{name}' not found in pyodide-lock.json, skipping")
            continue
        resolved.add(name)
        for dep in packages[name].get("depends", []):
            if dep not in resolved:
                queue.append(dep)

    return sorted(resolved)


def download_packages(
    lock_data: dict,
    package_names: list[str],
    cdn: str,
    cache: Path,
) -> dict[str, bytes]:
    """Download wheel files for the resolved package list."""
    packages = lock_data["packages"]
    pkg_cache = cache / "packages"
    pkg_cache.mkdir(parents=True, exist_ok=True)

    wheels = {}
    for name in package_names:
        meta = packages[name]
        filename = meta["file_name"]
        cached = pkg_cache / filename
        if cached.exists():
            print(f"  Using cached {filename} ({cached.stat().st_size / 1024:.0f} KB)")
            wheels[filename] = cached.read_bytes()
        else:
            wheels[filename] = download(f"{cdn}/{filename}", filename)
            cached.write_bytes(wheels[filename])

    return wheels


def build(
    input_html: str = "notebook.html",
    output_html: str = "notebook-standalone.html",
    pyodide_version: str = PYODIDE_VERSION,
    cache_dir: str = ".pyodide-cache",
    packages: list[str] | None = None,
):
    script_dir = Path(__file__).parent
    input_path = script_dir / input_html
    output_path = script_dir / output_html
    cache = script_dir / cache_dir / pyodide_version

    print(f"Building standalone JupyterLite notebook")
    print(f"  Pyodide version: {pyodide_version}")
    print(f"  Input: {input_path}")
    print(f"  Output: {output_path}")
    if packages:
        print(f"  Packages: {', '.join(packages)}")
    print()

    # Download core assets
    cdn = CDN_BASE.format(version=pyodide_version)
    cache.mkdir(parents=True, exist_ok=True)

    assets = {}
    for name in CORE_ASSETS:
        cached = cache / name
        if cached.exists():
            print(f"  Using cached {name} ({cached.stat().st_size / (1024*1024):.1f} MB)")
            assets[name] = cached.read_bytes()
        else:
            assets[name] = download(f"{cdn}/{name}", name)
            cached.write_bytes(assets[name])

    print()

    # Resolve and download packages
    lock_data = json.loads(assets["pyodide-lock.json"])
    pkg_wheels = {}
    resolved_packages = []

    if packages:
        print("Resolving package dependencies...")
        resolved_packages = resolve_dependencies(lock_data, packages)
        print(f"  Resolved {len(resolved_packages)} packages: {', '.join(resolved_packages)}")
        print()

        print("Downloading packages...")
        pkg_wheels = download_packages(lock_data, resolved_packages, cdn, cache)
        total_pkg_size = sum(len(v) for v in pkg_wheels.values())
        print(f"  Total package data: {total_pkg_size / (1024*1024):.1f} MB")
        print()

    # Read the HTML template
    html = input_path.read_text(encoding="utf-8")

    # Encode core assets to base64
    print("Encoding assets to base64...")
    wasm_b64 = base64.b64encode(assets["pyodide.asm.wasm"]).decode("ascii")
    stdlib_b64 = base64.b64encode(assets["python_stdlib.zip"]).decode("ascii")
    print(f"  WASM: {len(wasm_b64) / (1024*1024):.1f} MB (base64)")
    print(f"  stdlib: {len(stdlib_b64) / (1024*1024):.1f} MB (base64)")

    # Prepare the Pyodide JS runtime
    pyodide_js = assets["pyodide.js"].decode("utf-8")
    pyodide_asm_js = assets["pyodide.asm.js"].decode("utf-8")
    lock_json = assets["pyodide-lock.json"].decode("utf-8")

    # Escape the lock JSON for embedding in a JS template literal
    lock_json_escaped = (
        lock_json
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
        .replace("</script>", "<\\/script>")
    )

    # Build package data block
    pkg_js_entries = []
    if pkg_wheels:
        print("Encoding packages to base64...")
        for filename, data in sorted(pkg_wheels.items()):
            b64 = base64.b64encode(data).decode("ascii")
            pkg_js_entries.append(f'"{filename}": "{b64}"')
            print(f"  {filename}: {len(b64) / 1024:.0f} KB (base64)")

    pkg_block = "{\n" + ",\n".join(pkg_js_entries) + "\n}" if pkg_js_entries else "{}"
    pkg_names_js = json.dumps(resolved_packages)

    # Build the inlined data block to inject before the main script
    inline_block = f"""<script>
// === INLINED PYODIDE ASSETS ===
// These were embedded by build-standalone.py
// Pyodide version: {pyodide_version}

const __PYODIDE_WASM_BASE64__ = "{wasm_b64}";
const __PYODIDE_STDLIB_BASE64__ = "{stdlib_b64}";
const __PYODIDE_LOCK_JSON__ = `{lock_json_escaped}`;
const __PYODIDE_PACKAGES__ = {pkg_block};
const __PYODIDE_PACKAGE_LIST__ = {pkg_names_js};
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
    html = html.replace(
        "<script>\n// ===========================================================================",
        inline_block + "\n<script>\n// ===========================================================================",
    )

    # Write output
    print(f"\nWriting standalone HTML...")
    output_path.write_text(html, encoding="utf-8")
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Done! Output: {output_path} ({size_mb:.1f} MB)")
    if resolved_packages:
        print(f"Bundled packages: {', '.join(resolved_packages)}")
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
    parser.add_argument(
        "-p", "--packages",
        nargs="+",
        default=None,
        metavar="PKG",
        help="Python packages to bundle (e.g. --packages numpy matplotlib pandas). "
             "Dependencies are resolved automatically from pyodide-lock.json.",
    )
    args = parser.parse_args()
    build(args.input, args.output, args.pyodide_version, args.cache_dir, args.packages)


if __name__ == "__main__":
    main()
