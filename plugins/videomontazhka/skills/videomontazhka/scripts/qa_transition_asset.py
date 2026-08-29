#!/usr/bin/env python3
"""Quality-check one semantic-approved full-frame or alpha transition asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from visual_asset_provenance import (
    FileSnapshot,
    VisualProvenanceError,
    assert_snapshots_current,
    atomic_write_json,
    file_sha256,
    load_approved_visual_plan_item,
)


QA_VERSION = 1
QA_TYPE = "sprut_transition_asset_qa"
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


class TransitionQAError(RuntimeError):
    pass


def _resolved_under_edit(edit_dir: Path, value: Path, label: str) -> Path:
    raw = value.expanduser()
    candidate = raw if raw.is_absolute() else edit_dir / raw
    return path_under_edit(edit_dir, candidate, label)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-_")
    return slug[:48].rstrip("-_") or "transition"


def _positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TransitionQAError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise TransitionQAError(f"{label} must be finite and greater than zero")
    return number


def _nonnegative(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TransitionQAError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise TransitionQAError(f"{label} must be finite and non-negative")
    return number


def _fraction(value: Any, label: str) -> float:
    number = _nonnegative(value, label)
    if number > 1:
        raise TransitionQAError(f"{label} must be between 0 and 1")
    return number


def _tool(name: str) -> tuple[Path, str]:
    executable = shutil.which(name)
    if executable is None:
        raise TransitionQAError(f"missing executable: {name}")
    path = Path(executable).resolve()
    result = subprocess.run(
        [str(path), "-version"], text=True, capture_output=True, check=False
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise TransitionQAError(f"cannot identify {name}: {detail[-1000:]}")
    lines = (result.stdout + result.stderr).splitlines()
    return path, (lines[0].strip() if lines else "")


def _rate(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else 0.0
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
            "stream=codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,duration:stream_tags=alpha_mode:format=duration",
            "-of",
            "json",
            str(asset),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise TransitionQAError(
            f"ffprobe failed for transition asset: {(result.stderr or result.stdout).strip()}"
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
        raise TransitionQAError(f"cannot parse transition metadata: {exc}") from exc
    if width < 1 or height < 1 or not math.isfinite(duration) or duration <= 0 or fps <= 0:
        raise TransitionQAError("transition has invalid dimensions, duration, or fps")
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    return {
        "codec_name": stream.get("codec_name"),
        "width": width,
        "height": height,
        "pix_fmt": stream.get("pix_fmt"),
        "fps": fps,
        "duration_s": duration,
        "alpha_mode_tag": tags.get("alpha_mode"),
    }


def _pix_fmt_declares_alpha(pix_fmt: Any, alpha_mode_tag: Any = None) -> bool:
    if str(alpha_mode_tag) == "1":
        return True
    value = str(pix_fmt or "").casefold()
    return bool(
        value.startswith(("yuva", "gbrap", "rgba", "bgra", "argb", "abgr", "ya"))
        or value in {"pal8", "gray8a", "gray16a", "ayuv64le", "ayuv64be"}
    )


class FrameAnalyzer:
    def __init__(
        self,
        *,
        fps: float,
        mode: str,
        black_luma_threshold: float,
        black_pixel_fraction: float,
        flash_mean_threshold: float,
        flash_pixel_fraction: float,
    ) -> None:
        self.fps = fps
        self.mode = mode
        self.black_luma_threshold = black_luma_threshold
        self.black_pixel_fraction = black_pixel_fraction
        self.flash_mean_threshold = flash_mean_threshold
        self.flash_pixel_fraction = flash_pixel_fraction
        self.frame_count = 0
        self.black_frames: list[dict[str, Any]] = []
        self.flash_pairs: list[dict[str, Any]] = []
        self.nonopaque_fractions: list[float] = []
        self.transparent_fractions: list[float] = []
        self.partial_fractions: list[float] = []
        self.visible_fractions: list[float] = []
        self.previous_luma: np.ndarray[Any, Any] | None = None

    def add(self, rgba: np.ndarray[Any, Any]) -> None:
        if rgba.ndim != 3 or rgba.shape[2] != 4 or rgba.dtype != np.uint8:
            raise TransitionQAError("decoded frame must be uint8 RGBA")
        rgb = rgba[:, :, :3].astype(np.float32) / 255.0
        alpha = rgba[:, :, 3].astype(np.float32) / 255.0
        nonopaque_fraction = float(np.mean(alpha < (254.5 / 255.0)))
        transparent_fraction = float(np.mean(alpha <= (0.5 / 255.0)))
        partial_fraction = float(np.mean((alpha > (0.5 / 255.0)) & (alpha < (254.5 / 255.0))))
        visible_mask = alpha > 0.02
        visible_fraction = float(np.mean(visible_mask))
        self.nonopaque_fractions.append(nonopaque_fraction)
        self.transparent_fractions.append(transparent_fraction)
        self.partial_fractions.append(partial_fraction)
        self.visible_fractions.append(visible_fraction)

        if self.mode == "alpha":
            perceived = rgb * alpha[:, :, None] + 0.5 * (1.0 - alpha[:, :, None])
        else:
            perceived = rgb
        luma = (
            perceived[:, :, 0] * 0.2126
            + perceived[:, :, 1] * 0.7152
            + perceived[:, :, 2] * 0.0722
        )

        if self.mode == "alpha":
            if np.any(visible_mask):
                visible_luma = luma[visible_mask]
                dark_fraction = float(np.mean(visible_luma <= self.black_luma_threshold))
                mean_luma = float(np.mean(visible_luma))
                all_black = (
                    visible_fraction >= 0.01
                    and dark_fraction >= self.black_pixel_fraction
                    and mean_luma <= self.black_luma_threshold
                )
            else:
                dark_fraction = 0.0
                mean_luma = 0.0
                all_black = False
        else:
            dark_fraction = float(np.mean(luma <= self.black_luma_threshold))
            mean_luma = float(np.mean(luma))
            all_black = (
                dark_fraction >= self.black_pixel_fraction
                and mean_luma <= self.black_luma_threshold
            )
        if all_black:
            self.black_frames.append(
                {
                    "frame_index": self.frame_count,
                    "time_s": self.frame_count / self.fps,
                    "mean_luma": mean_luma,
                    "dark_pixel_fraction": dark_fraction,
                    "visible_pixel_fraction": visible_fraction,
                }
            )

        if self.previous_luma is not None:
            difference = np.abs(luma - self.previous_luma)
            mean_difference = float(np.mean(difference))
            severe_pixel_fraction = float(np.mean(difference >= 0.60))
            if (
                mean_difference >= self.flash_mean_threshold
                and severe_pixel_fraction >= self.flash_pixel_fraction
            ):
                self.flash_pairs.append(
                    {
                        "from_frame": self.frame_count - 1,
                        "to_frame": self.frame_count,
                        "from_time_s": (self.frame_count - 1) / self.fps,
                        "to_time_s": self.frame_count / self.fps,
                        "mean_luma_difference": mean_difference,
                        "severe_pixel_fraction": severe_pixel_fraction,
                    }
                )
        self.previous_luma = luma.copy()
        self.frame_count += 1

    def finish(self) -> dict[str, Any]:
        if self.frame_count == 0:
            raise TransitionQAError("transition decoded zero frames")

        def summary(values: list[float]) -> dict[str, float]:
            return {
                "minimum": min(values),
                "maximum": max(values),
                "mean": float(sum(values) / len(values)),
            }

        return {
            "frame_count": self.frame_count,
            "decoded_duration_s": self.frame_count / self.fps,
            "alpha_coverage": {
                "nonopaque_pixel_fraction": summary(self.nonopaque_fractions),
                "fully_transparent_pixel_fraction": summary(self.transparent_fractions),
                "partially_transparent_pixel_fraction": summary(self.partial_fractions),
                "visible_pixel_fraction": summary(self.visible_fractions),
            },
            "all_black_frames": self.black_frames,
            "severe_adjacent_flash_pairs": self.flash_pairs,
        }


def analyze_rgba_frames(
    frames: Iterable[np.ndarray[Any, Any]],
    *,
    fps: float,
    mode: str,
    black_luma_threshold: float = 0.02,
    black_pixel_fraction: float = 0.995,
    flash_mean_threshold: float = 0.45,
    flash_pixel_fraction: float = 0.80,
) -> dict[str, Any]:
    analyzer = FrameAnalyzer(
        fps=fps,
        mode=mode,
        black_luma_threshold=black_luma_threshold,
        black_pixel_fraction=black_pixel_fraction,
        flash_mean_threshold=flash_mean_threshold,
        flash_pixel_fraction=flash_pixel_fraction,
    )
    for frame in frames:
        analyzer.add(frame)
    return analyzer.finish()


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _analyze_media(
    ffmpeg: Path,
    asset: Path,
    media: dict[str, Any],
    *,
    mode: str,
    black_luma_threshold: float,
    black_pixel_fraction: float,
    flash_mean_threshold: float,
    flash_pixel_fraction: float,
) -> dict[str, Any]:
    width, height = int(media["width"]), int(media["height"])
    frame_size = width * height * 4
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(asset),
        "-map",
        "0:v:0",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-fps_mode",
        "passthrough",
        "-",
    ]
    analyzer = FrameAnalyzer(
        fps=float(media["fps"]),
        mode=mode,
        black_luma_threshold=black_luma_threshold,
        black_pixel_fraction=black_pixel_fraction,
        flash_mean_threshold=flash_mean_threshold,
        flash_pixel_fraction=flash_pixel_fraction,
    )
    # A corrupt asset can emit many decoder errors. Keep stderr in a temporary file
    # instead of a pipe so it cannot fill and deadlock while raw frames are streamed.
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file)
        if process.stdout is None:
            process.kill()
            raise TransitionQAError("cannot open ffmpeg decode pipe")
        try:
            while True:
                raw = _read_exact(process.stdout, frame_size)
                if not raw:
                    break
                if len(raw) != frame_size:
                    raise TransitionQAError(
                        f"ffmpeg produced a truncated raw frame ({len(raw)} of {frame_size} bytes)"
                    )
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))
                analyzer.add(frame)
            process.stdout.close()
            return_code = process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", "replace")
            if return_code:
                raise TransitionQAError(f"ffmpeg decode failed: {stderr.strip()[-2000:]}")
            return analyzer.finish()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _synthetic_frames(root: Path, name: str, frames: list[np.ndarray[Any, Any]]) -> list[np.ndarray[Any, Any]]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise TransitionQAError("Pillow is required for transition --self-test") from exc
    output = root / name
    output.mkdir()
    loaded: list[np.ndarray[Any, Any]] = []
    for index, frame in enumerate(frames):
        path = output / f"frame_{index:02d}.png"
        Image.fromarray(frame, mode="RGBA").save(path)
        with Image.open(path) as image:
            loaded.append(np.asarray(image.convert("RGBA"), dtype=np.uint8).copy())
    return loaded


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="sprut-transition-self-test-", dir="/tmp") as name:
        root = Path(name)
        smooth_source = [
            np.full((36, 64, 4), (value, value, value, 255), dtype=np.uint8)
            for value in (48, 52, 56, 60, 64, 68)
        ]
        smooth = analyze_rgba_frames(
            _synthetic_frames(root, "smooth", smooth_source), fps=12, mode="full-frame"
        )
        if smooth["all_black_frames"] or smooth["severe_adjacent_flash_pairs"]:
            raise TransitionQAError("self-test smooth sequence produced a false positive")

        flash_source = [
            np.full((36, 64, 4), (48, 48, 48, 255), dtype=np.uint8),
            np.full((36, 64, 4), (255, 255, 255, 255), dtype=np.uint8),
            np.full((36, 64, 4), (48, 48, 48, 255), dtype=np.uint8),
        ]
        flash = analyze_rgba_frames(
            _synthetic_frames(root, "flash", flash_source), fps=12, mode="full-frame"
        )
        if len(flash["severe_adjacent_flash_pairs"]) != 2:
            raise TransitionQAError("self-test did not detect the synthetic severe flash")

        black_source = smooth_source[:2] + [
            np.full((36, 64, 4), (0, 0, 0, 255), dtype=np.uint8)
        ] + smooth_source[2:4]
        black = analyze_rgba_frames(
            _synthetic_frames(root, "black", black_source), fps=12, mode="full-frame"
        )
        if not black["all_black_frames"]:
            raise TransitionQAError("self-test did not detect the synthetic all-black frame")

        alpha_source: list[np.ndarray[Any, Any]] = []
        for offset in (0, 8, 16, 24):
            frame = np.zeros((36, 64, 4), dtype=np.uint8)
            frame[10:26, offset : min(64, offset + 24), :3] = (255, 106, 0)
            frame[10:26, offset : min(64, offset + 24), 3] = 160
            alpha_source.append(frame)
        alpha = analyze_rgba_frames(
            _synthetic_frames(root, "alpha", alpha_source), fps=12, mode="alpha"
        )
        coverage = alpha["alpha_coverage"]["nonopaque_pixel_fraction"]["maximum"]
        visible = alpha["alpha_coverage"]["visible_pixel_fraction"]["maximum"]
        if coverage < 0.001 or visible < 0.001:
            raise TransitionQAError("self-test did not measure synthetic alpha coverage")

        print(
            json.dumps(
                {
                    "version": QA_VERSION,
                    "type": "sprut_transition_asset_qa_self_test",
                    "status": "PASS",
                    "synthetic_root": str(root),
                    "checks": {
                        "smooth_false_positives": 0,
                        "flash_pairs_detected": len(flash["severe_adjacent_flash_pairs"]),
                        "black_frames_detected": len(black["all_black_frames"]),
                        "alpha_nonopaque_fraction_max": coverage,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check duration, fps, alpha coverage, black frames, and severe flashes"
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--visual-id")
    parser.add_argument("--asset", type=Path)
    parser.add_argument("--mode", choices=("full-frame", "alpha"))
    parser.add_argument("--expected-duration", type=float)
    parser.add_argument("--expected-fps", type=float)
    parser.add_argument("--duration-tolerance", type=float)
    parser.add_argument("--fps-tolerance", type=float, default=0.01)
    parser.add_argument("--black-luma-threshold", type=float, default=0.02)
    parser.add_argument("--black-pixel-fraction", type=float, default=0.995)
    parser.add_argument("--flash-mean-threshold", type=float, default=0.45)
    parser.add_argument("--flash-pixel-fraction", type=float, default=0.80)
    args = parser.parse_args()

    if args.self_test:
        forbidden = (
            args.edit_dir,
            args.visual_id,
            args.asset,
            args.mode,
            args.expected_duration,
            args.expected_fps,
            args.duration_tolerance,
        )
        if any(value is not None for value in forbidden):
            raise TransitionQAError("--self-test does not accept project or expectation arguments")
        return run_self_test()

    missing = [
        name
        for name, value in (
            ("--edit-dir", args.edit_dir),
            ("--visual-id", args.visual_id),
            ("--asset", args.asset),
            ("--mode", args.mode),
            ("--expected-duration", args.expected_duration),
            ("--expected-fps", args.expected_fps),
        )
        if value is None
    ]
    if missing:
        raise TransitionQAError(f"missing required arguments: {', '.join(missing)}")

    expected_duration = _positive(args.expected_duration, "--expected-duration")
    expected_fps = _positive(args.expected_fps, "--expected-fps")
    fps_tolerance = _nonnegative(args.fps_tolerance, "--fps-tolerance")
    black_luma_threshold = _fraction(args.black_luma_threshold, "--black-luma-threshold")
    black_pixel_fraction = _fraction(args.black_pixel_fraction, "--black-pixel-fraction")
    flash_mean_threshold = _fraction(args.flash_mean_threshold, "--flash-mean-threshold")
    flash_pixel_fraction = _fraction(args.flash_pixel_fraction, "--flash-pixel-fraction")

    edit_dir = canonical_edit_dir(args.edit_dir)
    asset = _resolved_under_edit(edit_dir, args.asset, "transition asset")
    if not asset.is_file():
        raise TransitionQAError(f"transition asset is missing: {asset}")
    if asset.suffix.casefold() not in VIDEO_SUFFIXES:
        raise TransitionQAError(f"unsupported transition extension: {asset.suffix}")

    # Fail closed before probing, decoding, or creating edit/verify output.
    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    if approved.asset_type == "none":
        raise TransitionQAError("approved visual asset_type='none' cannot be a transition")
    asset_snapshot = FileSnapshot(asset, file_sha256(asset))
    snapshots = [asset_snapshot, approved.plan_snapshot, approved.approval_snapshot]
    assert_snapshots_current(snapshots)

    ffmpeg, ffmpeg_version = _tool("ffmpeg")
    ffprobe, ffprobe_version = _tool("ffprobe")
    media = _probe(ffprobe, asset)
    duration_tolerance = (
        _nonnegative(args.duration_tolerance, "--duration-tolerance")
        if args.duration_tolerance is not None
        else max(0.05, 1.0 / expected_fps)
    )
    analysis = _analyze_media(
        ffmpeg,
        asset,
        media,
        mode=args.mode,
        black_luma_threshold=black_luma_threshold,
        black_pixel_fraction=black_pixel_fraction,
        flash_mean_threshold=flash_mean_threshold,
        flash_pixel_fraction=flash_pixel_fraction,
    )
    assert_snapshots_current(snapshots)

    duration_error = abs(float(media["duration_s"]) - expected_duration)
    fps_error = abs(float(media["fps"]) - expected_fps)
    expected_decoded_frames = max(1, round(float(media["duration_s"]) * float(media["fps"])))
    decoded_frame_error = abs(int(analysis["frame_count"]) - expected_decoded_frames)
    declared_alpha = _pix_fmt_declares_alpha(media.get("pix_fmt"), media.get("alpha_mode_tag"))
    alpha_max = analysis["alpha_coverage"]["nonopaque_pixel_fraction"]["maximum"]
    visible_max = analysis["alpha_coverage"]["visible_pixel_fraction"]["maximum"]
    alpha_required = args.mode == "alpha"
    alpha_pass = not alpha_required or (alpha_max >= 0.001 and visible_max >= 0.001)
    checks = {
        "duration": {
            "expected_s": expected_duration,
            "actual_s": media["duration_s"],
            "absolute_error_s": duration_error,
            "tolerance_s": duration_tolerance,
            "pass": duration_error <= duration_tolerance,
        },
        "fps": {
            "expected": expected_fps,
            "actual": media["fps"],
            "absolute_error": fps_error,
            "tolerance": fps_tolerance,
            "pass": fps_error <= fps_tolerance,
        },
        "decoded_frame_count": {
            "expected_from_probed_duration_and_fps": expected_decoded_frames,
            "actual": analysis["frame_count"],
            "absolute_error_frames": decoded_frame_error,
            "tolerance_frames": 1,
            "pass": decoded_frame_error <= 1,
        },
        "alpha": {
            "expected": alpha_required,
            "pixel_format_declares_alpha": declared_alpha,
            "coverage": analysis["alpha_coverage"],
            "pass": alpha_pass,
        },
        "all_black_frames": {
            "count": len(analysis["all_black_frames"]),
            "frames": analysis["all_black_frames"],
            "thresholds": {
                "luma": black_luma_threshold,
                "dark_pixel_fraction": black_pixel_fraction,
            },
            "pass": not analysis["all_black_frames"],
        },
        "severe_adjacent_flash": {
            "count": len(analysis["severe_adjacent_flash_pairs"]),
            "pairs": analysis["severe_adjacent_flash_pairs"],
            "thresholds": {
                "mean_luma_difference": flash_mean_threshold,
                "severe_pixel_fraction": flash_pixel_fraction,
                "per_pixel_difference": 0.60,
            },
            "pass": not analysis["severe_adjacent_flash_pairs"],
        },
    }
    failures = [name for name, check in checks.items() if not check["pass"]]
    status = "PASS" if not failures else "FAIL"
    signature = hashlib.sha256(
        json.dumps(
            {
                "asset_sha256": asset_snapshot.sha256,
                "visual_id": approved.visual_id,
                "mode": args.mode,
                "expected_duration": expected_duration,
                "expected_fps": expected_fps,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    output = edit_dir / "verify" / f"transition_qa_{_slug(approved.visual_id)}_{signature}.json"
    payload = {
        "version": QA_VERSION,
        "type": QA_TYPE,
        "status": status,
        "failures": failures,
        "visual_id": approved.visual_id,
        "section_id": approved.section_id,
        "meaning_ids": list(approved.meaning_ids),
        "purpose": approved.purpose,
        "treatment": approved.treatment,
        "asset_type": approved.asset_type,
        "transition_mode": args.mode,
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
        "decode": {
            "frame_count": analysis["frame_count"],
            "decoded_duration_s": analysis["decoded_duration_s"],
        },
        "checks": checks,
        "qa_tool": {
            "path": str(Path(__file__).resolve()),
            "sha256": file_sha256(Path(__file__).resolve()),
        },
        "tools": {
            "ffmpeg": {"path": str(ffmpeg), "version": ffmpeg_version},
            "ffprobe": {"path": str(ffprobe), "version": ffprobe_version},
        },
    }
    assert_snapshots_current(snapshots)
    atomic_write_json(output, payload)
    print(f"transition QA {status}: {output}")
    if failures:
        print(f"failed checks: {', '.join(failures)}", file=sys.stderr)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, OSError, TransitionQAError, VisualProvenanceError, ValueError) as exc:
        print(f"qa_transition_asset: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
