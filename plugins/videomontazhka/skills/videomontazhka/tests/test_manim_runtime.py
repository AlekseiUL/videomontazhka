from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from runtime_paths import MANIM_RUNTIME  # noqa: E402

SCRIPT = ROOT / "scripts" / "install_manim_runtime.py"
SPEC = importlib.util.spec_from_file_location("sprut_manim_runtime", SCRIPT)
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class ManimRuntimeTest(unittest.TestCase):
    def test_shared_runtime_path_is_independent_of_skill_copy(self) -> None:
        self.assertEqual(
            RUNTIME.DEFAULT_RUNTIME,
            MANIM_RUNTIME,
        )

    def test_requirements_pin_exact_supported_version(self) -> None:
        self.assertEqual(
            RUNTIME.REQUIREMENTS.read_text(encoding="utf-8"),
            "manim==0.20.1\n",
        )

    def test_installed_runtime_is_local_arm64_and_manifest_bound(self) -> None:
        if not RUNTIME.DEFAULT_RUNTIME.is_dir():
            self.skipTest("Manim runtime is not installed")
        observed = RUNTIME.inspect(RUNTIME.DEFAULT_RUNTIME)
        RUNTIME.verify_manifest(RUNTIME.DEFAULT_RUNTIME, observed)
        self.assertEqual(observed["manim_version"], "0.20.1")
        self.assertEqual(observed["machine"], "arm64")
        self.assertTrue(observed["capabilities"]["architecture_diagrams"])
        self.assertEqual(observed["smoke"]["render"]["status"], "PASS")
        self.assertEqual(observed["smoke"]["render"]["network_calls_made"], 0)
        self.assertFalse(observed["policy"]["network_required_for_render"])

    def test_missing_runtime_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-missing-manim-") as temporary:
            with self.assertRaises(RUNTIME.ManimRuntimeError):
                RUNTIME.inspect(Path(temporary) / "missing")


if __name__ == "__main__":
    unittest.main()
