#!/usr/bin/env python3
"""Smooth local presenter ROI observations into normalized layout keyframes.

The script intentionally returns rectangles only. It does not decide whether a
presenter should be displayed as a circle, rectangle, full frame, or hidden.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


Box = tuple[float, float, float, float]  # center x, center y, width, height


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply EMA, dead-zone, dropout hold, and max-speed limiting to "
            "normalized top-left presenter tracking JSON."
        )
    )
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument(
        "--ema-alpha",
        type=float,
        default=0.35,
        help="EMA weight for each new observation (default: 0.35)",
    )
    parser.add_argument(
        "--dead-zone",
        type=float,
        default=0.008,
        help="Ignore center/size jitter below this normalized distance (default: 0.008)",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0.75,
        help="Hold the last box through short tracking dropouts (default: 0.75)",
    )
    parser.add_argument(
        "--max-speed",
        type=float,
        default=0.80,
        help="Maximum change per box component per second (default: 0.80)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.20,
        help="Treat observations below this confidence as missing (default: 0.20)",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=0.0,
        help="Optional fractional padding around each measured box (default: 0)",
    )
    return parser.parse_args()


def require_range(name: str, value: float, low: float, high: float) -> None:
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}], got {value}")


def validate_options(args: argparse.Namespace) -> None:
    require_range("--ema-alpha", args.ema_alpha, 0.0, 1.0)
    require_range("--dead-zone", args.dead_zone, 0.0, 1.0)
    require_range("--hold-seconds", args.hold_seconds, 0.0, 60.0)
    require_range("--max-speed", args.max_speed, 0.0, 100.0)
    require_range("--min-confidence", args.min_confidence, 0.0, 1.0)
    require_range("--padding", args.padding, 0.0, 5.0)
    if args.ema_alpha == 0.0:
        raise ValueError("--ema-alpha must be greater than zero")
    if args.max_speed == 0.0:
        raise ValueError("--max-speed must be greater than zero")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_roi(value: Any, padding: float) -> Box | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("roi must be an object or null")

    try:
        x = float(value["x"])
        y = float(value["y"])
        width = float(value["width"])
        height = float(value["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("roi must contain numeric x, y, width, and height") from exc

    values = (x, y, width, height)
    if not all(math.isfinite(item) for item in values):
        raise ValueError("roi values must be finite")
    if width <= 0.0 or height <= 0.0:
        return None
    if x < -1e-6 or y < -1e-6 or x + width > 1.0 + 1e-6 or y + height > 1.0 + 1e-6:
        raise ValueError(f"roi is outside normalized top-left bounds: {value}")

    center_x = x + width / 2.0
    center_y = y + height / 2.0
    padded_width = min(1.0, width * (1.0 + 2.0 * padding))
    padded_height = min(1.0, height * (1.0 + 2.0 * padding))
    center_x = clamp(center_x, padded_width / 2.0, 1.0 - padded_width / 2.0)
    center_y = clamp(center_y, padded_height / 2.0, 1.0 - padded_height / 2.0)
    return center_x, center_y, padded_width, padded_height


def box_to_roi(box: Box) -> dict[str, float]:
    center_x, center_y, width, height = box
    return {
        "x": round(clamp(center_x - width / 2.0, 0.0, 1.0), 8),
        "y": round(clamp(center_y - height / 2.0, 0.0, 1.0), 8),
        "width": round(clamp(width, 0.0, 1.0), 8),
        "height": round(clamp(height, 0.0, 1.0), 8),
    }


def within_dead_zone(current: Box, target: Box, threshold: float) -> bool:
    center_distance = math.hypot(target[0] - current[0], target[1] - current[1])
    size_distance = max(abs(target[2] - current[2]), abs(target[3] - current[3]))
    return center_distance <= threshold and size_distance <= threshold


def smooth_step(current: Box, target: Box, alpha: float, max_delta: float) -> Box:
    candidate = tuple(
        current[index] + alpha * (target[index] - current[index]) for index in range(4)
    )
    limited = tuple(
        current[index]
        + clamp(candidate[index] - current[index], -max_delta, max_delta)
        for index in range(4)
    )
    width = clamp(limited[2], 1e-6, 1.0)
    height = clamp(limited[3], 1e-6, 1.0)
    center_x = clamp(limited[0], width / 2.0, 1.0 - width / 2.0)
    center_y = clamp(limited[1], height / 2.0, 1.0 - height / 2.0)
    return center_x, center_y, width, height


def load_frames(document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("coordinate_space") != "normalized_top_left":
        raise ValueError("input coordinate_space must be 'normalized_top_left'")
    frames = document.get("frames")
    if not isinstance(frames, list):
        raise ValueError("input must contain a frames array")

    normalized: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"frames[{index}] must be an object")
        try:
            time_seconds = float(frame["time_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"frames[{index}].time_seconds must be numeric") from exc
        if not math.isfinite(time_seconds) or time_seconds < 0.0:
            raise ValueError(f"frames[{index}].time_seconds must be finite and non-negative")
        normalized.append({**frame, "time_seconds": time_seconds, "_index": index})

    normalized.sort(key=lambda item: (item["time_seconds"], item["_index"]))
    return normalized


def build_keyframes(document: dict[str, Any], args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, int]]:
    frames = load_frames(document)
    keyframes: list[dict[str, Any]] = []
    state: Box | None = None
    last_output_time: float | None = None
    last_measurement_time: float | None = None
    last_confidence = 0.0
    counts = {"measured": 0, "held": 0, "missing": 0}

    for frame in frames:
        timestamp = frame["time_seconds"]
        try:
            confidence = float(frame.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0

        measurement = parse_roi(frame.get("roi"), args.padding)
        if confidence < args.min_confidence:
            measurement = None

        if measurement is not None:
            if state is None:
                state = measurement
            else:
                target = state if within_dead_zone(state, measurement, args.dead_zone) else measurement
                previous_time = timestamp if last_output_time is None else last_output_time
                delta_time = max(1e-6, timestamp - previous_time)
                state = smooth_step(
                    state,
                    target,
                    args.ema_alpha,
                    args.max_speed * delta_time,
                )
            last_measurement_time = timestamp
            last_confidence = clamp(confidence, 0.0, 1.0)
            output_state = "measured"
            counts["measured"] += 1
        elif (
            state is not None
            and last_measurement_time is not None
            and timestamp - last_measurement_time <= args.hold_seconds + 1e-9
        ):
            output_state = "held"
            counts["held"] += 1
        else:
            state = None
            output_state = "missing"
            last_confidence = 0.0
            counts["missing"] += 1

        keyframes.append(
            {
                "time_seconds": round(timestamp, 6),
                "roi": box_to_roi(state) if state is not None else None,
                "confidence": round(last_confidence, 6),
                "state": output_state,
            }
        )
        last_output_time = timestamp

    return keyframes, counts


def main() -> int:
    args = parse_args()
    try:
        validate_options(args)
        with args.input_json.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict):
            raise ValueError("input JSON root must be an object")

        keyframes, counts = build_keyframes(document, args)
        output = {
            "version": 1,
            "type": "presenter_layout_keyframes",
            "coordinate_space": "normalized_top_left",
            "interpolation": "linear",
            "source": document.get("source"),
            "video": document.get("video", {}),
            "policy": {
                "ema_alpha": args.ema_alpha,
                "dead_zone": args.dead_zone,
                "hold_seconds": args.hold_seconds,
                "max_speed_per_second": args.max_speed,
                "min_confidence": args.min_confidence,
                "padding": args.padding,
                "geometry_decision": "not_set",
            },
            "summary": {
                "keyframes": len(keyframes),
                **counts,
            },
            "keyframes": keyframes,
        }

        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output_json.with_suffix(args.output_json.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(args.output_json)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"smooth_tracking: {exc}") from exc

    print(
        f"Wrote {len(keyframes)} layout keyframes "
        f"({counts['measured']} measured, {counts['held']} held, "
        f"{counts['missing']} missing) to {args.output_json}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
