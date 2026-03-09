"""A JupyterLite addon for creating a standalone single-file HTML notebook.

This addon runs as a ``post_build`` step and inlines all JS bundles, CSS,
schemas, extension assets, and configuration into a single HTML file that
can be opened directly from the filesystem (``file://``) with no server
or network access required.

Usage::

    jupyter lite build --apps notebooks
    # or to also produce a standalone HTML:
    jupyter lite build --apps notebooks \\
        --LiteManager.extra_args='["--standalone"]'

The resulting file is written alongside the normal build output.
"""

import base64
import json
import mimetypes
import re
from pathlib import Path

from traitlets import Bool, Unicode, default

from ..constants import UTF8
from .base import BaseAddon


class StandaloneAddon(BaseAddon):
    """Inline a built JupyterLite app into a single, self-contained HTML file."""

    __all__ = ["post_build"]

    standalone = Bool(
        default_value=False,
        help="If True, produce a standalone single-file HTML after the build.",
    ).tag(config=True)

    standalone_app = Unicode(
        default_value="notebooks",
        help="Which app to inline into the standalone HTML (e.g. notebooks, repl, lab).",
    ).tag(config=True)

    standalone_output = Unicode(
        default_value="",
        help="Output path for the standalone HTML file. Defaults to <output_dir>/standalone.html.",
    ).tag(config=True)

    standalone_title = Unicode(
        default_value="JupyterLite",
        help="Page title for the standalone HTML.",
    ).tag(config=True)

    def post_build(self, manager):
        """After the standard build, optionally produce a standalone HTML."""
        if not self.standalone:
            return

        output_dir = manager.output_dir
        app_name = self.standalone_app
        app_dir = output_dir / app_name
        build_dir = output_dir / "build"
        index_html = app_dir / "index.html"

        if not index_html.exists():
            self.log.warning(
                f"[standalone] App '{app_name}' not found at {app_dir}, skipping."
            )
            return

        out_path = Path(self.standalone_output) if self.standalone_output else (
            output_dir / "standalone.html"
        )

        yield self.task(
            name="standalone",
            doc=f"Create standalone single-file HTML from the '{app_name}' app",
            file_dep=[index_html],
            actions=[(self._build_standalone, [output_dir, app_name, out_path])],
            targets=[out_path],
        )

    def _build_standalone(self, output_dir, app_name, out_path):
        """Assemble the standalone HTML by inlining all assets."""
        app_dir = output_dir / app_name
        build_dir = output_dir / "build"

        self.log.info(f"[standalone] Building standalone HTML from '{app_name}' app...")

        # 1. Read the app's index.html
        index_html = app_dir / "index.html"
        html = index_html.read_text(**UTF8)

        # 2. Collect all JS/CSS/JSON assets we need to inline
        inlined_assets = {}  # path -> content

        # 3. Read the jupyter-config-data from the HTML and any jupyter-lite.json
        config_data = self._build_config(output_dir, app_name)

        # 4. Collect all JS bundle files referenced by the app
        bundle_files = self._collect_bundle_files(output_dir, app_name, config_data)

        # 5. Collect extension assets
        extension_scripts = self._collect_extension_scripts(output_dir, config_data)

        # 6. Collect schemas
        schemas_content = self._read_schemas(build_dir)

        # 7. Collect theme CSS and font assets
        theme_styles = self._collect_themes(build_dir)

        # 8. Build the standalone HTML
        standalone_html = self._assemble_html(
            config_data=config_data,
            bundle_files=bundle_files,
            extension_scripts=extension_scripts,
            schemas_content=schemas_content,
            theme_styles=theme_styles,
            title=self.standalone_title,
            app_name=app_name,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(standalone_html, **UTF8)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        self.log.info(f"[standalone] Written: {out_path} ({size_mb:.1f} MB)")

    def _build_config(self, output_dir, app_name):
        """Build the merged jupyter-config-data for the standalone app."""
        config = {}

        # Read root jupyter-lite.json
        root_config = output_dir / "jupyter-lite.json"
        if root_config.exists():
            data = json.loads(root_config.read_text(**UTF8))
            config.update(data.get("jupyter-config-data", {}))

        # Read app-specific jupyter-lite.json
        app_config = output_dir / app_name / "jupyter-lite.json"
        if app_config.exists():
            data = json.loads(app_config.read_text(**UTF8))
            app_conf = data.get("jupyter-config-data", {})
            # Merge federated_extensions (append)
            if "federated_extensions" in app_conf:
                existing = config.get("federated_extensions", [])
                config["federated_extensions"] = existing + app_conf["federated_extensions"]
                del app_conf["federated_extensions"]
            config.update(app_conf)

        # Override URLs for standalone mode - everything is inline
        config["baseUrl"] = "./"
        config["fullStaticUrl"] = "./build"
        config["settingsUrl"] = "./build/schemas"
        config["fullLabextensionsUrl"] = "./extensions"

        return config

    def _collect_bundle_files(self, output_dir, app_name, config_data):
        """Collect all JS bundle files needed by the app."""
        build_dir = output_dir / "build"
        bundles = {}

        # The app's main bundle and publicpath
        app_build = build_dir / app_name
        if app_build.exists():
            for js_file in sorted(app_build.glob("*.js")):
                key = f"build/{app_name}/{js_file.name}"
                bundles[key] = js_file.read_text(**UTF8)

        # Shared chunks (jlab_core, numbered chunks)
        for js_file in sorted(build_dir.glob("*.js")):
            if js_file.name == "service-worker.js":
                continue
            key = f"build/{js_file.name}"
            if key not in bundles:
                bundles[key] = js_file.read_text(**UTF8)

        # config-utils.js and bootstrap are at the root level
        for name in ["config-utils.js"]:
            f = output_dir / name
            if f.exists():
                bundles[name] = f.read_text(**UTF8)

        return bundles

    def _collect_extension_scripts(self, output_dir, config_data):
        """Collect federated extension remote entry scripts."""
        extensions = config_data.get("federated_extensions", [])
        ext_scripts = {}

        for ext in extensions:
            ext_name = ext.get("name", "")
            load_path = ext.get("load", "")
            if not ext_name or not load_path:
                continue

            remote_entry = output_dir / "extensions" / ext_name / load_path
            if remote_entry.exists():
                key = f"extensions/{ext_name}/{load_path}"
                ext_scripts[key] = remote_entry.read_text(**UTF8)
            else:
                self.log.warning(
                    f"[standalone] Extension entry not found: {remote_entry}"
                )

        return ext_scripts

    def _read_schemas(self, build_dir):
        """Read the compiled schemas."""
        schemas_file = build_dir / "schemas" / "all.json"
        if schemas_file.exists():
            return schemas_file.read_text(**UTF8)
        return "{}"

    def _collect_themes(self, build_dir):
        """Collect theme CSS with fonts inlined as data URIs."""
        themes_dir = build_dir / "themes"
        theme_css_parts = []

        if not themes_dir.exists():
            return ""

        for css_file in sorted(themes_dir.rglob("*.css")):
            css_text = css_file.read_text(**UTF8)
            # Inline url() references (fonts, images) as data URIs
            css_text = self._inline_css_urls(css_text, css_file.parent)
            theme_css_parts.append(f"/* === {css_file.relative_to(themes_dir)} === */\n{css_text}")

        return "\n".join(theme_css_parts)

    def _inline_css_urls(self, css_text, base_dir):
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
                if asset_path.suffix == ".woff2":
                    mime = "font/woff2"
                elif asset_path.suffix == ".woff":
                    mime = "font/woff"
                elif asset_path.suffix == ".ttf":
                    mime = "font/ttf"
                else:
                    mime = "application/octet-stream"
            data = base64.b64encode(asset_path.read_bytes()).decode("ascii")
            return f"url(data:{mime};base64,{data})"

        return re.sub(r'url\(([^)]+)\)', replace_url, css_text)

    def _assemble_html(
        self,
        config_data,
        bundle_files,
        extension_scripts,
        schemas_content,
        theme_styles,
        title,
        app_name,
    ):
        """Assemble the final standalone HTML document."""
        # Build a virtual filesystem that the app can use via fetch interception
        virtual_fs = {}

        # Add schemas
        virtual_fs["build/schemas/all.json"] = schemas_content

        # Add jupyter-lite.json (so config-utils.js can find it)
        virtual_fs["jupyter-lite.json"] = json.dumps({
            "jupyter-lite-schema-version": 0,
            "jupyter-config-data": config_data,
        })

        # Add app-level jupyter-lite.json
        virtual_fs[f"{app_name}/jupyter-lite.json"] = json.dumps({
            "jupyter-lite-schema-version": 0,
            "jupyter-config-data": config_data,
        })

        # Add the root index.html (needed by config-utils.js path traversal)
        virtual_fs["index.html"] = f"""<!DOCTYPE html>
<html><head>
<script id="jupyter-config-data" type="application/json" data-jupyter-lite-root=".">
{json.dumps(config_data, indent=2)}
</script></head><body></body></html>"""

        # Add the app index.html for config loading
        virtual_fs[f"{app_name}/index.html"] = f"""<!DOCTYPE html>
<html><head>
<script id="jupyter-config-data" type="application/json" data-jupyter-lite-root="..">
{json.dumps(config_data, indent=2)}
</script></head><body></body></html>"""

        # Serialize the virtual filesystem
        vfs_json = json.dumps(virtual_fs)
        vfs_json_escaped = (
            vfs_json
            .replace("\\", "\\\\")
            .replace("`", "\\`")
            .replace("${", "\\${")
            .replace("</script>", "<\\/script>")
        )

        # Serialize all JS bundles
        all_scripts = []

        # Extension scripts need to be loaded first (module federation)
        for path, content in sorted(extension_scripts.items()):
            all_scripts.append(
                f'// === Extension: {path} ===\n'
                f'try {{ {content} }} catch(e) {{ console.warn("Failed to load {path}:", e); }}'
            )

        # All bundle scripts (excluding config-utils.js and the main bootstrap,
        # which we handle specially)
        chunk_scripts = []
        bootstrap_content = None
        config_utils_content = bundle_files.get("config-utils.js", "")

        for path, content in sorted(bundle_files.items()):
            if path == "config-utils.js":
                continue
            if "bootstrap" in path:
                bootstrap_content = content
                continue
            if "publicpath" in path:
                continue  # We handle public path differently
            chunk_scripts.append(f"// === {path} ===\n{content}")

        config_json = json.dumps(config_data, indent=2)

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{
  margin: 0;
  padding: 0;
  transition: background-color 0.3s ease;
  background-color: #fff;
  color: #000;
}}
body.jp-mod-dark {{
  background-color: #111;
  color: #fff;
}}
#jupyterlite-loading-indicator {{
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
  z-index: 1000;
}}
.jupyterlite-loading-indicator-spinner {{
  width: 60px;
  height: 60px;
  margin: 0 auto 20px;
  border: 6px solid rgba(0, 0, 0, 0.1);
  border-top: 6px solid #FFDC00;
  border-radius: 50%;
  animation: jupyter-spin 1s linear infinite;
}}
body.jp-mod-dark .jupyterlite-loading-indicator-spinner {{
  border: 6px solid rgba(255, 255, 255, 0.1);
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
/* Inlined theme CSS */
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

<script>
// === STANDALONE VIRTUAL FILESYSTEM ===
// Intercept fetch() to serve inlined assets without any network requests.
(function() {{
  const __VIRTUAL_FS__ = `{vfs_json_escaped}`;
  const _vfs = JSON.parse(__VIRTUAL_FS__);
  const _origFetch = window.fetch;

  window.fetch = function(input, init) {{
    const url = (typeof input === 'string') ? input : (input?.url || String(input));

    // Normalize the URL to a relative path
    let relPath = url;
    try {{
      const u = new URL(url, window.location.href);
      const base = window.location.href.replace(/[^/]*$/, '');
      if (u.href.startsWith(base)) {{
        relPath = u.href.slice(base.length);
      }}
    }} catch(e) {{}}

    // Strip leading ./ and trailing query/hash
    relPath = relPath.replace(/^\\.?\\//, '').replace(/[?#].*$/, '');

    if (relPath in _vfs) {{
      const content = _vfs[relPath];
      const isJson = relPath.endsWith('.json');
      const mime = isJson ? 'application/json' : 'text/html';
      return Promise.resolve(new Response(content, {{
        status: 200,
        headers: {{ 'Content-Type': mime }},
      }}));
    }}

    // For file:// protocol, intercept 404s gracefully
    if (window.location.protocol === 'file:') {{
      // Block requests that would fail on file://
      if (relPath.endsWith('.json') || relPath.endsWith('.ipynb')) {{
        return Promise.resolve(new Response('{{}}', {{
          status: 200,
          headers: {{ 'Content-Type': 'application/json' }},
        }}));
      }}
    }}

    return _origFetch.apply(this, arguments);
  }};
}})();

// Set the webpack public path before anything loads
window.__webpack_public_path__ = './build/';
</script>

<script>
// === INLINED EXTENSION SCRIPTS ===
window._JUPYTERLAB = window._JUPYTERLAB || {{}};
{"".join(f"""
// --- Extension script ---
{script}
""" for script in all_scripts)}
</script>

<script>
// === INLINED BUNDLE CHUNKS ===
{"".join(f"""
{chunk}
""" for chunk in chunk_scripts)}
</script>

<script>
// === BOOTSTRAP ===
{bootstrap_content or "console.error('No bootstrap found');"}
</script>
</body>
</html>
"""
