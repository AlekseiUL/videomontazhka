#!/usr/bin/env python3
# Portions derived from video-use, Copyright (c) 2026 Browser Use, MIT License.
# Modifications Copyright 2026 Алексей Ульянов, Apache-2.0.
# See repository NOTICE and third_party/licenses/video-use-MIT.txt.
"""Pack only manifest-declared Scribe transcripts into evidence-ready Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

from transcription_safety import (
    TranscriptionPathError,
    contained_child,
    validate_source_id,
)


class PackError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PackError(f"file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PackError(f"JSON root must be an object: {path}")
    return data


def source_entries(edit_dir: Path) -> list[dict[str, Any]]:
    project = load(edit_dir / "project.json")
    raw_manifest = Path(str(project.get("source_manifest") or "source_manifest.json")).expanduser()
    manifest_path = raw_manifest.resolve() if raw_manifest.is_absolute() else (edit_dir / raw_manifest).resolve()
    manifest = load(manifest_path)
    entries = manifest.get("sources")
    if not isinstance(entries, list) or not entries:
        raise PackError("source manifest has no sources")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise PackError(f"source manifest entry {index} has no id")
        source_id = validate_source_id(
            item.get("id"), label=f"source manifest entry {index} id"
        )
        if source_id in seen:
            raise PackError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        result.append(item)
    return result


def finite_time(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PackError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise PackError(f"{field} must be finite and non-negative")
    return number


def token_text(item: dict[str, Any]) -> str:
    raw = str(item.get("text") or "").strip()
    if item.get("type") == "audio_event" and raw and not raw.startswith("("):
        return f"({raw})"
    return raw


def group_phrases(words: list[Any], silence_threshold: float, label: str) -> list[dict[str, Any]]:
    phrases: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_speaker: str | None = None
    previous_end: float | None = None

    def flush() -> None:
        nonlocal current, current_speaker
        text = " ".join(part for item in current if (part := token_text(item)))
        text = text.replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!")
        if text:
            phrases.append({
                "start": finite_time(current[0].get("start"), f"{label} word start"),
                "end": finite_time(current[-1].get("end", current[-1].get("start")), f"{label} word end"),
                "speaker_id": current_speaker,
                "text": text,
            })
        current = []
        current_speaker = None

    for index, raw in enumerate(words):
        if not isinstance(raw, dict):
            raise PackError(f"{label}.words[{index}] is not an object")
        kind = str(raw.get("type") or "word")
        if kind == "spacing":
            start = finite_time(raw.get("start"), f"{label}.words[{index}].start")
            end = finite_time(raw.get("end"), f"{label}.words[{index}].end")
            if end - start >= silence_threshold:
                flush()
            continue
        if kind not in {"word", "audio_event"}:
            continue
        start = finite_time(raw.get("start"), f"{label}.words[{index}].start")
        end = finite_time(raw.get("end", start), f"{label}.words[{index}].end")
        if end < start:
            raise PackError(f"{label}.words[{index}] ends before it starts")
        speaker = raw.get("speaker_id")
        speaker_text = None if speaker is None else str(speaker)
        if current and current_speaker is not None and speaker_text is not None and speaker_text != current_speaker:
            flush()
        if current and previous_end is not None and start - previous_end >= silence_threshold:
            flush()
        if not current:
            current_speaker = speaker_text
        current.append(raw)
        previous_end = end
    flush()
    return phrases


def clock(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    remainder = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{remainder:06.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Pack only source-manifest transcripts")
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument("--silence-threshold", type=float, default=0.5)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    edit_dir = args.edit_dir.expanduser().resolve()
    if not edit_dir.is_dir():
        raise PackError(f"edit directory not found: {edit_dir}")
    if not math.isfinite(args.silence_threshold) or not 0.1 <= args.silence_threshold <= 5:
        raise PackError("silence threshold must be between 0.1 and 5 seconds")
    output = (args.output or edit_dir / "takes_packed.md").expanduser().resolve()
    if output.exists() and not args.force:
        raise PackError(f"output exists; use --force to replace: {output}")
    transcripts_dir = edit_dir / "transcripts"
    lines = [
        "# Packed source transcripts",
        "",
        f"Manifest-driven phrase view; break threshold {args.silence_threshold:.3f}s.",
        "Timecodes are source-local and suitable for semantic evidence and EDL decisions.",
        "",
    ]
    packed_manifest: list[dict[str, Any]] = []
    total_phrases = 0
    visual_only_count = 0
    for item in source_entries(edit_dir):
        source_id = str(item["id"])
        source_name = Path(str(item.get("path") or "")).name
        if item.get("audio") is None:
            duration = finite_time(item.get("duration_s"), f"source {source_id!r} duration_s")
            if duration <= 0:
                raise PackError(f"source {source_id!r} duration_s must be positive")
            visual_only_count += 1
            packed_manifest.append({
                "source": source_id,
                "source_sha256": item.get("sha256"),
                "visual_only": True,
                "duration_s": duration,
                "phrases": 0,
            })
            lines.append(f"## {source_id} — {source_name}")
            lines.extend([
                "",
                "_Visual-only source: the manifest reports no audio stream. Use source-local "
                "timecodes plus `modality: \"visual\"` and a factual `description`; no transcript "
                "or transcript metadata exists for this source._",
                "",
            ])
            continue
        transcript_path = contained_child(
            transcripts_dir,
            f"{source_id}.json",
            label="packed transcript input",
        )
        transcript = load(transcript_path)
        words = transcript.get("words")
        if not isinstance(words, list):
            raise PackError(f"transcript {transcript_path.name} has no words array")
        phrases = group_phrases(words, args.silence_threshold, transcript_path.name)
        total_phrases += len(phrases)
        packed_manifest.append({
            "source": source_id,
            "source_sha256": item.get("sha256"),
            "transcript": str(transcript_path),
            "transcript_sha256": sha256(transcript_path),
            "phrases": len(phrases),
        })
        lines.append(f"## {source_id} — {source_name}")
        lines.append("")
        if not phrases:
            lines.extend(["_No speech detected._", ""])
            continue
        for phrase in phrases:
            speaker = phrase.get("speaker_id")
            speaker_tag = f" {speaker}" if speaker else ""
            lines.append(
                f"[{clock(float(phrase['start']))}–{clock(float(phrase['end']))}]{speaker_tag} {phrase['text']}"
            )
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".part.md")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temporary.replace(output)
    index_path = edit_dir / "takes_packed_manifest.json"
    index_temp = index_path.with_suffix(".part.json")
    index_temp.write_text(json.dumps({
        "version": 1,
        "output": str(output),
        "output_sha256": sha256(output),
        "silence_threshold_s": args.silence_threshold,
        "sources": packed_manifest,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_temp.replace(index_path)
    print(
        f"packed {len(packed_manifest)} source(s): "
        f"{len(packed_manifest) - visual_only_count} transcript(s), "
        f"{visual_only_count} visual-only, {total_phrases} phrases: {output}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PackError, TranscriptionPathError, OSError, json.JSONDecodeError) as exc:
        print(f"pack_transcripts_safe: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
