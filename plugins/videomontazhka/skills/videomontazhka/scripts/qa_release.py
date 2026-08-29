#!/usr/bin/env python3
"""Run the authoritative technical QA suite from a render manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from artifact_provenance import (
    RENDERER_VERSION,
    ProvenanceError,
    artifact_key,
    default_qa_dir,
    invalidate_release_state_from_manifest_path,
    preview_approval_name,
    release_manifest_name,
    render_manifest_name,
    renderer_identity,
    resolve_subtitle_fonts,
)
from runtime_paths import configured_python
from validate_caption_fit import (
    DEFAULT_DURATION_TOLERANCE_S,
    format_report as format_caption_report,
    validate_caption_file,
)


AUDIO_RATE = 48_000
AAC_ACCESS_UNIT_SAMPLES = 1_024
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class QAError(RuntimeError):
    pass


def qa_python() -> str:
    candidates = [
        os.environ.get("VIDEOMONTAZHKA_QA_PYTHON"),
        os.environ.get("SPRUT_QA_PYTHON"),
        str(configured_python()),
        sys.executable,
    ]
    visited: set[Path] = set()
    for value in candidates:
        if not value:
            continue
        candidate = Path(os.path.abspath(Path(value).expanduser()))
        if candidate in visited or not candidate.is_file():
            continue
        visited.add(candidate)
        check = subprocess.run(
            [str(candidate), "-c", "import PIL,numpy"],
            text=True, capture_output=True, check=False,
        )
        if check.returncode == 0:
            return str(candidate)
    raise QAError("no Python runtime with PIL and NumPy; run scripts/doctor.py")


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise QAError(f"file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise QAError(f"JSON root must be an object: {path}")
    return data


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        details = (result.stdout + result.stderr).strip()
        raise QAError(f"{label} failed ({result.returncode})\n{details[-4000:]}")
    return result


def parse_positive_fraction(value: Any, label: str) -> Fraction:
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise QAError(f"invalid {label}: {value!r}") from exc
    if result <= 0:
        raise QAError(f"invalid {label}: {value!r}")
    return result


def optional_int(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def logical_audio_samples(stream: dict[str, Any]) -> int | None:
    duration_ts = optional_int(stream.get("duration_ts"))
    sample_rate = optional_int(stream.get("sample_rate"))
    time_base = stream.get("time_base")
    if duration_ts is None or sample_rate is None or not time_base:
        return None
    try:
        samples = Fraction(duration_ts) * Fraction(str(time_base)) * sample_rate
    except (ValueError, ZeroDivisionError):
        return None
    return int(samples) if samples.denominator == 1 else round(samples)


def probe(path: Path) -> dict[str, Any]:
    result = run([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", str(path)
    ], "ffprobe")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    subtitles = [item for item in streams if item.get("codec_type") == "subtitle"]
    if len(videos) != 1 or len(audios) != 1:
        raise QAError("output must contain video and audio streams")
    video, audio = videos[0], audios[0]
    rate = parse_positive_fraction(
        video.get("avg_frame_rate") or video.get("r_frame_rate"), "video frame rate"
    )
    format_duration = float((data.get("format") or {}).get("duration") or 0)
    frame_count = optional_int(video.get("nb_read_frames"))
    if frame_count is None:
        frame_count = optional_int(video.get("nb_frames"))
    video_duration = float(video.get("duration") or (
        Fraction(frame_count, 1) / rate if frame_count is not None else format_duration
    ))
    audio_duration = float(audio.get("duration") or format_duration)
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": float(rate),
        "fps_fraction": f"{rate.numerator}/{rate.denominator}",
        "frame_count": frame_count,
        "video_duration_s": video_duration,
        "audio_duration_s": audio_duration,
        "duration_s": format_duration,
        "av_duration_delta_s": audio_duration - video_duration,
        "video_codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "sample_aspect_ratio": video.get("sample_aspect_ratio"),
        "field_order": video.get("field_order"),
        "video_start_s": float(video.get("start_time") or 0),
        "color_range": video.get("color_range"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
        "audio_codec": audio.get("codec_name"),
        "audio_start_s": float(audio.get("start_time") or 0),
        "audio_sample_rate": optional_int(audio.get("sample_rate")),
        "audio_channels": optional_int(audio.get("channels")),
        "audio_logical_samples": logical_audio_samples(audio),
        "video_stream_count": len(videos),
        "audio_stream_count": len(audios),
        "subtitle_stream_count": len(subtitles),
        "format_name": (data.get("format") or {}).get("format_name"),
    }


def finite_measurement(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def loudness(path: Path) -> dict[str, Any]:
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(path),
        "-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
        "-vn", "-f", "null", "-",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise QAError("loudness measurement failed")
    blocks = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.DOTALL)
    if not blocks:
        raise QAError("loudnorm returned no measurement JSON")
    data = json.loads(blocks[-1])
    integrated = finite_measurement(data.get("input_i"))
    true_peak = finite_measurement(data.get("input_tp"))
    loudness_range = finite_measurement(data.get("input_lra"))
    silent = integrated is None and true_peak is None
    if not silent and (integrated is None or true_peak is None):
        raise QAError("loudnorm returned a partial/non-finite measurement")
    return {
        "silent": silent,
        "integrated_lufs": integrated,
        "true_peak_dbtp": true_peak,
        "loudness_range_lu": loudness_range,
    }


def path_from_edl(value: Any, edl_dir: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return (edl_dir / path if not path.is_absolute() else path).resolve()


def source_has_audio(path: Path) -> bool | None:
    if not path.is_file():
        return None
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "json", str(path),
    ], text=True, capture_output=True, check=False)
    if result.returncode:
        return None
    try:
        return bool(json.loads(result.stdout).get("streams") or [])
    except json.JSONDecodeError:
        return None


def silence_is_expected(edl: dict[str, Any], project: dict[str, Any], edl_dir: Path) -> bool:
    audio_config = project.get("audio") or {}
    if edl.get("allow_silent_program") is True or audio_config.get("allow_silent_program") is True:
        return True
    if edl.get("audio_overlays"):
        return False
    sources = edl.get("sources") or {}
    ranges = edl.get("ranges") or []
    used = {str(item.get("source")) for item in ranges if isinstance(item, dict)}
    if not used or not isinstance(sources, dict):
        return False
    states: list[bool | None] = []
    for source_id in used:
        path = path_from_edl(sources.get(source_id), edl_dir)
        states.append(source_has_audio(path) if path else None)
    return bool(states) and all(state is False for state in states)


def inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def resolved_path(value: Any, base: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return (base / path if not path.is_absolute() else path).resolve()


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def current_file_record(
    path: Path | None, expected_sha256: Any, label: str, errors: list[str],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path) if path else None,
        "expected_sha256": expected_sha256,
        "actual_sha256": None,
        "matches": False,
    }
    if path is None:
        errors.append(f"{label} has no valid path")
        return record
    if not valid_sha256(expected_sha256):
        errors.append(f"{label} has no valid SHA-256 in render manifest")
    if not path.is_file():
        errors.append(f"{label} is missing: {path}")
        return record
    actual = sha256(path)
    record["actual_sha256"] = actual
    record["matches"] = valid_sha256(expected_sha256) and actual == expected_sha256
    if valid_sha256(expected_sha256) and actual != expected_sha256:
        errors.append(f"{label} changed after render: {path}")
    return record


def load_current_object(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is not readable JSON: {path} ({exc})")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} JSON root must be an object: {path}")
        return {}
    return value


def validate_asset_records(
    name: str,
    value: Any,
    edit_dir: Path,
    errors: list[str],
    *,
    require_provenance: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors.append(f"render manifest {name} must be an array")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"{name}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        path = resolved_path(item.get("path"), edit_dir)
        record = current_file_record(path, item.get("sha256"), label, errors)
        nested = item.get("provenance")
        if nested is None:
            if require_provenance:
                errors.append(f"{label} has no semantic provenance record")
        elif not isinstance(nested, dict):
            errors.append(f"{label}.provenance must be an object")
        else:
            nested_path = resolved_path(nested.get("path"), edit_dir)
            record["provenance"] = current_file_record(
                nested_path,
                nested.get("sha256"),
                f"{label}.provenance",
                errors,
            )
        result.append(record)
    return result


def validate_input_provenance(
    manifest: dict[str, Any], manifest_path: Path, errors: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Re-hash every approved input that could affect the rendered programme."""
    edit_dir = manifest_path.parent.resolve()
    provenance: dict[str, Any] = {"edit_dir": str(edit_dir)}

    project_path = resolved_path(manifest.get("project"), edit_dir)
    canonical_project = (edit_dir / "project.json").resolve()
    if project_path != canonical_project:
        errors.append(
            f"render manifest project path is not the current edit project: {project_path}"
        )
    provenance["project"] = current_file_record(
        project_path, manifest.get("project_sha256"), "project", errors
    )
    project = load_current_object(project_path, "project", errors) if project_path else {}

    edl_path = resolved_path(manifest.get("edl"), edit_dir)
    if edl_path is not None and not inside(edl_path, edit_dir):
        errors.append(f"render manifest EDL path escapes edit directory: {edl_path}")
    provenance["edl"] = current_file_record(
        edl_path, manifest.get("edl_sha256"), "EDL", errors
    )
    edl = load_current_object(edl_path, "EDL", errors) if edl_path else {}

    deliverable_id = manifest.get("deliverable_id")
    manifest_mode = manifest.get("mode")
    try:
        deliverable_artifact_key = artifact_key(deliverable_id)
    except ProvenanceError as exc:
        errors.append(str(exc))
        deliverable_artifact_key = None
    provenance["deliverable_id"] = deliverable_id
    provenance["artifact_key"] = deliverable_artifact_key
    if edl.get("deliverable_id") != deliverable_id:
        errors.append("render manifest deliverable_id differs from the current EDL")
    if manifest.get("artifact_key") != deliverable_artifact_key:
        errors.append("render manifest has a non-canonical artifact key")
    if deliverable_artifact_key is not None and manifest_mode in {"draft", "preview", "final"}:
        canonical_manifest = (
            edit_dir / render_manifest_name(deliverable_id, str(manifest_mode))
        ).resolve()
        if manifest_path != canonical_manifest:
            errors.append(
                f"QA requires the canonical deliverable manifest: {canonical_manifest}"
            )
    if manifest.get("renderer") != RENDERER_VERSION:
        errors.append(
            f"render manifest requires renderer {RENDERER_VERSION}, got {manifest.get('renderer')!r}"
        )
    try:
        current_identity = renderer_identity()
    except ProvenanceError as exc:
        errors.append(f"cannot establish current renderer identity: {exc}")
        current_identity = {}
    provenance["renderer_identity"] = current_identity
    if manifest.get("renderer_identity") != current_identity:
        errors.append("renderer implementation or FFmpeg/FFprobe identity differs from render manifest")
    output_value = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    rendered_output_path = resolved_path(output_value.get("path"), edit_dir)
    if rendered_output_path is not None and not inside(rendered_output_path, edit_dir):
        errors.append("rendered output path escapes the EDL edit directory")

    plan_path = (edit_dir / "semantic_plan.json").resolve()
    plan_hash = manifest.get("approval_plan_sha256")
    provenance["semantic_plan"] = current_file_record(
        plan_path, plan_hash, "semantic plan", errors
    )
    approval_path = (edit_dir / "approval.json").resolve()
    approval = load_current_object(approval_path, "semantic approval", errors)
    approval_record: dict[str, Any] = {
        "path": str(approval_path),
        "status": approval.get("status"),
        "proposal_sha256": approval.get("proposal_sha256"),
    }
    if not approval_path.is_file():
        errors.append(f"semantic approval is missing: {approval_path}")
    if approval.get("status") != "approved":
        errors.append("semantic approval status is not approved")
    if approval.get("proposal_sha256") != plan_hash:
        errors.append("semantic approval hash differs from render manifest approval plan")
    proposal_path = resolved_path(approval.get("proposal_file"), edit_dir)
    approval_record["proposal_path"] = str(proposal_path) if proposal_path else None
    if proposal_path != plan_path:
        errors.append("semantic approval does not point to the current semantic_plan.json")
    provenance["approval"] = approval_record

    fingerprints = manifest.get("source_fingerprints")
    sources = edl.get("sources")
    if not isinstance(fingerprints, dict) or not fingerprints:
        errors.append("render manifest source_fingerprints must be a non-empty object")
        fingerprints = {}
    if not isinstance(sources, dict) or not sources:
        errors.append("current EDL sources must be a non-empty object")
        sources = {}
    fingerprint_ids = {str(item) for item in fingerprints}
    source_ids = {str(item) for item in sources}
    if fingerprint_ids != source_ids:
        errors.append(
            "render manifest source_fingerprints IDs differ from current EDL sources: "
            f"manifest={sorted(fingerprint_ids)}, EDL={sorted(source_ids)}"
        )
    source_records: list[dict[str, Any]] = []
    for source_id in sorted(fingerprint_ids | source_ids):
        path = resolved_path(sources.get(source_id), edit_dir)
        record = current_file_record(
            path, fingerprints.get(source_id), f"source_fingerprints[{source_id!r}]", errors
        )
        record["id"] = source_id
        source_records.append(record)
    provenance["sources"] = source_records

    provenance["visual_assets"] = validate_asset_records(
        "visual_assets",
        manifest.get("visual_assets"),
        edit_dir,
        errors,
        require_provenance=True,
    )
    provenance["audio_assets"] = validate_asset_records(
        "audio_assets", manifest.get("audio_assets"), edit_dir, errors
    )
    control_inputs = manifest.get("control_inputs")
    provenance["control_inputs"] = validate_asset_records(
        "control_inputs", control_inputs, edit_dir, errors
    )
    required_control_labels = {
        "EDL",
        "project",
        "semantic plan",
        "semantic approval",
        "source manifest",
        "packed transcript manifest",
        "packed transcript view",
    }
    if not isinstance(control_inputs, list):
        control_inputs = []
    labels = {
        item.get("label")
        for item in control_inputs
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    }
    missing_control_labels = required_control_labels - labels
    if missing_control_labels:
        errors.append(
            f"render manifest control_inputs is missing required records: "
            f"{sorted(missing_control_labels)}"
        )

    subtitle_mode = manifest.get("subtitle_mode")
    subtitle_item = manifest.get("subtitle_asset")
    subtitle_source = resolved_path(edl.get("subtitles"), edit_dir)
    if subtitle_mode in {"burned", "sidecar"}:
        if not isinstance(subtitle_item, dict):
            errors.append(f"subtitle_mode={subtitle_mode} requires subtitle_asset provenance")
            subtitle_record = None
        else:
            subtitle_path = resolved_path(subtitle_item.get("path"), edit_dir)
            subtitle_record = current_file_record(
                subtitle_path, subtitle_item.get("sha256"), "subtitle_asset", errors
            )
            if subtitle_path != subtitle_source:
                errors.append("subtitle_asset path differs from current EDL subtitles path")
    else:
        subtitle_record = None
        if subtitle_item not in (None, {}):
            errors.append(f"subtitle_mode={subtitle_mode!r} must not declare subtitle_asset")
    provenance["subtitle_asset"] = subtitle_record

    font_assets = manifest.get("font_assets")
    provenance["font_assets"] = validate_asset_records(
        "font_assets", font_assets, edit_dir, errors
    )
    if subtitle_mode == "burned":
        if not isinstance(font_assets, list) or not font_assets:
            errors.append("burned subtitles require resolved font_assets provenance")
        if subtitle_source is not None and subtitle_source.is_file():
            try:
                current_fonts = resolve_subtitle_fonts(subtitle_source)
            except (OSError, ProvenanceError) as exc:
                errors.append(f"cannot resolve current burned-subtitle fonts: {exc}")
            else:
                if current_fonts != font_assets:
                    errors.append("current fc-match subtitle fonts differ from render manifest")
    elif font_assets != []:
        errors.append("non-burned render manifest must declare font_assets as an empty array")

    sidecar_raw = manifest.get("sidecar")
    sidecar_hash = manifest.get("sidecar_sha256")
    if subtitle_mode == "sidecar":
        output_record = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
        output_path = resolved_path(output_record.get("path"), edit_dir)
        sidecar_path = resolved_path(
            sidecar_raw, output_path.parent if output_path else edit_dir
        )
        sidecar_record = current_file_record(
            sidecar_path, sidecar_hash, "subtitle sidecar", errors
        )
        expected_sidecar = (
            output_path.with_suffix(subtitle_source.suffix.lower())
            if output_path and subtitle_source else None
        )
        sidecar_record["expected_path"] = str(expected_sidecar) if expected_sidecar else None
        if sidecar_path != expected_sidecar:
            errors.append("subtitle sidecar path differs from output-derived path")
        if (
            sidecar_record.get("actual_sha256")
            and subtitle_record
            and sidecar_record["actual_sha256"] != subtitle_record.get("actual_sha256")
        ):
            errors.append("subtitle sidecar differs from the current subtitle asset")
    else:
        sidecar_record = None
        if sidecar_raw not in (None, "") or sidecar_hash not in (None, ""):
            errors.append(f"subtitle_mode={subtitle_mode!r} must not declare sidecar provenance")
    provenance["sidecar"] = sidecar_record

    authorization = manifest.get("final_authorization")
    if manifest.get("mode") == "final":
        if not isinstance(authorization, dict):
            errors.append("final render manifest has no preview-approval authorization")
            authorization_record: dict[str, Any] | None = None
        else:
            approval_path = resolved_path(authorization.get("preview_approval"), edit_dir)
            canonical_preview_approval = (
                edit_dir / preview_approval_name(deliverable_id)
            ).resolve()
            if approval_path != canonical_preview_approval:
                errors.append(
                    "final authorization does not point to the current deliverable preview approval"
                )
            authorization_record = current_file_record(
                approval_path,
                authorization.get("preview_approval_sha256"),
                "preview approval authorization",
                errors,
            )
            preview_approval = (
                load_current_object(approval_path, "preview approval", errors)
                if approval_path else {}
            )
            if preview_approval.get("deliverable_id") != deliverable_id:
                errors.append("final authorization preview approval belongs to another deliverable")
            if preview_approval.get("artifact_key") != deliverable_artifact_key:
                errors.append("final authorization preview approval has a non-canonical artifact key")
            if preview_approval.get("renderer") != RENDERER_VERSION:
                errors.append("final authorization preview approval uses a different renderer")
            if preview_approval.get("renderer_identity") != current_identity:
                errors.append("preview-approved renderer identity differs from current v6 identity")
            if authorization.get("renderer_identity_sha256") != current_identity.get("identity_sha256"):
                errors.append("final authorization renderer identity digest is invalid")
            if preview_approval.get("font_assets") != manifest.get("font_assets"):
                errors.append("final fonts differ from preview-approved fonts")
            if preview_approval.get("visual_assets") != manifest.get("visual_assets"):
                errors.append(
                    "final visual asset/provenance records differ from preview approval"
                )
            if authorization.get("visual_assets") != preview_approval.get("visual_assets"):
                errors.append(
                    "final authorization visual assets differ from preview approval"
                )
            if authorization.get("font_assets") != preview_approval.get("font_assets"):
                errors.append("final authorization font assets differ from preview approval")
            preview_path = resolved_path(
                preview_approval.get("preview_file"), edit_dir
            )
            if preview_path is not None and not inside(preview_path, edit_dir):
                errors.append("approved preview path escapes the edit directory")
            declared_preview_path = resolved_path(
                authorization.get("preview_file"), edit_dir
            )
            if declared_preview_path != preview_path:
                errors.append("final authorization preview path differs from preview approval")
            current_file_record(
                preview_path,
                authorization.get("preview_sha256"),
                "approved preview",
                errors,
            )
            approved_preview_sidecar = preview_approval.get("preview_sidecar")
            authorized_preview_sidecar = authorization.get("preview_sidecar")
            if authorized_preview_sidecar != approved_preview_sidecar:
                errors.append(
                    "final authorization preview sidecar differs from preview approval"
                )
            if subtitle_mode == "sidecar":
                if (
                    not isinstance(approved_preview_sidecar, dict)
                    or set(approved_preview_sidecar) != {"path", "sha256"}
                ):
                    errors.append(
                        "preview approval has no canonical preview sidecar path/hash"
                    )
                    approved_preview_sidecar_record = None
                else:
                    approved_preview_sidecar_path = resolved_path(
                        approved_preview_sidecar.get("path"), edit_dir
                    )
                    if (
                        approved_preview_sidecar_path is not None
                        and not inside(approved_preview_sidecar_path, edit_dir)
                    ):
                        errors.append("approved preview sidecar path escapes the edit directory")
                    expected_preview_sidecar = (
                        preview_path.with_suffix(subtitle_source.suffix.lower())
                        if preview_path is not None and subtitle_source is not None
                        else None
                    )
                    if approved_preview_sidecar_path != expected_preview_sidecar:
                        errors.append(
                            "approved preview sidecar path differs from the preview output-derived path"
                        )
                    approved_preview_sidecar_record = current_file_record(
                        approved_preview_sidecar_path,
                        approved_preview_sidecar.get("sha256"),
                        "approved preview sidecar",
                        errors,
                    )
                    if (
                        approved_preview_sidecar_record.get("actual_sha256")
                        and subtitle_record
                        and approved_preview_sidecar_record["actual_sha256"]
                        != subtitle_record.get("actual_sha256")
                    ):
                        errors.append(
                            "approved preview sidecar differs from the current subtitle asset"
                        )
                authorization_record["preview_sidecar"] = {
                    "approved": approved_preview_sidecar,
                    "current": approved_preview_sidecar_record,
                }
            else:
                if approved_preview_sidecar is not None or authorized_preview_sidecar is not None:
                    errors.append(
                        f"subtitle_mode={subtitle_mode!r} must not have approved preview sidecar provenance"
                    )
                authorization_record["preview_sidecar"] = None
            for field, approval_field in (
                ("preview_sha256", "preview_sha256"),
                ("preview_render_manifest_sha256", "render_manifest_sha256"),
                ("preview_qa_report_sha256", "qa_report_sha256"),
            ):
                if authorization.get(field) != preview_approval.get(approval_field):
                    errors.append(
                        f"final authorization {field} differs from preview approval"
                    )
            preview_manifest_path = resolved_path(
                preview_approval.get("render_manifest_file"), edit_dir
            )
            canonical_preview_manifest = (
                edit_dir / render_manifest_name(deliverable_id, "preview")
            ).resolve()
            if preview_manifest_path != canonical_preview_manifest:
                errors.append("preview approval references a non-canonical preview manifest")
            current_file_record(
                preview_manifest_path,
                preview_approval.get("render_manifest_sha256"),
                "approved preview render manifest",
                errors,
            )
            preview_qa_path = resolved_path(preview_approval.get("qa_report"), edit_dir)
            current_file_record(
                preview_qa_path,
                preview_approval.get("qa_report_sha256"),
                "approved preview QA report",
                errors,
            )
            final_output = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
            final_output_path = resolved_path(final_output.get("path"), edit_dir)
            if final_output_path is not None and final_output_path == preview_path:
                errors.append("final output is the approved preview, not a distinct final render")
            authorization_record["preview"] = str(preview_path) if preview_path else None
            authorization_record["preview_sha256"] = authorization.get("preview_sha256")
        provenance["final_authorization"] = authorization_record
    else:
        if authorization not in (None, {}):
            errors.append("non-final render manifest must not contain final_authorization")
        provenance["final_authorization"] = None
    provenance["status"] = "PASS" if not errors else "FAIL"
    return provenance, edl, project


def publish_release_state(
    manifest: dict[str, Any], manifest_path: Path, report_path: Path | None,
    status: str, errors: list[str],
) -> None:
    """Atomically replace any stale final PASS as soon as a final QA run is unsafe."""
    if manifest.get("mode") != "final":
        return
    deliverable_id = manifest.get("deliverable_id")
    try:
        deliverable_artifact_key = artifact_key(deliverable_id)
        canonical_manifest = (
            manifest_path.parent / render_manifest_name(deliverable_id, "final")
        ).resolve()
    except ProvenanceError:
        return
    if (
        manifest.get("artifact_key") != deliverable_artifact_key
        or manifest_path.resolve() != canonical_manifest
    ):
        return
    payload: dict[str, Any] = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "renderer": manifest.get("renderer"),
        "renderer_identity": manifest.get("renderer_identity"),
        "deliverable_id": deliverable_id,
        "artifact_key": deliverable_artifact_key,
        "render_manifest": str(manifest_path),
        "render_manifest_sha256": sha256(manifest_path),
        "semantic_plan_sha256": manifest.get("approval_plan_sha256"),
        "project_sha256": manifest.get("project_sha256"),
        "edl_sha256": manifest.get("edl_sha256"),
        "source_fingerprints": manifest.get("source_fingerprints"),
        "visual_assets": manifest.get("visual_assets") or [],
        "audio_assets": manifest.get("audio_assets") or [],
        "subtitle_asset": manifest.get("subtitle_asset"),
        "font_assets": manifest.get("font_assets") or [],
        "sidecar": manifest.get("sidecar"),
        "sidecar_sha256": manifest.get("sidecar_sha256"),
        "final_authorization": manifest.get("final_authorization"),
        "output": manifest.get("output"),
        "errors": list(errors),
    }
    if report_path is not None and report_path.is_file():
        payload["qa_report"] = str(report_path)
        payload["qa_report_sha256"] = sha256(report_path)
    release_path = manifest_path.parent / release_manifest_name(deliverable_id)
    temporary = release_path.with_name(f".{release_path.name}.part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(release_path)


def validate_subtitles(
    manifest: dict[str, Any], edl: dict[str, Any], output: Path, info: dict[str, Any],
    edl_dir: Path, out_dir: Path, errors: list[str], warnings: list[str],
) -> dict[str, Any]:
    mode = manifest.get("subtitle_mode")
    sidecar_raw = manifest.get("sidecar")
    result: dict[str, Any] = {
        "mode": mode,
        "sidecar": sidecar_raw,
        "source": None,
        "caption_fit": None,
        "caption_fit_log": None,
    }
    if mode not in {"none", "burned", "sidecar"}:
        errors.append(f"invalid manifest subtitle_mode: {mode!r}")
        return result
    if info["subtitle_stream_count"]:
        errors.append("output unexpectedly contains a muxed subtitle stream")

    subtitle_source = path_from_edl(edl.get("subtitles"), edl_dir)
    result["source"] = str(subtitle_source) if subtitle_source else None
    requested = edl.get("subtitle_mode") or ("burned" if subtitle_source else "none")
    if requested != mode:
        warnings.append(
            f"rendered subtitle_mode={mode} differs from EDL request={requested}; "
            "confirm that the no-subtitles override was intentional"
        )

    if mode == "none":
        if sidecar_raw not in (None, ""):
            errors.append("subtitle_mode=none must not declare a sidecar")
        return result

    rendered_duration_s = finite_measurement(info.get("video_duration_s"))
    if rendered_duration_s is None or rendered_duration_s <= 0:
        rendered_duration_s = finite_measurement(info.get("duration_s")) or 0.0
    if subtitle_source is None:
        caption_fit = {
            "version": 1,
            "path": None,
            "format": None,
            "status": "FAIL",
            "checked_cues": 0,
            "rendered_duration_s": rendered_duration_s,
            "duration_tolerance_s": DEFAULT_DURATION_TOLERANCE_S,
            "limits": {},
            "observed": {},
            "errors": ["EDL has no subtitle asset path"],
        }
    else:
        caption_fit = validate_caption_file(
            subtitle_source,
            rendered_duration_s=rendered_duration_s,
            duration_tolerance_s=DEFAULT_DURATION_TOLERANCE_S,
        )
    caption_log = out_dir / "caption_fit.log"
    caption_log.write_text(format_caption_report(caption_fit), encoding="utf-8")
    result["caption_fit"] = caption_fit
    result["caption_fit_log"] = caption_log.name
    if caption_fit.get("status") != "PASS":
        for message in caption_fit.get("errors") or ["unknown caption validation failure"]:
            errors.append(f"subtitle caption-fit validation: {message}")

    if subtitle_source is None or not subtitle_source.is_file():
        errors.append(f"subtitle_mode={mode} requires an existing EDL subtitles file")
    elif subtitle_source.stat().st_size == 0:
        errors.append("EDL subtitles file is empty")
    else:
        result["source_sha256"] = sha256(subtitle_source)

    if mode == "burned":
        if sidecar_raw not in (None, ""):
            errors.append("subtitle_mode=burned must not declare a sidecar")
        return result

    if not isinstance(sidecar_raw, str) or not sidecar_raw.strip():
        errors.append("subtitle_mode=sidecar requires a sidecar path")
        return result
    sidecar = Path(sidecar_raw).expanduser()
    sidecar = (output.parent / sidecar if not sidecar.is_absolute() else sidecar).resolve()
    result["sidecar"] = str(sidecar)
    expected_sidecar = output.with_suffix(subtitle_source.suffix.lower()) if subtitle_source else None
    if expected_sidecar and sidecar != expected_sidecar:
        errors.append(f"sidecar path differs from output-derived path: {sidecar}")
    if sidecar == output:
        errors.append("sidecar path collides with rendered output")
    if not sidecar.is_file():
        errors.append(f"subtitle sidecar not found: {sidecar}")
    elif sidecar.stat().st_size == 0:
        errors.append("subtitle sidecar is empty")
    else:
        result["sidecar_sha256"] = sha256(sidecar)
        if subtitle_source and subtitle_source.is_file() and sha256(sidecar) != sha256(subtitle_source):
            errors.append("subtitle sidecar differs from the approved EDL subtitles file")
    return result


def validate_exact_timing(
    manifest: dict[str, Any], edl: dict[str, Any], clips_dir: Path,
    output_info: dict[str, Any], expected_fps: Fraction, errors: list[str],
) -> dict[str, Any]:
    records = manifest.get("segments")
    ranges = edl.get("ranges")
    if not isinstance(records, list) or not records:
        errors.append("render manifest has no segment records")
        return {}
    if not isinstance(ranges, list) or len(ranges) != len(records):
        errors.append("manifest segment count differs from EDL range count")

    expected_frames = 0
    expected_samples = 0
    edl_cumulative_duration = Fraction(0)
    edl_cumulative_frames = 0
    edl_timeline_valid = True
    segment_results: list[dict[str, Any]] = []
    declared_frame_counts: list[int | None] = []
    declared_paths: set[Path] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"segment record {index} is not an object")
            declared_frame_counts.append(None)
            continue
        if record.get("index") != index:
            errors.append(f"segment indexes are not contiguous at record {index}")
        range_item = (
            ranges[index]
            if isinstance(ranges, list)
            and index < len(ranges)
            and isinstance(ranges[index], dict)
            else None
        )
        if range_item is not None:
            if record.get("range_contract") != range_item:
                errors.append(f"segment {index} range contract differs from current EDL")
            if record.get("source") != range_item.get("source"):
                errors.append(f"segment {index} source differs from current EDL")
            for record_field, range_field in (
                ("source_start_s", "start"),
                ("source_end_s", "end"),
            ):
                try:
                    recorded_value = Fraction(str(record[record_field]))
                    edl_value = Fraction(str(range_item[range_field]))
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    errors.append(
                        f"segment {index} has invalid {record_field}/{range_field} binding"
                    )
                else:
                    if recorded_value != edl_value:
                        errors.append(
                            f"segment {index} {record_field} differs from current EDL {range_field}"
                        )
            if record.get("audio_mode") != range_item.get("audio_mode"):
                errors.append(f"segment {index} audio_mode differs from current EDL")
        frames = optional_int(record.get("frames"))
        samples = optional_int(record.get("audio_samples"))
        declared_frame_counts.append(frames)
        if frames is None or frames <= 0:
            errors.append(f"segment {index} has invalid expected frame count")
            continue
        if samples is None or samples <= 0:
            errors.append(f"segment {index} has invalid expected audio sample count")
            continue
        expected_frames += frames
        expected_samples += samples
        if isinstance(ranges, list) and index < len(ranges) and isinstance(ranges[index], dict):
            try:
                range_duration = Fraction(str(ranges[index]["end"])) - Fraction(str(ranges[index]["start"]))
                if range_duration <= 0:
                    raise ValueError
                edl_cumulative_duration += range_duration
                next_cumulative_frames = max(
                    edl_cumulative_frames + 1,
                    round(edl_cumulative_duration * expected_fps),
                )
                edl_expected_frames = next_cumulative_frames - edl_cumulative_frames
                edl_cumulative_frames = next_cumulative_frames
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                errors.append(f"EDL range {index} has invalid start/end for exact frame validation")
                edl_timeline_valid = False
            else:
                if edl_timeline_valid and frames != edl_expected_frames:
                    errors.append(
                        f"segment {index} frame count differs from EDL frame-lock rule: "
                        f"{frames} vs {edl_expected_frames}"
                    )
        mathematically_expected = round(
            Fraction(expected_frames * AUDIO_RATE * expected_fps.denominator, expected_fps.numerator)
        )
        if expected_samples != mathematically_expected:
            errors.append(
                f"segment {index} cumulative samples mismatch: manifest {expected_samples}, "
                f"frame-locked expectation {mathematically_expected}"
            )

        path_raw = record.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            errors.append(f"segment {index} has no path")
            continue
        path = Path(path_raw).expanduser()
        path = (clips_dir / path if not path.is_absolute() else path).resolve()
        if not inside(path, clips_dir):
            errors.append(f"segment {index} path escapes segments_dir")
        if path in declared_paths:
            errors.append(f"segment {index} reuses another segment path")
        declared_paths.add(path)
        if not path.is_file():
            errors.append(f"segment {index} file not found: {path}")
            continue
        expected_segment_sha256 = record.get("sha256")
        if not valid_sha256(expected_segment_sha256):
            errors.append(f"segment {index} has no valid SHA-256")
        elif sha256(path) != expected_segment_sha256:
            errors.append(f"segment {index} bytes differ from render manifest")
        segment = probe(path)
        actual_frames = segment.get("frame_count")
        actual_samples = segment.get("audio_logical_samples")
        segment_results.append({
            "index": index,
            "path": str(path),
            "expected_frames": frames,
            "actual_frames": actual_frames,
            "expected_audio_samples": samples,
            "actual_audio_samples": actual_samples,
        })
        if actual_frames != frames:
            errors.append(f"segment {index} frame count mismatch: {actual_frames} vs {frames}")
        if actual_samples != samples:
            errors.append(f"segment {index} PCM sample count mismatch: {actual_samples} vs {samples}")
        if segment["video_codec"] != "h264" or segment["audio_codec"] != "pcm_s16le":
            errors.append(f"segment {index} is not the required H.264 + PCM intermediate")
        if segment["audio_sample_rate"] != AUDIO_RATE or segment["audio_channels"] != 2:
            errors.append(f"segment {index} audio must be 48 kHz stereo")
        if (segment["width"], segment["height"]) != (
            output_info["width"], output_info["height"]
        ):
            errors.append(f"segment {index} dimensions differ from output")
        if Fraction(segment["fps_fraction"]) != expected_fps:
            errors.append(f"segment {index} FPS differs from output profile")

    actual_frames = output_info.get("frame_count")
    if actual_frames != expected_frames:
        errors.append(f"output frame count mismatch: {actual_frames} vs {expected_frames}")
    actual_samples = output_info.get("audio_logical_samples")
    sample_delta = None if actual_samples is None else actual_samples - expected_samples
    if actual_samples is None:
        errors.append("could not determine logical output audio sample count")
    elif abs(sample_delta) > AAC_ACCESS_UNIT_SAMPLES:
        errors.append(
            f"output audio sample delta {sample_delta:+d} exceeds one AAC access unit "
            f"({AAC_ACCESS_UNIT_SAMPLES} samples)"
        )

    cut_times = manifest.get("cut_times_s")
    if not isinstance(cut_times, list) or len(cut_times) != max(0, len(records) - 1):
        errors.append("manifest cut_times_s count is inconsistent with segments")
    else:
        cursor = 0
        cursor_valid = True
        for index, (frames, actual) in enumerate(zip(declared_frame_counts[:-1], cut_times)):
            if frames is None or frames <= 0:
                errors.append(f"cannot validate cut_times_s[{index}] without valid segment frames")
                cursor_valid = False
                continue
            if not cursor_valid:
                errors.append(f"cannot validate cut_times_s[{index}] after an invalid prior segment")
                continue
            cursor += frames
            expected = float(Fraction(cursor, 1) / expected_fps)
            try:
                delta = abs(float(actual) - expected)
            except (TypeError, ValueError):
                errors.append(f"cut_times_s[{index}] is not numeric")
                continue
            if delta > 0.001:
                errors.append(
                    f"cut_times_s[{index}] differs from frame-locked boundary by {delta:.6f}s"
                )

    return {
        "expected_frames": expected_frames,
        "actual_frames": actual_frames,
        "expected_audio_samples": expected_samples,
        "actual_audio_logical_samples": actual_samples,
        "audio_sample_delta": sample_delta,
        "aac_tolerance_samples": AAC_ACCESS_UNIT_SAMPLES,
        "segments": segment_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run exact-cut, flash, audio, sync, and loudness QA")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    manifest_path = args.manifest.expanduser().resolve()
    invalidate_release_state_from_manifest_path(
        manifest_path,
        "final QA invocation has not completed successfully; any prior PASS is invalid",
    )
    manifest = load(manifest_path)
    deliverable_id = manifest.get("deliverable_id")
    deliverable_artifact_key = artifact_key(deliverable_id)
    out_dir = (
        args.out_dir
        or default_qa_dir(manifest_path.parent, deliverable_id, str(manifest.get("mode")))
    ).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "release_metrics.json"
    errors: list[str] = []
    warnings: list[str] = []
    publish_release_state(
        manifest, manifest_path, None, "FAIL",
        ["QA run has not completed successfully; any prior release result is invalid"],
    )

    provenance, edl_data, project = validate_input_provenance(
        manifest, manifest_path, errors
    )
    output_record = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    output = resolved_path(output_record.get("path"), manifest_path.parent)
    edl = resolved_path(manifest.get("edl"), manifest_path.parent)
    clips_dir = resolved_path(manifest.get("segments_dir"), manifest_path.parent)
    if errors:
        report = {
            "version": 3,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "FAIL",
            "deliverable_id": deliverable_id,
            "artifact_key": deliverable_artifact_key,
            "manifest": str(manifest_path),
            "render_manifest_sha256": sha256(manifest_path),
            "output": str(output) if output else None,
            "output_sha256": sha256(output) if output and output.is_file() else None,
            "input_provenance": provenance,
            "errors": errors,
            "warnings": warnings,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        publish_release_state(manifest, manifest_path, report_path, "FAIL", errors)
        print(f"SPRUT release QA: FAIL | {report_path}")
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    final_gate: dict[str, Any] = {"required": manifest.get("mode") == "final"}
    if manifest.get("mode") == "final":
        gate = subprocess.run([
            sys.executable,
            str(SCRIPT_DIR / "validate_gate.py"),
            "--edit-dir", str(manifest_path.parent),
            "--phase", "final",
            "--edl", str(edl),
        ], text=True, capture_output=True, check=False)
        gate_log = out_dir / "final_gate.log"
        gate_log.write_text(gate.stdout + gate.stderr, encoding="utf-8")
        final_gate.update({
            "status": "PASS" if gate.returncode == 0 else "FAIL",
            "returncode": gate.returncode,
            "log": gate_log.name,
        })
        if gate.returncode:
            errors.append("final semantic/preview approval gate failed")

    if output is None or not output.is_file():
        raise QAError(f"rendered output not found: {output}")
    if sha256(output) != manifest["output"].get("sha256"):
        errors.append("output hash differs from render manifest")
    if edl is None or not edl.is_file():
        raise QAError(f"EDL not found: {edl}")
    if clips_dir is None or not clips_dir.is_dir():
        raise QAError(f"segments directory not found: {clips_dir}")
    if manifest.get("edl_sha256") != sha256(edl):
        errors.append("EDL hash differs from render manifest")

    helper_python = qa_python()
    exact = subprocess.run([
        helper_python, str(SCRIPT_DIR / "verify_exact_boundary_frames.py"),
        "--video", str(output), "--edl", str(edl), "--clips-dir", str(clips_dir),
        "--manifest", str(manifest_path),
    ], text=True, capture_output=True, check=False)
    (out_dir / "exact_boundary.log").write_text(exact.stdout + exact.stderr, encoding="utf-8")
    if exact.returncode:
        errors.append("exact N-1/N/N+1 boundary check failed")

    cuts_dir = out_dir / "all_cuts"
    cuts = subprocess.run([
        helper_python, str(SCRIPT_DIR / "qa_all_cuts.py"),
        "--video", str(output), "--edl", str(edl), "--clips-dir", str(clips_dir),
        "--out-dir", str(cuts_dir),
    ], text=True, capture_output=True, check=False)
    (out_dir / "all_cuts.log").write_text(cuts.stdout + cuts.stderr, encoding="utf-8")
    if cuts.returncode:
        errors.append("all-cut flash/audio QA failed")
    cut_metrics = load(cuts_dir / "metrics.json") if (cuts_dir / "metrics.json").is_file() else {}
    expected_cuts = max(0, len(manifest.get("segments") or []) - 1)
    tested_cuts = int((cut_metrics.get("timeline") or {}).get("cut_count", -1))
    if tested_cuts != expected_cuts:
        errors.append(f"cut coverage mismatch: expected {expected_cuts}, tested {tested_cuts}")
    blocking = int((cut_metrics.get("summary") or {}).get("blocking_failures", -1))
    if blocking != 0:
        errors.append(f"blocking cut failures: {blocking}")

    info = probe(output)
    profile = manifest.get("profile") or {}
    profile_width = int(profile.get("width", -1))
    profile_height = int(profile.get("height", -1))
    if info["width"] != profile_width or info["height"] != profile_height:
        errors.append("output dimensions differ from manifest profile")
    if min(profile_width, profile_height) < 320 or profile_width % 2 or profile_height % 2:
        errors.append("manifest profile dimensions violate the renderer output contract")
    expected_fps = parse_positive_fraction(profile.get("fps"), "manifest profile FPS")
    if Fraction(info["fps_fraction"]) != expected_fps:
        errors.append(f"output FPS mismatch: {info['fps_fraction']} vs {expected_fps}")
    if info["video_codec"] != "h264" or info["audio_codec"] != "aac":
        errors.append("output codecs must be H.264 video and AAC audio")
    if info["pixel_format"] != "yuv420p":
        errors.append(f"output pixel format must be yuv420p, got {info['pixel_format']}")
    if info["sample_aspect_ratio"] not in {"1:1", "1/1"}:
        errors.append(f"output sample aspect ratio must be 1:1, got {info['sample_aspect_ratio']}")
    if info["field_order"] not in {None, "unknown", "progressive"}:
        errors.append(f"output must be progressive, got field_order={info['field_order']}")
    if abs(info["video_start_s"]) > 0.001 or abs(info["audio_start_s"]) > 0.001:
        errors.append(
            f"output streams must start at zero PTS; video/audio="
            f"{info['video_start_s']:.6f}/{info['audio_start_s']:.6f}s"
        )
    if info["audio_sample_rate"] != AUDIO_RATE or info["audio_channels"] != 2:
        errors.append("output audio must be 48 kHz stereo")
    if "mp4" not in str(info.get("format_name") or "").split(","):
        errors.append(f"output container is not MP4: {info.get('format_name')}")
    if manifest.get("mode") not in {"draft", "preview", "final"}:
        errors.append(f"invalid render mode in manifest: {manifest.get('mode')!r}")
    if manifest.get("renderer") != RENDERER_VERSION:
        errors.append(f"manifest renderer must be exactly {RENDERER_VERSION}")
    if manifest.get("renderer") == RENDERER_VERSION:
        color_values = {
            info["color_space"], info["color_transfer"], info["color_primaries"]
        }
        if color_values != {"bt709"} or info["color_range"] != "tv":
            errors.append("sprut-render-6 output must carry normalized limited-range Rec.709 metadata")
    output_record = manifest.get("output") or {}
    for field in ("width", "height", "video_codec", "audio_codec"):
        if field in output_record and output_record[field] != info[field]:
            errors.append(f"manifest output.{field} differs from the rendered stream")
    if "fps" in output_record and abs(float(output_record["fps"]) - info["fps"]) > 0.000001:
        errors.append("manifest output.fps differs from the rendered stream")
    for field in ("duration_s", "audio_duration_s"):
        if field in output_record and abs(float(output_record[field]) - info[field]) > 0.001:
            errors.append(f"manifest output.{field} differs from the rendered stream")

    exact_timing = validate_exact_timing(
        manifest, edl_data, clips_dir, info, expected_fps, errors
    )
    expected_duration = float(
        Fraction(int(exact_timing.get("expected_frames") or 0), 1) / expected_fps
    )
    if exact_timing and abs(info["video_duration_s"] - expected_duration) > 0.002:
        errors.append(
            f"video duration differs from exact frame duration: "
            f"{info['video_duration_s']:.6f}s vs {expected_duration:.6f}s"
        )
    sync_tolerance = max(0.040, 2 / max(info["fps"], 1))
    if abs(info["av_duration_delta_s"]) > sync_tolerance:
        errors.append(f"A/V duration delta {info['av_duration_delta_s']:+.3f}s exceeds {sync_tolerance:.3f}s")

    levels = loudness(output)
    audio = project.get("audio") or {}
    target_lufs = float(audio.get("target_lufs", -14.0))
    target_tp = float(audio.get("true_peak_dbtp", -2.0))
    if levels["silent"]:
        if silence_is_expected(edl_data, project, edl.parent):
            warnings.append("programme is intentionally silent; LUFS/true-peak targets are not applicable")
        else:
            errors.append("programme is unexpectedly fully silent (loudness and true peak are -inf)")
    else:
        if abs(levels["integrated_lufs"] - target_lufs) > 1.0:
            errors.append(f"integrated loudness {levels['integrated_lufs']:.2f} LUFS is outside target ±1 LU")
        if levels["true_peak_dbtp"] > target_tp + 0.8:
            errors.append(f"true peak {levels['true_peak_dbtp']:.2f} dBTP exceeds allowed encoded headroom")
    subtitle_checks = validate_subtitles(
        manifest, edl_data, output, info, edl.parent, out_dir, errors, warnings
    )
    if int((cut_metrics.get("summary") or {}).get("large_visual_jump_warning", 0)):
        warnings.append("large visual jumps exist; inspect generated contact sheets for editorial intent")

    logs = {"exact_boundary": "exact_boundary.log", "all_cuts": "all_cuts.log"}
    if subtitle_checks.get("caption_fit_log"):
        logs["caption_fit"] = subtitle_checks["caption_fit_log"]
    report = {
        "version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if not errors else "FAIL",
        "deliverable_id": deliverable_id,
        "artifact_key": deliverable_artifact_key,
        "manifest": str(manifest_path),
        "render_manifest_sha256": sha256(manifest_path),
        "output": str(output),
        "output_sha256": sha256(output),
        "input_provenance": provenance,
        "final_gate": final_gate,
        "profile": info,
        "exact_timing": exact_timing,
        "loudness": levels,
        "subtitles": subtitle_checks,
        "cut_coverage": {"expected": expected_cuts, "tested": tested_cuts},
        "blocking_cut_failures": blocking,
        "errors": errors,
        "warnings": warnings,
        "logs": logs,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    publish_release_state(
        manifest, manifest_path, report_path, report["status"], errors
    )
    print(f"SPRUT release QA: {report['status']} | {report_path}")
    for error in errors:
        print(f"ERROR: {error}")
    for warning in warnings:
        print(f"warning: {warning}")
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QAError, ProvenanceError, OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(f"qa_release: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
