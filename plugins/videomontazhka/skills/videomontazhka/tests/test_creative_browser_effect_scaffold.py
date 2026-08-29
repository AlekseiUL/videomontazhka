from __future__ import annotations

import hashlib
import json
import re
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
ASSETS = ROOT / "assets" / "creative-browser-effects"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from runtime_paths import APP_HOME, CREATIVE_BROWSER_RUNTIME  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
    }


class CreativeBrowserEffectScaffoldTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sprut-creative-effect-")
        self.video_dir = Path(self.temporary.name)
        self.edit_dir = self.video_dir / "edit"
        self.edit_dir.mkdir()
        source = self.video_dir / "source.bin"
        source.write_bytes(b"immutable creative visual source\n")
        stat = source.stat()
        write_json(
            self.edit_dir / "project.json",
            {
                "version": 1,
                "name": "creative browser fixture",
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
                        "duration_s": 3.2,
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
                        "duration_s": 3.2,
                        "phrases": 0,
                    }
                ],
            },
        )
        self.plan_path = self.edit_dir / "semantic_plan.json"
        self.plan = {
            "version": 1,
            "status": "pending",
            "viewer_promise": "Understand the approved relationship clearly.",
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
                            "description": "The approved relationship is visible.",
                        }
                    ],
                }
            ],
            "narrative": [
                {
                    "id": "section-1",
                    "title": "Agent memory",
                    "purpose": "Explain the approved relationship.",
                    "meaning_ids": ["meaning-1"],
                    "payoff": "The relationship is clear.",
                    "estimated_duration_s": 3.0,
                }
            ],
            "hooks": [
                {
                    "id": "hook-1",
                    "text": "What is agent memory?",
                    "payoff": "The relationship is clear.",
                    "meaning_ids": ["meaning-1"],
                },
                {
                    "id": "hook-2",
                    "text": "See how memory works.",
                    "payoff": "The relationship is clear.",
                    "meaning_ids": ["meaning-1"],
                },
            ],
            "recommended_hook_id": "hook-1",
            "ending": {
                "section_id": "section-1",
                "meaning_ids": ["meaning-1"],
                "takeaway": "The relationship is clear.",
            },
            "visual_plan": [
                {
                    "id": "visual-creative",
                    "section_id": "section-1",
                    "meaning_ids": ["meaning-1"],
                    "treatment": "A local deterministic creative browser effect.",
                    "purpose": "Explain the approved relationship.",
                    "approved_text": "ПАМЯТЬ АГЕНТА — ЭТО СИСТЕМА",
                    "asset_type": "diagram",
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
                    "target_duration_s": 3.0,
                    "minimum_duration_s": 1.5,
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
        self.runtime = self.video_dir / "runtime"
        self._write_fake_runtime()

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
                "user_quote": "I approve this exact creative visual plan.",
            },
        )
        write_creative_approval(self.edit_dir)

    def _write_fake_runtime(self) -> None:
        files = {
            "vendor/sprut-pixi.js": "var SPRUT_PIXI={Application:function(){}};\n",
            "vendor/rough-notation.iife.js": "var RoughNotation={annotate:function(){}};\n",
            "vendor/lottie-light.min.js": "var lottie={loadAnimation:function(){}};\n",
            "vendor/sprut-three.js": "var SPRUT_THREE={Scene:function(){}};\n",
            "licenses/pixi.js-MIT.txt": "PixiJS MIT test license\n",
            "licenses/pixi-filters-MIT.txt": "pixi-filters MIT test license\n",
            "licenses/rough-notation-MIT.txt": "Rough Notation MIT test license\n",
            "licenses/lottie-web-MIT.txt": "lottie-web MIT test license\n",
            "licenses/three-MIT.txt": "Three.js MIT test license\n",
        }
        for relative, content in files.items():
            path = self.runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        packages = [
            ("pixi.js", "8.19.0"),
            ("pixi-filters", "6.1.5"),
            ("rough-notation", "0.5.1"),
            ("lottie-web", "5.13.0"),
            ("three", "0.185.1"),
        ]
        write_json(
            self.runtime / "THIRD_PARTY_PACKAGES.json",
            {
                "version": 1,
                "packages": [
                    {
                        "direct": True,
                        "integrity": "sha512-test",
                        "license": "MIT",
                        "license_files_in_package": ["LICENSE"],
                        "name": name,
                        "resolved": "registry fixture",
                        "version": version,
                    }
                    for name, version in packages
                ],
            },
        )
        recorded = [
            file_record(path, self.runtime)
            for path in sorted(self.runtime.rglob("*"))
            if path.is_file()
        ]
        write_json(
            self.runtime / "RUNTIME_MANIFEST.json",
            {
                "version": 1,
                "runtime_id": "sprut-creative-browser-v1",
                "policy": {
                    "local_only": True,
                    "network_required_for_render": False,
                    "remote_media_inputs": "prohibited",
                    "remotion": "prohibited",
                },
                "dependencies": dict(packages),
                "files": recorded,
            },
        )

    def _run(
        self, effect: str, *extra: str, accept_terms: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = [
                sys.executable,
                str(SCRIPTS / "scaffold_creative_browser_effect.py"),
                "--edit-dir",
                str(self.edit_dir),
                "--visual-id",
                "visual-creative",
                "--effect",
                effect,
                "--runtime-dir",
                str(self.runtime),
                "--gsap-bundle",
                str(self.gsap),
                *extra,
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
            / "creative-browser"
            / "visual-creative"
        )

    def test_templates_and_catalog_are_strict_offline_allowlists(self) -> None:
        expected = {
            "pixi-semantic-accent",
            "rough-screen-annotation",
            "lottie-local-icon",
            "three-spatial-system",
        }
        actual = {path.parent.name for path in (ASSETS / "templates").glob("*/template.json")}
        self.assertEqual(actual, expected)
        catalog = json.loads((ASSETS / "effects.catalog.v1.json").read_text(encoding="utf-8"))
        self.assertEqual({item["id"] for item in catalog["effects"]}, expected)
        self.assertEqual(catalog["deferred_effects"][0]["id"], "shader-transition")
        self.assertEqual(catalog["deferred_effects"][0]["status"], "blocked")
        for effect_id in sorted(expected):
            with self.subTest(effect=effect_id):
                directory = ASSETS / "templates" / effect_id
                metadata = json.loads((directory / "template.json").read_text(encoding="utf-8"))
                html = (directory / "index.html").read_text(encoding="utf-8")
                self.assertTrue(metadata["audited"])
                self.assertTrue(metadata["deterministic"])
                self.assertIn("./vendor/gsap.min.js", html)
                self.assertIn("window.SPRUTCreative.prepare", html)
                self.assertIn("window.SPRUTCreative.register", html)
                self.assertNotRegex(html, r"(?i)(?:src|href)=[\"'](?:https?:)?//")
                self.assertNotIn("Math.random", html)
                self.assertNotIn("Date.now", html)
                self.assertNotIn("setTimeout", html)
                self.assertNotIn("Remotion", html)
        self.assertIn("new api.Application", (ASSETS / "templates/pixi-semantic-accent/index.html").read_text(encoding="utf-8"))
        self.assertIn("RoughNotation.annotate", (ASSETS / "templates/rough-screen-annotation/index.html").read_text(encoding="utf-8"))
        self.assertIn("lottie.loadAnimation", (ASSETS / "templates/lottie-local-icon/index.html").read_text(encoding="utf-8"))
        self.assertIn("new api.WebGLRenderer", (ASSETS / "templates/three-spatial-system/index.html").read_text(encoding="utf-8"))

    def test_describe_json_is_side_effect_free_and_paths_ignore_skill_location(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "scaffold_creative_browser_effect.py"),
                "--describe-json",
            ],
            text=True,
            capture_output=True,
            check=False,
            cwd=self.video_dir,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["type"], "sprut_creative_browser_scaffolder_discovery")
        self.assertEqual(
            {item["id"] for item in payload["effects"] if item["callable"]},
            {
                "pixi-semantic-accent",
                "rough-screen-annotation",
                "lottie-local-icon",
                "three-spatial-system",
            },
        )
        self.assertEqual(payload["deferred_effects"][0]["id"], "shader-transition")
        self.assertFalse(payload["deferred_effects"][0]["callable"])
        self.assertEqual(Path(payload["defaults"]["studio_root"]), APP_HOME)
        self.assertEqual(
            Path(payload["defaults"]["creative_runtime"]),
            CREATIVE_BROWSER_RUNTIME,
        )
        self.assertTrue(payload["defaults"]["paths_are_independent_of_skill_install_location"])
        self.assertFalse((self.edit_dir / "animations").exists())

    def test_pixi_success_binds_approval_runtime_hashes_and_every_file(self) -> None:
        result = self._run(
            "pixi-semantic-accent", "--seed", "923", "--pixi-mode", "combined",
            "--duration", "6.5",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.instance / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["effect"]["type"], "pixi-semantic-accent")
        self.assertEqual(config["effect"]["seed"], 923)
        self.assertEqual(config["content"]["approved_text"], "ПАМЯТЬ АГЕНТА — ЭТО СИСТЕМА")
        self.assertFalse(config["runtime"]["network_allowed"])
        self.assertFalse(config["runtime"]["remotion"])
        terms = config["runtime"]["gsap_terms"]
        self.assertTrue(terms["terms_explicitly_accepted"])
        self.assertEqual(terms["version"], "3.14.2")
        self.assertEqual(terms["license_url"], "https://gsap.com/standard-license")
        self.assertEqual(terms["package_json_sha256"], sha256(self.gsap_root / "package.json"))
        self.assertIn(
            'data-composition-id="pixi-semantic-accent" data-start="0" data-duration="6.5"',
            (self.instance / "index.html").read_text(encoding="utf-8"),
        )
        self.assertTrue((self.instance / "vendor/sprut-pixi.js").is_file())
        self.assertFalse((self.instance / "vendor/sprut-three.js").exists())
        manifest = json.loads((self.instance / "source-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["visual"]["visual_id"], "visual-creative")
        self.assertEqual(manifest["effect"]["id"], "pixi-semantic-accent")
        self.assertEqual(manifest["review_requirement"], "full_preview_user_approval")
        self.assertEqual([item["name"] for item in manifest["runtime"]["packages"]], ["pixi.js", "pixi-filters"])
        self.assertTrue(manifest["runtime"]["gsap_terms"]["terms_explicitly_accepted"])
        self.assertTrue((self.instance / "vendor" / "gsap-package.json").is_file())
        self.assertTrue((self.instance / "vendor" / "GSAP_README.md").is_file())
        records = {item["path"]: item for item in manifest["files"]}
        expected_files = {
            path.relative_to(self.instance).as_posix()
            for path in self.instance.rglob("*")
            if path.is_file() and path.name != "source-manifest.json"
        }
        self.assertEqual(set(records), expected_files)
        for relative, record in records.items():
            self.assertEqual(record["sha256"], sha256(self.instance / relative))
        repeated = self._run("pixi-semantic-accent")
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("already exists", repeated.stderr)

    def test_refuses_gsap_copy_without_explicit_terms_acceptance(self) -> None:
        result = self._run("pixi-semantic-accent", accept_terms=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--accept-gsap-terms", result.stderr)
        self.assertFalse((self.edit_dir / "animations").exists())

    def test_failed_asset_gate_writes_no_output_tree(self) -> None:
        approval = json.loads((self.edit_dir / "approval.json").read_text(encoding="utf-8"))
        approval["proposal_sha256"] = "0" * 64
        write_json(self.edit_dir / "approval.json", approval)
        result = self._run("pixi-semantic-accent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("asset gate failed", result.stderr)
        self.assertFalse((self.edit_dir / "animations").exists())

    def test_lottie_requires_user_owned_local_pure_vector_json(self) -> None:
        icon = self.edit_dir / "assets" / "icon.json"
        write_json(
            icon,
            {
                "v": "5.13.0",
                "fr": 30,
                "ip": 0,
                "op": 45,
                "w": 256,
                "h": 256,
                "nm": "user-owned-vector-icon",
                "ddd": 0,
                "assets": [],
                "layers": [{"ty": 4, "nm": "shape", "ip": 0, "op": 45, "st": 0, "ks": {}, "shapes": []}],
            },
        )
        missing_attestation = self._run("lottie-local-icon", "--lottie-json", str(icon))
        self.assertNotEqual(missing_attestation.returncode, 0)
        self.assertIn("confirm-user-owned-lottie", missing_attestation.stderr)
        self.assertFalse(self.instance.exists())
        result = self._run(
            "lottie-local-icon",
            "--lottie-json",
            str(icon),
            "--confirm-user-owned-lottie",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.instance / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["effect"]["rights_attestation"], "user_owned")
        self.assertEqual(config["effect"]["source_sha256"], sha256(icon))
        self.assertEqual(sha256(self.instance / "assets/lottie-source.json"), sha256(icon))
        self.assertIn("window.SPRUT_LOTTIE_DATA", (self.instance / "lottie-data.js").read_text(encoding="utf-8"))

    def test_lottie_rejects_url_or_image_asset_and_external_path(self) -> None:
        outside = self.video_dir / "outside.json"
        write_json(outside, {"v": "5", "fr": 30, "ip": 0, "op": 30, "w": 100, "h": 100, "assets": [], "layers": []})
        external = self._run(
            "lottie-local-icon", "--lottie-json", str(outside), "--confirm-user-owned-lottie"
        )
        self.assertNotEqual(external.returncode, 0)
        self.assertIn("under the canonical edit directory", external.stderr)
        malicious = self.edit_dir / "assets" / "remote.json"
        write_json(
            malicious,
            {
                "v": "5",
                "fr": 30,
                "ip": 0,
                "op": 30,
                "w": 100,
                "h": 100,
                "assets": [{"id": "image", "u": "https://invalid.test/", "p": "x.png"}],
                "layers": [],
            },
        )
        remote = self._run(
            "lottie-local-icon", "--lottie-json", str(malicious), "--confirm-user-owned-lottie"
        )
        self.assertNotEqual(remote.returncode, 0)
        self.assertIn("image/footage assets are prohibited", remote.stderr)
        self.assertFalse(self.instance.exists())

    def test_three_is_off_by_default_and_requires_explicit_flag(self) -> None:
        blocked = self._run("three-spatial-system")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("off by default", blocked.stderr)
        self.assertFalse(self.instance.exists())
        result = self._run("three-spatial-system", "--enable-experimental-three", "--seed", "77")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.instance / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(config["effect"]["experimental"])
        self.assertTrue(config["effect"]["enabled"])
        self.assertEqual(config["effect"]["seed"], 77)

    def test_shader_is_machine_readable_blocked_not_callable(self) -> None:
        result = self._run("shader-transition")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no_audited_seek_safe_compositor", result.stderr)
        self.assertFalse((self.edit_dir / "animations").exists())

    def test_runtime_tamper_and_wrong_approved_asset_type_fail_closed(self) -> None:
        bundle = self.runtime / "vendor/sprut-pixi.js"
        bundle.write_text(bundle.read_text(encoding="utf-8") + "// tampered\n", encoding="utf-8")
        tampered = self._run("pixi-semantic-accent")
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("differs from its pinned manifest", tampered.stderr)
        self.assertFalse((self.edit_dir / "animations").exists())

        self._write_fake_runtime()
        self.plan["visual_plan"][0]["asset_type"] = "cta"
        write_json(self.plan_path, self.plan)
        self._approve()
        wrong_type = self._run("rough-screen-annotation")
        self.assertNotEqual(wrong_type.returncode, 0)
        self.assertIn("requires approved asset_type", wrong_type.stderr)
        self.assertFalse((self.edit_dir / "animations").exists())


if __name__ == "__main__":
    unittest.main()
