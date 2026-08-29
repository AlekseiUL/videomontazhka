#!/usr/bin/env python3
# Portions derived from video-use, Copyright (c) 2026 Browser Use, MIT License.
# Modifications Copyright 2026 Алексей Ульянов, Apache-2.0.
# See repository NOTICE and third_party/licenses/video-use-MIT.txt.
"""Render an approved SPRUT EDL with CFR H.264 + PCM intermediates.

Pipeline:
  1. Validate the semantic approval and EDL traceability gate.
  2. Render frame-aligned H.264/PCM MOV segments with reset PTS and 30 ms fades.
  3. Concat compatible segments without re-encoding.
  4. Composite overlays, optional subtitles last, and local SFX while audio is PCM.
  5. Apply selected programme-level audio filters and two-pass loudnorm.
  6. Encode AAC exactly once and write a machine-readable render manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

from artifact_provenance import (
    RENDERER_VERSION,
    ProvenanceError,
    artifact_key,
    preview_approval_name,
    invalidate_release_state,
    render_manifest_name,
    renderer_identity,
    resolve_subtitle_fonts,
)
from visual_asset_provenance import (
    VisualProvenanceError,
    verify_visual_asset_provenance,
)

AUDIO_CLEANUP_FILTERS = {
    "acompressor",
    "adeclick",
    "adeclip",
    "afftdn",
    "alimiter",
    "anlmdn",
    "deesser",
    "dialoguenhance",
    "equalizer",
    "highpass",
    "lowpass",
    "volume",
}
AUDIO_RATE = 48_000
AUDIO_FADE_S = 0.030
AAC_BITRATE = "192k"
CACHE_ATTESTATION_VERSION = 1
DEFAULT_LUFS = -14.0
DEFAULT_TRUE_PEAK = -2.0
DEFAULT_LRA = 11.0
SCRIPT_DIR = Path(__file__).resolve().parent
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
FFMPEG_EXECUTABLE = "ffmpeg"
FFPROBE_EXECUTABLE = "ffprobe"
TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    mode: str
    declared_width: int
    declared_height: int
    width: int
    height: int
    fps: Fraction
    preset: str
    crf: int

    @property
    def fps_expr(self) -> str:
        return f"{self.fps.numerator}/{self.fps.denominator}"

    @property
    def fps_float(self) -> float:
        return float(self.fps)

    @property
    def scale_x(self) -> float:
        return self.width / self.declared_width

    @property
    def scale_y(self) -> float:
        return self.height / self.declared_height

    def video_args(self) -> list[str]:
        gop = max(1, round(self.fps_float * 2))
        return [
            "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf),
            "-profile:v", "high", "-g", str(gop), "-keyint_min", str(gop),
            "-sc_threshold", "0", "-pix_fmt", "yuv420p",
        ]


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    duration_s: float
    has_audio: bool
    audio_duration_s: float | None
    coded_width: int
    coded_height: int
    width: int
    height: int
    display_rotation_degrees: int
    fps: float | None
    color_transfer: str | None
    fingerprint: str
    size_bytes: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    item: dict[str, Any]
    frame_count: int
    sample_count: int


@dataclass(frozen=True)
class FileSnapshot:
    label: str
    path: Path
    sha256: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


def log(message: str) -> None:
    print(f"[sprut-render] {message}", flush=True)


def command_text(command: Sequence[str]) -> str:
    rendered = shlex.join([str(value) for value in command])
    return rendered if len(rendered) <= 800 else rendered[:797] + "..."


def run(
    command: list[str],
    *,
    label: str,
    capture: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    log(f"{label}: {command_text(command)}")
    options: dict[str, Any] = {}
    if pass_fds:
        options["pass_fds"] = pass_fds
    result = subprocess.run(
        command, text=True, capture_output=True, check=False, **options
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RenderError(f"{label} failed ({result.returncode})\n{detail[-5000:]}")
    return result


def ffmpeg_base() -> list[str]:
    return [FFMPEG_EXECUTABLE, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


def require_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RenderError(f"missing executable(s): {', '.join(missing)}")


def configure_render_tools(identity: dict[str, Any]) -> None:
    """Pin execution to the exact binaries fingerprinted in renderer identity."""
    resolved: dict[str, str] = {}
    tools = identity.get("tools")
    if not isinstance(tools, dict):
        raise RenderError("renderer identity has no tool records")
    for name in ("ffmpeg", "ffprobe"):
        record = tools.get(name)
        if not isinstance(record, dict):
            raise RenderError(f"renderer identity has no {name} record")
        raw_path = record.get("path")
        expected_sha256 = record.get("binary_sha256")
        if not isinstance(raw_path, str) or not raw_path:
            raise RenderError(f"renderer identity has no {name} executable path")
        path = Path(raw_path).resolve()
        if not path.is_file() or file_sha256(path) != expected_sha256:
            raise RenderError(f"identified {name} executable changed before rendering")
        resolved[name] = str(path)
    global FFMPEG_EXECUTABLE, FFPROBE_EXECUTABLE
    FFMPEG_EXECUTABLE = resolved["ffmpeg"]
    FFPROBE_EXECUTABLE = resolved["ffprobe"]


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RenderError(f"file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RenderError(f"JSON root must be an object: {path}")
    return data


def capture_file_snapshot(path: Path, label: str) -> tuple[FileSnapshot, bytes]:
    """Read one immutable control/asset input and bind the exact opened inode."""
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read()
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise RenderError(f"cannot snapshot {label}: {resolved} ({exc})") from exc
    before_state = stat_snapshot(before)
    after_state = stat_snapshot(after)
    if before_state != after_state:
        raise RenderError(f"{label} changed while it was being snapshotted: {resolved}")
    snapshot = FileSnapshot(
        label=label,
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=int(after.st_size),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        mtime_ns=int(after.st_mtime_ns),
        ctime_ns=int(after.st_ctime_ns),
    )
    return snapshot, raw


def load_json_snapshot(path: Path, label: str) -> tuple[dict[str, Any], FileSnapshot]:
    snapshot, raw = capture_file_snapshot(path, label)
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read JSON {snapshot.path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RenderError(f"JSON root must be an object: {snapshot.path}")
    return data, snapshot


def assert_file_snapshot(snapshot: FileSnapshot) -> None:
    """Fail if a path no longer names the exact snapshotted inode and bytes."""
    try:
        current = snapshot.path.stat()
    except OSError as exc:
        raise RenderError(f"{snapshot.label} disappeared after validation: {snapshot.path}") from exc
    state = (
        int(current.st_dev),
        int(current.st_ino),
        int(current.st_size),
        int(current.st_mtime_ns),
        int(current.st_ctime_ns),
    )
    expected = (
        snapshot.device,
        snapshot.inode,
        snapshot.size_bytes,
        snapshot.mtime_ns,
        snapshot.ctime_ns,
    )
    if state != expected or file_sha256(snapshot.path) != snapshot.sha256:
        raise RenderError(f"{snapshot.label} changed after validation: {snapshot.path}")


def assert_file_snapshots(snapshots: Sequence[FileSnapshot]) -> None:
    for snapshot in snapshots:
        assert_file_snapshot(snapshot)


def snapshot_records(snapshots: Sequence[FileSnapshot]) -> list[dict[str, Any]]:
    return [
        {"label": item.label, "path": str(item.path), "sha256": item.sha256}
        for item in snapshots
    ]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def open_file_sha256(handle: Any) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while chunk := handle.read(4 * 1024 * 1024):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def source_snapshot(source: SourceInfo) -> tuple[int, int, int, int, int] | None:
    values = (
        source.device,
        source.inode,
        source.size_bytes,
        source.mtime_ns,
        source.ctime_ns,
    )
    if any(value is None for value in values):
        return None
    return tuple(int(value) for value in values)  # type: ignore[arg-type, return-value]


def stat_snapshot(stat_info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(stat_info.st_dev),
        int(stat_info.st_ino),
        int(stat_info.st_size),
        int(stat_info.st_mtime_ns),
        int(stat_info.st_ctime_ns),
    )


def verify_source_path(source: SourceInfo) -> None:
    """Require the current source path to name the approved immutable bytes."""
    try:
        if not source.path.is_file():
            raise RenderError(f"source disappeared before segment render: {source.path}")
        expected = source_snapshot(source)
        if expected is not None:
            if stat_snapshot(source.path.stat()) != expected:
                raise RenderError(f"source changed after fingerprinting: {source.path}")
        elif file_sha256(source.path) != source.fingerprint:
            raise RenderError(f"source SHA-256 changed before segment render: {source.path}")
    except OSError as exc:
        raise RenderError(f"cannot revalidate source before segment render: {source.path}") from exc


def verify_open_source(handle: Any, source: SourceInfo) -> None:
    """Verify the exact open inode passed to FFmpeg, not merely its pathname."""
    try:
        expected = source_snapshot(source)
        if expected is not None:
            if stat_snapshot(os.fstat(handle.fileno())) != expected:
                raise RenderError(f"opened source differs from approved snapshot: {source.path}")
        elif open_file_sha256(handle) != source.fingerprint:
            raise RenderError(f"opened source SHA-256 differs from approval: {source.path}")
    except OSError as exc:
        raise RenderError(f"cannot verify opened source: {source.path}") from exc


def pinned_source_reference(handle: Any, source: SourceInfo) -> tuple[str, tuple[int, ...]]:
    """Return a seekable inherited-FD path when the host supports one."""
    if os.name == "posix":
        for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
            if directory.is_dir():
                descriptor = handle.fileno()
                return str(directory / str(descriptor)), (descriptor,)
    # Non-POSIX fallback retains pre/post path checks, but supported SPRUT
    # production hosts use the inherited descriptor path above.
    return str(source.path), ()


def resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def ensure_inside(path: Path, parent: Path, description: str) -> None:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise RenderError(f"{description} must stay inside {parent}") from exc


def parse_fps(value: Any) -> Fraction:
    try:
        if isinstance(value, str) and "/" in value:
            fps = Fraction(value)
        else:
            fps = Fraction(str(value)).limit_denominator(1001)
    except (ValueError, ZeroDivisionError) as exc:
        raise RenderError(f"invalid output fps: {value!r}") from exc
    if not 20 <= float(fps) <= 60:
        raise RenderError("output fps must be between 20 and 60")
    return fps


def parse_probe_rate(value: Any) -> float | None:
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def probe_display_rotation(video: dict[str, Any]) -> int:
    """Return a normalized right-angle display rotation from ffprobe data.

    FFmpeg autorotation applies this display matrix before our video filters.
    Layout ROIs therefore need the display-oriented dimensions, not the coded
    width/height reported at the top of the stream object.
    """
    raw: Any = None
    for side_data in video.get("side_data_list") or []:
        if isinstance(side_data, dict) and side_data.get("rotation") is not None:
            raw = side_data["rotation"]
            break
    if raw is None and isinstance(video.get("tags"), dict):
        raw = video["tags"].get("rotate")
    if raw is None:
        return 0
    try:
        angle = float(raw)
    except (TypeError, ValueError) as exc:
        raise RenderError(f"invalid source display rotation: {raw!r}") from exc
    if not math.isfinite(angle):
        raise RenderError(f"invalid source display rotation: {raw!r}")
    normalized = angle % 360.0
    nearest = round(normalized / 90.0) * 90
    if abs(normalized - nearest) > 0.1 and abs(normalized - (nearest % 360)) > 0.1:
        raise RenderError(
            f"unsupported non-right-angle source display rotation {angle:g} degrees; "
            "normalize the source before authoring ROIs"
        )
    return int(nearest) % 360


def probe(path: Path) -> dict[str, Any]:
    result = run(
        [FFPROBE_EXECUTABLE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        label=f"probe {path.name}", capture=True,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"ffprobe returned invalid JSON for {path}") from exc
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None:
        raise RenderError(f"no video stream: {path}")
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    coded_width = int(video.get("width") or 0)
    coded_height = int(video.get("height") or 0)
    display_rotation = probe_display_rotation(video)
    display_width, display_height = coded_width, coded_height
    if display_rotation in {90, 270}:
        display_width, display_height = coded_height, coded_width
    return {
        "duration_s": duration,
        "coded_width": coded_width,
        "coded_height": coded_height,
        "width": display_width,
        "height": display_height,
        "display_rotation_degrees": display_rotation,
        "fps": parse_probe_rate(video.get("avg_frame_rate")) or parse_probe_rate(video.get("r_frame_rate")),
        "video_codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "color_transfer": video.get("color_transfer"),
        "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_duration_s": float(audio.get("duration") or duration) if audio else None,
    }


def probe_exact_counts(path: Path) -> dict[str, int | None]:
    """Return decoded video frames and PCM samples, independent of container duration."""
    result = run(
        [
            FFPROBE_EXECUTABLE, "-v", "error", "-count_frames", "-show_streams",
            "-show_entries",
            "stream=codec_type,nb_read_frames,duration_ts,time_base,sample_rate",
            "-of", "json", str(path),
        ],
        label=f"count frames/samples {path.name}", capture=True,
    )
    streams = json.loads(result.stdout).get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    frames: int | None = None
    samples: int | None = None
    if video and str(video.get("nb_read_frames") or "").isdigit():
        frames = int(video["nb_read_frames"])
    if audio:
        try:
            duration_ts = int(audio["duration_ts"])
            time_base = Fraction(str(audio["time_base"]))
            sample_rate = int(audio["sample_rate"])
            samples = round(duration_ts * time_base * sample_rate)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            samples = None
    return {"video_frames": frames, "audio_samples": samples}


def simple_filter(value: Any, *, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise RenderError(f"{field} cannot be empty")
    if any(char in text for char in ";[]"):
        raise RenderError(f"{field} must be a simple comma-separated filter chain")
    return text


def crop_filter(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text if text.startswith("crop=") else f"crop={text}"
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return "crop=" + ":".join(str(part) for part in value)
    if isinstance(value, dict):
        width = value.get("w", value.get("width"))
        height = value.get("h", value.get("height"))
        if width is None or height is None:
            raise RenderError("crop object needs width and height")
        return f"crop={width}:{height}:{value.get('x', 0)}:{value.get('y', 0)}"
    raise RenderError("crop must be a filter string, [w,h,x,y], or an object")


def range_view_filters(item: dict[str, Any]) -> list[str]:
    filters: list[str] = []
    if item.get("crop") is not None:
        filters.append(simple_filter(crop_filter(item["crop"]), field="range.crop"))
    if item.get("view_filter") is not None:
        filters.append(simple_filter(item["view_filter"], field="range.view_filter"))
    return filters


def capture_control_inputs(
    edit_dir: Path,
    edl_snapshot: FileSnapshot,
    project: dict[str, Any],
    project_snapshot: FileSnapshot,
    *,
    mode: str,
    deliverable_id: str,
) -> tuple[list[FileSnapshot], dict[str, Any], dict[str, Any] | None]:
    """Snapshot every mutable approval/manifest byte consulted by the gate/render."""
    snapshots: dict[Path, FileSnapshot] = {
        edl_snapshot.path: edl_snapshot,
        project_snapshot.path: project_snapshot,
    }

    def add(path: Path, label: str) -> tuple[FileSnapshot, bytes]:
        resolved = path.expanduser().resolve()
        existing = snapshots.get(resolved)
        if existing is not None:
            return existing, resolved.read_bytes()
        snapshot, raw = capture_file_snapshot(resolved, label)
        snapshots[resolved] = snapshot
        return snapshot, raw

    def add_json(path: Path, label: str) -> dict[str, Any]:
        snapshot, raw = add(path, label)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RenderError(f"cannot read JSON {snapshot.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RenderError(f"JSON root must be an object: {snapshot.path}")
        return value

    add(edit_dir / "semantic_plan.json", "semantic plan")
    add(edit_dir / "approval.json", "semantic approval")
    source_manifest_path = resolve_path(
        str(project.get("source_manifest") or "source_manifest.json"), edit_dir
    )
    source_manifest = add_json(source_manifest_path, "source manifest")
    packed = add_json(edit_dir / "takes_packed_manifest.json", "packed transcript manifest")
    packed_output = packed.get("output")
    if isinstance(packed_output, str) and packed_output.strip():
        add(resolve_path(packed_output, edit_dir), "packed transcript view")
    transcript_dir = (edit_dir / "transcripts").resolve()
    for item in source_manifest.get("sources") or []:
        if not isinstance(item, dict) or item.get("audio") is None:
            continue
        source_id = str(item.get("id") or "")
        if source_id:
            add(transcript_dir / f"{source_id}.json", f"transcript {source_id}")
            add(
                transcript_dir / ".metadata" / f"{source_id}.json",
                f"transcript metadata {source_id}",
            )

    preview_approval: dict[str, Any] | None = None
    if mode == "final":
        approval_path = edit_dir / preview_approval_name(deliverable_id)
        preview_approval = add_json(approval_path, "preview approval")
        preview_manifest_value = preview_approval.get("render_manifest_file")
        if isinstance(preview_manifest_value, str) and preview_manifest_value.strip():
            add(resolve_path(preview_manifest_value, edit_dir), "approved preview manifest")
        qa_value = preview_approval.get("qa_report")
        if isinstance(qa_value, str) and qa_value.strip():
            add(resolve_path(qa_value, edit_dir), "approved preview QA report")
        preview_value = preview_approval.get("preview_file")
        if isinstance(preview_value, str) and preview_value.strip():
            add(resolve_path(preview_value, edit_dir), "approved preview media")
        preview_sidecar = preview_approval.get("preview_sidecar")
        if isinstance(preview_sidecar, dict):
            sidecar_value = preview_sidecar.get("path")
            if isinstance(sidecar_value, str) and sidecar_value.strip():
                add(resolve_path(sidecar_value, edit_dir), "approved preview sidecar")
    return list(snapshots.values()), source_manifest, preview_approval


def validate_approved_preview_sidecar(
    preview_approval: dict[str, Any],
    *,
    subtitle_mode: str,
    subtitles: Path | None,
    subtitle_sha256: str | None,
    approved_preview: Path,
    edit_dir: Path,
    control_snapshots: list[FileSnapshot],
) -> dict[str, str] | None:
    """Validate and return the exact sidecar the user saw with the preview."""
    record = preview_approval.get("preview_sidecar")
    if subtitle_mode != "sidecar":
        if record is not None:
            raise RenderError(
                f"subtitle_mode={subtitle_mode!r} must not have an approved preview sidecar"
            )
        return None
    if subtitles is None or subtitle_sha256 is None:
        raise RenderError("sidecar final render has no current subtitle source provenance")
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise RenderError("preview approval has no canonical preview sidecar path/hash")
    raw_path = record.get("path")
    expected_digest = record.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RenderError("preview approval has no canonical preview sidecar path")
    approved_sidecar = resolve_path(raw_path, edit_dir)
    ensure_inside(approved_sidecar, edit_dir, "approved preview sidecar")
    expected_path = approved_preview.with_suffix(subtitles.suffix.lower())
    if approved_sidecar != expected_path:
        raise RenderError(
            "approved preview sidecar path differs from the preview output-derived path"
        )
    snapshot = next(
        (item for item in control_snapshots if item.path == approved_sidecar), None
    )
    if snapshot is None:
        raise RenderError("approved preview sidecar was not snapshotted before the final gate")
    if snapshot.sha256 != expected_digest:
        raise RenderError("approved preview sidecar changed after user approval")
    if expected_digest != subtitle_sha256:
        raise RenderError("approved preview sidecar differs from the final subtitle source")
    return {"path": raw_path, "sha256": str(expected_digest)}


def load_manifest_sources(
    edit_dir: Path,
    project: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    manifest_path = resolve_path(str(project.get("source_manifest") or "source_manifest.json"), edit_dir)
    manifest = manifest if manifest is not None else load_json(manifest_path)
    root_value = Path(str(manifest.get("root") or "..")).expanduser()
    root = root_value.resolve() if root_value.is_absolute() else (manifest_path.parent / root_value).resolve()
    approved: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(manifest.get("sources") or []):
        if not isinstance(item, dict) or not item.get("id") or not item.get("path") or not item.get("sha256"):
            raise RenderError(f"invalid source manifest entry {index}")
        source_id = str(item["id"])
        if source_id in approved:
            raise RenderError(f"duplicate source id in manifest: {source_id}")
        raw_path = Path(str(item["path"])).expanduser()
        canonical = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
        approved[source_id] = {**item, "path": canonical, "sha256": str(item["sha256"])}
    if not approved:
        raise RenderError("source manifest has no sources")
    return approved


def build_sources(edl: dict[str, Any], edit_dir: Path, approved: dict[str, dict[str, Any]]) -> dict[str, SourceInfo]:
    raw = edl.get("sources")
    if not isinstance(raw, dict) or not raw:
        raise RenderError("EDL needs a non-empty sources object")
    sources: dict[str, SourceInfo] = {}
    for source_id, value in raw.items():
        source_id = str(source_id)
        manifest_item = approved.get(source_id)
        if manifest_item is None:
            raise RenderError(f"source {source_id!r} is not present in the approved manifest")
        declared_path = resolve_path(str(value), edit_dir)
        path = Path(manifest_item["path"]).resolve()
        if declared_path != path:
            raise RenderError(f"EDL source {source_id!r} does not match the hashed manifest path")
        if not path.is_file():
            raise RenderError(f"source not found: {source_id} -> {path}")
        fingerprint = str(manifest_item.get("sha256") or "")
        if len(fingerprint) != 64:
            raise RenderError(f"source {source_id} has no approved manifest fingerprint")
        stat_info = path.stat()
        if (
            manifest_item.get("size_bytes") != stat_info.st_size
            or manifest_item.get("mtime_ns") != stat_info.st_mtime_ns
        ):
            raise RenderError(f"source {source_id} changed since ingest; re-initialize the project")
        info = probe(path)
        if stat_snapshot(path.stat()) != stat_snapshot(stat_info):
            raise RenderError(f"source {source_id} changed while it was being probed")
        if file_sha256(path) != fingerprint:
            raise RenderError(f"source {source_id} SHA-256 differs from the approved manifest")
        verified_stat = path.stat()
        if stat_snapshot(verified_stat) != stat_snapshot(stat_info):
            raise RenderError(f"source {source_id} changed while it was being fingerprinted")
        sources[source_id] = SourceInfo(
            path=path,
            duration_s=float(info["duration_s"]),
            has_audio=bool(info["has_audio"]),
            audio_duration_s=info["audio_duration_s"],
            coded_width=int(info["coded_width"]),
            coded_height=int(info["coded_height"]),
            width=int(info["width"]),
            height=int(info["height"]),
            display_rotation_degrees=int(info["display_rotation_degrees"]),
            fps=info["fps"],
            color_transfer=info["color_transfer"],
            fingerprint=fingerprint,
            size_bytes=verified_stat.st_size,
            mtime_ns=verified_stat.st_mtime_ns,
            ctime_ns=verified_stat.st_ctime_ns,
            device=verified_stat.st_dev,
            inode=verified_stat.st_ino,
        )
    return sources


def build_profile(edl: dict[str, Any], mode: str) -> Profile:
    output = edl.get("output")
    if not isinstance(output, dict):
        raise RenderError("EDL output must be an object")
    try:
        declared_width = int(output["width"])
        declared_height = int(output["height"])
        fps = parse_fps(output["fps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderError("EDL output needs valid width, height, and fps") from exc
    if declared_width < 320 or declared_height < 320 or declared_width % 2 or declared_height % 2:
        raise RenderError("output dimensions must be even and at least 320")
    width, height = declared_width, declared_height
    if mode == "draft":
        # Use one scale factor so a draft never distorts the declared canvas.
        # When either side is already at the supported minimum, keeping the
        # declared dimensions is safer than independently clamping the axes.
        draft_scale = min(
            1.0,
            max(0.5, 320 / declared_width, 320 / declared_height),
        )
        width = max(320, round(declared_width * draft_scale / 2) * 2)
        height = max(320, round(declared_height * draft_scale / 2) * 2)
        preset, crf = "ultrafast", 27
    elif mode == "preview":
        preset, crf = "veryfast", 22
    else:
        preset, crf = "slow", 18
    return Profile(mode, declared_width, declared_height, width, height, fps, preset, crf)


def resolved_roi(value: Any, width: int, height: int, field: str) -> tuple[int, int, int, int]:
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    if not isinstance(value, list) or len(value) != 4:
        raise RenderError(f"{field} must be normalized [x,y,w,h]")
    try:
        x, y, w, h = (float(part) for part in value)
    except (TypeError, ValueError) as exc:
        raise RenderError(f"{field} contains a non-numeric value") from exc
    if not all(math.isfinite(part) for part in (x, y, w, h)):
        raise RenderError(f"{field} contains a non-finite value")
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
        raise RenderError(f"{field} must stay inside normalized source bounds")
    px = min(width - 2, max(0, round(x * width)))
    py = min(height - 2, max(0, round(y * height)))
    pw = min(width - px, max(2, round(w * width)))
    ph = min(height - py, max(2, round(h * height)))
    return px, py, pw, ph


def output_box(value: Any, profile: Profile, field: str) -> tuple[int, int, int, int]:
    if isinstance(value, dict) and value.get("space") == "pixels":
        try:
            x, y, width, height = (round(float(value[key])) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError(f"{field} has invalid pixel coordinates") from exc
        if (
            x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > profile.declared_width
            or y + height > profile.declared_height
        ):
            raise RenderError(f"{field} must stay inside the declared output canvas")
        if profile.mode == "draft":
            # Pixel-space layout is authored against EDL output dimensions.
            # Materialize it onto the smaller draft canvas instead of treating
            # those declared pixels as if they belonged to the draft itself.
            left = min(profile.width - 2, max(0, round(x * profile.scale_x)))
            top = min(profile.height - 2, max(0, round(y * profile.scale_y)))
            right = min(profile.width, max(left + 2, round((x + width) * profile.scale_x)))
            bottom = min(profile.height, max(top + 2, round((y + height) * profile.scale_y)))
            x, y = left, top
            width = right - left
            height = bottom - top
        return x, y, width, height
    if isinstance(value, dict) and value.get("space") == "normalized":
        value = {key: value.get(key) for key in ("x", "y", "width", "height")}
    return resolved_roi(value, profile.width, profile.height, field)


def draft_overlay_value(value: Any, scale: float, *, field: str, minimum: int | None = None) -> Any:
    """Scale a declared-canvas numeric overlay value for a draft.

    FFmpeg expressions such as ``main_w-overlay_w-40`` are valid for the
    declared preview/final canvas, but rewriting their coordinate system would
    require an expression parser. Reject them in draft mode instead of silently
    placing the overlay differently. Plain numeric strings are safe to scale.
    """
    if isinstance(value, bool):
        raise RenderError(f"{field} must be numeric")
    numeric: float
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value.strip()):
        numeric = float(value)
    else:
        raise RenderError(
            f"{field} uses an FFmpeg expression that cannot be safely transformed for draft; "
            "use a numeric declared-canvas coordinate or render preview"
        )
    if not math.isfinite(numeric):
        raise RenderError(f"{field} must be finite")
    scaled = round(numeric * scale)
    return max(minimum, scaled) if minimum is not None else scaled


def overlay_geometry(item: dict[str, Any], profile: Profile, number: int) -> tuple[Any, Any, Any, Any]:
    """Resolve overlay geometry onto the current profile canvas."""
    full_frame = bool(item.get("full_frame"))
    width: Any = profile.width if full_frame else item.get("width")
    height: Any = profile.height if full_frame else item.get("height")
    x: Any = item.get("x", 0)
    y: Any = item.get("y", 0)
    if profile.mode != "draft":
        return width, height, x, y
    if not full_frame:
        if width is not None:
            width = draft_overlay_value(width, profile.scale_x, field=f"overlay {number}.width", minimum=2)
        if height is not None:
            height = draft_overlay_value(height, profile.scale_y, field=f"overlay {number}.height", minimum=2)
    x = draft_overlay_value(x, profile.scale_x, field=f"overlay {number}.x")
    y = draft_overlay_value(y, profile.scale_y, field=f"overlay {number}.y")
    return width, height, x, y


def layout_for_range(
    raw: dict[str, Any], layouts: list[dict[str, Any]], index: int
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for layout in layouts:
        if not isinstance(layout, dict) or layout.get("source") != raw.get("source"):
            continue
        try:
            if float(layout["start"]) <= float(raw["start"]) + 1e-6 and float(layout["end"]) >= float(raw["end"]) - 1e-6:
                matches.append(layout)
        except (KeyError, TypeError, ValueError):
            continue
    if len(matches) != 1:
        raise RenderError(f"range {index} must resolve to exactly one layout entry; found {len(matches)}")
    return dict(matches[0])


def clean_ranges(edl: dict[str, Any], sources: dict[str, SourceInfo]) -> list[dict[str, Any]]:
    raw_ranges = edl.get("ranges")
    if not isinstance(raw_ranges, list) or not raw_ranges:
        raise RenderError("EDL needs a non-empty ranges array")
    layouts = edl.get("layout_plan")
    if not isinstance(layouts, list) or not layouts:
        raise RenderError("EDL needs a resolved layout_plan")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_ranges):
        if not isinstance(raw, dict):
            raise RenderError(f"range {index} must be an object")
        source_id = str(raw.get("source") or "")
        if source_id not in sources:
            raise RenderError(f"range {index} references unknown source {source_id!r}")
        try:
            start, end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RenderError(f"range {index} has invalid start/end") from exc
        if start < 0 or end <= start or not math.isfinite(start + end):
            raise RenderError(f"range {index} has invalid bounds {start}-{end}")
        if end > sources[source_id].duration_s + 0.050:
            raise RenderError(f"range {index} ends after source duration")
        if end - start < 0.100:
            raise RenderError(f"range {index} is shorter than 100 ms")
        item = dict(raw)
        item.update(source=source_id, start=start, end=end)
        layout = layout_for_range(item, layouts, index)
        composition = str(layout.get("composition") or "preserve_source")
        if composition == "screen_with_presenter":
            composition = "presenter_with_screen"
        if composition not in {"preserve_source", "presenter_with_screen", "screen_only", "presenter_only"}:
            raise RenderError(f"range {index} has unsupported layout composition {composition!r}")
        layout["composition"] = composition
        if composition != "preserve_source" and (item.get("crop") is not None or item.get("view_filter") is not None):
            raise RenderError(f"range {index} cannot mix custom crop/view_filter with composed layout")
        item["_layout"] = layout
        screen_roi = resolved_roi(layout.get("screen_roi"), sources[source_id].width, sources[source_id].height, "layout.screen_roi") if layout.get("screen_roi") is not None else None
        important_roi = resolved_roi(layout.get("important_screen_roi"), sources[source_id].width, sources[source_id].height, "layout.important_screen_roi") if layout.get("important_screen_roi") is not None else None
        if screen_roi and important_roi:
            sx, sy, sw, sh = screen_roi
            ix, iy, iw, ih = important_roi
            if ix < sx or iy < sy or ix + iw > sx + sw or iy + ih > sy + sh:
                raise RenderError(f"range {index} important_screen_roi lies outside screen_roi")
        range_view_filters(item)
        result.append(item)
    return result


def build_segment_plan(ranges: list[dict[str, Any]], profile: Profile) -> list[SegmentPlan]:
    result: list[SegmentPlan] = []
    cumulative_duration = Fraction(0)
    cumulative_frames = 0
    cumulative_samples = 0
    for index, item in enumerate(ranges):
        duration = Fraction(str(item["end"])) - Fraction(str(item["start"]))
        cumulative_duration += duration
        # Quantize the cumulative timeline, not every edit independently. This
        # error-diffuses sub-frame remainders instead of adding up to one frame
        # at every cut.
        ideal_frames = cumulative_duration * profile.fps
        next_frames = max(cumulative_frames + 1, round(ideal_frames))
        frames = next_frames - cumulative_frames
        next_samples = round(next_frames * AUDIO_RATE * profile.fps.denominator / profile.fps.numerator)
        samples = next_samples - cumulative_samples
        result.append(SegmentPlan(index, item, frames, samples))
        cumulative_frames = next_frames
        cumulative_samples = next_samples
    return result


def roi_crop(layout: dict[str, Any], key: str, source: SourceInfo) -> str:
    x, y, width, height = resolved_roi(layout.get(key), source.width, source.height, f"layout.{key}")
    return f"crop={width}:{height}:{x}:{y}"


def finish_video_filters(plan: SegmentPlan, profile: Profile, grade: str | None) -> list[str]:
    parts = ["setsar=1", f"fps={profile.fps_expr}"]
    if grade and grade not in {"none", "null"}:
        parts.append(simple_filter(grade, field="EDL grade"))
    parts.extend([
        "format=yuv420p",
        # Normalize stream color metadata as well as pixels. Mixed sources can
        # otherwise reconfigure the decoder/filter graph at a cut, reset frame
        # counters used by exact QA, and produce player-dependent color shifts.
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "tpad=stop_mode=clone:stop_duration=0.100",
        f"trim=end_frame={plan.frame_count}",
        "setpts=PTS-STARTPTS",
    ])
    return parts


def video_filter_spec(
    plan: SegmentPlan, source: SourceInfo, profile: Profile, grade: str | None
) -> tuple[str, bool]:
    """Materialize the approved layout into either -vf or a labeled graph."""
    layout = plan.item["_layout"]
    composition = layout["composition"]
    pre = ["setpts=PTS-STARTPTS"]
    if source.color_transfer in HDR_TRANSFERS:
        pre.append(TONEMAP_CHAIN)

    if composition == "preserve_source":
        parts = [*pre, *range_view_filters(plan.item)]
        parts.extend([
            f"scale=w={profile.width}:h={profile.height}:flags=lanczos:force_original_aspect_ratio=decrease",
            f"pad=w={profile.width}:h={profile.height}:x=(ow-iw)/2:y=(oh-ih)/2:color=black",
            *finish_video_filters(plan, profile, grade),
        ])
        return ",".join(parts), False

    if composition in {"screen_only", "presenter_only"}:
        roi_key = "screen_roi" if composition == "screen_only" else "presenter_roi"
        parts = [*pre, roi_crop(layout, roi_key, source)]
        box_key = "screen_box" if composition == "screen_only" else "presenter_box"
        box_value = layout.get(box_key)
        output_shape = str(layout.get("output_shape") or "")
        if composition == "presenter_only" and output_shape == "circle":
            box_x, box_y, box_w, box_h = output_box(box_value, profile, f"layout.{box_key}")
            if abs(box_w - box_h) > 2:
                raise RenderError("circle presenter_box must be square")
            crop = roi_crop(layout, roi_key, source)
            mask = (
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                "a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2)\\,(W/2-2)*(W/2-2))\\,255\\,0)'"
            )
            graph = [
                f"[0:v]{','.join(pre)},{crop},"
                f"scale=w={box_w}:h={box_h}:flags=lanczos:force_original_aspect_ratio=decrease,"
                f"pad=w={box_w}:h={box_h}:x=(ow-iw)/2:y=(oh-ih)/2:color=0x121212,"
                f"format=rgba,{mask}[presenter]",
                f"color=c=black:s={profile.width}x{profile.height}:r={profile.fps_expr}[canvas]",
                f"[canvas][presenter]overlay=x={box_x}:y={box_y}:eof_action=pass:repeatlast=0,"
                + ",".join(finish_video_filters(plan, profile, grade))
                + "[vout]",
            ]
            return ";".join(graph), True
        if box_value is not None:
            box_x, box_y, box_w, box_h = output_box(box_value, profile, f"layout.{box_key}")
            parts.extend([
                f"scale=w={box_w}:h={box_h}:flags=lanczos:force_original_aspect_ratio=decrease",
                f"pad=w={box_w}:h={box_h}:x=(ow-iw)/2:y=(oh-ih)/2:color=black",
                f"pad=w={profile.width}:h={profile.height}:x={box_x}:y={box_y}:color=black",
                *finish_video_filters(plan, profile, grade),
            ])
            return ",".join(parts), False
        parts.extend([
            f"scale=w={profile.width}:h={profile.height}:flags=lanczos:force_original_aspect_ratio=decrease",
            f"pad=w={profile.width}:h={profile.height}:x=(ow-iw)/2:y=(oh-ih)/2:color=black",
            *finish_video_filters(plan, profile, grade),
        ])
        return ",".join(parts), False

    if composition != "presenter_with_screen":
        raise RenderError(f"unsupported layout composition: {composition}")

    screen_crop = roi_crop(layout, "screen_roi", source)
    presenter_crop = roi_crop(layout, "presenter_roi", source)
    box_x, box_y, box_w, box_h = output_box(layout.get("presenter_box"), profile, "layout.presenter_box")
    output_shape = str(layout.get("output_shape") or "")
    if output_shape not in {"rectangle", "circle"}:
        raise RenderError("presenter_with_screen needs output_shape rectangle or circle")
    if output_shape == "circle" and abs(box_w - box_h) > 2:
        raise RenderError("circle presenter_box must be square")

    graph: list[str] = [f"[0:v]{','.join(pre)},split=2[screen_src][presenter_src]"]
    screen_box_value = layout.get("screen_box")
    if screen_box_value is None:
        screen_x, screen_y, screen_w, screen_h = 0, 0, profile.width, profile.height
    else:
        screen_x, screen_y, screen_w, screen_h = output_box(screen_box_value, profile, "layout.screen_box")
    screen_chain = (
        f"[screen_src]{screen_crop},"
        f"scale=w={screen_w}:h={screen_h}:flags=lanczos:force_original_aspect_ratio=decrease,"
        f"pad=w={screen_w}:h={screen_h}:x=(ow-iw)/2:y=(oh-ih)/2:color=black"
    )
    if (screen_x, screen_y, screen_w, screen_h) != (0, 0, profile.width, profile.height):
        screen_chain += f",pad=w={profile.width}:h={profile.height}:x={screen_x}:y={screen_y}:color=black"
    graph.append(screen_chain + "[screen]")
    presenter_chain = (
        f"[presenter_src]{presenter_crop},"
        f"scale=w={box_w}:h={box_h}:flags=lanczos:force_original_aspect_ratio=decrease,"
        f"pad=w={box_w}:h={box_h}:x=(ow-iw)/2:y=(oh-ih)/2:color=0x121212,format=rgba"
    )
    if output_shape == "circle":
        # Keep an existing/isolated circular presenter as a true alpha cutout.
        # The escaped commas belong to the FFmpeg expression parser.
        mask = (
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            "a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2)\\,(W/2-2)*(W/2-2))\\,255\\,0)'"
        )
        presenter_chain += f",{mask}[presenter]"
    else:
        presenter_chain += "[presenter]"
    graph.append(presenter_chain)
    graph.append(
        f"[screen][presenter]overlay=x={box_x}:y={box_y}:eof_action=pass:repeatlast=0,"
        + ",".join(finish_video_filters(plan, profile, grade))
        + "[vout]"
    )
    return ";".join(graph), True


def audio_filter(plan: SegmentPlan, source: SourceInfo) -> str:
    audio_mode = plan.item.get("audio_mode")
    if audio_mode not in {"source", "mute"}:
        raise RenderError(
            f"range {plan.index} audio_mode must be exactly 'source' or 'mute'"
        )
    if audio_mode == "source" and not source.has_audio:
        raise RenderError(
            f"range {plan.index} requests source audio, but source has no audio stream"
        )
    use_source_audio = audio_mode == "source"
    target = plan.sample_count
    requested_samples = max(1, round((plan.item["end"] - plan.item["start"]) * AUDIO_RATE))
    available_samples = requested_samples
    if source.audio_duration_s is not None:
        available_s = max(0.0, source.audio_duration_s - plan.item["start"])
        available_samples = min(available_samples, round(available_s * AUDIO_RATE))
    audible = min(available_samples, target) if use_source_audio else 0
    parts = [
        f"aresample={AUDIO_RATE}:async=0:first_pts=0",
        "aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo",
    ]
    if use_source_audio and audible > 0:
        fade_out = max(0.0, audible / AUDIO_RATE - AUDIO_FADE_S)
        parts.extend([
            f"atrim=end_sample={audible}", "asetpts=PTS-STARTPTS",
            f"afade=t=in:st=0:d={AUDIO_FADE_S:.3f}",
            f"afade=t=out:st={fade_out:.6f}:d={AUDIO_FADE_S:.3f}",
        ])
    else:
        parts.extend(["atrim=end_sample=0", "asetpts=PTS-STARTPTS"])
    parts.extend([
        f"apad=whole_len={target}", f"atrim=end_sample={target}", "asetpts=PTS-STARTPTS",
    ])
    return ",".join(parts)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def segment_cache_recipe(
    plan: SegmentPlan,
    source: SourceInfo,
    profile: Profile,
    vf: str,
    af: str,
    renderer_identity_sha256: str,
    *,
    grade: str | None = None,
    video_is_complex: bool = False,
) -> dict[str, Any]:
    """Describe every input that can affect an extracted segment's bytes."""
    return {
        "version": 1,
        "renderer": {
            "name": RENDERER_VERSION,
            # renderer_identity covers render code plus FFmpeg/FFprobe binaries,
            # version output, and the linked libass binary when discoverable.
            "render_code_toolchain_identity_sha256": renderer_identity_sha256,
        },
        "source": {
            "id": plan.item["source"],
            "path": str(source.path),
            "sha256": source.fingerprint,
            "start_s": plan.item["start"],
            "end_s": plan.item["end"],
        },
        "edit": {
            "layout": plan.item["_layout"],
            "audio_mode": plan.item["audio_mode"],
            "crop": plan.item.get("crop"),
            "view_filter": plan.item.get("view_filter"),
            "grade": grade,
            "resolved_video_filter": vf,
            "resolved_video_filter_is_complex": video_is_complex,
            "resolved_audio_filter": af,
        },
        "timeline": {
            "video_frames": plan.frame_count,
            "audio_samples": plan.sample_count,
        },
        "profile": {
            "mode": profile.mode,
            "declared_width": profile.declared_width,
            "declared_height": profile.declared_height,
            "width": profile.width,
            "height": profile.height,
            "fps": profile.fps_expr, "preset": profile.preset, "crf": profile.crf,
        },
        "video_filter": vf,
        "audio_filter": af,
        "video_args": profile.video_args(),
        "audio_codec": "pcm_s16le/48000/stereo",
    }


def segment_recipe_sha256(recipe: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(recipe)).hexdigest()


def segment_digest(
    plan: SegmentPlan,
    source: SourceInfo,
    profile: Profile,
    vf: str,
    af: str,
    renderer_identity_sha256: str,
    *,
    grade: str | None = None,
    video_is_complex: bool = False,
) -> str:
    """Compatibility wrapper for callers that need only the cache key."""
    return segment_recipe_sha256(segment_cache_recipe(
        plan,
        source,
        profile,
        vf,
        af,
        renderer_identity_sha256,
        grade=grade,
        video_is_complex=video_is_complex,
    ))


def valid_segment_structure(path: Path, profile: Profile, plan: SegmentPlan) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 4096:
            return False
    except OSError:
        return False
    try:
        info = probe(path)
    except RenderError:
        return False
    valid_streams = bool(
        info["video_codec"] == "h264"
        and info["audio_codec"] == "pcm_s16le"
        and info["width"] == profile.width
        and info["height"] == profile.height
        and info["fps"] is not None
        and abs(float(info["fps"]) - profile.fps_float) < 0.02
        and info["duration_s"] > 0
    )
    if not valid_streams:
        return False
    try:
        counts = probe_exact_counts(path)
    except (RenderError, json.JSONDecodeError):
        return False
    return bool(
        counts["video_frames"] == plan.frame_count
        and counts["audio_samples"] == plan.sample_count
    )


def cache_attestation_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".attestation.json")


def cache_attestation(
    cache_path: Path,
    cache_key: str,
    recipe: dict[str, Any],
    segment_sha256: str,
    segment_size_bytes: int,
) -> dict[str, Any]:
    return {
        "version": CACHE_ATTESTATION_VERSION,
        "cache_key": cache_key,
        "render_recipe": recipe,
        "segment": {
            "file": cache_path.name,
            "sha256": segment_sha256,
            "size_bytes": segment_size_bytes,
        },
    }


def valid_attested_cached_segment(
    cache_path: Path,
    profile: Profile,
    plan: SegmentPlan,
    expected_cache_key: str,
    expected_recipe: dict[str, Any],
) -> tuple[bool, str | None]:
    """Fail closed unless the recipe sidecar and current bytes match exactly."""
    attestation_path = cache_attestation_path(cache_path)
    try:
        if cache_path.is_symlink() or attestation_path.is_symlink():
            return False, None
        if not cache_path.is_file() or not attestation_path.is_file():
            return False, None
        # A legitimate recipe is small. Bound parsing so a poisoned cache
        # cannot make the renderer consume an arbitrarily large JSON sidecar.
        if attestation_path.stat().st_size > 1_000_000:
            return False, None
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False, None
    if not isinstance(attestation, dict) or set(attestation) != {
        "version", "cache_key", "render_recipe", "segment"
    }:
        return False, None
    if attestation.get("version") != CACHE_ATTESTATION_VERSION:
        return False, None
    if attestation.get("cache_key") != expected_cache_key:
        return False, None
    if segment_recipe_sha256(expected_recipe) != expected_cache_key:
        return False, None
    try:
        if canonical_json_bytes(attestation.get("render_recipe")) != canonical_json_bytes(expected_recipe):
            return False, None
    except (TypeError, ValueError):
        return False, None
    segment = attestation.get("segment")
    if not isinstance(segment, dict) or set(segment) != {"file", "sha256", "size_bytes"}:
        return False, None
    attested_sha256 = segment.get("sha256")
    if (
        segment.get("file") != cache_path.name
        or not isinstance(attested_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", attested_sha256) is None
        or type(segment.get("size_bytes")) is not int
    ):
        return False, None
    try:
        if cache_path.stat().st_size != segment["size_bytes"]:
            return False, None
    except OSError:
        return False, None
    if not valid_segment_structure(cache_path, profile, plan):
        return False, None
    try:
        current_sha256 = file_sha256(cache_path)
    except OSError:
        return False, None
    if current_sha256 != attested_sha256:
        return False, None
    try:
        expected_attestation = cache_attestation(
            cache_path,
            expected_cache_key,
            expected_recipe,
            current_sha256,
            cache_path.stat().st_size,
        )
        if canonical_json_bytes(attestation) != canonical_json_bytes(expected_attestation):
            return False, None
    except (OSError, TypeError, ValueError):
        return False, None
    return True, current_sha256


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Publish JSON by one same-filesystem replace after durable file write."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_attested_cache_segment(
    temporary: Path,
    cache_path: Path,
    profile: Profile,
    plan: SegmentPlan,
    cache_key: str,
    recipe: dict[str, Any],
) -> str:
    """Validate bytes, replace the cache file, then atomically publish trust."""
    if segment_recipe_sha256(recipe) != cache_key:
        raise RenderError("cache key does not match the exact segment render recipe")
    if not valid_segment_structure(temporary, profile, plan):
        raise RenderError(f"rendered segment {plan.index} failed validation")
    segment_sha256 = file_sha256(temporary)
    payload = cache_attestation(
        cache_path, cache_key, recipe, segment_sha256, temporary.stat().st_size
    )
    attestation_path = cache_attestation_path(cache_path)
    # Invalidate trust before replacing bytes. A crash can leave an unusable
    # cache entry, never an old sidecar that authorizes new content.
    attestation_path.unlink(missing_ok=True)
    os.replace(temporary, cache_path)
    # Publish the canonical sidecar atomically and last. Reuse is impossible
    # until the complete attestation exists under this name.
    write_json_atomic(attestation_path, payload)
    return segment_sha256


def materialize_private_segment(
    cache_path: Path,
    ordered: Path,
    profile: Profile,
    plan: SegmentPlan,
    expected_sha256: str,
) -> None:
    """Copy attested bytes atomically so work output never shares a cache inode."""
    if ordered.is_symlink():
        ordered.unlink()
    if ordered.exists():
        try:
            private_and_valid = bool(
                ordered.stat().st_nlink == 1
                and valid_segment_structure(ordered, profile, plan)
                and file_sha256(ordered) == expected_sha256
            )
        except OSError:
            private_and_valid = False
        if private_and_valid:
            return
        ordered.unlink()
    temporary = ordered.with_name(f".{ordered.name}.{os.getpid()}.part")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(cache_path, temporary)
        if (
            temporary.is_symlink()
            or not valid_segment_structure(temporary, profile, plan)
            or file_sha256(temporary) != expected_sha256
        ):
            raise RenderError(
                f"ordered segment {plan.index} differs from its attested cache bytes"
            )
        os.replace(temporary, ordered)
    finally:
        temporary.unlink(missing_ok=True)


def extract_segment(
    plan: SegmentPlan,
    source: SourceInfo,
    profile: Profile,
    vf: str,
    video_is_complex: bool,
    af: str,
    output: Path,
) -> None:
    duration = plan.item["end"] - plan.item["start"]
    try:
        source_handle = source.path.open("rb")
    except OSError as exc:
        raise RenderError(f"cannot open approved source for segment {plan.index}") from exc
    with source_handle:
        verify_open_source(source_handle, source)
        input_reference, pass_fds = pinned_source_reference(source_handle, source)
        command = [
            *ffmpeg_base(), "-ss", f"{plan.item['start']:.6f}", "-t", f"{duration:.6f}",
            "-autorotate", "-i", input_reference,
        ]
        audio_mode = plan.item.get("audio_mode")
        if audio_mode not in {"source", "mute"}:
            raise RenderError(
                f"range {plan.index} audio_mode must be exactly 'source' or 'mute'"
            )
        use_source_audio = audio_mode == "source"
        if use_source_audio and not source.has_audio:
            raise RenderError(
                f"range {plan.index} requests source audio, but source has no audio stream"
            )
        if not use_source_audio:
            command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        audio_input = "0:a:0" if use_source_audio else "1:a:0"
        if video_is_complex:
            command.extend(["-filter_complex", vf, "-map", "[vout]"])
        else:
            command.extend(["-map", "0:v:0", "-vf", vf])
        command.extend([
            "-map", audio_input, "-sn", "-dn", "-af", af, *profile.video_args(),
            "-fps_mode", "cfr", "-r", profile.fps_expr,
            "-c:a", "pcm_s16le", "-ar", str(AUDIO_RATE), "-ac", "2",
            "-map_metadata", "-1", str(output),
        ])
        run(command, label=f"extract segment {plan.index}", pass_fds=pass_fds)
        # Inherited descriptor pinning defeats pathname swaps. This post-check
        # rejects in-place writes to the pinned inode before cache publication.
        verify_open_source(source_handle, source)


def build_segments(
    plans: list[SegmentPlan],
    sources: dict[str, SourceInfo],
    profile: Profile,
    grade: str | None,
    cache_dir: Path,
    work_dir: Path,
    use_cache: bool,
    renderer_identity_sha256: str,
) -> tuple[list[Path], list[dict[str, Any]]]:
    ordered_dir = work_dir / "segments"
    ordered_dir.mkdir(parents=True, exist_ok=True)
    segment_paths: list[Path] = []
    records: list[dict[str, Any]] = []
    hits = 0
    for plan in plans:
        source = sources[plan.item["source"]]
        # Refuse both rendering and cache reuse if the approved source path no
        # longer names the bytes represented by source.fingerprint.
        verify_source_path(source)
        vf, video_is_complex = video_filter_spec(plan, source, profile, grade)
        af = audio_filter(plan, source)
        recipe = segment_cache_recipe(
            plan,
            source,
            profile,
            vf,
            af,
            renderer_identity_sha256,
            grade=grade,
            video_is_complex=video_is_complex,
        )
        digest = segment_recipe_sha256(recipe)
        cache_path = cache_dir / profile.mode / f"{digest}.mov"
        render_path = cache_path if use_cache else ordered_dir / f"seg_{plan.index:04d}.mov"
        cache_valid, segment_sha256 = (
            valid_attested_cached_segment(cache_path, profile, plan, digest, recipe)
            if use_cache else (False, None)
        )
        if cache_valid:
            hits += 1
            log(f"segment {plan.index + 1}/{len(plans)} attested cache hit {digest[:12]}")
        else:
            render_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = render_path.with_name(f".{render_path.stem}.{os.getpid()}.part.mov")
            temporary.unlink(missing_ok=True)
            try:
                extract_segment(plan, source, profile, vf, video_is_complex, af, temporary)
                verify_source_path(source)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            if use_cache:
                try:
                    segment_sha256 = publish_attested_cache_segment(
                        temporary, cache_path, profile, plan, digest, recipe
                    )
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            else:
                if not valid_segment_structure(temporary, profile, plan):
                    temporary.unlink(missing_ok=True)
                    raise RenderError(f"rendered segment {plan.index} failed validation")
                segment_sha256 = file_sha256(temporary)
                temporary.replace(render_path)
        if segment_sha256 is None:
            raise RenderError(f"segment {plan.index} has no verified content hash")
        if use_cache:
            ordered = ordered_dir / f"seg_{plan.index:04d}_{digest[:12]}.mov"
            materialize_private_segment(
                cache_path, ordered, profile, plan, segment_sha256
            )
            if (
                not valid_segment_structure(ordered, profile, plan)
                or file_sha256(ordered) != segment_sha256
            ):
                raise RenderError(
                    f"ordered segment {plan.index} differs from its attested cache bytes"
                )
        else:
            ordered = render_path
        info = probe(ordered)
        segment_paths.append(ordered)
        records.append({
            "index": plan.index,
            "source": plan.item["source"],
            "source_start_s": plan.item["start"],
            "source_end_s": plan.item["end"],
            "audio_mode": plan.item["audio_mode"],
            "range_contract": {
                key: value for key, value in plan.item.items() if key != "_layout"
            },
            "frames": plan.frame_count,
            "audio_samples": plan.sample_count,
            "exact_duration_s": plan.frame_count / profile.fps_float,
            "actual_duration_s": info["duration_s"],
            "layout": plan.item["_layout"],
            "cache_key": digest,
            "sha256": segment_sha256,
            "cache_attestation": (
                str(cache_attestation_path(cache_path)) if use_cache else None
            ),
            "path": str(ordered),
        })
    log(f"segments ready: {len(segment_paths)} ({hits} validated cache hits)")
    return segment_paths, records


def ffconcat_quote(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def concat_segments(segments: list[Path], output: Path, list_path: Path) -> None:
    list_path.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{ffconcat_quote(path)}'\n" for path in segments),
        encoding="utf-8",
    )
    run([
        *ffmpeg_base(), "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", str(output),
    ], label="lossless concat")


def anchor_index(raw: dict[str, Any], records: list[dict[str, Any]], number: int) -> tuple[int | None, bool]:
    if "start_at_range_index" in raw:
        index = int(raw["start_at_range_index"])
        after = False
    elif "start_after_range_index" in raw:
        index = int(raw["start_after_range_index"])
        after = True
    else:
        return None, False
    if not 0 <= index < len(records):
        raise RenderError(f"overlay/SFX {number} range anchor is out of bounds")
    return index, after


def resolve_visual_overlays(
    items: list[dict[str, Any]], records: list[dict[str, Any]], profile: Profile, total_frames: int
) -> list[dict[str, Any]]:
    prefix = [0]
    for record in records:
        prefix.append(prefix[-1] + int(record["frames"]))
    resolved: list[dict[str, Any]] = []
    for number, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise RenderError(f"overlay {number} must be an object")
        item = dict(raw)
        offset = float(item.pop("offset_s", 0.0))
        index, after = anchor_index(item, records, number)
        if index is not None:
            start_frame = prefix[index + (1 if after else 0)] + round(offset * profile.fps_float)
        elif item.pop("align_to_end", False):
            duration_frames = max(1, round(float(item["duration"]) * profile.fps_float))
            start_frame = total_frames - duration_frames - round(offset * profile.fps_float)
        else:
            start_value = item.get("start_in_output", item.get("start"))
            if start_value is None:
                raise RenderError(f"overlay {number} needs one start anchor")
            start_frame = round(float(start_value) * profile.fps_float)
        duration_frames = max(1, round(float(item.get("duration") or 0) * profile.fps_float))
        end_frame = start_frame + duration_frames
        if start_frame < 0 or end_frame > total_frames:
            raise RenderError(f"overlay {number} timing is outside the programme")
        item.update(
            start_frame=start_frame,
            end_frame=end_frame,
            start_in_output=start_frame / profile.fps_float,
            duration=duration_frames / profile.fps_float,
        )
        resolved.append(item)
    return resolved


def resolve_audio_overlays(
    items: list[dict[str, Any]], records: list[dict[str, Any]], total_samples: int
) -> list[dict[str, Any]]:
    prefix = [0]
    for record in records:
        prefix.append(prefix[-1] + int(record["audio_samples"]))
    resolved: list[dict[str, Any]] = []
    for number, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise RenderError(f"SFX {number} must be an object")
        item = dict(raw)
        offset_samples = round(float(item.pop("offset_s", 0.0)) * AUDIO_RATE)
        index, after = anchor_index(item, records, number)
        duration_samples: int | None = None
        if item.get("duration") is not None:
            duration_samples = max(1, round(float(item["duration"]) * AUDIO_RATE))
        if index is not None:
            start_sample = prefix[index + (1 if after else 0)] + offset_samples
        elif item.pop("align_to_end", False):
            if duration_samples is None:
                raise RenderError(f"SFX {number} align_to_end requires duration")
            start_sample = total_samples - duration_samples - offset_samples
        else:
            start_value = item.get("start_in_output", item.get("start"))
            if start_value is None:
                raise RenderError(f"SFX {number} needs one start anchor")
            start_sample = round(float(start_value) * AUDIO_RATE)
        if start_sample < 0 or start_sample >= total_samples:
            raise RenderError(f"SFX {number} start is outside the programme")
        if duration_samples is not None and start_sample + duration_samples > total_samples:
            duration_samples = total_samples - start_sample
        item.update(
            start_sample=start_sample,
            start_in_output=start_sample / AUDIO_RATE,
            duration_samples=duration_samples,
        )
        if duration_samples is not None:
            item["duration"] = duration_samples / AUDIO_RATE
        resolved.append(item)
    return resolved


def subtitle_filter(path: Path) -> str:
    escaped = str(path.resolve()).replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
    return f"subtitles='{escaped}'"


def composite(
    base: Path,
    overlays: list[dict[str, Any]],
    sfx: list[dict[str, Any]],
    subtitles: Path | None,
    profile: Profile,
    edit_dir: Path,
    duration: float,
    total_samples: int,
    output: Path,
) -> Path:
    video_changed = bool(overlays or subtitles)
    audio_changed = bool(sfx)
    if not video_changed and not audio_changed:
        return base

    inputs: list[str] = ["-i", str(base)]
    for item in overlays:
        path = resolve_path(str(item.get("file") or ""), edit_dir)
        if not path.is_file():
            raise RenderError(f"overlay not found: {path}")
        inputs.extend(["-i", str(path)])
    for item in sfx:
        path = resolve_path(str(item.get("file") or ""), edit_dir)
        if not path.is_file():
            raise RenderError(f"SFX not found: {path}")
        inputs.extend(["-i", str(path)])

    filters: list[str] = []
    video_map = "0:v:0"
    if video_changed:
        filters.append("[0:v]setpts=PTS-STARTPTS[v0]")
        current = "[v0]"
        for number, item in enumerate(overlays, start=1):
            start_frame = int(item["start_frame"])
            duration_frames = int(item["end_frame"]) - start_frame
            chain = [f"[{number}:v]setpts=PTS-STARTPTS", f"fps={profile.fps_expr}"]
            width, height, x, y = overlay_geometry(item, profile, number)
            if width is not None or height is not None:
                target_w = width if width is not None else -2
                target_h = height if height is not None else -2
                fit_mode = str(item.get("fit_mode") or "contain")
                if fit_mode == "stretch":
                    chain.append(f"scale={target_w}:{target_h}:flags=lanczos")
                elif fit_mode == "cover" and width is not None and height is not None:
                    chain.extend([
                        f"scale={target_w}:{target_h}:flags=lanczos:force_original_aspect_ratio=increase",
                        f"crop={target_w}:{target_h}",
                    ])
                elif fit_mode == "contain" and width is not None and height is not None:
                    # A positioned overlay must retain transparency around its
                    # contained image. An opaque black pad is appropriate only
                    # for an approved full-frame card.
                    pad_color = "black" if item.get("full_frame") else "0x00000000"
                    if not item.get("full_frame"):
                        chain.append("format=rgba")
                    chain.extend([
                        f"scale={target_w}:{target_h}:flags=lanczos:force_original_aspect_ratio=decrease",
                        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color={pad_color}",
                    ])
                elif fit_mode == "contain":
                    chain.append(f"scale={target_w}:{target_h}:flags=lanczos:force_original_aspect_ratio=decrease")
                else:
                    raise RenderError(f"overlay {number} has invalid fit_mode {fit_mode!r}")
            chain.extend([
                "format=rgba", f"tpad=stop_mode=clone:stop={duration_frames}",
                f"trim=end_frame={duration_frames}",
            ])
            if item.get("filter"):
                chain.append(simple_filter(item["filter"], field=f"overlay {number}.filter"))
            # `fps` establishes one PTS tick per output frame. Restore that
            # time base after any optional filter, then use the already
            # resolved integer frame anchor. Formatting start seconds to six
            # decimals can otherwise turn N/fps into N-epsilon and make
            # framesync drop the first overlay frame and expose the base on
            # the final enabled frame.
            chain.extend([
                f"settb=expr={profile.fps.denominator}/{profile.fps.numerator}",
                f"setpts=PTS-STARTPTS+{start_frame}[ov{number}]",
            ])
            filters.append(",".join(chain))
            x = str(x)
            y = str(y)
            if any(char in x + y for char in ";[]'"):
                raise RenderError(f"overlay {number} has unsafe x/y expression")
            next_label = f"[v{number}]"
            filters.append(
                f"{current}[ov{number}]overlay=x={x}:y={y}:eof_action=pass:repeatlast=0:shortest=0:"
                f"enable='gte(n,{int(item['start_frame'])})*lt(n,{int(item['end_frame'])})'{next_label}"
            )
            current = next_label
        if subtitles:
            filters.append(f"{current}{subtitle_filter(subtitles)}[vsub]")
            current = "[vsub]"
        filters.append(f"{current}format=yuv420p[vout]")
        video_map = "[vout]"

    audio_map = "0:a:0"
    if audio_changed:
        filters.append("[0:a]aresample=48000,asetpts=N/SR/TB[abase]")
        labels = ["[abase]"]
        first_sfx_index = 1 + len(overlays)
        for number, item in enumerate(sfx, start=1):
            gain = float(item.get("gain_db", -12.0))
            if not math.isfinite(gain) or not -60 <= gain <= 12:
                raise RenderError(f"SFX {number} gain_db must be between -60 and +12")
            trim = (
                f",atrim=end_sample={int(item['duration_samples'])}"
                if item.get("duration_samples") is not None else ""
            )
            fade = ""
            if item.get("duration_samples") is not None:
                sfx_duration = int(item["duration_samples"]) / AUDIO_RATE
                fade_d = min(0.015, sfx_duration / 4)
                fade = f",afade=t=in:st=0:d={fade_d:.6f},afade=t=out:st={max(0.0, sfx_duration-fade_d):.6f}:d={fade_d:.6f}"
            input_index = first_sfx_index + number - 1
            label = f"[sfx{number}]"
            filters.append(
                f"[{input_index}:a]asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo"
                f"{trim}{fade},volume={gain:.3f}dB,"
                f"adelay=delays={int(item['start_sample'])}S:all=1,apad,atrim=end_sample={total_samples},"
                f"asetpts=N/SR/TB{label}"
            )
            labels.append(label)
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
            "alimiter=limit=0.95,asetpts=N/SR/TB[aout]"
        )
        audio_map = "[aout]"

    command = [*ffmpeg_base(), *inputs, "-filter_complex", ";".join(filters), "-map", video_map, "-map", audio_map]
    if video_changed:
        command.extend([*profile.video_args(), "-fps_mode", "cfr", "-r", profile.fps_expr])
    else:
        command.extend(["-c:v", "copy"])
    command.extend(["-c:a", "pcm_s16le" if audio_changed else "copy", "-map_metadata", "-1", str(output)])
    run(command, label=f"composite overlays={len(overlays)} sfx={len(sfx)} subtitles={'yes' if subtitles else 'no'}")
    return output


def parse_loudnorm(stderr: str) -> dict[str, str]:
    blocks = re.findall(r"\{\s*\"input_i\".*?\}", stderr, flags=re.DOTALL)
    if not blocks:
        raise RenderError("loudnorm pass 1 did not return measurement JSON")
    data = json.loads(blocks[-1])
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data):
        raise RenderError("loudnorm measurement is incomplete")
    return {key: str(data[key]) for key in needed}


def audio_cleanup(edl: dict[str, Any]) -> list[str]:
    raw = (edl.get("audio") or {}).get("filters") or []
    if not isinstance(raw, list):
        raise RenderError("EDL audio.filters must be an array")
    result: list[str] = []
    for index, item in enumerate(raw):
        value = simple_filter(item, field=f"audio.filters[{index}]")
        parts: list[str] = []
        start = 0
        quote: str | None = None
        escaped = False
        depth = 0
        for position, character in enumerate(value):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0:
                    raise RenderError(f"audio.filters[{index}] has unbalanced parentheses")
            elif character == "," and depth == 0:
                parts.append(value[start:position].strip())
                start = position + 1
        if quote is not None or depth != 0 or escaped:
            raise RenderError(f"audio.filters[{index}] has an unterminated expression")
        parts.append(value[start:].strip())
        if any(not part for part in parts):
            raise RenderError(f"audio.filters[{index}] contains an empty filter")
        for part in parts:
            match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(?:=|$)", part)
            name = match.group(1).lower() if match else ""
            if name not in AUDIO_CLEANUP_FILTERS:
                raise RenderError(
                    f"audio.filters[{index}] uses {name or part!r}; only duration/PTS-preserving "
                    f"cleanup filters are allowed: {sorted(AUDIO_CLEANUP_FILTERS)}"
                )
        result.append(value)
    return result


def loudnorm_finalize(
    source: Path,
    output: Path,
    duration: float,
    cleanup: list[str],
    target_i: float,
    target_tp: float,
    target_lra: float,
) -> None:
    prefix = ",".join(cleanup)
    measure_filter = (prefix + "," if prefix else "") + (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    )
    command = [
        *ffmpeg_base(), "-i", str(source), "-map", "0:a:0", "-af", measure_filter,
        "-vn", "-f", "null", "-",
    ]
    command[command.index("error")] = "info"
    measured = parse_loudnorm(run(command, label="loudnorm pass 1/2", capture=True).stderr)
    try:
        finite_measurement = all(math.isfinite(float(value)) for value in measured.values())
    except ValueError:
        finite_measurement = False
    temporary = output.with_name(f".{output.stem}.{os.getpid()}.rendering{output.suffix}")
    if not finite_measurement:
        # FFmpeg reports -inf/inf for a completely silent programme and rejects
        # those values in linear pass 2. Silence is already at a safe level, so
        # keep the exact PCM timeline and perform only the single delivery encode.
        bypass_filter = (prefix + "," if prefix else "") + "asetpts=N/SR/TB"
        run([
            *ffmpeg_base(), "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-af", bypass_filter, "-c:a", "aac", "-b:a", AAC_BITRATE,
            "-ar", str(AUDIO_RATE), "-ac", "2", "-t", f"{duration:.6f}",
            "-map_metadata", "-1", "-movflags", "+faststart", str(temporary),
        ], label="silent programme bypass + one AAC encode")
        temporary.replace(output)
        return
    final_filter = (prefix + "," if prefix else "") + (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=summary,asetpts=N/SR/TB"
    )
    run([
        *ffmpeg_base(), "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-af", final_filter, "-c:a", "aac", "-b:a", AAC_BITRATE,
        "-ar", str(AUDIO_RATE), "-ac", "2", "-t", f"{duration:.6f}",
        "-map_metadata", "-1", "-movflags", "+faststart", str(temporary),
    ], label="loudnorm pass 2/2 + one AAC encode")
    temporary.replace(output)


def run_gate(edit_dir: Path, edl: Path, phase: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "validate_gate.py"), "--edit-dir", str(edit_dir), "--phase", phase, "--edl", str(edl)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RenderError(f"approval/render gate failed\n{(result.stdout + result.stderr).strip()}")
    log(result.stdout.strip())


def work_identity(
    edl_sha256: str,
    profile: Profile,
    fingerprints: dict[str, str],
    renderer_identity_sha256: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(edl_sha256.encode("ascii"))
    digest.update(RENDERER_VERSION.encode())
    digest.update(profile.mode.encode())
    digest.update(json.dumps(fingerprints, sort_keys=True).encode())
    digest.update(renderer_identity_sha256.encode())
    return digest.hexdigest()[:16]


def asset_records(
    items: list[dict[str, Any]], edit_dir: Path, label: str
) -> tuple[list[dict[str, Any]], list[FileSnapshot]]:
    records: list[dict[str, Any]] = []
    snapshots: list[FileSnapshot] = []
    seen: set[Path] = set()
    for item in items:
        path = resolve_path(str(item.get("file") or ""), edit_dir)
        if path in seen:
            continue
        seen.add(path)
        snapshot, _ = capture_file_snapshot(path, f"{label} asset")
        snapshots.append(snapshot)
        records.append({"path": str(path), "sha256": snapshot.sha256})
    return records, snapshots


def visual_asset_records(
    items: list[dict[str, Any]], edit_dir: Path
) -> tuple[list[dict[str, Any]], list[FileSnapshot]]:
    """Snapshot and verify every visual together with its semantic sidecar."""
    records: list[dict[str, Any]] = []
    snapshots: list[FileSnapshot] = []
    seen: dict[Path, Path] = {}
    for index, item in enumerate(items):
        path = resolve_path(str(item.get("file") or ""), edit_dir)
        provenance_path = resolve_path(str(item.get("provenance") or ""), edit_dir)
        prior = seen.get(path)
        if prior is not None:
            if prior != provenance_path:
                raise RenderError(
                    f"visual asset {path} is referenced with multiple provenance sidecars"
                )
            continue
        seen[path] = provenance_path
        asset_snapshot, _ = capture_file_snapshot(path, f"visual asset {index}")
        provenance_snapshot, _ = capture_file_snapshot(
            provenance_path, f"visual provenance {index}"
        )
        try:
            verify_visual_asset_provenance(
                edit_dir,
                provenance_path,
                asset_path=path,
                overlay=item,
            )
        except (OSError, VisualProvenanceError) as exc:
            raise RenderError(f"visual asset {index} provenance is invalid: {exc}") from exc
        assert_file_snapshots([asset_snapshot, provenance_snapshot])
        snapshots.extend([asset_snapshot, provenance_snapshot])
        records.append(
            {
                "path": str(path),
                "sha256": asset_snapshot.sha256,
                "provenance": {
                    "path": str(provenance_path),
                    "sha256": provenance_snapshot.sha256,
                },
            }
        )
    return records, snapshots


def render(args: argparse.Namespace) -> None:
    require_tools()
    edl_path = args.edl.expanduser().resolve()
    edit_dir = edl_path.parent
    edl, edl_snapshot = load_json_snapshot(edl_path, "EDL")
    deliverable_id = edl.get("deliverable_id")
    deliverable_artifact_key = artifact_key(deliverable_id)
    render_identity = renderer_identity()
    configure_render_tools(render_identity)
    output = args.output.expanduser().resolve()
    ensure_inside(output, edit_dir, "output")
    if output.suffix.lower() != ".mp4":
        raise RenderError("output must use .mp4")
    if output.exists() and not args.force:
        raise RenderError(f"output exists; use --force to replace: {output}")
    mode = "draft" if args.draft else "preview" if args.preview else "final"
    if mode == "final" and args.no_subtitles:
        raise RenderError("--no-subtitles cannot override an approved EDL during final render")
    project, project_snapshot = load_json_snapshot(edit_dir / "project.json", "project")
    control_snapshots, source_manifest, preview_approval_snapshot = capture_control_inputs(
        edit_dir,
        edl_snapshot,
        project,
        project_snapshot,
        mode=mode,
        deliverable_id=str(deliverable_id),
    )
    run_gate(edit_dir, edl_path, "final" if mode == "final" else "render")
    assert_file_snapshots(control_snapshots)

    profile = build_profile(edl, mode)
    approved_sources = load_manifest_sources(edit_dir, project, source_manifest)
    sources = build_sources(edl, edit_dir, approved_sources)
    fingerprints = {source_id: source.fingerprint for source_id, source in sources.items()}
    ranges = clean_ranges(edl, sources)
    plans = build_segment_plan(ranges, profile)
    total_frames = sum(plan.frame_count for plan in plans)
    total_samples = sum(plan.sample_count for plan in plans)
    programme_duration = total_frames / profile.fps_float
    cache_dir = (args.cache_dir or edit_dir / "cache").expanduser().resolve()
    work_root = (args.work_dir or edit_dir / "work").expanduser().resolve()
    ensure_inside(cache_dir, edit_dir, "cache directory")
    ensure_inside(work_root, edit_dir, "work directory")
    work_dir = (
        work_root
        / deliverable_artifact_key
        / f"{mode}_{work_identity(edl_snapshot.sha256, profile, fingerprints, render_identity['identity_sha256'])}"
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    log(
        f"mode={mode} declared={profile.declared_width}x{profile.declared_height} "
        f"profile={profile.width}x{profile.height}@{profile.fps_expr} ranges={len(ranges)}"
    )
    log("intermediates=H.264+PCM; per-segment PTS reset; AAC encoded once")
    segments, records = build_segments(
        plans,
        sources,
        profile,
        edl.get("grade"),
        cache_dir,
        work_dir,
        not args.no_cache,
        render_identity["identity_sha256"],
    )
    base = work_dir / "base_pcm.mov"
    concat_segments(segments, base, work_dir / "segments.ffconcat")
    overlays = resolve_visual_overlays(edl.get("overlays") or [], records, profile, total_frames)
    sfx = resolve_audio_overlays(edl.get("audio_overlays") or [], records, total_samples)
    subtitle_mode = str(edl.get("subtitle_mode") or ("burned" if edl.get("subtitles") else "none"))
    if args.no_subtitles:
        subtitle_mode = "none"
    if subtitle_mode not in {"none", "burned", "sidecar"}:
        raise RenderError("subtitle_mode must be none, burned, or sidecar")
    subtitles: Path | None = None
    if subtitle_mode in {"burned", "sidecar"}:
        if not edl.get("subtitles"):
            raise RenderError(f"subtitle_mode={subtitle_mode} but EDL has no subtitles path")
        subtitles = resolve_path(str(edl["subtitles"]), edit_dir)
        if not subtitles.is_file():
            raise RenderError(f"subtitles not found: {subtitles}")
        if subtitles.suffix.lower() not in {".srt", ".vtt", ".ass", ".ssa"}:
            raise RenderError("subtitles must use .srt, .vtt, .ass, or .ssa")

    visual_assets, visual_snapshots = visual_asset_records(overlays, edit_dir)
    audio_assets, audio_snapshots = asset_records(sfx, edit_dir, "audio")
    consumed_snapshots = [*control_snapshots, *visual_snapshots, *audio_snapshots]
    subtitle_snapshot: FileSnapshot | None = None
    if subtitles is not None:
        subtitle_snapshot, _ = capture_file_snapshot(subtitles, "subtitle source")
        consumed_snapshots.append(subtitle_snapshot)
    subtitle_asset = (
        {"path": str(subtitles), "sha256": subtitle_snapshot.sha256}
        if subtitles is not None and subtitle_snapshot is not None
        else None
    )
    font_assets = (
        resolve_subtitle_fonts(subtitles)
        if subtitle_mode == "burned" and subtitles is not None
        else []
    )
    for index, font in enumerate(font_assets):
        font_snapshot, _ = capture_file_snapshot(
            Path(str(font.get("path") or "")), f"subtitle font {index}"
        )
        if font_snapshot.sha256 != font.get("sha256"):
            raise RenderError(f"subtitle font {index} changed during resolution")
        consumed_snapshots.append(font_snapshot)
    protected_paths = {
        edl_path,
        (edit_dir / "project.json").resolve(),
        *(source.path for source in sources.values()),
        *(Path(item["path"]).resolve() for item in visual_assets),
        *(
            Path(item["provenance"]["path"]).resolve()
            for item in visual_assets
        ),
        *(Path(item["path"]).resolve() for item in audio_assets),
    }
    if subtitles:
        protected_paths.add(subtitles.resolve())
    sidecar_path: Path | None = None
    if subtitle_mode == "sidecar" and subtitles:
        sidecar_path = output.with_suffix(subtitles.suffix.lower())
        if sidecar_path == output or sidecar_path.resolve() in protected_paths:
            raise RenderError("sidecar subtitle path collides with protected input/output")
        if sidecar_path.exists() and not args.force:
            raise RenderError(f"subtitle sidecar exists; use --force to replace: {sidecar_path}")
    if output in protected_paths:
        raise RenderError("output path collides with an input, source, or configuration file")
    final_authorization: dict[str, Any] | None = None
    if mode == "final":
        preview_approval_path = (
            edit_dir / preview_approval_name(deliverable_id)
        ).resolve()
        preview_approval = preview_approval_snapshot
        if not isinstance(preview_approval, dict):
            raise RenderError("preview approval was not snapshotted before the final gate")
        if preview_approval.get("deliverable_id") != deliverable_id:
            raise RenderError("preview approval belongs to a different deliverable")
        if preview_approval.get("artifact_key") != deliverable_artifact_key:
            raise RenderError("preview approval has a non-canonical artifact key")
        if preview_approval.get("renderer") != RENDERER_VERSION:
            raise RenderError("approved preview was created by a different renderer version")
        if preview_approval.get("renderer_identity") != render_identity:
            raise RenderError(
                "renderer implementation or FFmpeg/FFprobe identity differs from the approved preview"
            )
        if preview_approval.get("font_assets") != font_assets:
            raise RenderError("burned-subtitle font resolution differs from the approved preview")
        if preview_approval.get("visual_assets") != visual_assets:
            raise RenderError(
                "visual asset/provenance bytes differ from the approved preview"
            )
        approved_preview = resolve_path(
            str(preview_approval.get("preview_file") or ""), edit_dir
        )
        ensure_inside(approved_preview, edit_dir, "approved preview")
        if output == approved_preview:
            raise RenderError("final output cannot overwrite the approved preview")
        approved_preview_sidecar = validate_approved_preview_sidecar(
            preview_approval,
            subtitle_mode=subtitle_mode,
            subtitles=subtitles,
            subtitle_sha256=(subtitle_snapshot.sha256 if subtitle_snapshot else None),
            approved_preview=approved_preview,
            edit_dir=edit_dir,
            control_snapshots=control_snapshots,
        )
        final_authorization = {
            "preview_approval": str(preview_approval_path),
            "preview_approval_sha256": next(
                item.sha256 for item in control_snapshots if item.path == preview_approval_path
            ),
            "preview_file": str(approved_preview),
            "preview_sha256": preview_approval.get("preview_sha256"),
            "preview_render_manifest_sha256": preview_approval.get("render_manifest_sha256"),
            "preview_qa_report_sha256": preview_approval.get("qa_report_sha256"),
            "renderer_identity_sha256": render_identity.get("identity_sha256"),
            "visual_assets": visual_assets,
            "font_assets": font_assets,
            "preview_sidecar": approved_preview_sidecar,
        }

    # Refuse to consume mutable assets/control files after a long segment build
    # unless every byte still equals the gate-time snapshot.
    assert_file_snapshots(consumed_snapshots)
    prenorm = composite(
        base, overlays, sfx, subtitles if subtitle_mode == "burned" else None,
        profile, edit_dir, programme_duration, total_samples, work_dir / "composited_pcm.mov",
    )
    assert_file_snapshots(consumed_snapshots)
    audio_config = project.get("audio") or {}
    if mode == "final":
        invalidate_release_state(
            edit_dir,
            deliverable_id,
            "final render started; release QA has not passed for the new output",
            render_manifest=edit_dir / render_manifest_name(deliverable_id, "final"),
        )
    loudnorm_finalize(
        prenorm, output, programme_duration, audio_cleanup(edl),
        float(audio_config.get("target_lufs", DEFAULT_LUFS)),
        float(audio_config.get("true_peak_dbtp", DEFAULT_TRUE_PEAK)),
        float(audio_config.get("loudness_range_lu", DEFAULT_LRA)),
    )
    if subtitle_mode == "sidecar" and subtitles:
        assert sidecar_path is not None
        sidecar_temporary = sidecar_path.with_name(f".{sidecar_path.stem}.part{sidecar_path.suffix}")
        shutil.copy2(subtitles, sidecar_temporary)
        sidecar_temporary.replace(sidecar_path)

    final_info = probe(output)
    cumulative_frames = 0
    cut_times = []
    for record in records[:-1]:
        cumulative_frames += int(record["frames"])
        cut_times.append(cumulative_frames / profile.fps_float)
    cleanup_filters = audio_cleanup(edl)
    assert_file_snapshots(consumed_snapshots)
    for source in sources.values():
        verify_source_path(source)
    current_render_identity = renderer_identity()
    if current_render_identity != render_identity:
        raise RenderError("renderer implementation or toolchain changed during render")
    manifest = {
        "version": 2,
        "renderer": RENDERER_VERSION,
        "renderer_identity": render_identity,
        "mode": mode,
        "deliverable_id": deliverable_id,
        "artifact_key": deliverable_artifact_key,
        "edl": str(edl_path),
        "edl_sha256": edl_snapshot.sha256,
        "project": str((edit_dir / "project.json").resolve()),
        "project_sha256": project_snapshot.sha256,
        "control_inputs": snapshot_records(control_snapshots),
        "approval_plan_sha256": edl.get("approval_plan_sha256"),
        "source_fingerprints": fingerprints,
        "source_geometry": {
            source_id: {
                "coded_width": source.coded_width,
                "coded_height": source.coded_height,
                "display_width": source.width,
                "display_height": source.height,
                "display_rotation_degrees": source.display_rotation_degrees,
            }
            for source_id, source in sources.items()
        },
        "profile": {
            "declared_width": profile.declared_width,
            "declared_height": profile.declared_height,
            "width": profile.width,
            "height": profile.height,
            "fps": profile.fps_expr,
        },
        "expected_total_frames": total_frames,
        "expected_total_audio_samples": total_samples,
        "expected_duration_s": programme_duration,
        "segments_dir": str(work_dir / "segments"),
        "segments": records,
        "cut_times_s": cut_times,
        "overlays": overlays,
        "audio_overlays": sfx,
        "visual_assets": visual_assets,
        "audio_assets": audio_assets,
        "subtitle_mode": subtitle_mode,
        "subtitle_asset": subtitle_asset,
        "font_assets": font_assets,
        "sidecar": str(sidecar_path) if sidecar_path else None,
        "sidecar_sha256": file_sha256(sidecar_path) if sidecar_path else None,
        "final_authorization": final_authorization,
        "audio_processing": {
            "filters": cleanup_filters,
            "target_lufs": float(audio_config.get("target_lufs", DEFAULT_LUFS)),
            "true_peak_dbtp": float(audio_config.get("true_peak_dbtp", DEFAULT_TRUE_PEAK)),
            "loudness_range_lu": float(audio_config.get("loudness_range_lu", DEFAULT_LRA)),
        },
        "output": {"path": str(output), "sha256": file_sha256(output), **final_info},
    }
    manifest_path = edit_dir / render_manifest_name(deliverable_id, mode)
    write_json_atomic(manifest_path, manifest)
    log(f"done: {output} | {final_info['duration_s']:.3f}s | manifest={manifest_path.name}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Render an approved semantic EDL safely")
    result.add_argument("edl", type=Path)
    result.add_argument("-o", "--output", type=Path, required=True)
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--draft", action="store_true")
    modes.add_argument("--preview", action="store_true")
    modes.add_argument("--final", action="store_true")
    result.add_argument("--cache-dir", type=Path)
    result.add_argument("--work-dir", type=Path)
    result.add_argument("--no-cache", action="store_true")
    result.add_argument("--no-subtitles", action="store_true")
    result.add_argument("--force", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        render(args)
    except (RenderError, ProvenanceError, OSError, json.JSONDecodeError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
