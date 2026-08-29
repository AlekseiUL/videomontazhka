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
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from artifact_provenance import (  # noqa: E402
    artifact_key,
    preview_approval_name,
    release_manifest_name,
    render_manifest_name,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class EndToEndReleaseTest(unittest.TestCase):
    def run_script(self, name: str, *arguments: object) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / name), *(str(value) for value in arguments)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"{name} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def run_script_failure(
        self, name: str, *arguments: object
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / name), *(str(value) for value in arguments)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(
            result.returncode,
            0,
            f"{name} unexpectedly succeeded\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def test_preview_approval_final_release_chain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-e2e-") as temporary:
            videos = Path(temporary) / "videos"
            videos.mkdir()
            source = videos / "source.mp4"
            generated = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=30:duration=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-colorspace",
                    "bt709",
                    "-c:a",
                    "aac",
                    str(source),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            self.run_script("init_project.py", videos, "--source-mode", "multi_take")
            edit = videos / "edit"
            manifest = json.loads((edit / "source_manifest.json").read_text(encoding="utf-8"))
            source_record = manifest["sources"][0]
            source_id = source_record["id"]

            transcript = edit / "transcripts" / f"{source_id}.json"
            words = [
                {"type": "word", "text": "hello", "start": 0.40, "end": 0.70},
                {"type": "word", "text": "world", "start": 0.90, "end": 1.20},
                {"type": "word", "text": "again", "start": 1.40, "end": 1.70},
            ]
            write_json(transcript, {"words": words})
            write_json(
                edit / "transcripts" / ".metadata" / f"{source_id}.json",
                {
                    "version": 1,
                    "identity": {
                        "source": str(source.resolve()),
                        "source_sha256": source_record["sha256"],
                        "source_size": source_record["size_bytes"],
                        "source_mtime_ns": source_record["mtime_ns"],
                        "model_id": "scribe_v1",
                        "timestamps_granularity": "word",
                    },
                    "transcript": transcript.name,
                    "words": len(words),
                },
            )
            self.run_script("pack_transcripts_safe.py", "--edit-dir", edit)

            deliverable_id = "youtube_smoke"
            plan = {
                "version": 1,
                "status": "pending",
                "viewer_promise": "See one exact, source-bound statement through the full release chain.",
                "audience": "Video editors",
                "source_mode": "multi_take",
                "source_truth": [
                    {
                        "id": "meaning_smoke",
                        "meaning": "The retained source-backed phrase is hello world again.",
                        "evidence": [
                            {
                                "id": "evidence_smoke",
                                "source": source_id,
                                "start": 0.25,
                                "end": 1.95,
                                "modality": "speech",
                                "quote": "hello world again",
                            }
                        ],
                    }
                ],
                "narrative": [
                    {
                        "id": "section_smoke",
                        "title": "Verified phrase",
                        "purpose": "Retain the exact approved phrase.",
                        "meaning_ids": ["meaning_smoke"],
                        "payoff": "The release remains traceable to the source transcript.",
                        "estimated_duration_s": 1.7,
                    }
                ],
                "hooks": [
                    {
                        "id": "hook_smoke",
                        "text": "Watch one source-bound release.",
                        "payoff": "Every stage is verified.",
                        "meaning_ids": ["meaning_smoke"],
                    },
                    {
                        "id": "hook_alternate",
                        "text": "Can a release stay source-bound?",
                        "payoff": "This one does.",
                        "meaning_ids": ["meaning_smoke"],
                    },
                ],
                "recommended_hook_id": "hook_smoke",
                "ending": {
                    "section_id": "section_smoke",
                    "meaning_ids": ["meaning_smoke"],
                    "takeaway": "The exact release chain passes.",
                },
                "visual_plan": [
                    {
                        "id": "visual_smoke",
                        "section_id": "section_smoke",
                        "meaning_ids": ["meaning_smoke"],
                        "treatment": "Show one restrained source-bound title card.",
                        "purpose": "Reinforce the exact approved release claim.",
                        "approved_text": "VERIFIED RELEASE",
                        "asset_type": "title",
                    }
                ],
                "audio_plan": {
                    "cleanup": "Apply restrained local dialogue cleanup.",
                    "target_lufs": -14.0,
                    "true_peak_dbtp": -1.0,
                },
                "deliverables": [
                    {
                        "id": deliverable_id,
                        "platform": "YouTube",
                        "format": "custom",
                        "width": 640,
                        "height": 360,
                        "fps": 30,
                        "target_duration_s": 1.7,
                        "minimum_duration_s": 1.0,
                        "subtitle_mode": "sidecar",
                        "section_ids": ["section_smoke"],
                        "hook_id": "hook_smoke",
                        "ending_section_id": "section_smoke",
                    }
                ],
            }
            plan_path = edit / "semantic_plan.json"
            write_json(plan_path, plan)
            self.run_script(
                "record_approval.py",
                "--plan",
                plan_path,
                "--quote",
                "I approve this exact smoke-test plan.",
            )
            write_creative_approval(edit)
            card_spec = edit / "animations" / "smoke-card.json"
            write_json(
                card_spec,
                {
                    "kind": "title",
                    "title": "VERIFIED RELEASE",
                    "width": 640,
                    "height": 360,
                    "fps": 30,
                    "duration_s": 1.0,
                },
            )
            card = edit / "animations" / "smoke-card.mp4"
            self.run_script(
                "render_motion_card.py",
                "--edit-dir",
                edit,
                "--visual-id",
                "visual_smoke",
                card_spec,
                "-o",
                card,
            )
            card_provenance = card.with_name(f"{card.name}.provenance.json")
            self.assertTrue(card_provenance.is_file())
            subtitles = edit / "captions.srt"
            subtitles.write_text(
                "1\n00:00:00,000 --> 00:00:01,700\nhello world again\n",
                encoding="utf-8",
            )

            edl = {
                "version": 1,
                "approval_plan_sha256": sha256(plan_path),
                "deliverable_id": deliverable_id,
                "hook_id": "hook_smoke",
                "sources": {source_id: str(source.resolve())},
                "output": {"width": 640, "height": 360, "fps": 30},
                "ranges": [
                    {
                        "source": source_id,
                        "start": 0.25,
                        "end": 1.95,
                        "section_id": "section_smoke",
                        "meaning_ids": ["meaning_smoke"],
                        "evidence_ids": ["evidence_smoke"],
                        "audio_mode": "source",
                        "quote": "hello world again",
                        "reason": "Retain the exact approved source-bound phrase.",
                        "transition_after": "hard_cut",
                    }
                ],
                "layout_plan": [
                    {
                        "source": source_id,
                        "start": 0.25,
                        "end": 1.95,
                        "source_class": "full_frame_presenter",
                        "output_shape": "full_frame",
                        "composition": "preserve_source",
                        "reason": "Preserve the complete source frame.",
                    }
                ],
                "overlays": [
                    {
                        "visual_id": "visual_smoke",
                        "file": str(card),
                        "provenance": str(card_provenance),
                        "purpose": "Reinforce the exact approved release claim.",
                        "section_id": "section_smoke",
                        "meaning_ids": ["meaning_smoke"],
                        "semantic_text": "VERIFIED RELEASE",
                        "start_in_output": 0.0,
                        "duration": 0.6,
                        "full_frame": True,
                    }
                ],
                "subtitle_mode": "sidecar",
                "subtitles": str(subtitles),
                "audio": {"filters": ["highpass=f=70"]},
            }
            edl_path = edit / "edl_youtube_smoke.json"
            write_json(edl_path, edl)

            key = artifact_key(deliverable_id)
            preview = edit / "youtube-smoke-preview.mp4"
            final = edit / "youtube-smoke-final.mp4"
            self.run_script("render_edl.py", edl_path, "-o", preview, "--preview")
            preview_manifest = edit / render_manifest_name(deliverable_id, "preview")
            self.assertTrue(preview_manifest.is_file())
            self.run_script("qa_release.py", "--manifest", preview_manifest)
            preview_qa = edit / "verify" / key / "preview" / "release_metrics.json"
            self.assertEqual(
                json.loads(preview_qa.read_text(encoding="utf-8"))["status"], "PASS"
            )
            preview_manifest_data = json.loads(
                preview_manifest.read_text(encoding="utf-8")
            )
            self.assertEqual(
                preview_manifest_data["visual_assets"][0]["provenance"]["sha256"],
                sha256(card_provenance),
            )
            original_card_provenance = card_provenance.read_bytes()
            card_provenance.write_bytes(original_card_provenance + b"tampered")
            rejected_visual_approval = self.run_script_failure(
                "record_preview_approval.py",
                "--preview",
                preview,
                "--quote",
                "I approve this exact smoke-test preview.",
            )
            self.assertIn("provenance", rejected_visual_approval.stderr)
            card_provenance.write_bytes(original_card_provenance)
            preview_sidecar = preview.with_suffix(".srt")
            original_preview_sidecar = preview_sidecar.read_bytes()
            preview_sidecar.write_bytes(original_preview_sidecar + b"tampered")
            rejected_approval = self.run_script_failure(
                "record_preview_approval.py",
                "--preview",
                preview,
                "--quote",
                "I approve this exact smoke-test preview.",
            )
            self.assertIn("preview sidecar hash", rejected_approval.stderr)
            self.assertFalse((edit / preview_approval_name(deliverable_id)).exists())
            preview_sidecar.write_bytes(original_preview_sidecar)
            self.run_script(
                "record_preview_approval.py",
                "--preview",
                preview,
                "--quote",
                "I approve this exact smoke-test preview.",
            )
            preview_approval_path = edit / preview_approval_name(deliverable_id)
            self.assertTrue(preview_approval_path.is_file())
            preview_approval = json.loads(preview_approval_path.read_text(encoding="utf-8"))
            self.assertEqual(
                preview_approval["preview_sidecar"],
                {
                    "path": preview_sidecar.relative_to(edit).as_posix(),
                    "sha256": sha256(preview_sidecar),
                },
            )

            card_provenance.write_bytes(original_card_provenance + b"tampered")
            rejected_visual_final = self.run_script_failure(
                "render_edl.py", edl_path, "-o", final, "--final"
            )
            self.assertIn(
                "provenance", rejected_visual_final.stdout + rejected_visual_final.stderr
            )
            self.assertFalse(final.exists())
            card_provenance.write_bytes(original_card_provenance)

            preview_sidecar.write_bytes(original_preview_sidecar + b"tampered")
            rejected_final = self.run_script_failure(
                "render_edl.py", edl_path, "-o", final, "--final"
            )
            self.assertIn("preview sidecar", rejected_final.stdout + rejected_final.stderr)
            self.assertFalse(final.exists())
            preview_sidecar.write_bytes(original_preview_sidecar)

            self.run_script("render_edl.py", edl_path, "-o", final, "--final")
            final_manifest = edit / render_manifest_name(deliverable_id, "final")
            self.assertTrue(final_manifest.is_file())
            final_manifest_data = json.loads(final_manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                final_manifest_data["final_authorization"]["preview_sidecar"],
                preview_approval["preview_sidecar"],
            )
            self.run_script("qa_release.py", "--manifest", final_manifest)
            release = edit / release_manifest_name(deliverable_id)
            release_data = json.loads(release.read_text(encoding="utf-8"))
            self.assertEqual(release_data["status"], "PASS")
            self.assertEqual(release_data["deliverable_id"], deliverable_id)
            self.assertEqual(release_data["artifact_key"], key)
            self.assertEqual(release_data["output"]["sha256"], sha256(final))

            card_provenance.write_bytes(original_card_provenance + b"tampered")
            rejected_visual_release = self.run_script_failure(
                "qa_release.py", "--manifest", final_manifest
            )
            self.assertIn(
                "provenance",
                rejected_visual_release.stdout + rejected_visual_release.stderr,
            )
            failed_visual_release = json.loads(release.read_text(encoding="utf-8"))
            self.assertEqual(failed_visual_release["status"], "FAIL")
            card_provenance.write_bytes(original_card_provenance)
            self.run_script("qa_release.py", "--manifest", final_manifest)

            preview_sidecar.write_bytes(original_preview_sidecar + b"tampered")
            rejected_release = self.run_script_failure(
                "qa_release.py", "--manifest", final_manifest
            )
            self.assertIn(
                "preview sidecar",
                rejected_release.stdout + rejected_release.stderr,
            )
            failed_release = json.loads(release.read_text(encoding="utf-8"))
            self.assertEqual(failed_release["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
