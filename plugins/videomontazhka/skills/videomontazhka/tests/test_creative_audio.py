from __future__ import annotations

import hashlib
import importlib.util
import json
import math
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
HAS_LIBROSA = importlib.util.find_spec("librosa") is not None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CreativeAudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-creative-audio-")
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
                "name": "creative audio fixture",
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
                        "duration_s": 4.0,
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
                        "duration_s": 4.0,
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
                "viewer_promise": "Understand one approved visible result.",
                "audience": "Viewers",
                "source_mode": "long_stream",
                "source_truth": [
                    {
                        "id": "meaning-1",
                        "meaning": "The source contains an approved visible result.",
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
                        "purpose": "Show the approved result.",
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
                    "cleanup": "Keep dialogue primary; use only approved restrained accents.",
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
                "approved_scope": ["semantic_structure", "editing_strategy", "visual_strategy"],
                "user_quote": "I approve this exact creative audio fixture.",
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

    def write_sfx_spec(self, path: Path, **updates: object) -> Path:
        value: dict[str, object] = {
            "version": 1,
            "preset": "semantic_hit",
            "gain_db": -16.0,
            "seed": 6800,
            "intensity": 0.7,
            "brightness": 0.55,
            "stereo_width": 0.25,
            "purpose": "Land the approved visible result.",
            "section_id": "section-1",
        }
        value.update(updates)
        write_json(path, value)
        return path

    def test_discovery_contracts_are_machine_readable_and_local(self) -> None:
        listed = self.run_script("generate_creative_sfx.py", "--list-presets")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        presets = json.loads(listed.stdout)
        preset_ids = [item["id"] for item in presets["presets"]]
        self.assertEqual(len(preset_ids), 9)
        self.assertEqual(len(preset_ids), len(set(preset_ids)))
        self_tested = self.run_script("generate_creative_sfx.py", "--self-test")
        self.assertEqual(self_tested.returncode, 0, self_tested.stdout + self_tested.stderr)
        self.assertEqual(json.loads(self_tested.stdout)["presets_tested"], len(preset_ids))

        described = self.run_script("generate_creative_sfx.py", "--describe-json")
        self.assertEqual(described.returncode, 0, described.stdout + described.stderr)
        sfx = json.loads(described.stdout)
        self.assertFalse(sfx["availability"]["network_required"])
        self.assertFalse(sfx["availability"]["paid_api_required"])
        self.assertFalse(sfx["availability"]["external_audio_assets_required"])
        self.assertTrue(sfx["production_contract"]["semantic_approval_required"])

        rhythm_result = self.run_script("analyze_rhythm.py", "--describe-json")
        self.assertEqual(rhythm_result.returncode, 0, rhythm_result.stdout + rhythm_result.stderr)
        rhythm = json.loads(rhythm_result.stdout)
        self.assertEqual(rhythm["tool_id"], "sprut.audio.rhythm_map.v1")
        self.assertFalse(rhythm["availability"]["network_required"])
        self.assertTrue(rhythm["production_contract"]["semantic_approval_required"])
        self.assertIn("meaning and intelligibility first", rhythm["routing"]["precedence"])

    def test_creative_sfx_is_deterministic_and_writes_bound_provenance(self) -> None:
        spec = self.write_sfx_spec(self.edit_dir / "audio" / "specs" / "hit.json")
        first = self.edit_dir / "audio" / "sfx" / "hit-a.wav"
        second = self.edit_dir / "audio" / "sfx" / "hit-b.wav"
        for output in (first, second):
            result = self.run_script(
                "generate_creative_sfx.py",
                "--edit-dir",
                self.edit_dir,
                "--spec",
                spec,
                "-o",
                output,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with wave.open(str(first), "rb") as handle:
            self.assertEqual(handle.getframerate(), 48_000)
            self.assertEqual(handle.getnchannels(), 2)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getnframes(), round(0.34 * 48_000))
        sidecar = first.with_name(f"{first.name}.provenance.json")
        provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(provenance["output"]["sha256"], sha256(first))
        self.assertEqual(provenance["spec"]["sha256"], sha256(spec))
        self.assertEqual(provenance["semantic_contract"]["plan"]["sha256"], sha256(self.plan_path))
        self.assertEqual(provenance["third_party_audio_assets"], [])
        self.assertFalse(provenance["network_used"])
        self.assertFalse(provenance["paid_api_used"])
        self.assertEqual(provenance["edl_audio_overlay_template"]["section_id"], "section-1")

    def test_creative_sfx_can_adapt_an_end_aligned_rhythm_anchor(self) -> None:
        spec = self.write_sfx_spec(
            self.edit_dir / "audio" / "specs" / "swell.json",
            preset="reverse_swell",
            gain_db=-22.0,
            rhythm_anchor_s=2.0,
            anchor_alignment="end_at_anchor",
        )
        output = self.edit_dir / "audio" / "sfx" / "swell.wav"
        result = self.run_script(
            "generate_creative_sfx.py",
            "--edit-dir",
            self.edit_dir,
            "--spec",
            spec,
            "-o",
            output,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        provenance = json.loads(
            output.with_name(f"{output.name}.provenance.json").read_text(encoding="utf-8")
        )
        template = provenance["edl_audio_overlay_template"]
        self.assertAlmostEqual(template["start_in_output"], 2.0 - template["duration"], places=6)

    def test_creative_sfx_rejects_invalid_spec_without_output(self) -> None:
        spec = self.write_sfx_spec(
            self.edit_dir / "invalid.json", unreviewed_external_asset="mystery.wav"
        )
        output = self.edit_dir / "invalid-output" / "hit.wav"
        result = self.run_script(
            "generate_creative_sfx.py",
            "--edit-dir",
            self.edit_dir,
            "--spec",
            spec,
            "-o",
            output,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match schema", result.stderr)
        self.assertFalse(output.parent.exists())

    def test_both_production_tools_fail_closed_before_creating_outputs(self) -> None:
        spec = self.write_sfx_spec(self.edit_dir / "approved-spec.json")
        music = self.edit_dir / "music.wav"
        self.write_pulse_audio(music)
        (self.edit_dir / "approval.json").unlink()
        cases = [
            (
                "generate_creative_sfx.py",
                ("--edit-dir", self.edit_dir, "--spec", spec, "-o", self.edit_dir / "blocked-sfx" / "x.wav"),
                self.edit_dir / "blocked-sfx",
            ),
            (
                "analyze_rhythm.py",
                ("--edit-dir", self.edit_dir, "--input", music, "-o", self.edit_dir / "blocked-rhythm" / "x.json"),
                self.edit_dir / "blocked-rhythm",
            ),
        ]
        for script, arguments, output_root in cases:
            with self.subTest(script=script):
                result = self.run_script(script, *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("asset gate failed", result.stderr)
                self.assertFalse(output_root.exists())

    def write_pulse_audio(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 48_000
        duration_s = 4.0
        frames = bytearray(b"\0\0" * round(sample_rate * duration_s))
        for pulse_time in (0.25, 0.75, 1.25, 1.75, 2.25, 2.75, 3.25, 3.75):
            start = round(pulse_time * sample_rate)
            length = round(0.018 * sample_rate)
            for offset in range(length):
                envelope = math.sin(math.pi * offset / max(1, length - 1)) ** 2
                sample = round(24_000 * envelope * math.sin(2 * math.pi * 1000 * offset / sample_rate))
                struct.pack_into("<h", frames, (start + offset) * 2, sample)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(frames)

    @unittest.skipUnless(HAS_LIBROSA, "Python runtime with librosa is required")
    def test_rhythm_map_is_deterministic_schema_checked_and_project_scoped(self) -> None:
        music = self.edit_dir / "audio" / "music.wav"
        self.write_pulse_audio(music)
        first = self.edit_dir / "audio" / "rhythm" / "map-a.json"
        second = self.edit_dir / "audio" / "rhythm" / "map-b.json"
        for output in (first, second):
            result = self.run_script(
                "analyze_rhythm.py",
                "--edit-dir",
                self.edit_dir,
                "--input",
                music,
                "-o",
                output,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        rhythm = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(rhythm["source"]["scope"], "edit_asset")
        self.assertEqual(rhythm["source"]["sha256"], sha256(music))
        self.assertEqual(rhythm["semantic_contract"]["plan"]["sha256"], sha256(self.plan_path))
        self.assertGreaterEqual(rhythm["statistics"]["onset_count"], 6)
        self.assertGreaterEqual(rhythm["statistics"]["beat_count"], 5)
        self.assertTrue(
            all(item["status"] == "candidate_only_requires_semantic_router" for item in rhythm["suggested_accents"])
        )
        self.assertIn("No downbeat", rhythm["limitations"][1])

    @unittest.skipUnless(HAS_LIBROSA, "Python runtime with librosa is required")
    def test_rhythm_map_rejects_unscoped_external_input(self) -> None:
        external = self.videos_dir / "unregistered.wav"
        self.write_pulse_audio(external)
        output = self.edit_dir / "rhythm-rejected" / "map.json"
        result = self.run_script(
            "analyze_rhythm.py",
            "--edit-dir",
            self.edit_dir,
            "--input",
            external,
            "-o",
            output,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source_manifest item or a local asset under edit", result.stderr)
        self.assertFalse(output.parent.exists())


if __name__ == "__main__":
    unittest.main()
