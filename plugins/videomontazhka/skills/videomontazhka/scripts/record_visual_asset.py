#!/usr/bin/env python3
"""Record approval-bound provenance for an externally generated local visual."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from visual_asset_provenance import (
    EXTERNAL_PROVENANCE_TYPE,
    FileSnapshot,
    VisualProvenanceError,
    assert_snapshots_current,
    atomic_write_json,
    build_external_visual_provenance,
    capture_external_recorder_snapshots,
    file_sha256,
    invalidate_provenance,
    load_approved_visual_plan_item,
    provenance_path_for,
    validate_declared_visible_text,
    verify_visual_asset_provenance,
)


def resolved_under_edit(edit_dir: Path, value: Path, label: str) -> Path:
    raw = value.expanduser()
    candidate = raw if raw.is_absolute() else edit_dir / raw
    return path_under_edit(edit_dir, candidate, label)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bind a local HyperFrames, Manim, or user-supplied visual to an approved "
            "semantic_plan.visual_plan item without claiming pixel-level OCR"
        )
    )
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--visual-id", required=True)
    parser.add_argument(
        "--declared-visible-text",
        help="human declaration of every visible word, required when approved_text is non-null",
    )
    parser.add_argument(
        "--source-spec",
        type=Path,
        help="optional local HTML/JSON/Python/Manim source used to generate the visual",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    edit_dir = canonical_edit_dir(args.edit_dir)
    asset = resolved_under_edit(edit_dir, args.asset, "external visual asset")
    if not asset.is_file():
        raise VisualProvenanceError(f"external visual asset is missing: {asset}")
    sidecar = path_under_edit(
        edit_dir, provenance_path_for(asset), "external visual provenance sidecar"
    )
    source_spec = (
        resolved_under_edit(edit_dir, args.source_spec, "external visual source spec")
        if args.source_spec is not None
        else None
    )
    if source_spec is not None and not source_spec.is_file():
        raise VisualProvenanceError(f"external visual source spec is missing: {source_spec}")
    reserved = {
        sidecar,
        (edit_dir / "semantic_plan.json").resolve(),
        (edit_dir / "approval.json").resolve(),
    }
    if asset in reserved:
        raise VisualProvenanceError("external visual asset collides with a control/provenance file")
    if source_spec is not None and source_spec in reserved | {asset}:
        raise VisualProvenanceError(
            "external visual source spec must differ from the asset and control files"
        )
    if sidecar.exists() and not args.force:
        raise VisualProvenanceError(
            f"provenance sidecar exists; use --force to replace: {sidecar}"
        )

    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    if approved.asset_type == "none":
        raise VisualProvenanceError(
            "approved visual asset_type='none' cannot be recorded as an external overlay"
        )
    validate_declared_visible_text(approved.approved_text, args.declared_visible_text)
    recorder_snapshot, helper_snapshot = capture_external_recorder_snapshots(Path(__file__))
    asset_snapshot = FileSnapshot(asset, file_sha256(asset))
    source_spec_snapshot = (
        FileSnapshot(source_spec, file_sha256(source_spec))
        if source_spec is not None
        else None
    )
    control_snapshots = [
        asset_snapshot,
        approved.plan_snapshot,
        approved.approval_snapshot,
        recorder_snapshot,
        helper_snapshot,
    ]
    if source_spec_snapshot is not None:
        control_snapshots.append(source_spec_snapshot)
    assert_snapshots_current(control_snapshots)
    payload = build_external_visual_provenance(
        edit_dir=edit_dir,
        asset=asset,
        approved=approved,
        declared_visible_text=args.declared_visible_text,
        recorder_snapshot=recorder_snapshot,
        helper_snapshot=helper_snapshot,
        source_spec_snapshot=source_spec_snapshot,
    )
    assert_snapshots_current(control_snapshots)
    atomic_write_json(sidecar, payload)
    try:
        verify_visual_asset_provenance(edit_dir, sidecar, asset_path=asset)
    except (OSError, VisualProvenanceError) as exc:
        invalidate_provenance(
            sidecar,
            f"post-write verification failed: {exc}",
            EXTERNAL_PROVENANCE_TYPE,
        )
        raise
    print(f"external visual provenance recorded: {sidecar}")
    print("review requirement: full preview user approval")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, OSError, VisualProvenanceError, ValueError) as exc:
        print(f"record_visual_asset: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
