#!/usr/bin/env python3
"""Initialize a non-destructive SPRUT video project and hash source media."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any


MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts"}


class InitError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_rate(value: Any) -> float | None:
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def display_rotation(video: dict[str, Any]) -> int:
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
        raise InitError(f"invalid source display rotation: {raw!r}") from exc
    if not math.isfinite(angle):
        raise InitError(f"invalid source display rotation: {raw!r}")
    normalized = angle % 360.0
    nearest = round(normalized / 90.0) * 90
    if abs(normalized - nearest) > 0.1 and abs(normalized - (nearest % 360)) > 0.1:
        raise InitError(
            f"unsupported non-right-angle source display rotation {angle:g} degrees; "
            "normalize the source before initializing the project"
        )
    return int(nearest) % 360


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise InitError(f"ffprobe failed for {path}: {result.stderr.strip()[-1000:]}")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video is None:
        raise InitError(f"no video stream: {path}")
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    coded_width = int(video.get("width") or 0)
    coded_height = int(video.get("height") or 0)
    rotation = display_rotation(video)
    shown_width, shown_height = coded_width, coded_height
    if rotation in {90, 270}:
        shown_width, shown_height = coded_height, coded_width
    return {
        "duration_s": round(duration, 6),
        "video": {
            "codec": video.get("codec_name"),
            "width": coded_width,
            "height": coded_height,
            "coded_width": coded_width,
            "coded_height": coded_height,
            "display_width": shown_width,
            "display_height": shown_height,
            "display_rotation_degrees": rotation,
            "fps": parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")),
            "pixel_format": video.get("pix_fmt"),
            "color_transfer": video.get("color_transfer"),
        },
        "audio": None if audio is None else {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio.get("sample_rate") or 0),
            "channels": int(audio.get("channels") or 0),
        },
    }


def source_id(path: Path, used: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", path.stem).strip("_").lower() or "source"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def detect_mode(items: list[dict[str, Any]]) -> str:
    durations = sorted(float(item["duration_s"]) for item in items)
    if len(items) == 1 and durations[0] >= 15 * 60:
        return "long_stream"
    if len(items) == 1:
        return "multi_take"
    median = durations[len(durations) // 2]
    if len(items) >= 2 and median <= 5 * 60 and max(durations) <= 15 * 60:
        return "multi_take"
    return "mixed"


def edit_not_in_path(path: Path, root: Path) -> bool:
    """Keep recursive ingest out of the generated edit workspace."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return not relative.parts or relative.parts[0] != "edit"


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize an edit/ workspace without changing sources")
    parser.add_argument("videos_dir", type=Path)
    parser.add_argument("--name", help="project name; defaults to folder name")
    parser.add_argument("--source-mode", choices=("auto", "long_stream", "multi_take", "mixed"), default="auto")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="include supported video files from nested folders (edit/ is always excluded)",
    )
    args = parser.parse_args()

    root = args.videos_dir.expanduser().resolve()
    if not root.is_dir():
        raise InitError(f"videos directory does not exist: {root}")
    if root == Path("/") or root == Path.home():
        raise InitError("refusing to initialize a broad system/home directory")
    if shutil.which("ffprobe") is None:
        raise InitError("ffprobe is required")

    candidates = root.rglob("*") if args.recursive else root.iterdir()
    media = sorted(
        path for path in candidates
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and (not args.recursive or edit_not_in_path(path, root))
    )
    if not media:
        scope = "recursively" if args.recursive else "directly"
        raise InitError(f"no supported video files found {scope} in {root}")

    edit = root / "edit"
    for relative in ("transcripts", "animations", "work", "cache", "verify"):
        (edit / relative).mkdir(parents=True, exist_ok=True)

    skill_root = Path(__file__).resolve().parent.parent
    template_path = skill_root / "assets" / "default-project.json"
    project_path = edit / "project.json"
    manifest_path = edit / "source_manifest.json"
    if project_path.exists() or manifest_path.exists():
        raise InitError("project already initialized; existing project.json/source_manifest.json left untouched")

    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    for index, path in enumerate(media, start=1):
        print(f"[{index}/{len(media)}] probing and hashing {path.name}", flush=True)
        stat_info = path.stat()
        info = probe(path)
        entries.append({
            "id": source_id(path, used),
            "path": str(path.relative_to(root)),
            "sha256": file_sha256(path),
            "size_bytes": stat_info.st_size,
            "mtime_ns": stat_info.st_mtime_ns,
            "role": "unclassified",
            **info,
        })

    project = json.loads(template_path.read_text(encoding="utf-8"))
    project["name"] = args.name or root.name
    project["source_mode"] = detect_mode(entries) if args.source_mode == "auto" else args.source_mode
    project["created_at"] = datetime.now(timezone.utc).isoformat()
    project["source_manifest"] = "source_manifest.json"
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": "..",
        "sources": entries,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"initialized: {edit}")
    print(f"source mode: {project['source_mode']} | sources: {len(entries)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InitError, OSError, json.JSONDecodeError) as exc:
        print(f"init_project: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
