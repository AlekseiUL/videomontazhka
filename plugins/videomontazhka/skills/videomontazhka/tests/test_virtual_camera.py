from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_virtual_camera.py"


class VirtualCameraTest(unittest.TestCase):
    def test_self_test(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), "--self-test"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")

    def test_rejects_weak_or_excessive_zoom_and_missing_reason(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import plan_virtual_camera
        base = {
            "version": 1, "type": "sprut_virtual_camera_brief", "fps": 30,
            "events": [{
                "id": "one", "shot_id": "shot", "start_s": 0, "end_s": 2,
                "target": {"x": 0.5, "y": 0.5}, "zoom": 1.36, "reason": "too much zoom",
            }],
        }
        with self.assertRaises(ValueError):
            plan_virtual_camera.build(base)
        base["events"][0]["zoom"] = 1.2
        base["events"][0]["reason"] = "x"
        with self.assertRaises(ValueError):
            plan_virtual_camera.build(base)
        base["events"][0]["reason"] = "Concrete approved payoff emphasis."
        base["events"][0]["zoom"] = 1.08
        with self.assertRaises(ValueError):
            plan_virtual_camera.build(base)


if __name__ == "__main__":
    unittest.main()
