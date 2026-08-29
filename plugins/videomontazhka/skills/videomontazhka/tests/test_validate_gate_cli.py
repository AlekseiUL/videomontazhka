from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_gate.py"


class ValidateGateCliTests(unittest.TestCase):
    def test_edl_phases_require_the_exact_edl_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-gate-cli-") as temporary:
            for phase in ("edl", "render", "final"):
                with self.subTest(phase=phase):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "--edit-dir",
                            temporary,
                            "--phase",
                            phase,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("--edl is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
