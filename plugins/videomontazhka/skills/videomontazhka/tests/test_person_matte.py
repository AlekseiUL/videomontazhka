from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import person_matte  # noqa: E402
from asset_gate import AssetGateError  # noqa: E402


class PersonMatteContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-person-matte-test-")
        self.root = Path(self.temporary.name)
        self.edit = self.root / "edit"
        self.edit.mkdir()
        self.source = self.root / "source.mov"
        self.source.write_bytes(b"fixture only; native tool is not invoked")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(
        self,
        *,
        matte: Path | None = Path("animations/matte.mov"),
        foreground: Path | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            self_test=False,
            edit_dir=self.edit,
            input=self.source,
            matte=matte,
            foreground=foreground,
            quality="accurate",
        )

    def test_compile_command_uses_only_native_local_frameworks(self) -> None:
        command = person_matte.compile_command(
            "/usr/bin/xcrun",
            SCRIPTS / "segment_person.m",
            self.root / "segment_person",
        )
        self.assertEqual(command[:2], ["/usr/bin/xcrun", "clang"])
        self.assertIn("-Werror", command)
        frameworks = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-framework"
        ]
        self.assertEqual(tuple(frameworks), person_matte.FRAMEWORKS)
        self.assertNotIn("coremltools", " ".join(command).lower())
        self.assertNotIn("rvm", " ".join(command).lower())

    def test_native_source_uses_stateful_vision_and_wrapper_encodes_prores_4444(self) -> None:
        source = (SCRIPTS / "segment_person.m").read_text(encoding="utf-8")
        self.assertIn("VNGeneratePersonSegmentationRequest", source)
        self.assertIn("VNSequenceRequestHandler", source)
        self.assertIn("kCVPixelFormatType_OneComponent8", source)
        self.assertIn("WriteRawFrame", source)
        command = person_matte.raw_encode_command(
            "/opt/homebrew/bin/ffmpeg",
            self.root / "foreground.mov",
            width=640,
            height=360,
            fps=30.0,
        )
        self.assertIn("prores_ks", command)
        self.assertIn("4444", command)
        self.assertIn("yuva444p10le", command)
        self.assertIn("-an", command)

    def test_output_escape_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssetGateError, "must be under"):
            person_matte.prepare_project_request(
                self.args(matte=self.root / "outside.mov")
            )

    def test_requires_mov_and_distinct_outputs(self) -> None:
        with self.assertRaisesRegex(person_matte.PersonMatteError, "must use .mov"):
            person_matte.prepare_project_request(
                self.args(matte=Path("animations/matte.mp4"))
            )
        with self.assertRaisesRegex(person_matte.PersonMatteError, "different files"):
            person_matte.prepare_project_request(
                self.args(
                    matte=Path("animations/same.mov"),
                    foreground=Path("animations/same.mov"),
                )
            )

    def test_requires_at_least_one_output(self) -> None:
        with self.assertRaisesRegex(person_matte.PersonMatteError, "at least one"):
            person_matte.prepare_project_request(self.args(matte=None, foreground=None))

    def test_asset_gate_blocks_before_any_work_directory_or_output_parent(self) -> None:
        request = person_matte.prepare_project_request(
            self.args(matte=Path("animations/new/matte.mov"))
        )
        with mock.patch.object(
            person_matte,
            "require_asset_gate",
            side_effect=AssetGateError("semantic approval missing"),
        ) as gate:
            with self.assertRaisesRegex(AssetGateError, "approval missing"):
                person_matte.run_project(request)
        gate.assert_called_once_with(self.edit.resolve())
        self.assertFalse((self.edit / "work").exists())
        self.assertFalse((self.edit / "animations").exists())

    def test_cli_missing_approval_fails_closed_before_native_probe(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "person_matte.py"),
                "--edit-dir",
                str(self.edit),
                "--input",
                str(self.source),
                "--matte",
                "animations/matte.mov",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("asset gate failed", result.stderr)
        self.assertFalse((self.edit / "work").exists())
        self.assertFalse((self.edit / "animations").exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        output = self.edit / "animations" / "matte.mov"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"user-owned existing output")
        with self.assertRaisesRegex(person_matte.PersonMatteError, "refusing to overwrite"):
            person_matte.prepare_project_request(self.args())
        self.assertEqual(output.read_bytes(), b"user-owned existing output")


@unittest.skipUnless(
    sys.platform == "darwin"
    and shutil.which("xcrun")
    and shutil.which("ffmpeg")
    and shutil.which("ffprobe")
    and os.environ.get("SPRUT_RUN_PERSON_MATTE_SELF_TEST") == "1",
    "set SPRUT_RUN_PERSON_MATTE_SELF_TEST=1 to run the native synthetic smoke test",
)
class PersonMatteNativeSelfTest(unittest.TestCase):
    def test_synthetic_self_test(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "person_matte.py"), "--self-test"],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"self_test": "PASS"', result.stdout)
        self.assertIn('"synthetic_only": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
