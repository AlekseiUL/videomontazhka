from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MOTION_KIT = ROOT / "assets" / "motion-kit"
FONT_ROOT = ROOT / "assets" / "fonts"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class MotionKitScaffoldTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-motion-kit-")
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
                "name": "motion kit fixture",
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
                        "duration_s": 3.0,
                        "audio": None,
                    }
                ],
            },
        )
        packed = self.edit_dir / "takes_packed.md"
        packed.write_text("# Packed transcripts\n\nVisual-only fixture.\n", encoding="utf-8")
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
                        "duration_s": 3.0,
                        "phrases": 0,
                    }
                ],
            },
        )
        self.plan_path = self.edit_dir / "semantic_plan.json"
        self.plan = {
            "version": 1,
            "status": "pending",
            "viewer_promise": "Understand the approved motion graphic clearly.",
            "audience": "Viewers",
            "source_mode": "long_stream",
            "source_truth": [
                {
                    "id": "meaning-1",
                    "meaning": "The source supports the approved visual statement.",
                    "evidence": [
                        {
                            "id": "evidence-1",
                            "source": "source-1",
                            "start": 0.0,
                            "end": 1.0,
                            "modality": "visual",
                            "description": "The approved visual statement is supported.",
                        }
                    ],
                }
            ],
            "narrative": [
                {
                    "id": "section-1",
                    "title": "Agent memory",
                    "purpose": "Explain the approved statement.",
                    "meaning_ids": ["meaning-1"],
                    "payoff": "The statement is clear.",
                    "estimated_duration_s": 2.4,
                }
            ],
            "hooks": [
                {
                    "id": "hook-1",
                    "text": "What is agent memory?",
                    "payoff": "The statement is clear.",
                    "meaning_ids": ["meaning-1"],
                },
                {
                    "id": "hook-2",
                    "text": "See how memory works.",
                    "payoff": "The statement is clear.",
                    "meaning_ids": ["meaning-1"],
                },
            ],
            "recommended_hook_id": "hook-1",
            "ending": {
                "section_id": "section-1",
                "meaning_ids": ["meaning-1"],
                "takeaway": "The statement is clear.",
            },
            "visual_plan": [
                {
                    "id": "visual-motion",
                    "section_id": "section-1",
                    "meaning_ids": ["meaning-1"],
                    "treatment": "A local kinetic keyword overlay.",
                    "purpose": "Explain the approved statement.",
                    "approved_text": "ПАМЯТЬ АГЕНТА — ЭТО СИСТЕМА",
                    "asset_type": "title",
                }
            ],
            "audio_plan": {},
            "deliverables": [
                {
                    "id": "video-1",
                    "platform": "YouTube",
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "target_duration_s": 2.4,
                    "subtitle_mode": "none",
                    "section_ids": ["section-1"],
                    "hook_id": "hook-1",
                    "ending_section_id": "section-1",
                }
            ],
        }
        write_json(self.plan_path, self.plan)
        self._approve()
        self.gsap_root = self.video_dir / "gsap"
        self.gsap = self.gsap_root / "dist" / "gsap.min.js"
        self.gsap.parent.mkdir(parents=True)
        self.gsap.write_text(
            "/* GSAP local fixture bundle; test-only bytes. */\n"
            "window.gsap={timeline:function(){return {seek:function(){},pause:function(){}};}};\n"
            + ("/* gsap offline */\n" * 12),
            encoding="utf-8",
        )
        write_json(
            self.gsap_root / "package.json",
            {
                "name": "gsap",
                "version": "3.14.2",
                "license": "Standard no charge license: https://gsap.com/standard-license.",
            },
        )
        (self.gsap_root / "README.md").write_text("Synthetic GSAP README.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _approve(self) -> None:
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
                "user_quote": "I approve this exact motion visual plan.",
            },
        )

    def _run(
        self, template: str = "kinetic-keyword", *, accept_terms: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = [
                sys.executable,
                str(SCRIPTS / "scaffold_motion_kit.py"),
                "--edit-dir",
                str(self.edit_dir),
                "--visual-id",
                "visual-motion",
                "--template",
                template,
                "--gsap-bundle",
                str(self.gsap),
            ]
        if accept_terms:
            command.append("--accept-gsap-terms")
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )

    @property
    def instance(self) -> Path:
        return (
            self.edit_dir
            / "animations"
            / "hyperframes"
            / "instances"
            / "visual-motion"
        )

    def test_all_templates_are_audited_offline_sources(self) -> None:
        schema = json.loads((MOTION_KIT / "motion-kit.schema.v1.json").read_text(encoding="utf-8"))
        expected = set(schema["properties"]["template"]["enum"])
        actual = {path.parent.name for path in (MOTION_KIT / "templates").glob("*/template.json")}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 8)
        for template_id in sorted(actual):
            with self.subTest(template=template_id):
                directory = MOTION_KIT / "templates" / template_id
                metadata = json.loads((directory / "template.json").read_text(encoding="utf-8"))
                html = (directory / "index.html").read_text(encoding="utf-8")
                self.assertTrue(metadata["audited"])
                self.assertEqual(metadata["id"], template_id)
                self.assertIn("./vendor/gsap.min.js", html)
                self.assertNotRegex(html, r"(?i)(?:src|href)=[\"'](?:https?:)?//")
                self.assertNotIn("Remotion", html)
                for banned in ("#0000FF", "#00FFFF", "#7F00FF", "#3B82F6"):
                    self.assertNotIn(banned.lower(), html.lower())
        diagram = (MOTION_KIT / "templates" / "diagram-focus" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("var(--sprut-primary)", diagram)
        self.assertIn("var(--sprut-accent)", diagram)

    def test_success_copies_offline_runtime_fonts_licenses_and_hashes_every_file(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.instance.is_dir())
        config = json.loads((self.instance / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["content"]["approved_text"], "ПАМЯТЬ АГЕНТА — ЭТО СИСТЕМА")
        self.assertFalse(config["runtime"]["network_allowed"])
        self.assertEqual(config["runtime"]["paid_apis"], [])
        terms = config["runtime"]["gsap_terms"]
        self.assertTrue(terms["terms_explicitly_accepted"])
        self.assertEqual(terms["version"], "3.14.2")
        self.assertEqual(terms["license_url"], "https://gsap.com/standard-license")
        self.assertEqual(terms["package_json_sha256"], sha256(self.gsap_root / "package.json"))
        self.assertEqual(sha256(self.instance / "vendor" / "gsap.min.js"), sha256(self.gsap))
        self.assertEqual(sha256(self.instance / "vendor" / "gsap-package.json"), sha256(self.gsap_root / "package.json"))
        self.assertEqual(sha256(self.instance / "vendor" / "GSAP_README.md"), sha256(self.gsap_root / "README.md"))

        font_manifest = json.loads((FONT_ROOT / "manifest.json").read_text(encoding="utf-8"))
        for family in font_manifest["families"]:
            copied_font = self.instance / "fonts" / family["file"]
            copied_license = self.instance / "fonts" / family["license_file"]
            self.assertTrue(copied_font.is_file())
            self.assertTrue(copied_license.is_file())
            self.assertEqual(sha256(copied_font), family["sha256"])
            self.assertEqual(sha256(copied_license), family["license_sha256"])
            role = {
                "expressive_display": "display",
                "readable_body": "body",
                "technical_labels_and_data": "mono",
            }[family["role"]]
            self.assertEqual(config["fonts"][role]["license_sha256"], family["license_sha256"])

        manifest = json.loads((self.instance / "source-manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["runtime"]["offline"])
        self.assertFalse(manifest["runtime"]["network_allowed"])
        self.assertFalse(manifest["runtime"]["remotion"])
        self.assertEqual(manifest["runtime"]["paid_apis"], [])
        self.assertTrue(manifest["runtime"]["gsap_terms"]["terms_explicitly_accepted"])
        records = {item["path"]: item for item in manifest["files"]}
        expected_files = {
            str(path.relative_to(self.instance))
            for path in self.instance.rglob("*")
            if path.is_file() and path.name != "source-manifest.json"
        }
        self.assertEqual(set(records), expected_files)
        for relative, record in records.items():
            self.assertEqual(record["sha256"], sha256(self.instance / relative))
        self.assertFalse(any(path.suffix.lower() in {".mp4", ".mov", ".webm"} for path in self.instance.rglob("*")))

        before = sha256(self.instance / "source-manifest.json")
        repeated = self._run()
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("already exists", repeated.stderr)
        self.assertEqual(sha256(self.instance / "source-manifest.json"), before)

    def test_refuses_gsap_copy_without_explicit_terms_acceptance(self) -> None:
        result = self._run(accept_terms=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--accept-gsap-terms", result.stderr)
        self.assertFalse((self.edit_dir / "animations").exists())

    def test_failed_asset_gate_writes_no_instance_directory(self) -> None:
        approval = json.loads((self.edit_dir / "approval.json").read_text(encoding="utf-8"))
        approval["proposal_sha256"] = "0" * 64
        write_json(self.edit_dir / "approval.json", approval)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("asset gate failed", result.stderr)
        self.assertFalse(self.instance.exists())
        self.assertFalse((self.edit_dir / "animations").exists())

    def test_template_rejects_wrong_asset_type_and_text_contract(self) -> None:
        self.plan["visual_plan"][0]["asset_type"] = "chapter"
        write_json(self.plan_path, self.plan)
        self._approve()
        wrong_type = self._run("kinetic-keyword")
        self.assertNotEqual(wrong_type.returncode, 0)
        self.assertIn("requires approved asset_type", wrong_type.stderr)
        self.assertFalse(self.instance.exists())

        self.plan["visual_plan"][0]["approved_text"] = None
        write_json(self.plan_path, self.plan)
        self._approve()
        cover = self._run("cover-wipe-transition")
        self.assertEqual(cover.returncode, 0, cover.stdout + cover.stderr)
        config = json.loads((self.instance / "config.json").read_text(encoding="utf-8"))
        self.assertIsNone(config["content"]["approved_text"])
        self.assertEqual(config["content"]["lines"], [])


if __name__ == "__main__":
    unittest.main()
