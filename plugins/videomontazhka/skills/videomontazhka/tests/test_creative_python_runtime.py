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

from runtime_paths import CREATIVE_PYTHON_RUNTIME  # noqa: E402

SCRIPT = ROOT / "scripts" / "install_creative_python_runtime.py"
SPEC = importlib.util.spec_from_file_location("creative_python_runtime", SCRIPT)
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class CreativePythonRuntimeTest(unittest.TestCase):
    def test_shared_runtime_path_is_independent_of_skill_install_location(self) -> None:
        self.assertEqual(
            RUNTIME.DEFAULT_RUNTIME,
            CREATIVE_PYTHON_RUNTIME,
        )

    def test_requirements_exactly_match_expected_versions(self) -> None:
        parsed = {}
        for line in RUNTIME.REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                name, version = line.split("==", 1)
                parsed[name] = version
        self.assertEqual(parsed, RUNTIME.EXPECTED)
        self.assertEqual(set(RUNTIME.EXPECTED), set(RUNTIME.LICENSES))

    def test_installed_runtime_is_isolated_arm64_and_manifest_bound(self) -> None:
        if not RUNTIME.DEFAULT_RUNTIME.is_dir():
            self.skipTest("creative Python runtime is not installed")
        observed = RUNTIME.inspect(RUNTIME.DEFAULT_RUNTIME)
        RUNTIME.verify_manifest(RUNTIME.DEFAULT_RUNTIME, observed)
        self.assertEqual(observed["machine"], "arm64")
        self.assertEqual(
            observed["requirements"]["path"],
            "assets/creative-python-requirements.v1.txt",
        )
        self.assertTrue(observed["policy"]["isolated_product_runtime"])
        self.assertEqual(observed["smoke"]["status"], "PASS")

    def test_missing_runtime_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-missing-creative-python-") as temporary:
            with self.assertRaises(RUNTIME.RuntimeErrorChecked):
                RUNTIME.inspect(Path(temporary) / "missing")


if __name__ == "__main__":
    unittest.main()
