#!/usr/bin/env python3
"""Choose one purpose-fit creative effect from approval-bound scene features.

The router is intentionally conservative.  It does not invent semantic
features, parse prose, render media, or make network calls.  A normal routing
run requires the current SPRUT asset gate, an exact binding to one approved
``semantic_plan.visual_plan`` item, explicit local tool readiness, and a strict
JSON feature document.  It emits at most one primary visual effect and one
supporting audio accent; ``none`` is a first-class safe decision.

Commands::

    creative_tool_router.py route --edit-dir /project/edit \
        --input /project/edit/creative/visual-1.features.json \
        --output /project/edit/creative/visual-1.decision.json
    creative_tool_router.py availability --array-only
    creative_tool_router.py tools
    creative_tool_router.py self-test

No third-party package is required by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from schema_check import SchemaDefinitionError, Validator
from visual_asset_provenance import (
    ApprovedVisualPlanItem,
    VisualProvenanceError,
    load_approved_visual_plan_item,
)


ROUTER_VERSION = "sprut-creative-router-1"
SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parent.parent
TOOL_MAP_PATH = SKILL_ROOT / "assets" / "creative-tool-router-map.v1.json"
INPUT_SCHEMA_PATH = SKILL_ROOT / "schemas" / "creative_router_input.schema.json"
OUTPUT_SCHEMA_PATH = SKILL_ROOT / "schemas" / "creative_decision.schema.json"
CREATIVE_REGISTRY_PATH = SCRIPT_PATH.parent / "creative_tool_registry.py"
CREATIVE_SFX_PATH = SCRIPT_PATH.parent / "generate_creative_sfx.py"
MOTION_CARD_PATH = SCRIPT_PATH.parent / "render_motion_card.py"
MAX_JSON_BYTES = 4 * 1024 * 1024

ASSET_TYPES = {
    "none",
    "title",
    "chapter",
    "diagram",
    "comparison",
    "process",
    "quote",
    "cta",
    "b_roll",
}
SEMANTIC_ROLES = {
    "hook",
    "definition",
    "explanation",
    "comparison",
    "process",
    "evidence",
    "demonstration",
    "payoff",
    "chapter",
    "transition",
    "cta",
}
SCREEN_PRIORITIES = {"none", "context", "important"}
PRESENTER_GEOMETRIES = {"none", "circle", "rectangle", "full_frame", "isolated_subject"}
INTENSITIES = {"restrained", "medium", "hero"}
TOOL_MATURITIES = {"core", "optional", "experimental"}
TOOL_STATUSES = {"ready", "experimental", "unavailable"}
EFFECT_KINDS = {"primary_visual", "support_audio"}
SCENE_FLAGS = {
    "experimental_effect_allowed",
    "person_matte_allowed",
    "screen_full_frame_visual_approved",
    "transition_renderer_verified",
}
AUDIO_FLAGS = {"speech_present", "music_present", "beat_map_available", "sfx_allowed"}
QA_TYPES = {
    "asset_provenance",
    "audio_mix_review",
    "boundary_frame_review",
    "caption_collision_review",
    "diagram_legibility_review",
    "full_preview_user_approval",
    "matte_edge_review",
    "motion_stability_review",
    "transition_asset_qa",
    "visual_preview_sheet",
}
KNOWN_SIGNALS = {
    "abstract_system",
    "action_cta",
    "cause_effect",
    "chapter_change",
    "code_or_ui_detail",
    "comparison_contrast",
    "concept_icon",
    "definition_reveal",
    "diagram_relationship",
    "digital_error",
    "emotional_payoff",
    "formal_logic",
    "freeze_frame_payoff",
    "geometry_match",
    "hide_weak_cut",
    "identity_context",
    "keyword_emphasis",
    "music_rhythm",
    "ordered_steps",
    "screen_target",
    "shot_local_reframe",
    "source_quote",
    "spatial_depth",
    "speaker_foreground_layering",
    "statistic_payoff",
    "time_place_change",
    "title_scope",
    "transition_motivated",
    "transformation_relationship",
}

MAP_ROOT_KEYS = {"version", "map_id", "policy", "signal_definitions", "tools"}
REGISTRY_ROOT_KEYS = {
    "version",
    "type",
    "host",
    "network_calls_made",
    "paid_api_allowlist",
    "capability_ids",
    "engines",
    "experimental_not_default",
    "requirements",
}
MAP_POLICY_KEYS = {
    "density_window_s",
    "max_primary_events_per_window",
    "max_hero_events_per_window",
    "max_audio_support_events_per_window",
    "max_visual_coverage_ratio",
    "max_active_visual_layers",
    "max_supporting_effects",
    "global_block_signals",
}
TOOL_KEYS = {
    "id",
    "label",
    "engine",
    "cost_class",
    "license",
    "maturity",
    "responsibilities",
    "avoid",
    "effects",
}
EFFECT_KEYS = {
    "id",
    "label",
    "kind",
    "family",
    "selection_rank",
    "hero",
    "experimental",
    "detail_safe",
    "base_intensity",
    "min_duration_s",
    "max_duration_s",
    "cooldown_s",
    "full_frame",
    "match",
    "qa",
    "recipe",
}
MATCH_KEYS = {
    "asset_types",
    "semantic_roles",
    "required_all",
    "required_any",
    "forbidden_any",
    "screen_priorities",
    "presenter_geometries",
    "desired_intensities",
    "required_scene_flags",
    "required_audio_flags",
    "required_tools",
}


class CreativeRouterError(RuntimeError):
    """A fail-closed routing, contract, or input error."""


@dataclass(frozen=True)
class ApprovalContext:
    visual_id: str
    section_id: str
    meaning_ids: tuple[str, ...]
    asset_type: str
    plan_sha256: str
    approval_sha256: str


@dataclass(frozen=True)
class RouterHashes:
    feature_input_sha256: str
    tool_map_sha256: str
    input_schema_sha256: str
    output_schema_sha256: str
    router_sha256: str


@dataclass(frozen=True)
class EffectRef:
    tool: Mapping[str, Any]
    effect: Mapping[str, Any]


@dataclass(frozen=True)
class DensityState:
    window_start_s: float
    window_end_s: float
    primary_events_before: int
    hero_events_before: int
    audio_support_events_before: int
    visual_coverage_ratio_before: float
    active_visual_layers_before: int
    max_primary_events: int
    max_hero_events: int
    max_audio_support_events: int
    max_visual_coverage_ratio: float
    max_active_visual_layers: int
    max_supporting_effects: int
    window_s: float
    blocks: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "window_start_s": rounded(self.window_start_s),
            "window_end_s": rounded(self.window_end_s),
            "primary_events_before": self.primary_events_before,
            "hero_events_before": self.hero_events_before,
            "audio_support_events_before": self.audio_support_events_before,
            "visual_coverage_ratio_before": rounded(self.visual_coverage_ratio_before, 6),
            "active_visual_layers_before": self.active_visual_layers_before,
            "limits": {
                "window_s": rounded(self.window_s),
                "max_primary_events": self.max_primary_events,
                "max_hero_events": self.max_hero_events,
                "max_audio_support_events": self.max_audio_support_events,
                "max_visual_coverage_ratio": rounded(self.max_visual_coverage_ratio, 6),
                "max_active_visual_layers": self.max_active_visual_layers,
                "max_supporting_effects": self.max_supporting_effects,
            },
            "blocks": list(self.blocks),
        }


@dataclass(frozen=True)
class CandidateResult:
    ref: EffectRef
    matched_signals: tuple[str, ...]
    eligible: bool
    rejection_code: str | None = None
    rejection_reason: str | None = None


def rounded(value: float, digits: int = 3) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0 else result


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CreativeRouterError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def load_json_object(path: Path, label: str, *, reject_symlink: bool = True) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if reject_symlink and path.is_symlink():
        raise CreativeRouterError(f"{label} must not be a symlink: {path}")
    if not resolved.is_file():
        raise CreativeRouterError(f"{label} not found: {resolved}")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise CreativeRouterError(
            f"{label} size must be between 1 and {MAX_JSON_BYTES} bytes: {resolved}"
        )
    try:
        raw = resolved.read_bytes()
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CreativeRouterError(f"cannot load {label} {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise CreativeRouterError(f"{label} must be a JSON object: {resolved}")
    return value


def validate_with_schema(instance: Any, schema: Mapping[str, Any], label: str) -> None:
    try:
        errors = Validator(dict(schema)).validate(instance)
    except SchemaDefinitionError as exc:
        raise CreativeRouterError(f"{label} schema is invalid: {exc}") from exc
    if errors:
        rendered = "\n".join(f"- {error.render()}" for error in errors[:50])
        suffix = "\n- additional errors omitted" if len(errors) > 50 else ""
        raise CreativeRouterError(f"{label} failed schema validation:\n{rendered}{suffix}")


def require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CreativeRouterError(f"{label} fields are not canonical; missing={missing}, extra={extra}")


def require_trimmed_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or (not allow_empty and not value):
        raise CreativeRouterError(f"{label} must be a trimmed {'string' if allow_empty else 'non-empty string'}")
    return value


def require_string_list(
    value: Any,
    label: str,
    *,
    allowed: set[str] | None = None,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        suffix = "" if allow_empty else " non-empty"
        raise CreativeRouterError(f"{label} must be a{suffix} JSON array")
    result: list[str] = []
    for index, raw in enumerate(value):
        item = require_trimmed_text(raw, f"{label}[{index}]")
        if allowed is not None and item not in allowed:
            raise CreativeRouterError(f"{label}[{index}] is not allowed: {item!r}")
        result.append(item)
    if len(result) != len(set(result)):
        raise CreativeRouterError(f"{label} contains duplicates")
    return tuple(result)


def finite_number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CreativeRouterError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise CreativeRouterError(f"{label} must be between {low:g} and {high:g}")
    return number


def integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CreativeRouterError(f"{label} must be an integer between {low} and {high}")
    return value


def validate_tool_map(tool_map: Mapping[str, Any]) -> None:
    require_exact_keys(tool_map, MAP_ROOT_KEYS, "tool map")
    if tool_map.get("version") != 1 or tool_map.get("map_id") != "sprut-creative-tool-map-v1":
        raise CreativeRouterError("tool map must be the canonical v1 map")

    policy = tool_map.get("policy")
    if not isinstance(policy, dict):
        raise CreativeRouterError("tool map policy must be an object")
    require_exact_keys(policy, MAP_POLICY_KEYS, "tool map policy")
    finite_number(policy["density_window_s"], "policy.density_window_s", 5, 120)
    integer(policy["max_primary_events_per_window"], "policy.max_primary_events_per_window", 0, 20)
    integer(policy["max_hero_events_per_window"], "policy.max_hero_events_per_window", 0, 10)
    integer(
        policy["max_audio_support_events_per_window"],
        "policy.max_audio_support_events_per_window",
        0,
        30,
    )
    finite_number(policy["max_visual_coverage_ratio"], "policy.max_visual_coverage_ratio", 0, 1)
    integer(policy["max_active_visual_layers"], "policy.max_active_visual_layers", 0, 8)
    if integer(policy["max_supporting_effects"], "policy.max_supporting_effects", 0, 1) > 1:
        raise CreativeRouterError("tool map allows more than one supporting effect")
    global_blocks = require_string_list(
        policy["global_block_signals"],
        "policy.global_block_signals",
        allowed=KNOWN_SIGNALS,
    )
    if "hide_weak_cut" not in global_blocks:
        raise CreativeRouterError("tool map must block hide_weak_cut")

    definitions = tool_map.get("signal_definitions")
    if not isinstance(definitions, dict) or set(definitions) != KNOWN_SIGNALS:
        raise CreativeRouterError("tool map signal_definitions must exactly cover known signals")
    for signal, description in definitions.items():
        require_trimmed_text(description, f"signal_definitions.{signal}")

    tools = tool_map.get("tools")
    if not isinstance(tools, list) or not tools:
        raise CreativeRouterError("tool map tools must be a non-empty array")
    tool_ids: list[str] = []
    effect_ids: list[str] = []
    pending_dependencies: list[tuple[str, tuple[str, ...]]] = []
    for tool_index, tool in enumerate(tools):
        label = f"tools[{tool_index}]"
        if not isinstance(tool, dict):
            raise CreativeRouterError(f"{label} must be an object")
        require_exact_keys(tool, TOOL_KEYS, label)
        tool_id = require_trimmed_text(tool["id"], f"{label}.id")
        if not tool_id.replace("_", "a").isalnum() or not tool_id[0].islower():
            raise CreativeRouterError(f"{label}.id is not a safe lowercase identifier")
        tool_ids.append(tool_id)
        for key in ("label", "engine", "license"):
            require_trimmed_text(tool[key], f"{label}.{key}")
        if tool["cost_class"] != "free_local":
            raise CreativeRouterError(f"{label} is not free_local")
        if tool["maturity"] not in TOOL_MATURITIES:
            raise CreativeRouterError(f"{label}.maturity is invalid")
        require_string_list(tool["responsibilities"], f"{label}.responsibilities", allow_empty=False)
        require_string_list(tool["avoid"], f"{label}.avoid", allow_empty=False)
        effects = tool["effects"]
        if not isinstance(effects, list):
            raise CreativeRouterError(f"{label}.effects must be an array")
        for effect_index, effect in enumerate(effects):
            effect_label = f"{label}.effects[{effect_index}]"
            if not isinstance(effect, dict):
                raise CreativeRouterError(f"{effect_label} must be an object")
            require_exact_keys(effect, EFFECT_KEYS, effect_label)
            effect_id = require_trimmed_text(effect["id"], f"{effect_label}.id")
            if not effect_id.replace("_", "a").isalnum() or not effect_id[0].islower():
                raise CreativeRouterError(f"{effect_label}.id is not a safe lowercase identifier")
            effect_ids.append(effect_id)
            require_trimmed_text(effect["label"], f"{effect_label}.label")
            require_trimmed_text(effect["family"], f"{effect_label}.family")
            require_trimmed_text(effect["recipe"], f"{effect_label}.recipe")
            if effect["kind"] not in EFFECT_KINDS:
                raise CreativeRouterError(f"{effect_label}.kind is invalid")
            integer(effect["selection_rank"], f"{effect_label}.selection_rank", 0, 10000)
            for boolean_name in ("hero", "experimental", "detail_safe", "full_frame"):
                if not isinstance(effect[boolean_name], bool):
                    raise CreativeRouterError(f"{effect_label}.{boolean_name} must be boolean")
            if effect["base_intensity"] not in INTENSITIES:
                raise CreativeRouterError(f"{effect_label}.base_intensity is invalid")
            minimum = finite_number(effect["min_duration_s"], f"{effect_label}.min_duration_s", 0.01, 300)
            maximum = finite_number(effect["max_duration_s"], f"{effect_label}.max_duration_s", 0.01, 300)
            if minimum > maximum:
                raise CreativeRouterError(f"{effect_label} duration bounds are inverted")
            finite_number(effect["cooldown_s"], f"{effect_label}.cooldown_s", 0, 300)
            match = effect["match"]
            if not isinstance(match, dict):
                raise CreativeRouterError(f"{effect_label}.match must be an object")
            require_exact_keys(match, MATCH_KEYS, f"{effect_label}.match")
            require_string_list(match["asset_types"], f"{effect_label}.match.asset_types", allowed=ASSET_TYPES, allow_empty=False)
            require_string_list(match["semantic_roles"], f"{effect_label}.match.semantic_roles", allowed=SEMANTIC_ROLES, allow_empty=False)
            require_string_list(match["required_all"], f"{effect_label}.match.required_all", allowed=KNOWN_SIGNALS)
            require_string_list(match["required_any"], f"{effect_label}.match.required_any", allowed=KNOWN_SIGNALS, allow_empty=False)
            require_string_list(match["forbidden_any"], f"{effect_label}.match.forbidden_any", allowed=KNOWN_SIGNALS)
            require_string_list(match["screen_priorities"], f"{effect_label}.match.screen_priorities", allowed=SCREEN_PRIORITIES, allow_empty=False)
            require_string_list(match["presenter_geometries"], f"{effect_label}.match.presenter_geometries", allowed=PRESENTER_GEOMETRIES, allow_empty=False)
            require_string_list(match["desired_intensities"], f"{effect_label}.match.desired_intensities", allowed=INTENSITIES, allow_empty=False)
            require_string_list(match["required_scene_flags"], f"{effect_label}.match.required_scene_flags", allowed=SCENE_FLAGS)
            require_string_list(match["required_audio_flags"], f"{effect_label}.match.required_audio_flags", allowed=AUDIO_FLAGS)
            dependencies = require_string_list(match["required_tools"], f"{effect_label}.match.required_tools")
            pending_dependencies.append((effect_label, dependencies))
            require_string_list(effect["qa"], f"{effect_label}.qa", allowed=QA_TYPES)

    if len(tool_ids) != len(set(tool_ids)):
        raise CreativeRouterError("tool map contains duplicate tool ids")
    if len(effect_ids) != len(set(effect_ids)):
        raise CreativeRouterError("tool map contains duplicate effect ids")
    known_tools = set(tool_ids)
    for label, dependencies in pending_dependencies:
        unknown = sorted(set(dependencies) - known_tools)
        if unknown:
            raise CreativeRouterError(f"{label} has unknown required_tools: {unknown}")


def effect_refs(tool_map: Mapping[str, Any]) -> list[EffectRef]:
    return [
        EffectRef(tool=tool, effect=effect)
        for tool in tool_map["tools"]
        for effect in tool["effects"]
    ]


def effect_index(tool_map: Mapping[str, Any]) -> dict[str, EffectRef]:
    return {ref.effect["id"]: ref for ref in effect_refs(tool_map)}


def normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize arrays whose order is contractually set-like before hashing/routing."""

    result = json.loads(json.dumps(request, ensure_ascii=False))
    result["scene"]["signals"] = sorted(result["scene"]["signals"])
    result["scene"]["scene_flags"] = sorted(result["scene"]["scene_flags"])
    result["available_tools"] = sorted(
        result["available_tools"], key=lambda item: (item["tool_id"], item["status"])
    )
    result["timeline"]["prior_effects"] = sorted(
        result["timeline"]["prior_effects"],
        key=lambda item: (
            item["start_s"],
            item["end_s"],
            item["tool_id"],
            item["effect_id"],
            item["intensity"],
            item["layer_count"],
        ),
    )
    return result


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_request_semantics(request: Mapping[str, Any], tool_map: Mapping[str, Any]) -> None:
    scene = request["scene"]
    timeline = request["timeline"]
    scene_end = float(scene["start_s"]) + float(scene["duration_s"])
    deliverable_duration = float(timeline["deliverable_duration_s"])
    if scene_end > deliverable_duration + 1e-9:
        raise CreativeRouterError("scene interval exceeds timeline.deliverable_duration_s")
    if scene["audio"]["beat_map_available"] and not scene["audio"]["music_present"]:
        raise CreativeRouterError("beat_map_available=true requires music_present=true")
    signals = set(scene["signals"])
    if scene["screen_priority"] == "none" and signals & {"screen_target", "code_or_ui_detail"}:
        raise CreativeRouterError("screen_target/code_or_ui_detail contradict screen_priority=none")
    if "speaker_foreground_layering" in signals and scene["presenter_geometry"] == "none":
        raise CreativeRouterError("speaker_foreground_layering requires a visible presenter")
    if "person_matte_allowed" in scene["scene_flags"] and scene["presenter_geometry"] == "none":
        raise CreativeRouterError("person_matte_allowed requires a visible presenter")

    known_tool_ids = {tool["id"] for tool in tool_map["tools"]}
    available_ids: list[str] = []
    for entry in request["available_tools"]:
        tool_id = entry["tool_id"]
        if tool_id not in known_tool_ids:
            raise CreativeRouterError(f"available_tools contains unknown tool_id: {tool_id!r}")
        if entry["status"] not in TOOL_STATUSES:
            raise CreativeRouterError(f"available_tools status is invalid for {tool_id!r}")
        available_ids.append(tool_id)
    if len(available_ids) != len(set(available_ids)):
        raise CreativeRouterError("available_tools contains duplicate tool ids")

    index = effect_index(tool_map)
    prior_identity: set[tuple[Any, ...]] = set()
    for item_index, prior in enumerate(timeline["prior_effects"]):
        label = f"timeline.prior_effects[{item_index}]"
        ref = index.get(prior["effect_id"])
        if ref is None or ref.tool["id"] != prior["tool_id"]:
            raise CreativeRouterError(f"{label} does not identify a mapped tool/effect pair")
        start = float(prior["start_s"])
        end = float(prior["end_s"])
        if not start < end:
            raise CreativeRouterError(f"{label}.end_s must be greater than start_s")
        if end > deliverable_duration + 1e-9:
            raise CreativeRouterError(f"{label} exceeds deliverable duration")
        if start >= float(scene["start_s"]):
            raise CreativeRouterError(f"{label} is not prior to the routed scene")
        if ref.effect["kind"] == "primary_visual" and prior["layer_count"] < 1:
            raise CreativeRouterError(f"{label} primary visual must declare layer_count >= 1")
        if ref.effect["kind"] == "support_audio" and prior["layer_count"] != 0:
            raise CreativeRouterError(f"{label} audio support must declare layer_count = 0")
        identity = (
            prior["tool_id"],
            prior["effect_id"],
            start,
            end,
            prior["intensity"],
            prior["layer_count"],
        )
        if identity in prior_identity:
            raise CreativeRouterError(f"{label} duplicates an existing prior effect")
        prior_identity.add(identity)


def approval_context(item: ApprovedVisualPlanItem) -> ApprovalContext:
    return ApprovalContext(
        visual_id=item.visual_id,
        section_id=item.section_id,
        meaning_ids=item.meaning_ids,
        asset_type=item.asset_type,
        plan_sha256=item.plan_snapshot.sha256,
        approval_sha256=item.approval_snapshot.sha256,
    )


def validate_binding(request: Mapping[str, Any], approved: ApprovalContext) -> None:
    binding = request["binding"]
    comparisons = {
        "visual_id": approved.visual_id,
        "semantic_plan_sha256": approved.plan_sha256,
        "approval_sha256": approved.approval_sha256,
        "section_id": approved.section_id,
    }
    for field, expected in comparisons.items():
        if binding[field] != expected:
            raise CreativeRouterError(f"binding.{field} does not match the current approved visual")
    if tuple(binding["meaning_ids"]) != approved.meaning_ids:
        raise CreativeRouterError("binding.meaning_ids do not exactly match the approved visual")
    if approved.asset_type not in ASSET_TYPES:
        raise CreativeRouterError(f"approved visual has unsupported asset_type: {approved.asset_type!r}")


def availability_by_tool(request: Mapping[str, Any]) -> dict[str, str]:
    return {entry["tool_id"]: entry["status"] for entry in request["available_tools"]}


def interval_union_duration(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return 0.0
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def compute_density(
    request: Mapping[str, Any],
    tool_map: Mapping[str, Any],
    index: Mapping[str, EffectRef],
) -> DensityState:
    policy = tool_map["policy"]
    scene = request["scene"]
    start = float(scene["start_s"])
    window_s = float(policy["density_window_s"])
    window_start = max(0.0, start - window_s)
    prior_in_window = [
        item
        for item in request["timeline"]["prior_effects"]
        if float(item["end_s"]) > window_start and float(item["start_s"]) < start
    ]
    primary = [item for item in prior_in_window if index[item["effect_id"]].effect["kind"] == "primary_visual"]
    audio = [item for item in prior_in_window if index[item["effect_id"]].effect["kind"] == "support_audio"]
    hero = [item for item in primary if index[item["effect_id"]].effect["hero"]]
    visual_intervals = [
        (max(window_start, float(item["start_s"])), min(start, float(item["end_s"])))
        for item in primary
    ]
    coverage = min(1.0, interval_union_duration(visual_intervals) / window_s)
    active_layers = sum(
        int(item["layer_count"])
        for item in primary
        if float(item["start_s"]) <= start < float(item["end_s"])
    )

    max_primary = int(policy["max_primary_events_per_window"])
    max_hero = int(policy["max_hero_events_per_window"])
    max_audio = int(policy["max_audio_support_events_per_window"])
    max_coverage = float(policy["max_visual_coverage_ratio"])
    max_layers = int(policy["max_active_visual_layers"])
    if scene["content_density"] == "high":
        max_primary = min(max_primary, 3)
        max_coverage = min(max_coverage, 0.35)
    if scene["screen_priority"] == "important":
        max_coverage = min(max_coverage, 0.30)
        max_layers = min(max_layers, 1)

    blocks: list[str] = []
    if len(primary) >= max_primary:
        blocks.append("primary event budget is exhausted")
    if len(hero) >= max_hero:
        blocks.append("hero event budget is exhausted")
    if len(audio) >= max_audio:
        blocks.append("audio support budget is exhausted")
    if coverage >= max_coverage:
        blocks.append("visual coverage budget is exhausted")
    if active_layers >= max_layers:
        blocks.append("active visual layer budget is exhausted")
    return DensityState(
        window_start_s=window_start,
        window_end_s=start,
        primary_events_before=len(primary),
        hero_events_before=len(hero),
        audio_support_events_before=len(audio),
        visual_coverage_ratio_before=coverage,
        active_visual_layers_before=active_layers,
        max_primary_events=max_primary,
        max_hero_events=max_hero,
        max_audio_support_events=max_audio,
        max_visual_coverage_ratio=max_coverage,
        max_active_visual_layers=max_layers,
        max_supporting_effects=int(policy["max_supporting_effects"]),
        window_s=window_s,
        blocks=tuple(blocks),
    )


def candidate_rejection(
    ref: EffectRef,
    request: Mapping[str, Any],
    approved: ApprovalContext,
    availability: Mapping[str, str],
    density: DensityState,
    index: Mapping[str, EffectRef],
) -> CandidateResult | None:
    effect = ref.effect
    match = effect["match"]
    scene = request["scene"]
    signals = set(scene["signals"])

    if approved.asset_type not in match["asset_types"] or scene["semantic_role"] not in match["semantic_roles"]:
        return None
    all_required = set(match["required_all"])
    any_required = set(match["required_any"])
    matched = tuple(sorted((all_required | any_required) & signals))
    if not all_required.issubset(signals):
        missing = sorted(all_required - signals)
        return CandidateResult(ref, matched, False, "missing_required_signal", f"missing required signals: {missing}")
    if not (any_required & signals):
        return CandidateResult(
            ref,
            matched,
            False,
            "semantic_signal_mismatch",
            "none of the effect's approved semantic signals is present",
        )
    forbidden = sorted(set(match["forbidden_any"]) & signals)
    if forbidden:
        return CandidateResult(ref, matched, False, "forbidden_signal", f"forbidden signals are present: {forbidden}")
    if scene["screen_priority"] not in match["screen_priorities"]:
        return CandidateResult(ref, matched, False, "screen_priority_mismatch", "effect would not preserve the declared screen priority")
    if scene["presenter_geometry"] not in match["presenter_geometries"]:
        return CandidateResult(ref, matched, False, "presenter_geometry_mismatch", "effect is incompatible with the declared presenter geometry")
    if scene["desired_intensity"] not in match["desired_intensities"]:
        return CandidateResult(ref, matched, False, "intensity_mismatch", "effect intensity is outside the approved scene intensity")

    scene_flags = set(scene["scene_flags"])
    missing_scene_flags = sorted(set(match["required_scene_flags"]) - scene_flags)
    if missing_scene_flags:
        return CandidateResult(ref, matched, False, "missing_scene_flag", f"missing explicit scene flags: {missing_scene_flags}")
    missing_audio_flags = sorted(
        name for name in match["required_audio_flags"] if scene["audio"].get(name) is not True
    )
    if missing_audio_flags:
        return CandidateResult(ref, matched, False, "missing_audio_condition", f"required audio conditions are false: {missing_audio_flags}")

    tool_status = availability.get(ref.tool["id"], "unavailable")
    if tool_status == "unavailable":
        return CandidateResult(ref, matched, False, "tool_unavailable", "tool was not explicitly reported ready")
    if tool_status == "experimental" and "experimental_effect_allowed" not in scene_flags:
        return CandidateResult(ref, matched, False, "experimental_not_approved", "tool is experimental for this machine/project")
    if (ref.tool["maturity"] == "experimental" or effect["experimental"]) and "experimental_effect_allowed" not in scene_flags:
        return CandidateResult(ref, matched, False, "experimental_not_approved", "experimental effect was not explicitly allowed")
    missing_tools = sorted(
        tool_id
        for tool_id in match["required_tools"]
        if availability.get(tool_id, "unavailable") != "ready"
    )
    if missing_tools:
        return CandidateResult(ref, matched, False, "dependency_unavailable", f"required tools are not ready: {missing_tools}")

    if effect["full_frame"] and scene["screen_priority"] == "important" and "screen_full_frame_visual_approved" not in scene_flags:
        return CandidateResult(ref, matched, False, "important_screen_block", "full-frame effect lacks explicit approval to cover an important screen")
    if not effect["detail_safe"] and (scene["screen_priority"] == "important" or "code_or_ui_detail" in signals):
        return CandidateResult(ref, matched, False, "detail_legibility_block", "effect may damage protected source detail")
    if effect["hero"] and scene["content_density"] == "high" and not effect["detail_safe"]:
        return CandidateResult(ref, matched, False, "dense_scene_block", "unsafe hero effect is blocked in a high-density scene")
    if float(scene["duration_s"]) + 1e-9 < float(effect["min_duration_s"]):
        return CandidateResult(ref, matched, False, "scene_too_short", "scene is shorter than the effect's readable minimum")

    prior = request["timeline"]["prior_effects"]
    start = float(scene["start_s"])
    same_family = [
        item
        for item in prior
        if index[item["effect_id"]].effect["family"] == effect["family"]
        and float(item["end_s"]) > density.window_start_s
    ]
    if same_family:
        latest_end = max(float(item["end_s"]) for item in same_family)
        remaining = latest_end + float(effect["cooldown_s"]) - start
        if remaining > 1e-9:
            return CandidateResult(ref, matched, False, "family_cooldown", f"effect family cooldown has {remaining:.3f}s remaining")

    if effect["kind"] == "primary_visual":
        if density.primary_events_before >= density.max_primary_events:
            return CandidateResult(ref, matched, False, "primary_density_limit", "primary effect density limit is exhausted")
        if effect["hero"] and density.hero_events_before >= density.max_hero_events:
            return CandidateResult(ref, matched, False, "hero_density_limit", "hero effect density limit is exhausted")
        if density.active_visual_layers_before + 1 > density.max_active_visual_layers:
            return CandidateResult(ref, matched, False, "layer_density_limit", "another visual layer would exceed the active-layer limit")
        duration = min(float(scene["duration_s"]), float(effect["max_duration_s"]))
        prior_visual_intervals = [
            (
                max(density.window_start_s, float(item["start_s"])),
                min(start, float(item["end_s"])),
            )
            for item in prior
            if index[item["effect_id"]].effect["kind"] == "primary_visual"
            and float(item["end_s"]) > density.window_start_s
            and float(item["start_s"]) < start
        ]
        projected = interval_union_duration([*prior_visual_intervals, (start, start + duration)])
        projected_ratio = min(1.0, projected / density.window_s)
        if projected_ratio > density.max_visual_coverage_ratio + 1e-9:
            return CandidateResult(ref, matched, False, "coverage_density_limit", f"projected visual coverage {projected_ratio:.3f} exceeds {density.max_visual_coverage_ratio:.3f}")
    elif density.audio_support_events_before >= density.max_audio_support_events:
        return CandidateResult(ref, matched, False, "audio_density_limit", "audio support density limit is exhausted")

    return CandidateResult(ref, matched, True)


def selected_effect(result: CandidateResult, request: Mapping[str, Any]) -> dict[str, Any]:
    effect = result.ref.effect
    scene_duration = float(request["scene"]["duration_s"])
    duration = min(max(scene_duration, float(effect["min_duration_s"])), float(effect["max_duration_s"]))
    matched = list(result.matched_signals)
    return {
        "tool_id": result.ref.tool["id"],
        "effect_id": effect["id"],
        "label": effect["label"],
        "kind": effect["kind"],
        "family": effect["family"],
        "intensity": effect["base_intensity"],
        "duration_s": rounded(duration),
        "full_frame": effect["full_frame"],
        "matched_signals": matched,
        "recipe": effect["recipe"],
        "rationale": (
            f"The approved {request['scene']['semantic_role']} scene carries "
            f"{', '.join(matched)}; {effect['label']} is the first eligible rule in the "
            "deterministic tool map after readiness, legibility, cooldown, and density checks."
        ),
    }


def rejection_json(result: CandidateResult) -> dict[str, str]:
    assert result.rejection_code is not None and result.rejection_reason is not None
    return {
        "tool_id": result.ref.tool["id"],
        "effect_id": result.ref.effect["id"],
        "code": result.rejection_code,
        "reason": result.rejection_reason,
    }


def decision_guardrails(request: Mapping[str, Any], decision: str) -> list[str]:
    values = [
        "Use only the approved visual text, purpose, section, and meaning IDs; the router does not authorize new claims.",
        "Never use an effect to hide an editorially weak cut; repair or re-approve the edit instead.",
        "At most one primary visual and one supporting audio accent may be emitted for this scene.",
    ]
    if decision == "effect":
        values.append("A new or materially changed visual must pass a 3–4-frame visual preview sheet before full-program use.")
    if request["scene"]["screen_priority"] == "important":
        values.append("The declared important screen region, labels, code, and controls must remain legible at normal playback speed.")
    return sorted(set(values))


def choose_none_reason(
    approved: ApprovalContext,
    request: Mapping[str, Any],
    rejections: Sequence[CandidateResult],
    global_blocks: Sequence[str],
) -> str:
    if approved.asset_type == "none":
        return "The approved visual item has asset_type=none, so no creative effect is authorized."
    if global_blocks:
        return f"Routing is blocked by global anti-effect signals: {list(global_blocks)}."
    if not request["scene"]["signals"]:
        return "No approved semantic signal justifies a creative effect in this scene."
    codes = {item.rejection_code for item in rejections}
    density_codes = {
        "primary_density_limit",
        "hero_density_limit",
        "layer_density_limit",
        "coverage_density_limit",
        "family_cooldown",
        "audio_density_limit",
    }
    if codes & density_codes:
        return "Purpose-fit effects exist, but the anti-overeffect density/cooldown budget is exhausted."
    if "tool_unavailable" in codes or "dependency_unavailable" in codes:
        return "Purpose-fit effects exist, but no eligible local tool and dependency set was explicitly reported ready."
    return "No mapped effect safely matches the approved semantic role, visual context, intensity, and duration."


def route_request(
    request: Mapping[str, Any],
    tool_map: Mapping[str, Any],
    approved: ApprovalContext,
    hashes: RouterHashes,
) -> dict[str, Any]:
    validate_tool_map(tool_map)
    validate_request_semantics(request, tool_map)
    validate_binding(request, approved)
    index = effect_index(tool_map)
    density = compute_density(request, tool_map, index)
    availability = availability_by_tool(request)
    signals = set(request["scene"]["signals"])
    global_blocks = sorted(signals & set(tool_map["policy"]["global_block_signals"]))

    evaluated: list[CandidateResult] = []
    if approved.asset_type != "none" and not global_blocks:
        for ref in effect_refs(tool_map):
            candidate = candidate_rejection(
                ref,
                request,
                approved,
                availability,
                density,
                index,
            )
            if candidate is not None:
                evaluated.append(candidate)

    primary_candidates = sorted(
        (item for item in evaluated if item.eligible and item.ref.effect["kind"] == "primary_visual"),
        key=lambda item: (
            item.ref.effect["selection_rank"],
            item.ref.tool["id"],
            item.ref.effect["id"],
        ),
    )
    primary = primary_candidates[0] if primary_candidates else None
    supporting: list[CandidateResult] = []
    if primary is not None and density.max_supporting_effects:
        supporting = sorted(
            (item for item in evaluated if item.eligible and item.ref.effect["kind"] == "support_audio"),
            key=lambda item: (
                item.ref.effect["selection_rank"],
                item.ref.tool["id"],
                item.ref.effect["id"],
            ),
        )[: density.max_supporting_effects]

    selected_ids = {
        (item.ref.tool["id"], item.ref.effect["id"])
        for item in ([primary] if primary is not None else []) + supporting
    }
    rejected = sorted(
        (
            item
            for item in evaluated
            if not item.eligible
            or (item.ref.tool["id"], item.ref.effect["id"]) not in selected_ids
        ),
        key=lambda item: (
            item.ref.effect["selection_rank"],
            item.ref.tool["id"],
            item.ref.effect["id"],
            item.rejection_code or "eligible_but_lower_rank",
        ),
    )
    rejection_payload: list[dict[str, str]] = []
    for item in rejected:
        if item.eligible:
            rejection_payload.append(
                {
                    "tool_id": item.ref.tool["id"],
                    "effect_id": item.ref.effect["id"],
                    "code": "lower_ranked_match",
                    "reason": "eligible but a more specific earlier rule was selected",
                }
            )
        else:
            rejection_payload.append(rejection_json(item))

    decision = "effect" if primary is not None else "none"
    selected = [item for item in ([primary] if primary is not None else []) + supporting]
    required_qa = sorted({qa for item in selected for qa in item.ref.effect["qa"]})
    none_reason = None
    if decision == "none":
        primary_evaluated = [
            item for item in evaluated if item.ref.effect["kind"] == "primary_visual"
        ]
        none_reason = choose_none_reason(
            approved,
            request,
            primary_evaluated,
            global_blocks,
        )
    output = {
        "version": 1,
        "router_version": ROUTER_VERSION,
        "map_id": tool_map["map_id"],
        "decision": decision,
        "none_reason": none_reason,
        "primary_effect": selected_effect(primary, request) if primary is not None else None,
        "supporting_effects": [selected_effect(item, request) for item in supporting],
        "density": density.to_json(),
        "rejected_candidates": rejection_payload[:100],
        "required_qa": required_qa,
        "guardrails": decision_guardrails(request, decision),
        "provenance": {
            "visual_id": approved.visual_id,
            "section_id": approved.section_id,
            "meaning_ids": list(approved.meaning_ids),
            "semantic_plan_sha256": approved.plan_sha256,
            "approval_sha256": approved.approval_sha256,
            "feature_input_sha256": hashes.feature_input_sha256,
            "tool_map_sha256": hashes.tool_map_sha256,
            "input_schema_sha256": hashes.input_schema_sha256,
            "output_schema_sha256": hashes.output_schema_sha256,
            "router_sha256": hashes.router_sha256,
        },
    }
    return output


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def loaded_contracts() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tool_map = load_json_object(TOOL_MAP_PATH, "creative tool map")
    input_schema = load_json_object(INPUT_SCHEMA_PATH, "creative router input schema")
    output_schema = load_json_object(OUTPUT_SCHEMA_PATH, "creative decision schema")
    validate_tool_map(tool_map)
    return tool_map, input_schema, output_schema


def validate_registry_report(report: Mapping[str, Any]) -> None:
    """Validate the security-critical subset of creative_tool_registry JSON."""

    require_exact_keys(report, REGISTRY_ROOT_KEYS, "creative tool registry report")
    if report.get("version") != 1 or report.get("type") != "sprut_creative_tool_registry":
        raise CreativeRouterError("creative tool registry must be canonical v1")
    if report.get("network_calls_made") != 0:
        raise CreativeRouterError("creative tool registry reports network activity")
    if report.get("paid_api_allowlist") != ["elevenlabs"]:
        raise CreativeRouterError("creative tool registry paid API boundary is not canonical")
    engines = report.get("engines")
    if not isinstance(engines, dict) or not engines:
        raise CreativeRouterError("creative tool registry engines must be a non-empty object")
    for engine_id, engine in engines.items():
        require_trimmed_text(engine_id, "creative registry engine id")
        if not isinstance(engine, dict):
            raise CreativeRouterError(f"creative registry engine {engine_id!r} must be an object")
        if engine.get("local_only") is not True or engine.get("paid_api") is not False:
            raise CreativeRouterError(
                f"creative registry engine {engine_id!r} is not verified local/no-paid-API"
            )
        require_trimmed_text(engine.get("status"), f"creative registry {engine_id}.status")
        capabilities = engine.get("capabilities")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item for item in capabilities
        ):
            raise CreativeRouterError(
                f"creative registry {engine_id}.capabilities must be a string array"
            )
        if len(capabilities) != len(set(capabilities)):
            raise CreativeRouterError(
                f"creative registry {engine_id}.capabilities contains duplicates"
            )


def invoke_registry() -> dict[str, Any]:
    if not CREATIVE_REGISTRY_PATH.is_file():
        raise CreativeRouterError(f"creative tool registry is missing: {CREATIVE_REGISTRY_PATH}")
    result = subprocess.run(
        [sys.executable, str(CREATIVE_REGISTRY_PATH), "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        raise CreativeRouterError(
            f"creative tool registry failed ({result.returncode}): {details or 'no diagnostics'}"
        )
    try:
        report = json.loads(result.stdout, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CreativeRouterError(f"creative tool registry emitted invalid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise CreativeRouterError("creative tool registry output must be an object")
    validate_registry_report(report)
    return report


def registry_engine_ready(
    report: Mapping[str, Any],
    engine_id: str,
    *,
    capabilities: Sequence[str] = (),
) -> bool:
    engine = report["engines"].get(engine_id)
    if not isinstance(engine, dict) or engine.get("status") != "ready":
        return False
    if engine.get("local_only") is not True or engine.get("paid_api") is not False:
        return False
    return set(capabilities).issubset(set(engine.get("capabilities", [])))


def registry_filter_ready(report: Mapping[str, Any], engine_id: str, filter_name: str) -> bool:
    engine = report["engines"].get(engine_id)
    return bool(
        isinstance(engine, dict)
        and engine.get("status") == "ready"
        and engine.get("local_only") is True
        and engine.get("paid_api") is False
        and filter_name in engine.get("filters", [])
    )


def bundled_motion_cards_ready() -> bool:
    if not MOTION_CARD_PATH.is_file():
        return False
    doctor = SKILL_ROOT / "scripts" / "doctor.py"
    if doctor.is_file():
        result = subprocess.run(
            [sys.executable, str(doctor), "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                report = json.loads(result.stdout)
                runtime_path = report.get("required", {}).get("python_runtime", {}).get("path")
            except json.JSONDecodeError:
                runtime_path = None
            if isinstance(runtime_path, str) and Path(runtime_path).is_file():
                result = subprocess.run(
                    [runtime_path, "-c", "from PIL import Image; import numpy"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                return result.returncode == 0
    result = subprocess.run(
        [sys.executable, "-c", "from PIL import Image; import numpy"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def bundled_creative_sfx_ready(tool_map: Mapping[str, Any]) -> bool:
    if not CREATIVE_SFX_PATH.is_file():
        return False
    result = subprocess.run(
        [sys.executable, str(CREATIVE_SFX_PATH), "--describe-json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return False
    try:
        discovery = json.loads(result.stdout, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, CreativeRouterError):
        return False
    if not isinstance(discovery, dict):
        return False
    availability = discovery.get("availability")
    if not isinstance(availability, dict) or any(
        availability.get(field) is not False
        for field in (
            "network_required",
            "paid_api_required",
            "external_audio_assets_required",
        )
    ):
        return False
    discovered_ids = {
        item.get("id")
        for item in discovery.get("presets", [])
        if isinstance(item, dict)
    }
    map_tool = next(
        (tool for tool in tool_map["tools"] if tool["id"] == "sprut_sfx"),
        None,
    )
    mapped_ids = {effect["id"] for effect in map_tool["effects"]} if map_tool else set()
    return bool(discovered_ids) and discovered_ids == mapped_ids


def registry_availability(
    report: Mapping[str, Any],
    tool_map: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate the offline registry into the router's closed tool-id/status set.

    Missing, limited, on-demand, or unknown engines become ``unavailable``.
    Engines intentionally held behind a pilot/release gate become
    ``experimental`` even when their local package is present.
    """

    validate_registry_report(report)
    status: dict[str, str] = {
        tool["id"]: "unavailable" for tool in tool_map["tools"]
    }

    if bundled_motion_cards_ready():
        status["motion_cards"] = "ready"
    if registry_engine_ready(
        report,
        "gsap_motion",
        capabilities=("kinetic_typography", "svg_draw", "svg_morph"),
    ):
        status["hyperframes_gsap"] = "ready"
    if registry_engine_ready(report, "browser_fx", capabilities=("rough_annotation",)):
        status["rough_notation"] = "ready"
    if registry_engine_ready(report, "manim", capabilities=("technical_diagram",)):
        status["manim"] = "ready"
    if registry_engine_ready(
        report,
        "shot_aware_camera",
        capabilities=("shot_detection", "subpixel_camera"),
    ):
        status["virtual_camera"] = "ready"
    if registry_engine_ready(
        report,
        "browser_fx",
        capabilities=("gpu_2d", "gpu_particles", "shockwave"),
    ):
        status["pixijs_filters"] = "ready"
    if registry_engine_ready(report, "browser_fx", capabilities=("lottie_playback",)):
        status["lottie_web"] = "ready"
    if registry_engine_ready(report, "frei0r_recipes", capabilities=("curated_frei0r_fx",)):
        status["ffmpeg_frei0r"] = "ready"
    if registry_engine_ready(report, "rhythm_analysis", capabilities=("rhythm_map", "onset_map")):
        status["librosa_beat"] = "ready"
    if registry_engine_ready(
        report,
        "apple_vision",
        capabilities=("presenter_tracking", "person_matte"),
    ):
        status["apple_vision"] = "ready"
    if bundled_creative_sfx_ready(tool_map):
        status["sprut_sfx"] = "ready"

    # A runtime alone is not callable. Require the checked-in, discovery-safe
    # source adapter before exposing browser/GSAP effect families as ready.
    if not registry_engine_ready(report, "gsap_creative_adapter"):
        status["hyperframes_gsap"] = "unavailable"
    if not registry_engine_ready(report, "browser_creative_adapter"):
        for tool_id in ("rough_notation", "pixijs_filters", "lottie_web"):
            status[tool_id] = "unavailable"

    # Installed source/runtime is not sufficient for these effects to become a
    # default.  The router will additionally require the explicit scene flag.
    if registry_engine_ready(report, "browser_fx", capabilities=("procedural_3d",)) and registry_engine_ready(report, "browser_creative_adapter"):
        status["threejs"] = "experimental"
    # Source code or a raw FFmpeg filter is not an editorially callable
    # transition.  Keep both families unavailable until a dedicated,
    # seek-safe compositor and exact-boundary QA adapter are registered.
    # This prevents an explicit experimental flag from bypassing the missing
    # renderer contract.
    if registry_engine_ready(report, "depth_parallax"):
        status["depth_anything_small"] = "experimental"
    if registry_engine_ready(report, "gmic"):
        status["gmic"] = "experimental"
    if registry_engine_ready(report, "zzfx"):
        status["zzfx"] = "ready"

    available_tools = [
        {"tool_id": tool_id, "status": tool_status}
        for tool_id, tool_status in sorted(status.items())
    ]
    counts = {
        value: sum(1 for item in available_tools if item["status"] == value)
        for value in ("ready", "experimental", "unavailable")
    }
    return {
        "version": 1,
        "type": "sprut_creative_router_availability",
        "registry_sha256": sha256_bytes(canonical_json_bytes(report)),
        "network_calls_made": 0,
        "available_tools": available_tools,
        "status_counts": counts,
    }


def sample_request(
    approved: ApprovalContext,
    *,
    signals: Sequence[str] = ("keyword_emphasis",),
    prior_effects: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "version": 1,
        "binding": {
            "visual_id": approved.visual_id,
            "semantic_plan_sha256": approved.plan_sha256,
            "approval_sha256": approved.approval_sha256,
            "section_id": approved.section_id,
            "meaning_ids": list(approved.meaning_ids),
        },
        "scene": {
            "start_s": 20.0,
            "duration_s": 3.0,
            "semantic_role": "hook",
            "signals": list(signals),
            "content_density": "medium",
            "screen_priority": "none",
            "presenter_geometry": "rectangle",
            "desired_intensity": "medium",
            "scene_flags": [],
            "audio": {
                "speech_present": true_value(),
                "music_present": False,
                "beat_map_available": False,
                "sfx_allowed": True,
            },
        },
        "timeline": {
            "deliverable_duration_s": 60.0,
            "prior_effects": [dict(item) for item in prior_effects],
        },
        "available_tools": [
            {"tool_id": "motion_cards", "status": "ready"},
            {"tool_id": "hyperframes_gsap", "status": "ready"},
            {"tool_id": "sprut_sfx", "status": "ready"},
        ],
    }


def true_value() -> bool:
    """Keep JSON fixture booleans visually distinct from numeric literals."""

    return True


def self_test() -> dict[str, Any]:
    tool_map, input_schema, output_schema = loaded_contracts()
    approved = ApprovalContext(
        visual_id="visual-self-test",
        section_id="section-self-test",
        meaning_ids=("meaning-self-test",),
        asset_type="title",
        plan_sha256="1" * 64,
        approval_sha256="2" * 64,
    )
    hashes = RouterHashes(
        feature_input_sha256="3" * 64,
        tool_map_sha256=file_sha256(TOOL_MAP_PATH),
        input_schema_sha256=file_sha256(INPUT_SCHEMA_PATH),
        output_schema_sha256=file_sha256(OUTPUT_SCHEMA_PATH),
        router_sha256=file_sha256(SCRIPT_PATH),
    )
    checks: list[str] = []

    request = normalize_request(sample_request(approved))
    validate_with_schema(request, input_schema, "self-test input")
    first = route_request(request, tool_map, approved, hashes)
    validate_with_schema(first, output_schema, "self-test decision")
    if first["decision"] != "effect" or first["primary_effect"]["effect_id"] != "kinetic_keyword":
        raise CreativeRouterError("self-test expected kinetic_keyword")
    if [item["effect_id"] for item in first["supporting_effects"]] != ["semantic_hit"]:
        raise CreativeRouterError("self-test expected one restrained semantic_hit")
    checks.append("purpose-fit visual plus one audio support")

    repeated = route_request(request, tool_map, approved, hashes)
    if canonical_json_bytes(first) != canonical_json_bytes(repeated):
        raise CreativeRouterError("self-test routing is not deterministic")
    checks.append("byte-stable deterministic routing")

    empty = normalize_request(sample_request(approved, signals=()))
    validate_with_schema(empty, input_schema, "self-test no-signal input")
    no_effect = route_request(empty, tool_map, approved, hashes)
    validate_with_schema(no_effect, output_schema, "self-test none decision")
    if no_effect["decision"] != "none" or not no_effect["none_reason"]:
        raise CreativeRouterError("self-test expected an explained none decision")
    checks.append("none with an explicit reason")

    blocked = normalize_request(sample_request(approved, signals=("hide_weak_cut",)))
    validate_with_schema(blocked, input_schema, "self-test anti-effect input")
    blocked_result = route_request(blocked, tool_map, approved, hashes)
    validate_with_schema(blocked_result, output_schema, "self-test anti-effect decision")
    if blocked_result["decision"] != "none" or "hide_weak_cut" not in blocked_result["none_reason"]:
        raise CreativeRouterError("self-test failed the hide_weak_cut guard")
    checks.append("weak-cut concealment guard")

    experimental_approved = ApprovalContext(
        visual_id=approved.visual_id,
        section_id=approved.section_id,
        meaning_ids=approved.meaning_ids,
        asset_type="b_roll",
        plan_sha256=approved.plan_sha256,
        approval_sha256=approved.approval_sha256,
    )
    experimental = sample_request(
        experimental_approved,
        signals=("spatial_depth", "keyword_emphasis"),
    )
    experimental["scene"]["desired_intensity"] = "hero"
    experimental["available_tools"] = [
        {"tool_id": "depth_anything_small", "status": "experimental"}
    ]
    experimental = normalize_request(experimental)
    validate_with_schema(experimental, input_schema, "self-test experimental input")
    experimental_result = route_request(
        experimental,
        tool_map,
        experimental_approved,
        hashes,
    )
    validate_with_schema(
        experimental_result,
        output_schema,
        "self-test experimental decision",
    )
    if experimental_result["decision"] != "none":
        raise CreativeRouterError("self-test selected an experimental engine by default")
    checks.append("experimental and unavailable engines are not defaults")

    return {
        "status": "PASS",
        "router_version": ROUTER_VERSION,
        "map_id": tool_map["map_id"],
        "checks": checks,
        "network_calls_made": 0,
    }


def run_route(args: argparse.Namespace) -> int:
    tool_map, input_schema, output_schema = loaded_contracts()
    edit_dir = canonical_edit_dir(args.edit_dir)
    input_path = path_under_edit(edit_dir, args.input, "creative router input")
    if args.input.is_symlink() or input_path.is_symlink():
        raise CreativeRouterError("creative router input must not be a symlink")
    request = load_json_object(input_path, "creative router input")
    validate_with_schema(request, input_schema, "creative router input")
    normalized = normalize_request(request)
    validate_request_semantics(normalized, tool_map)

    require_asset_gate(edit_dir)
    approved_item = load_approved_visual_plan_item(
        edit_dir,
        normalized["binding"]["visual_id"],
        require_gate3=False,
    )
    approved = approval_context(approved_item)
    validate_binding(normalized, approved)

    hashes = RouterHashes(
        feature_input_sha256=sha256_bytes(canonical_json_bytes(normalized)),
        tool_map_sha256=file_sha256(TOOL_MAP_PATH),
        input_schema_sha256=file_sha256(INPUT_SCHEMA_PATH),
        output_schema_sha256=file_sha256(OUTPUT_SCHEMA_PATH),
        router_sha256=file_sha256(SCRIPT_PATH),
    )
    decision = route_request(normalized, tool_map, approved, hashes)
    validate_with_schema(decision, output_schema, "creative router decision")

    if args.output is None:
        print(json.dumps(decision, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    output = path_under_edit(edit_dir, args.output, "creative router output")
    if output.suffix.lower() != ".json":
        raise CreativeRouterError("creative router output must use .json")
    if output == input_path or output in {TOOL_MAP_PATH, INPUT_SCHEMA_PATH, OUTPUT_SCHEMA_PATH, SCRIPT_PATH}:
        raise CreativeRouterError("creative router output cannot replace an input or implementation file")
    if output.is_symlink():
        raise CreativeRouterError("creative router output must not be a symlink")
    if output.exists() and not args.force:
        raise CreativeRouterError(f"output exists; use --force to replace: {output}")
    atomic_write_json(output, decision)
    print(f"creative decision: {output}")
    print(f"decision: {decision['decision']}")
    if decision["decision"] == "effect":
        print(
            "primary: "
            f"{decision['primary_effect']['tool_id']}/{decision['primary_effect']['effect_id']}"
        )
    else:
        print(f"reason: {decision['none_reason']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route one approval-bound SPRUT visual to a purpose-fit local creative tool"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    route_parser = subparsers.add_parser("route", help="route one strict scene feature document")
    route_parser.add_argument("--edit-dir", type=Path, required=True)
    route_parser.add_argument("--input", type=Path, required=True)
    route_parser.add_argument("--output", type=Path)
    route_parser.add_argument("--force", action="store_true")

    subparsers.add_parser("tools", help="print the validated creative tool map as JSON")
    availability_parser = subparsers.add_parser(
        "availability",
        help="translate the offline creative registry into router available_tools JSON",
    )
    availability_parser.add_argument(
        "--registry-report",
        type=Path,
        help="use an existing creative_tool_registry.py --json report instead of invoking it",
    )
    availability_parser.add_argument(
        "--array-only",
        action="store_true",
        help="emit only the available_tools array accepted by the router input schema",
    )
    subparsers.add_parser("self-test", help="run deterministic in-memory contract checks")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "route":
        return run_route(args)
    if args.command == "tools":
        tool_map, _input_schema, _output_schema = loaded_contracts()
        print(json.dumps(tool_map, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "availability":
        tool_map, _input_schema, _output_schema = loaded_contracts()
        if args.registry_report is None:
            report = invoke_registry()
        else:
            report = load_json_object(args.registry_report, "creative tool registry report")
            validate_registry_report(report)
        availability = registry_availability(report, tool_map)
        payload: Any = (
            availability["available_tools"] if args.array_only else availability
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "self-test":
        print(json.dumps(self_test(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    raise CreativeRouterError(f"unknown command: {args.command!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssetGateError,
        CreativeRouterError,
        OSError,
        ValueError,
        VisualProvenanceError,
    ) as exc:
        print(f"creative_tool_router: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
