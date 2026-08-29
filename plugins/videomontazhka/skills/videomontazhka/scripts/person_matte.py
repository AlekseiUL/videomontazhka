#!/usr/bin/env python3
"""Approval-gated local Apple Vision person-matte runner for macOS."""

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
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate


SCRIPT_DIR = Path(__file__).resolve().parent
NATIVE_SOURCE = SCRIPT_DIR / "segment_person.m"
BUILD_VERSION = "sprut-person-matte-build-1"
FRAMEWORKS = (
    "Foundation",
    "AVFoundation",
    "Vision",
    "CoreImage",
    "CoreGraphics",
    "CoreMedia",
    "CoreVideo",
    "ImageIO",
)
QUALITY_LEVELS = ("fast", "balanced", "accurate")


class PersonMatteError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectRequest:
    edit_dir: Path
    source: Path
    matte: Path | None
    foreground: Path | None
    quality: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_checked(command: Sequence[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise PersonMatteError(f"{label} could not start: {exc}") from exc
    if result.returncode:
        details = "\n".join(
            value.strip()
            for value in (result.stdout, result.stderr)
            if value and value.strip()
        )
        raise PersonMatteError(
            f"{label} failed with exit {result.returncode}"
            + (f":\n{details}" if details else "")
        )
    return result


def require_macos_tools() -> tuple[str, str, str]:
    if sys.platform != "darwin":
        raise PersonMatteError("Apple Vision person matting requires macOS")
    xcrun = shutil.which("xcrun")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    missing = [
        name
        for name, value in (("xcrun", xcrun), ("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
        if not value
    ]
    if missing:
        raise PersonMatteError(f"missing required local tool(s): {', '.join(missing)}")
    if not NATIVE_SOURCE.is_file():
        raise PersonMatteError(f"native source is missing: {NATIVE_SOURCE}")
    return str(xcrun), str(ffmpeg), str(ffprobe)


def compile_command(xcrun: str, source: Path, output: Path) -> list[str]:
    command = [
        xcrun,
        "clang",
        "-fobjc-arc",
        "-fblocks",
        "-Wall",
        "-Wextra",
        "-Werror",
    ]
    for framework in FRAMEWORKS:
        command.extend(["-framework", framework])
    command.extend([str(source), "-o", str(output)])
    return command


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_binary(build_dir: Path) -> Path:
    xcrun, _, _ = require_macos_tools()
    build_dir = build_dir.expanduser().resolve()
    binary = build_dir / "segment_person"
    metadata = build_dir / "segment_person.build.json"
    clang = run_checked([xcrun, "--find", "clang"], label="locate Apple clang").stdout.strip()
    clang_version = run_checked([clang, "--version"], label="inspect Apple clang").stdout.splitlines()[0]
    source_hash = file_sha256(NATIVE_SOURCE)
    fingerprint_payload = {
        "build_version": BUILD_VERSION,
        "source": str(NATIVE_SOURCE),
        "source_sha256": source_hash,
        "clang": clang,
        "clang_version": clang_version,
        "compile_flags": compile_command(xcrun, NATIVE_SOURCE, Path("<output>"))[:-2],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if binary.is_file() and os.access(binary, os.X_OK) and metadata.is_file():
        try:
            recorded = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recorded = None
        if isinstance(recorded, dict) and recorded.get("fingerprint") == fingerprint:
            return binary

    build_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="segment-person-build-", dir=str(build_dir)) as temporary:
        temporary_binary = Path(temporary) / "segment_person"
        run_checked(
            compile_command(xcrun, NATIVE_SOURCE, temporary_binary),
            label="compile Apple Vision person-matte tool",
        )
        os.chmod(temporary_binary, 0o700)
        os.replace(temporary_binary, binary)
    atomic_write_json(
        metadata,
        {
            **fingerprint_payload,
            "fingerprint": fingerprint,
            "binary": str(binary),
            "binary_sha256": file_sha256(binary),
        },
    )
    return binary


def fraction_value(value: Any) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        result = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def probe_media(path: Path, *, count_frames: bool = False) -> dict[str, Any]:
    _, _, ffprobe = require_macos_tools()
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
    ]
    if count_frames:
        command.insert(3, "-count_frames")
    command.append(str(path))
    result = run_checked(command, label=f"ffprobe {path.name}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PersonMatteError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise PersonMatteError(f"ffprobe returned an invalid object for {path}")
    return payload


def video_stream(probe: dict[str, Any], path: Path) -> dict[str, Any]:
    streams = [
        stream
        for stream in probe.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if len(streams) != 1:
        raise PersonMatteError(f"expected exactly one video stream in {path}, found {len(streams)}")
    return streams[0]


def stream_rotation(stream: dict[str, Any]) -> int:
    for side_data in stream.get("side_data_list", []):
        if isinstance(side_data, dict) and side_data.get("rotation") is not None:
            try:
                return int(round(float(side_data["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    tags = stream.get("tags")
    if isinstance(tags, dict) and tags.get("rotate") is not None:
        try:
            return int(round(float(tags["rotate"]))) % 360
        except (TypeError, ValueError):
            pass
    return 0


def display_dimensions(stream: dict[str, Any]) -> tuple[int, int]:
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PersonMatteError("video stream has invalid dimensions") from exc
    if stream_rotation(stream) in {90, 270}:
        return height, width
    return width, height


def stream_fps(stream: dict[str, Any]) -> float | None:
    return fraction_value(stream.get("avg_frame_rate")) or fraction_value(stream.get("r_frame_rate"))


def stream_duration(probe: dict[str, Any], stream: dict[str, Any]) -> float | None:
    for raw in (stream.get("duration"), (probe.get("format") or {}).get("duration")):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def stream_frame_count(stream: dict[str, Any]) -> int | None:
    for field in ("nb_read_frames", "nb_frames"):
        try:
            value = int(stream[field])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def output_summary(
    path: Path,
    *,
    expected_dimensions: tuple[int, int],
    expected_fps: float | None,
    expected_duration: float | None,
    expected_frames: int | None,
    count_frames: bool,
    require_alpha: bool,
) -> dict[str, Any]:
    probe = probe_media(path, count_frames=count_frames)
    stream = video_stream(probe, path)
    audio_streams = [
        item
        for item in probe.get("streams", [])
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if audio_streams:
        raise PersonMatteError(f"{path.name} violates overlay audio policy: audio stream present")
    dimensions = display_dimensions(stream)
    if dimensions != expected_dimensions:
        raise PersonMatteError(
            f"{path.name} display dimensions changed: {dimensions} != {expected_dimensions}"
        )
    codec = str(stream.get("codec_name") or "")
    profile = str(stream.get("profile") or "")
    if codec != "prores" or "4444" not in profile:
        raise PersonMatteError(
            f"{path.name} is not ProRes 4444 (codec={codec!r}, profile={profile!r})"
        )
    pixel_format = str(stream.get("pix_fmt") or "")
    if require_alpha and not pixel_format.startswith("yuva"):
        raise PersonMatteError(
            f"{path.name} does not expose an alpha-capable pixel format: {pixel_format!r}"
        )
    fps = stream_fps(stream)
    if expected_fps is not None and fps is not None:
        tolerance = max(0.02, expected_fps * 0.005)
        if abs(fps - expected_fps) > tolerance:
            raise PersonMatteError(
                f"{path.name} frame rate changed: {fps:.6f} != {expected_fps:.6f}"
            )
    duration = stream_duration(probe, stream)
    if expected_duration is not None and duration is not None:
        frame_tolerance = 2.0 / (expected_fps or 30.0)
        if abs(duration - expected_duration) > max(0.10, frame_tolerance):
            raise PersonMatteError(
                f"{path.name} duration changed: {duration:.6f} != {expected_duration:.6f}"
            )
    frames = stream_frame_count(stream)
    if expected_frames is not None and frames is not None and frames != expected_frames:
        raise PersonMatteError(
            f"{path.name} frame count changed: {frames} != {expected_frames}"
        )
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        "width": dimensions[0],
        "height": dimensions[1],
        "fps": fps,
        "duration_s": duration,
        "frames": frames,
        "codec": codec,
        "profile": profile,
        "pixel_format": pixel_format,
        "audio_streams": 0,
    }


def verify_outputs(
    source: Path,
    *,
    matte: Path | None,
    foreground: Path | None,
    count_frames: bool = False,
) -> dict[str, Any]:
    source_probe = probe_media(source, count_frames=count_frames)
    source_stream = video_stream(source_probe, source)
    dimensions = display_dimensions(source_stream)
    fps = stream_fps(source_stream)
    duration = stream_duration(source_probe, source_stream)
    frames = stream_frame_count(source_stream)
    outputs: dict[str, Any] = {}
    if matte is not None:
        outputs["matte"] = output_summary(
            matte,
            expected_dimensions=dimensions,
            expected_fps=fps,
            expected_duration=duration,
            expected_frames=frames,
            count_frames=count_frames,
            require_alpha=False,
        )
    if foreground is not None:
        outputs["foreground"] = output_summary(
            foreground,
            expected_dimensions=dimensions,
            expected_fps=fps,
            expected_duration=duration,
            expected_frames=frames,
            count_frames=count_frames,
            require_alpha=True,
        )
    return {
        "source": {
            "path": str(source),
            "sha256": file_sha256(source),
            "width": dimensions[0],
            "height": dimensions[1],
            "fps": fps,
            "duration_s": duration,
            "frames": frames,
        },
        "outputs": outputs,
        "audio_policy": "video_only_no_audio",
    }


def native_command(
    binary: Path,
    source: Path,
    *,
    mode: str,
    quality: str,
) -> list[str]:
    if mode not in {"matte", "foreground"}:
        raise PersonMatteError(f"unsupported native output mode: {mode}")
    return [str(binary), str(source), "--mode", mode, "--quality", quality]


def raw_encode_command(
    ffmpeg: str,
    output: Path,
    *,
    width: int,
    height: int,
    fps: float,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "rawvideo",
        "-pixel_format",
        "bgra",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        f"{fps:.12g}",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-alpha_bits",
        "16",
        "-map_metadata",
        "-1",
        str(output),
    ]


def encode_native_stream(
    binary: Path,
    source: Path,
    output: Path,
    *,
    mode: str,
    quality: str,
    width: int,
    height: int,
    fps: float,
) -> str:
    _, ffmpeg, _ = require_macos_tools()
    try:
        native = subprocess.Popen(
            native_command(binary, source, mode=mode, quality=quality),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise PersonMatteError(f"Apple Vision person segmentation could not start: {exc}") from exc
    assert native.stdout is not None
    assert native.stderr is not None
    try:
        encoder = subprocess.Popen(
            raw_encode_command(
                ffmpeg,
                output,
                width=width,
                height=height,
                fps=fps,
            ),
            stdin=native.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        native.terminate()
        native.wait()
        raise PersonMatteError(f"ProRes 4444 encoder could not start: {exc}") from exc
    native.stdout.close()
    _, encoder_stderr = encoder.communicate()
    native_stderr = native.stderr.read()
    native_returncode = native.wait()
    native_log = native_stderr.decode("utf-8", errors="replace").strip()
    encoder_log = encoder_stderr.decode("utf-8", errors="replace").strip()
    if native_returncode or encoder.returncode:
        details = "\n".join(value for value in (native_log, encoder_log) if value)
        raise PersonMatteError(
            f"{mode} render failed (Vision={native_returncode}, FFmpeg={encoder.returncode})"
            + (f":\n{details}" if details else "")
        )
    return native_log


def run_native(
    binary: Path,
    source: Path,
    *,
    matte: Path | None,
    foreground: Path | None,
    quality: str,
) -> str:
    probe = probe_media(source)
    stream = video_stream(probe, source)
    width, height = display_dimensions(stream)
    fps = stream_fps(stream) or 30.0
    logs: list[str] = []
    for mode, output in (("matte", matte), ("foreground", foreground)):
        if output is None:
            continue
        logs.append(
            encode_native_stream(
                binary,
                source,
                output,
                mode=mode,
                quality=quality,
                width=width,
                height=height,
                fps=fps,
            )
        )
    return "\n".join(logs)


def resolved_under_edit(edit_dir: Path, value: Path, label: str) -> Path:
    expanded = value.expanduser()
    candidate = expanded if expanded.is_absolute() else edit_dir / expanded
    return path_under_edit(edit_dir, candidate, label)


def prepare_project_request(args: argparse.Namespace) -> ProjectRequest:
    if args.edit_dir is None or args.input is None:
        raise PersonMatteError("real project output requires --edit-dir and --input")
    if args.matte is None and args.foreground is None:
        raise PersonMatteError("at least one of --matte or --foreground is required")
    edit_dir = canonical_edit_dir(args.edit_dir)
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise PersonMatteError(f"input video not found: {source}")
    matte = resolved_under_edit(edit_dir, args.matte, "matte output") if args.matte else None
    foreground = (
        resolved_under_edit(edit_dir, args.foreground, "foreground output")
        if args.foreground
        else None
    )
    outputs = [path for path in (matte, foreground) if path is not None]
    if len(outputs) != len(set(outputs)):
        raise PersonMatteError("matte and foreground outputs must be different files")
    for path in outputs:
        if path == source:
            raise PersonMatteError("an output cannot replace the input video")
        if path.suffix.lower() != ".mov":
            raise PersonMatteError(f"person-matte output must use .mov: {path}")
        if path.exists():
            raise PersonMatteError(f"output exists; refusing to overwrite: {path}")
    return ProjectRequest(edit_dir, source, matte, foreground, args.quality)


def publish_outputs(pairs: Sequence[tuple[Path, Path]]) -> None:
    for _, destination in pairs:
        if destination.exists():
            raise PersonMatteError(f"output appeared during render; refusing to overwrite: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    try:
        for temporary, destination in pairs:
            os.replace(temporary, destination)
            published.append(destination)
    except OSError as exc:
        for path in published:
            try:
                path.unlink()
            except OSError:
                pass
        raise PersonMatteError(f"could not publish person-matte outputs atomically: {exc}") from exc


def run_project(request: ProjectRequest) -> dict[str, Any]:
    # The gate must run before the build directory, a temporary render, or an
    # output parent is created. A stale/missing semantic approval fails closed.
    require_asset_gate(request.edit_dir)
    source_hash = file_sha256(request.source)
    work_dir = path_under_edit(
        request.edit_dir,
        request.edit_dir / "work" / "person_matte",
        "person-matte work directory",
    )
    binary = build_binary(work_dir / "bin")
    run_root = work_dir / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="person-matte-", dir=str(run_root)) as temporary:
        temporary_dir = Path(temporary)
        temporary_matte = temporary_dir / "matte.mov" if request.matte else None
        temporary_foreground = temporary_dir / "foreground.mov" if request.foreground else None
        native_log = run_native(
            binary,
            request.source,
            matte=temporary_matte,
            foreground=temporary_foreground,
            quality=request.quality,
        )
        report = verify_outputs(
            request.source,
            matte=temporary_matte,
            foreground=temporary_foreground,
        )
        if file_sha256(request.source) != source_hash:
            raise PersonMatteError("input video changed while the person matte was being rendered")
        pairs: list[tuple[Path, Path]] = []
        if temporary_matte is not None and request.matte is not None:
            pairs.append((temporary_matte, request.matte))
        if temporary_foreground is not None and request.foreground is not None:
            pairs.append((temporary_foreground, request.foreground))
        publish_outputs(pairs)

    for name, destination in (("matte", request.matte), ("foreground", request.foreground)):
        if destination is not None:
            report["outputs"][name]["path"] = str(destination)
            report["outputs"][name]["sha256"] = file_sha256(destination)
    report.update(
        {
            "version": 1,
            "engine": "apple_vision_local",
            "quality": request.quality,
            "binary": str(binary),
            "binary_sha256": file_sha256(binary),
            "native_log": native_log,
            "uploads_data": False,
            "source_sha256": source_hash,
        }
    )
    return report


def run_self_test() -> dict[str, Any]:
    _, ffmpeg, _ = require_macos_tools()
    with tempfile.TemporaryDirectory(prefix="sprut-person-matte-self-test-") as temporary:
        root = Path(temporary)
        source = root / "synthetic-input.mp4"
        matte = root / "synthetic-matte.mov"
        foreground = root / "synthetic-foreground.mov"
        run_checked(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x240:rate=12:duration=0.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=0.5",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ],
            label="generate synthetic self-test video",
        )
        binary = build_binary(root / "bin")
        native_log = run_native(
            binary,
            source,
            matte=matte,
            foreground=foreground,
            quality="fast",
        )
        report = verify_outputs(
            source,
            matte=matte,
            foreground=foreground,
            count_frames=True,
        )
        return {
            "version": 1,
            "self_test": "PASS",
            "synthetic_only": True,
            "asset_gate_bypassed": "self-test writes only inside an auto-deleted temporary directory",
            "engine": "apple_vision_local",
            "quality": "fast",
            "native_log": native_log,
            "checks": report,
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Build and run the local Apple Vision person-matte tool. Real project "
            "outputs require the SPRUT asset gate and must stay under edit/."
        )
    )
    result.add_argument("--self-test", action="store_true", help="run a synthetic local smoke test")
    result.add_argument("--edit-dir", type=Path)
    result.add_argument("--input", type=Path)
    result.add_argument("--matte", type=Path, help="opaque grayscale ProRes 4444 MOV under edit/")
    result.add_argument(
        "--foreground",
        type=Path,
        help="source RGB with Vision alpha as a video-only ProRes 4444 MOV under edit/",
    )
    result.add_argument("--quality", choices=QUALITY_LEVELS, default="accurate")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.self_test:
        if any(value is not None for value in (args.edit_dir, args.input, args.matte, args.foreground)):
            raise PersonMatteError("--self-test cannot be combined with project paths")
        report = run_self_test()
    else:
        report = run_project(prepare_project_request(args))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, PersonMatteError, OSError, ValueError) as exc:
        print(f"person_matte: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
