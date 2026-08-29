from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
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
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AssetGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-asset-gate-")
        self.videos_dir = Path(self.temporary.name)
        self.edit_dir = self.videos_dir / "edit"
        self.edit_dir.mkdir()
        self.source = self.videos_dir / "visual-source.bin"
        self.source.write_bytes(b"immutable visual source\n")
        stat = self.source.stat()

        write_json(
            self.edit_dir / "project.json",
            {
                "version": 1,
                "name": "asset gate fixture",
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
                        "path": self.source.name,
                        "sha256": sha256(self.source),
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "duration_s": 2.0,
                        "audio": None,
                    }
                ],
            },
        )
        packed = self.edit_dir / "takes_packed.md"
        packed.write_text("# Packed source transcripts\n\nVisual-only fixture.\n", encoding="utf-8")
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
                        "source_sha256": sha256(self.source),
                        "visual_only": True,
                        "duration_s": 2.0,
                        "phrases": 0,
                    }
                ],
            },
        )
        self.plan_path = self.edit_dir / "semantic_plan.json"
        write_json(
            self.plan_path,
            {
                "version": 1,
                "status": "pending",
                "viewer_promise": "Understand the visible result clearly.",
                "audience": "Viewers",
                "source_mode": "long_stream",
                "source_truth": [
                    {
                        "id": "meaning-1",
                        "meaning": "The source contains the approved visible result.",
                        "evidence": [
                            {
                                "id": "evidence-1",
                                "source": "source-1",
                                "start": 0.0,
                                "end": 1.0,
                                "modality": "visual",
                                "description": "approved visible result",
                            }
                        ],
                    }
                ],
                "narrative": [
                    {
                        "id": "section-1",
                        "title": "Result",
                        "purpose": "Show the visible result.",
                        "meaning_ids": ["meaning-1"],
                        "payoff": "The result is clear.",
                        "estimated_duration_s": 1.0,
                    }
                ],
                "hooks": [
                    {
                        "id": "hook-1",
                        "text": "See the approved result.",
                        "payoff": "The result is visible.",
                        "meaning_ids": ["meaning-1"],
                    },
                    {
                        "id": "hook-2",
                        "text": "What does the source show?",
                        "payoff": "The result is visible.",
                        "meaning_ids": ["meaning-1"],
                    },
                ],
                "recommended_hook_id": "hook-1",
                "ending": {
                    "section_id": "section-1",
                    "meaning_ids": ["meaning-1"],
                    "takeaway": "The approved result is visible.",
                },
                "visual_plan": [],
                "audio_plan": {
                    "cleanup": "Keep the visual-only source muted.",
                    "target_lufs": -14.0,
                    "true_peak_dbtp": -1.0,
                },
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
            },
        )
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
                "user_quote": "I approve this exact plan.",
            },
        )
        write_creative_approval(self.edit_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(self, name: str, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *(str(value) for value in arguments)],
            text=True,
            capture_output=True,
            check=False,
        )

    def make_audio_source(self) -> Path:
        source = self.videos_dir / "audio-source.wav"
        with wave.open(str(source), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48_000)
            frames = bytearray()
            for index in range(48_000):
                sample = round(8_000 * math.sin(2 * math.pi * 220 * index / 48_000))
                frames.extend(struct.pack("<h", sample))
            handle.writeframes(frames)
        return source

    def make_ab_approval(self) -> tuple[Path, Path, Path]:
        source = self.make_audio_source()
        preview_dir = self.edit_dir / "audio" / "ab"
        preview = self.run_script(
            "audio_polish.py",
            "preview",
            "--edit-dir",
            self.edit_dir,
            source,
            "--output-dir",
            preview_dir,
            "--start",
            "0.1",
            "--duration",
            "0.4",
        )
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        decision = preview_dir / "ab_decision.json"
        approval = preview_dir / "ab_approval.json"
        recorded = self.run_script(
            "audio_polish.py",
            "approve",
            "--edit-dir",
            self.edit_dir,
            "--decision",
            decision,
            "--quote",
            "I approve processed preview B exactly.",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        return source, decision, approval

    def reapprove_plan(self, plan: dict[str, object]) -> None:
        write_json(self.plan_path, plan)
        approval_path = self.edit_dir / "approval.json"
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["proposal_sha256"] = sha256(self.plan_path)
        write_json(approval_path, approval)

    def test_semantic_plan_requires_visual_and_audio_plans(self) -> None:
        for field in ("visual_plan", "audio_plan"):
            with self.subTest(field=field):
                plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
                plan.pop(field)
                self.reapprove_plan(plan)
                gate = self.run_script(
                    "validate_gate.py", "--edit-dir", self.edit_dir, "--phase", "asset"
                )
                self.assertNotEqual(gate.returncode, 0, gate.stdout + gate.stderr)
                self.assertIn(field, gate.stdout + gate.stderr)
                # Restore the canonical fixture for the next subtest.
                plan[field] = [] if field == "visual_plan" else {
                    "cleanup": "Keep the visual-only source muted."
                }
                self.reapprove_plan(plan)

    def test_visual_plan_item_requires_asset_type(self) -> None:
        plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        plan["visual_plan"] = [{
            "id": "visual-title",
            "section_id": "section-1",
            "meaning_ids": ["meaning-1"],
            "treatment": "Show an approved explanatory title.",
            "purpose": "Clarify the visible result.",
            "approved_text": "Approved result",
        }]
        self.reapprove_plan(plan)
        gate = self.run_script(
            "validate_gate.py", "--edit-dir", self.edit_dir, "--phase", "asset"
        )
        self.assertNotEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertIn("asset_type", gate.stdout + gate.stderr)

    def test_asset_phase_passes_without_edl_and_allows_sfx(self) -> None:
        gate = self.run_script(
            "validate_gate.py", "--edit-dir", self.edit_dir, "--phase", "asset"
        )
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertFalse((self.edit_dir / "edl.json").exists())

        output = self.edit_dir / "audio" / "approved-hit.wav"
        generated = self.run_script(
            "generate_sfx.py",
            "--edit-dir",
            self.edit_dir,
            "hit",
            "-o",
            output,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertTrue(output.is_file())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_approved_motion_card_generation_works(self) -> None:
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is required")
        plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        plan["visual_plan"] = [{
            "id": "approved-card",
            "section_id": "section-1",
            "meaning_ids": ["meaning-1"],
            "treatment": "Show a restrained approved title card.",
            "purpose": "Clarify the visible result.",
            "approved_text": "Approved asset",
            "asset_type": "title",
        }]
        self.reapprove_plan(plan)
        spec = self.edit_dir / "animations" / "approved-card.json"
        spec.parent.mkdir()
        write_json(
            spec,
            {
                "kind": "title",
                "title": "Approved asset",
                "width": 640,
                "height": 360,
                "fps": 20,
                "duration_s": 1.0,
            },
        )
        output = self.edit_dir / "animations" / "approved-card.mp4"
        poster = self.edit_dir / "animations" / "approved-card.png"
        generated = self.run_script(
            "render_motion_card.py",
            "--edit-dir",
            self.edit_dir,
            "--visual-id",
            "approved-card",
            spec,
            "-o",
            output,
            "--poster",
            poster,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertTrue(output.is_file())
        self.assertTrue(poster.is_file())

    def test_missing_approval_leaves_each_writer_output_absent(self) -> None:
        (self.edit_dir / "approval.json").unlink()
        card_spec = self.edit_dir / "card.json"
        write_json(card_spec, {"kind": "title", "title": "Blocked", "duration_s": 1})

        cases = [
            (
                "generate_sfx.py",
                ("--edit-dir", self.edit_dir, "hit", "-o", self.edit_dir / "sfx" / "x.wav"),
                self.edit_dir / "sfx",
            ),
            (
                "render_motion_card.py",
                (
                    "--edit-dir",
                    self.edit_dir,
                    "--visual-id",
                    "unused-visual",
                    card_spec,
                    "-o",
                    self.edit_dir / "motion" / "x.mp4",
                    "--poster",
                    self.edit_dir / "motion" / "x.png",
                ),
                self.edit_dir / "motion",
            ),
            (
                "audio_polish.py",
                (
                    "preview",
                    "--edit-dir",
                    self.edit_dir,
                    self.source,
                    "--output-dir",
                    self.edit_dir / "ab",
                ),
                self.edit_dir / "ab",
            ),
            (
                "audio_polish.py",
                (
                    "apply",
                    "--edit-dir",
                    self.edit_dir,
                    self.source,
                    "-o",
                    self.edit_dir / "polish" / "x.mov",
                    "--approval",
                    self.edit_dir / "ab_approval.json",
                ),
                self.edit_dir / "polish",
            ),
        ]
        for script, arguments, output_root in cases:
            with self.subTest(script=script, arguments=arguments):
                result = self.run_script(script, *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("asset gate failed", result.stderr)
                self.assertFalse(output_root.exists())

    def test_stale_plan_leaves_each_writer_output_absent(self) -> None:
        self.plan_path.write_text(
            self.plan_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        card_spec = self.edit_dir / "stale-card.json"
        write_json(card_spec, {"kind": "title", "title": "Blocked", "duration_s": 1})
        cases = [
            (
                "generate_sfx.py",
                (
                    "--edit-dir",
                    self.edit_dir,
                    "hit",
                    "-o",
                    self.edit_dir / "stale-sfx" / "x.wav",
                ),
                self.edit_dir / "stale-sfx",
            ),
            (
                "render_motion_card.py",
                (
                    "--edit-dir",
                    self.edit_dir,
                    "--visual-id",
                    "unused-visual",
                    card_spec,
                    "-o",
                    self.edit_dir / "stale-motion" / "x.mp4",
                    "--poster",
                    self.edit_dir / "stale-motion" / "x.png",
                ),
                self.edit_dir / "stale-motion",
            ),
            (
                "audio_polish.py",
                (
                    "preview",
                    "--edit-dir",
                    self.edit_dir,
                    self.source,
                    "--output-dir",
                    self.edit_dir / "stale-ab",
                ),
                self.edit_dir / "stale-ab",
            ),
            (
                "audio_polish.py",
                (
                    "apply",
                    "--edit-dir",
                    self.edit_dir,
                    self.source,
                    "-o",
                    self.edit_dir / "stale-polish" / "x.mov",
                    "--approval",
                    self.edit_dir / "ab_approval.json",
                ),
                self.edit_dir / "stale-polish",
            ),
        ]
        for script, arguments, output_root in cases:
            with self.subTest(script=script, arguments=arguments):
                result = self.run_script(script, *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("semantic plan changed after approval", result.stderr)
                self.assertFalse(output_root.exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_analyze_is_allowed_without_approval_but_stays_under_edit(self) -> None:
        (self.edit_dir / "approval.json").unlink()
        audio_source = self.videos_dir / "analysis-source.wav"
        with wave.open(str(audio_source), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48_000)
            handle.writeframes(b"\0\0" * 48_000)
        output = self.edit_dir / "analysis" / "source.json"
        result = self.run_script(
            "audio_polish.py",
            "analyze",
            "--edit-dir",
            self.edit_dir,
            audio_source,
            "-o",
            output,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(output.is_file())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_audio_ab_approval_round_trip_is_hash_bound(self) -> None:
        source, decision_path, approval_path = self.make_ab_approval()
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        self.assertEqual(decision["source_sha256"], sha256(source))
        self.assertEqual(
            decision["excerpt"], {"start_s": 0.1, "end_s": 0.5, "duration_s": 0.4}
        )
        self.assertEqual(decision["filter"], "highpass=f=70:poles=2,aresample=48000,asetpts=N/SR/TB")
        for key, name in (("A", "A_original.wav"), ("B", "B_processed.wav")):
            self.assertEqual(
                decision["preview_artifacts"][key]["sha256"], sha256(decision_path.parent / name)
            )
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["user_quote"], "I approve processed preview B exactly.")
        self.assertEqual(approval["decision_sha256"], sha256(decision_path))
        self.assertRegex(approval["binding_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(approval["excerpt"], decision["excerpt"])
        self.assertEqual(approval["preview_artifacts"], decision["preview_artifacts"])

        bare_output = self.edit_dir / "bare-boolean" / "polished.mov"
        bare = self.run_script(
            "audio_polish.py",
            "apply",
            "--edit-dir",
            self.edit_dir,
            source,
            "-o",
            bare_output,
            "--approved-ab",
        )
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn("--approval", bare.stderr)
        self.assertFalse(bare_output.parent.exists())

        output = self.edit_dir / "audio" / "polished.mov"
        applied = self.run_script(
            "audio_polish.py",
            "apply",
            "--edit-dir",
            self.edit_dir,
            source,
            "-o",
            output,
            "--approval",
            approval_path,
        )
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertTrue(output.is_file())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_audio_apply_rejects_tampering_before_output_creation(self) -> None:
        source, decision_path, approval_path = self.make_ab_approval()
        processed = decision_path.parent / "B_processed.wav"
        originals = {
            "source": source.read_bytes(),
            "decision": decision_path.read_bytes(),
            "approval": approval_path.read_bytes(),
            "processed": processed.read_bytes(),
        }

        def mutate_source() -> None:
            source.write_bytes(originals["source"] + b"tampered")

        def mutate_bounds() -> None:
            decision = json.loads(originals["decision"])
            decision["excerpt"]["start_s"] = 0.2
            decision["excerpt"]["end_s"] = 0.6
            write_json(decision_path, decision)

        def mutate_preview() -> None:
            processed.write_bytes(originals["processed"] + b"tampered")

        def mutate_status() -> None:
            approval = json.loads(originals["approval"])
            approval["status"] = "pending"
            write_json(approval_path, approval)

        def remove_quote() -> None:
            approval = json.loads(originals["approval"])
            approval["user_quote"] = ""
            write_json(approval_path, approval)

        def replace_quote() -> None:
            approval = json.loads(originals["approval"])
            approval["user_quote"] = "I approve a different preview instead."
            write_json(approval_path, approval)

        cases = [
            ("source", mutate_source, (), "source changed"),
            ("bounds", mutate_bounds, (), "decision changed"),
            ("preview", mutate_preview, (), "preview artifact B changed"),
            ("status", mutate_status, (), "status is not approved"),
            ("quote", remove_quote, (), "no exact user quote"),
            ("valid quote mutation", replace_quote, (), "binding hash does not match"),
            ("filter args", lambda: None, ("--highpass", "90"), "filter arguments differ"),
        ]
        for index, (label, mutate, extra_args, expected_error) in enumerate(cases):
            with self.subTest(label=label):
                source.write_bytes(originals["source"])
                decision_path.write_bytes(originals["decision"])
                approval_path.write_bytes(originals["approval"])
                processed.write_bytes(originals["processed"])
                mutate()
                output = self.edit_dir / f"rejected-{index}" / "polished.mov"
                result = self.run_script(
                    "audio_polish.py",
                    "apply",
                    "--edit-dir",
                    self.edit_dir,
                    source,
                    "-o",
                    output,
                    "--approval",
                    approval_path,
                    *extra_args,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(output.parent.exists())

        source.write_bytes(originals["source"])
        decision_path.write_bytes(originals["decision"])
        approval_path.write_bytes(originals["approval"])
        processed.write_bytes(originals["processed"])
        existing = self.edit_dir / "audio" / "existing-polish.mov"
        existing.write_bytes(b"preserve this existing output")
        rejected_force = self.run_script(
            "audio_polish.py",
            "apply",
            "--edit-dir",
            self.edit_dir,
            source,
            "-o",
            existing,
            "--approval",
            approval_path,
            "--highpass",
            "90",
            "--force",
        )
        self.assertNotEqual(rejected_force.returncode, 0)
        self.assertEqual(existing.read_bytes(), b"preserve this existing output")


if __name__ == "__main__":
    unittest.main()
