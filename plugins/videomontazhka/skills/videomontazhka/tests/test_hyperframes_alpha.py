from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import normalize_hyperframes_alpha as alpha  # noqa: E402


class HyperFramesAlphaTest(unittest.TestCase):
    def test_command_forces_alpha_preserving_decoder_and_prores_4444(self) -> None:
        command = alpha.normalize_command(
            "/opt/homebrew/bin/ffmpeg",
            Path("input.webm"),
            Path("output.mov"),
        )
        decoder_index = command.index("libvpx-vp9")
        input_index = command.index("-i")
        self.assertLess(decoder_index, input_index)
        self.assertIn("prores_ks", command)
        self.assertIn("yuva444p10le", command)
        self.assertIn("-an", command)

    def test_self_test_preserves_synthetic_alpha_without_network(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "normalize_hyperframes_alpha.py"), "--self-test"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"self_test": "PASS"', result.stdout)
        self.assertIn('"pixel_format": "yuva', result.stdout)
        self.assertIn('"network_calls": 0', result.stdout)

    def test_project_mode_requires_complete_arguments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-alpha-args-") as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "normalize_hyperframes_alpha.py"),
                    "--edit-dir",
                    str(Path(temporary) / "edit"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --edit-dir", result.stderr)


if __name__ == "__main__":
    unittest.main()
