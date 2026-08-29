from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests.test_visual_preview_sheet import make_approved_edit, sha256


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class TransitionAssetQATest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-transition-qa-")
        self.root = Path(self.temporary.name)
        self.edit_dir = make_approved_edit(self.root, visual_id="visual-transition")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def encode_frames(self, name: str, colors: list[tuple[int, int, int]]) -> Path:
        frames_dir = self.root / f"{name}-frames"
        frames_dir.mkdir()
        for index, color in enumerate(colors):
            Image.new("RGB", (160, 90), color).save(frames_dir / f"frame_{index:02d}.png")
        output = self.edit_dir / "animations" / f"{name}.mp4"
        output.parent.mkdir(exist_ok=True)
        encoded = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                "10",
                "-i",
                str(frames_dir / "frame_%02d.png"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(output),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(encoded.returncode, 0, encoded.stdout + encoded.stderr)
        return output

    def run_qa(
        self,
        asset: Path,
        *,
        duration: float,
        mode: str = "full-frame",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "qa_transition_asset.py"),
                "--edit-dir",
                str(self.edit_dir),
                "--visual-id",
                "visual-transition",
                "--asset",
                str(asset),
                "--mode",
                mode,
                "--expected-duration",
                str(duration),
                "--expected-fps",
                "10",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def only_report(self) -> tuple[Path, dict[str, object]]:
        reports = list((self.edit_dir / "verify").glob("transition_qa_*.json"))
        self.assertEqual(len(reports), 1)
        return reports[0], json.loads(reports[0].read_text(encoding="utf-8"))

    def test_smooth_full_frame_asset_passes_and_report_is_hash_bound(self) -> None:
        asset = self.encode_frames(
            "smooth", [(48, 48, 48), (52, 52, 52), (56, 56, 56), (60, 60, 60), (64, 64, 64), (68, 68, 68)]
        )
        result = self.run_qa(asset, duration=0.6)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report_path, report = self.only_report()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["asset"]["sha256"], sha256(asset))
        self.assertEqual(report["decode"]["frame_count"], 6)
        self.assertTrue(report["checks"]["duration"]["pass"])
        self.assertTrue(report["checks"]["fps"]["pass"])
        self.assertTrue(report["checks"]["all_black_frames"]["pass"])
        self.assertTrue(report["checks"]["severe_adjacent_flash"]["pass"])
        self.assertTrue(report_path.is_relative_to(self.edit_dir / "verify"))

    def test_black_frame_and_severe_flash_fail_but_write_diagnostics(self) -> None:
        asset = self.encode_frames(
            "flash-black",
            [(48, 48, 48), (255, 255, 255), (0, 0, 0), (48, 48, 48)],
        )
        result = self.run_qa(asset, duration=0.4)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, report = self.only_report()
        self.assertEqual(report["status"], "FAIL")
        self.assertGreater(report["checks"]["all_black_frames"]["count"], 0)
        self.assertGreater(report["checks"]["severe_adjacent_flash"]["count"], 0)
        self.assertIn("all_black_frames", report["failures"])
        self.assertIn("severe_adjacent_flash", report["failures"])

    def test_alpha_mode_rejects_an_opaque_asset_with_coverage_evidence(self) -> None:
        asset = self.encode_frames("opaque", [(80, 80, 80)] * 5)
        result = self.run_qa(asset, duration=0.5, mode="alpha")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        _, report = self.only_report()
        alpha = report["checks"]["alpha"]
        self.assertFalse(alpha["pass"])
        self.assertFalse(alpha["pixel_format_declares_alpha"])
        self.assertEqual(alpha["coverage"]["nonopaque_pixel_fraction"]["maximum"], 0.0)

    def test_missing_approval_prevents_decode_and_report_creation(self) -> None:
        asset = self.encode_frames("blocked", [(64, 64, 64)] * 5)
        (self.edit_dir / "approval.json").unlink()
        result = self.run_qa(asset, duration=0.5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("asset gate failed", result.stderr)
        self.assertFalse((self.edit_dir / "verify").exists())

    def test_self_test_uses_no_project_and_detects_synthetic_failures(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "qa_transition_asset.py"), "--self-test"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["checks"]["smooth_false_positives"], 0)
        self.assertEqual(report["checks"]["flash_pairs_detected"], 2)
        self.assertGreater(report["checks"]["black_frames_detected"], 0)
        self.assertGreater(report["checks"]["alpha_nonopaque_fraction_max"], 0)


if __name__ == "__main__":
    unittest.main()
