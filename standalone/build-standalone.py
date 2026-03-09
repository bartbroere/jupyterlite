#!/usr/bin/env python3
"""
Build a standalone single-file JupyterLite HTML.

This script can work in two modes:

1. **With a pre-built JupyterLite site** (``--site-dir``):
   Takes an existing ``jupyter lite build`` output and inlines everything
   into a single HTML file.

2. **With inlined Pyodide WASM** (``--inline-pyodide``):
   Additionally downloads and inlines the Pyodide WebAssembly runtime so
   the file works completely offline with zero network requests.

Usage::

    # First, build JupyterLite normally:
    pip install jupyterlite-core jupyterlite-pyodide-kernel
    jupyter lite build --output-dir _site

    # Then inline it:
    python build-standalone.py --site-dir _site --app notebooks

    # Or with Pyodide inlined for full offline use:
    python build-standalone.py --site-dir _site --app notebooks --inline-pyodide

    # Or bundle specific packages too:
    python build-standalone.py --site-dir _site --app notebooks \\
        --inline-pyodide --packages numpy matplotlib
"""

import argparse
import base64
import json
import mimetypes
import re
import sys
import urllib.request
from pathlib import Path


PYODIDE_VERSION = "0.27.5"
CDN_BASE = "https://cdn.jsdelivr.net/pyodide/v{version}/full"

PYODIDE_CORE_ASSETS = [
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

    while True:
        chunk = resp.read(1 << 16)
        if not chunk:
            break
        data.extend(chunk)
        if total:
            pct = len(data) * 100 // total
            print(f"\r  {desc}: {len(data) / (1024*1024):.1f} MB ({pct}%)", end="", flush=True)

    if total:
        print()
    print(f"  {desc}: {len(data) / (1024*1024):.1f} MB")
    return bytes(data)


def resolve_dependencies(lock_data: dict, package_names: list[str]) -> list[str]:
    """Resolve the full dependency tree for requested packages."""
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


def inline_css_urls(css_text: str, base_dir: Path) -> str:
    """Replace url() references in CSS with inline data URIs."""
    def replace_url(match):
        url = match.group(1).strip("'\"")
        if url.startswith("data:") or url.startswith("http"):
            return match.group(0)
        asset_path = (base_dir / url).resolve()
        if not asset_path.exists():
            return match.group(0)
        mime, _ = mimetypes.guess_type(str(asset_path))
        if mime is None:
            suffix_map = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf"}
            mime = suffix_map.get(asset_path.suffix, "application/octet-stream")
        data = base64.b64encode(asset_path.read_bytes()).decode("ascii")
        return f"url(data:{mime};base64,{data})"

    return re.sub(r'url\(([^)]+)\)', replace_url, css_text)


def collect_themes(build_dir: Path) -> str:
    """Collect theme CSS with fonts inlined as data URIs."""
    themes_dir = build_dir / "themes"
    parts = []
    if not themes_dir.exists():
        return ""
    for css_file in sorted(themes_dir.rglob("*.css")):
        css = inline_css_urls(css_file.read_text(encoding="utf-8"), css_file.parent)
        parts.append(f"/* === {css_file.relative_to(themes_dir)} === */\n{css}")
    return "\n".join(parts)


def build_config(output_dir: Path, app_name: str) -> dict:
    """Build the merged jupyter-config-data for the standalone app."""
    config = {}
    for cfg_path in [output_dir / "jupyter-lite.json", output_dir / app_name / "jupyter-lite.json"]:
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            app_conf = data.get("jupyter-config-data", {})
            if "federated_extensions" in app_conf:
                existing = config.get("federated_extensions", [])
                config["federated_extensions"] = existing + app_conf.pop("federated_extensions")
            config.update(app_conf)

    config["baseUrl"] = "./"
    config["fullStaticUrl"] = "./build"
    config["settingsUrl"] = "./build/schemas"
    config["fullLabextensionsUrl"] = "./extensions"
    return config


def build_standalone(
    site_dir: str,
    app_name: str = "notebooks",
    output: str = "standalone.html",
    title: str = "JupyterLite",
    inline_pyodide: bool = False,
    pyodide_version: str = PYODIDE_VERSION,
    packages: list[str] | None = None,
    cache_dir: str = ".pyodide-cache",
):
    output_dir = Path(site_dir)
    build_dir = output_dir / "build"
    app_dir = output_dir / app_name
    out_path = Path(output)

    if not app_dir.exists():
        print(f"Error: App '{app_name}' not found at {app_dir}")
        sys.exit(1)

    print(f"Building standalone JupyterLite HTML")
    print(f"  Site dir: {output_dir}")
    print(f"  App: {app_name}")
    print(f"  Output: {out_path}")
    print()

    # 1. Build config
    config_data = build_config(output_dir, app_name)

    # 2. Collect JS bundles
    print("Collecting JS bundles...")
    bundle_files = {}
    app_build = build_dir / app_name
    if app_build.exists():
        for f in sorted(app_build.glob("*.js")):
            bundle_files[f"build/{app_name}/{f.name}"] = f.read_text(encoding="utf-8")
    for f in sorted(build_dir.glob("*.js")):
        if f.name == "service-worker.js":
            continue
        key = f"build/{f.name}"
        if key not in bundle_files:
            bundle_files[key] = f.read_text(encoding="utf-8")
    config_utils = output_dir / "config-utils.js"
    if config_utils.exists():
        bundle_files["config-utils.js"] = config_utils.read_text(encoding="utf-8")
    print(f"  {len(bundle_files)} JS files")

    # 3. Collect extension scripts
    print("Collecting extension scripts...")
    ext_scripts = {}
    for ext in config_data.get("federated_extensions", []):
        ext_name = ext.get("name", "")
        load_path = ext.get("load", "")
        if not ext_name or not load_path:
            continue
        remote_entry = output_dir / "extensions" / ext_name / load_path
        if remote_entry.exists():
            ext_scripts[f"extensions/{ext_name}/{load_path}"] = remote_entry.read_text(encoding="utf-8")
        else:
            print(f"  Warning: extension entry not found: {remote_entry}")
    print(f"  {len(ext_scripts)} extension scripts")

    # 4. Schemas
    schemas_file = build_dir / "schemas" / "all.json"
    schemas_content = schemas_file.read_text(encoding="utf-8") if schemas_file.exists() else "{}"

    # 5. Themes
    print("Collecting themes...")
    theme_styles = collect_themes(build_dir)
    print(f"  {len(theme_styles)} bytes of CSS")

    # 6. Pyodide inlining (optional)
    pyodide_block = ""
    if inline_pyodide:
        pyodide_block = build_pyodide_block(pyodide_version, packages, cache_dir)

    # 7. Assemble
    print("\nAssembling standalone HTML...")

    # Virtual filesystem for config-utils.js fetch interception
    virtual_fs = {
        "build/schemas/all.json": schemas_content,
        "jupyter-lite.json": json.dumps({
            "jupyter-lite-schema-version": 0,
            "jupyter-config-data": config_data,
        }),
        f"{app_name}/jupyter-lite.json": json.dumps({
            "jupyter-lite-schema-version": 0,
            "jupyter-config-data": config_data,
        }),
    }
    # Root and app index.html for config-utils path traversal
    for path in ["index.html", f"{app_name}/index.html"]:
        root = "." if "/" not in path else ".."
        virtual_fs[path] = (
            f'<!DOCTYPE html><html><head>'
            f'<script id="jupyter-config-data" type="application/json" '
            f'data-jupyter-lite-root="{root}">'
            f'{json.dumps(config_data)}'
            f'</script></head><body></body></html>'
        )

    vfs_json = json.dumps(virtual_fs)
    vfs_escaped = (
        vfs_json
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
        .replace("</script>", "<\\/script>")
    )

    # Separate bootstrap from regular chunks
    ext_script_blocks = []
    for path, content in sorted(ext_scripts.items()):
        ext_script_blocks.append(
            f'// === Extension: {path} ===\n'
            f'try {{ {content} }} catch(e) {{ console.warn("Failed to load {path}:", e); }}'
        )

    chunk_blocks = []
    bootstrap_content = ""
    for path, content in sorted(bundle_files.items()):
        if path == "config-utils.js":
            continue
        if "bootstrap" in path:
            bootstrap_content = content
            continue
        if "publicpath" in path:
            continue
        chunk_blocks.append(f"// === {path} ===\n{content}")

    config_json = json.dumps(config_data, indent=2)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{
  margin: 0; padding: 0;
  transition: background-color 0.3s ease;
  background-color: #fff; color: #000;
}}
body.jp-mod-dark {{ background-color: #111; color: #fff; }}
#jupyterlite-loading-indicator {{
  position: fixed; top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  text-align: center; z-index: 1000;
}}
.jupyterlite-loading-indicator-spinner {{
  width: 60px; height: 60px; margin: 0 auto 20px;
  border: 6px solid rgba(0,0,0,0.1);
  border-top: 6px solid #FFDC00;
  border-radius: 50%;
  animation: jupyter-spin 1s linear infinite;
}}
body.jp-mod-dark .jupyterlite-loading-indicator-spinner {{
  border: 6px solid rgba(255,255,255,0.1);
  border-top: 6px solid #FFDC00;
}}
.jupyterlite-loading-indicator-text {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  font-size: 16px;
}}
@keyframes jupyter-spin {{
  0% {{ transform: rotate(0deg); }}
  100% {{ transform: rotate(360deg); }}
}}
/* === Inlined theme CSS === */
{theme_styles}
</style>
<script id="jupyter-config-data" type="application/json" data-jupyter-lite-root=".">
{config_json}
</script>
</head>
<body class="jp-ThemedContainer" data-notebook="{app_name}">
<div id="jupyterlite-loading-indicator">
  <div class="jupyterlite-loading-indicator-spinner"></div>
  <div class="jupyterlite-loading-indicator-text">Loading JupyterLite...</div>
</div>
<noscript>
  <div style="text-align: center; padding: 20px;">
    JupyterLite requires JavaScript to be enabled in your browser.
  </div>
</noscript>

{pyodide_block}

<script>
// === STANDALONE VIRTUAL FILESYSTEM ===
(function() {{
  const _vfs = JSON.parse(`{vfs_escaped}`);
  const _origFetch = window.fetch;
  window.fetch = function(input, init) {{
    const url = (typeof input === 'string') ? input : (input?.url || String(input));
    let relPath = url;
    try {{
      const u = new URL(url, window.location.href);
      const base = window.location.href.replace(/[^/]*$/, '');
      if (u.href.startsWith(base)) relPath = u.href.slice(base.length);
    }} catch(e) {{}}
    relPath = relPath.replace(/^\\.?\\//, '').replace(/[?#].*$/, '');
    if (relPath in _vfs) {{
      const content = _vfs[relPath];
      const mime = relPath.endsWith('.json') ? 'application/json' : 'text/html';
      return Promise.resolve(new Response(content, {{
        status: 200, headers: {{ 'Content-Type': mime }},
      }}));
    }}
    if (window.location.protocol === 'file:' &&
        (relPath.endsWith('.json') || relPath.endsWith('.ipynb'))) {{
      return Promise.resolve(new Response('{{}}', {{
        status: 200, headers: {{ 'Content-Type': 'application/json' }},
      }}));
    }}
    return _origFetch.apply(this, arguments);
  }};
}})();
window.__webpack_public_path__ = './build/';
</script>

<script>
// === INLINED EXTENSION SCRIPTS ===
window._JUPYTERLAB = window._JUPYTERLAB || {{}};
{"".join(f'{s}' + chr(10) for s in ext_script_blocks)}
</script>

<script>
// === INLINED BUNDLE CHUNKS ===
{"".join(f'{c}' + chr(10) for c in chunk_blocks)}
</script>

<script>
// === BOOTSTRAP ===
{bootstrap_content or "console.error('No bootstrap found');"}
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Done! Output: {out_path} ({size_mb:.1f} MB)")
    print(f"\nOpen in browser: file://{out_path.resolve()}")


def build_pyodide_block(pyodide_version, packages, cache_dir):
    """Download Pyodide and build the inlined script block."""
    cdn = CDN_BASE.format(version=pyodide_version)
    cache = Path(cache_dir) / pyodide_version
    cache.mkdir(parents=True, exist_ok=True)

    print("Downloading Pyodide assets...")
    assets = {}
    for name in PYODIDE_CORE_ASSETS:
        cached = cache / name
        if cached.exists():
            print(f"  Using cached {name} ({cached.stat().st_size / (1024*1024):.1f} MB)")
            assets[name] = cached.read_bytes()
        else:
            assets[name] = download(f"{cdn}/{name}", name)
            cached.write_bytes(assets[name])

    # Resolve and download packages
    pkg_block = "{}"
    pkg_names_js = "[]"
    if packages:
        lock_data = json.loads(assets["pyodide-lock.json"])
        print("\nResolving package dependencies...")
        resolved = resolve_dependencies(lock_data, packages)
        print(f"  Resolved {len(resolved)} packages: {', '.join(resolved)}")
        print("\nDownloading packages...")
        pkg_cache = cache / "packages"
        pkg_cache.mkdir(exist_ok=True)
        pkg_entries = []
        for name in resolved:
            meta = lock_data["packages"][name]
            filename = meta["file_name"]
            cached = pkg_cache / filename
            if cached.exists():
                data = cached.read_bytes()
                print(f"  Using cached {filename} ({len(data)/1024:.0f} KB)")
            else:
                data = download(f"{cdn}/{filename}", filename)
                cached.write_bytes(data)
            b64 = base64.b64encode(data).decode("ascii")
            pkg_entries.append(f'"{filename}": "{b64}"')

        pkg_block = "{\n" + ",\n".join(pkg_entries) + "\n}"
        pkg_names_js = json.dumps(resolved)

    print("\nEncoding Pyodide to base64...")
    wasm_b64 = base64.b64encode(assets["pyodide.asm.wasm"]).decode("ascii")
    stdlib_b64 = base64.b64encode(assets["python_stdlib.zip"]).decode("ascii")
    lock_json = assets["pyodide-lock.json"].decode("utf-8")
    lock_escaped = (
        lock_json.replace("\\", "\\\\").replace("`", "\\`")
        .replace("${", "\\${").replace("</script>", "<\\/script>")
    )
    pyodide_js = assets["pyodide.js"].decode("utf-8")
    pyodide_asm_js = assets["pyodide.asm.js"].decode("utf-8")

    return f"""
<script>
// === INLINED PYODIDE ASSETS ===
const __PYODIDE_WASM_BASE64__ = "{wasm_b64}";
const __PYODIDE_STDLIB_BASE64__ = "{stdlib_b64}";
const __PYODIDE_LOCK_JSON__ = `{lock_escaped}`;
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


def main():
    parser = argparse.ArgumentParser(
        description="Build a standalone single-file JupyterLite HTML from a pre-built site"
    )
    parser.add_argument(
        "--site-dir", required=True,
        help="Path to a built JupyterLite site (output of 'jupyter lite build')",
    )
    parser.add_argument(
        "--app", default="notebooks",
        help="Which app to inline (default: notebooks). Options: lab, notebooks, repl, etc.",
    )
    parser.add_argument(
        "-o", "--output", default="standalone.html",
        help="Output HTML filename (default: standalone.html)",
    )
    parser.add_argument(
        "--title", default="JupyterLite",
        help="Page title (default: JupyterLite)",
    )
    parser.add_argument(
        "--inline-pyodide", action="store_true",
        help="Also inline the Pyodide WASM runtime for full offline use",
    )
    parser.add_argument(
        "--pyodide-version", default=PYODIDE_VERSION,
        help=f"Pyodide version to embed (default: {PYODIDE_VERSION})",
    )
    parser.add_argument(
        "-p", "--packages", nargs="+", default=None, metavar="PKG",
        help="Python packages to bundle (e.g. --packages numpy matplotlib). "
             "Implies --inline-pyodide.",
    )
    parser.add_argument(
        "--cache-dir", default=".pyodide-cache",
        help="Directory to cache downloaded Pyodide assets (default: .pyodide-cache)",
    )
    args = parser.parse_args()

    if args.packages:
        args.inline_pyodide = True

    build_standalone(
        site_dir=args.site_dir,
        app_name=args.app,
        output=args.output,
        title=args.title,
        inline_pyodide=args.inline_pyodide,
        pyodide_version=args.pyodide_version,
        packages=args.packages,
        cache_dir=args.cache_dir,
    )


if __name__ == "__main__":
    main()
