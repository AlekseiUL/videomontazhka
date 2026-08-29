from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import scaffold_gsap_creative_effect as gsap_scaffold  # noqa: E402
from asset_gate import AssetGateError  # noqa: E402
from runtime_paths import HYPERFRAMES_RUNTIME  # noqa: E402
from visual_asset_provenance import ApprovedVisualPlanItem, FileSnapshot, file_sha256  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class GSAPCreativeEffectScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.edit = self.root / "edit"
        self.edit.mkdir()
        self.plan = self.edit / "semantic_plan.json"
        self.approval = self.edit / "approval.json"
        write_json(self.plan, {"status": "approved-test-snapshot"})
        write_json(self.approval, {"status": "approved-test-snapshot"})
        self.gsap_root = self.root / "runtime" / "gsap"
        write_json(
            self.gsap_root / "package.json",
            {
                "name": "gsap",
                "version": gsap_scaffold.PINNED_GSAP_VERSION,
                "license": "Standard no charge package license: https://gsap.com/standard-license.",
            },
        )
        (self.gsap_root / "README.md").write_text(
            "Synthetic offline GSAP fixture for unit tests.\n",
            encoding="utf-8",
        )
        bundle_names = {
            "gsap.min.js",
            *(name for names in gsap_scaffold.EFFECT_PLUGIN_CONTRACT.values() for name in names),
        }
        for name in bundle_names:
            bundle = self.gsap_root / "dist" / name
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.write_bytes(b"/* synthetic offline test bundle */\n" + b"x" * 1100)
        self.gsap_patch = mock.patch.object(gsap_scaffold, "GSAP_PACKAGE_ROOT", self.gsap_root)
        self.gsap_patch.start()

    def tearDown(self) -> None:
        self.gsap_patch.stop()
        self.temporary.cleanup()

    def approved(
        self,
        *,
        visual_id: str = "route-visual",
        asset_type: str = "process",
        approved_text: str | None = "Сигнал превращается в решение",
    ) -> ApprovedVisualPlanItem:
        return ApprovedVisualPlanItem(
            visual_id=visual_id,
            section_id="section-mechanism",
            meaning_ids=("meaning-mechanism",),
            purpose="Показать механизм по шагам.",
            treatment="Белая схема с одной оранжевой активной траекторией.",
            asset_type=asset_type,
            approved_text=approved_text,
            plan_snapshot=FileSnapshot(self.plan.resolve(), file_sha256(self.plan)),
            approval_snapshot=FileSnapshot(self.approval.resolve(), file_sha256(self.approval)),
        )

    def spec(
        self,
        *,
        visual_id: str = "route-visual",
        effect_type: str = "route_draw",
        options: dict[str, object] | None = None,
        extra: dict[str, object] | None = None,
    ) -> Path:
        by_effect: dict[str, dict[str, object]] = {
            "kinetic_split_keyword": {"split": "words", "accent_word_index": 0},
            "morph_concept": {"target_shape": "arrow"},
            "route_draw": {"path_style": "arc", "auto_rotate": False},
            "data_scramble": {"charset": "upper_numeric", "reveal_order": "start"},
            "flip_before_after": {"layout": "side_by_side"},
        }
        value: dict[str, object] = {
            "version": 1,
            "visual_id": visual_id,
            "effect_type": effect_type,
            "composition": {"width": 1920, "height": 1080, "fps": 30, "duration_s": 4.2},
            "options": options if options is not None else by_effect.get(effect_type, {}),
        }
        if extra:
            value.update(extra)
        path = self.edit / "requests" / f"{visual_id}.json"
        write_json(path, value)
        return path

    def args(
        self,
        spec: Path,
        visual_id: str = "route-visual",
        *,
        accept_terms: bool = True,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            edit_dir=self.edit,
            visual_id=visual_id,
            spec=spec,
            accept_gsap_terms=accept_terms,
        )

    def test_describe_json_catalog_is_complete_and_source_only(self) -> None:
        description = gsap_scaffold.describe_catalog()
        effects = {item["effect_type"]: item for item in description["effects"]}
        self.assertEqual(set(effects), set(gsap_scaffold.AUDITED_EFFECTS))
        self.assertEqual(effects["route_draw"]["plugins"], ["DrawSVGPlugin", "MotionPathPlugin"])
        self.assertEqual(effects["flip_before_after"]["plugins"], ["Flip"])
        self.assertTrue(description["constraints"]["semantic_approval_required"])
        self.assertTrue(description["constraints"]["source_only"])
        self.assertFalse(description["constraints"]["network_allowed"])
        self.assertEqual(description["constraints"]["paid_apis"], [])

    def test_default_runtime_path_is_shared_and_does_not_depend_on_skill_location(self) -> None:
        expected = HYPERFRAMES_RUNTIME / "node_modules" / "gsap"
        self.assertEqual(gsap_scaffold.DEFAULT_GSAP_PACKAGE_ROOT, expected)
        self.assertFalse(gsap_scaffold.GSAP_PACKAGE_ROOT.is_relative_to(gsap_scaffold.SKILL_ROOT))

    def test_strict_schema_rejects_unknown_option_and_unsupported_effect(self) -> None:
        unknown_option = self.spec(options={"path_style": "arc", "auto_rotate": False, "surprise": 1})
        with self.assertRaisesRegex(gsap_scaffold.GSAPCreativeError, "additional property"):
            gsap_scaffold.load_spec(self.edit, unknown_option)

        unsupported = self.spec(visual_id="bad-effect", effect_type="magic_cloud")
        with self.assertRaisesRegex(gsap_scaffold.GSAPCreativeError, "effect_type"):
            gsap_scaffold.load_spec(self.edit, unsupported)

    def test_spec_must_stay_under_edit_and_match_visual_id(self) -> None:
        outside = self.root / "outside.json"
        write_json(outside, {})
        with self.assertRaisesRegex(AssetGateError, "under the canonical edit directory"):
            gsap_scaffold.load_spec(self.edit, outside)

        path = self.spec(visual_id="spec-id")
        with self.assertRaisesRegex(gsap_scaffold.GSAPCreativeError, "exactly equal"):
            with mock.patch.object(gsap_scaffold, "require_asset_gate"):
                gsap_scaffold.scaffold(self.args(path, visual_id="cli-id"))

    def test_gate_failure_creates_nothing(self) -> None:
        path = self.spec()
        with mock.patch.object(
            gsap_scaffold,
            "require_asset_gate",
            side_effect=AssetGateError("synthetic gate failure"),
        ) as gate:
            with self.assertRaisesRegex(AssetGateError, "synthetic gate failure"):
                gsap_scaffold.scaffold(self.args(path))
        gate.assert_called_once_with(self.edit.resolve())
        self.assertFalse((self.edit / "animations").exists())

    def test_refuses_gsap_copy_without_explicit_terms_acceptance(self) -> None:
        path = self.spec()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "scaffold_gsap_creative_effect.py"),
                "--edit-dir",
                str(self.edit),
                "--visual-id",
                "route-visual",
                "--spec",
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--accept-gsap-terms", result.stderr)
        self.assertFalse((self.edit / "animations").exists())

    def test_wrong_approved_asset_type_and_null_text_fail_closed(self) -> None:
        path = self.spec()
        with mock.patch.object(gsap_scaffold, "require_asset_gate"), mock.patch.object(
            gsap_scaffold,
            "load_approved_visual_plan_item",
            return_value=self.approved(asset_type="chapter"),
        ):
            with self.assertRaisesRegex(gsap_scaffold.GSAPCreativeError, "requires approved asset_type"):
                gsap_scaffold.scaffold(self.args(path))

        with mock.patch.object(gsap_scaffold, "require_asset_gate"), mock.patch.object(
            gsap_scaffold,
            "load_approved_visual_plan_item",
            return_value=self.approved(approved_text=None),
        ):
            with self.assertRaisesRegex(gsap_scaffold.GSAPCreativeError, "requires non-null approved_text"):
                gsap_scaffold.scaffold(self.args(path))
        self.assertFalse((self.edit / "animations").exists())

    def test_scaffold_copies_only_required_local_bundles_and_hashes_every_file(self) -> None:
        path = self.spec()
        approved = self.approved()
        with mock.patch.object(gsap_scaffold, "require_asset_gate") as gate, mock.patch.object(
            gsap_scaffold,
            "load_approved_visual_plan_item",
            return_value=approved,
        ) as lookup:
            target = gsap_scaffold.scaffold(self.args(path))

        gate.assert_called_once_with(self.edit.resolve())
        lookup.assert_called_once_with(self.edit.resolve(), "route-visual")
        self.assertTrue(target.is_relative_to(self.edit.resolve()))
        self.assertEqual(
            target,
            self.edit.resolve() / "animations" / "hyperframes" / "gsap-creative" / "route-visual",
        )

        manifest_path = target / "source-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["visual"]["visual_id"], "route-visual")
        self.assertEqual(manifest["visual"]["approved_text"], approved.approved_text)
        self.assertEqual(manifest["effect"]["plugins"], ["DrawSVGPlugin", "MotionPathPlugin"])
        self.assertTrue(manifest["runtime"]["offline"])
        self.assertFalse(manifest["runtime"]["network_allowed"])
        self.assertEqual(manifest["runtime"]["paid_apis"], [])
        terms = json.loads((target / "config.json").read_text(encoding="utf-8"))["runtime"]["gsap_terms"]
        self.assertTrue(terms["terms_explicitly_accepted"])
        self.assertEqual(terms["version"], gsap_scaffold.PINNED_GSAP_VERSION)
        self.assertEqual(terms["license_url"], "https://gsap.com/standard-license")
        self.assertEqual(terms["package_json_sha256"], file_sha256(self.gsap_root / "package.json"))
        self.assertTrue(manifest["runtime"]["gsap"]["terms_explicitly_accepted"])
        self.assertEqual(manifest["runtime"]["gsap"]["license_url"], terms["license_url"])
        self.assertTrue((target / "vendor" / "gsap-package.json").is_file())
        self.assertTrue((target / "vendor" / "GSAP_README.md").is_file())
        self.assertEqual(manifest["review_requirement"], "full_preview_user_approval")

        runtime_names = {Path(item["path"]).name for item in manifest["runtime"]["gsap"]["bundles"]}
        self.assertEqual(runtime_names, {"gsap.min.js", "DrawSVGPlugin.min.js", "MotionPathPlugin.min.js"})
        for item in manifest["runtime"]["gsap"]["bundles"]:
            copied = target / item["path"]
            source = Path(item["source_path"])
            self.assertEqual(item["sha256"], file_sha256(copied))
            self.assertEqual(item["source_sha256"], file_sha256(source))
            self.assertEqual(item["sha256"], item["source_sha256"])
        self.assertFalse((target / "vendor" / "SplitText.min.js").exists())
        self.assertFalse((target / "vendor" / "MorphSVGPlugin.min.js").exists())

        actual = {
            str(file.relative_to(target)): file
            for file in target.rglob("*")
            if file.is_file() and file.name != "source-manifest.json"
        }
        recorded = {item["path"]: item for item in manifest["files"]}
        self.assertEqual(set(recorded), set(actual))
        for relative, file in actual.items():
            self.assertEqual(recorded[relative]["sha256"], file_sha256(file))
            self.assertEqual(recorded[relative]["size_bytes"], file.stat().st_size)

        html = (target / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"(?:https?:)?//")
        config = json.loads((target / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["content"]["approved_text"], approved.approved_text)
        self.assertEqual(
            tuple(gsap_scaffold.normalized_words("\n".join(config["content"]["fragments"]))),
            tuple(gsap_scaffold.normalized_words(approved.approved_text or "")),
        )

        before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        with mock.patch.object(gsap_scaffold, "require_asset_gate"), mock.patch.object(
            gsap_scaffold,
            "load_approved_visual_plan_item",
            return_value=approved,
        ):
            with self.assertRaisesRegex(gsap_scaffold.GSAPCreativeError, "already exists"):
                gsap_scaffold.scaffold(self.args(path))
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), before)

    def test_all_templates_are_offline_and_request_the_audited_plugin_set(self) -> None:
        for effect_type in gsap_scaffold.AUDITED_EFFECTS:
            with self.subTest(effect_type=effect_type):
                contract = gsap_scaffold.effect_contract(effect_type)
                source = contract.template_source.read_text(encoding="utf-8")
                self.assertNotRegex(source, r"(?:https?:)?//")
                self.assertNotIn("fetch(", source)
                self.assertNotIn("repeat: -1", source)
                for plugin in gsap_scaffold.EFFECT_PLUGIN_CONTRACT[effect_type]:
                    self.assertIn(f"./vendor/{plugin}", source)


if __name__ == "__main__":
    unittest.main()
