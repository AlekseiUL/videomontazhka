from __future__ import annotations

import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import artifact_provenance as provenance  # noqa: E402
import qa_release  # noqa: E402
import render_edl  # noqa: E402


class RendererProvenanceTest(unittest.TestCase):
    def test_creative_decision_and_generators_are_release_bound(self) -> None:
        required = {
            "scripts/creative_tool_registry.py",
            "scripts/creative_tool_router.py",
            "assets/creative-tool-router-map.v1.json",
            "scripts/compile_creative_treatment_plan.py",
            "schemas/creative_treatment_plan.schema.json",
            "scripts/generate_creative_sfx.py",
            "scripts/scaffold_gsap_creative_effect.py",
            "scripts/scaffold_creative_browser_effect.py",
            "scripts/plan_virtual_camera.py",
            "scripts/render_virtual_camera.py",
            "scripts/install_manim_runtime.py",
            "assets/manim-runtime-requirements.v1.txt",
        }
        self.assertTrue(required <= set(provenance.IDENTITY_FILES))
        for name in required:
            self.assertTrue(provenance.IDENTITY_FILES[name].is_file(), name)

    def test_snapshot_recheck_rejects_changed_control_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-snapshot-") as temporary:
            path = Path(temporary) / "edl.json"
            path.write_text('{"version":1}\n', encoding="utf-8")
            snapshot, _ = render_edl.capture_file_snapshot(path, "EDL")
            path.write_text('{"version":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(render_edl.RenderError, "changed after validation"):
                render_edl.assert_file_snapshot(snapshot)

    def test_corrupt_final_manifest_invocation_invalidates_existing_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-release-") as temporary:
            edit = Path(temporary)
            deliverable_id = "youtube_main"
            key = provenance.artifact_key(deliverable_id)
            release = edit / provenance.release_manifest_name(deliverable_id)
            release.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "status": "PASS",
                        "deliverable_id": deliverable_id,
                        "artifact_key": key,
                    }
                ),
                encoding="utf-8",
            )
            manifest = edit / provenance.render_manifest_name(deliverable_id, "final")
            manifest.write_text("{corrupt", encoding="utf-8")
            result = provenance.invalidate_release_state_from_manifest_path(
                manifest, "QA could not validate the final manifest"
            )
            self.assertEqual(result, release.resolve())
            current = json.loads(release.read_text(encoding="utf-8"))
            self.assertEqual(current["status"], "FAIL")
            self.assertIn("QA could not validate", current["errors"][0])

    def test_exact_timing_binds_segment_source_bounds_and_audio_mode_to_edl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-segment-binding-") as temporary:
            clips = Path(temporary)
            segment_path = clips / "segment.mov"
            segment_path.write_bytes(b"synthetic segment bytes")
            edl_range = {
                "source": "talk",
                "start": 0.5,
                "end": 1.2,
                "audio_mode": "source",
            }
            manifest = {
                "segments": [
                    {
                        "index": 0,
                        "source": "other",
                        "source_start_s": 0.6,
                        "source_end_s": 1.3,
                        "audio_mode": "mute",
                        "range_contract": {
                            "source": "other",
                            "start": 0.6,
                            "end": 1.3,
                            "audio_mode": "mute",
                        },
                        "frames": 21,
                        "audio_samples": 33_600,
                        "path": str(segment_path),
                        "sha256": qa_release.sha256(segment_path),
                    }
                ],
                "cut_times_s": [],
            }
            output_info = {
                "width": 640,
                "height": 360,
                "frame_count": 21,
                "audio_logical_samples": 33_600,
            }
            segment_info = {
                "frame_count": 21,
                "audio_logical_samples": 33_600,
                "video_codec": "h264",
                "audio_codec": "pcm_s16le",
                "audio_sample_rate": 48_000,
                "audio_channels": 2,
                "width": 640,
                "height": 360,
                "fps_fraction": "30/1",
            }
            errors: list[str] = []
            with mock.patch.object(qa_release, "probe", return_value=segment_info):
                qa_release.validate_exact_timing(
                    manifest,
                    {"ranges": [edl_range]},
                    clips,
                    output_info,
                    Fraction(30, 1),
                    errors,
                )
            joined = "\n".join(errors)
            self.assertIn("range contract differs", joined)
            self.assertIn("source differs", joined)
            self.assertIn("source_start_s differs", joined)
            self.assertIn("source_end_s differs", joined)
            self.assertIn("audio_mode differs", joined)


if __name__ == "__main__":
    unittest.main()
