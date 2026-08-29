from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class PortabilityTest(unittest.TestCase):
    def test_checked_in_scripts_have_no_author_or_neighbour_skill_paths(self) -> None:
        banned = (
            "/Users/aleksejulanov",
            "SPRUT Video Studio",
            ".codex/skills/video-use",
        )
        violations: list[str] = []
        for path in sorted(SCRIPTS.iterdir()):
            if path.suffix not in {".py", ".m", ".mjs"}:
                continue
            text = path.read_text(encoding="utf-8")
            for marker in banned:
                if marker in text:
                    violations.append(f"{path.name}: {marker}")
        self.assertEqual(violations, [])

    def test_runtime_defaults_follow_environment_in_fresh_processes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-portable-") as temporary:
            app_home = (Path(temporary) / "portable-home").resolve()
            environment = dict(os.environ)
            environment["VIDEOMONTAZHKA_HOME"] = str(app_home)
            expression = (
                "import json,sys;"
                f"sys.path.insert(0,{str(SCRIPTS)!r});"
                "import install_creative_browser_runtime as b,"
                "install_creative_python_runtime as p,install_manim_runtime as m;"
                "print(json.dumps({'browser':str(b.DEFAULT_RUNTIME),"
                "'creative_python':str(p.DEFAULT_RUNTIME),'manim':str(m.DEFAULT_RUNTIME)}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", expression],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual(
                observed,
                {
                    "browser": str(app_home / "runtime" / "creative-browser"),
                    "creative_python": str(app_home / "runtime" / "creative-python"),
                    "manim": str(app_home / "runtime" / "manim"),
                },
            )

    def test_browser_scaffolder_discovery_uses_portable_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-discovery-") as temporary:
            app_home = (Path(temporary) / "app").resolve()
            environment = dict(os.environ)
            environment["VIDEOMONTAZHKA_HOME"] = str(app_home)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "scaffold_creative_browser_effect.py"),
                    "--describe-json",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(Path(payload["defaults"]["studio_root"]), app_home)
            self.assertEqual(
                Path(payload["defaults"]["creative_runtime"]),
                app_home / "runtime" / "creative-browser",
            )
            self.assertEqual(
                Path(payload["defaults"]["gsap_bundle"]),
                app_home / "runtime" / "hyperframes" / "node_modules" / "gsap" / "dist" / "gsap.min.js",
            )


if __name__ == "__main__":
    unittest.main()
