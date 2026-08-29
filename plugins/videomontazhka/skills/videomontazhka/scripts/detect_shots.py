#!/usr/bin/env python3
"""Detect source-local shot boundaries for the SPRUT virtual camera.

Analysis is read-only with respect to media. The JSON report must live below the
project's canonical edit directory. This command performs no semantic edit and
therefore may run before approval, like other source measurement tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from asset_gate import canonical_edit_dir, path_under_edit


VERSION = "sprut-shot-detect-1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(path)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def detect(input_path: Path, threshold: float, min_scene_len: int) -> list[dict[str, Any]]:
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError as exc:
        raise RuntimeError("PySceneDetect is not installed in the configured SPRUT Python runtime") from exc
    video = open_video(str(input_path))
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_scene_len))
    manager.detect_scenes(video=video, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)
    if not scenes:
        raise RuntimeError("shot detection returned no scenes")
    return [
        {
            "id": f"shot-{index + 1:04d}",
            "start_s": round(start.get_seconds(), 6),
            "end_s": round(end.get_seconds(), 6),
            "start_frame": start.get_frames(),
            "end_frame_exclusive": end.get_frames(),
            "duration_s": round(end.get_seconds() - start.get_seconds(), 6),
            "camera_state_reset_required": True,
        }
        for index, (start, end) in enumerate(scenes)
    ]


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="sprut-shot-selftest-") as temporary:
        root = Path(temporary)
        source = root / "test.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:r=10:d=1",
            "-f", "lavfi", "-i", "color=c=white:s=320x240:r=10:d=1",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=1",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        scenes = detect(source, threshold=12.0, min_scene_len=2)
        if len(scenes) != 3 or any(not item["camera_state_reset_required"] for item in scenes):
            raise RuntimeError(f"unexpected synthetic shot result: {scenes}")
        print(json.dumps({"status": "PASS", "shots": len(scenes)}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect shot boundaries for shot-aware camera planning")
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--threshold", type=float, default=27.0)
    parser.add_argument("--min-scene-frames", type=int, default=12)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.edit_dir is None or args.input is None or args.output is None:
        raise ValueError("--edit-dir, --input, and --output are required")
    if not math.isfinite(args.threshold) or not 1 <= args.threshold <= 100:
        raise ValueError("--threshold must be within 1..100")
    if not 1 <= args.min_scene_frames <= 10000:
        raise ValueError("--min-scene-frames must be within 1..10000")
    edit_dir = canonical_edit_dir(args.edit_dir)
    output = path_under_edit(edit_dir, args.output, "shot detection output")
    source = args.input.expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("--input must be a regular local media file")
    if output.exists():
        raise ValueError(f"output exists and was left untouched: {output}")
    scenes = detect(source, args.threshold, args.min_scene_frames)
    report = {
        "version": 1,
        "type": "sprut_shot_boundaries",
        "generator": VERSION,
        "source": str(source),
        "source_sha256": sha256(source),
        "policy": {
            "algorithm": "PySceneDetect ContentDetector",
            "threshold": args.threshold,
            "min_scene_frames": args.min_scene_frames,
            "editorial_use": "reset camera/tracking state; never cut solely because a detector fired",
        },
        "shots": scenes,
    }
    atomic_json(output, report)
    print(f"wrote {len(scenes)} shot intervals: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"detect_shots: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
