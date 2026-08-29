#!/usr/bin/env python3
"""Fail QA when exact N-1/N/N+1 cut frames contain an unintended dark flash."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


NEARBLACK_LIMIT = 0.97
MEAN_LIMIT = 8.0


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    frame_count: int
    fps: float


@dataclass(frozen=True)
class FrameStats:
    mean: float
    p99: float
    nearblack8_frac: float

    @property
    def failed(self) -> bool:
        return self.nearblack8_frac > NEARBLACK_LIMIT or self.mean < MEAN_LIMIT


def run_bytes(command: list[str]) -> bytes:
    return subprocess.run(command, check=True, stdout=subprocess.PIPE).stdout


def probe_video(path: Path) -> VideoInfo:
    raw = run_bytes(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=width,height,avg_frame_rate,nb_read_frames",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(raw)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"no video stream: {path}")
    stream = streams[0]
    numerator, denominator = str(stream["avg_frame_rate"]).split("/", 1)
    fps = float(numerator) / float(denominator)
    return VideoInfo(
        width=int(stream["width"]),
        height=int(stream["height"]),
        frame_count=int(stream["nb_read_frames"]),
        fps=fps,
    )


def stats_for_frame(frame: np.ndarray) -> FrameStats:
    return FrameStats(
        mean=float(frame.mean()),
        p99=float(np.percentile(frame, 99)),
        nearblack8_frac=float(np.mean(frame <= 8)),
    )


def decode_selected_frames(
    path: Path, info: VideoInfo, frame_indices: list[int]
) -> list[np.ndarray]:
    if not frame_indices:
        return []
    if frame_indices != sorted(set(frame_indices)):
        raise ValueError("frame indices must be unique and sorted")
    if frame_indices[0] < 0 or frame_indices[-1] >= info.frame_count:
        raise ValueError(
            f"requested frame outside {path.name}: "
            f"{frame_indices[0]}..{frame_indices[-1]} vs {info.frame_count} frames"
        )

    # Commas inside select expressions must be escaped for FFmpeg's filter parser.
    expression = "+".join(f"eq(n\\,{index})" for index in frame_indices)
    raw = run_bytes(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select={expression},format=gray",
            "-fps_mode",
            "passthrough",
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
    )
    frame_size = info.width * info.height
    expected_size = frame_size * len(frame_indices)
    if len(raw) != expected_size:
        raise RuntimeError(
            f"decoded {len(raw)} bytes from {path}, expected {expected_size} "
            f"for {len(frame_indices)} frame(s)"
        )
    array = np.frombuffer(raw, dtype=np.uint8)
    return list(array.reshape((len(frame_indices), info.height, info.width)))


def discover_segment_clips(clips_dir: Path) -> list[Path]:
    numbered: list[tuple[int, Path]] = []
    for path in clips_dir.glob("seg_*.*"):
        # Accept both seg_0000.mov and content-addressed seg_0000_ab12cd34.mov.
        match = re.fullmatch(r"seg_(\d+)(?:_[A-Za-z0-9-]+)?", path.stem)
        if match:
            numbered.append((int(match.group(1)), path))
    numbered.sort(key=lambda item: item[0])
    if not numbered:
        raise RuntimeError(f"no segment clips found in {clips_dir}")
    actual = [number for number, _ in numbered]
    expected = list(range(len(numbered)))
    if actual != expected:
        raise RuntimeError(f"segment numbering is not contiguous: {actual}")
    return [path for _, path in numbered]


def format_failure(scope: str, index: int, stats: FrameStats) -> str:
    return (
        f"  {scope} {index:02d}: mean={stats.mean:.2f}, "
        f"p99={stats.p99:.1f}, nearblack<=8={stats.nearblack8_frac:.4f}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--edl", required=True, type=Path)
    parser.add_argument("--clips-dir", required=True, type=Path)
    parser.add_argument(
        "--manifest", type=Path,
        help="optional render manifest; when supplied, declared frame counts and paths are exact invariants",
    )
    return parser.parse_args()


def same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve() == right.expanduser().resolve()


def main() -> int:
    args = parse_args()
    required_paths = [args.video, args.edl, args.clips_dir]
    if args.manifest:
        required_paths.append(args.manifest)
    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(path)

    edl = json.loads(args.edl.read_text(encoding="utf-8"))
    ranges = edl.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise RuntimeError("EDL must contain a non-empty ranges array")

    clips = discover_segment_clips(args.clips_dir)
    if len(clips) != len(ranges):
        raise RuntimeError(
            f"EDL has {len(ranges)} ranges but clips directory has {len(clips)} clips"
        )

    manifest_records: list[dict] | None = None
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("render manifest root must be an object")
        for field, actual in (
            ("edl", args.edl), ("segments_dir", args.clips_dir),
        ):
            declared = manifest.get(field)
            if not isinstance(declared, str) or not same_path(Path(declared), actual):
                raise RuntimeError(f"manifest {field} does not match the QA input")
        output_record = manifest.get("output") or {}
        declared_video = output_record.get("path") if isinstance(output_record, dict) else None
        if not isinstance(declared_video, str) or not same_path(Path(declared_video), args.video):
            raise RuntimeError("manifest output.path does not match the QA input")
        raw_records = manifest.get("segments")
        if not isinstance(raw_records, list) or len(raw_records) != len(clips):
            raise RuntimeError("manifest segment count does not match clips directory")
        if not all(isinstance(item, dict) for item in raw_records):
            raise RuntimeError("every manifest segment record must be an object")
        manifest_records = raw_records
        for index, (clip, record) in enumerate(zip(clips, manifest_records)):
            if record.get("index") != index:
                raise RuntimeError(f"manifest segment index mismatch at {index}")
            declared_path = record.get("path")
            if not isinstance(declared_path, str) or not same_path(Path(declared_path), clip):
                raise RuntimeError(f"manifest segment {index} path does not match clips directory")

    clip_infos: list[VideoInfo] = []
    clip_stats: list[FrameStats] = []
    for clip in clips:
        info = probe_video(clip)
        clip_infos.append(info)
        clip_stats.append(stats_for_frame(decode_selected_frames(clip, info, [0])[0]))

    if manifest_records is not None:
        for index, (info, record) in enumerate(zip(clip_infos, manifest_records)):
            try:
                declared_frames = int(record["frames"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"manifest segment {index} has invalid frames") from exc
            if declared_frames <= 0 or info.frame_count != declared_frames:
                raise RuntimeError(
                    f"manifest segment {index} declares {declared_frames} frames, "
                    f"clip contains {info.frame_count}"
                )

    video_info = probe_video(args.video)
    output = edl.get("output") or {}
    raw_expected_fps = output.get("fps", edl.get("output_fps", video_info.fps))
    if isinstance(raw_expected_fps, str) and "/" in raw_expected_fps:
        numerator, denominator = raw_expected_fps.split("/", 1)
        expected_fps = float(numerator) / float(denominator)
    else:
        expected_fps = float(raw_expected_fps)
    if abs(video_info.fps - expected_fps) > 1e-6:
        raise RuntimeError(
            f"video fps {video_info.fps:g} differs from EDL fps {expected_fps:g}"
        )
    if any(abs(info.fps - video_info.fps) > 1e-6 for info in clip_infos):
        raise RuntimeError("segment clip fps does not match output video fps")
    expected_output_frames = sum(info.frame_count for info in clip_infos)
    if video_info.frame_count != expected_output_frames:
        raise RuntimeError(
            f"output contains {video_info.frame_count} frames, ordered clips contain "
            f"{expected_output_frames}"
        )

    # If segment i has F frames, cumulative F is the exact index of the first
    # output frame from segment i+1. These are the 37 post-cut frames for 38 clips.
    boundary_indices: list[int] = []
    cursor = 0
    for info in clip_infos[:-1]:
        cursor += info.frame_count
        boundary_indices.append(cursor)
    # Inspect the exact last frame before, first frame after, and the following
    # frame. Decode them in one pass so long programmes are not scanned once
    # per cut.
    triplet_indices = sorted(
        {
            frame_index
            for boundary in boundary_indices
            for frame_index in (boundary - 1, boundary, boundary + 1)
            if 0 <= frame_index < video_info.frame_count
        }
    )
    triplet_frames = decode_selected_frames(args.video, video_info, triplet_indices)
    triplet_stats_by_index = {
        frame_index: stats_for_frame(frame)
        for frame_index, frame in zip(triplet_indices, triplet_frames)
    }
    boundary_stats = [triplet_stats_by_index[index] for index in boundary_indices]

    clip_failures = [
        (index, stats) for index, stats in enumerate(clip_stats) if stats.failed
    ]
    boundary_failures = [
        (index + 1, boundary_indices[index], stats)
        for index, stats in enumerate(boundary_stats)
        if stats.failed
    ]
    triplet_failures = [
        (cut_number, frame_index, triplet_stats_by_index[frame_index])
        for cut_number, boundary in enumerate(boundary_indices, start=1)
        for frame_index in (boundary - 1, boundary, boundary + 1)
        if frame_index in triplet_stats_by_index and triplet_stats_by_index[frame_index].failed
    ]

    print(
        f"Exact-boundary QA: {len(clips)} segment starts, "
        f"{len(boundary_indices)} cuts × N-1/N/N+1, {video_info.fps:g} fps"
    )
    print(
        f"Dark segment frame-0: {len(clip_failures)}/{len(clips)}; "
        f"dark post-cut frames: {len(boundary_failures)}/{len(boundary_indices)}"
    )

    if clip_failures or triplet_failures:
        print("FAIL: near-black exact boundary frame(s) detected")
        for index, stats in clip_failures:
            print(format_failure("segment", index, stats))
        for cut_number, frame_index, stats in triplet_failures:
            print(
                format_failure("cut", cut_number, stats)
                + f" (output frame {frame_index})"
            )
        return 1

    darkest_clip = min(stats.mean for stats in clip_stats)
    darkest_boundary = min((stats.mean for stats in triplet_stats_by_index.values()), default=float("nan"))
    print(
        f"PASS: no exact boundary frame exceeded thresholds; "
        f"minimum means clip/output={darkest_clip:.2f}/"
        f"{'n/a' if not triplet_stats_by_index else f'{darkest_boundary:.2f}'}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
