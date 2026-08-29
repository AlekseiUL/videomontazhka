#!/usr/bin/env python3
"""Parse real caption assets and enforce timing/readability release limits."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MAX_CPS = 20.0
DEFAULT_MIN_DURATION_S = 0.40
DEFAULT_MAX_LINES = 2
DEFAULT_MAX_CHARS_PER_LINE = 42
DEFAULT_DURATION_TOLERANCE_S = 0.05
TIMING_EPSILON_S = 1e-9
SUPPORTED_SUFFIXES = {".srt", ".vtt", ".ass", ".ssa", ".json"}
HTML_TAG_RE = re.compile(r"<[^>]*>")
ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


class CaptionParseError(RuntimeError):
    """Raised when a caption asset cannot be parsed unambiguously."""


@dataclass(frozen=True)
class CaptionCue:
    start: Any
    end: Any
    text: str
    source_ref: str


def visible_text(value: Any, *, ass: bool = False) -> str:
    """Return visible cue text while preserving intentional line breaks."""
    text = str(value or "")
    if ass:
        text = ASS_OVERRIDE_RE.sub("", text)
        text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    text = HTML_TAG_RE.sub("", text)
    return html.unescape(text)


def parse_clock(value: str, *, allow_minutes_only: bool, label: str) -> float:
    token = value.strip()
    pattern = (
        r"(?:(\d+):)?(\d{2}):(\d{2})(?:[,.](\d{1,3}))?"
        if allow_minutes_only
        else r"(\d+):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?"
    )
    match = re.fullmatch(pattern, token)
    if not match:
        raise CaptionParseError(f"{label}: invalid timestamp {value!r}")
    if allow_minutes_only:
        hours_raw, minutes_raw, seconds_raw, fraction_raw = match.groups()
        hours = int(hours_raw or 0)
    else:
        hours_raw, minutes_raw, seconds_raw, fraction_raw = match.groups()
        hours = int(hours_raw)
    minutes = int(minutes_raw)
    seconds = int(seconds_raw)
    if minutes >= 60 or seconds >= 60:
        raise CaptionParseError(f"{label}: timestamp component out of range {value!r}")
    fraction = int(fraction_raw or 0) / (10 ** len(fraction_raw or "")) if fraction_raw else 0.0
    result = hours * 3600 + minutes * 60 + seconds + fraction
    if not math.isfinite(result):
        raise CaptionParseError(f"{label}: non-finite timestamp {value!r}")
    return result


def parse_timing_line(line: str, *, allow_minutes_only: bool, label: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*(\S+)\s+-->\s+(\S+)(?:\s+.*)?", line)
    if not match:
        raise CaptionParseError(f"{label}: malformed timing line {line!r}")
    return (
        parse_clock(match.group(1), allow_minutes_only=allow_minutes_only, label=f"{label} start"),
        parse_clock(match.group(2), allow_minutes_only=allow_minutes_only, label=f"{label} end"),
    )


def normalized_lines(raw: str) -> list[str]:
    return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_srt(path: Path, raw: str) -> list[CaptionCue]:
    text = raw.lstrip("\ufeff").strip()
    if not text:
        raise CaptionParseError(f"{path.name}: empty SRT file")
    blocks = re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    cues: list[CaptionCue] = []
    for block_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        timing_index = 0
        if lines and re.fullmatch(r"\s*\d+\s*", lines[0]):
            timing_index = 1
        if timing_index >= len(lines):
            raise CaptionParseError(f"{path.name} block {block_index}: missing timing line")
        start, end = parse_timing_line(
            lines[timing_index], allow_minutes_only=False,
            label=f"{path.name} block {block_index}",
        )
        cue_text = visible_text("\n".join(lines[timing_index + 1:]))
        cues.append(CaptionCue(start, end, cue_text, f"block {block_index}"))
    return cues


def parse_vtt(path: Path, raw: str) -> list[CaptionCue]:
    text = raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", lines[0].strip()):
        raise CaptionParseError(f"{path.name}: missing WEBVTT header")
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise CaptionParseError(f"{path.name}: no cues")
    blocks = re.split(r"\n[ \t]*\n", body)
    cues: list[CaptionCue] = []
    for block_index, block in enumerate(blocks, start=1):
        block_lines = block.split("\n")
        first = block_lines[0].lstrip()
        if first == "STYLE" or first == "REGION" or first.startswith("NOTE"):
            continue
        timing_index = 0 if "-->" in block_lines[0] else 1
        if timing_index >= len(block_lines):
            raise CaptionParseError(f"{path.name} block {block_index}: missing timing line")
        start, end = parse_timing_line(
            block_lines[timing_index], allow_minutes_only=True,
            label=f"{path.name} block {block_index}",
        )
        cue_text = visible_text("\n".join(block_lines[timing_index + 1:]))
        cues.append(CaptionCue(start, end, cue_text, f"block {block_index}"))
    if not cues:
        raise CaptionParseError(f"{path.name}: no cues")
    return cues


def parse_ass_clock(value: str, label: str) -> float:
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})(?:[.](\d+))?\s*", value)
    if not match:
        raise CaptionParseError(f"{label}: invalid ASS timestamp {value!r}")
    hours, minutes, seconds = (int(part) for part in match.groups()[:3])
    if minutes >= 60 or seconds >= 60:
        raise CaptionParseError(f"{label}: timestamp component out of range {value!r}")
    fraction_raw = match.group(4)
    fraction = int(fraction_raw) / (10 ** len(fraction_raw)) if fraction_raw else 0.0
    return hours * 3600 + minutes * 60 + seconds + fraction


def parse_ass(path: Path, raw: str) -> list[CaptionCue]:
    in_events = False
    fields: list[str] | None = None
    cues: list[CaptionCue] = []
    for line_number, raw_line in enumerate(normalized_lines(raw.lstrip("\ufeff")), start=1):
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_events = line.casefold() == "[events]"
            continue
        if not in_events:
            continue
        key, separator, value = raw_line.partition(":")
        if not separator:
            raise CaptionParseError(f"{path.name} line {line_number}: malformed Events row")
        row_type = key.strip().casefold()
        if row_type == "format":
            fields = [part.strip().casefold() for part in value.split(",")]
            if len(fields) != len(set(fields)):
                raise CaptionParseError(f"{path.name} line {line_number}: duplicate Format field")
            missing = {"start", "end", "text"} - set(fields)
            if missing:
                raise CaptionParseError(
                    f"{path.name} line {line_number}: Format misses {sorted(missing)}"
                )
            continue
        if row_type != "dialogue":
            continue
        if fields is None:
            raise CaptionParseError(f"{path.name} line {line_number}: Dialogue appears before Format")
        values = value.lstrip().split(",", len(fields) - 1)
        if len(values) != len(fields):
            raise CaptionParseError(
                f"{path.name} line {line_number}: Dialogue has {len(values)} fields; expected {len(fields)}"
            )
        record = dict(zip(fields, values))
        start = parse_ass_clock(record["start"], f"{path.name} line {line_number} start")
        end = parse_ass_clock(record["end"], f"{path.name} line {line_number} end")
        cues.append(CaptionCue(
            start, end, visible_text(record["text"], ass=True), f"line {line_number}"
        ))
    if fields is None:
        raise CaptionParseError(f"{path.name}: missing [Events] Format row")
    if not cues:
        raise CaptionParseError(f"{path.name}: no Dialogue cues")
    return cues


def parse_json_plan(path: Path, raw: str) -> list[CaptionCue]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaptionParseError(f"{path.name}: malformed JSON ({exc.msg})") from exc
    cues = data.get("cues") if isinstance(data, dict) else None
    if not isinstance(cues, list) or not cues:
        raise CaptionParseError(f"{path.name}: legacy JSON needs a non-empty cues array")
    result: list[CaptionCue] = []
    for index, cue in enumerate(cues):
        if not isinstance(cue, dict) or not all(key in cue for key in ("start", "end", "text")):
            raise CaptionParseError(f"{path.name} cue {index}: missing start/end/text")
        result.append(CaptionCue(cue["start"], cue["end"], str(cue["text"]), f"cue {index}"))
    return result


def parse_caption_file(path: Path) -> tuple[str, list[CaptionCue]]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise CaptionParseError(
            f"{path.name}: unsupported caption format {suffix or '<none>'}; "
            f"expected {sorted(SUPPORTED_SUFFIXES)}"
        )
    if not path.is_file():
        raise CaptionParseError(f"caption file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaptionParseError(f"{path.name}: captions must be UTF-8 ({exc})") from exc
    if suffix == ".srt":
        return "srt", parse_srt(path, raw)
    if suffix == ".vtt":
        return "vtt", parse_vtt(path, raw)
    if suffix in {".ass", ".ssa"}:
        return suffix[1:], parse_ass(path, raw)
    return "json", parse_json_plan(path, raw)


def validate_caption_file(
    path: Path,
    *,
    rendered_duration_s: float,
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    max_cps: float = DEFAULT_MAX_CPS,
    min_duration_s: float = DEFAULT_MIN_DURATION_S,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE,
) -> dict[str, Any]:
    """Return structured, JSON-safe evidence for one caption asset."""
    resolved = path.expanduser().resolve()
    report: dict[str, Any] = {
        "version": 1,
        "path": str(resolved),
        "format": None,
        "status": "FAIL",
        "checked_cues": 0,
        "rendered_duration_s": rendered_duration_s,
        "duration_tolerance_s": duration_tolerance_s,
        "limits": {
            "max_cps": max_cps,
            "min_duration_s": min_duration_s,
            "max_lines": max_lines,
            "max_chars_per_line": max_chars_per_line,
        },
        "observed": {},
        "errors": [],
    }
    errors: list[str] = report["errors"]
    numeric_limits = (rendered_duration_s, duration_tolerance_s, max_cps, min_duration_s)
    if not all(math.isfinite(float(value)) for value in numeric_limits):
        errors.append("caption validation limits and rendered duration must be finite")
        return report
    if rendered_duration_s <= 0:
        errors.append("rendered duration must be positive")
        return report
    if duration_tolerance_s < 0 or max_cps <= 0 or min_duration_s <= 0:
        errors.append("caption validation limits are invalid")
        return report
    if max_lines <= 0 or max_chars_per_line <= 0:
        errors.append("caption line limits must be positive integers")
        return report
    try:
        caption_format, cues = parse_caption_file(resolved)
    except (CaptionParseError, OSError) as exc:
        errors.append(str(exc))
        return report
    report["format"] = caption_format
    report["checked_cues"] = len(cues)
    previous_start: float | None = None
    previous_end: float | None = None
    first_start: float | None = None
    last_end: float | None = None
    observed_max_cps = 0.0
    observed_max_lines = 0
    observed_max_chars = 0
    for index, cue in enumerate(cues, start=1):
        label = f"{resolved.name} cue {index} ({cue.source_ref})"
        try:
            start = float(cue.start)
            end = float(cue.end)
        except (TypeError, ValueError):
            errors.append(f"{label}: start/end must be numeric")
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            errors.append(f"{label}: start/end must be finite")
            continue
        if start < 0:
            errors.append(f"{label}: start must be non-negative")
        if end <= start:
            errors.append(f"{label}: duration must be positive")
        if previous_start is not None and start < previous_start - TIMING_EPSILON_S:
            errors.append(f"{label}: cue is out of chronological order")
        if previous_end is not None and start < previous_end - TIMING_EPSILON_S:
            errors.append(f"{label}: overlaps the previous cue ending at {previous_end:.3f}s")
        duration = end - start
        if duration > 0 and duration < min_duration_s - 1e-9:
            errors.append(f"{label}: duration {duration:.3f}s < {min_duration_s:.3f}s")
        if end > rendered_duration_s + duration_tolerance_s + 1e-9:
            errors.append(
                f"{label}: ends at {end:.3f}s after rendered duration "
                f"{rendered_duration_s:.3f}s + {duration_tolerance_s:.3f}s tolerance"
            )
        text = visible_text(cue.text, ass=caption_format in {"ass", "ssa"}).strip()
        lines = [re.sub(r"[ \t]+", " ", part).strip() for part in text.splitlines()]
        if not lines:
            lines = [""]
        if not any(lines):
            errors.append(f"{label}: empty visible text")
        line_count = len(lines)
        longest_line = max((len(part) for part in lines), default=0)
        char_count = sum(len(part) for part in lines)
        cps = char_count / duration if duration > 0 else 0.0
        observed_max_cps = max(observed_max_cps, cps)
        observed_max_lines = max(observed_max_lines, line_count)
        observed_max_chars = max(observed_max_chars, longest_line)
        if line_count > max_lines:
            errors.append(f"{label}: {line_count} lines > {max_lines}")
        if longest_line > max_chars_per_line:
            errors.append(
                f"{label}: longest line has {longest_line} chars > {max_chars_per_line}"
            )
        if duration > 0 and cps > max_cps + 1e-9:
            errors.append(f"{label}: {cps:.1f} chars/s > {max_cps:g}")
        previous_start = start
        previous_end = end
        first_start = start if first_start is None else min(first_start, start)
        last_end = end if last_end is None else max(last_end, end)
    if not cues:
        errors.append(f"{resolved.name}: zero caption cues were checked")
    report["observed"] = {
        "first_start_s": first_start,
        "last_end_s": last_end,
        "max_cps": round(observed_max_cps, 3) if math.isfinite(observed_max_cps) else None,
        "max_lines": observed_max_lines,
        "max_chars_per_line": observed_max_chars,
    }
    report["status"] = "PASS" if not errors else "FAIL"
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"caption validation: {report.get('status')} | "
        f"file={Path(str(report.get('path') or '')).name} "
        f"format={report.get('format')} cues={report.get('checked_cues', 0)}"
    ]
    observed = report.get("observed") or {}
    if observed:
        lines.append(
            "observed: "
            f"last_end={observed.get('last_end_s')} "
            f"max_cps={observed.get('max_cps')} "
            f"max_lines={observed.get('max_lines')} "
            f"max_chars_per_line={observed.get('max_chars_per_line')}"
        )
    lines.extend(f"ERROR: {error}" for error in report.get("errors") or [])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate real caption files against rendered duration")
    parser.add_argument("captions", nargs="+", type=Path, help="SRT, VTT, ASS/SSA, or legacy JSON")
    parser.add_argument("--rendered-duration", type=float, required=True)
    parser.add_argument("--duration-tolerance", type=float, default=DEFAULT_DURATION_TOLERANCE_S)
    parser.add_argument("--max-cps", type=float, default=DEFAULT_MAX_CPS)
    parser.add_argument("--min-duration", type=float, default=DEFAULT_MIN_DURATION_S)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--max-chars-per-line", type=int, default=DEFAULT_MAX_CHARS_PER_LINE)
    parser.add_argument("--json", action="store_true", help="emit structured JSON evidence")
    args = parser.parse_args()
    reports = [
        validate_caption_file(
            path,
            rendered_duration_s=args.rendered_duration,
            duration_tolerance_s=args.duration_tolerance,
            max_cps=args.max_cps,
            min_duration_s=args.min_duration,
            max_lines=args.max_lines,
            max_chars_per_line=args.max_chars_per_line,
        )
        for path in args.captions
    ]
    status = "PASS" if reports and all(item["status"] == "PASS" for item in reports) else "FAIL"
    if args.json:
        print(json.dumps({"version": 1, "status": status, "files": reports}, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print(format_report(report), end="")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
