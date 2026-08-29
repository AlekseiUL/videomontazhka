from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "creative_tool_registry.py"


class CreativeToolRegistryTest(unittest.TestCase):
    def test_registry_is_offline_and_has_no_paid_creative_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-registry-") as temporary:
            environment = dict(os.environ)
            environment["VIDEOMONTAZHKA_HOME"] = str(Path(temporary) / "app")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--json"],
                text=True,
                capture_output=True,
                env=environment,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["network_calls_made"], 0)
        self.assertEqual(report["paid_api_allowlist"], ["elevenlabs"])
        for engine in report["engines"].values():
            self.assertFalse(engine["paid_api"])
            self.assertTrue(engine["local_only"])
        self.assertIn(report["engines"]["rhythm_analysis"]["status"], {"ready", "limited"})
        self.assertEqual(report["engines"]["shot_aware_camera"]["status"], "limited")
        self.assertEqual(report["engines"]["gsap_creative_adapter"]["status"], "ready")
        self.assertEqual(report["engines"]["browser_creative_adapter"]["status"], "ready")
        self.assertEqual(report["engines"]["manim"]["status"], "on_demand")
        self.assertEqual(report["engines"]["manim"]["capabilities"], [])

    def test_unknown_requirement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-registry-") as temporary:
            environment = dict(os.environ)
            environment["VIDEOMONTAZHKA_HOME"] = str(Path(temporary) / "app")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--json", "--require", "does_not_exist"],
                text=True,
                capture_output=True,
                env=environment,
            )
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["requirements"]["missing"], ["does_not_exist"])


if __name__ == "__main__":
    unittest.main()
