from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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


class VisualAssetProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-visual-provenance-")
        self.video_dir = Path(self.temporary.name)
        self.edit_dir = self.video_dir / "edit"
        self.edit_dir.mkdir()
        source = self.video_dir / "visual-source.bin"
        source.write_bytes(b"immutable visual fixture\n")
        stat = source.stat()
        write_json(
            self.edit_dir / "project.json",
            {
                "version": 1,
                "name": "visual provenance fixture",
                "source_mode": "long_stream",
                "source_manifest": "source_manifest.json",
                "paid_api_allowlist": ["elevenlabs"],
            },
        )
        write_json(
            self.edit_dir / "source_manifest.json",
            {
                "version": 1,
                "root": "..",
                "sources": [
                    {
                        "id": "source-1",
                        "path": source.name,
                        "sha256": sha256(source),
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "duration_s": 2.0,
                        "audio": None,
                    }
                ],
            },
        )
        packed = self.edit_dir / "takes_packed.md"
        packed.write_text(
            "# Packed source transcripts\n\nVisual-only fixture.\n", encoding="utf-8"
        )
        write_json(
            self.edit_dir / "takes_packed_manifest.json",
            {
                "version": 1,
                "output": "takes_packed.md",
                "output_sha256": sha256(packed),
                "silence_threshold_s": 0.5,
                "sources": [
                    {
                        "source": "source-1",
                        "source_sha256": sha256(source),
                        "visual_only": True,
                        "duration_s": 2.0,
                        "phrases": 0,
                    }
                ],
            },
        )
        self.plan_path = self.edit_dir / "semantic_plan.json"
        self.plan = {
            "version": 1,
            "status": "pending",
            "viewer_promise": "Understand the approved visual message.",
            "audience": "Viewers",
            "source_mode": "long_stream",
            "source_truth": [
                {
                    "id": "meaning-1",
                    "meaning": "The source supports the approved visual message.",
                    "evidence": [
                        {
                            "id": "evidence-1",
                            "source": "source-1",
                            "start": 0.0,
                            "end": 1.0,
                            "modality": "visual",
                            "description": "Approved visual message is visible.",
                        }
                    ],
                }
            ],
            "narrative": [
                {
                    "id": "section-1",
                    "title": "Agent memory",
                    "purpose": "Explain the approved visual message.",
                    "meaning_ids": ["meaning-1"],
                    "payoff": "The visual message is clear.",
                    "estimated_duration_s": 1.0,
                }
            ],
            "hooks": [
                {
                    "id": "hook-1",
                    "text": "What is agent memory?",
                    "payoff": "The visual message is clear.",
                    "meaning_ids": ["meaning-1"],
                },
                {
                    "id": "hook-2",
                    "text": "See agent memory clearly.",
                    "payoff": "The visual message is clear.",
                    "meaning_ids": ["meaning-1"],
                },
            ],
            "recommended_hook_id": "hook-1",
            "ending": {
                "section_id": "section-1",
                "meaning_ids": ["meaning-1"],
                "takeaway": "The approved visual message is clear.",
            },
            "visual_plan": [
                {
                    "id": "visual-title",
                    "section_id": "section-1",
                    "meaning_ids": ["meaning-1"],
                    "treatment": "Local black, orange, and white chapter card.",
                    "purpose": "Explain the approved visual message.",
                    "approved_text": "Chapter one: AGENT MEMORY — context and history.",
                    "asset_type": "chapter",
                }
            ],
            "audio_plan": {},
            "deliverables": [
                {
                    "id": "video-1",
                    "platform": "YouTube",
                    "width": 640,
                    "height": 640,
                    "fps": 20,
                    "target_duration_s": 1.0,
                    "minimum_duration_s": 0.5,
                    "subtitle_mode": "none",
                    "section_ids": ["section-1"],
                    "hook_id": "hook-1",
                    "ending_section_id": "section-1",
                }
            ],
        }
        write_json(self.plan_path, self.plan)
        self._refresh_approval()
        self.spec_path = self.edit_dir / "animations" / "card.json"
        self.spec = {
            "kind": "chapter",
            "kicker": "chapter one",
            "title": "Agent memory",
            "body": "Context and history",
            "width": 640,
            "height": 640,
            "fps": 20,
            "duration_s": 1.0,
        }
        write_json(self.spec_path, self.spec)
        self.output = self.edit_dir / "animations" / "card.mp4"
        self.poster = self.edit_dir / "animations" / "card.png"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _refresh_approval(self) -> None:
        write_json(
            self.edit_dir / "approval.json",
            {
                "version": 1,
                "proposal_file": "semantic_plan.json",
                "proposal_sha256": sha256(self.plan_path),
                "status": "approved",
                "approved_scope": [
                    "semantic_structure",
                    "editing_strategy",
                    "visual_strategy",
                ],
                "user_quote": "I approve this exact visual plan.",
            },
        )
        write_creative_approval(self.edit_dir)

    def _render(self, visual_id: str = "visual-title") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "render_motion_card.py"),
                "--edit-dir",
                str(self.edit_dir),
                "--visual-id",
                visual_id,
                str(self.spec_path),
                "-o",
                str(self.output),
                "--poster",
                str(self.poster),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def _require_renderer(self) -> None:
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is required")
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is required")

    def test_success_writes_and_verifies_semantic_sidecar(self) -> None:
        self._require_renderer()
        result = self._render()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sidecar = provenance_path_for(self.output)
        overlay = {
            "file": str(self.output.relative_to(self.edit_dir)),
            "visual_id": "visual-title",
            "section_id": "section-1",
            "meaning_ids": ["meaning-1"],
            "purpose": "Explain the approved visual message.",
            "semantic_text": "Chapter one: AGENT MEMORY — context and history.",
        }
        value = verify_visual_asset_provenance(
            self.edit_dir,
            sidecar,
            asset_path=self.output,
            overlay=overlay,
        )
        self.assertEqual(value["visual_id"], "visual-title")
        self.assertEqual(value["asset_type"], "chapter")
        self.assertEqual(value["card_kind"], "chapter")
        self.assertEqual(
            value["treatment"], "Local black, orange, and white chapter card."
        )
        self.assertEqual(
            value["visible_text"],
            "CHAPTER ONE\nAgent memory\nContext and history",
        )
        self.assertEqual(
            value["normalized_words"],
            ["chapter", "one", "agent", "memory", "context", "and", "history"],
        )
        self.assertEqual(value["output"]["sha256"], sha256(self.output))
        self.assertEqual(value["spec"]["sha256"], sha256(self.spec_path))
        self.assertEqual(value["semantic_plan"]["sha256"], sha256(self.plan_path))
        self.assertEqual(value["poster"]["sha256"], sha256(self.poster))
        self.assertNotIn("created_at", value)

    def test_mismatched_visible_words_leave_no_asset_or_sidecar(self) -> None:
        self._require_renderer()
        self.spec["body"] = "Context without history"
        write_json(self.spec_path, self.spec)
        result = self._render()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("visible words do not exactly match", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(provenance_path_for(self.output).exists())

    def test_wrong_visual_id_leaves_no_asset_or_sidecar(self) -> None:
        self._require_renderer()
        result = self._render("visual-does-not-exist")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must match exactly one approved visual_plan item", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(provenance_path_for(self.output).exists())

    def test_duplicate_visual_id_is_rejected_before_generation(self) -> None:
        duplicate = dict(self.plan["visual_plan"][0])
        self.plan["visual_plan"].append(duplicate)
        write_json(self.plan_path, self.plan)
        self._refresh_approval()
        result = self._render()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicates visual id", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(provenance_path_for(self.output).exists())

    def test_null_approved_text_is_rejected_before_generation(self) -> None:
        self.plan["visual_plan"][0]["approved_text"] = None
        write_json(self.plan_path, self.plan)
        self._refresh_approval()
        result = self._render()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot use a null approved_text", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(provenance_path_for(self.output).exists())

    def test_wrong_card_kind_for_approved_asset_type_is_rejected(self) -> None:
        self.plan["visual_plan"][0]["asset_type"] = "title"
        write_json(self.plan_path, self.plan)
        self._refresh_approval()
        result = self._render()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "kind 'chapter' requires approved asset_type 'chapter', got 'title'",
            result.stderr,
        )
        self.assertFalse(self.output.exists())
        self.assertFalse(provenance_path_for(self.output).exists())

    def test_changed_asset_spec_plan_and_sidecar_are_rejected(self) -> None:
        self._require_renderer()
        result = self._render()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sidecar = provenance_path_for(self.output)

        original_asset = self.output.read_bytes()
        self.output.write_bytes(original_asset + b"tamper")
        with self.assertRaisesRegex(VisualProvenanceError, "output changed"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)
        self.output.write_bytes(original_asset)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

        original_spec = self.spec_path.read_bytes()
        changed_spec = dict(self.spec)
        changed_spec["title"] = "Changed memory"
        write_json(self.spec_path, changed_spec)
        with self.assertRaisesRegex(VisualProvenanceError, "spec changed"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)
        self.spec_path.write_bytes(original_spec)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

        original_plan = self.plan_path.read_bytes()
        self.plan_path.write_bytes(original_plan + b"\n")
        with self.assertRaisesRegex(VisualProvenanceError, "semantic_plan changed"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)
        self.plan_path.write_bytes(original_plan)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

        sidecar_value = json.loads(sidecar.read_text(encoding="utf-8"))
        sidecar_value["visual_id"] = "visual-does-not-exist"
        write_json(sidecar, sidecar_value)
        with self.assertRaisesRegex(VisualProvenanceError, "must match exactly one"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)


if __name__ == "__main__":
    unittest.main()
