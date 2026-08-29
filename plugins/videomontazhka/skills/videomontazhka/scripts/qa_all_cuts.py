#!/usr/bin/env python3
"""QA every edit boundary in a rendered long-form preview.

Cut times are derived from the *actual encoded durations* of the ordered
segment files, not from requested EDL ranges.  A normal visual jump is only a
warning.  The process exits with status 1 only when a cut is flagged for an
extreme black/flash frame or an obvious boundary audio spike.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont


MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
FRAME_WIDTH = 320
FRAME_HEIGHT = 180
FRAME_OFFSET_S = 0.080
AUDIO_HALF_WINDOW_S = 0.250
AUDIO_RATE = 48_000

# Conservative thresholds: ordinary screencast jump cuts should warn, not fail.
THRESHOLDS = {
    "near_black_rgb_level": 12,
    "near_white_rgb_level": 248,
    "black_fraction": 0.997,
    "black_mean_luma": 0.012,
    "white_fraction": 0.997,
    "white_mean_luma": 0.985,
    "flash_brightness_delta": 0.78,
    "flash_frame_difference": 0.65,
    "large_visual_jump_difference": 0.18,
    "large_visual_jump_brightness_delta": 0.28,
    "audio_boundary_step": 0.22,
    "audio_step_over_context_ratio": 6.0,
    "audio_transient_peak": 0.97,
    "audio_transient_over_context_db": 14.0,
}


class QAError(RuntimeError):
    """Raised for invalid inputs or failed external media operations."""


def command_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise QAError(f"required executable not found on PATH: {name}")
    return path


def run_capture(command: Sequence[str]) -> bytes:
    result = subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if len(detail) > 2_000:
            detail = detail[-2_000:]
        raise QAError(f"command failed ({result.returncode}): {' '.join(command[:8])}\n{detail}")
    return result.stdout


def ffprobe_json(ffprobe: str, media: Path) -> dict[str, Any]:
    output = run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media),
        ]
    )
    try:
        return json.loads(output.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise QAError(f"ffprobe returned invalid JSON for {media}: {exc}") from exc


def positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def parse_rate(value: Any) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        denominator_f = float(denominator)
        if denominator_f == 0:
            return None
        rate = float(numerator) / denominator_f
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def duration_from_probe(probe: dict[str, Any]) -> float:
    format_info = probe.get("format") or {}
    duration = positive_float(format_info.get("duration"))
    if duration is not None:
        return duration

    for stream in probe.get("streams") or []:
        duration = positive_float(stream.get("duration"))
        if duration is not None:
            return duration
        frames = positive_float(stream.get("nb_frames"))
        fps = parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate"))
        if frames is not None and fps is not None:
            return frames / fps
    raise QAError("could not determine media duration from ffprobe")


def probe_video(ffprobe: str, video: Path) -> dict[str, Any]:
    probe = ffprobe_json(ffprobe, video)
    streams = probe.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise QAError(f"no video stream found: {video}")
    fps = parse_rate(video_stream.get("avg_frame_rate")) or parse_rate(
        video_stream.get("r_frame_rate")
    )
    return {
        "duration_s": duration_from_probe(probe),
        "fps": fps,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def load_edl_ranges(edl_path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(edl_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QAError(f"could not read EDL {edl_path}: {exc}") from exc

    if isinstance(data, list):
        ranges = data
    elif isinstance(data, dict):
        ranges = data.get("ranges")
    else:
        ranges = None
    if not isinstance(ranges, list) or not ranges:
        raise QAError("EDL must contain a non-empty 'ranges' array")
    if not all(isinstance(item, dict) for item in ranges):
        raise QAError("every EDL range must be an object")
    return ranges


def natural_key(path: Path) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def discover_clips(clips_dir: Path, expected_count: int) -> list[Path]:
    media = sorted(
        (path for path in clips_dir.iterdir() if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS),
        key=natural_key,
    )
    if len(media) != expected_count:
        raise QAError(
            f"EDL has {expected_count} ranges but clips-dir contains {len(media)} media files: "
            f"{clips_dir}"
        )

    index_pattern = re.compile(r"^(?:seg(?:ment)?|clip)[_-]?(\d+)(?:[_\-.]|$)", re.IGNORECASE)
    indexed: list[tuple[int, Path]] = []
    for path in media:
        match = index_pattern.match(path.name)
        if match:
            indexed.append((int(match.group(1)), path))

    if len(indexed) == len(media):
        indexes = [index for index, _ in indexed]
        if len(set(indexes)) != len(indexes):
            raise QAError("duplicate numeric segment indexes in clips-dir")
        indexed.sort(key=lambda item: item[0])
        ordered_indexes = [index for index, _ in indexed]
        expected_indexes = list(range(expected_count))
        if ordered_indexes != expected_indexes:
            raise QAError(
                f"segment indexes must be contiguous 0..{expected_count - 1}; found {ordered_indexes}"
            )
        return [path for _, path in indexed]

    # Fallback supports custom names, while deterministic natural sorting keeps order explicit.
    return media


def extract_rgb_frame(ffmpeg: str, video: Path, time_s: float) -> np.ndarray:
    raw = run_capture(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, time_s):.6f}",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=bilinear",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    expected = FRAME_WIDTH * FRAME_HEIGHT * 3
    if len(raw) < expected:
        raise QAError(
            f"short decoded frame at {time_s:.6f}s: expected {expected} bytes, got {len(raw)}"
        )
    return np.frombuffer(raw[:expected], dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH, 3).copy()


def frame_stats(frame: np.ndarray) -> dict[str, float]:
    rgb = frame.astype(np.float32)
    luma = (0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]) / 255.0
    near_black = np.max(frame, axis=2) <= THRESHOLDS["near_black_rgb_level"]
    near_white = np.min(frame, axis=2) >= THRESHOLDS["near_white_rgb_level"]
    return {
        "mean_luma": float(np.mean(luma)),
        "near_black_fraction": float(np.mean(near_black)),
        "near_white_fraction": float(np.mean(near_white)),
    }


def visual_metrics(before: np.ndarray, after: np.ndarray) -> tuple[dict[str, Any], dict[str, bool]]:
    before_stats = frame_stats(before)
    after_stats = frame_stats(after)
    normalized_difference = float(
        np.mean(np.abs(before.astype(np.float32) - after.astype(np.float32))) / 255.0
    )
    brightness_delta = abs(before_stats["mean_luma"] - after_stats["mean_luma"])

    before_black = (
        before_stats["near_black_fraction"] >= THRESHOLDS["black_fraction"]
        and before_stats["mean_luma"] <= THRESHOLDS["black_mean_luma"]
    )
    after_black = (
        after_stats["near_black_fraction"] >= THRESHOLDS["black_fraction"]
        and after_stats["mean_luma"] <= THRESHOLDS["black_mean_luma"]
    )
    before_white = (
        before_stats["near_white_fraction"] >= THRESHOLDS["white_fraction"]
        and before_stats["mean_luma"] >= THRESHOLDS["white_mean_luma"]
    )
    after_white = (
        after_stats["near_white_fraction"] >= THRESHOLDS["white_fraction"]
        and after_stats["mean_luma"] >= THRESHOLDS["white_mean_luma"]
    )
    pure_white_flash = (
        before_white != after_white
        and normalized_difference >= THRESHOLDS["flash_frame_difference"]
    )
    extreme_brightness_flash = (
        brightness_delta >= THRESHOLDS["flash_brightness_delta"]
        and normalized_difference >= THRESHOLDS["flash_frame_difference"]
        and (
            min(before_stats["mean_luma"], after_stats["mean_luma"]) <= 0.03
            or max(before_stats["mean_luma"], after_stats["mean_luma"]) >= 0.97
        )
    )
    black_or_flash = bool(
        before_black or after_black or pure_white_flash or extreme_brightness_flash
    )
    large_visual_jump = bool(
        normalized_difference >= THRESHOLDS["large_visual_jump_difference"]
        or brightness_delta >= THRESHOLDS["large_visual_jump_brightness_delta"]
    )
    metrics = {
        "before": before_stats,
        "after": after_stats,
        "normalized_frame_difference": normalized_difference,
        "brightness_delta": brightness_delta,
        "before_is_black": bool(before_black),
        "after_is_black": bool(after_black),
        "before_is_white": bool(before_white),
        "after_is_white": bool(after_white),
        "extreme_brightness_flash": bool(extreme_brightness_flash),
    }
    flags = {
        "black_or_flash": black_or_flash,
        "large_visual_jump": large_visual_jump,
    }
    return metrics, flags


def extract_mono_pcm(
    ffmpeg: str,
    video: Path,
    cut_time_s: float,
    video_duration_s: float,
) -> tuple[np.ndarray, int, float, float]:
    left_window = min(AUDIO_HALF_WINDOW_S, cut_time_s)
    right_window = min(AUDIO_HALF_WINDOW_S, max(0.0, video_duration_s - cut_time_s))
    start = max(0.0, cut_time_s - left_window)
    requested_samples = max(1, round((left_window + right_window) * AUDIO_RATE))
    split_index = min(requested_samples, round(left_window * AUDIO_RATE))

    raw = run_capture(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.6f}",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            f"{left_window + right_window:.6f}",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    samples_i16 = np.frombuffer(raw, dtype="<i2")
    if len(samples_i16) < requested_samples:
        samples_i16 = np.pad(samples_i16, (0, requested_samples - len(samples_i16)))
    elif len(samples_i16) > requested_samples:
        samples_i16 = samples_i16[:requested_samples]
    samples = samples_i16.astype(np.float32) / 32768.0
    return samples, split_index, left_window, right_window


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def peak(samples: np.ndarray) -> float:
    return float(np.max(np.abs(samples))) if samples.size else 0.0


def amplitude_db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-6))


def analyze_audio(samples: np.ndarray, split_index: int) -> tuple[dict[str, Any], bool]:
    left = samples[:split_index]
    right = samples[split_index:]
    peak_left = peak(left)
    peak_right = peak(right)
    rms_left = rms(left)
    rms_right = rms(right)
    boundary_step = (
        float(abs(float(right[0]) - float(left[-1]))) if left.size and right.size else 0.0
    )

    guard = max(1, round(0.010 * AUDIO_RATE))
    boundary_region = np.concatenate((left[-guard:], right[:guard]))
    context_parts = [part for part in (left[:-guard], right[guard:]) if part.size]
    context = np.concatenate(context_parts) if context_parts else np.zeros(1, dtype=np.float32)
    boundary_peak = peak(boundary_region)
    context_rms = rms(context)
    step_over_context_ratio = boundary_step / max(context_rms, 1e-6)
    transient_over_context_db = amplitude_db(boundary_peak) - amplitude_db(context_rms)
    rms_jump_db = amplitude_db(rms_right) - amplitude_db(rms_left)

    step_spike = (
        boundary_step >= THRESHOLDS["audio_boundary_step"]
        and step_over_context_ratio >= THRESHOLDS["audio_step_over_context_ratio"]
    )
    transient_spike = (
        boundary_peak >= THRESHOLDS["audio_transient_peak"]
        and transient_over_context_db >= THRESHOLDS["audio_transient_over_context_db"]
    )
    audio_spike = bool(step_spike or transient_spike)
    metrics = {
        "peak_left": peak_left,
        "peak_right": peak_right,
        "rms_left": rms_left,
        "rms_right": rms_right,
        "peak_jump_abs": abs(peak_right - peak_left),
        "rms_jump_db": rms_jump_db,
        "boundary_step_abs": boundary_step,
        "boundary_peak_around_10ms": boundary_peak,
        "context_rms_excluding_10ms": context_rms,
        "boundary_step_over_context_ratio": step_over_context_ratio,
        "transient_over_context_db": transient_over_context_db,
    }
    return metrics, audio_spike


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf") if bold else Path(
            "/System/Library/Fonts/Supplemental/Arial.ttf"
        ),
        Path("/System/Library/Fonts/Helvetica.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if bold else Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:06.3f}"


def draw_waveform(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    samples: np.ndarray | None,
    split_index: int,
) -> None:
    x0, y0, x1, y1 = bounds
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    middle_y = y0 + height // 2
    draw.rectangle(bounds, fill=(10, 15, 20), outline=(48, 61, 69), width=1)
    draw.line((x0, middle_y, x1, middle_y), fill=(55, 67, 75), width=1)
    if samples is None or samples.size == 0:
        font = load_font(11)
        draw.text((x0 + 8, y0 + 5), "no audio", font=font, fill=(133, 145, 153))
        return

    boundary_x = x0 + round(width * split_index / max(1, len(samples)))
    draw.line((boundary_x, y0 + 1, boundary_x, y1 - 1), fill=(86, 215, 196), width=1)
    edges = np.linspace(0, len(samples), width + 1, dtype=int)
    amplitude = max(1, height // 2 - 3)
    for column in range(width):
        start, end = edges[column], edges[column + 1]
        chunk = samples[start:end]
        if chunk.size == 0:
            continue
        low = float(np.min(chunk))
        high = float(np.max(chunk))
        line_y0 = middle_y - round(high * amplitude)
        line_y1 = middle_y - round(low * amplitude)
        color = (92, 174, 224) if x0 + column < boundary_x else (230, 172, 83)
        draw.line((x0 + column, line_y0, x0 + column, line_y1), fill=color, width=1)


def render_contact_sheet(
    out_dir: Path,
    items: list[dict[str, Any]],
    page_index: int,
    page_count: int,
) -> Path:
    columns, rows = 3, 4
    margin, header_h = 18, 50
    cell_w, cell_h = 500, 260
    page_w = margin * (columns + 1) + cell_w * columns
    page_h = header_h + margin * (rows + 1) + cell_h * rows
    page = Image.new("RGB", (page_w, page_h), (8, 12, 17))
    draw = ImageDraw.Draw(page)
    title_font = load_font(20, bold=True)
    label_font = load_font(16, bold=True)
    small_font = load_font(12)
    draw.text(
        (margin, 14),
        f"ALL CUTS QA  |  page {page_index}/{page_count}",
        font=title_font,
        fill=(236, 241, 244),
    )

    for item_index, item in enumerate(items):
        row, column = divmod(item_index, columns)
        x = margin + column * (cell_w + margin)
        y = header_h + margin + row * (cell_h + margin)
        flags = item["metrics"]["flags"]
        blocking = flags["black_or_flash"] or flags["audio_spike"]
        border = (222, 79, 79) if blocking else (
            (225, 178, 66) if flags["large_visual_jump"] else (55, 78, 88)
        )
        draw.rounded_rectangle(
            (x, y, x + cell_w, y + cell_h),
            radius=12,
            fill=(14, 20, 27),
            outline=border,
            width=2,
        )
        label = f"cut {item['metrics']['cut']} @ {format_time(item['metrics']['time_s'])}"
        draw.text((x + 12, y + 10), label, font=label_font, fill=(244, 247, 249))

        flag_names = []
        if flags["black_or_flash"]:
            flag_names.append("BLACK/FLASH")
        if flags["audio_spike"]:
            flag_names.append("AUDIO SPIKE")
        if flags["large_visual_jump"]:
            flag_names.append("VISUAL JUMP")
        flag_text = " | ".join(flag_names) if flag_names else "OK"
        flag_color = (239, 100, 100) if blocking else (
            (238, 191, 77) if flags["large_visual_jump"] else (86, 215, 196)
        )
        text_bbox = draw.textbbox((0, 0), flag_text, font=small_font)
        draw.text(
            (x + cell_w - 12 - (text_bbox[2] - text_bbox[0]), y + 13),
            flag_text,
            font=small_font,
            fill=flag_color,
        )

        thumb_w, thumb_h = 224, 126
        before_image = Image.fromarray(item["before"], mode="RGB").resize(
            (thumb_w, thumb_h), Image.Resampling.LANCZOS
        )
        after_image = Image.fromarray(item["after"], mode="RGB").resize(
            (thumb_w, thumb_h), Image.Resampling.LANCZOS
        )
        before_x, after_x, thumb_y = x + 12, x + 264, y + 42
        page.paste(before_image, (before_x, thumb_y))
        page.paste(after_image, (after_x, thumb_y))
        draw.rectangle(
            (before_x, thumb_y, before_x + thumb_w, thumb_y + thumb_h),
            outline=(76, 91, 101),
            width=1,
        )
        draw.rectangle(
            (after_x, thumb_y, after_x + thumb_w, thumb_y + thumb_h),
            outline=(76, 91, 101),
            width=1,
        )
        draw.text((before_x + 5, thumb_y + 5), "BEFORE -80ms", font=small_font, fill=(255, 255, 255))
        draw.text((after_x + 5, thumb_y + 5), "AFTER +80ms", font=small_font, fill=(255, 255, 255))

        visual = item["metrics"]["visual"]
        audio = item["metrics"]["audio"]
        stats_text = (
            f"diff {visual['normalized_frame_difference']:.3f}  "
            f"luma {visual['before']['mean_luma']:.2f}->{visual['after']['mean_luma']:.2f}  "
            f"step {audio.get('boundary_step_abs', 0.0):.3f}"
        )
        draw.text((x + 12, y + 174), stats_text, font=small_font, fill=(171, 184, 192))
        draw_waveform(
            draw,
            (x + 12, y + 195, x + cell_w - 12, y + cell_h - 12),
            item["samples"],
            item["split_index"],
        )

    first_cut = items[0]["metrics"]["cut"]
    last_cut = items[-1]["metrics"]["cut"]
    output = out_dir / f"cuts_{first_cut:03d}-{last_cut:03d}.png"
    page.save(output, format="PNG", optimize=True)
    return output


def rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value


def range_summary(edl_range: dict[str, Any]) -> dict[str, Any]:
    allowed = ("source", "start", "end", "beat", "quote", "description", "note")
    return {key: edl_range[key] for key in allowed if key in edl_range}


def run_qa(args: argparse.Namespace) -> int:
    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    video = args.video.expanduser().resolve()
    edl_path = args.edl.expanduser().resolve()
    clips_dir = args.clips_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()

    for path, description in ((video, "video"), (edl_path, "EDL")):
        if not path.is_file():
            raise QAError(f"{description} does not exist: {path}")
    if not clips_dir.is_dir():
        raise QAError(f"clips-dir does not exist: {clips_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    ranges = load_edl_ranges(edl_path)
    clips = discover_clips(clips_dir, len(ranges))
    clip_durations = [duration_from_probe(ffprobe_json(ffprobe, clip)) for clip in clips]
    if any(duration <= 0 for duration in clip_durations):
        raise QAError("all segment durations must be positive")

    video_info = probe_video(ffprobe, video)
    video_duration = float(video_info["duration_s"])
    fps = float(video_info["fps"] or 25.0)
    computed_duration = float(sum(clip_durations))
    duration_delta = video_duration - computed_duration
    warnings: list[str] = []
    duration_tolerance = max(0.12, 2.0 / fps)
    if abs(duration_delta) > duration_tolerance:
        warnings.append(
            f"preview duration differs from summed clip duration by {duration_delta:+.3f}s"
        )
    if computed_duration - video_duration > duration_tolerance:
        raise QAError(
            "computed cut timeline extends beyond preview duration; check clip order or preview source"
        )
    if not video_info["has_audio"]:
        warnings.append("preview has no audio stream; audio metrics are marked unavailable")

    cut_times = np.cumsum(np.asarray(clip_durations, dtype=np.float64))[:-1].tolist()
    metrics: list[dict[str, Any]] = []
    visual_items: list[dict[str, Any]] = []
    total_pages = math.ceil(len(cut_times) / 12) if cut_times else 0
    contact_sheets: list[str] = []

    for cut_zero_index, cut_time in enumerate(cut_times):
        cut_number = cut_zero_index + 1
        frame_end_guard = max(0.0, video_duration - 0.5 / fps)
        before_time = max(0.0, cut_time - FRAME_OFFSET_S)
        after_time = min(frame_end_guard, cut_time + FRAME_OFFSET_S)
        before = extract_rgb_frame(ffmpeg, video, before_time)
        after = extract_rgb_frame(ffmpeg, video, after_time)
        visual, visual_flags = visual_metrics(before, after)

        samples: np.ndarray | None = None
        split_index = 0
        if video_info["has_audio"]:
            samples, split_index, left_window, right_window = extract_mono_pcm(
                ffmpeg, video, cut_time, video_duration
            )
            audio, audio_spike = analyze_audio(samples, split_index)
            audio.update(
                {
                    "available": True,
                    "sample_rate": AUDIO_RATE,
                    "left_window_s": left_window,
                    "right_window_s": right_window,
                }
            )
        else:
            audio = {
                "available": False,
                "sample_rate": AUDIO_RATE,
                "left_window_s": 0.0,
                "right_window_s": 0.0,
            }
            audio_spike = False

        flags = {
            "black_or_flash": visual_flags["black_or_flash"],
            "audio_spike": bool(audio_spike),
            "large_visual_jump": visual_flags["large_visual_jump"],
        }
        cut_metrics = {
            "cut": cut_number,
            "time_s": float(cut_time),
            "sample_times_s": {"before": before_time, "after": after_time},
            "left_segment": {
                "index": cut_zero_index,
                "clip": clips[cut_zero_index].name,
                "actual_duration_s": clip_durations[cut_zero_index],
                "edl": range_summary(ranges[cut_zero_index]),
            },
            "right_segment": {
                "index": cut_zero_index + 1,
                "clip": clips[cut_zero_index + 1].name,
                "actual_duration_s": clip_durations[cut_zero_index + 1],
                "edl": range_summary(ranges[cut_zero_index + 1]),
            },
            "visual": visual,
            "audio": audio,
            "flags": flags,
        }
        metrics.append(cut_metrics)
        visual_items.append(
            {
                "metrics": cut_metrics,
                "before": before,
                "after": after,
                "samples": samples,
                "split_index": split_index,
            }
        )

        if len(visual_items) == 12 or cut_number == len(cut_times):
            page_index = len(contact_sheets) + 1
            sheet = render_contact_sheet(out_dir, visual_items, page_index, total_pages)
            contact_sheets.append(sheet.name)
            visual_items.clear()

    black_or_flash_count = sum(item["flags"]["black_or_flash"] for item in metrics)
    audio_spike_count = sum(item["flags"]["audio_spike"] for item in metrics)
    visual_jump_count = sum(item["flags"]["large_visual_jump"] for item in metrics)
    blocking_failures = sum(
        item["flags"]["black_or_flash"] or item["flags"]["audio_spike"]
        for item in metrics
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "video": str(video),
            "edl": str(edl_path),
            "clips_dir": str(clips_dir),
            "out_dir": str(out_dir),
        },
        "video": video_info,
        "timeline": {
            "segment_count": len(clips),
            "cut_count": len(cut_times),
            "clip_durations_s": clip_durations,
            "summed_clip_duration_s": computed_duration,
            "preview_duration_s": video_duration,
            "preview_minus_clips_s": duration_delta,
        },
        "thresholds": THRESHOLDS,
        "summary": {
            "black_or_flash": black_or_flash_count,
            "audio_spike": audio_spike_count,
            "large_visual_jump_warning": visual_jump_count,
            "blocking_failures": blocking_failures,
            "exit_code": 1 if blocking_failures else 0,
            "contact_sheets": contact_sheets,
            "warnings": warnings,
        },
        "cuts": metrics,
    }
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(rounded(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"QA complete: {len(cut_times)} cuts | black/flash={black_or_flash_count} | "
        f"audio_spike={audio_spike_count} | visual_jump_warning={visual_jump_count}"
    )
    print(f"metrics: {metrics_path}")
    if contact_sheets:
        print(f"contact sheets: {len(contact_sheets)} page(s) in {out_dir}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 1 if blocking_failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "QA all output cut boundaries using actual encoded segment durations; "
            "ordinary visual jump cuts are warnings only."
        )
    )
    parser.add_argument("--video", required=True, type=Path, help="rendered preview/final video")
    parser.add_argument("--edl", required=True, type=Path, help="EDL JSON with ordered ranges")
    parser.add_argument(
        "--clips-dir",
        required=True,
        type=Path,
        help="directory containing ordered encoded segment clips",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="output directory for metrics.json and 12-cut contact sheets",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_qa(args)
    except QAError as exc:
        print(f"qa_all_cuts: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
