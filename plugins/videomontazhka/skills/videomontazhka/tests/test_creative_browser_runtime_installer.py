from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from runtime_paths import CREATIVE_BROWSER_RUNTIME  # noqa: E402

SCRIPT = ROOT / "scripts" / "install_creative_browser_runtime.py"
SPEC = importlib.util.spec_from_file_location("creative_browser_installer", SCRIPT)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class CreativeBrowserRuntimeTest(unittest.TestCase):
    def test_shared_runtime_path_is_independent_of_skill_install_location(self) -> None:
        self.assertEqual(
            INSTALLER.DEFAULT_RUNTIME,
            CREATIVE_BROWSER_RUNTIME,
        )

    def test_lock_is_exact_official_and_license_allowlisted(self) -> None:
        package = json.loads((ROOT / "assets" / "creative-browser-package.v1.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "assets" / "creative-browser-package-lock.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(package["dependencies"], INSTALLER.TOP_LEVEL)
        self.assertEqual(lock["packages"][""]["dependencies"], INSTALLER.TOP_LEVEL)
        self.assertNotIn("@lottiefiles/dotlottie-web", package["dependencies"])
        self.assertNotIn("remotion", {name.lower() for name in package["dependencies"]})
        for path, record in lock["packages"].items():
            if not path:
                continue
            with self.subTest(package=path):
                self.assertTrue(record["resolved"].startswith(INSTALLER.OFFICIAL_REGISTRY))
                self.assertTrue(record["integrity"].startswith("sha512-"))
                self.assertIn(record["license"], INSTALLER.ALLOWED_LICENSES)

    def test_source_hashes_and_shader_allowlist_are_closed(self) -> None:
        INSTALLER.check_sources()
        self.assertEqual(len(INSTALLER.GL_TRANSITIONS), 8)
        self.assertIn("fade", INSTALLER.GL_TRANSITIONS)
        self.assertIn("GlitchDisplace", INSTALLER.GL_TRANSITIONS)
        self.assertNotIn("*", INSTALLER.GL_TRANSITIONS)

    def test_installed_runtime_is_minimal_offline_and_tamper_evident(self) -> None:
        runtime = INSTALLER.DEFAULT_RUNTIME
        if not runtime.is_dir():
            self.skipTest("creative browser runtime has not been installed yet")
        manifest = INSTALLER.verify_runtime(runtime)
        self.assertFalse(manifest["policy"]["network_required_for_render"])
        self.assertEqual(manifest["policy"]["remote_media_inputs"], "prohibited")
        self.assertEqual(manifest["gl_transition_policy"]["unlisted_shader_default"], "blocked")
        self.assertEqual(manifest["browser_smoke"]["external_requests"], [])
        self.assertEqual(manifest["security_audit"]["vulnerabilities"]["total"], 0)
        self.assertFalse((runtime / "node_modules").exists())
        with tempfile.TemporaryDirectory(prefix="sprut-creative-tamper-") as temporary:
            copy = Path(temporary) / "runtime"
            shutil.copytree(runtime, copy)
            with (copy / "vendor" / "rough-notation.iife.js").open("ab") as handle:
                handle.write(b"\n// tampered\n")
            with self.assertRaises(INSTALLER.InstallError):
                INSTALLER.verify_runtime(copy)


if __name__ == "__main__":
    unittest.main()
