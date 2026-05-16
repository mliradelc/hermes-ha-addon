"""Regression tests for Home Assistant Ingress dashboard routing patches."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH_SCRIPT = ROOT / "hermes_agent" / "dashboard-patches.py"
RUN_SH = ROOT / "hermes_agent" / "run.sh"
NGINX_TEMPLATE = ROOT / "hermes_agent" / "nginx.conf.tpl"
NGINX_PORTS_TEMPLATE = ROOT / "hermes_agent" / "nginx-ports.conf.tpl"


def run_dashboard_patches(src: Path, status_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCH_SCRIPT), str(src), str(status_file)],
        check=False,
        text=True,
        capture_output=True,
    )


class DashboardIngressPatchTests(unittest.TestCase):
    def test_dashboard_router_uses_runtime_base_as_basename(self) -> None:
        """React Router must generate /dashboard/* links behind HA Ingress.

        The add-on serves the SPA below /dashboard/. API/assets were already
        base-aware, but BrowserRouter without a basename still emitted top-level
        links like /logs. Those work for in-app navigation but 404 on frame
        reload because nginx only proxies dashboard traffic below /dashboard/.
        """
        patch_script = PATCH_SCRIPT.read_text()

        self.assertIn("HA-ADDON-ROUTER-BASENAME-PATCHED", patch_script)
        self.assertIn('import { BASE } from "@/lib/api";', patch_script)
        self.assertIn('basename={BASE || "/"}', patch_script)

    def test_nginx_keeps_dashboard_deep_links_under_dashboard_prefix(self) -> None:
        """Direct /dashboard/<route> reloads must keep proxying to the SPA."""
        nginx_conf = NGINX_TEMPLATE.read_text()

        self.assertIn("location = /dashboard { return 302 /dashboard/; }", nginx_conf)
        self.assertIn("location /dashboard/api/", nginx_conf)
        self.assertIn("location /dashboard/", nginx_conf)
        self.assertIn("proxy_pass http://hermes_dashboard/;", nginx_conf)

    def test_direct_port_dashboard_api_accepts_spa_session_header(self) -> None:
        """Direct-port nginx guard must match the SPA's current token header.

        The React dashboard sends X-Hermes-Session-Token, not Authorization.
        Direct LAN ports turn basic auth off for /dashboard/api/* and rely on
        nginx to verify the SPA token before proxying, so checking only
        $http_authorization rejects the legitimate dashboard with 401.
        """
        nginx_ports = (ROOT / "hermes_agent" / "nginx-ports.conf.tpl").read_text()

        self.assertIn("$http_x_hermes_session_token", nginx_ports)
        self.assertIn("map_hash_bucket_size 128;", nginx_ports)
        self.assertIn("$dashboard_token_ok", nginx_ports)
        self.assertIn("~^%%DASHBOARD_TOKEN%%\\|", nginx_ports)
        self.assertIn("~^\\|Bearer\\ %%DASHBOARD_TOKEN%%$", nginx_ports)
        self.assertNotIn("$http_authorization != \"Bearer %%DASHBOARD_TOKEN%%\"", nginx_ports)

    def test_nginx_forwards_dashboard_prefix_to_modern_hermes(self) -> None:
        """Modern Hermes reads X-Forwarded-Prefix to set SPA base paths."""
        nginx_conf = NGINX_TEMPLATE.read_text()
        nginx_ports = NGINX_PORTS_TEMPLATE.read_text()

        self.assertIn("map $http_x_forwarded_prefix $dashboard_proxy_prefix", nginx_conf)
        self.assertIn("map $http_x_ingress_path $dashboard_forwarded_prefix", nginx_conf)
        self.assertIn('default "$http_x_ingress_path/dashboard";', nginx_conf)
        self.assertIn('"" $dashboard_proxy_prefix;', nginx_conf)
        self.assertEqual(nginx_conf.count("proxy_set_header X-Forwarded-Prefix"), 2)
        self.assertEqual(nginx_ports.count("proxy_set_header X-Forwarded-Prefix"), 6)

    def test_run_script_delegates_dashboard_patches_to_helper(self) -> None:
        """The startup path should not contain fragile multi-expression sed edits."""
        run_sh = RUN_SH.read_text()

        self.assertIn("hermes-dashboard-patches", run_sh)
        self.assertNotIn("BASE ||", run_sh)
        self.assertNotIn("HA-ADDON-ROUTER-BASENAME-PATCHED", run_sh)

    def test_modern_dashboard_sources_are_left_unpatched(self) -> None:
        """Current Hermes already supports proxy prefixes and should not be rewritten."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "web/src/lib").mkdir(parents=True)
            (src / "web/src/plugins").mkdir(parents=True)
            (src / "web").mkdir(exist_ok=True)
            (src / "web/src/lib/api.ts").write_text(
                'export const HERMES_BASE_PATH = readBasePath();\n'
                "const BASE = HERMES_BASE_PATH;\n"
                "declare global { interface Window { __HERMES_BASE_PATH__?: string; } }\n"
            )
            (src / "web/src/plugins/usePlugins.ts").write_text(
                'import { api, HERMES_BASE_PATH } from "@/lib/api";\n'
                "const baseUrl = `${HERMES_BASE_PATH}/dashboard-plugins/x.js`;\n"
            )
            (src / "web/src/main.tsx").write_text(
                'import { HERMES_BASE_PATH } from "./lib/api";\n'
                "<BrowserRouter basename={HERMES_BASE_PATH || undefined}>\n"
            )
            (src / "web/vite.config.ts").write_text("export default defineConfig({});\n")
            status = src / "status"

            result = run_dashboard_patches(src, status)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status.read_text(), "")
            self.assertIn("source patches skipped", result.stdout)
            self.assertNotIn("HA-ADDON", (src / "web/src/lib/api.ts").read_text())

    def test_modern_dashboard_repairs_obsolete_legacy_base_patch(self) -> None:
        """A failed previous start may have patched api.ts before dying later."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "web/src/lib").mkdir(parents=True)
            (src / "web/src/plugins").mkdir(parents=True)
            (src / "web").mkdir(exist_ok=True)
            (src / "web/src/lib/api.ts").write_text(
                'export const HERMES_BASE_PATH = readBasePath();\n'
                'export const BASE = new URL(/* @vite-ignore */ "..", import.meta.url)'
                '.pathname.replace(/\\/$/, ""); /* HA-ADDON-BASE-PATCHED */\n'
                "declare global { interface Window { __HERMES_BASE_PATH__?: string; } }\n"
            )
            status = src / "status"

            result = run_dashboard_patches(src, status)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status.read_text(), "changed")
            self.assertIn("Removed obsolete dashboard BASE source patch", result.stdout)
            self.assertIn("const BASE = HERMES_BASE_PATH;", (src / "web/src/lib/api.ts").read_text())

    def test_modern_dashboard_repairs_pre_vite_ignore_base_patch(self) -> None:
        """Older v1.0.4 starts used the same marker without @vite-ignore."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "web/src/lib").mkdir(parents=True)
            (src / "web/src/lib/api.ts").write_text(
                'export const HERMES_BASE_PATH = readBasePath();\n'
                'export const BASE = new URL("..", import.meta.url)'
                '.pathname.replace(/\\/$/, ""); /* HA-ADDON-BASE-PATCHED */\n'
                "declare global { interface Window { __HERMES_BASE_PATH__?: string; } }\n"
            )
            status = src / "status"

            result = run_dashboard_patches(src, status)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status.read_text(), "changed")
            self.assertIn("Removed obsolete dashboard BASE source patch", result.stdout)
            self.assertIn("const BASE = HERMES_BASE_PATH;", (src / "web/src/lib/api.ts").read_text())

    def test_legacy_dashboard_sources_are_patched_without_sed_delimiter_bug(self) -> None:
        """Legacy root-only dashboard sources still get the compatibility patches."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "web/src/lib").mkdir(parents=True)
            (src / "web/src/plugins").mkdir(parents=True)
            (src / "web").mkdir(exist_ok=True)
            (src / "web/src/lib/api.ts").write_text('const BASE = "";\n')
            (src / "web/src/plugins/usePlugins.ts").write_text(
                'import { api } from "@/lib/api";\n'
                "const baseUrl = `/dashboard-plugins/${manifest.name}/${manifest.entry}`;\n"
            )
            (src / "web/src/main.tsx").write_text(
                'import { BrowserRouter } from "react-router-dom";\n'
                "<BrowserRouter>\n"
            )
            (src / "web/vite.config.ts").write_text(
                "export default defineConfig({\n"
                "  plugins: [],\n"
                "});\n"
            )
            status = src / "status"

            result = run_dashboard_patches(src, status)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status.read_text(), "changed")
            self.assertIn("HA-ADDON-BASE-PATCHED", (src / "web/src/lib/api.ts").read_text())
            self.assertIn("`${BASE}/dashboard-plugins/", (src / "web/src/plugins/usePlugins.ts").read_text())
            self.assertIn('basename={BASE || "/"}', (src / "web/src/main.tsx").read_text())
            self.assertIn('base: "./"', (src / "web/vite.config.ts").read_text())


if __name__ == "__main__":
    unittest.main()
