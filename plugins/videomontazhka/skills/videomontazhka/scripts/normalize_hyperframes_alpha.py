#!/usr/bin/env python3
"""Normalize a HyperFrames VP9-alpha WebM to approval-bound ProRes 4444."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from visual_asset_provenance import (
    VisualProvenanceError,
    atomic_write_json,
    file_sha256,
    load_approved_visual_plan_item,
)


NORMALIZER_VERSION = "sprut-hyperframes-alpha-normalizer-1"


class AlphaNormalizationError(RuntimeError):
    pass


def command_output(command: Sequence[str], label: str) -> str:
    try:
        result = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise AlphaNormalizationError(f"{label} could not start: {exc}") from exc
    if result.returncode:
        details = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value and value.strip()
        )
        raise AlphaNormalizationError(
            f"{label} failed with exit {result.returncode}"
            + (f":\n{details}" if details else "")
        )
    return result.stdout


def local_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise AlphaNormalizationError("local ffmpeg and ffprobe are required")
    decoders = command_output([ffmpeg, "-hide_banner", "-decoders"], "inspect FFmpeg decoders")
    if "libvpx-vp9" not in decoders:
        raise AlphaNormalizationError(
            "this FFmpeg build lacks the libvpx-vp9 decoder required to preserve WebM alpha"
        )
    return ffmpeg, ffprobe


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    output = command_output(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        f"probe {path.name}",
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AlphaNormalizationError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(value, dict):
        raise AlphaNormalizationError(f"ffprobe returned an invalid object for {path}")
    return value


def one_video_stream(value: dict[str, Any], path: Path) -> dict[str, Any]:
    streams = [
        item
        for item in value.get("streams", [])
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(streams) != 1:
        raise AlphaNormalizationError(
            f"expected exactly one video stream in {path}, found {len(streams)}"
        )
    return streams[0]


def numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if isinstance(raw, str) and "/" in raw:
            numerator, denominator = raw.split("/", 1)
            try:
                result = float(numerator) / float(denominator)
            except (ValueError, ZeroDivisionError):
                continue
            if math.isfinite(result) and result > 0:
                return result
    return None


def duration(value: dict[str, Any], stream: dict[str, Any]) -> float | None:
    for raw in (stream.get("duration"), (value.get("format") or {}).get("duration")):
        result = numeric(raw)
        if result is not None and result > 0:
            return result
    return None


def alpha_mode(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return None
    for key, value in tags.items():
        if str(key).casefold() == "alpha_mode":
            return str(value)
    return None


def normalize_command(ffmpeg: str, source: Path, output: Path) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-n",
        "-c:v",
        "libvpx-vp9",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4",
        "-pix_fmt",
        "yuva444p10le",
        "-alpha_bits",
        "16",
        "-map_metadata",
        "-1",
        str(output),
    ]


def verify_pair(source: Path, output: Path, ffprobe: str) -> dict[str, Any]:
    source_probe = probe(source, ffprobe)
    source_stream = one_video_stream(source_probe, source)
    if source_stream.get("codec_name") != "vp9":
        raise AlphaNormalizationError("HyperFrames alpha input must use VP9 WebM")
    if alpha_mode(source_stream) != "1":
        raise AlphaNormalizationError("input does not declare WebM ALPHA_MODE=1")

    output_probe = probe(output, ffprobe)
    output_stream = one_video_stream(output_probe, output)
    audio = [
        item
        for item in output_probe.get("streams", [])
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if audio:
        raise AlphaNormalizationError("normalized overlay must not contain audio")
    if output_stream.get("codec_name") != "prores" or "4444" not in str(
        output_stream.get("profile") or ""
    ):
        raise AlphaNormalizationError("normalized output is not ProRes 4444")
    if not str(output_stream.get("pix_fmt") or "").startswith("yuva"):
        raise AlphaNormalizationError("normalized output does not expose an alpha pixel format")
    for field in ("width", "height"):
        if int(output_stream.get(field) or 0) != int(source_stream.get(field) or 0):
            raise AlphaNormalizationError(f"normalized output changed {field}")
    source_fps = fps(source_stream)
    output_fps = fps(output_stream)
    if source_fps and output_fps and abs(source_fps - output_fps) > max(0.02, source_fps * 0.001):
        raise AlphaNormalizationError("normalized output changed frame rate")
    source_duration = duration(source_probe, source_stream)
    output_duration = duration(output_probe, output_stream)
    if source_duration and output_duration:
        tolerance = max(0.05, 1.5 / (source_fps or 30.0))
        if abs(source_duration - output_duration) > tolerance:
            raise AlphaNormalizationError("normalized output changed duration")
    return {
        "source": {
            "codec": source_stream.get("codec_name"),
            "alpha_mode": alpha_mode(source_stream),
            "width": source_stream.get("width"),
            "height": source_stream.get("height"),
            "fps": source_fps,
            "duration_s": source_duration,
        },
        "output": {
            "codec": output_stream.get("codec_name"),
            "profile": output_stream.get("profile"),
            "pixel_format": output_stream.get("pix_fmt"),
            "width": output_stream.get("width"),
            "height": output_stream.get("height"),
            "fps": output_fps,
            "duration_s": output_duration,
            "audio_streams": 0,
        },
    }


def normalize_project(args: argparse.Namespace) -> tuple[Path, Path]:
    edit_dir = canonical_edit_dir(args.edit_dir)
    source = path_under_edit(edit_dir, args.input, "HyperFrames alpha input")
    output = path_under_edit(edit_dir, args.output, "ProRes 4444 output")
    report_path = output.with_suffix(output.suffix + ".alpha-normalization.json")
    path_under_edit(edit_dir, report_path, "alpha normalization report")
    if not source.is_file() or source.is_symlink() or source.suffix.lower() != ".webm":
        raise AlphaNormalizationError("--input must be a regular WebM file under edit/")
    if output.suffix.lower() != ".mov":
        raise AlphaNormalizationError("--output must use .mov")
    if output.exists() or report_path.exists():
        raise AlphaNormalizationError("output or normalization report already exists")

    # No output directory or temporary asset exists before both gates pass.
    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    ffmpeg, ffprobe = local_tools()
    source_hash = file_sha256(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.stem}-alpha-", dir=str(output.parent)) as tmp:
        temporary = Path(tmp) / "normalized.mov"
        command_output(normalize_command(ffmpeg, source, temporary), "normalize HyperFrames alpha")
        checks = verify_pair(source, temporary, ffprobe)
        if file_sha256(source) != source_hash:
            raise AlphaNormalizationError("HyperFrames input changed during normalization")
        output_hash = file_sha256(temporary)
        os.replace(temporary, output)

    report = {
        "version": 1,
        "type": "sprut_hyperframes_alpha_normalization",
        "generator": {
            "version": NORMALIZER_VERSION,
            "path": str(Path(__file__).resolve()),
            "sha256": file_sha256(Path(__file__).resolve()),
        },
        "visual": {
            "visual_id": approved.visual_id,
            "section_id": approved.section_id,
            "meaning_ids": list(approved.meaning_ids),
            "approved_text": approved.approved_text,
            "semantic_plan_sha256": approved.plan_snapshot.sha256,
            "approval_sha256": approved.approval_snapshot.sha256,
        },
        "input": {"path": str(source), "sha256": source_hash},
        "output": {"path": str(output), "sha256": output_hash},
        "decoder": "libvpx-vp9",
        "network_calls": 0,
        "audio_policy": "video_only_no_audio",
        "checks": checks,
    }
    try:
        atomic_write_json(report_path, report)
    except Exception:
        if output.exists() and file_sha256(output) == output_hash:
            output.unlink()
        raise
    return output, report_path


def self_test() -> dict[str, Any]:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise AlphaNormalizationError("Pillow is required for the synthetic self-test") from exc
    ffmpeg, ffprobe = local_tools()
    with tempfile.TemporaryDirectory(prefix="sprut-alpha-normalizer-self-test-") as tmp:
        root = Path(tmp)
        frames = root / "frames"
        frames.mkdir()
        for index in range(6):
            image = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (30 + index * 8, 40, 220 + index * 8, 140),
                radius=18,
                fill=(255, 106, 0, 210),
            )
            image.save(frames / f"frame-{index:03d}.png")
        source = root / "alpha.webm"
        output = root / "alpha.mov"
        command_output(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-framerate",
                "12",
                "-i",
                str(frames / "frame-%03d.png"),
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                "-auto-alt-ref",
                "0",
                str(source),
            ],
            "encode synthetic VP9 alpha",
        )
        command_output(normalize_command(ffmpeg, source, output), "normalize synthetic alpha")
        checks = verify_pair(source, output, ffprobe)
        return {
            "version": 1,
            "self_test": "PASS",
            "synthetic_only": True,
            "network_calls": 0,
            "checks": checks,
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Normalize approval-bound HyperFrames VP9 alpha to ProRes 4444"
    )
    result.add_argument("--self-test", action="store_true")
    result.add_argument("--edit-dir", type=Path)
    result.add_argument("--visual-id")
    result.add_argument("--input", type=Path)
    result.add_argument("--output", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        if any(value is not None for value in (args.edit_dir, args.visual_id, args.input, args.output)):
            raise AlphaNormalizationError("--self-test cannot be combined with project arguments")
        print(json.dumps(self_test(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if any(value is None for value in (args.edit_dir, args.visual_id, args.input, args.output)):
        raise AlphaNormalizationError(
            "project mode requires --edit-dir, --visual-id, --input, and --output"
        )
    output, report = normalize_project(args)
    print(f"normalized ProRes 4444: {output}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AlphaNormalizationError, AssetGateError, OSError, VisualProvenanceError) as exc:
        print(f"normalize_hyperframes_alpha: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
