#!/usr/bin/env python3
"""Build an approval-bound contact sheet for one rendered visual asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from visual_asset_provenance import (
    FileSnapshot,
    VisualProvenanceError,
    assert_snapshots_current,
    atomic_write_json,
    file_sha256,
    load_approved_visual_plan_item,
)


PREVIEW_VERSION = 1
PREVIEW_TYPE = "sprut_visual_preview_sheet"
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


class VisualPreviewError(RuntimeError):
    pass


def _resolved_under_edit(edit_dir: Path, value: Path, label: str) -> Path:
    raw = value.expanduser()
    candidate = raw if raw.is_absolute() else edit_dir / raw
    return path_under_edit(edit_dir, candidate, label)


def _parse_timestamps(values: Iterable[str]) -> list[float]:
    timestamps: list[float] = []
    for raw in values:
        try:
            value = float(raw)
        except ValueError as exc:
            raise VisualPreviewError(f"invalid timestamp: {raw!r}") from exc
        if not math.isfinite(value) or value < 0:
            raise VisualPreviewError("timestamps must be finite and non-negative")
        timestamps.append(value)
    if len(timestamps) not in {3, 4}:
        raise VisualPreviewError("provide exactly 3 or 4 explicit --timestamp values")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise VisualPreviewError("timestamps must be unique and strictly increasing")
    return timestamps


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-_")
    return (slug[:48].rstrip("-_") or "visual")


def _tool(name: str) -> tuple[Path, str]:
    executable = shutil.which(name)
    if executable is None:
        raise VisualPreviewError(f"missing executable: {name}")
    path = Path(executable).resolve()
    result = subprocess.run(
        [str(path), "-version"], text=True, capture_output=True, check=False
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise VisualPreviewError(f"cannot identify {name}: {detail[-1000:]}")
    lines = (result.stdout + result.stderr).splitlines()
    return path, (lines[0].strip() if lines else "")


def _rate(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _probe(ffprobe: Path, asset: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,duration:format=duration",
            "-of",
            "json",
            str(asset),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise VisualPreviewError(
            f"ffprobe failed for rendered visual: {(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams")
        stream = streams[0] if isinstance(streams, list) and streams else None
        if not isinstance(stream, dict):
            raise ValueError("no video stream")
        width = int(stream["width"])
        height = int(stream["height"])
        duration_value = payload.get("format", {}).get("duration") or stream.get("duration")
        duration = float(duration_value)
        fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))
    except (KeyError, TypeError, ValueError) as exc:
        raise VisualPreviewError(f"cannot parse rendered visual metadata: {exc}") from exc
    if width < 1 or height < 1 or not math.isfinite(duration) or duration <= 0 or fps <= 0:
        raise VisualPreviewError("rendered visual has invalid dimensions, duration, or fps")
    return {
        "codec_name": stream.get("codec_name"),
        "width": width,
        "height": height,
        "pix_fmt": stream.get("pix_fmt"),
        "fps": fps,
        "duration_s": duration,
    }


def _extract_frame(ffmpeg: Path, asset: Path, timestamp: float, output: Path) -> None:
    result = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(asset),
            "-ss",
            f"{timestamp:.6f}",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-y",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        detail = (result.stderr or result.stdout).strip()
        raise VisualPreviewError(
            f"ffmpeg could not extract frame at {timestamp:.3f}s: {detail[-1000:]}"
        )


def _font(size: int, *, bold: bool = False) -> Any:
    from PIL import ImageFont

    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
        if bold
        else Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if bold
        else Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _checkerboard(width: int, height: int, tile: int = 16) -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), (42, 42, 42, 255))
    draw = ImageDraw.Draw(image)
    alternate = (68, 68, 68, 255)
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            if (x // tile + y // tile) % 2:
                draw.rectangle(
                    (x, y, min(width, x + tile) - 1, min(height, y + tile) - 1),
                    fill=alternate,
                )
    return image


def _contact_sheet(
    frame_paths: list[Path], timestamps: list[float], visual_id: str, asset_name: str, output: Path
) -> tuple[int, int]:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise VisualPreviewError("Pillow is required to build the contact sheet") from exc

    columns = 2
    rows = math.ceil(len(frame_paths) / columns)
    cell_width, cell_height = 640, 410
    header_height = 76
    sheet = Image.new(
        "RGB", (columns * cell_width, header_height + rows * cell_height), (7, 7, 7)
    )
    draw = ImageDraw.Draw(sheet)
    title_font = _font(26, bold=True)
    label_font = _font(22, bold=True)
    meta_font = _font(17)
    draw.text((24, 14), f"VISUAL PREVIEW — {visual_id}", font=title_font, fill=(255, 106, 0))
    draw.text((24, 46), asset_name, font=meta_font, fill=(190, 190, 190))

    for index, (frame_path, timestamp) in enumerate(zip(frame_paths, timestamps)):
        column, row = index % columns, index // columns
        cell_x, cell_y = column * cell_width, header_height + row * cell_height
        draw.rectangle(
            (cell_x, cell_y, cell_x + cell_width - 1, cell_y + cell_height - 1),
            outline=(48, 48, 48),
            width=2,
        )
        with Image.open(frame_path) as opened:
            frame = opened.convert("RGBA")
        max_width, max_height = cell_width - 40, cell_height - 72
        scale = min(max_width / frame.width, max_height / frame.height)
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        frame = frame.resize(size, Image.Resampling.LANCZOS)
        background = _checkerboard(*size)
        background.alpha_composite(frame)
        position = (
            cell_x + (cell_width - size[0]) // 2,
            cell_y + 16 + (max_height - size[1]) // 2,
        )
        sheet.paste(background.convert("RGB"), position)
        draw.text(
            (cell_x + 20, cell_y + cell_height - 42),
            f"{index + 1:02d}   {timestamp:.3f} s",
            font=label_font,
            fill=(255, 255, 255),
        )

    sheet.save(output, format="PNG", optimize=True)
    return sheet.size


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract 3–4 approval-bound frames and build a labeled visual contact sheet"
    )
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument("--visual-id", required=True)
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument(
        "--timestamp",
        action="append",
        required=True,
        metavar="SECONDS",
        help="repeat exactly 3 or 4 times in strictly increasing order",
    )
    args = parser.parse_args()

    timestamps = _parse_timestamps(args.timestamp)
    edit_dir = canonical_edit_dir(args.edit_dir)
    asset = _resolved_under_edit(edit_dir, args.asset, "rendered visual asset")
    if not asset.is_file():
        raise VisualPreviewError(f"rendered visual asset is missing: {asset}")
    if asset.suffix.casefold() not in VIDEO_SUFFIXES:
        raise VisualPreviewError(f"unsupported rendered visual extension: {asset.suffix}")

    # The semantic gate and visual lookup intentionally precede every ffprobe/ffmpeg call
    # and every output-directory creation. A stale or missing approval leaves no frames.
    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    if approved.asset_type == "none":
        raise VisualPreviewError("approved visual asset_type='none' has no previewable asset")

    asset_snapshot = FileSnapshot(asset, file_sha256(asset))
    snapshots = [asset_snapshot, approved.plan_snapshot, approved.approval_snapshot]
    assert_snapshots_current(snapshots)
    ffmpeg, ffmpeg_version = _tool("ffmpeg")
    ffprobe, ffprobe_version = _tool("ffprobe")
    media = _probe(ffprobe, asset)
    if timestamps[-1] >= media["duration_s"]:
        raise VisualPreviewError(
            f"timestamp {timestamps[-1]:.3f}s is outside asset duration "
            f"{media['duration_s']:.3f}s"
        )

    signature = hashlib.sha256(
        json.dumps(
            {
                "asset_sha256": asset_snapshot.sha256,
                "visual_id": approved.visual_id,
                "timestamps": timestamps,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    verify_dir = edit_dir / "verify"
    output_dir = verify_dir / f"visual_preview_{_slug(approved.visual_id)}_{signature}"
    if output_dir.exists():
        raise VisualPreviewError(f"visual preview output already exists: {output_dir}")
    verify_dir.mkdir(parents=True, exist_ok=True)
    staging = verify_dir / f".{output_dir.name}.{os.getpid()}.part"
    if staging.exists():
        raise VisualPreviewError(f"staging path already exists: {staging}")
    staging.mkdir()

    try:
        frame_paths: list[Path] = []
        for index, timestamp in enumerate(timestamps, start=1):
            frame = staging / f"frame_{index:02d}.png"
            _extract_frame(ffmpeg, asset, timestamp, frame)
            frame_paths.append(frame)
        sheet_path = staging / "contact_sheet.png"
        sheet_width, sheet_height = _contact_sheet(
            frame_paths, timestamps, approved.visual_id, asset.name, sheet_path
        )
        assert_snapshots_current(snapshots)

        final_frames = [output_dir / path.name for path in frame_paths]
        final_sheet = output_dir / sheet_path.name
        manifest = {
            "version": PREVIEW_VERSION,
            "type": PREVIEW_TYPE,
            "status": "PASS",
            "visual_id": approved.visual_id,
            "section_id": approved.section_id,
            "meaning_ids": list(approved.meaning_ids),
            "purpose": approved.purpose,
            "treatment": approved.treatment,
            "asset_type": approved.asset_type,
            "asset": {"path": str(asset), "sha256": asset_snapshot.sha256},
            "semantic_plan": {
                "path": str(approved.plan_snapshot.path),
                "sha256": approved.plan_snapshot.sha256,
            },
            "approval": {
                "path": str(approved.approval_snapshot.path),
                "sha256": approved.approval_snapshot.sha256,
                "proposal_sha256": approved.plan_snapshot.sha256,
            },
            "media": media,
            "timestamps_s": timestamps,
            "frames": [
                {
                    "index": index,
                    "timestamp_s": timestamp,
                    "path": str(final_path),
                    "sha256": file_sha256(staging_path),
                }
                for index, (timestamp, staging_path, final_path) in enumerate(
                    zip(timestamps, frame_paths, final_frames), start=1
                )
            ],
            "contact_sheet": {
                "path": str(final_sheet),
                "sha256": file_sha256(sheet_path),
                "width": sheet_width,
                "height": sheet_height,
            },
            "generator": {
                "path": str(Path(__file__).resolve()),
                "sha256": file_sha256(Path(__file__).resolve()),
            },
            "tools": {
                "ffmpeg": {"path": str(ffmpeg), "version": ffmpeg_version},
                "ffprobe": {"path": str(ffprobe), "version": ffprobe_version},
            },
        }
        atomic_write_json(staging / "manifest.json", manifest)
        assert_snapshots_current(snapshots)
        os.replace(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(f"visual preview sheet: {output_dir / 'contact_sheet.png'}")
    print(f"visual preview manifest: {output_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, OSError, VisualPreviewError, VisualProvenanceError, ValueError) as exc:
        print(f"build_visual_preview_sheet: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
