from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPO = ROOT.parents[3]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class Hardening011RegressionTest(unittest.TestCase):
    def test_gate3_creative_approval_blocks_graphics_and_sfx_until_bound_human_approval(self) -> None:
        creative_approval = importlib.import_module("creative_approval")
        with tempfile.TemporaryDirectory(prefix="videomontazhka-gate3-") as temporary:
            edit_dir = Path(temporary) / "edit"
            edit_dir.mkdir()
            treatment = edit_dir / "creative_treatment_plan.json"
            treatment.write_text(
                json.dumps({"version": 1, "type": "sprut_creative_treatment_plan"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "creative approval"):
                creative_approval.require_creative_approval(edit_dir)

            digest = hashlib.sha256(treatment.read_bytes()).hexdigest()
            approval = {
                "version": 1,
                "type": "videomontazhka_creative_approval",
                "status": "approved",
                "creative_treatment_plan": "creative_treatment_plan.json",
                "creative_treatment_plan_sha256": digest,
                "user_quote": "I approve this exact visual and audio treatment.",
            }
            (edit_dir / "creative_approval.json").write_text(
                json.dumps(approval) + "\n", encoding="utf-8"
            )
            observed = creative_approval.require_creative_approval(edit_dir)
            self.assertEqual(observed["creative_treatment_plan_sha256"], digest)

        visual_source = (SCRIPTS / "visual_asset_provenance.py").read_text(encoding="utf-8")
        sfx_source = (SCRIPTS / "generate_creative_sfx.py").read_text(encoding="utf-8")
        self.assertIn("require_creative_approval(canonical)", visual_source)
        self.assertIn("require_creative_approval(edit_dir)", sfx_source)

    def test_strict_overall_minimum_is_independent_from_target_plus_minus_15_percent(self) -> None:
        validate_gate = importlib.import_module("validate_gate")
        errors: list[str] = []
        validate_gate.validate_duration_contract(
            {
                "id": "youtube",
                "target_duration_s": 100.0,
                "minimum_duration_s": 95.0,
            },
            retained_duration_s=90.0,
            errors=errors,
        )
        self.assertTrue(any("strict minimum" in error for error in errors), errors)
        self.assertFalse(any("±15%" in error for error in errors), errors)

        errors = []
        validate_gate.validate_duration_contract(
            {
                "id": "youtube",
                "target_duration_s": 100.0,
                "minimum_duration_s": 70.0,
            },
            retained_duration_s=84.9,
            errors=errors,
        )
        self.assertTrue(any("±15%" in error for error in errors), errors)

        schema = json.loads((ROOT / "assets/semantic-plan.schema.json").read_text(encoding="utf-8"))
        deliverable = schema["$defs"]["deliverable"]
        self.assertIn("minimum_duration_s", deliverable["required"])

    def test_doctor_rejects_non_python_executable_and_accepts_compatible_python(self) -> None:
        doctor = importlib.import_module("doctor")
        false_status = doctor.python_runtime_status(Path("/usr/bin/true"))
        self.assertFalse(false_status["ok"], false_status)
        self.assertIn("not a compatible Python", false_status["error"])
        real_status = doctor.python_runtime_status(Path(sys.executable))
        self.assertTrue(real_status["ok"], real_status)
        self.assertGreaterEqual(tuple(real_status["version_info"][:2]), (3, 11))

    def test_runtime_install_lock_is_exclusive_bounded_and_recoverable(self) -> None:
        install_runtime = importlib.import_module("install_runtime")
        with tempfile.TemporaryDirectory(prefix="videomontazhka-lock-") as temporary:
            target = Path(temporary) / "runtime" / "python"
            with install_runtime.install_lock(target, timeout_s=0.2, poll_s=0.01):
                with self.assertRaisesRegex(install_runtime.RuntimeInstallError, "in progress"):
                    with install_runtime.install_lock(target, timeout_s=0.05, poll_s=0.01):
                        self.fail("contending installer acquired the same lock")
            with install_runtime.install_lock(target, timeout_s=0.2, poll_s=0.01):
                self.assertFalse(target.exists())

    def test_runtime_manifest_inventory_includes_transitive_installed_packages(self) -> None:
        install_runtime = importlib.import_module("install_runtime")
        inventory = install_runtime.package_inventory(
            {"Pillow": "12.3.0", "requests": "2.34.2", "urllib3": "2.6.3"},
            direct_names=["Pillow", "requests"],
        )
        self.assertEqual([item["name"] for item in inventory], ["Pillow", "requests", "urllib3"])
        self.assertEqual(
            {item["name"]: item["direct"] for item in inventory},
            {"Pillow": True, "requests": True, "urllib3": False},
        )

    def test_setup_python_attribution_is_present(self) -> None:
        text = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("actions/setup-python", text)
        self.assertIn("https://github.com/actions/setup-python", text)
        self.assertRegex(text, r"actions/setup-python[\s\S]{0,500}MIT")

    def test_pillow_notice_uses_upstream_mit_cmu_license_expression(self) -> None:
        text = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"\| Pillow \| `12\.3\.0` \|[^\n]+\| MIT-CMU \|")
        self.assertNotRegex(text, r"\| Pillow \| `12\.3\.0` \|[^\n]+\| HPND \|")

    def test_dependency_disclosure_matches_runtime_manifest_fields(self) -> None:
        text = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("имена и версии пакетов и признак `direct`", text)
        self.assertIn("Источник и лицензия каждого установленного пакета", text)
        self.assertIn("в текущую версию runtime\nmanifest не входят", text)
        self.assertIn("Заявленные прямые и опциональные зависимости", text)
        self.assertIn("точный фактически установленный\nтранзитивный состав", text)
        self.assertNotIn("признак `direct` и источник", text)
        self.assertNotIn("Полный список и лицензионная политика", text)

    def test_public_manifests_link_owner_and_sprut_ai(self) -> None:
        manifests = (
            REPO / ".agents/plugins/marketplace.json",
            REPO / "plugins/videomontazhka/.codex-plugin/plugin.json",
        )
        for path in manifests:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("Aleksei Ulyanov", text)
                self.assertIn("https://github.com/AlekseiUL", text)
                self.assertIn("SPRUT_AI", text)
                self.assertIn("https://t.me/Sprut_AI", text)

    def test_verified_upstream_wording_is_unified(self) -> None:
        documents = (
            REPO / "README.md",
            REPO / "PROVENANCE.md",
            REPO / "THIRD_PARTY_NOTICES.md",
        )
        phrase = "verified default-branch revisions"
        for path in documents:
            with self.subTest(path=path):
                self.assertIn(phrase, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
