from __future__ import annotations

import hashlib
import json
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
    EXTERNAL_PROVENANCE_TYPE,
    EXTERNAL_REVIEW_REQUIREMENT,
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


class ExternalVisualProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-external-visual-")
        self.video_dir = Path(self.temporary.name)
        self.edit_dir = self.video_dir / "edit"
        self.edit_dir.mkdir()
        source = self.video_dir / "source.bin"
        source.write_bytes(b"immutable visual source\n")
        stat = source.stat()
        write_json(
            self.edit_dir / "project.json",
            {
                "version": 1,
                "name": "external visual provenance fixture",
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
        packed.write_text("# Packed transcripts\n\nVisual-only.\n", encoding="utf-8")
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
            "viewer_promise": "Understand the approved external visual.",
            "audience": "Viewers",
            "source_mode": "long_stream",
            "source_truth": [
                {
                    "id": "meaning-1",
                    "meaning": "The source supports the approved visual.",
                    "evidence": [
                        {
                            "id": "evidence-1",
                            "source": "source-1",
                            "start": 0.0,
                            "end": 1.0,
                            "modality": "visual",
                            "description": "The approved visual concept is supported.",
                        }
                    ],
                }
            ],
            "narrative": [
                {
                    "id": "section-1",
                    "title": "Agent memory",
                    "purpose": "Explain the approved visual.",
                    "meaning_ids": ["meaning-1"],
                    "payoff": "The visual is clear.",
                    "estimated_duration_s": 1.0,
                }
            ],
            "hooks": [
                {
                    "id": "hook-1",
                    "text": "How does agent memory work?",
                    "payoff": "The visual is clear.",
                    "meaning_ids": ["meaning-1"],
                },
                {
                    "id": "hook-2",
                    "text": "See the three memory layers.",
                    "payoff": "The visual is clear.",
                    "meaning_ids": ["meaning-1"],
                },
            ],
            "recommended_hook_id": "hook-1",
            "ending": {
                "section_id": "section-1",
                "meaning_ids": ["meaning-1"],
                "takeaway": "The memory layers are clear.",
            },
            "visual_plan": [
                {
                    "id": "visual-external",
                    "section_id": "section-1",
                    "meaning_ids": ["meaning-1"],
                    "treatment": "A locally generated explanatory diagram.",
                    "purpose": "Explain the approved visual.",
                    "approved_text": "Agent memory has three layers.",
                    "asset_type": "diagram",
                }
            ],
            "audio_plan": {},
            "deliverables": [
                {
                    "id": "video-1",
                    "platform": "YouTube",
                    "width": 640,
                    "height": 360,
                    "fps": 30,
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
        self.asset = self.edit_dir / "animations" / "external.mp4"
        self.asset.parent.mkdir()
        self.asset.write_bytes(b"opaque local visual bytes\n")
        self.source_spec = self.edit_dir / "animations" / "external.html"
        self.source_spec.write_text("<main>Agent memory has three layers.</main>\n", encoding="utf-8")

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
                "user_quote": "I approve this exact external visual plan.",
            },
        )
        write_creative_approval(self.edit_dir)

    def _record(
        self,
        *,
        declared_text: str | None = "AGENT MEMORY — has three layers!",
        source_spec: bool = True,
        force: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPTS / "record_visual_asset.py"),
            "--edit-dir",
            str(self.edit_dir),
            "--asset",
            str(self.asset),
            "--visual-id",
            "visual-external",
        ]
        if declared_text is not None:
            command += ["--declared-visible-text", declared_text]
        if source_spec:
            command += ["--source-spec", str(self.source_spec)]
        if force:
            command.append("--force")
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def test_text_visual_success_binds_source_and_requires_full_preview(self) -> None:
        result = self._record()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sidecar = provenance_path_for(self.asset)
        overlay = {
            "file": str(self.asset.relative_to(self.edit_dir)),
            "visual_id": "visual-external",
            "section_id": "section-1",
            "meaning_ids": ["meaning-1"],
            "purpose": "Explain the approved visual.",
            "semantic_text": "Agent memory has three layers.",
        }
        value = verify_visual_asset_provenance(
            self.edit_dir, sidecar, asset_path=self.asset, overlay=overlay
        )
        self.assertEqual(value["type"], EXTERNAL_PROVENANCE_TYPE)
        self.assertEqual(value["review_requirement"], EXTERNAL_REVIEW_REQUIREMENT)
        self.assertEqual(value["output"]["sha256"], sha256(self.asset))
        self.assertEqual(value["source_spec"]["sha256"], sha256(self.source_spec))
        self.assertEqual(value["declared_visible_text"], "AGENT MEMORY — has three layers!")
        self.assertEqual(
            value["normalized_words"],
            ["agent", "memory", "has", "three", "layers"],
        )
        self.assertNotIn("pixel_ocr", value)

        repeated = self._record()
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("sidecar exists; use --force", repeated.stderr)
        replaced = self._record(force=True)
        self.assertEqual(replaced.returncode, 0, replaced.stdout + replaced.stderr)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

    def test_no_text_visual_success_forbids_a_text_claim(self) -> None:
        self.plan["visual_plan"][0]["approved_text"] = None
        self.plan["visual_plan"][0]["asset_type"] = "b_roll"
        write_json(self.plan_path, self.plan)
        self._refresh_approval()
        result = self._record(declared_text=None, source_spec=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = verify_visual_asset_provenance(
            self.edit_dir, provenance_path_for(self.asset)
        )
        self.assertIsNone(value["approved_text"])
        self.assertIsNone(value["semantic_text"])
        self.assertIsNone(value["declared_visible_text"])
        self.assertEqual(value["normalized_words"], [])
        self.assertNotIn("source_spec", value)

        provenance_path_for(self.asset).unlink()
        rejected = self._record(declared_text="Unapproved words", source_spec=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("non-empty --declared-visible-text is forbidden", rejected.stderr)
        self.assertFalse(provenance_path_for(self.asset).exists())

    def test_wrong_declared_text_is_rejected_without_sidecar(self) -> None:
        result = self._record(declared_text="Agent memory has four layers")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("declared visible words do not exactly match", result.stderr)
        self.assertFalse(provenance_path_for(self.asset).exists())

    def test_asset_source_plan_and_sidecar_tamper_are_rejected(self) -> None:
        result = self._record()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sidecar = provenance_path_for(self.asset)

        original_asset = self.asset.read_bytes()
        self.asset.write_bytes(original_asset + b"tamper")
        with self.assertRaisesRegex(VisualProvenanceError, "output changed"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)
        self.asset.write_bytes(original_asset)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

        original_source_spec = self.source_spec.read_bytes()
        self.source_spec.write_bytes(original_source_spec + b"tamper")
        with self.assertRaisesRegex(VisualProvenanceError, "source_spec changed"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)
        self.source_spec.write_bytes(original_source_spec)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

        original_plan = self.plan_path.read_bytes()
        self.plan_path.write_bytes(original_plan + b"\n")
        with self.assertRaisesRegex(VisualProvenanceError, "semantic_plan changed"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)
        self.plan_path.write_bytes(original_plan)
        verify_visual_asset_provenance(self.edit_dir, sidecar)

        value = json.loads(sidecar.read_text(encoding="utf-8"))
        value["review_requirement"] = "automatic_pixel_approval"
        write_json(sidecar, value)
        with self.assertRaisesRegex(VisualProvenanceError, "review_requirement"):
            verify_visual_asset_provenance(self.edit_dir, sidecar)


if __name__ == "__main__":
    unittest.main()
