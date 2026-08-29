from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import creative_tool_router as router  # noqa: E402


class CreativeToolRouterTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tool_map, self.input_schema, self.output_schema = router.loaded_contracts()
        self.approved = router.ApprovalContext(
            visual_id="visual-creative",
            section_id="section-creative",
            meaning_ids=("meaning-creative",),
            asset_type="title",
            plan_sha256="a" * 64,
            approval_sha256="b" * 64,
        )
        self.hashes = router.RouterHashes(
            feature_input_sha256="c" * 64,
            tool_map_sha256=router.file_sha256(router.TOOL_MAP_PATH),
            input_schema_sha256=router.file_sha256(router.INPUT_SCHEMA_PATH),
            output_schema_sha256=router.file_sha256(router.OUTPUT_SCHEMA_PATH),
            router_sha256=router.file_sha256(router.SCRIPT_PATH),
        )

    def request(
        self,
        *,
        approved: router.ApprovalContext | None = None,
        signals: tuple[str, ...] = ("keyword_emphasis",),
    ) -> dict[str, object]:
        value = router.sample_request(approved or self.approved, signals=signals)
        return router.normalize_request(value)

    def route(
        self,
        request: dict[str, object],
        approved: router.ApprovalContext | None = None,
    ) -> dict[str, object]:
        router.validate_with_schema(request, self.input_schema, "test input")
        decision = router.route_request(
            request,
            self.tool_map,
            approved or self.approved,
            self.hashes,
        )
        router.validate_with_schema(decision, self.output_schema, "test decision")
        return decision

    def test_map_is_local_free_and_has_human_routing_guidance(self) -> None:
        router.validate_tool_map(self.tool_map)
        self.assertEqual(set(self.tool_map["signal_definitions"]), router.KNOWN_SIGNALS)
        self.assertGreaterEqual(len(self.tool_map["tools"]), 10)
        effect_ids: set[str] = set()
        for tool in self.tool_map["tools"]:
            self.assertEqual(tool["cost_class"], "free_local")
            self.assertTrue(tool["responsibilities"])
            self.assertTrue(tool["avoid"])
            for effect in tool["effects"]:
                self.assertNotIn(effect["id"], effect_ids)
                effect_ids.add(effect["id"])
                self.assertTrue(effect["recipe"])
                self.assertTrue(effect["match"]["required_any"])
        self.assertIn("kinetic_keyword", effect_ids)
        self.assertIn("screen_annotation", effect_ids)
        self.assertIn("depth_parallax", effect_ids)
        self.assertIn("beat_synced_accent", effect_ids)

    def test_sfx_map_exactly_matches_all_nine_generator_presets(self) -> None:
        listed = subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_creative_sfx.py"), "--list-presets"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        discovered = {
            item["id"] for item in json.loads(listed.stdout)["presets"]
        }
        mapped_tool = next(
            tool for tool in self.tool_map["tools"] if tool["id"] == "sprut_sfx"
        )
        mapped = {effect["id"] for effect in mapped_tool["effects"]}
        self.assertEqual(len(discovered), 9)
        self.assertEqual(mapped, discovered)
        for effect in mapped_tool["effects"]:
            self.assertIn(f"preset {effect['id']}", effect["recipe"])

    def test_registry_adapter_emits_complete_closed_availability_set(self) -> None:
        report = router.invoke_registry()
        availability = router.registry_availability(report, self.tool_map)
        entries = availability["available_tools"]
        self.assertEqual(
            {item["tool_id"] for item in entries},
            {tool["id"] for tool in self.tool_map["tools"]},
        )
        self.assertEqual(len(entries), len({item["tool_id"] for item in entries}))
        self.assertTrue(
            all(item["status"] in router.TOOL_STATUSES for item in entries)
        )
        by_id = {item["tool_id"]: item["status"] for item in entries}
        self.assertEqual(by_id["depth_anything_small"], "unavailable")
        self.assertEqual(by_id["gmic"], "unavailable")
        self.assertIn(by_id["threejs"], {"experimental", "unavailable"})
        self.assertIn(by_id["gl_transitions"], {"ready", "unavailable"})
        self.assertIn(by_id["xfade_easing"], {"ready", "unavailable"})
        self.assertEqual(availability["network_calls_made"], 0)

    def test_availability_cli_array_is_direct_router_input(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "creative_tool_router.py"),
                "availability",
                "--array-only",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        available_tools = json.loads(result.stdout)
        request = self.request()
        request["available_tools"] = available_tools
        request = router.normalize_request(request)
        router.validate_with_schema(request, self.input_schema, "registry-derived input")
        router.validate_request_semantics(request, self.tool_map)

    def test_selects_one_visual_and_at_most_one_audio_support(self) -> None:
        decision = self.route(self.request())
        self.assertEqual(decision["decision"], "effect")
        self.assertEqual(decision["primary_effect"]["tool_id"], "hyperframes_gsap")
        self.assertEqual(decision["primary_effect"]["effect_id"], "kinetic_keyword")
        self.assertEqual(
            [item["effect_id"] for item in decision["supporting_effects"]],
            ["semantic_hit"],
        )
        self.assertLessEqual(len(decision["supporting_effects"]), 1)

    def test_unordered_input_sets_produce_same_decision(self) -> None:
        first = self.request(signals=("keyword_emphasis", "emotional_payoff"))
        second = copy.deepcopy(first)
        second["scene"]["signals"].reverse()
        second["available_tools"].reverse()
        first = router.normalize_request(first)
        second = router.normalize_request(second)
        self.assertEqual(
            router.canonical_json_bytes(self.route(first)),
            router.canonical_json_bytes(self.route(second)),
        )

    def test_none_is_explicit_when_no_signal_or_asset_is_authorized(self) -> None:
        no_signal = self.route(self.request(signals=()))
        self.assertEqual(no_signal["decision"], "none")
        self.assertIn("No approved semantic signal", no_signal["none_reason"])
        self.assertIsNone(no_signal["primary_effect"])
        self.assertEqual(no_signal["supporting_effects"], [])

        approved_none = router.ApprovalContext(
            visual_id=self.approved.visual_id,
            section_id=self.approved.section_id,
            meaning_ids=self.approved.meaning_ids,
            asset_type="none",
            plan_sha256=self.approved.plan_sha256,
            approval_sha256=self.approved.approval_sha256,
        )
        no_asset = self.route(self.request(approved=approved_none), approved_none)
        self.assertEqual(no_asset["decision"], "none")
        self.assertIn("asset_type=none", no_asset["none_reason"])

    def test_never_uses_effect_only_to_hide_weak_cut(self) -> None:
        decision = self.route(self.request(signals=("hide_weak_cut", "keyword_emphasis")))
        self.assertEqual(decision["decision"], "none")
        self.assertIn("hide_weak_cut", decision["none_reason"])

    def test_formal_diagram_prefers_manim_then_falls_back_to_gsap(self) -> None:
        approved = router.ApprovalContext(
            visual_id=self.approved.visual_id,
            section_id=self.approved.section_id,
            meaning_ids=self.approved.meaning_ids,
            asset_type="diagram",
            plan_sha256=self.approved.plan_sha256,
            approval_sha256=self.approved.approval_sha256,
        )
        request = self.request(
            approved=approved,
            signals=("formal_logic", "diagram_relationship"),
        )
        request["scene"]["semantic_role"] = "explanation"
        request["scene"]["duration_s"] = 5.0
        request["available_tools"] = [
            {"tool_id": "manim", "status": "ready"},
            {"tool_id": "hyperframes_gsap", "status": "ready"},
        ]
        request = router.normalize_request(request)
        selected = self.route(request, approved)
        self.assertEqual(selected["primary_effect"]["effect_id"], "formal_diagram")

        next(
            item for item in request["available_tools"] if item["tool_id"] == "manim"
        )["status"] = "unavailable"
        fallback = self.route(router.normalize_request(request), approved)
        self.assertEqual(fallback["primary_effect"]["effect_id"], "draw_svg_diagram")
        self.assertTrue(
            any(
                item["effect_id"] == "formal_diagram" and item["code"] == "tool_unavailable"
                for item in fallback["rejected_candidates"]
            )
        )

    def test_important_screen_prefers_detail_safe_annotation(self) -> None:
        approved = router.ApprovalContext(
            visual_id=self.approved.visual_id,
            section_id=self.approved.section_id,
            meaning_ids=self.approved.meaning_ids,
            asset_type="diagram",
            plan_sha256=self.approved.plan_sha256,
            approval_sha256=self.approved.approval_sha256,
        )
        request = self.request(
            approved=approved,
            signals=("screen_target", "code_or_ui_detail", "diagram_relationship"),
        )
        request["scene"].update(
            {
                "semantic_role": "explanation",
                "screen_priority": "important",
                "desired_intensity": "restrained",
                "content_density": "high",
            }
        )
        request["available_tools"] = [
            {"tool_id": "rough_notation", "status": "ready"},
            {"tool_id": "virtual_camera", "status": "ready"},
            {"tool_id": "hyperframes_gsap", "status": "ready"},
        ]
        decision = self.route(router.normalize_request(request), approved)
        self.assertEqual(decision["primary_effect"]["effect_id"], "screen_annotation")
        self.assertIn("diagram_legibility_review", decision["required_qa"])
        self.assertTrue(any("important screen" in item for item in decision["guardrails"]))

    def test_explicit_shot_local_reframe_selects_camera_not_rough_annotation(self) -> None:
        approved = router.ApprovalContext(
            visual_id=self.approved.visual_id,
            section_id=self.approved.section_id,
            meaning_ids=self.approved.meaning_ids,
            asset_type="diagram",
            plan_sha256=self.approved.plan_sha256,
            approval_sha256=self.approved.approval_sha256,
        )
        request = self.request(
            approved=approved,
            signals=("screen_target", "shot_local_reframe"),
        )
        request["scene"].update(
            {
                "semantic_role": "definition",
                "screen_priority": "important",
                "desired_intensity": "restrained",
                "duration_s": 5.0,
            }
        )
        request["available_tools"] = [
            {"tool_id": "rough_notation", "status": "ready"},
            {"tool_id": "virtual_camera", "status": "ready"},
        ]
        decision = self.route(router.normalize_request(request), approved)
        self.assertEqual(decision["primary_effect"]["tool_id"], "virtual_camera")
        self.assertEqual(decision["primary_effect"]["effect_id"], "screen_reframe")
        rejected_rough = next(
            item
            for item in decision["rejected_candidates"]
            if item["effect_id"] == "screen_annotation"
        )
        self.assertEqual(rejected_rough["code"], "semantic_signal_mismatch")

    def test_experimental_depth_requires_explicit_flag(self) -> None:
        approved = router.ApprovalContext(
            visual_id=self.approved.visual_id,
            section_id=self.approved.section_id,
            meaning_ids=self.approved.meaning_ids,
            asset_type="b_roll",
            plan_sha256=self.approved.plan_sha256,
            approval_sha256=self.approved.approval_sha256,
        )
        request = self.request(
            approved=approved,
            signals=("spatial_depth", "keyword_emphasis"),
        )
        request["scene"]["desired_intensity"] = "hero"
        request["available_tools"] = [
            {"tool_id": "depth_anything_small", "status": "ready"}
        ]
        blocked = self.route(router.normalize_request(request), approved)
        self.assertEqual(blocked["decision"], "none")
        self.assertTrue(
            any(
                item["code"] in {"experimental_not_approved", "missing_scene_flag"}
                for item in blocked["rejected_candidates"]
            )
        )

        request["scene"]["scene_flags"] = ["experimental_effect_allowed"]
        selected = self.route(router.normalize_request(request), approved)
        self.assertEqual(selected["primary_effect"]["effect_id"], "depth_parallax")

    def test_density_budget_returns_none_instead_of_overeffect(self) -> None:
        request = self.request()
        request["available_tools"] = [
            {"tool_id": "motion_cards", "status": "ready"},
            {"tool_id": "hyperframes_gsap", "status": "ready"},
        ]
        request["timeline"]["prior_effects"] = [
            {
                "tool_id": "hyperframes_gsap",
                "effect_id": "kinetic_keyword",
                "start_s": start,
                "end_s": start + 1.0,
                "intensity": "medium",
                "layer_count": 1,
            }
            for start in (1.0, 5.0, 9.0, 13.0)
        ]
        decision = self.route(router.normalize_request(request))
        self.assertEqual(decision["decision"], "none")
        self.assertIn("density/cooldown", decision["none_reason"])
        self.assertEqual(decision["density"]["primary_events_before"], 4)

    def test_unknown_fields_duplicate_tools_and_stale_binding_fail_closed(self) -> None:
        unknown = self.request()
        unknown["scene"]["surprise"] = True
        with self.assertRaisesRegex(router.CreativeRouterError, "additional property"):
            router.validate_with_schema(unknown, self.input_schema, "test input")

        duplicate = self.request()
        duplicate["available_tools"].append(copy.deepcopy(duplicate["available_tools"][0]))
        duplicate = router.normalize_request(duplicate)
        with self.assertRaisesRegex(router.CreativeRouterError, "duplicate tool ids"):
            router.validate_request_semantics(duplicate, self.tool_map)

        stale = self.request()
        stale["binding"]["semantic_plan_sha256"] = "0" * 64
        with self.assertRaisesRegex(router.CreativeRouterError, "does not match"):
            router.route_request(stale, self.tool_map, self.approved, self.hashes)

    def test_invalid_prior_effect_contract_fails_closed(self) -> None:
        request = self.request()
        request["timeline"]["prior_effects"] = [
            {
                "tool_id": "hyperframes_gsap",
                "effect_id": "unknown_effect",
                "start_s": 1.0,
                "end_s": 2.0,
                "intensity": "medium",
                "layer_count": 1,
            }
        ]
        request = router.normalize_request(request)
        router.validate_with_schema(request, self.input_schema, "test input")
        with self.assertRaisesRegex(router.CreativeRouterError, "mapped tool/effect pair"):
            router.validate_request_semantics(request, self.tool_map)

    def test_cli_self_test_is_json_and_offline(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "creative_tool_router.py"), "self-test"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["network_calls_made"], 0)
        self.assertGreaterEqual(len(report["checks"]), 4)


if __name__ == "__main__":
    unittest.main()
