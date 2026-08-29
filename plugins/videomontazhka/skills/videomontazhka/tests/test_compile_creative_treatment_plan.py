from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compile_creative_treatment_plan as compiler  # noqa: E402
from schema_check import Validator  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class CreativeTreatmentCompilerTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-treatment-compiler-")
        self.videos_dir = Path(self.temporary.name)
        self.edit_dir = self.videos_dir / "edit"
        self.edit_dir.mkdir()
        self.source = self.videos_dir / "visual-source.bin"
        self.source.write_bytes(b"immutable visual source\n")
        source_stat = self.source.stat()

        write_json(
            self.edit_dir / "project.json",
            {
                "version": 1,
                "name": "creative treatment compiler fixture",
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
                        "size_bytes": source_stat.st_size,
                        "mtime_ns": source_stat.st_mtime_ns,
                        "duration_s": 4.0,
                        "audio": None,
                    }
                ],
            },
        )
        packed = self.edit_dir / "takes_packed.md"
        packed.write_text(
            "# Packed source transcripts\n\nVisual-only fixture.\n",
            encoding="utf-8",
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
                        "source_sha256": sha256(self.source),
                        "visual_only": True,
                        "duration_s": 4.0,
                        "phrases": 0,
                    }
                ],
            },
        )

        # visual-b deliberately precedes visual-a in visual_plan.  The compiler
        # must still order by narrative section and retain each original index.
        self.plan: dict[str, Any] = {
            "version": 1,
            "status": "pending",
            "viewer_promise": "Understand two source-backed ideas in their approved order.",
            "audience": "Video editors",
            "source_mode": "long_stream",
            "source_truth": [
                {
                    "id": "meaning-a",
                    "meaning": "The first approved visual explains idea A.",
                    "evidence": [
                        {
                            "id": "evidence-a",
                            "source": "source-1",
                            "start": 0.0,
                            "end": 1.0,
                            "modality": "visual",
                            "description": "source-backed idea A",
                        }
                    ],
                },
                {
                    "id": "meaning-b",
                    "meaning": "The second approved visual explains idea B.",
                    "evidence": [
                        {
                            "id": "evidence-b",
                            "source": "source-1",
                            "start": 1.0,
                            "end": 2.0,
                            "modality": "visual",
                            "description": "source-backed idea B",
                        }
                    ],
                },
            ],
            "narrative": [
                {
                    "id": "section-a",
                    "title": "Idea A",
                    "purpose": "Explain the first idea.",
                    "meaning_ids": ["meaning-a"],
                    "payoff": "Idea A is clear.",
                    "estimated_duration_s": 1.0,
                },
                {
                    "id": "section-b",
                    "title": "Idea B",
                    "purpose": "Explain the second idea.",
                    "meaning_ids": ["meaning-b"],
                    "payoff": "Idea B is clear.",
                    "estimated_duration_s": 1.0,
                },
            ],
            "hooks": [
                {
                    "id": "hook-a",
                    "text": "See the first approved idea.",
                    "payoff": "Idea A becomes clear.",
                    "meaning_ids": ["meaning-a"],
                },
                {
                    "id": "hook-b",
                    "text": "Can two ideas stay traceable?",
                    "payoff": "Both ideas remain source-backed.",
                    "meaning_ids": ["meaning-a"],
                },
            ],
            "recommended_hook_id": "hook-a",
            "ending": {
                "section_id": "section-b",
                "meaning_ids": ["meaning-b"],
                "takeaway": "Both approved ideas are now clear.",
            },
            "visual_plan": [
                {
                    "id": "visual-b",
                    "section_id": "section-b",
                    "meaning_ids": ["meaning-b"],
                    "asset_type": "diagram",
                    "purpose": "Clarify idea B.",
                    "treatment": "Keep the approved diagram restrained.",
                    "approved_text": None,
                },
                {
                    "id": "visual-a",
                    "section_id": "section-a",
                    "meaning_ids": ["meaning-a"],
                    "asset_type": "title",
                    "purpose": "Clarify idea A.",
                    "treatment": "Keep the approved title restrained.",
                    "approved_text": "IDEA A",
                },
            ],
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
                    "target_duration_s": 2.0,
                    "subtitle_mode": "none",
                    "section_ids": ["section-a", "section-b"],
                    "hook_id": "hook-a",
                    "ending_section_id": "section-b",
                }
            ],
        }
        self.plan_path = self.edit_dir / "semantic_plan.json"
        write_json(self.plan_path, self.plan)
        self.approval_path = self.edit_dir / "approval.json"
        write_json(
            self.approval_path,
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
        self.controls = compiler.load_controls()
        self.decision_dir = self.edit_dir / "creative"
        self.decision_dir.mkdir()
        self.decision_paths: dict[str, Path] = {}
        for visual in compiler.visual_contracts(self.plan):
            decision = compiler.none_decision_fixture(
                visual,
                sha256(self.plan_path),
                sha256(self.approval_path),
                self.controls,
            )
            path = self.decision_dir / f"{visual.visual_id}.decision.json"
            write_json(path, decision)
            self.decision_paths[visual.visual_id] = path

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def output(self) -> Path:
        return self.edit_dir / compiler.OUTPUT_NAME

    def run_compiler(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "compile_creative_treatment_plan.py"),
                *(str(value) for value in arguments),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def production_arguments(self, *decision_ids: str) -> list[object]:
        arguments: list[object] = ["--edit-dir", self.edit_dir]
        for visual_id in decision_ids:
            arguments.extend(["--decision", self.decision_paths[visual_id]])
        return arguments

    def assert_failed_without_output(
        self,
        result: subprocess.CompletedProcess[str],
        expected: str,
    ) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(expected, result.stdout + result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.edit_dir.glob(f".{compiler.OUTPUT_NAME}.*.part")), [])

    def test_describe_and_self_test_are_project_free_machine_json(self) -> None:
        described = self.run_compiler("--describe-json")
        self.assertEqual(described.returncode, 0, described.stdout + described.stderr)
        description = json.loads(described.stdout)
        self.assertFalse(description["project_required"])
        self.assertEqual(description["files_written"], 0)
        self.assertEqual(description["network_calls_made"], 0)
        self.assertEqual(
            description["production_contract"]["output"],
            "edit/creative_treatment_plan.json",
        )

        tested = self.run_compiler("--self-test")
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
        report = json.loads(tested.stdout)
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["project_required"])
        self.assertEqual(report["files_written"], 0)
        self.assertEqual(report["network_calls_made"], 0)

    def test_exact_coverage_current_hashes_and_deterministic_order(self) -> None:
        before = {
            path.relative_to(self.edit_dir).as_posix()
            for path in self.edit_dir.rglob("*")
        }
        result = self.run_compiler(
            *self.production_arguments("visual-b", "visual-a")
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(Path(report["output"]).resolve(), self.output.resolve())
        after = {
            path.relative_to(self.edit_dir).as_posix()
            for path in self.edit_dir.rglob("*")
        }
        self.assertEqual(after - before, {compiler.OUTPUT_NAME})

        compiled = json.loads(self.output.read_text(encoding="utf-8"))
        schema = json.loads(compiler.TREATMENT_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(Validator(schema).validate(compiled), [])
        self.assertEqual(
            [entry["visual_id"] for entry in compiled["entries"]],
            ["visual-a", "visual-b"],
        )
        self.assertEqual(
            [entry["visual_plan_index"] for entry in compiled["entries"]],
            [1, 0],
        )
        self.assertEqual(
            [entry["timeline_index"] for entry in compiled["entries"]],
            [0, 1],
        )
        self.assertEqual(
            compiled["summary"],
            {
                "visual_plan_entries": 2,
                "effect_decisions": 0,
                "none_decisions": 2,
                "primary_visuals": 0,
                "supporting_audio_effects": 0,
            },
        )
        self.assertEqual(compiled["approval"]["semantic_plan"]["sha256"], sha256(self.plan_path))
        self.assertEqual(compiled["approval"]["semantic_approval"]["sha256"], sha256(self.approval_path))
        for name, snapshot in self.controls.snapshots.items():
            self.assertEqual(compiled["controls"][name]["sha256"], snapshot.sha256)
        for entry in compiled["entries"]:
            decision_path = self.edit_dir / entry["decision_file"]["path"]
            self.assertEqual(entry["decision_file"]["sha256"], sha256(decision_path))
            self.assertEqual(entry["decision"], "none")
            self.assertTrue(entry["none_reason"])

    def test_missing_approval_fails_before_any_write(self) -> None:
        self.approval_path.unlink()
        before = {
            path.relative_to(self.edit_dir).as_posix()
            for path in self.edit_dir.rglob("*")
        }
        result = self.run_compiler(
            *self.production_arguments("visual-a", "visual-b")
        )
        self.assert_failed_without_output(result, "asset gate failed")
        after = {
            path.relative_to(self.edit_dir).as_posix()
            for path in self.edit_dir.rglob("*")
        }
        self.assertEqual(after, before)

    def test_missing_or_unexpected_visual_decision_fails_closed(self) -> None:
        missing = self.run_compiler(*self.production_arguments("visual-a"))
        self.assert_failed_without_output(missing, "does not exactly cover")

        unexpected_value = json.loads(
            self.decision_paths["visual-b"].read_text(encoding="utf-8")
        )
        unexpected_value["provenance"]["visual_id"] = "visual-unapproved"
        unexpected_path = self.decision_dir / "visual-unapproved.decision.json"
        write_json(unexpected_path, unexpected_value)
        self.decision_paths["visual-unapproved"] = unexpected_path
        unexpected = self.run_compiler(
            *self.production_arguments("visual-a", "visual-unapproved")
        )
        self.assert_failed_without_output(unexpected, "unexpected=['visual-unapproved']")

    def test_duplicate_visual_id_fails_closed(self) -> None:
        duplicate = self.decision_dir / "visual-a-copy.decision.json"
        duplicate.write_bytes(self.decision_paths["visual-a"].read_bytes())
        result = self.run_compiler(
            *self.production_arguments("visual-a", "visual-b"),
            "--decision",
            duplicate,
        )
        self.assert_failed_without_output(result, "duplicate creative decision")

    def test_stale_router_hash_and_plan_tamper_fail_without_output(self) -> None:
        stale_value = json.loads(
            self.decision_paths["visual-a"].read_text(encoding="utf-8")
        )
        stale_value["provenance"]["router_sha256"] = "0" * 64
        write_json(self.decision_paths["visual-a"], stale_value)
        stale = self.run_compiler(
            *self.production_arguments("visual-a", "visual-b")
        )
        self.assert_failed_without_output(stale, "provenance.router_sha256")

        # Restore the decision and then alter the approved plan without updating
        # approval.json.  The shared semantic asset gate must stop the compiler.
        visual_a = next(
            item
            for item in compiler.visual_contracts(self.plan)
            if item.visual_id == "visual-a"
        )
        write_json(
            self.decision_paths["visual-a"],
            compiler.none_decision_fixture(
                visual_a,
                sha256(self.plan_path),
                sha256(self.approval_path),
                self.controls,
            ),
        )
        tampered_plan = copy.deepcopy(self.plan)
        tampered_plan["visual_plan"][1]["approved_text"] = "TAMPERED TEXT"
        write_json(self.plan_path, tampered_plan)
        tampered = self.run_compiler(
            *self.production_arguments("visual-a", "visual-b")
        )
        self.assert_failed_without_output(tampered, "asset gate failed")

    def test_every_decision_provenance_hash_must_be_current(self) -> None:
        path = self.decision_paths["visual-a"]
        pristine = json.loads(path.read_text(encoding="utf-8"))
        for field in (
            "semantic_plan_sha256",
            "approval_sha256",
            "tool_map_sha256",
            "input_schema_sha256",
            "output_schema_sha256",
            "router_sha256",
        ):
            with self.subTest(field=field):
                stale = copy.deepcopy(pristine)
                stale["provenance"][field] = "0" * 64
                write_json(path, stale)
                result = self.run_compiler(
                    *self.production_arguments("visual-a", "visual-b")
                )
                self.assert_failed_without_output(result, f"provenance.{field}")
        write_json(path, pristine)

    def test_outside_edit_decision_is_rejected(self) -> None:
        outside = self.videos_dir / "outside.decision.json"
        outside.write_bytes(self.decision_paths["visual-a"].read_bytes())
        result = self.run_compiler(
            "--edit-dir",
            self.edit_dir,
            "--decision",
            outside,
            "--decision",
            self.decision_paths["visual-b"],
        )
        self.assert_failed_without_output(result, "must be under the canonical edit directory")

    def test_atomic_output_is_never_overwritten(self) -> None:
        arguments = self.production_arguments("visual-a", "visual-b")
        first = self.run_compiler(*arguments)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original = self.output.read_bytes()
        original_sha = sha256(self.output)

        second = self.run_compiler(*arguments)
        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("not be overwritten", second.stdout + second.stderr)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(sha256(self.output), original_sha)
        self.assertEqual(list(self.edit_dir.glob(f".{compiler.OUTPUT_NAME}.*.part")), [])


if __name__ == "__main__":
    unittest.main()
