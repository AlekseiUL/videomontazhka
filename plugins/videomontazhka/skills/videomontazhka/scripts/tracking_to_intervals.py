#!/usr/bin/env python3
"""Convert smoothed presenter tracking into reviewable static ROI intervals.

The canonical renderer deliberately uses one fixed presenter ROI per retained
range. This adapter groups locally tracked keyframes into stable intervals; an
editor must review the suggestions and split EDL ranges at accepted boundaries.
It never decides presenter shape or composition.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group presenter tracking keyframes into fixed ROI intervals")
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--source-id", required=True, help="exact EDL/source-manifest source id")
    parser.add_argument("--max-center-drift", type=float, default=0.035)
    parser.add_argument("--max-size-drift", type=float, default=0.050)
    parser.add_argument("--max-interval-seconds", type=float, default=12.0)
    parser.add_argument("--min-review-seconds", type=float, default=0.35)
    parser.add_argument("--end-seconds", type=float, help="explicit end for the last sampled keyframe")
    return parser.parse_args()


def finite(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite number")
    return result


def parse_roi(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("roi must be an object or null")
    try:
        x, y, width, height = (finite(value[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("roi needs finite x, y, width, height") from exc
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
        raise ValueError("roi lies outside normalized top-left bounds")
    return x, y, width, height


def center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def compatible(
    boxes: list[tuple[float, float, float, float]],
    candidate: tuple[float, float, float, float],
    max_center_drift: float,
    max_size_drift: float,
) -> bool:
    reference = boxes[0]
    rx, ry = center(reference)
    cx, cy = center(candidate)
    return (
        math.hypot(cx - rx, cy - ry) <= max_center_drift
        and abs(candidate[2] - reference[2]) <= max_size_drift
        and abs(candidate[3] - reference[3]) <= max_size_drift
    )


def union_roi(boxes: list[tuple[float, float, float, float]]) -> dict[str, float]:
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return {
        "x": round(left, 8),
        "y": round(top, 8),
        "width": round(right - left, 8),
        "height": round(bottom - top, 8),
    }


def main() -> int:
    args = parse_args()
    for name in ("max_center_drift", "max_size_drift", "max_interval_seconds", "min_review_seconds"):
        value = finite(getattr(args, name))
        if value <= 0:
            raise SystemExit(f"tracking_to_intervals: --{name.replace('_', '-')} must be positive")

    try:
        document = json.loads(args.input_json.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("coordinate_space") != "normalized_top_left":
            raise ValueError("input must use normalized_top_left coordinates")
        raw_frames = document.get("keyframes")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise ValueError("input has no keyframes")
        source_id = args.source_id
        if not source_id.strip() or any(char.isspace() for char in (source_id[0], source_id[-1])):
            raise ValueError("--source-id must be a non-empty, trimmed source-manifest id")

        frames: list[tuple[float, tuple[float, float, float, float] | None, float, str]] = []
        for index, raw in enumerate(raw_frames):
            if not isinstance(raw, dict):
                raise ValueError(f"keyframes[{index}] is not an object")
            timestamp = finite(raw.get("time_seconds"))
            if timestamp < 0:
                raise ValueError(f"keyframes[{index}] has a negative timestamp")
            confidence = max(0.0, min(1.0, finite(raw.get("confidence", 0))))
            frames.append((timestamp, parse_roi(raw.get("roi")), confidence, str(raw.get("state") or "unknown")))
        frames.sort(key=lambda item: item[0])
        if any(frames[index][0] <= frames[index - 1][0] for index in range(1, len(frames))):
            raise ValueError("keyframe timestamps must be unique and increasing")

        deltas = [frames[index][0] - frames[index - 1][0] for index in range(1, len(frames))]
        sample_step = statistics.median(deltas) if deltas else 1 / 30
        video_info = document.get("video") if isinstance(document.get("video"), dict) else {}
        detected_end = video_info.get("duration_s")
        last_end = args.end_seconds if args.end_seconds is not None else detected_end
        if last_end is None:
            last_end = frames[-1][0] + sample_step
        last_end = finite(last_end)
        if last_end <= frames[-1][0]:
            raise ValueError("last interval end must be after the final keyframe")

        intervals: list[dict[str, Any]] = []
        active_start: float | None = None
        active_boxes: list[tuple[float, float, float, float]] = []
        active_confidences: list[float] = []
        active_states: set[str] = set()

        def finish(end: float) -> None:
            nonlocal active_start, active_boxes, active_confidences, active_states
            if active_start is None or not active_boxes or end <= active_start:
                active_start, active_boxes, active_confidences, active_states = None, [], [], set()
                return
            duration = end - active_start
            intervals.append({
                "source": source_id.strip(),
                "start": round(active_start, 6),
                "end": round(end, 6),
                "presenter_roi": union_roi(active_boxes),
                "confidence_min": round(min(active_confidences), 6),
                "confidence_mean": round(statistics.fmean(active_confidences), 6),
                "tracking_states": sorted(active_states),
                "review_required": duration < args.min_review_seconds,
                "reason": "Local presenter tracking grouped into a fixed source-space ROI; review before copying to EDL.",
            })
            active_start, active_boxes, active_confidences, active_states = None, [], [], set()

        for index, (timestamp, box, confidence, state) in enumerate(frames):
            next_timestamp = frames[index + 1][0] if index + 1 < len(frames) else last_end
            if box is None:
                finish(timestamp)
                continue
            if active_start is None:
                active_start = timestamp
            elif (
                timestamp - active_start >= args.max_interval_seconds
                or not compatible(active_boxes, box, args.max_center_drift, args.max_size_drift)
            ):
                finish(timestamp)
                active_start = timestamp
            active_boxes.append(box)
            active_confidences.append(confidence)
            active_states.add(state)
            if index + 1 == len(frames):
                finish(next_timestamp)

        output = {
            "version": 1,
            "type": "presenter_layout_intervals",
            "coordinate_space": "normalized_top_left",
            "source": source_id.strip(),
            "editorial_contract": {
                "shape_decision": "not_set",
                "composition_decision": "not_set",
                "usage": "Review, then split retained EDL ranges at accepted boundaries and copy presenter_roi.",
            },
            "policy": {
                "max_center_drift": args.max_center_drift,
                "max_size_drift": args.max_size_drift,
                "max_interval_seconds": args.max_interval_seconds,
                "min_review_seconds": args.min_review_seconds,
            },
            "intervals": intervals,
        }
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output_json.with_suffix(args.output_json.suffix + ".tmp")
        temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.output_json)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit(f"tracking_to_intervals: {exc}") from exc

    print(f"Wrote {len(intervals)} reviewable presenter ROI intervals to {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
