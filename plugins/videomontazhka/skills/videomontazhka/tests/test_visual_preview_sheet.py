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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def make_approved_edit(root: Path, *, visual_id: str = "visual-preview") -> Path:
    edit_dir = root / "edit"
    edit_dir.mkdir()
    source = root / "source.bin"
    source.write_bytes(b"immutable visual-only source\n")
    stat = source.stat()
    write_json(
        edit_dir / "project.json",
        {
            "version": 1,
            "name": "visual preview fixture",
            "source_mode": "long_stream",
            "source_manifest": "source_manifest.json",
            "paid_api_allowlist": ["elevenlabs"],
        },
    )
    write_json(
        edit_dir / "source_manifest.json",
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
    packed = edit_dir / "takes_packed.md"
    packed.write_text("# Packed transcripts\n\nVisual-only fixture.\n", encoding="utf-8")
    write_json(
        edit_dir / "takes_packed_manifest.json",
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
    plan = {
        "version": 1,
        "status": "pending",
        "viewer_promise": "Understand the approved transition visual.",
        "audience": "Viewers",
        "source_mode": "long_stream",
        "source_truth": [
            {
                "id": "meaning-1",
                "meaning": "The source supports the approved visual treatment.",
                "evidence": [
                    {
                        "id": "evidence-1",
                        "source": "source-1",
                        "start": 0.0,
                        "end": 1.0,
                        "modality": "visual",
                        "description": "The source contains the approved visual subject.",
                    }
                ],
            }
        ],
        "narrative": [
            {
                "id": "section-1",
                "title": "Approved visual",
                "purpose": "Show the approved visual treatment.",
                "meaning_ids": ["meaning-1"],
                "payoff": "The transition is clear.",
                "estimated_duration_s": 1.0,
            }
        ],
        "hooks": [
            {
                "id": "hook-1",
                "text": "See the approved transition.",
                "payoff": "The transition is clear.",
                "meaning_ids": ["meaning-1"],
            },
            {
                "id": "hook-2",
                "text": "How does the transition support the idea?",
                "payoff": "The transition is clear.",
                "meaning_ids": ["meaning-1"],
            },
        ],
        "recommended_hook_id": "hook-1",
        "ending": {
            "section_id": "section-1",
            "meaning_ids": ["meaning-1"],
            "takeaway": "The approved transition supports the explanation.",
        },
        "visual_plan": [
            {
                "id": visual_id,
                "section_id": "section-1",
                "meaning_ids": ["meaning-1"],
                "treatment": "Use one restrained full-frame transition asset.",
                "purpose": "Make the approved topic change clear.",
                "approved_text": None,
                "asset_type": "b_roll",
            }
        ],
        "audio_plan": {
            "cleanup": "Keep the visual-only fixture muted.",
            "target_lufs": -14.0,
            "true_peak_dbtp": -1.0,
        },
        "deliverables": [
            {
                "id": "video-1",
                "platform": "YouTube",
                "width": 640,
                "height": 360,
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
    plan_path = edit_dir / "semantic_plan.json"
    write_json(plan_path, plan)
    write_json(
        edit_dir / "approval.json",
        {
            "version": 1,
            "proposal_file": "semantic_plan.json",
            "proposal_sha256": sha256(plan_path),
            "status": "approved",
            "approved_scope": [
                "semantic_structure",
                "editing_strategy",
                "visual_strategy",
            ],
            "user_quote": "I approve this exact visual plan.",
        },
    )
    write_creative_approval(edit_dir)
    return edit_dir


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class VisualPreviewSheetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-preview-sheet-")
        self.root = Path(self.temporary.name)
        self.edit_dir = make_approved_edit(self.root)
        self.asset = self.edit_dir / "animations" / "transition.mp4"
        self.asset.parent.mkdir()
        generated = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x303030:s=160x90:r=10:d=1",
                "-vf",
                "drawbox=x=10+40*t:y=20:w=32:h=32:color=0xFF6A00:t=fill",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(self.asset),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_preview(self, timestamps: tuple[str, ...] = ("0.0", "0.4", "0.8")) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPTS / "build_visual_preview_sheet.py"),
            "--edit-dir",
            str(self.edit_dir),
            "--visual-id",
            "visual-preview",
            "--asset",
            str(self.asset),
        ]
        for timestamp in timestamps:
            command += ["--timestamp", timestamp]
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def test_builds_three_hashed_frames_sheet_and_manifest_under_verify(self) -> None:
        result = self.run_preview()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifests = list((self.edit_dir / "verify").glob("visual_preview_*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["visual_id"], "visual-preview")
        self.assertEqual(manifest["timestamps_s"], [0.0, 0.4, 0.8])
        self.assertEqual(manifest["asset"]["sha256"], sha256(self.asset))
        self.assertEqual(len(manifest["frames"]), 3)
        for record in manifest["frames"]:
            frame = Path(record["path"])
            self.assertTrue(frame.is_file())
            self.assertEqual(record["sha256"], sha256(frame))
            self.assertTrue(frame.resolve().is_relative_to((self.edit_dir / "verify").resolve()))
        sheet = Path(manifest["contact_sheet"]["path"])
        self.assertTrue(sheet.is_file())
        self.assertEqual(manifest["contact_sheet"]["sha256"], sha256(sheet))
        self.assertEqual(manifest["semantic_plan"]["sha256"], sha256(self.edit_dir / "semantic_plan.json"))
        self.assertEqual(manifest["approval"]["sha256"], sha256(self.edit_dir / "approval.json"))

    def test_missing_approval_fails_before_verify_or_frame_creation(self) -> None:
        (self.edit_dir / "approval.json").unlink()
        result = self.run_preview()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("asset gate failed", result.stderr)
        self.assertFalse((self.edit_dir / "verify").exists())

    def test_requires_exactly_three_or_four_explicit_timestamps(self) -> None:
        result = self.run_preview(("0.1", "0.5"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly 3 or 4", result.stderr)
        self.assertFalse((self.edit_dir / "verify").exists())

    def test_rejects_timestamp_outside_asset_after_gate_without_frames(self) -> None:
        result = self.run_preview(("0.1", "0.5", "1.5"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside asset duration", result.stderr)
        self.assertFalse((self.edit_dir / "verify").exists())


if __name__ == "__main__":
    unittest.main()
