#!/usr/bin/env python3
"""Bind final-render permission to one deliverable's exact v6 preview."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_provenance import (
    RENDERER_VERSION,
    ProvenanceError,
    artifact_key,
    default_qa_dir,
    file_sha256,
    preview_approval_name,
    render_manifest_name,
    renderer_identity,
    resolve_subtitle_fonts,
)


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def resolved_path(value: Any, base: Path) -> Path:
    path = Path(str(value or "")).expanduser()
    return (base / path if not path.is_absolute() else path).resolve()


def find_manifest(preview: Path, digest: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    matches: list[Path] = []
    directory = preview.parent
    for _ in range(16):
        for candidate in sorted(directory.glob("render_manifest_*_preview.json")):
            try:
                manifest = load_object(candidate)
                output_record = (
                    manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
                )
                output = resolved_path(output_record.get("path"), candidate.parent)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                manifest.get("mode") == "preview"
                and output == preview
                and output_record.get("sha256") == digest
            ):
                matches.append(candidate.resolve())
        if (directory / "project.json").is_file() or directory.parent == directory:
            break
        directory = directory.parent
    if not matches:
        raise ValueError(
            "no canonical preview render manifest matches this file; pass --manifest explicitly"
        )
    if len(matches) > 1:
        raise ValueError("multiple preview manifests match this file; pass --manifest explicitly")
    return matches[0]


def validate_file_records(
    value: Any, label: str, *, require_provenance: bool = False
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"preview render manifest {label} must be an array")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"preview render manifest {label}[{index}] is invalid")
        path = Path(str(item.get("path") or "")).expanduser().resolve()
        if not path.is_file() or file_sha256(path) != item.get("sha256"):
            raise ValueError(f"preview {label}[{index}] changed or is missing: {path}")
        provenance = item.get("provenance")
        if provenance is None:
            if require_provenance:
                raise ValueError(
                    f"preview {label}[{index}] has no semantic provenance record"
                )
        elif not isinstance(provenance, dict):
            raise ValueError(f"preview {label}[{index}].provenance is invalid")
        else:
            provenance_path = Path(
                str(provenance.get("path") or "")
            ).expanduser().resolve()
            if (
                not provenance_path.is_file()
                or file_sha256(provenance_path) != provenance.get("sha256")
            ):
                raise ValueError(
                    f"preview {label}[{index}] provenance changed or is missing: "
                    f"{provenance_path}"
                )
        records.append(dict(item))
    return records


def validate_preview_sidecar(
    manifest: dict[str, Any],
    preview: Path,
    edit_dir: Path,
    subtitle_asset: Any,
) -> dict[str, str] | None:
    """Return canonical preview-sidecar provenance or fail closed.

    A sidecar is a separately delivered preview artifact.  Binding only the
    subtitle source is insufficient because the copied file shown to the user
    can be replaced between preview QA and approval.
    """
    mode = manifest.get("subtitle_mode")
    sidecar_value = manifest.get("sidecar")
    sidecar_digest = manifest.get("sidecar_sha256")
    if mode != "sidecar":
        if sidecar_value not in (None, "") or sidecar_digest not in (None, ""):
            raise ValueError(f"subtitle_mode={mode!r} must not declare preview sidecar provenance")
        return None
    if not isinstance(subtitle_asset, dict):
        raise ValueError("sidecar preview has no subtitle asset")
    subtitle_path = Path(str(subtitle_asset.get("path") or "")).expanduser().resolve()
    if not subtitle_path.is_file():
        raise ValueError(f"sidecar preview subtitle asset is missing: {subtitle_path}")
    if not isinstance(sidecar_value, str) or not sidecar_value.strip():
        raise ValueError("sidecar preview manifest has no sidecar path")
    sidecar = resolved_path(sidecar_value, preview.parent)
    try:
        relative = sidecar.relative_to(edit_dir)
    except ValueError as exc:
        raise ValueError("preview sidecar must stay inside the EDL edit directory") from exc
    expected = preview.with_suffix(subtitle_path.suffix.lower())
    if sidecar != expected:
        raise ValueError(f"preview sidecar path differs from output-derived path: {expected}")
    if not sidecar.is_file():
        raise ValueError(f"preview sidecar not found: {sidecar}")
    actual = file_sha256(sidecar)
    if sidecar_digest != actual:
        raise ValueError("preview sidecar hash does not match its render manifest")
    if subtitle_asset.get("sha256") != actual:
        raise ValueError("preview sidecar bytes differ from the approved subtitle asset")
    return {"path": relative.as_posix(), "sha256": actual}


def main() -> int:
    parser = argparse.ArgumentParser(description="Record explicit approval of an exact preview")
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument(
        "--manifest", type=Path,
        help="defaults to the unique namespaced preview manifest matching --preview",
    )
    parser.add_argument(
        "--qa-report", type=Path,
        help="defaults to verify/<artifact-key>/preview/release_metrics.json",
    )
    parser.add_argument("--quote", required=True)
    parser.add_argument("--message-ref")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    preview = args.preview.expanduser().resolve()
    if not preview.is_file():
        raise ValueError(f"preview not found: {preview}")
    if len(args.quote.strip()) < 2:
        raise ValueError("approval quote is empty")
    digest = file_sha256(preview)
    manifest_path = find_manifest(preview, digest, args.manifest)
    if not manifest_path.is_file():
        raise ValueError(f"preview render manifest not found: {manifest_path}")
    manifest = load_object(manifest_path)
    if manifest.get("mode") != "preview":
        raise ValueError("approval manifest is not a preview manifest")

    deliverable_id = manifest.get("deliverable_id")
    deliverable_artifact_key = artifact_key(deliverable_id)
    edit_dir = manifest_path.parent.resolve()
    canonical_manifest = edit_dir / render_manifest_name(deliverable_id, "preview")
    if manifest_path != canonical_manifest:
        raise ValueError(f"preview manifest is not canonical for this deliverable: {canonical_manifest}")
    if manifest.get("artifact_key") != deliverable_artifact_key:
        raise ValueError("preview manifest has a non-canonical artifact key")
    try:
        preview_relative = preview.relative_to(edit_dir)
    except ValueError as exc:
        raise ValueError("preview must stay inside the EDL edit directory") from exc

    output_record = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    manifest_output = resolved_path(output_record.get("path"), edit_dir)
    if manifest_output != preview:
        raise ValueError("preview path does not match its render manifest")
    if output_record.get("sha256") != digest:
        raise ValueError("preview hash does not match its render manifest")
    current_identity = renderer_identity()
    if manifest.get("renderer") != RENDERER_VERSION:
        raise ValueError("preview was created by an unsupported renderer version")
    if manifest.get("renderer_identity") != current_identity:
        raise ValueError("preview renderer implementation or toolchain is no longer current")

    for field in ("edl_sha256", "project_sha256", "approval_plan_sha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"preview render manifest has no valid {field}")
    edl_path = resolved_path(manifest.get("edl"), edit_dir)
    project_path = resolved_path(manifest.get("project"), edit_dir)
    if not edl_path.is_file() or file_sha256(edl_path) != manifest["edl_sha256"]:
        raise ValueError("current EDL does not match the preview render manifest")
    edl = load_object(edl_path)
    if edl.get("deliverable_id") != deliverable_id:
        raise ValueError("current EDL deliverable differs from the preview manifest")
    if not project_path.is_file() or file_sha256(project_path) != manifest["project_sha256"]:
        raise ValueError("current project configuration does not match the preview render manifest")

    validate_file_records(
        manifest.get("visual_assets") or [],
        "visual_assets",
        require_provenance=True,
    )
    validate_file_records(manifest.get("audio_assets") or [], "audio_assets")
    subtitle_asset = manifest.get("subtitle_asset")
    if subtitle_asset:
        validate_file_records([subtitle_asset], "subtitle_asset")
    font_assets = validate_file_records(manifest.get("font_assets"), "font_assets")
    subtitle_mode = manifest.get("subtitle_mode")
    if subtitle_mode == "burned":
        if not isinstance(subtitle_asset, dict):
            raise ValueError("burned preview has no subtitle asset")
        subtitle_path = Path(str(subtitle_asset.get("path") or "")).expanduser().resolve()
        if resolve_subtitle_fonts(subtitle_path) != font_assets:
            raise ValueError("current fc-match subtitle fonts differ from the preview manifest")
        if not font_assets:
            raise ValueError("burned preview has no resolved font provenance")
    elif font_assets:
        raise ValueError("non-burned preview must not declare font assets")
    preview_sidecar = validate_preview_sidecar(
        manifest, preview, edit_dir, subtitle_asset
    )

    qa_path = (
        args.qa_report
        or default_qa_dir(edit_dir, deliverable_id, "preview") / "release_metrics.json"
    ).expanduser().resolve()
    if not qa_path.is_file():
        raise ValueError(f"preview QA report not found: {qa_path}")
    qa = load_object(qa_path)
    if qa.get("status") != "PASS":
        raise ValueError("preview cannot be approved before release QA passes")
    if resolved_path(qa.get("manifest"), edit_dir) != manifest_path:
        raise ValueError("QA report belongs to a different render manifest")
    if qa.get("render_manifest_sha256") != file_sha256(manifest_path):
        raise ValueError("QA report was produced for different render-manifest bytes")
    if qa.get("output_sha256") != digest:
        raise ValueError("QA report was produced for a different preview file")
    if qa.get("deliverable_id") != deliverable_id or qa.get("artifact_key") != deliverable_artifact_key:
        raise ValueError("QA report belongs to a different deliverable")
    qa_subtitles = qa.get("subtitles") if isinstance(qa.get("subtitles"), dict) else {}
    if qa_subtitles.get("mode") != subtitle_mode:
        raise ValueError("QA report subtitle mode differs from the preview manifest")
    if preview_sidecar is not None:
        qa_sidecar = resolved_path(qa_subtitles.get("sidecar"), edit_dir)
        expected_sidecar = (edit_dir / preview_sidecar["path"]).resolve()
        if qa_sidecar != expected_sidecar:
            raise ValueError("QA report belongs to a different preview sidecar")
        qa_provenance = (
            qa.get("input_provenance")
            if isinstance(qa.get("input_provenance"), dict)
            else {}
        )
        qa_sidecar_record = (
            qa_provenance.get("sidecar")
            if isinstance(qa_provenance.get("sidecar"), dict)
            else {}
        )
        if (
            qa_sidecar_record.get("matches") is not True
            or qa_sidecar_record.get("actual_sha256") != preview_sidecar["sha256"]
        ):
            raise ValueError("QA report does not attest the exact preview sidecar bytes")

    output = edit_dir / preview_approval_name(deliverable_id)
    payload = {
        "version": 2,
        "deliverable_id": deliverable_id,
        "artifact_key": deliverable_artifact_key,
        "preview_file": preview_relative.as_posix(),
        "preview_sha256": digest,
        "render_manifest_file": manifest_path.name,
        "render_manifest_sha256": file_sha256(manifest_path),
        "renderer": manifest.get("renderer"),
        "renderer_identity": current_identity,
        "edl_sha256": manifest.get("edl_sha256"),
        "project_sha256": manifest.get("project_sha256"),
        "approval_plan_sha256": manifest.get("approval_plan_sha256"),
        "visual_assets": manifest.get("visual_assets") or [],
        "audio_assets": manifest.get("audio_assets") or [],
        "subtitle_asset": subtitle_asset,
        "font_assets": font_assets,
        "preview_sidecar": preview_sidecar,
        "qa_report": str(qa_path),
        "qa_report_sha256": file_sha256(qa_path),
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "user_quote": args.quote.strip(),
        "user_message_ref": args.message_ref,
    }
    if output.exists() and not args.replace:
        existing = load_object(output)
        stable_fields = (
            "deliverable_id", "artifact_key", "preview_sha256",
            "render_manifest_sha256", "renderer_identity", "font_assets",
            "preview_sidecar", "qa_report_sha256",
        )
        if all(existing.get(field) == payload.get(field) for field in stable_fields):
            print(f"preview approval already matches: {output}")
            return 0
        raise ValueError(
            "a different preview approval exists; use --replace only after new explicit approval"
        )
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"preview approval recorded: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ProvenanceError, ValueError, json.JSONDecodeError) as exc:
        print(f"record_preview_approval: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
