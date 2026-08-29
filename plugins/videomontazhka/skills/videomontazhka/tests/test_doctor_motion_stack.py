from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor  # noqa: E402


class DoctorMotionStackTest(unittest.TestCase):
    def test_explicit_hyperframes_binary_has_priority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-doctor-hf-") as temporary:
            fake = Path(temporary) / "hyperframes"
            fake.write_text("#!/bin/sh\nprintf '9.9.9\\n'\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"VIDEOMONTAZHKA_HYPERFRAMES_BIN": str(fake)}):
                self.assertEqual(doctor.find_hyperframes_cli(), fake.resolve())

    def test_json_contract_reports_local_motion_and_vision_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-doctor-home-") as temporary:
            app_home = Path(temporary) / "app-data"
            environment = dict(os.environ)
            environment["VIDEOMONTAZHKA_HOME"] = str(app_home)
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "doctor.py"), "--json"],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
        self.assertIn(result.returncode, {0, 1}, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["network_calls_made"], 0)
        self.assertEqual(report["paid_api_allowlist"], ["elevenlabs"])
        self.assertEqual(Path(report["paths"]["application_home"]), app_home.resolve())
        self.assertEqual(Path(report["paths"]["runtime_root"]), app_home.resolve() / "runtime")
        python_runtime = report["required"]["python_runtime"]
        self.assertIn("recommended_install_command", python_runtime)
        hyperframes = report["optional"]["hyperframes_local"]
        for field in ("ready", "installed", "path", "version", "node"):
            self.assertIn(field, hyperframes)
        self.assertFalse(hyperframes["network_required_for_render"])
        manim = report["optional"]["manim_local"]
        self.assertFalse(manim["ready"], manim)
        self.assertIsNone(manim["path"])
        self.assertEqual(
            Path(manim["runtime_manifest"]),
            app_home.resolve() / "runtime" / "manim" / "RUNTIME_MANIFEST.json",
        )
        vision = report["optional"]["apple_vision"]
        self.assertTrue(vision["person_segmenter_source"].endswith("segment_person.m"))
        self.assertTrue(vision["person_matte_wrapper"].endswith("person_matte.py"))
        arsenal = report["optional"]["creative_arsenal"]
        self.assertFalse(arsenal["ready"], arsenal)
        self.assertEqual(arsenal["paid_api_allowlist"], ["elevenlabs"])
        self.assertEqual(arsenal["network_calls_made"], 0)
        self.assertIn("browser_creative_adapter", arsenal["ready_engines"])
        self.assertIn("gsap_creative_adapter", arsenal["ready_engines"])


if __name__ == "__main__":
    unittest.main()
