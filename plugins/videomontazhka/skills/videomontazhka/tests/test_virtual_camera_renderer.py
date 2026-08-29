from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_virtual_camera.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("render_virtual_camera", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VirtualCameraRendererTest(unittest.TestCase):
    def test_interpolation_is_subframe_and_returns_base(self) -> None:
        frames = [
            {"time_s": 0.0, "zoom": 1.0, "center_x": 0.5, "center_y": 0.5},
            {"time_s": 1.0, "zoom": 1.3, "center_x": 0.7, "center_y": 0.4},
            {"time_s": 2.0, "zoom": 1.0, "center_x": 0.5, "center_y": 0.5},
        ]
        self.assertAlmostEqual(MODULE.interpolate(frames, 0.5, "zoom"), 1.15)
        self.assertAlmostEqual(MODULE.interpolate(frames, 1.5, "center_x"), 0.6)
        self.assertEqual(MODULE.interpolate(frames, 2.0, "zoom"), 1.0)

    def test_renderer_declares_lanczos_subpixel_path(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("cv2.warpAffine", source)
        self.assertIn("cv2.INTER_LANCZOS4", source)
        self.assertIn("require_asset_gate", source)
        self.assertIn('parser.add_argument("--visual-id", required=True)', source)
        self.assertIn("build_virtual_camera_provenance", source)
        self.assertIn("verify_visual_asset_provenance", source)
        self.assertNotIn("zoompan", source)

    def test_exact_cfr_duration_keeps_both_reset_endpoints(self) -> None:
        frames = MODULE.render_frame_count(0.0, 5.7, 30.0)
        self.assertEqual(frames, 171)
        self.assertEqual(MODULE.camera_sample_time(0, frames, 0.0, 5.7), 0.0)
        self.assertEqual(MODULE.camera_sample_time(frames - 1, frames, 0.0, 5.7), 5.7)


if __name__ == "__main__":
    unittest.main()
