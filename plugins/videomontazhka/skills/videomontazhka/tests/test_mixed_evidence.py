from __future__ import annotations

import hashlib
import json
import os
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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class EvidenceFixture:
    def __init__(self, base: Path, *, source_mode: str, include_audio: bool, include_visual: bool):
        self.root = base / "videos"
        self.edit = self.root / "edit"
        self.transcripts = self.edit / "transcripts"
        self.metadata = self.transcripts / ".metadata"
        self.metadata.mkdir(parents=True)
        self.source_mode = source_mode
        self.source_paths: dict[str, Path] = {}
        self.source_entries: list[dict[str, Any]] = []

        if include_audio:
            self._add_source("talk", audio=True, duration_s=1200.0 if source_mode == "long_stream" else 5.0)
        if include_visual:
            self._add_source("broll", audio=False, duration_s=5.0)

        project = json.loads((ROOT / "assets" / "default-project.json").read_text(encoding="utf-8"))
        project.update({
            "name": "evidence-regression",
            "source_mode": source_mode,
            "source_manifest": "source_manifest.json",
        })
        write_json(self.edit / "project.json", project)
        write_json(self.edit / "source_manifest.json", {
            "version": 1,
            "root": "..",
            "sources": self.source_entries,
        })
        self._write_audio_transcripts()
        packed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "pack_transcripts_safe.py"),
                "--edit-dir",
                str(self.edit),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if packed.returncode != 0:
            raise AssertionError(f"packer failed:\n{packed.stdout}\n{packed.stderr}")
        self.plan, self.edl = self._contracts(include_audio=include_audio, include_visual=include_visual)
        self.write_contracts()

    def _add_source(self, source_id: str, *, audio: bool, duration_s: float) -> None:
        path = self.root / f"{source_id}.mov"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((f"immutable-{source_id}-source\n").encode("utf-8"))
        stat = path.stat()
        self.source_paths[source_id] = path
        self.source_entries.append({
            "id": source_id,
            "path": path.name,
            "sha256": sha256(path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "duration_s": duration_s,
            "role": "primary" if audio else "b_roll",
            "video": {"codec": "h264", "width": 640, "height": 360, "fps": 30.0},
            "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2} if audio else None,
        })

    def _write_audio_transcripts(self) -> None:
        words = [
            {"type": "word", "text": "Hello", "start": 0.5, "end": 0.8},
            {"type": "word", "text": "world", "start": 0.9, "end": 1.2},
            {"type": "word", "text": "again", "start": 1.4, "end": 1.7},
        ]
        for source in self.source_entries:
            if source["audio"] is None:
                continue
            source_id = source["id"]
            transcript_path = self.transcripts / f"{source_id}.json"
            write_json(transcript_path, {"words": words})
            write_json(self.metadata / f"{source_id}.json", {
                "version": 1,
                "identity": {
                    "source": str(self.source_paths[source_id].resolve()),
                    "source_sha256": source["sha256"],
                    "source_size": source["size_bytes"],
                    "source_mtime_ns": source["mtime_ns"],
                    "model_id": "scribe_v1",
                    "timestamps_granularity": "word",
                },
                "transcript": transcript_path.name,
                "words": len(words),
            })

    def _contracts(self, *, include_audio: bool, include_visual: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        meanings: list[dict[str, Any]] = []
        ranges: list[dict[str, Any]] = []
        layouts: list[dict[str, Any]] = []
        narrative_meanings: list[str] = []
        total_duration = 0.0

        if include_audio:
            meanings.append({
                "id": "spoken_truth",
                "meaning": "The speaker audibly says hello world.",
                "evidence": [{
                    "id": "speech_hello",
                    "source": "talk",
                    "start": 0.5,
                    "end": 1.2,
                    "modality": "speech",
                    "quote": "Hello world",
                }],
            })
            ranges.append({
                "source": "talk",
                "start": 0.5,
                "end": 1.2,
                "section_id": "main",
                "meaning_ids": ["spoken_truth"],
                "evidence_ids": ["speech_hello"],
                "audio_mode": "source",
                "quote": "Hello world",
                "reason": "Retains the exact spoken statement.",
                "transition_after": "hard_cut",
            })
            layouts.append({
                "source": "talk",
                "start": 0.5,
                "end": 1.2,
                "source_class": "full_frame_presenter",
                "output_shape": "full_frame",
                "composition": "preserve_source",
                "reason": "Preserve the source frame.",
            })
            narrative_meanings.append("spoken_truth")
            total_duration += 0.7

        if include_visual:
            meanings.append({
                "id": "visible_truth",
                "meaning": "An orange chart visibly rises on screen.",
                "evidence": [{
                    "id": "visual_chart",
                    "source": "broll",
                    "start": 0.5,
                    "end": 1.5,
                    "modality": "visual",
                    "description": "Orange chart rises on screen",
                }],
            })
            ranges.append({
                "source": "broll",
                "start": 0.5,
                "end": 1.5,
                "section_id": "main",
                "meaning_ids": ["visible_truth"],
                "evidence_ids": ["visual_chart"],
                "audio_mode": "mute",
                "description": "Orange chart rises on screen",
                "reason": "Retains the approved visible demonstration.",
                "transition_after": "hard_cut",
            })
            layouts.append({
                "source": "broll",
                "start": 0.5,
                "end": 1.5,
                "source_class": "screen_only",
                "output_shape": "hidden",
                "composition": "preserve_source",
                "reason": "Preserve the full B-roll frame.",
            })
            narrative_meanings.append("visible_truth")
            total_duration += 1.0

        first_meaning = narrative_meanings[0]
        final_meaning = narrative_meanings[-1]
        plan = {
            "version": 1,
            "status": "approved",
            "viewer_promise": "See one source-grounded statement without invented evidence.",
            "audience": "Video editors",
            "source_mode": self.source_mode,
            "source_truth": meanings,
            "narrative": [{
                "id": "main",
                "title": "Main evidence",
                "purpose": "Present only source-grounded material.",
                "meaning_ids": narrative_meanings,
                "payoff": "The retained material is verifiable.",
                "estimated_duration_s": total_duration,
            }],
            "hooks": [
                {
                    "id": "hook_primary",
                    "text": "Show the verified moment.",
                    "payoff": "The source proves it.",
                    "meaning_ids": [first_meaning],
                },
                {
                    "id": "hook_alternate",
                    "text": "Start with source truth.",
                    "payoff": "No evidence is invented.",
                    "meaning_ids": [first_meaning],
                },
            ],
            "recommended_hook_id": "hook_primary",
            "ending": {
                "section_id": "main",
                "meaning_ids": [final_meaning],
                "takeaway": "Every retained claim stays bound to its source.",
            },
            "visual_plan": [],
            "audio_plan": {
                "cleanup": "Preserve intelligibility with local dialogue cleanup only.",
                "target_lufs": -14.0,
                "true_peak_dbtp": -1.0,
            },
            "deliverables": [{
                "id": "main_video",
                "platform": "YouTube",
                "format": "custom",
                "width": 640,
                "height": 360,
                "fps": 30,
                "target_duration_s": total_duration,
                "minimum_duration_s": total_duration * 0.5,
                "subtitle_mode": "none",
                "section_ids": ["main"],
                "hook_id": "hook_primary",
                "ending_section_id": "main",
            }],
        }
        edl = {
            "version": 1,
            "approval_plan_sha256": "0" * 64,
            "deliverable_id": "main_video",
            "hook_id": "hook_primary",
            "sources": {key: str(path.resolve()) for key, path in self.source_paths.items()},
            "output": {"width": 640, "height": 360, "fps": 30},
            "ranges": ranges,
            "layout_plan": layouts,
            "subtitle_mode": "none",
            "audio": {"filters": []},
        }
        return plan, edl

    def write_contracts(self) -> None:
        write_json(self.edit / "semantic_plan.json", self.plan)
        plan_digest = sha256(self.edit / "semantic_plan.json")
        write_json(self.edit / "approval.json", {
            "version": 1,
            "proposal_file": "semantic_plan.json",
            "status": "approved",
            "proposal_sha256": plan_digest,
            "approved_scope": ["semantic_structure", "editing_strategy", "visual_strategy"],
            "user_quote": "I approve this semantic plan.",
        })
        write_creative_approval(self.edit)
        self.edl["approval_plan_sha256"] = plan_digest
        visual_by_id = {
            item["id"]: item
            for item in self.plan.get("visual_plan", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for overlay in self.edl.get("overlays", []):
            asset = (self.edit / str(overlay["file"])).resolve()
            asset.parent.mkdir(parents=True, exist_ok=True)
            if not asset.exists():
                asset.write_bytes(b"synthetic approved visual asset\n")
            command = [
                sys.executable,
                str(SCRIPTS / "record_visual_asset.py"),
                "--edit-dir",
                str(self.edit),
                "--asset",
                str(asset),
                "--visual-id",
                str(overlay["visual_id"]),
                "--force",
            ]
            visual = visual_by_id.get(str(overlay["visual_id"]))
            if isinstance(visual, dict) and visual.get("approved_text") is not None:
                command.extend(
                    ["--declared-visible-text", str(visual["approved_text"])]
                )
            recorded = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
            if recorded.returncode:
                raise AssertionError(
                    "visual provenance fixture failed:\n"
                    f"{recorded.stdout}\n{recorded.stderr}"
                )
        write_json(self.edit / "edl.json", self.edl)

    def gate(self) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "validate_gate.py"),
                "--edit-dir",
                str(self.edit),
                "--phase",
                "edl",
                "--edl",
                str(self.edit / "edl.json"),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"gate did not emit JSON:\n{result.stdout}\n{result.stderr}") from exc
        return result, report


class MixedEvidenceTests(unittest.TestCase):
    def fixture(self, **kwargs: Any) -> tuple[tempfile.TemporaryDirectory[str], EvidenceFixture]:
        temporary = tempfile.TemporaryDirectory(prefix="sprut-evidence-test-")
        fixture = EvidenceFixture(Path(temporary.name), **kwargs)
        return temporary, fixture

    def assert_gate_passes(self, fixture: EvidenceFixture) -> None:
        result, report = fixture.gate()
        self.assertEqual(result.returncode, 0, report["errors"])
        self.assertEqual(report["status"], "PASS", report["errors"])
        self.assertEqual(report["errors"], [])

    def assert_gate_fails_with(self, fixture: EvidenceFixture, needle: str) -> None:
        result, report = fixture.gate()
        self.assertEqual(result.returncode, 1, report)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn(needle, "\n".join(report["errors"]))

    def replace_talk_timeline(
        self, fixture: EvidenceFixture, words: list[dict[str, Any]]
    ) -> None:
        transcript_path = fixture.transcripts / "talk.json"
        write_json(transcript_path, {"words": words})
        metadata_path = fixture.metadata / "talk.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["words"] = len(words)
        write_json(metadata_path, metadata)
        packed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "pack_transcripts_safe.py"),
                "--edit-dir",
                str(fixture.edit),
                "--force",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(packed.returncode, 0, packed.stderr)

    @staticmethod
    def configure_internal_pause(fixture: EvidenceFixture) -> None:
        fixture.plan["source_truth"][0]["evidence"][0]["end"] = 2.0
        fixture.plan["narrative"][0]["estimated_duration_s"] = 1.5
        fixture.plan["deliverables"][0]["target_duration_s"] = 1.5
        fixture.edl["ranges"][0]["end"] = 2.0
        fixture.edl["layout_plan"][0]["end"] = 2.0

    @staticmethod
    def add_approved_overlay(fixture: EvidenceFixture) -> None:
        fixture.plan["visual_plan"] = [{
            "id": "visual_title",
            "section_id": "main",
            "meaning_ids": ["spoken_truth"],
            "treatment": "A restrained title card reinforces the verified statement.",
            "purpose": "Clarify the approved spoken claim.",
            "approved_text": "Hello world",
            "asset_type": "title",
        }]
        fixture.edl["overlays"] = [{
            "visual_id": "visual_title",
            "file": "animations/title.mp4",
            "provenance": "animations/title.mp4.provenance.json",
            "purpose": "Clarify the approved spoken claim.",
            "section_id": "main",
            "meaning_ids": ["spoken_truth"],
            "semantic_text": "Hello world",
            "start_in_output": 0.0,
            "duration": 0.5,
        }]

    @staticmethod
    def split_speech_into_two_ranges(fixture: EvidenceFixture) -> None:
        fixture.plan["source_truth"][0]["evidence"] = [
            {
                "id": "speech_hello",
                "source": "talk",
                "start": 0.5,
                "end": 0.8,
                "modality": "speech",
                "quote": "Hello",
            },
            {
                "id": "speech_world",
                "source": "talk",
                "start": 0.9,
                "end": 1.2,
                "modality": "speech",
                "quote": "world",
            },
        ]
        fixture.plan["narrative"][0]["estimated_duration_s"] = 0.6
        fixture.plan["deliverables"][0]["target_duration_s"] = 0.6
        fixture.edl["ranges"] = [
            {
                "source": "talk",
                "start": 0.5,
                "end": 0.8,
                "section_id": "main",
                "meaning_ids": ["spoken_truth"],
                "evidence_ids": ["speech_hello"],
                "audio_mode": "source",
                "quote": "Hello",
                "reason": "Retains the first approved word.",
                "transition_after": "hard_cut",
            },
            {
                "source": "talk",
                "start": 0.9,
                "end": 1.2,
                "section_id": "main",
                "meaning_ids": ["spoken_truth"],
                "evidence_ids": ["speech_world"],
                "audio_mode": "source",
                "quote": "world",
                "reason": "Retains the second approved word.",
                "transition_after": "hard_cut",
            },
        ]
        fixture.edl["layout_plan"] = [
            {
                "source": "talk",
                "start": start,
                "end": end,
                "source_class": "full_frame_presenter",
                "output_shape": "full_frame",
                "composition": "preserve_source",
                "reason": "Preserve the source frame.",
            }
            for start, end in ((0.5, 0.8), (0.9, 1.2))
        ]

    def test_long_audio_speech_evidence_passes(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.assert_gate_passes(fixture)

    def test_mixed_audio_speech_and_silent_visual_evidence_pass(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=True, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        self.assert_gate_passes(fixture)

        packed = json.loads((fixture.edit / "takes_packed_manifest.json").read_text(encoding="utf-8"))
        visual = next(item for item in packed["sources"] if item["source"] == "broll")
        self.assertEqual(
            set(visual),
            {"source", "source_sha256", "visual_only", "duration_s", "phrases"},
        )
        self.assertIs(visual["visual_only"], True)
        self.assertFalse((fixture.transcripts / "broll.json").exists())
        self.assertFalse((fixture.metadata / "broll.json").exists())
        markdown = (fixture.edit / "takes_packed.md").read_text(encoding="utf-8")
        self.assertIn("Visual-only source", markdown)
        packed_bytes = (fixture.edit / "takes_packed_manifest.json").read_bytes()
        markdown_bytes = (fixture.edit / "takes_packed.md").read_bytes()
        repeated = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "pack_transcripts_safe.py"),
                "--edit-dir",
                str(fixture.edit),
                "--force",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((fixture.edit / "takes_packed_manifest.json").read_bytes(), packed_bytes)
        self.assertEqual((fixture.edit / "takes_packed.md").read_bytes(), markdown_bytes)

    def test_batch_transcription_reports_silent_source_skip_without_api_key(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=False, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        environment = dict(os.environ)
        environment.pop("ELEVENLABS_API_KEY", None)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "transcribe_batch_safe.py"), str(fixture.root)],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIP: broll", result.stdout)
        self.assertIn("0 transcribed, 1 visual-only source(s) skipped", result.stdout)

    def test_speech_evidence_on_silent_source_fails(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=False, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        evidence = fixture.plan["source_truth"][0]["evidence"][0]
        evidence["modality"] = "speech"
        evidence["quote"] = evidence.pop("description")
        retained = fixture.edl["ranges"][0]
        retained["quote"] = retained.pop("description")
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "speech evidence")

    def test_visual_evidence_without_description_fails(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=False, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        fixture.plan["source_truth"][0]["evidence"][0].pop("description")
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "description")

    def test_forged_modality_fails(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.plan["source_truth"][0]["evidence"][0]["modality"] = "synthetic"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "modality")

    def test_forged_transcript_hash_fails(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        packed_path = fixture.edit / "takes_packed_manifest.json"
        packed = json.loads(packed_path.read_text(encoding="utf-8"))
        packed["sources"][0]["transcript_sha256"] = "0" * 64
        write_json(packed_path, packed)
        self.assert_gate_fails_with(fixture, "transcript_sha256 is stale")

    def test_forged_source_hash_fails(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=False, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        manifest_path = fixture.edit / "source_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sources"][0]["sha256"] = "0" * 64
        write_json(manifest_path, manifest)
        self.assert_gate_fails_with(fixture, "source content hash changed")

    def test_speech_range_cannot_append_fabricated_words(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["ranges"][0]["quote"] = "Hello world fabricated words"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "full quote must exactly equal every transcript word retained")

    def test_speech_range_quote_cannot_hide_retained_words(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.plan["source_truth"][0]["evidence"][0]["quote"] = "world"
        fixture.edl["ranges"][0]["quote"] = "world"
        fixture.write_contracts()
        self.assert_gate_fails_with(
            fixture, "full quote must exactly equal every transcript word retained"
        )

    def test_vocalized_filler_cannot_survive_a_speech_range(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        transcript_path = fixture.transcripts / "talk.json"
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        transcript["words"].insert(
            1, {"type": "word", "text": "um", "start": 0.82, "end": 0.88}
        )
        write_json(transcript_path, transcript)
        metadata_path = fixture.metadata / "talk.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["words"] = 4
        write_json(metadata_path, metadata)
        packed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "pack_transcripts_safe.py"),
                "--edit-dir",
                str(fixture.edit),
                "--force",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(packed.returncode, 0, packed.stderr)
        fixture.plan["source_truth"][0]["evidence"][0]["quote"] = "Hello um world"
        fixture.edl["ranges"][0]["quote"] = "Hello um world"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "retains vocalized filler")

    def test_filler_audio_event_cannot_survive_a_speech_range(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.replace_talk_timeline(fixture, [
            {"type": "word", "text": "Hello", "start": 0.5, "end": 0.8},
            {"type": "audio_event", "text": "эээ", "start": 0.82, "end": 0.88},
            {"type": "word", "text": "world", "start": 0.9, "end": 1.2},
            {"type": "word", "text": "again", "start": 1.4, "end": 1.7},
        ])
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "filler-like audio_event")

    def test_long_internal_spacing_requires_intentional_pause_reason(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.replace_talk_timeline(fixture, [
            {"type": "word", "text": "Hello", "start": 0.5, "end": 0.8},
            {"type": "spacing", "text": " ", "start": 0.8, "end": 1.7},
            {"type": "word", "text": "world", "start": 1.7, "end": 2.0},
            {"type": "word", "text": "again", "start": 2.2, "end": 2.5},
        ])
        self.configure_internal_pause(fixture)
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "unapproved internal silence")

    def test_approved_intentional_internal_pause_passes(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.replace_talk_timeline(fixture, [
            {"type": "word", "text": "Hello", "start": 0.5, "end": 0.8},
            {"type": "spacing", "text": " ", "start": 0.8, "end": 1.7},
            {"type": "word", "text": "world", "start": 1.7, "end": 2.0},
            {"type": "word", "text": "again", "start": 2.2, "end": 2.5},
        ])
        self.configure_internal_pause(fixture)
        fixture.edl["ranges"][0]["intentional_pause_reason"] = (
            "Preserve the approved dramatic pause before the payoff."
        )
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    def test_long_boundary_silence_is_blocked(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.plan["source_truth"][0]["evidence"][0]["start"] = 0.0
        fixture.plan["narrative"][0]["estimated_duration_s"] = 1.2
        fixture.plan["deliverables"][0]["target_duration_s"] = 1.2
        fixture.edl["ranges"][0]["start"] = 0.0
        fixture.edl["layout_plan"][0]["start"] = 0.0
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "unapproved boundary silence")

    def test_speech_range_requires_source_audio_mode(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["ranges"][0]["audio_mode"] = "mute"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "speech evidence and must set audio_mode='source'")

    def test_no_audio_visual_range_requires_mute_audio_mode(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=False, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["ranges"][0]["audio_mode"] = "source"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "no-audio source")

    def test_audio_source_without_speech_evidence_must_be_muted(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        evidence = fixture.plan["source_truth"][0]["evidence"][0]
        evidence["modality"] = "visual"
        evidence["description"] = "Presenter remains visible on screen"
        evidence.pop("quote")
        retained = fixture.edl["ranges"][0]
        retained["description"] = "Presenter remains visible on screen"
        retained.pop("quote")
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "no speech evidence and must set audio_mode='mute'")

    def test_subtitle_text_exactly_matches_speech_ranges_for_burned_and_sidecar(self) -> None:
        for subtitle_mode in ("burned", "sidecar"):
            with self.subTest(subtitle_mode=subtitle_mode):
                temporary, fixture = self.fixture(
                    source_mode="long_stream", include_audio=True, include_visual=False
                )
                self.addCleanup(temporary.cleanup)
                subtitle = fixture.edit / f"captions-{subtitle_mode}.srt"
                subtitle.write_text(
                    "1\n00:00:00,000 --> 00:00:00,700\nHello world\n",
                    encoding="utf-8",
                )
                fixture.plan["deliverables"][0]["subtitle_mode"] = subtitle_mode
                fixture.edl["subtitle_mode"] = subtitle_mode
                fixture.edl["subtitles"] = subtitle.name
                fixture.write_contracts()
                self.assert_gate_passes(fixture)

    def test_subtitle_cue_cannot_run_over_muted_b_roll(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=True, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        subtitle = fixture.edit / "captions-over-muted-broll.srt"
        subtitle.write_text(
            "1\n00:00:00,400 --> 00:00:01,200\nHello world\n",
            encoding="utf-8",
        )
        fixture.plan["deliverables"][0]["subtitle_mode"] = "sidecar"
        fixture.edl["subtitle_mode"] = "sidecar"
        fixture.edl["subtitles"] = subtitle.name
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "overlaps muted/non-speech output range[1]")

    def test_subtitle_case_punctuation_and_multilingual_splitting_stays_bound(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.split_speech_into_two_ranges(fixture)
        self.replace_talk_timeline(fixture, [
            {"type": "word", "text": "Привет", "start": 0.5, "end": 0.8},
            {"type": "word", "text": "МИР", "start": 0.9, "end": 1.2},
            {"type": "word", "text": "again", "start": 1.4, "end": 1.7},
        ])
        for evidence, text in zip(
            fixture.plan["source_truth"][0]["evidence"], ("Привет", "МИР")
        ):
            evidence["quote"] = text
        for retained, text in zip(fixture.edl["ranges"], ("Привет", "МИР")):
            retained["quote"] = text
        subtitle = fixture.edit / "captions-multilingual.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,300\nПРИВЕТ!\n\n"
            "2\n00:00:00,300 --> 00:00:00,600\nмир…\n",
            encoding="utf-8",
        )
        fixture.plan["deliverables"][0]["subtitle_mode"] = "burned"
        fixture.edl["subtitle_mode"] = "burned"
        fixture.edl["subtitles"] = subtitle.name
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    def test_subtitle_cues_must_be_ordered_and_non_overlapping(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.split_speech_into_two_ranges(fixture)
        subtitle = fixture.edit / "captions-overlap.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,400\nHello\n\n"
            "2\n00:00:00,300 --> 00:00:00,600\nworld\n",
            encoding="utf-8",
        )
        fixture.plan["deliverables"][0]["subtitle_mode"] = "sidecar"
        fixture.edl["subtitle_mode"] = "sidecar"
        fixture.edl["subtitles"] = subtitle.name
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "overlaps the previous subtitle cue")

    def test_subtitle_cue_must_stay_inside_quantized_programme(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        subtitle = fixture.edit / "captions-outside-programme.srt"
        subtitle.write_text(
            "1\n00:00:00,100 --> 00:00:00,800\nHello world\n",
            encoding="utf-8",
        )
        fixture.plan["deliverables"][0]["subtitle_mode"] = "sidecar"
        fixture.edl["subtitle_mode"] = "sidecar"
        fixture.edl["subtitles"] = subtitle.name
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "outside the frame-quantized programme")

    def test_editorial_cta_cannot_be_smuggled_into_subtitles(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        subtitle = fixture.edit / "captions.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,700\nHello world\n\n"
            "2\n00:00:00,700 --> 00:00:01,500\nFollow me\n",
            encoding="utf-8",
        )
        fixture.plan["deliverables"][0]["subtitle_mode"] = "burned"
        fixture.edl["subtitle_mode"] = "burned"
        fixture.edl["subtitles"] = subtitle.name
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "subtitle visible cue text must exactly equal")

    def test_subtitle_text_must_follow_speech_range_output_order(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.split_speech_into_two_ranges(fixture)
        subtitle = fixture.edit / "captions-reversed.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,300\nworld\n\n"
            "2\n00:00:00,300 --> 00:00:00,600\nHello\n",
            encoding="utf-8",
        )
        fixture.plan["deliverables"][0]["subtitle_mode"] = "sidecar"
        fixture.edl["subtitle_mode"] = "sidecar"
        fixture.edl["subtitles"] = subtitle.name
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "concatenated speech-range quotes in output order")

    def test_overlay_metadata_exactly_matches_approved_visual_plan(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.add_approved_overlay(fixture)
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    def test_overlay_requires_current_visual_provenance(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.add_approved_overlay(fixture)
        fixture.write_contracts()
        provenance = fixture.edit / "animations" / "title.mp4.provenance.json"
        provenance.unlink()
        self.assert_gate_fails_with(fixture, "visual provenance is invalid")

    def test_overlay_rejects_tampered_visual_provenance(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.add_approved_overlay(fixture)
        fixture.write_contracts()
        provenance = fixture.edit / "animations" / "title.mp4.provenance.json"
        value = json.loads(provenance.read_text(encoding="utf-8"))
        value["review_requirement"] = "not-reviewed"
        write_json(provenance, value)
        self.assert_gate_fails_with(fixture, "visual provenance is invalid")

    def test_overlay_supported_anchors_use_renderer_timing(self) -> None:
        anchors = (
            {"start_in_output": 0.0},
            {"start": 0.0},
            {"start_at_range_index": 0, "offset_s": 0.1},
            {"align_to_end": True, "offset_s": 0.0},
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                temporary, fixture = self.fixture(
                    source_mode="long_stream", include_audio=True, include_visual=False
                )
                self.addCleanup(temporary.cleanup)
                self.add_approved_overlay(fixture)
                fixture.edl["overlays"][0].pop("start_in_output")
                fixture.edl["overlays"][0].update(anchor)
                fixture.edl["overlays"][0]["duration"] = 0.2
                fixture.write_contracts()
                self.assert_gate_passes(fixture)

        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.split_speech_into_two_ranges(fixture)
        self.add_approved_overlay(fixture)
        fixture.edl["overlays"][0].pop("start_in_output")
        fixture.edl["overlays"][0].update({
            "start_after_range_index": 0,
            "offset_s": 0.0,
            "duration": 0.2,
        })
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    def test_overlay_timing_must_stay_in_its_approved_section(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=True, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        fixture.plan["narrative"] = [
            {
                "id": "opening",
                "title": "Opening",
                "purpose": "Present the approved spoken statement.",
                "meaning_ids": ["spoken_truth"],
                "payoff": "The statement is heard.",
                "estimated_duration_s": 0.7,
            },
            {
                "id": "ending",
                "title": "Ending",
                "purpose": "Show the approved visible result.",
                "meaning_ids": ["visible_truth"],
                "payoff": "The result is visible.",
                "estimated_duration_s": 1.0,
            },
        ]
        fixture.plan["ending"] = {
            "section_id": "ending",
            "meaning_ids": ["visible_truth"],
            "takeaway": "The visible result closes the sequence.",
        }
        fixture.plan["deliverables"][0]["section_ids"] = ["opening", "ending"]
        fixture.plan["deliverables"][0]["ending_section_id"] = "ending"
        fixture.edl["ranges"][0]["section_id"] = "opening"
        fixture.edl["ranges"][1]["section_id"] = "ending"
        fixture.plan["visual_plan"] = [{
            "id": "ending_title",
            "section_id": "ending",
            "meaning_ids": ["visible_truth"],
            "treatment": "Label the approved result when it appears.",
            "purpose": "Clarify the visible ending.",
            "approved_text": "Visible result",
            "asset_type": "title",
        }]
        fixture.edl["overlays"] = [{
            "visual_id": "ending_title",
            "file": "animations/ending-title.mp4",
            "provenance": "animations/ending-title.mp4.provenance.json",
            "purpose": "Clarify the visible ending.",
            "section_id": "ending",
            "meaning_ids": ["visible_truth"],
            "semantic_text": "Visible result",
            "start_at_range_index": 0,
            "duration": 0.5,
        }]
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "is outside approved section 'ending'")

    def test_overlay_semantic_text_cannot_drift_from_approved_visual_plan(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.add_approved_overlay(fixture)
        fixture.edl["overlays"][0]["semantic_text"] = "Follow me"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "semantic_text must exactly match")

    def test_approved_no_text_overlay_uses_null_semantic_text(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        self.add_approved_overlay(fixture)
        fixture.plan["visual_plan"][0]["approved_text"] = None
        fixture.edl["overlays"][0]["semantic_text"] = None
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    def test_overlay_section_must_be_inside_selected_deliverable_scope(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.plan["narrative"].append({
            "id": "appendix",
            "title": "Appendix",
            "purpose": "Hold material outside this deliverable.",
            "meaning_ids": ["spoken_truth"],
            "payoff": "The optional appendix remains source-grounded.",
            "estimated_duration_s": 0.7,
        })
        fixture.plan["ending"]["section_id"] = "appendix"
        self.add_approved_overlay(fixture)
        fixture.plan["visual_plan"][0]["section_id"] = "appendix"
        fixture.edl["overlays"][0]["section_id"] = "appendix"
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "section_id is outside the selected deliverable scope")

    def test_visual_only_entry_rejects_forged_transcript_fields(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="mixed", include_audio=False, include_visual=True
        )
        self.addCleanup(temporary.cleanup)
        packed_path = fixture.edit / "takes_packed_manifest.json"
        packed = json.loads(packed_path.read_text(encoding="utf-8"))
        packed["sources"][0]["transcript_sha256"] = "0" * 64
        write_json(packed_path, packed)
        self.assert_gate_fails_with(fixture, "non-canonical keys")

    def test_duration_changing_audio_filter_fails_at_edl_gate(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["audio"]["filters"] = ["adelay=1000|1000"]
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "duration/PTS-preserving cleanup filters")

    def test_allowlisted_audio_cleanup_chain_passes(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["audio"]["filters"] = ["highpass=f=70,afftdn=nf=-28"]
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    @staticmethod
    def _screen_layout(*, presenter_box: dict[str, Any], important: list[float]) -> dict[str, Any]:
        return {
            "source": "talk",
            "start": 0.5,
            "end": 1.2,
            "source_class": "rectangular_with_context",
            "output_shape": "rectangle",
            "composition": "presenter_with_screen",
            "screen_roi": [0.0, 0.0, 1.0, 1.0],
            "important_screen_roi": important,
            "presenter_roi": [0.0, 0.0, 0.25, 0.5],
            "screen_box": {
                "space": "pixels",
                "x": 0,
                "y": 0,
                "width": 640,
                "height": 360,
            },
            "presenter_box": presenter_box,
            "reason": "Keep the approved screen detail visible beside the presenter.",
        }

    def test_important_screen_roi_clear_and_inside_safe_area_passes(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["layout_plan"] = [self._screen_layout(
            presenter_box={
                "space": "pixels",
                "x": 460,
                "y": 210,
                "width": 140,
                "height": 100,
            },
            important=[0.1, 0.1, 0.4, 0.4],
        )]
        fixture.write_contracts()
        self.assert_gate_passes(fixture)

    def test_presenter_cannot_cover_important_screen_roi(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["layout_plan"] = [self._screen_layout(
            presenter_box={
                "space": "pixels",
                "x": 100,
                "y": 60,
                "width": 180,
                "height": 100,
            },
            important=[0.1, 0.1, 0.4, 0.4],
        )]
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "presenter_box covers mapped important_screen_roi")

    def test_important_screen_roi_cannot_leave_platform_safe_area(self) -> None:
        temporary, fixture = self.fixture(
            source_mode="long_stream", include_audio=True, include_visual=False
        )
        self.addCleanup(temporary.cleanup)
        fixture.edl["layout_plan"] = [self._screen_layout(
            presenter_box={
                "space": "pixels",
                "x": 460,
                "y": 210,
                "width": 140,
                "height": 100,
            },
            important=[0.0, 0.0, 0.2, 0.2],
        )]
        fixture.write_contracts()
        self.assert_gate_fails_with(fixture, "mapped important_screen_roi leaves the platform safe area")


if __name__ == "__main__":
    unittest.main()
