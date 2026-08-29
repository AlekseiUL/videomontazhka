from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests.creative_approval_fixture import write_creative_approval
except ModuleNotFoundError:  # CI discovers this file as a top-level module.
    from creative_approval_fixture import write_creative_approval


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from visual_asset_provenance import (  # noqa: E402
    VisualProvenanceError,
    provenance_path_for,
    verify_visual_asset_provenance,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class VirtualCameraProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-camera-provenance-")
        self.edit_dir = Path(self.temporary.name).resolve() / "edit"
        self.edit_dir.mkdir()

        self.plan_path = self.edit_dir / "semantic_plan.json"
        write_json(
            self.plan_path,
            {
                "version": 1,
                "status": "pending",
                "visual_plan": [
                    {
                        "id": "visual-camera-config",
                        "section_id": "section-system",
                        "meaning_ids": ["meaning-system"],
                        "treatment": "Shot-local 1.18x reframe to the Config node.",
                        "purpose": "Make the approved source detail legible.",
                        "approved_text": None,
                        "asset_type": "diagram",
                    }
                ],
            },
        )
        self.approval_path = self.edit_dir / "approval.json"
        write_json(
            self.approval_path,
            {
                "version": 1,
                "proposal_file": "semantic_plan.json",
                "proposal_sha256": sha256(self.plan_path),
                "status": "approved",
                "user_quote": "I approve the exact camera treatment.",
            },
        )
        write_creative_approval(self.edit_dir)

        self.source = self.edit_dir / "work" / "source-backed.mov"
        self.source.parent.mkdir()
        self.source.write_bytes(b"immutable source-backed camera input\n")
        self.output = self.edit_dir / "animations" / "camera-config.mov"
        self.output.parent.mkdir()
        self.output.write_bytes(b"silent virtual camera output\n")
        self.camera_plan = self.edit_dir / "camera" / "video.json"
        self.event = {
            "id": "camera-config",
            "shot_id": "shot-12",
            "start_s": 1.0,
            "end_s": 3.0,
            "target": {"x": 0.64, "y": 0.42},
            "zoom": 1.18,
            "reason": "Emphasize the approved Config detail.",
        }
        self.render_contract = {
            "subpixel_transform_required": True,
            "reset_at_each_shot": True,
            "interpolation": "cubic_smoothstep",
            "continuous_zoompan_across_cuts_forbidden": True,
        }
        write_json(
            self.camera_plan,
            {
                "version": 1,
                "type": "sprut_virtual_camera_plan",
                "generator": "sprut-virtual-camera-plan-1",
                "fps": 30.0,
                "coordinate_space": "normalized_display_top_left",
                "render_contract": self.render_contract,
                "events": [self.event],
                "keyframes": [],
            },
        )

        renderer = SCRIPTS / "render_virtual_camera.py"
        helper = SCRIPTS / "visual_asset_provenance.py"
        self.sidecar = provenance_path_for(self.output)
        write_json(
            self.sidecar,
            {
                "version": 1,
                "type": "sprut_virtual_camera_asset",
                "output": {"path": str(self.output), "sha256": sha256(self.output)},
                "semantic_plan": {
                    "path": str(self.plan_path),
                    "sha256": sha256(self.plan_path),
                },
                "approval": {
                    "path": str(self.approval_path),
                    "sha256": sha256(self.approval_path),
                    "proposal_sha256": sha256(self.plan_path),
                },
                "visual_id": "visual-camera-config",
                "section_id": "section-system",
                "meaning_ids": ["meaning-system"],
                "purpose": "Make the approved source detail legible.",
                "treatment": "Shot-local 1.18x reframe to the Config node.",
                "asset_type": "diagram",
                "approved_text": None,
                "semantic_text": None,
                "source": {"path": str(self.source), "sha256": sha256(self.source)},
                "plan": {"path": str(self.camera_plan), "sha256": sha256(self.camera_plan)},
                "event_id": "camera-config",
                "shot_id": "shot-12",
                "reason": "Emphasize the approved Config detail.",
                "fps": 30.0,
                "frames": 61,
                "audio_streams": 0,
                "render_contract": self.render_contract,
                "review_requirement": "motion_stability_and_exact_boundary_review",
                "renderer": {
                    "path": str(renderer),
                    "sha256": sha256(renderer),
                    "version": "sprut-virtual-camera-render-1",
                    "provenance_helper_path": str(helper),
                    "provenance_helper_sha256": sha256(helper),
                },
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_camera_sidecar_is_accepted_and_bound_to_overlay(self) -> None:
        overlay = {
            "file": str(self.output.relative_to(self.edit_dir)),
            "provenance": str(self.sidecar.relative_to(self.edit_dir)),
            "visual_id": "visual-camera-config",
            "section_id": "section-system",
            "meaning_ids": ["meaning-system"],
            "purpose": "Make the approved source detail legible.",
            "semantic_text": None,
        }
        value = verify_visual_asset_provenance(
            self.edit_dir,
            self.sidecar,
            asset_path=self.output,
            overlay=overlay,
        )
        self.assertEqual(value["type"], "sprut_virtual_camera_asset")
        self.assertEqual(value["event_id"], "camera-config")

    def test_camera_plan_tamper_is_rejected(self) -> None:
        original = self.camera_plan.read_bytes()
        self.camera_plan.write_bytes(original + b"\n")
        with self.assertRaisesRegex(VisualProvenanceError, "plan changed"):
            verify_visual_asset_provenance(self.edit_dir, self.sidecar)


if __name__ == "__main__":
    unittest.main()
