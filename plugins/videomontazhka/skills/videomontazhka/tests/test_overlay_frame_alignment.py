from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import render_edl as renderer  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class OverlayFrameAlignmentTest(unittest.TestCase):
    def run_ffmpeg(self, *arguments: object, capture_output: bool = False) -> bytes:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *(str(value) for value in arguments)],
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def test_align_to_end_covers_the_exact_last_programme_frame(self) -> None:
        """A two-frame overlay ending at frame 6 must cover frames 4 and 5."""
        with tempfile.TemporaryDirectory(prefix="sprut-overlay-frame-") as temporary:
            edit_dir = Path(temporary)
            base = edit_dir / "base.mov"
            overlay = edit_dir / "overlay.mp4"
            output = edit_dir / "composited.mov"

            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-frames:v", "6", "-t", "0.2", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", base,
            )
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=30",
                "-frames:v", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", overlay,
            )

            profile = renderer.Profile(
                mode="preview",
                declared_width=64,
                declared_height=64,
                width=64,
                height=64,
                fps=Fraction(30, 1),
                preset="ultrafast",
                crf=18,
            )
            overlays = renderer.resolve_visual_overlays(
                [{"file": str(overlay), "duration": 2 / 30, "align_to_end": True, "full_frame": True}],
                [{"frames": 6}],
                profile,
                total_frames=6,
            )
            self.assertEqual((overlays[0]["start_frame"], overlays[0]["end_frame"]), (4, 6))

            renderer.composite(
                base,
                overlays,
                [],
                None,
                profile,
                edit_dir,
                duration=0.2,
                total_samples=9_600,
                output=output,
            )

            last_frame = self.run_ffmpeg(
                "-i", output,
                "-vf", "select=eq(n\\,5)",
                "-frames:v", "1",
                "-pix_fmt", "rgb24",
                "-f", "rawvideo",
                "-",
                capture_output=True,
            )
            self.assertEqual(len(last_frame), 64 * 64 * 3)
            red = sum(last_frame[0::3]) / (64 * 64)
            blue = sum(last_frame[2::3]) / (64 * 64)
            self.assertGreater(blue, 150, "last programme frame fell back to the red base")
            self.assertLess(red, 80, "last programme frame fell back to the red base")


if __name__ == "__main__":
    unittest.main()
