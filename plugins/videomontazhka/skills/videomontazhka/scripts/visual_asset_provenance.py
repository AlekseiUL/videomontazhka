#!/usr/bin/env python3
"""Hash-bind generated visual assets to one approved semantic-plan item."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from asset_gate import canonical_edit_dir, path_under_edit
from creative_approval import require_creative_approval


PROVENANCE_VERSION = 1
PROVENANCE_TYPE = "sprut_motion_card"
EXTERNAL_PROVENANCE_TYPE = "sprut_external_visual"
VIRTUAL_CAMERA_PROVENANCE_TYPE = "sprut_virtual_camera_asset"
GENERATOR_VERSION = "sprut-motion-card-2"
EXTERNAL_RECORDER_VERSION = "sprut-external-visual-recorder-1"
EXTERNAL_REVIEW_REQUIREMENT = "full_preview_user_approval"
VIRTUAL_CAMERA_RENDERER_VERSION = "sprut-virtual-camera-render-1"
VIRTUAL_CAMERA_REVIEW_REQUIREMENT = "motion_stability_and_exact_boundary_review"
PROVENANCE_SUFFIX = ".provenance.json"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GENERATOR_PATH = SCRIPT_DIR / "render_motion_card.py"
DEFAULT_EXTERNAL_RECORDER_PATH = SCRIPT_DIR / "record_visual_asset.py"
DEFAULT_VIRTUAL_CAMERA_RENDERER_PATH = SCRIPT_DIR / "render_virtual_camera.py"
KIND_TO_ASSET_TYPE = {
    "title": "title",
    "chapter": "chapter",
    "definition": "diagram",
    "compare": "comparison",
    "process": "process",
    "quote": "quote",
    "cta": "cta",
}


class VisualProvenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ApprovedVisualPlanItem:
    visual_id: str
    section_id: str
    meaning_ids: tuple[str, ...]
    purpose: str
    treatment: str
    asset_type: str
    approved_text: str | None
    plan_snapshot: FileSnapshot
    approval_snapshot: FileSnapshot


@dataclass(frozen=True)
class ApprovedVisualContract(ApprovedVisualPlanItem):
    card_kind: str
    approved_text: str
    visible_text: str
    normalized_words: tuple[str, ...]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise VisualProvenanceError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def load_json_object_snapshot(path: Path, label: str) -> tuple[dict[str, Any], FileSnapshot]:
    resolved = path.expanduser().resolve()
    try:
        raw = resolved.read_bytes()
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualProvenanceError(f"cannot load {label} {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise VisualProvenanceError(f"{label} must be a JSON object: {resolved}")
    return value, FileSnapshot(resolved, hashlib.sha256(raw).hexdigest())


def provenance_path_for(asset_path: Path) -> Path:
    asset = asset_path.expanduser().resolve()
    return asset.with_name(f"{asset.name}{PROVENANCE_SUFFIX}")


def normalized_words(value: str) -> tuple[str, ...]:
    """Case-fold words while treating punctuation, symbols and line breaks as separators."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        characters.append(character if category[0] in {"L", "M", "N"} else " ")
    return tuple(
        unicodedata.normalize("NFC", word)
        for word in "".join(characters).split()
        if word
    )


def derive_visible_text(spec: Mapping[str, Any]) -> str:
    """Return card text in the exact field order used by the renderer."""
    parts: list[str] = []
    kicker = str(spec.get("kicker") or "").strip().upper()
    if kicker:
        parts.append(kicker)
    title = str(spec.get("title") or "").strip()
    if title:
        parts.append(title)
    body = str(spec.get("body") or "").strip()
    if body:
        parts.append(body)
    raw_items = spec.get("items")
    if raw_items is not None:
        if not isinstance(raw_items, list):
            raise VisualProvenanceError("motion-card items must be a JSON array")
        for index, raw_item in enumerate(raw_items):
            item = str(raw_item).strip()
            if not item:
                raise VisualProvenanceError(f"motion-card items[{index}] is blank")
            parts.append(item)
    cta = str(spec.get("cta") or "").strip()
    if cta:
        parts.append(cta)
    return "\n".join(parts)


def _resolved_edit_reference(edit_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise VisualProvenanceError(f"{label} must be a non-empty path")
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else edit_dir / raw
    try:
        return path_under_edit(edit_dir, candidate, label)
    except Exception as exc:
        raise VisualProvenanceError(str(exc)) from exc


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise VisualProvenanceError(f"{label} must be a trimmed non-empty string")
    return value


def _require_meaning_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise VisualProvenanceError("approved visual meaning_ids must be a non-empty array")
    result = tuple(_require_string(item, "approved visual meaning_id") for item in value)
    if len(result) != len(set(result)):
        raise VisualProvenanceError("approved visual meaning_ids contain duplicates")
    return result


def load_approved_visual_plan_item(
    edit_dir: Path,
    visual_id: str,
    *,
    require_gate3: bool = True,
) -> ApprovedVisualPlanItem:
    """Load exactly one current approval-bound visual-plan item."""
    canonical = canonical_edit_dir(edit_dir)
    if require_gate3:
        require_creative_approval(canonical)
    requested_id = _require_string(visual_id, "visual_id")
    plan_path = canonical / "semantic_plan.json"
    approval_path = canonical / "approval.json"
    plan, plan_snapshot = load_json_object_snapshot(plan_path, "semantic plan")
    approval, approval_snapshot = load_json_object_snapshot(approval_path, "semantic approval")

    if approval.get("status") != "approved":
        raise VisualProvenanceError("semantic approval status is not approved")
    proposal_path = _resolved_edit_reference(
        canonical, approval.get("proposal_file"), "semantic approval proposal_file"
    )
    if proposal_path != plan_path.resolve():
        raise VisualProvenanceError("semantic approval does not reference current semantic_plan.json")
    if approval.get("proposal_sha256") != plan_snapshot.sha256:
        raise VisualProvenanceError("semantic plan changed after approval")

    visual_plan = plan.get("visual_plan")
    if not isinstance(visual_plan, list):
        raise VisualProvenanceError("semantic_plan.visual_plan must be an array")
    matches = [
        item
        for item in visual_plan
        if isinstance(item, dict) and item.get("id") == requested_id
    ]
    if len(matches) != 1:
        raise VisualProvenanceError(
            f"visual_id {requested_id!r} must match exactly one approved visual_plan item; "
            f"found {len(matches)}"
        )
    item = matches[0]
    section_id = _require_string(item.get("section_id"), "approved visual section_id")
    meaning_ids = _require_meaning_ids(item.get("meaning_ids"))
    purpose = _require_string(item.get("purpose"), "approved visual purpose")
    treatment = _require_string(item.get("treatment"), "approved visual treatment")
    asset_type = _require_string(item.get("asset_type"), "approved visual asset_type")
    approved_text = item.get("approved_text")
    if approved_text is not None:
        approved_text = _require_string(approved_text, "approved visual approved_text")
        if not normalized_words(approved_text):
            raise VisualProvenanceError("approved visual approved_text contains no words")
    return ApprovedVisualPlanItem(
        visual_id=requested_id,
        section_id=section_id,
        meaning_ids=meaning_ids,
        purpose=purpose,
        treatment=treatment,
        asset_type=asset_type,
        approved_text=approved_text,
        plan_snapshot=plan_snapshot,
        approval_snapshot=approval_snapshot,
    )


def load_approved_visual_contract(
    edit_dir: Path,
    visual_id: str,
    spec: Mapping[str, Any],
) -> ApprovedVisualContract:
    """Load one current approval-bound visual and match it word-for-word to a card spec."""
    approved = load_approved_visual_plan_item(edit_dir, visual_id)
    card_kind = _require_string(spec.get("kind"), "motion-card kind")
    expected_asset_type = KIND_TO_ASSET_TYPE.get(card_kind)
    if expected_asset_type is None:
        raise VisualProvenanceError(
            f"motion-card kind {card_kind!r} has no approved asset_type mapping"
        )
    if approved.asset_type in {"none", "b_roll"}:
        raise VisualProvenanceError(
            f"approved visual asset_type={approved.asset_type!r} cannot be rendered as a motion card"
        )
    if approved.asset_type != expected_asset_type:
        raise VisualProvenanceError(
            f"motion-card kind {card_kind!r} requires approved asset_type "
            f"{expected_asset_type!r}, got {approved.asset_type!r}"
        )
    if approved.approved_text is None:
        raise VisualProvenanceError("motion cards cannot use a null approved_text")
    approved_text = approved.approved_text
    approved_words = normalized_words(approved_text)

    visible_text = derive_visible_text(spec)
    visible_words = normalized_words(visible_text)
    if visible_words != approved_words:
        raise VisualProvenanceError(
            "motion-card visible words do not exactly match approved_text in order; "
            f"approved={list(approved_words)!r}, visible={list(visible_words)!r}"
        )
    return ApprovedVisualContract(
        visual_id=approved.visual_id,
        section_id=approved.section_id,
        meaning_ids=approved.meaning_ids,
        purpose=approved.purpose,
        treatment=approved.treatment,
        asset_type=approved.asset_type,
        card_kind=card_kind,
        approved_text=approved_text,
        visible_text=visible_text,
        normalized_words=visible_words,
        plan_snapshot=approved.plan_snapshot,
        approval_snapshot=approved.approval_snapshot,
    )


def capture_generator_snapshots(
    generator_path: Path = DEFAULT_GENERATOR_PATH,
) -> tuple[FileSnapshot, FileSnapshot]:
    generator = generator_path.expanduser().resolve()
    helper = Path(__file__).resolve()
    if not generator.is_file():
        raise VisualProvenanceError(f"motion-card generator is missing: {generator}")
    return (
        FileSnapshot(generator, file_sha256(generator)),
        FileSnapshot(helper, file_sha256(helper)),
    )


def capture_external_recorder_snapshots(
    recorder_path: Path = DEFAULT_EXTERNAL_RECORDER_PATH,
) -> tuple[FileSnapshot, FileSnapshot]:
    recorder = recorder_path.expanduser().resolve()
    helper = Path(__file__).resolve()
    if not recorder.is_file():
        raise VisualProvenanceError(f"external visual recorder is missing: {recorder}")
    return (
        FileSnapshot(recorder, file_sha256(recorder)),
        FileSnapshot(helper, file_sha256(helper)),
    )


def capture_virtual_camera_renderer_snapshots(
    renderer_path: Path = DEFAULT_VIRTUAL_CAMERA_RENDERER_PATH,
) -> tuple[FileSnapshot, FileSnapshot]:
    renderer = renderer_path.expanduser().resolve()
    helper = Path(__file__).resolve()
    if not renderer.is_file():
        raise VisualProvenanceError(f"virtual-camera renderer is missing: {renderer}")
    return (
        FileSnapshot(renderer, file_sha256(renderer)),
        FileSnapshot(helper, file_sha256(helper)),
    )


def validate_declared_visible_text(
    approved_text: str | None,
    declared_visible_text: str | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Validate a human declaration without claiming that pixels were OCR-verified."""
    if approved_text is None:
        if declared_visible_text is not None and declared_visible_text.strip():
            raise VisualProvenanceError(
                "approved_text is null; non-empty --declared-visible-text is forbidden"
            )
        return None, ()
    declared = _require_string(
        declared_visible_text, "--declared-visible-text for a text-bearing external visual"
    )
    approved_words = normalized_words(approved_text)
    declared_words = normalized_words(declared)
    if declared_words != approved_words:
        raise VisualProvenanceError(
            "declared visible words do not exactly match approved_text in order; "
            f"approved={list(approved_words)!r}, declared={list(declared_words)!r}"
        )
    return declared, declared_words


def assert_snapshots_current(snapshots: Sequence[FileSnapshot]) -> None:
    for snapshot in snapshots:
        if not snapshot.path.is_file() or file_sha256(snapshot.path) != snapshot.sha256:
            raise VisualProvenanceError(f"provenance input changed during operation: {snapshot.path}")


def _file_record(path: Path) -> dict[str, str]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise VisualProvenanceError(f"provenance asset is missing: {resolved}")
    return {"path": str(resolved), "sha256": file_sha256(resolved)}


def build_motion_card_provenance(
    *,
    edit_dir: Path,
    output: Path,
    spec_snapshot: FileSnapshot,
    contract: ApprovedVisualContract,
    generator_snapshot: FileSnapshot,
    helper_snapshot: FileSnapshot,
    poster: Path | None = None,
) -> dict[str, Any]:
    canonical = canonical_edit_dir(edit_dir)
    asset = path_under_edit(canonical, output, "motion-card output")
    spec_path = path_under_edit(canonical, spec_snapshot.path, "motion-card spec")
    plan_path = path_under_edit(canonical, contract.plan_snapshot.path, "semantic plan")
    approval_path = path_under_edit(canonical, contract.approval_snapshot.path, "semantic approval")
    payload: dict[str, Any] = {
        "version": PROVENANCE_VERSION,
        "type": PROVENANCE_TYPE,
        "output": _file_record(asset),
        "spec": {"path": str(spec_path), "sha256": spec_snapshot.sha256},
        "semantic_plan": {
            "path": str(plan_path),
            "sha256": contract.plan_snapshot.sha256,
        },
        "approval": {
            "path": str(approval_path),
            "sha256": contract.approval_snapshot.sha256,
            "proposal_sha256": contract.plan_snapshot.sha256,
        },
        "visual_id": contract.visual_id,
        "section_id": contract.section_id,
        "meaning_ids": list(contract.meaning_ids),
        "purpose": contract.purpose,
        "treatment": contract.treatment,
        "asset_type": contract.asset_type,
        "card_kind": contract.card_kind,
        "approved_text": contract.approved_text,
        "semantic_text": contract.approved_text,
        "visible_text": contract.visible_text,
        "normalized_words": list(contract.normalized_words),
        "generator": {
            "path": str(generator_snapshot.path),
            "sha256": generator_snapshot.sha256,
            "version": GENERATOR_VERSION,
            "provenance_helper_path": str(helper_snapshot.path),
            "provenance_helper_sha256": helper_snapshot.sha256,
        },
    }
    if poster is not None:
        poster_path = path_under_edit(canonical, poster, "motion-card poster")
        payload["poster"] = _file_record(poster_path)
    return payload


def build_external_visual_provenance(
    *,
    edit_dir: Path,
    asset: Path,
    approved: ApprovedVisualPlanItem,
    declared_visible_text: str | None,
    recorder_snapshot: FileSnapshot,
    helper_snapshot: FileSnapshot,
    source_spec_snapshot: FileSnapshot | None = None,
) -> dict[str, Any]:
    canonical = canonical_edit_dir(edit_dir)
    asset_path = path_under_edit(canonical, asset, "external visual asset")
    plan_path = path_under_edit(canonical, approved.plan_snapshot.path, "semantic plan")
    approval_path = path_under_edit(
        canonical, approved.approval_snapshot.path, "semantic approval"
    )
    if approved.asset_type == "none":
        raise VisualProvenanceError(
            "approved visual asset_type='none' cannot be recorded as an external overlay"
        )
    declared, declared_words = validate_declared_visible_text(
        approved.approved_text, declared_visible_text
    )
    payload: dict[str, Any] = {
        "version": PROVENANCE_VERSION,
        "type": EXTERNAL_PROVENANCE_TYPE,
        "output": _file_record(asset_path),
        "semantic_plan": {
            "path": str(plan_path),
            "sha256": approved.plan_snapshot.sha256,
        },
        "approval": {
            "path": str(approval_path),
            "sha256": approved.approval_snapshot.sha256,
            "proposal_sha256": approved.plan_snapshot.sha256,
        },
        "visual_id": approved.visual_id,
        "section_id": approved.section_id,
        "meaning_ids": list(approved.meaning_ids),
        "purpose": approved.purpose,
        "treatment": approved.treatment,
        "asset_type": approved.asset_type,
        "approved_text": approved.approved_text,
        "semantic_text": approved.approved_text,
        "declared_visible_text": declared,
        "normalized_words": list(declared_words),
        "review_requirement": EXTERNAL_REVIEW_REQUIREMENT,
        "recorder": {
            "path": str(recorder_snapshot.path),
            "sha256": recorder_snapshot.sha256,
            "version": EXTERNAL_RECORDER_VERSION,
            "provenance_helper_path": str(helper_snapshot.path),
            "provenance_helper_sha256": helper_snapshot.sha256,
        },
    }
    if source_spec_snapshot is not None:
        source_spec_path = path_under_edit(
            canonical, source_spec_snapshot.path, "external visual source spec"
        )
        payload["source_spec"] = {
            "path": str(source_spec_path),
            "sha256": source_spec_snapshot.sha256,
        }
    return payload


def build_virtual_camera_provenance(
    *,
    edit_dir: Path,
    output: Path,
    source_snapshot: FileSnapshot,
    plan_snapshot: FileSnapshot,
    approved: ApprovedVisualPlanItem,
    renderer_snapshot: FileSnapshot,
    helper_snapshot: FileSnapshot,
    event: Mapping[str, Any],
    fps: float,
    frames: int,
    render_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one source-backed camera render to its exact approved visual beat."""
    canonical = canonical_edit_dir(edit_dir)
    asset_path = path_under_edit(canonical, output, "virtual-camera output")
    source_path = path_under_edit(
        canonical, source_snapshot.path, "virtual-camera source"
    )
    camera_plan_path = path_under_edit(
        canonical, plan_snapshot.path, "virtual-camera plan"
    )
    plan_path = path_under_edit(canonical, approved.plan_snapshot.path, "semantic plan")
    approval_path = path_under_edit(
        canonical, approved.approval_snapshot.path, "semantic approval"
    )
    event_id = _require_string(event.get("id"), "virtual-camera event id")
    shot_id = _require_string(event.get("shot_id"), "virtual-camera shot_id")
    reason = _require_string(event.get("reason"), "virtual-camera reason")
    if approved.asset_type == "none":
        raise VisualProvenanceError(
            "approved visual asset_type='none' cannot be a virtual-camera overlay"
        )
    if approved.approved_text is not None:
        raise VisualProvenanceError(
            "virtual-camera overlays cannot introduce approved_text; use a separate text asset"
        )
    if not isinstance(frames, int) or isinstance(frames, bool) or frames <= 0:
        raise VisualProvenanceError("virtual-camera frames must be a positive integer")
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or not (0 < float(fps) <= 120):
        raise VisualProvenanceError("virtual-camera fps must be finite and positive")
    return {
        "version": PROVENANCE_VERSION,
        "type": VIRTUAL_CAMERA_PROVENANCE_TYPE,
        "output": _file_record(asset_path),
        "semantic_plan": {
            "path": str(plan_path),
            "sha256": approved.plan_snapshot.sha256,
        },
        "approval": {
            "path": str(approval_path),
            "sha256": approved.approval_snapshot.sha256,
            "proposal_sha256": approved.plan_snapshot.sha256,
        },
        "visual_id": approved.visual_id,
        "section_id": approved.section_id,
        "meaning_ids": list(approved.meaning_ids),
        "purpose": approved.purpose,
        "treatment": approved.treatment,
        "asset_type": approved.asset_type,
        "approved_text": None,
        "semantic_text": None,
        "source": {"path": str(source_path), "sha256": source_snapshot.sha256},
        "plan": {"path": str(camera_plan_path), "sha256": plan_snapshot.sha256},
        "event_id": event_id,
        "shot_id": shot_id,
        "reason": reason,
        "fps": float(fps),
        "frames": frames,
        "audio_streams": 0,
        "render_contract": dict(render_contract),
        "review_requirement": VIRTUAL_CAMERA_REVIEW_REQUIREMENT,
        "renderer": {
            "path": str(renderer_snapshot.path),
            "sha256": renderer_snapshot.sha256,
            "version": VIRTUAL_CAMERA_RENDERER_VERSION,
            "provenance_helper_path": str(helper_snapshot.path),
            "provenance_helper_sha256": helper_snapshot.sha256,
        },
    }


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".part", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def invalidate_provenance(
    path: Path,
    reason: str,
    provenance_type: str = PROVENANCE_TYPE,
) -> None:
    atomic_write_json(
        path,
        {
            "version": PROVENANCE_VERSION,
            "type": provenance_type,
            "status": "invalidated",
            "reason": reason,
        },
    )


def _record_path_and_hash(
    edit_dir: Path,
    value: Any,
    label: str,
    *,
    extra_keys: set[str] | None = None,
) -> tuple[Path, str]:
    if not isinstance(value, dict):
        raise VisualProvenanceError(f"{label} must be an object")
    allowed = {"path", "sha256"} | (extra_keys or set())
    if set(value) != allowed:
        raise VisualProvenanceError(
            f"{label} fields must be exactly {sorted(allowed)}; got {sorted(value)}"
        )
    path = _resolved_edit_reference(edit_dir, value.get("path"), f"{label}.path")
    digest = value.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise VisualProvenanceError(f"{label}.sha256 is invalid")
    if not path.is_file() or file_sha256(path) != digest:
        raise VisualProvenanceError(f"{label} changed after provenance was recorded: {path}")
    return path, digest


def _verify_implementation_record(
    value: Any,
    label: str,
    expected_path: Path,
    expected_version: str,
) -> list[FileSnapshot]:
    expected_keys = {
        "path",
        "sha256",
        "version",
        "provenance_helper_path",
        "provenance_helper_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise VisualProvenanceError(f"{label} provenance fields are invalid")
    implementation_path = Path(str(value.get("path") or "")).expanduser().resolve()
    helper_path = Path(str(value.get("provenance_helper_path") or "")).expanduser().resolve()
    if implementation_path != expected_path.resolve() or helper_path != Path(__file__).resolve():
        raise VisualProvenanceError(f"visual provenance references a different {label}")
    if value.get("version") != expected_version:
        raise VisualProvenanceError(f"visual provenance {label} version is stale")
    snapshots: list[FileSnapshot] = []
    for path, key in (
        (implementation_path, "sha256"),
        (helper_path, "provenance_helper_sha256"),
    ):
        digest = value.get(key)
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not path.is_file()
            or digest != file_sha256(path)
        ):
            raise VisualProvenanceError(
                f"visual provenance {label} implementation changed: {path}"
            )
        snapshots.append(FileSnapshot(path, digest))
    return snapshots


def verify_visual_asset_provenance(
    edit_dir: Path,
    provenance_path: Path,
    *,
    asset_path: Path | None = None,
    overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a sidecar against current bytes, current approval and an EDL overlay contract."""
    canonical = canonical_edit_dir(edit_dir)
    sidecar = _resolved_edit_reference(
        canonical, str(provenance_path), "visual provenance sidecar"
    )
    value, sidecar_snapshot = load_json_object_snapshot(sidecar, "visual provenance sidecar")
    common_required = {
        "version",
        "type",
        "output",
        "semantic_plan",
        "approval",
        "visual_id",
        "section_id",
        "meaning_ids",
        "purpose",
        "treatment",
        "asset_type",
        "approved_text",
        "semantic_text",
    }
    motion_required = common_required | {
        "spec",
        "card_kind",
        "visible_text",
        "normalized_words",
        "generator",
    }
    external_required = common_required | {
        "declared_visible_text",
        "normalized_words",
        "review_requirement",
        "recorder",
    }
    virtual_camera_required = common_required | {
        "source",
        "plan",
        "event_id",
        "shot_id",
        "reason",
        "fps",
        "frames",
        "audio_streams",
        "render_contract",
        "review_requirement",
        "renderer",
    }
    provenance_type = value.get("type")
    if provenance_type == PROVENANCE_TYPE:
        required = motion_required
        allowed = required | {"poster"}
    elif provenance_type == EXTERNAL_PROVENANCE_TYPE:
        required = external_required
        allowed = required | {"source_spec"}
    elif provenance_type == VIRTUAL_CAMERA_PROVENANCE_TYPE:
        required = virtual_camera_required
        allowed = required
    else:
        raise VisualProvenanceError("unsupported visual provenance type")
    if set(value) - allowed or not required.issubset(value):
        raise VisualProvenanceError("visual provenance sidecar has missing or unknown fields")
    if value.get("version") != PROVENANCE_VERSION:
        raise VisualProvenanceError("unsupported visual provenance format")

    output_path, _ = _record_path_and_hash(canonical, value.get("output"), "output")
    expected_asset = (
        _resolved_edit_reference(canonical, str(asset_path), "visual asset")
        if asset_path is not None
        else output_path
    )
    if output_path != expected_asset:
        raise VisualProvenanceError("visual provenance belongs to a different asset")
    if sidecar != provenance_path_for(output_path):
        raise VisualProvenanceError("visual provenance sidecar is not next to its exact output")

    plan_path, plan_hash = _record_path_and_hash(
        canonical, value.get("semantic_plan"), "semantic_plan"
    )
    if plan_path != (canonical / "semantic_plan.json").resolve():
        raise VisualProvenanceError("visual provenance references a non-current semantic plan")
    approval_path, approval_hash = _record_path_and_hash(
        canonical,
        value.get("approval"),
        "approval",
        extra_keys={"proposal_sha256"},
    )
    if approval_path != (canonical / "approval.json").resolve():
        raise VisualProvenanceError("visual provenance references a non-current approval")
    if value["approval"].get("proposal_sha256") != plan_hash:
        raise VisualProvenanceError("visual provenance approval does not bind its semantic plan")

    visual_id = _require_string(value.get("visual_id"), "provenance visual_id")
    approved = load_approved_visual_plan_item(canonical, visual_id)
    if approved.plan_snapshot.sha256 != plan_hash:
        raise VisualProvenanceError("semantic plan hash differs from visual provenance")
    if approved.approval_snapshot.sha256 != approval_hash:
        raise VisualProvenanceError("semantic approval hash differs from visual provenance")

    expected_semantics: dict[str, Any] = {
        "visual_id": approved.visual_id,
        "section_id": approved.section_id,
        "meaning_ids": list(approved.meaning_ids),
        "purpose": approved.purpose,
        "treatment": approved.treatment,
        "asset_type": approved.asset_type,
        "approved_text": approved.approved_text,
        "semantic_text": approved.approved_text,
    }
    branch_snapshots: list[FileSnapshot] = []
    if provenance_type == PROVENANCE_TYPE:
        spec_path, spec_hash = _record_path_and_hash(canonical, value.get("spec"), "spec")
        spec, current_spec_snapshot = load_json_object_snapshot(spec_path, "motion-card spec")
        if current_spec_snapshot.sha256 != spec_hash:
            raise VisualProvenanceError("motion-card spec changed after provenance was recorded")
        contract = load_approved_visual_contract(canonical, visual_id, spec)
        expected_semantics.update(
            {
                "card_kind": contract.card_kind,
                "visible_text": contract.visible_text,
                "normalized_words": list(contract.normalized_words),
            }
        )
        branch_snapshots.append(current_spec_snapshot)
        if "poster" in value:
            poster_path, poster_hash = _record_path_and_hash(
                canonical, value.get("poster"), "poster"
            )
            branch_snapshots.append(FileSnapshot(poster_path, poster_hash))
        branch_snapshots.extend(
            _verify_implementation_record(
                value.get("generator"),
                "generator",
                DEFAULT_GENERATOR_PATH,
                GENERATOR_VERSION,
            )
        )
    elif provenance_type == EXTERNAL_PROVENANCE_TYPE:
        if approved.asset_type == "none":
            raise VisualProvenanceError(
                "approved visual asset_type='none' cannot be an external overlay"
            )
        declared, declared_words = validate_declared_visible_text(
            approved.approved_text, value.get("declared_visible_text")
        )
        expected_semantics.update(
            {
                "declared_visible_text": declared,
                "normalized_words": list(declared_words),
                "review_requirement": EXTERNAL_REVIEW_REQUIREMENT,
            }
        )
        if "source_spec" in value:
            source_spec_path, source_spec_hash = _record_path_and_hash(
                canonical, value.get("source_spec"), "source_spec"
            )
            if source_spec_path in {output_path, sidecar, plan_path, approval_path}:
                raise VisualProvenanceError(
                    "external source_spec must differ from asset and control/provenance files"
                )
            branch_snapshots.append(FileSnapshot(source_spec_path, source_spec_hash))
        branch_snapshots.extend(
            _verify_implementation_record(
                value.get("recorder"),
                "external visual recorder",
                DEFAULT_EXTERNAL_RECORDER_PATH,
                EXTERNAL_RECORDER_VERSION,
            )
        )
    else:
        if approved.asset_type == "none":
            raise VisualProvenanceError(
                "approved visual asset_type='none' cannot be a virtual-camera overlay"
            )
        if approved.approved_text is not None:
            raise VisualProvenanceError(
                "virtual-camera overlays cannot introduce approved_text"
            )
        source_path, source_hash = _record_path_and_hash(
            canonical, value.get("source"), "source"
        )
        camera_plan_path, camera_plan_hash = _record_path_and_hash(
            canonical, value.get("plan"), "plan"
        )
        if source_path in {output_path, sidecar, plan_path, approval_path, camera_plan_path}:
            raise VisualProvenanceError(
                "virtual-camera source must differ from output, plan, and control/provenance files"
            )
        if camera_plan_path in {output_path, sidecar, plan_path, approval_path}:
            raise VisualProvenanceError(
                "virtual-camera plan must differ from output and control/provenance files"
            )
        camera_plan, current_camera_plan_snapshot = load_json_object_snapshot(
            camera_plan_path, "virtual-camera plan"
        )
        if current_camera_plan_snapshot.sha256 != camera_plan_hash:
            raise VisualProvenanceError(
                "virtual-camera plan changed after provenance was recorded"
            )
        if (
            camera_plan.get("version") != 1
            or camera_plan.get("type") != "sprut_virtual_camera_plan"
            or camera_plan.get("generator") != "sprut-virtual-camera-plan-1"
        ):
            raise VisualProvenanceError("visual provenance references a non-canonical camera plan")
        event_id = _require_string(value.get("event_id"), "virtual-camera event_id")
        events = camera_plan.get("events")
        matches = (
            [item for item in events if isinstance(item, dict) and item.get("id") == event_id]
            if isinstance(events, list)
            else []
        )
        if len(matches) != 1:
            raise VisualProvenanceError(
                "virtual-camera event_id must match exactly one current plan event"
            )
        event = matches[0]
        fps = value.get("fps")
        if (
            not isinstance(fps, (int, float))
            or isinstance(fps, bool)
            or not (0 < float(fps) <= 120)
            or float(camera_plan.get("fps", 0)) != float(fps)
        ):
            raise VisualProvenanceError("virtual-camera fps differs from its current plan")
        frames = value.get("frames")
        if not isinstance(frames, int) or isinstance(frames, bool) or frames <= 0:
            raise VisualProvenanceError("virtual-camera frames must be a positive integer")
        if value.get("audio_streams") != 0:
            raise VisualProvenanceError("virtual-camera asset must be silent")
        expected_semantics.update(
            {
                "approved_text": None,
                "semantic_text": None,
                "event_id": event_id,
                "shot_id": _require_string(event.get("shot_id"), "virtual-camera shot_id"),
                "reason": _require_string(event.get("reason"), "virtual-camera reason"),
                "fps": float(camera_plan["fps"]),
                "render_contract": camera_plan.get("render_contract"),
                "review_requirement": VIRTUAL_CAMERA_REVIEW_REQUIREMENT,
            }
        )
        branch_snapshots.extend(
            [
                FileSnapshot(source_path, source_hash),
                current_camera_plan_snapshot,
                *_verify_implementation_record(
                    value.get("renderer"),
                    "virtual-camera renderer",
                    DEFAULT_VIRTUAL_CAMERA_RENDERER_PATH,
                    VIRTUAL_CAMERA_RENDERER_VERSION,
                ),
            ]
        )
    for field, expected in expected_semantics.items():
        if value.get(field) != expected:
            raise VisualProvenanceError(f"visual provenance {field} differs from current approval")

    if overlay is not None:
        for field in ("visual_id", "section_id", "meaning_ids", "purpose", "semantic_text"):
            if overlay.get(field) != expected_semantics[field]:
                raise VisualProvenanceError(
                    f"overlay {field} differs from generated visual provenance"
                )
        overlay_asset = _resolved_edit_reference(canonical, overlay.get("file"), "overlay.file")
        if overlay_asset != output_path:
            raise VisualProvenanceError("overlay.file differs from generated visual asset")
        if "provenance" in overlay:
            overlay_sidecar = _resolved_edit_reference(
                canonical, overlay.get("provenance"), "overlay.provenance"
            )
            if overlay_sidecar != sidecar:
                raise VisualProvenanceError("overlay.provenance references a different sidecar")

    final_snapshots = [
        sidecar_snapshot,
        approved.plan_snapshot,
        approved.approval_snapshot,
        FileSnapshot(output_path, value["output"]["sha256"]),
        *branch_snapshots,
    ]
    assert_snapshots_current(final_snapshots)
    return value
