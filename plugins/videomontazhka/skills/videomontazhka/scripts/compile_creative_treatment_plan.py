#!/usr/bin/env python3
"""Compile approval-bound creative decisions into one deterministic plan.

This compiler does not select effects and does not render assets.  Production
mode requires the canonical project ``edit/`` directory, passes the existing
SPRUT asset gate before any filesystem mutation, accepts only explicit
decision files under that tree, and publishes exactly one fixed output:
``edit/creative_treatment_plan.json``.

Discovery modes are read-only and project-free::

    compile_creative_treatment_plan.py --describe-json
    compile_creative_treatment_plan.py --self-test

Production example::

    compile_creative_treatment_plan.py --edit-dir /project/edit \
      --decision creative/visual-a.decision.json \
      --decision creative/visual-b.decision.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from creative_tool_router import KNOWN_SIGNALS, effect_refs, validate_tool_map
from schema_check import SchemaDefinitionError, Validator


COMPILER_VERSION = "sprut-creative-treatment-compiler-1"
OUTPUT_NAME = "creative_treatment_plan.json"
TIMELINE_ORDER = "narrative_section_index_then_visual_plan_index"
MAX_DECISIONS = 1000
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024

SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parent.parent
ROUTER_PATH = SCRIPT_PATH.parent / "creative_tool_router.py"
TOOL_MAP_PATH = SKILL_ROOT / "assets" / "creative-tool-router-map.v1.json"
ROUTER_INPUT_SCHEMA_PATH = SKILL_ROOT / "schemas" / "creative_router_input.schema.json"
DECISION_SCHEMA_PATH = SKILL_ROOT / "schemas" / "creative_decision.schema.json"
TREATMENT_SCHEMA_PATH = SKILL_ROOT / "schemas" / "creative_treatment_plan.schema.json"


class TreatmentCompileError(RuntimeError):
    """A fail-closed compiler contract or input error."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    sha256: str


@dataclass(frozen=True)
class VisualContract:
    visual_id: str
    section_id: str
    meaning_ids: tuple[str, ...]
    asset_type: str
    purpose: str
    treatment: str
    approved_text: str | None
    section_index: int
    visual_plan_index: int


@dataclass(frozen=True)
class DecisionRecord:
    path: Path
    relative_path: str
    sha256: str
    value: Mapping[str, Any]


@dataclass(frozen=True)
class ControlSet:
    tool_map: Mapping[str, Any]
    decision_schema: Mapping[str, Any]
    treatment_schema: Mapping[str, Any]
    snapshots: Mapping[str, FileSnapshot]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise TreatmentCompileError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def load_json_snapshot(path: Path, label: str) -> tuple[dict[str, Any], FileSnapshot]:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or not resolved.is_file():
        raise TreatmentCompileError(f"{label} must be a regular non-symlink file: {resolved}")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise TreatmentCompileError(
            f"{label} size must be between 1 and {MAX_JSON_BYTES} bytes: {resolved}"
        )
    try:
        raw = resolved.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TreatmentCompileError(f"cannot load {label} {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise TreatmentCompileError(f"{label} must be a JSON object: {resolved}")
    return value, FileSnapshot(resolved, sha256_bytes(raw))


def validate_schema(instance: Any, schema: Mapping[str, Any], label: str) -> None:
    try:
        errors = Validator(dict(schema)).validate(instance)
    except SchemaDefinitionError as exc:
        raise TreatmentCompileError(f"{label} schema is invalid: {exc}") from exc
    if errors:
        rendered = "\n".join(f"- {error.render()}" for error in errors[:50])
        suffix = "\n- additional errors omitted" if len(errors) > 50 else ""
        raise TreatmentCompileError(f"{label} failed schema validation:\n{rendered}{suffix}")


def trimmed_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TreatmentCompileError(f"{label} must be a trimmed non-empty string")
    return value


def string_array(value: Any, label: str, *, maximum: int = 32) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise TreatmentCompileError(
            f"{label} must be a non-empty array with at most {maximum} items"
        )
    result = tuple(trimmed_text(item, f"{label}[{index}]") for index, item in enumerate(value))
    if len(result) != len(set(result)):
        raise TreatmentCompileError(f"{label} contains duplicates")
    return result


def safe_relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def control_relative_path(path: Path) -> str:
    return path.resolve().relative_to(SKILL_ROOT.resolve()).as_posix()


def checked_decision_path(edit_dir: Path, raw: Path) -> Path:
    candidate = raw.expanduser() if raw.is_absolute() else edit_dir / raw
    resolved = path_under_edit(edit_dir, candidate, "creative decision")
    relative = resolved.relative_to(edit_dir)
    cursor = edit_dir
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TreatmentCompileError(
                f"creative decision path contains a symlink component: {cursor}"
            )
    if not resolved.is_file():
        raise TreatmentCompileError(f"creative decision not found: {resolved}")
    if resolved.suffix.lower() != ".json":
        raise TreatmentCompileError(f"creative decision must use .json: {resolved}")
    return resolved


def load_controls() -> ControlSet:
    values: dict[str, Mapping[str, Any]] = {}
    snapshots: dict[str, FileSnapshot] = {}
    for name, path, label in (
        ("router_map", TOOL_MAP_PATH, "creative router map"),
        ("router_input_schema", ROUTER_INPUT_SCHEMA_PATH, "creative router input schema"),
        ("decision_schema", DECISION_SCHEMA_PATH, "creative decision schema"),
        ("treatment_schema", TREATMENT_SCHEMA_PATH, "creative treatment schema"),
    ):
        value, snapshot = load_json_snapshot(path, label)
        values[name] = value
        snapshots[name] = snapshot
    for name, path in (("router", ROUTER_PATH), ("compiler", SCRIPT_PATH)):
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_file():
            raise TreatmentCompileError(f"control file is missing or symlinked: {resolved}")
        snapshots[name] = FileSnapshot(resolved, sha256_file(resolved))
    validate_tool_map(values["router_map"])
    # Validate each schema definition before trusting it for production input.
    for name in ("router_input_schema", "decision_schema", "treatment_schema"):
        try:
            Validator(dict(values[name])).validate({})
        except SchemaDefinitionError as exc:
            raise TreatmentCompileError(f"{name} is invalid: {exc}") from exc
    return ControlSet(
        tool_map=values["router_map"],
        decision_schema=values["decision_schema"],
        treatment_schema=values["treatment_schema"],
        snapshots=snapshots,
    )


def validate_approval(
    edit_dir: Path,
    plan: Mapping[str, Any],
    plan_snapshot: FileSnapshot,
    approval: Mapping[str, Any],
    approval_snapshot: FileSnapshot,
) -> None:
    if approval.get("status") != "approved":
        raise TreatmentCompileError("semantic approval status is not approved")
    proposal_file = approval.get("proposal_file")
    if not isinstance(proposal_file, str) or not proposal_file:
        raise TreatmentCompileError("semantic approval proposal_file is invalid")
    raw = Path(proposal_file).expanduser()
    proposal_path = raw if raw.is_absolute() else edit_dir / raw
    try:
        proposal_path = path_under_edit(edit_dir, proposal_path, "semantic approval proposal")
    except AssetGateError as exc:
        raise TreatmentCompileError(str(exc)) from exc
    if proposal_path != plan_snapshot.path:
        raise TreatmentCompileError(
            "semantic approval does not reference current semantic_plan.json"
        )
    if approval.get("proposal_sha256") != plan_snapshot.sha256:
        raise TreatmentCompileError("semantic plan changed after approval")
    if plan.get("status") not in {"pending", "approved"}:
        raise TreatmentCompileError("semantic plan status is invalid")
    if approval_snapshot.path != (edit_dir / "approval.json").resolve():
        raise TreatmentCompileError("semantic approval path is not canonical")


def visual_contracts(plan: Mapping[str, Any]) -> list[VisualContract]:
    narrative = plan.get("narrative")
    visual_plan = plan.get("visual_plan")
    if not isinstance(narrative, list) or not narrative:
        raise TreatmentCompileError("semantic_plan.narrative must be a non-empty array")
    if not isinstance(visual_plan, list) or len(visual_plan) > MAX_DECISIONS:
        raise TreatmentCompileError(
            f"semantic_plan.visual_plan must contain at most {MAX_DECISIONS} items"
        )
    section_ids: list[str] = []
    for index, item in enumerate(narrative):
        if not isinstance(item, dict):
            raise TreatmentCompileError(f"semantic_plan.narrative[{index}] must be an object")
        section_ids.append(trimmed_text(item.get("id"), f"narrative[{index}].id"))
    if len(section_ids) != len(set(section_ids)):
        raise TreatmentCompileError("semantic plan contains duplicate narrative section IDs")
    section_indices = {section_id: index for index, section_id in enumerate(section_ids)}

    contracts: list[VisualContract] = []
    seen: set[str] = set()
    allowed_asset_types = {
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
    for index, raw in enumerate(visual_plan):
        label = f"semantic_plan.visual_plan[{index}]"
        if not isinstance(raw, dict):
            raise TreatmentCompileError(f"{label} must be an object")
        visual_id = trimmed_text(raw.get("id"), f"{label}.id")
        if visual_id in seen:
            raise TreatmentCompileError(f"duplicate approved visual_id: {visual_id!r}")
        seen.add(visual_id)
        section_id = trimmed_text(raw.get("section_id"), f"{label}.section_id")
        if section_id not in section_indices:
            raise TreatmentCompileError(
                f"approved visual {visual_id!r} references an unknown section"
            )
        meaning_ids = string_array(raw.get("meaning_ids"), f"{label}.meaning_ids")
        asset_type = trimmed_text(raw.get("asset_type"), f"{label}.asset_type")
        if asset_type not in allowed_asset_types:
            raise TreatmentCompileError(
                f"approved visual {visual_id!r} has unsupported asset_type {asset_type!r}"
            )
        approved_text = raw.get("approved_text")
        if approved_text is not None:
            approved_text = trimmed_text(approved_text, f"{label}.approved_text")
        contracts.append(
            VisualContract(
                visual_id=visual_id,
                section_id=section_id,
                meaning_ids=meaning_ids,
                asset_type=asset_type,
                purpose=trimmed_text(raw.get("purpose"), f"{label}.purpose"),
                treatment=trimmed_text(raw.get("treatment"), f"{label}.treatment"),
                approved_text=approved_text,
                section_index=section_indices[section_id],
                visual_plan_index=index,
            )
        )
    return sorted(
        contracts,
        key=lambda item: (item.section_index, item.visual_plan_index, item.visual_id),
    )


def map_effect_index(tool_map: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (ref.tool["id"], ref.effect["id"]): ref.effect
        for ref in effect_refs(tool_map)
    }


def validate_selected_effect(
    selected: Mapping[str, Any],
    expected_kind: str,
    visual: VisualContract,
    effect_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> Mapping[str, Any]:
    identity = (selected["tool_id"], selected["effect_id"])
    mapped = effect_index.get(identity)
    if mapped is None:
        raise TreatmentCompileError(
            f"visual {visual.visual_id!r} selects an effect absent from the current map: "
            f"{identity[0]}/{identity[1]}"
        )
    if selected["kind"] != expected_kind:
        raise TreatmentCompileError(
            f"visual {visual.visual_id!r} selected {selected['effect_id']!r} in the wrong slot"
        )
    canonical_fields = {
        "label": "label",
        "kind": "kind",
        "family": "family",
        "intensity": "base_intensity",
        "full_frame": "full_frame",
        "recipe": "recipe",
    }
    for selected_field, map_field in canonical_fields.items():
        if selected[selected_field] != mapped[map_field]:
            raise TreatmentCompileError(
                f"visual {visual.visual_id!r} effect {selected['effect_id']!r} has stale or "
                f"forged {selected_field}"
            )
    duration = float(selected["duration_s"])
    if not math.isfinite(duration) or not (
        float(mapped["min_duration_s"]) <= duration <= float(mapped["max_duration_s"])
    ):
        raise TreatmentCompileError(
            f"visual {visual.visual_id!r} effect {selected['effect_id']!r} duration is outside "
            "the current map bounds"
        )
    if visual.asset_type not in mapped["match"]["asset_types"]:
        raise TreatmentCompileError(
            f"effect {selected['effect_id']!r} does not accept approved asset_type "
            f"{visual.asset_type!r}"
        )
    matched_signals = set(selected["matched_signals"])
    if not matched_signals <= KNOWN_SIGNALS:
        raise TreatmentCompileError(
            f"effect {selected['effect_id']!r} contains unknown matched signals: "
            f"{sorted(matched_signals - KNOWN_SIGNALS)}"
        )
    required_all = set(mapped["match"]["required_all"])
    required_any = set(mapped["match"]["required_any"])
    forbidden = set(mapped["match"]["forbidden_any"])
    if not required_all <= matched_signals or not (required_any & matched_signals):
        raise TreatmentCompileError(
            f"effect {selected['effect_id']!r} does not carry its mapped semantic signal contract"
        )
    if forbidden & matched_signals:
        raise TreatmentCompileError(
            f"effect {selected['effect_id']!r} carries forbidden semantic signals"
        )
    return mapped


def validate_decision(
    record: DecisionRecord,
    visual: VisualContract,
    plan_sha256: str,
    approval_sha256: str,
    controls: ControlSet,
    effect_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    decision = record.value
    validate_schema(decision, controls.decision_schema, f"creative decision {record.relative_path}")
    provenance = decision["provenance"]
    exact = {
        "visual_id": visual.visual_id,
        "section_id": visual.section_id,
        "semantic_plan_sha256": plan_sha256,
        "approval_sha256": approval_sha256,
        "tool_map_sha256": controls.snapshots["router_map"].sha256,
        "input_schema_sha256": controls.snapshots["router_input_schema"].sha256,
        "output_schema_sha256": controls.snapshots["decision_schema"].sha256,
        "router_sha256": controls.snapshots["router"].sha256,
    }
    for field, expected in exact.items():
        if provenance[field] != expected:
            raise TreatmentCompileError(
                f"decision {record.relative_path} has stale or mismatched provenance.{field}"
            )
    if tuple(provenance["meaning_ids"]) != visual.meaning_ids:
        raise TreatmentCompileError(
            f"decision {record.relative_path} meaning_ids do not exactly match approved visual"
        )
    if decision["map_id"] != controls.tool_map["map_id"]:
        raise TreatmentCompileError(f"decision {record.relative_path} map_id is stale")
    if visual.asset_type == "none" and decision["decision"] != "none":
        raise TreatmentCompileError(
            f"approved visual {visual.visual_id!r} has asset_type=none and cannot select an effect"
        )

    mapped_effects: list[Mapping[str, Any]] = []
    primary = decision["primary_effect"]
    if decision["decision"] == "effect":
        if not isinstance(primary, dict):
            raise TreatmentCompileError(
                f"effect decision {record.relative_path} has no primary effect"
            )
        mapped_effects.append(
            validate_selected_effect(primary, "primary_visual", visual, effect_index)
        )
    elif primary is not None:
        raise TreatmentCompileError(f"none decision {record.relative_path} carries a primary effect")
    for support in decision["supporting_effects"]:
        mapped_effects.append(
            validate_selected_effect(support, "support_audio", visual, effect_index)
        )
    selected_ids = [
        (item["tool_id"], item["effect_id"])
        for item in ([primary] if isinstance(primary, dict) else [])
        + list(decision["supporting_effects"])
    ]
    if len(selected_ids) != len(set(selected_ids)):
        raise TreatmentCompileError(f"decision {record.relative_path} repeats a selected effect")
    expected_qa = sorted({qa for mapped in mapped_effects for qa in mapped["qa"]})
    if decision["required_qa"] != expected_qa:
        raise TreatmentCompileError(
            f"decision {record.relative_path} required_qa does not exactly match selected effects"
        )


def compile_payload(
    plan: Mapping[str, Any],
    plan_snapshot: FileSnapshot,
    approval_snapshot: FileSnapshot,
    controls: ControlSet,
    decisions: Sequence[DecisionRecord],
) -> dict[str, Any]:
    visuals = visual_contracts(plan)
    approved_ids = {item.visual_id for item in visuals}
    if len(decisions) > MAX_DECISIONS:
        raise TreatmentCompileError(f"at most {MAX_DECISIONS} decisions may be compiled")
    by_visual: dict[str, DecisionRecord] = {}
    for record in decisions:
        provenance = record.value.get("provenance")
        visual_id = provenance.get("visual_id") if isinstance(provenance, Mapping) else None
        if not isinstance(visual_id, str):
            raise TreatmentCompileError(
                f"decision {record.relative_path} has no schema-valid provenance.visual_id"
            )
        if visual_id in by_visual:
            raise TreatmentCompileError(f"duplicate creative decision for visual_id {visual_id!r}")
        by_visual[visual_id] = record
    missing = sorted(approved_ids - set(by_visual))
    unexpected = sorted(set(by_visual) - approved_ids)
    if missing or unexpected:
        raise TreatmentCompileError(
            "creative decision set does not exactly cover semantic_plan.visual_plan; "
            f"missing={missing}, unexpected={unexpected}"
        )

    effect_index = map_effect_index(controls.tool_map)
    entries: list[dict[str, Any]] = []
    for timeline_index, visual in enumerate(visuals):
        record = by_visual[visual.visual_id]
        validate_decision(
            record,
            visual,
            plan_snapshot.sha256,
            approval_snapshot.sha256,
            controls,
            effect_index,
        )
        decision = record.value
        entries.append(
            {
                "timeline_index": timeline_index,
                "section_index": visual.section_index,
                "visual_plan_index": visual.visual_plan_index,
                "visual_id": visual.visual_id,
                "section_id": visual.section_id,
                "meaning_ids": list(visual.meaning_ids),
                "asset_type": visual.asset_type,
                "purpose": visual.purpose,
                "treatment": visual.treatment,
                "approved_text": visual.approved_text,
                "decision_file": {
                    "path": record.relative_path,
                    "sha256": record.sha256,
                },
                "feature_input_sha256": decision["provenance"]["feature_input_sha256"],
                "decision": decision["decision"],
                "none_reason": decision["none_reason"],
                "primary_effect": decision["primary_effect"],
                "supporting_effects": decision["supporting_effects"],
                "density": decision["density"],
                "required_qa": decision["required_qa"],
                "guardrails": decision["guardrails"],
            }
        )

    effect_count = sum(item["decision"] == "effect" for item in entries)
    none_count = len(entries) - effect_count
    control_records = {
        name: {
            "path": control_relative_path(snapshot.path),
            "sha256": snapshot.sha256,
        }
        for name, snapshot in controls.snapshots.items()
    }
    payload = {
        "version": 1,
        "type": "sprut_creative_treatment_plan",
        "compiler_version": COMPILER_VERSION,
        "map_id": controls.tool_map["map_id"],
        "timeline_order": TIMELINE_ORDER,
        "network_calls_made": 0,
        "approval": {
            "semantic_plan": {
                "path": "semantic_plan.json",
                "sha256": plan_snapshot.sha256,
            },
            "semantic_approval": {
                "path": "approval.json",
                "sha256": approval_snapshot.sha256,
            },
        },
        "controls": control_records,
        "summary": {
            "visual_plan_entries": len(entries),
            "effect_decisions": effect_count,
            "none_decisions": none_count,
            "primary_visuals": effect_count,
            "supporting_audio_effects": sum(
                len(item["supporting_effects"]) for item in entries
            ),
        },
        "entries": entries,
    }
    validate_schema(payload, controls.treatment_schema, "compiled creative treatment plan")
    return payload


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def assert_snapshots_current(snapshots: Sequence[FileSnapshot]) -> None:
    for snapshot in snapshots:
        if not snapshot.path.is_file() or sha256_file(snapshot.path) != snapshot.sha256:
            raise TreatmentCompileError(
                f"compiler input changed during validation: {snapshot.path}"
            )


def atomic_create(path: Path, encoded: bytes) -> None:
    """Atomically publish a complete file without ever replacing a target."""

    if len(encoded) > MAX_OUTPUT_BYTES:
        raise TreatmentCompileError(
            f"compiled plan exceeds {MAX_OUTPUT_BYTES} bytes"
        )
    if path.exists() or path.is_symlink():
        raise TreatmentCompileError(f"output already exists and will not be overwritten: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise TreatmentCompileError(
                f"output appeared during publish and was not overwritten: {path}"
            ) from exc
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def production_compile(edit_dir_value: Path, raw_decisions: Sequence[Path]) -> Path:
    if len(raw_decisions) > MAX_DECISIONS:
        raise TreatmentCompileError(f"at most {MAX_DECISIONS} decisions may be supplied")
    edit_dir = canonical_edit_dir(edit_dir_value)
    output = edit_dir / OUTPUT_NAME
    decision_paths = [checked_decision_path(edit_dir, raw) for raw in raw_decisions]
    if len(decision_paths) != len(set(decision_paths)):
        raise TreatmentCompileError("the same creative decision file was supplied more than once")

    # This must happen before mkstemp, mkdir, output creation, or replacement.
    require_asset_gate(edit_dir)
    if output.exists() or output.is_symlink():
        raise TreatmentCompileError(
            f"output already exists and will not be overwritten: {output}"
        )

    plan, plan_snapshot = load_json_snapshot(
        edit_dir / "semantic_plan.json", "semantic plan"
    )
    approval, approval_snapshot = load_json_snapshot(
        edit_dir / "approval.json", "semantic approval"
    )
    validate_approval(
        edit_dir,
        plan,
        plan_snapshot,
        approval,
        approval_snapshot,
    )
    controls = load_controls()
    decisions: list[DecisionRecord] = []
    for path in decision_paths:
        value, snapshot = load_json_snapshot(path, "creative decision")
        decisions.append(
            DecisionRecord(
                path=snapshot.path,
                relative_path=safe_relative_path(snapshot.path, edit_dir),
                sha256=snapshot.sha256,
                value=value,
            )
        )
    payload = compile_payload(
        plan,
        plan_snapshot,
        approval_snapshot,
        controls,
        decisions,
    )
    encoded = canonical_json(payload)
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise TreatmentCompileError(
            f"compiled plan exceeds {MAX_OUTPUT_BYTES} bytes"
        )
    assert_snapshots_current(
        [
            plan_snapshot,
            approval_snapshot,
            *controls.snapshots.values(),
            *(FileSnapshot(record.path, record.sha256) for record in decisions),
        ]
    )
    atomic_create(output, encoded)
    return output


def density_fixture() -> dict[str, Any]:
    return {
        "window_start_s": 0.0,
        "window_end_s": 10.0,
        "primary_events_before": 0,
        "hero_events_before": 0,
        "audio_support_events_before": 0,
        "visual_coverage_ratio_before": 0.0,
        "active_visual_layers_before": 0,
        "limits": {
            "window_s": 30.0,
            "max_primary_events": 4,
            "max_hero_events": 1,
            "max_audio_support_events": 5,
            "max_visual_coverage_ratio": 0.55,
            "max_active_visual_layers": 2,
            "max_supporting_effects": 1,
        },
        "blocks": [],
    }


def none_decision_fixture(
    visual: VisualContract,
    plan_sha: str,
    approval_sha: str,
    controls: ControlSet,
) -> dict[str, Any]:
    return {
        "version": 1,
        "router_version": "sprut-creative-router-1",
        "map_id": controls.tool_map["map_id"],
        "decision": "none",
        "none_reason": "No approved semantic signal justifies an effect.",
        "primary_effect": None,
        "supporting_effects": [],
        "density": density_fixture(),
        "rejected_candidates": [],
        "required_qa": [],
        "guardrails": ["Preserve the approved meaning without decorative motion."],
        "provenance": {
            "visual_id": visual.visual_id,
            "section_id": visual.section_id,
            "meaning_ids": list(visual.meaning_ids),
            "semantic_plan_sha256": plan_sha,
            "approval_sha256": approval_sha,
            "feature_input_sha256": "3" * 64,
            "tool_map_sha256": controls.snapshots["router_map"].sha256,
            "input_schema_sha256": controls.snapshots["router_input_schema"].sha256,
            "output_schema_sha256": controls.snapshots["decision_schema"].sha256,
            "router_sha256": controls.snapshots["router"].sha256,
        },
    }


def self_test() -> dict[str, Any]:
    controls = load_controls()
    plan = {
        "narrative": [{"id": "section-a"}, {"id": "section-b"}],
        "visual_plan": [
            {
                "id": "visual-b",
                "section_id": "section-b",
                "meaning_ids": ["meaning-b"],
                "asset_type": "diagram",
                "purpose": "Clarify B.",
                "treatment": "Use no effect when no signal exists.",
                "approved_text": None,
            },
            {
                "id": "visual-a",
                "section_id": "section-a",
                "meaning_ids": ["meaning-a"],
                "asset_type": "title",
                "purpose": "Clarify A.",
                "treatment": "Use no effect when no signal exists.",
                "approved_text": "A",
            },
        ],
    }
    plan_snapshot = FileSnapshot(Path("/self-test/semantic_plan.json"), "1" * 64)
    approval_snapshot = FileSnapshot(Path("/self-test/approval.json"), "2" * 64)
    visuals = visual_contracts(plan)
    decisions = [
        DecisionRecord(
            path=Path(f"/self-test/{visual.visual_id}.json"),
            relative_path=f"creative/{visual.visual_id}.json",
            sha256=str(index + 4) * 64,
            value=none_decision_fixture(
                visual,
                plan_snapshot.sha256,
                approval_snapshot.sha256,
                controls,
            ),
        )
        for index, visual in enumerate(reversed(visuals))
    ]
    payload = compile_payload(
        plan,
        plan_snapshot,
        approval_snapshot,
        controls,
        decisions,
    )
    checks: list[str] = []
    if [item["visual_id"] for item in payload["entries"]] != ["visual-a", "visual-b"]:
        raise TreatmentCompileError("self-test timeline ordering is not deterministic")
    checks.append("deterministic narrative/visual timeline order")
    if payload["summary"] != {
        "visual_plan_entries": 2,
        "effect_decisions": 0,
        "none_decisions": 2,
        "primary_visuals": 0,
        "supporting_audio_effects": 0,
    }:
        raise TreatmentCompileError("self-test summary is incorrect")
    checks.append("explicit none decisions are retained")
    repeated = compile_payload(
        plan,
        plan_snapshot,
        approval_snapshot,
        controls,
        list(reversed(decisions)),
    )
    if canonical_json(payload) != canonical_json(repeated):
        raise TreatmentCompileError("self-test output changes with decision argument order")
    checks.append("byte-stable compile independent of argument order")
    try:
        compile_payload(
            plan,
            plan_snapshot,
            approval_snapshot,
            controls,
            decisions[:1],
        )
    except TreatmentCompileError as exc:
        if "does not exactly cover" not in str(exc):
            raise
    else:
        raise TreatmentCompileError("self-test accepted a missing decision")
    checks.append("missing decision fails closed")
    try:
        compile_payload(
            plan,
            plan_snapshot,
            approval_snapshot,
            controls,
            [decisions[0], decisions[0], decisions[1]],
        )
    except TreatmentCompileError as exc:
        if "duplicate creative decision" not in str(exc):
            raise
    else:
        raise TreatmentCompileError("self-test accepted a duplicate visual decision")
    checks.append("duplicate visual decision fails closed")
    return {
        "version": 1,
        "type": "sprut_creative_treatment_compiler_self_test",
        "status": "PASS",
        "compiler_version": COMPILER_VERSION,
        "checks": checks,
        "project_required": False,
        "network_calls_made": 0,
        "files_written": 0,
    }


def description() -> dict[str, Any]:
    return {
        "version": 1,
        "type": "sprut_creative_treatment_compiler_description",
        "compiler_version": COMPILER_VERSION,
        "command": "scripts/compile_creative_treatment_plan.py",
        "network_calls_made": 0,
        "production_contract": {
            "edit_dir_required": True,
            "semantic_asset_gate_required_before_write": True,
            "decision_files": "explicit JSON files under the canonical edit directory",
            "coverage": "exactly one decision per approved visual_plan item, including none",
            "timeline_order": TIMELINE_ORDER,
            "output": f"edit/{OUTPUT_NAME}",
            "additional_outputs": 0,
            "atomic_publish": True,
            "overwrite_allowed": False,
            "maximum_decisions": MAX_DECISIONS,
            "maximum_decision_bytes": MAX_JSON_BYTES,
            "maximum_output_bytes": MAX_OUTPUT_BYTES,
        },
        "bound_controls": [
            control_relative_path(path)
            for path in (
                TOOL_MAP_PATH,
                ROUTER_PATH,
                ROUTER_INPUT_SCHEMA_PATH,
                DECISION_SCHEMA_PATH,
                TREATMENT_SCHEMA_PATH,
                SCRIPT_PATH,
            )
        ],
        "project_required": False,
        "files_written": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile existing approval-bound creative decisions into one treatment plan"
    )
    discovery = parser.add_mutually_exclusive_group()
    discovery.add_argument("--describe-json", action="store_true")
    discovery.add_argument("--self-test", action="store_true")
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument(
        "--decision",
        type=Path,
        action="append",
        default=[],
        help="decision JSON under edit/; repeat exactly once per approved visual",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.describe_json:
        if args.edit_dir is not None or args.decision:
            raise TreatmentCompileError(
                "--describe-json does not accept production project arguments"
            )
        print(json.dumps(description(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.self_test:
        if args.edit_dir is not None or args.decision:
            raise TreatmentCompileError(
                "--self-test does not accept production project arguments"
            )
        print(json.dumps(self_test(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.edit_dir is None:
        raise TreatmentCompileError("production mode requires --edit-dir")
    output = production_compile(args.edit_dir, args.decision)
    compiled = json.loads(output.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "version": 1,
                "status": "PASS",
                "output": str(output),
                "output_sha256": sha256_file(output),
                "summary": compiled["summary"],
                "network_calls_made": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssetGateError,
        OSError,
        TreatmentCompileError,
        ValueError,
    ) as exc:
        print(f"compile_creative_treatment_plan: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
