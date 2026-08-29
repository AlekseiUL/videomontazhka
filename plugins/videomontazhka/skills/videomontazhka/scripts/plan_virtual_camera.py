#!/usr/bin/env python3
"""Compile deliberate, shot-reset virtual-camera keyframes from reviewed targets.

The script plans camera motion; it does not render video or infer emphasis. Each
punch/focus event must already have an editorial reason and explicit target.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from asset_gate import canonical_edit_dir, path_under_edit, require_asset_gate


VERSION = "sprut-virtual-camera-plan-1"


def finite(value: Any, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def validate_target(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise ValueError(f"{label} must contain exactly x and y")
    x, y = finite(value["x"], f"{label}.x"), finite(value["y"], f"{label}.y")
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        raise ValueError(f"{label} must be normalized within 0..1")
    return {"x": x, "y": y}


def build(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("version") != 1 or document.get("type") != "sprut_virtual_camera_brief":
        raise ValueError("input must be sprut_virtual_camera_brief version 1")
    fps = finite(document.get("fps"), "fps")
    if not 20 <= fps <= 60:
        raise ValueError("fps must be within 20..60")
    events = document.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a non-empty array")
    keyframes: list[dict[str, Any]] = []
    previous_end = -1.0
    previous_shot: str | None = None
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"events[{index}] must be an object")
        required = {"id", "shot_id", "start_s", "end_s", "target", "zoom", "reason"}
        if set(event) - (required | {"enter_frames", "exit_frames"}) or not required <= set(event):
            raise ValueError(f"events[{index}] has non-canonical fields")
        shot_id = str(event["shot_id"]).strip()
        if not shot_id:
            raise ValueError("every camera event needs a shot_id")
        start = finite(event["start_s"], f"events[{index}].start_s")
        end = finite(event["end_s"], f"events[{index}].end_s")
        if start < previous_end - 1e-9 or end <= start:
            raise ValueError("camera events must be ordered, non-overlapping, and positive")
        zoom = finite(event["zoom"], f"events[{index}].zoom")
        if not 1.12 <= zoom <= 1.35:
            raise ValueError("a camera event must be a visible, justified punch within 1.12..1.35")
        target = validate_target(event["target"], f"events[{index}].target")
        reason = str(event["reason"]).strip()
        if len(reason) < 8:
            raise ValueError("every camera event needs a concrete editorial reason")
        enter_frames = int(event.get("enter_frames", 10))
        exit_frames = int(event.get("exit_frames", 10))
        if not 8 <= enter_frames <= 18 or not 8 <= exit_frames <= 18:
            raise ValueError("enter/exit frames must be within 8..18")
        frames = max(1, round((end - start) * fps))
        if frames <= enter_frames + exit_frames:
            raise ValueError("camera event is too short for enter + hold + exit")
        if previous_shot == shot_id and start <= previous_end + 1.0 / fps + 1e-9:
            raise ValueError("adjacent camera events may not imply a continuous trajectory inside one shot")
        for rel in range(frames + 1):
            if rel <= enter_frames:
                amount = smoothstep(rel / enter_frames)
            elif rel >= frames - exit_frames:
                amount = smoothstep((frames - rel) / exit_frames)
            else:
                amount = 1.0
            current_zoom = 1.0 + (zoom - 1.0) * amount
            center_x = 0.5 + (target["x"] - 0.5) * amount
            center_y = 0.5 + (target["y"] - 0.5) * amount
            keyframes.append({
                "time_s": round(start + rel / fps, 6),
                "shot_id": shot_id,
                "event_id": str(event["id"]),
                "center_x": round(center_x, 8),
                "center_y": round(center_y, 8),
                "zoom": round(current_zoom, 8),
                "phase": "enter" if rel < enter_frames else ("exit" if rel > frames - exit_frames else "hold"),
            })
        previous_end = end
        previous_shot = shot_id
    return {
        "version": 1,
        "type": "sprut_virtual_camera_plan",
        "generator": VERSION,
        "fps": fps,
        "coordinate_space": "normalized_display_top_left",
        "render_contract": {
            "subpixel_transform_required": True,
            "reset_at_each_shot": True,
            "interpolation": "cubic_smoothstep",
            "continuous_zoompan_across_cuts_forbidden": True,
        },
        "events": events,
        "keyframes": keyframes,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
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


def self_test() -> None:
    output = build({
        "version": 1,
        "type": "sprut_virtual_camera_brief",
        "fps": 30,
        "events": [{
            "id": "punch-1", "shot_id": "shot-1", "start_s": 1.0, "end_s": 3.0,
            "target": {"x": 0.7, "y": 0.4}, "zoom": 1.30,
            "reason": "Emphasize the approved payoff without crossing a cut.",
        }],
    })
    frames = output["keyframes"]
    if frames[0]["zoom"] != 1.0 or frames[-1]["zoom"] != 1.0:
        raise RuntimeError("camera does not return exactly to base")
    if max(frame["zoom"] for frame in frames) != 1.3:
        raise RuntimeError("camera does not reach requested hold zoom")
    print(json.dumps({"status": "PASS", "keyframes": len(frames)}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan shot-reset subpixel camera motion")
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.edit_dir is None or args.input is None or args.output is None:
        raise ValueError("--edit-dir, input, and output are required")
    edit_dir = canonical_edit_dir(args.edit_dir)
    input_path = path_under_edit(edit_dir, args.input, "virtual camera brief")
    output_path = path_under_edit(edit_dir, args.output, "virtual camera plan")
    if not input_path.is_file() or input_path.is_symlink():
        raise ValueError("input must be a regular approval-bound brief under edit/")
    if output_path.exists():
        raise ValueError(f"output exists and was left untouched: {output_path}")
    require_asset_gate(edit_dir)
    document = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("input root must be an object")
    planned = build(document)
    require_asset_gate(edit_dir)
    atomic_json(output_path, planned)
    print(f"wrote virtual camera plan: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        print(f"plan_virtual_camera: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
