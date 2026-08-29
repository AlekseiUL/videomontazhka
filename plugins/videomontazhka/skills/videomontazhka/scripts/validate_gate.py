#!/usr/bin/env python3
"""Validate semantic approval, asset, EDL, render, and final gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from fractions import Fraction
from pathlib import Path
from typing import Any

from artifact_provenance import (
    RENDERER_VERSION,
    ProvenanceError,
    artifact_key,
    preview_approval_name,
    render_manifest_name,
    renderer_identity,
    resolve_subtitle_fonts,
)
from schema_check import SchemaDefinitionError, Validator
from validate_caption_fit import CaptionParseError, parse_caption_file
from visual_asset_provenance import (
    VisualProvenanceError,
    verify_visual_asset_provenance,
)


REQUIRED_SCOPES = {"semantic_structure", "editing_strategy", "visual_strategy"}
ALLOWED_TRANSITIONS = {"hard_cut", "j_cut", "l_cut", "dissolve", "chapter_bridge", "match_cut"}
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
RESOLVED_PRESENTER = {"rectangle", "circle", "full_frame", "hidden", "none"}
LAYOUT_COMPOSITIONS = {"preserve_source", "presenter_with_screen", "screen_only", "presenter_only"}
COMPOSITION_OUTPUT_SHAPES = {
    "presenter_only": {"rectangle", "circle", "full_frame"},
    "screen_only": {"hidden", "none"},
    "presenter_with_screen": {"rectangle", "circle"},
}
PRESERVE_SOURCE_SHAPES = {
    "already_circular": {"circle"},
    "isolated_subject": {"rectangle"},
    "rectangular_with_context": {"rectangle"},
    "full_frame_presenter": {"full_frame"},
    "screen_only": {"hidden", "none"},
    "unknown": set(),
}
EXPECTED_RENDERER = RENDERER_VERSION
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
MAX_EVIDENCE_DURATION_S = 180.0
EVIDENCE_BOUNDARY_PADDING_S = 0.25
EXPECTED_TRANSCRIPT_MODEL = "scribe_v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DEFAULT_MAX_BOUNDARY_SILENCE_S = 0.35
DEFAULT_MAX_INTERNAL_SILENCE_S = 0.75
SEMANTIC_TIMING_TOLERANCE_S = 0.050
TranscriptEntry = tuple[str, str, float, float]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path, errors: list[str]) -> dict[str, Any]:
    if not path.is_file():
        errors.append(f"missing {path.name}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {path.name}: {exc}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path.name} root must be an object")
        return {}
    return data


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 2


def fps_fraction(value: Any) -> Fraction | None:
    if isinstance(value, bool):
        return None
    try:
        fps = Fraction(value) if isinstance(value, int) else Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return fps if fps > 0 else None


def normalize_quote(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def normalized_quote_contains(approved: Any, retained: Any) -> bool:
    needle = normalize_quote(approved)
    haystack = normalize_quote(retained)
    return bool(needle and f" {needle} " in f" {haystack} ")


def valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def finite_non_negative_time(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def build_evidence_map(plan: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    evidence_map: dict[str, tuple[str, dict[str, Any]]] = {}
    for meaning in plan.get("source_truth") or []:
        if not isinstance(meaning, dict) or not isinstance(meaning.get("id"), str):
            continue
        for evidence in meaning.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            evidence_id = evidence.get("id")
            if isinstance(evidence_id, str) and evidence_id.strip() and evidence_id not in evidence_map:
                evidence_map[evidence_id] = (meaning["id"], evidence)
    return evidence_map


def validate_schema(instance: dict[str, Any], schema_name: str, label: str, errors: list[str]) -> None:
    try:
        schema = json.loads((ASSET_DIR / schema_name).read_text(encoding="utf-8"))
        findings = Validator(schema).validate(instance)
    except (OSError, json.JSONDecodeError, SchemaDefinitionError) as exc:
        errors.append(f"cannot validate {label} schema: {exc}")
        return
    for finding in findings[:30]:
        errors.append(f"{label} schema: {finding.render()}")
    if len(findings) > 30:
        errors.append(f"{label} schema: {len(findings) - 30} additional error(s) omitted")


def validate_source_mode(project: dict[str, Any], plan: dict[str, Any], errors: list[str]) -> None:
    project_source_mode = project.get("source_mode")
    plan_source_mode = plan.get("source_mode")
    if project_source_mode != plan_source_mode:
        errors.append(
            f"project.source_mode={project_source_mode!r} must exactly match approved "
            f"semantic_plan.source_mode={plan_source_mode!r}; resolve auto before EDL"
        )


def validate_plan(
    plan: dict[str, Any], errors: list[str], source_records: dict[str, dict[str, Any]]
) -> tuple[set[str], dict[str, set[str]], dict[str, dict[str, Any]]]:
    valid_sources = set(source_records)
    for field in ("viewer_promise", "audience"):
        if not nonempty(plan.get(field)):
            errors.append(f"semantic_plan.{field} is empty")
    truth = plan.get("source_truth")
    if not isinstance(truth, list) or not truth:
        errors.append("semantic_plan.source_truth must be non-empty")
        truth = []
    meaning_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for index, item in enumerate(truth):
        if not isinstance(item, dict) or not nonempty(item.get("id")) or not nonempty(item.get("meaning")):
            errors.append(f"source_truth[{index}] needs id and meaning")
            continue
        if item["id"] in meaning_ids:
            errors.append(f"source_truth[{index}] duplicates meaning id {item['id']!r}")
        meaning_ids.add(item["id"])
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"source_truth[{index}] has no evidence")
            continue
        for number, proof in enumerate(evidence):
            if not isinstance(proof, dict) or not all(
                key in proof for key in ("id", "source", "start", "end", "modality")
            ):
                errors.append(f"source_truth[{index}].evidence[{number}] is incomplete")
                continue
            label = f"source_truth[{index}].evidence[{number}]"
            evidence_id = proof.get("id")
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                errors.append(f"{label} needs a stable id")
            elif evidence_id in evidence_ids:
                errors.append(f"{label} duplicates evidence id {evidence_id!r}")
            else:
                evidence_ids.add(evidence_id)
            source_id = proof.get("source")
            source_record = source_records.get(source_id) if isinstance(source_id, str) else None
            if source_id not in valid_sources:
                errors.append(f"{label} references unknown source")
            modality = proof.get("modality")
            if modality not in {"speech", "visual"}:
                errors.append(f"{label}.modality must be 'speech' or 'visual'")
            elif modality == "speech" and source_record is not None and source_record.get("audio") is None:
                errors.append(f"{label} uses speech evidence for no-audio source {source_id!r}")
            bounds: tuple[float, float] | None = None
            try:
                start, end = float(proof["start"]), float(proof["end"])
                if not math.isfinite(start + end) or start < 0 or end <= start:
                    raise ValueError
                bounds = (start, end)
                if end - start > MAX_EVIDENCE_DURATION_S:
                    errors.append(
                        f"{label} spans {end - start:.3f}s; "
                        f"maximum is {MAX_EVIDENCE_DURATION_S:.0f}s"
                    )
            except (TypeError, ValueError):
                errors.append(f"{label} has invalid bounds")
            if bounds is not None and source_record is not None:
                duration = finite_non_negative_time(source_record.get("duration_s"))
                if duration is not None and bounds[1] > duration + 1e-6:
                    errors.append(
                        f"{label} ends at {bounds[1]:.3f}s beyond source duration {duration:.3f}s"
                    )
            content_field = "quote" if modality == "speech" else "description"
            content = proof.get(content_field)
            if not isinstance(content, str) or not content.strip():
                errors.append(f"{label} has no {content_field}")
            elif not normalize_quote(content):
                errors.append(f"{label} {content_field} has no searchable words")
    narrative = plan.get("narrative")
    if not isinstance(narrative, list) or not narrative:
        errors.append("semantic_plan.narrative must be non-empty")
        narrative = []
    narrative_sections: dict[str, set[str]] = {}
    for index, section in enumerate(narrative):
        if not isinstance(section, dict) or not nonempty(section.get("id")):
            errors.append(f"narrative[{index}] needs id")
            continue
        section_id = section["id"]
        if section_id in narrative_sections:
            errors.append(f"narrative[{index}] duplicates section id {section['id']!r}")
        refs = set(section.get("meaning_ids") or [])
        narrative_sections.setdefault(section_id, set()).update(refs)
        if not refs:
            errors.append(f"narrative[{index}] needs meaning_ids")
        unknown = refs - meaning_ids
        if unknown:
            errors.append(f"narrative[{index}] references unknown meanings: {sorted(unknown)}")
        if not nonempty(section.get("payoff")):
            errors.append(f"narrative[{index}] has no payoff")
    visual_plan = plan.get("visual_plan")
    if visual_plan is None:
        visual_plan = []
    if not isinstance(visual_plan, list):
        errors.append("semantic_plan.visual_plan must be an array")
        visual_plan = []
    visual_ids: set[str] = set()
    for index, visual in enumerate(visual_plan):
        label = f"visual_plan[{index}]"
        if not isinstance(visual, dict):
            errors.append(f"{label} must be an object")
            continue
        visual_id = visual.get("id")
        if not isinstance(visual_id, str) or not visual_id.strip():
            errors.append(f"{label} needs a stable id")
        elif visual_id in visual_ids:
            errors.append(f"{label} duplicates visual id {visual_id!r}")
        else:
            visual_ids.add(visual_id)
        section_id = visual.get("section_id")
        if section_id not in narrative_sections:
            errors.append(f"{label}.section_id references an unknown narrative section")
        refs = set(visual.get("meaning_ids") or [])
        if not refs:
            errors.append(f"{label} needs meaning_ids")
        unknown = refs - meaning_ids
        if unknown:
            errors.append(f"{label} references unknown meanings: {sorted(unknown)}")
        if section_id in narrative_sections:
            misplaced = refs - narrative_sections[section_id]
            if misplaced:
                errors.append(
                    f"{label} meaning_ids are outside section {section_id!r}: {sorted(misplaced)}"
                )
        if not nonempty(visual.get("purpose")):
            errors.append(f"{label}.purpose is empty")
        approved_text = visual.get("approved_text")
        if approved_text is not None and not normalize_quote(approved_text):
            errors.append(
                f"{label}.approved_text must contain visible words or be null for a no-text visual"
            )
    hooks = plan.get("hooks")
    if not isinstance(hooks, list) or len(hooks) < 2:
        errors.append("semantic_plan needs at least two hook options")
        hooks = []
    hook_ids: set[str] = set()
    hook_meanings: dict[str, set[str]] = {}
    for index, hook in enumerate(hooks):
        if not isinstance(hook, dict) or not nonempty(hook.get("id")):
            continue
        if hook["id"] in hook_ids:
            errors.append(f"hooks[{index}] duplicates hook id {hook['id']!r}")
        hook_ids.add(hook["id"])
        refs = set(hook.get("meaning_ids") or [])
        hook_meanings[hook["id"]] = refs
        if not refs:
            errors.append(f"hooks[{index}] needs meaning_ids")
        unknown = refs - meaning_ids
        if unknown:
            errors.append(f"hooks[{index}] references unknown meanings: {sorted(unknown)}")
    recommended = plan.get("recommended_hook_id")
    if not nonempty(recommended):
        errors.append("semantic_plan.recommended_hook_id is required")
    elif recommended not in hook_ids:
        errors.append("semantic_plan.recommended_hook_id references an unknown hook")
    elif narrative_sections:
        first_section_id = next(iter(narrative_sections))
        outside_opening = hook_meanings.get(recommended, set()) - narrative_sections[first_section_id]
        if outside_opening:
            errors.append(
                f"recommended hook {recommended!r} uses meaning_ids outside first narrative section "
                f"{first_section_id!r}: {sorted(outside_opening)}"
            )
    ending = plan.get("ending")
    if not isinstance(ending, dict) or not ending:
        errors.append("semantic_plan.ending is empty")
    else:
        ending_section_id = ending.get("section_id")
        ending_refs = set(ending.get("meaning_ids") or [])
        if ending_section_id not in narrative_sections:
            errors.append("semantic_plan.ending.section_id references an unknown narrative section")
        elif narrative_sections and ending_section_id != next(reversed(narrative_sections)):
            errors.append("semantic_plan.ending.section_id must be the final narrative section")
        if not ending_refs:
            errors.append("semantic_plan.ending needs meaning_ids")
        unknown = ending_refs - meaning_ids
        if unknown:
            errors.append(f"semantic_plan.ending references unknown meanings: {sorted(unknown)}")
        if ending_section_id in narrative_sections:
            misplaced = ending_refs - narrative_sections[ending_section_id]
            if misplaced:
                errors.append(
                    f"semantic_plan.ending meaning_ids are outside narrative section "
                    f"{ending_section_id!r}: {sorted(misplaced)}"
                )
    deliverable_items = plan.get("deliverables")
    deliverables: dict[str, dict[str, Any]] = {}
    if not isinstance(deliverable_items, list) or not deliverable_items:
        errors.append("semantic_plan.deliverables is empty")
        deliverable_items = []
    for index, deliverable in enumerate(deliverable_items):
        if not isinstance(deliverable, dict) or not isinstance(deliverable.get("id"), str) or not deliverable["id"].strip():
            errors.append(f"deliverables[{index}] needs a stable id")
            continue
        deliverable_id = deliverable["id"]
        if deliverable_id in deliverables:
            errors.append(f"deliverables[{index}] duplicates deliverable id {deliverable_id!r}")
            continue
        deliverables[deliverable_id] = deliverable
        raw_scope = deliverable.get("section_ids")
        if not isinstance(raw_scope, list) or not raw_scope:
            errors.append(f"deliverables[{index}].section_ids must be a non-empty ordered array")
            scope: list[str] = []
        else:
            scope = [value for value in raw_scope if isinstance(value, str)]
            if len(scope) != len(raw_scope):
                errors.append(f"deliverables[{index}].section_ids contains a non-string ID")
            if len(set(scope)) != len(scope):
                errors.append(f"deliverables[{index}].section_ids contains duplicates")
            unknown_sections = [value for value in scope if value not in narrative_sections]
            if unknown_sections:
                errors.append(
                    f"deliverables[{index}].section_ids references unknown narrative sections: "
                    f"{unknown_sections}"
                )
        deliverable_hook_id = deliverable.get("hook_id")
        if deliverable_hook_id not in hook_ids:
            errors.append(f"deliverables[{index}].hook_id references an unknown hook")
        elif scope and scope[0] in narrative_sections:
            outside_opening = (
                hook_meanings.get(str(deliverable_hook_id), set())
                - narrative_sections[scope[0]]
            )
            if outside_opening:
                errors.append(
                    f"deliverables[{index}].hook_id={deliverable_hook_id!r} uses meaning_ids "
                    f"outside its first scoped section {scope[0]!r}: {sorted(outside_opening)}"
                )
        deliverable_ending = deliverable.get("ending_section_id")
        if deliverable_ending not in narrative_sections:
            errors.append(f"deliverables[{index}].ending_section_id references an unknown section")
        if scope and deliverable_ending != scope[-1]:
            errors.append(
                f"deliverables[{index}].ending_section_id must equal the final scoped section "
                f"{scope[-1]!r}"
            )
    return meaning_ids, narrative_sections, deliverables


def validate_sources(
    edit_dir: Path, project: dict[str, Any], errors: list[str]
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    manifest_path = resolve_project_path(edit_dir, str(project.get("source_manifest") or "source_manifest.json"))
    manifest = load(manifest_path, errors)
    raw_root = Path(str(manifest.get("root") or ".."))
    root = raw_root if raw_root.is_absolute() else (manifest_path.parent / raw_root).resolve()
    sources: dict[str, Path] = {}
    source_records: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(manifest.get("sources") or []):
        if not isinstance(item, dict):
            errors.append(f"source_manifest.sources[{index}] is invalid")
            continue
        raw_sid = item.get("id")
        sid = raw_sid if isinstance(raw_sid, str) else ""
        safe_sid = bool(sid and sid not in {".", ".."} and Path(sid).name == sid)
        if not safe_sid or sid in sources:
            errors.append(f"source_manifest.sources[{index}] has invalid/duplicate id")
        is_new_source = bool(safe_sid and sid not in sources)
        path = Path(str(item.get("path") or ""))
        path = path if path.is_absolute() else root / path
        path = path.resolve()
        if is_new_source:
            sources[sid] = path
            source_records[sid] = {
                "path": path,
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
                "mtime_ns": item.get("mtime_ns"),
                "audio": item.get("audio"),
                "video": item.get("video"),
                "duration_s": item.get("duration_s"),
            }
        if "audio" not in item or (item.get("audio") is not None and not isinstance(item.get("audio"), dict)):
            errors.append(f"source_manifest.sources[{index}].audio must be an object or null")
        duration = finite_non_negative_time(item.get("duration_s"))
        if duration is None or duration <= 0:
            errors.append(f"source_manifest.sources[{index}].duration_s must be positive")
        if not path.is_file():
            errors.append(f"source missing: {path}")
            continue
        stat_info = path.stat()
        if is_new_source:
            source_records[sid]["current_size_bytes"] = stat_info.st_size
            source_records[sid]["current_mtime_ns"] = stat_info.st_mtime_ns
        if stat_info.st_size != item.get("size_bytes") or stat_info.st_mtime_ns != item.get("mtime_ns"):
            errors.append(f"source changed since ingest: {path.name}; re-initialize/transcribe")
        digest = item.get("sha256")
        if not valid_sha256(digest):
            errors.append(f"source has no valid sha256: {sid}")
        else:
            try:
                current_digest = sha256(path)
            except OSError as exc:
                errors.append(f"cannot hash source {sid!r}: {exc}")
            else:
                if current_digest != digest:
                    errors.append(f"source content hash changed since ingest: {path.name}; re-initialize/transcribe")
                if is_new_source:
                    source_records[sid]["current_sha256"] = current_digest
    return sources, source_records


def resolve_project_path(edit_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (edit_dir / path).resolve()


def load_json_object(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"missing {label}: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {label}: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{label} root must be an object")
        return None
    return data


def parse_transcript_words(
    transcript_path: Path, source_id: str, errors: list[str]
) -> tuple[list[TranscriptEntry], int] | None:
    label = f"transcript for source {source_id!r}"
    transcript = load_json_object(transcript_path, label, errors)
    if transcript is None:
        return None
    words = transcript.get("words")
    if not isinstance(words, list):
        errors.append(f"{label} has no words array")
        return None

    timeline: list[TranscriptEntry] = []
    malformed = False
    previous_start: float | None = None
    for index, item in enumerate(words):
        item_label = f"{label}.words[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} is not an object")
            malformed = True
            continue
        kind = item.get("type", "word")
        if not isinstance(kind, str) or kind not in {"word", "spacing", "audio_event"}:
            errors.append(f"{item_label} has unsupported type {kind!r}")
            malformed = True
            continue
        text = item.get("text")
        if not isinstance(text, str):
            errors.append(f"{item_label}.text must be a string")
            malformed = True
            continue
        start = finite_non_negative_time(item.get("start"))
        end = finite_non_negative_time(item.get("end"))
        if start is None or end is None or end < start:
            errors.append(f"{item_label} has invalid word-level timestamps")
            malformed = True
            continue
        if previous_start is not None and start < previous_start - 1e-9:
            errors.append(f"{item_label} starts before the preceding transcript item")
            malformed = True
        previous_start = start
        # Retain every timed Scribe item. Quote matching still projects only
        # normalized word tokens, while pause/filler validation needs spacing
        # and audio_event intervals that would otherwise disappear.
        timeline.append((kind, text, start, end))
    if malformed:
        return None
    return timeline, len(words)


def transcript_word_tokens(
    transcript_timeline: list[TranscriptEntry],
) -> list[tuple[str, float, float]]:
    tokens: list[tuple[str, float, float]] = []
    for kind, text, start, end in transcript_timeline:
        if kind != "word":
            continue
        for token in normalize_quote(text).split():
            tokens.append((token, start, end))
    return tokens


def evidence_quote_matches_transcript(
    quote: Any,
    start: float,
    end: float,
    transcript_timeline: list[TranscriptEntry],
) -> bool:
    transcript_tokens = transcript_word_tokens(transcript_timeline)
    needle = normalize_quote(quote).split()
    if not needle or len(needle) > len(transcript_tokens):
        return False
    lower_bound = start - EVIDENCE_BOUNDARY_PADDING_S - 1e-9
    upper_bound = end + EVIDENCE_BOUNDARY_PADDING_S + 1e-9
    width = len(needle)
    for index in range(len(transcript_tokens) - width + 1):
        candidate = transcript_tokens[index:index + width]
        if [token for token, _, _ in candidate] != needle:
            continue
        candidate_start = min(token_start for _, token_start, _ in candidate)
        candidate_end = max(token_end for _, _, token_end in candidate)
        if candidate_start >= lower_bound and candidate_end <= upper_bound:
            return True
    return False


def retained_transcript_tokens(
    start: float,
    end: float,
    transcript_timeline: list[TranscriptEntry],
) -> list[str]:
    """Return every spoken token whose midpoint survives the exact cut."""
    return [
        token
        for token, token_start, token_end in transcript_word_tokens(transcript_timeline)
        if start - 1e-9 <= (token_start + token_end) / 2.0 <= end + 1e-9
    ]


def retained_quote_matches_transcript(
    quote: Any,
    start: float,
    end: float,
    transcript_timeline: list[TranscriptEntry],
) -> bool:
    return normalize_quote(quote).split() == retained_transcript_tokens(
        start, end, transcript_timeline
    )


def vocalized_fillers(tokens: list[str]) -> list[str]:
    """Find unambiguous hesitation sounds; lexical filler stays editorial."""
    patterns = (
        re.compile(r"^э+$"),
        re.compile(r"^э+м+$"),
        re.compile(r"^м+$"),
        re.compile(r"^а{2,}$"),
        re.compile(r"^u+h+$"),
        re.compile(r"^u+m+$"),
        re.compile(r"^e+r+m+$"),
        re.compile(r"^h+m+$"),
    )
    return [token for token in tokens if any(pattern.fullmatch(token) for pattern in patterns)]


def retained_filler_audio_events(
    start: float,
    end: float,
    transcript_timeline: list[TranscriptEntry],
) -> list[str]:
    fillers: list[str] = []
    for kind, text, event_start, event_end in transcript_timeline:
        if kind != "audio_event":
            continue
        midpoint = (event_start + event_end) / 2.0
        if start - 1e-9 <= midpoint <= end + 1e-9:
            normalized = normalize_quote(text).split()
            matched = vocalized_fillers(normalized)
            if matched:
                fillers.append(text.strip() or "<empty audio_event>")
    return fillers


def speech_silence_gaps(
    start: float,
    end: float,
    transcript_timeline: list[TranscriptEntry],
) -> list[tuple[str, float, float]]:
    """Return boundary/internal gaps between timed audible transcript items."""
    audible: list[tuple[float, float]] = []
    for kind, text, item_start, item_end in transcript_timeline:
        if kind not in {"word", "audio_event"} or not normalize_quote(text):
            continue
        clipped_start = max(start, item_start)
        clipped_end = min(end, item_end)
        if clipped_end >= clipped_start and item_end >= start and item_start <= end:
            audible.append((clipped_start, clipped_end))
    audible.sort()
    merged: list[list[float]] = []
    for item_start, item_end in audible:
        if not merged or item_start > merged[-1][1] + 1e-9:
            merged.append([item_start, item_end])
        else:
            merged[-1][1] = max(merged[-1][1], item_end)
    if not merged:
        return [("boundary", start, end)] if end > start else []
    gaps: list[tuple[str, float, float]] = []
    if merged[0][0] > start + 1e-9:
        gaps.append(("boundary", start, merged[0][0]))
    for previous, following in zip(merged, merged[1:]):
        if following[0] > previous[1] + 1e-9:
            gaps.append(("internal", previous[1], following[0]))
    if end > merged[-1][1] + 1e-9:
        gaps.append(("boundary", merged[-1][1], end))
    return gaps


def validate_transcript_metadata(
    metadata_path: Path,
    transcript_name: str,
    transcript_word_count: int | None,
    source_id: str,
    source_record: dict[str, Any],
    errors: list[str],
) -> bool:
    label = f"transcript metadata for source {source_id!r}"
    metadata = load_json_object(metadata_path, label, errors)
    if metadata is None:
        return False
    valid = True
    if metadata.get("version") != 1 or isinstance(metadata.get("version"), bool):
        errors.append(f"{label} version must be 1")
        valid = False
    identity = metadata.get("identity")
    if not isinstance(identity, dict):
        errors.append(f"{label}.identity must be an object")
        return False

    raw_source = identity.get("source")
    if not isinstance(raw_source, str) or not raw_source.strip():
        errors.append(f"{label}.identity.source is missing")
        valid = False
    else:
        metadata_source = Path(raw_source).expanduser()
        if not metadata_source.is_absolute() or metadata_source.resolve() != source_record.get("path"):
            errors.append(f"{label}.identity.source does not match source_manifest")
            valid = False

    manifest_digest = source_record.get("sha256")
    current_digest = source_record.get("current_sha256")
    metadata_digest = identity.get("source_sha256")
    if (
        not valid_sha256(metadata_digest)
        or metadata_digest != manifest_digest
        or current_digest is None
        or metadata_digest != current_digest
    ):
        errors.append(f"{label}.identity.source_sha256 is stale or invalid")
        valid = False

    for metadata_key, manifest_key, current_key in (
        ("source_size", "size_bytes", "current_size_bytes"),
        ("source_mtime_ns", "mtime_ns", "current_mtime_ns"),
    ):
        value = identity.get(metadata_key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != source_record.get(manifest_key)
            or value != source_record.get(current_key)
        ):
            errors.append(f"{label}.identity.{metadata_key} is stale or invalid")
            valid = False
    if identity.get("model_id") != EXPECTED_TRANSCRIPT_MODEL:
        errors.append(
            f"{label}.identity.model_id must be {EXPECTED_TRANSCRIPT_MODEL!r}"
        )
        valid = False
    if identity.get("timestamps_granularity") != "word":
        errors.append(f"{label}.identity.timestamps_granularity must be 'word'")
        valid = False
    if metadata.get("transcript") != transcript_name:
        errors.append(f"{label}.transcript must be {transcript_name!r}")
        valid = False
    recorded_words = metadata.get("words")
    if (
        transcript_word_count is None
        or isinstance(recorded_words, bool)
        or not isinstance(recorded_words, int)
        or recorded_words != transcript_word_count
    ):
        errors.append(f"{label}.words does not match the raw transcript")
        valid = False
    return valid


def validate_packed_transcripts(
    edit_dir: Path,
    source_records: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, list[TranscriptEntry]]:
    manifest_path = edit_dir / "takes_packed_manifest.json"
    packed = load_json_object(manifest_path, "takes_packed_manifest.json", errors)
    if packed is None:
        return {}
    if packed.get("version") != 1 or isinstance(packed.get("version"), bool):
        errors.append("takes_packed_manifest.json version must be 1")

    output_value = packed.get("output")
    output_path: Path | None = None
    if not isinstance(output_value, str) or not output_value.strip():
        errors.append("takes_packed_manifest.json output is missing")
    else:
        output_path = resolve_project_path(edit_dir, output_value)
    output_digest = packed.get("output_sha256")
    if not valid_sha256(output_digest):
        errors.append("takes_packed_manifest.json output_sha256 is invalid")
    if output_path is not None:
        if not output_path.is_file():
            errors.append(f"packed transcript output is missing: {output_path}")
        elif valid_sha256(output_digest) and sha256(output_path) != output_digest:
            errors.append("packed transcript output hash does not match takes_packed_manifest.json")

    silence_threshold = finite_non_negative_time(packed.get("silence_threshold_s"))
    if silence_threshold is None or not 0.1 <= silence_threshold <= 5.0:
        errors.append("takes_packed_manifest.json silence_threshold_s must be between 0.1 and 5")

    raw_entries = packed.get("sources")
    if not isinstance(raw_entries, list) or not raw_entries:
        errors.append("takes_packed_manifest.json sources must be a non-empty array")
        raw_entries = []
    packed_entries: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            errors.append(f"takes_packed_manifest.sources[{index}] is not an object")
            continue
        source_id = entry.get("source")
        if not isinstance(source_id, str) or not source_id:
            errors.append(f"takes_packed_manifest.sources[{index}].source is invalid")
            continue
        if source_id in packed_entries:
            errors.append(f"takes_packed_manifest.sources[{index}] duplicates source {source_id!r}")
            continue
        packed_entries[source_id] = (index, entry)

    expected_ids = set(source_records)
    packed_ids = set(packed_entries)
    missing_ids = expected_ids - packed_ids
    extra_ids = packed_ids - expected_ids
    if missing_ids:
        errors.append(f"takes_packed_manifest.json is missing source IDs: {sorted(missing_ids)}")
    if extra_ids:
        errors.append(f"takes_packed_manifest.json has unknown source IDs: {sorted(extra_ids)}")

    transcripts: dict[str, list[TranscriptEntry]] = {}
    transcripts_dir = (edit_dir / "transcripts").resolve()
    for source_id, source_record in source_records.items():
        transcript_path = (transcripts_dir / f"{source_id}.json").resolve()
        metadata_path = (transcripts_dir / ".metadata" / f"{source_id}.json").resolve()
        entry_record = packed_entries.get(source_id)
        source_valid = entry_record is not None
        if entry_record is not None:
            entry_index, entry = entry_record
            label = f"takes_packed_manifest.sources[{entry_index}]"
            packed_source_digest = entry.get("source_sha256")
            if (
                not valid_sha256(packed_source_digest)
                or packed_source_digest != source_record.get("sha256")
                or packed_source_digest != source_record.get("current_sha256")
            ):
                errors.append(f"{label}.source_sha256 is stale or invalid for source {source_id!r}")
                source_valid = False

        if source_record.get("audio") is None:
            if entry_record is None:
                continue
            entry_index, entry = entry_record
            label = f"takes_packed_manifest.sources[{entry_index}]"
            canonical_keys = {"source", "source_sha256", "visual_only", "duration_s", "phrases"}
            missing_keys = canonical_keys - set(entry)
            extra_keys = set(entry) - canonical_keys
            if missing_keys:
                errors.append(f"{label} visual_only entry is missing keys: {sorted(missing_keys)}")
            if extra_keys:
                errors.append(f"{label} visual_only entry has non-canonical keys: {sorted(extra_keys)}")
            if entry.get("visual_only") is not True:
                errors.append(f"{label}.visual_only must be true for no-audio source {source_id!r}")
            phrase_count = entry.get("phrases")
            if isinstance(phrase_count, bool) or phrase_count != 0:
                errors.append(f"{label}.phrases must be 0 for no-audio source {source_id!r}")
            packed_duration = finite_non_negative_time(entry.get("duration_s"))
            source_duration = finite_non_negative_time(source_record.get("duration_s"))
            if (
                packed_duration is None
                or packed_duration <= 0
                or source_duration is None
                or abs(packed_duration - source_duration) > 1e-6
            ):
                errors.append(f"{label}.duration_s is stale or invalid for source {source_id!r}")
            continue

        if entry_record is not None:
            entry_index, entry = entry_record
            label = f"takes_packed_manifest.sources[{entry_index}]"
            if "visual_only" in entry:
                errors.append(f"{label}.visual_only is forbidden for audio source {source_id!r}")
            raw_transcript_path = entry.get("transcript")
            if not isinstance(raw_transcript_path, str) or not raw_transcript_path.strip():
                errors.append(f"{label}.transcript is missing")
                source_valid = False
            elif resolve_project_path(edit_dir, raw_transcript_path) != transcript_path:
                errors.append(
                    f"{label}.transcript must reference transcripts/{source_id}.json"
                )
                source_valid = False
            phrase_count = entry.get("phrases")
            if isinstance(phrase_count, bool) or not isinstance(phrase_count, int) or phrase_count < 0:
                errors.append(f"{label}.phrases must be a non-negative integer")
                source_valid = False

        transcript_digest: str | None = None
        if not transcript_path.is_file():
            errors.append(f"missing transcript for source {source_id!r}: {transcript_path}")
            source_valid = False
        else:
            transcript_digest = sha256(transcript_path)
        if entry_record is not None:
            entry_index, entry = entry_record
            packed_transcript_digest = entry.get("transcript_sha256")
            if not valid_sha256(packed_transcript_digest):
                errors.append(
                    f"takes_packed_manifest.sources[{entry_index}].transcript_sha256 is invalid"
                )
                source_valid = False
            elif transcript_digest is None or packed_transcript_digest != transcript_digest:
                errors.append(
                    f"takes_packed_manifest.sources[{entry_index}].transcript_sha256 is stale "
                    f"for source {source_id!r}"
                )
                source_valid = False

        parsed = (
            parse_transcript_words(transcript_path, source_id, errors)
            if transcript_path.is_file()
            else None
        )
        transcript_word_count = parsed[1] if parsed is not None else None
        if parsed is None:
            source_valid = False
        if not validate_transcript_metadata(
            metadata_path,
            transcript_path.name,
            transcript_word_count,
            source_id,
            source_record,
            errors,
        ):
            source_valid = False
        if source_valid and parsed is not None:
            transcripts[source_id] = parsed[0]
    return transcripts


def validate_plan_evidence_transcripts(
    plan: dict[str, Any],
    transcript_tokens: dict[str, list[TranscriptEntry]],
    source_records: dict[str, dict[str, Any]],
    errors: list[str],
) -> None:
    for meaning_index, meaning in enumerate(plan.get("source_truth") or []):
        if not isinstance(meaning, dict):
            continue
        for evidence_index, evidence in enumerate(meaning.get("evidence") or []):
            if not isinstance(evidence, dict):
                continue
            label = f"source_truth[{meaning_index}].evidence[{evidence_index}]"
            if evidence.get("modality") != "speech":
                continue
            source_id = evidence.get("source")
            source_record = source_records.get(source_id) if isinstance(source_id, str) else None
            if source_record is not None and source_record.get("audio") is None:
                errors.append(f"{label} speech evidence cannot target no-audio source {source_id!r}")
                continue
            if not isinstance(source_id, str) or source_id not in transcript_tokens:
                errors.append(f"{label} cannot be verified against a valid packed transcript")
                continue
            start = finite_non_negative_time(evidence.get("start"))
            end = finite_non_negative_time(evidence.get("end"))
            if start is None or end is None or end <= start or not normalize_quote(evidence.get("quote")):
                continue
            if not evidence_quote_matches_transcript(
                evidence.get("quote"), start, end, transcript_tokens[source_id]
            ):
                errors.append(
                    f"{label} quote is not a contiguous normalized token sequence in source "
                    f"{source_id!r} transcript words within [{start:.3f}, {end:.3f}] plus "
                    f"{EVIDENCE_BOUNDARY_PADDING_S:.2f}s boundary tolerance"
                )


def valid_normalized_roi(value: Any) -> bool:
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    if not isinstance(value, list) or len(value) != 4:
        return False
    try:
        x, y, width, height = (float(part) for part in value)
    except (TypeError, ValueError):
        return False
    return bool(
        all(math.isfinite(part) for part in (x, y, width, height))
        and x >= 0 and y >= 0 and width > 0 and height > 0
        and x + width <= 1.000001 and y + height <= 1.000001
    )


def normalized_roi_values(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("width"), value.get("height")]
    if not valid_normalized_roi(value):
        return None
    return tuple(float(part) for part in value)  # type: ignore[return-value]


def output_box_dimensions(value: Any, output: dict[str, Any]) -> tuple[float, float] | None:
    if isinstance(value, list) and valid_normalized_roi(value):
        return float(value[2]) * float(output.get("width", 0)), float(value[3]) * float(output.get("height", 0))
    if not isinstance(value, dict):
        return None
    try:
        if value.get("space") == "pixels":
            width, height = float(value["width"]), float(value["height"])
            x, y = float(value["x"]), float(value["y"])
            if width <= 0 or height <= 0 or x < 0 or y < 0:
                return None
            if x + width > float(output.get("width", 0)) or y + height > float(output.get("height", 0)):
                return None
            return width, height
        if value.get("space") == "normalized" and valid_normalized_roi(value):
            return (
                float(value["width"]) * float(output.get("width", 0)),
                float(value["height"]) * float(output.get("height", 0)),
            )
    except (KeyError, TypeError, ValueError):
        return None
    return None


def output_box_values(
    value: Any, output: dict[str, Any]
) -> tuple[float, float, float, float] | None:
    """Resolve an output box to declared-output pixel coordinates."""
    try:
        output_width = float(output.get("width", 0))
        output_height = float(output.get("height", 0))
    except (TypeError, ValueError):
        return None
    if output_width <= 0 or output_height <= 0:
        return None
    if isinstance(value, list) and valid_normalized_roi(value):
        x, y, width, height = (float(part) for part in value)
        return (
            x * output_width,
            y * output_height,
            width * output_width,
            height * output_height,
        )
    if not isinstance(value, dict):
        return None
    try:
        x = float(value["x"])
        y = float(value["y"])
        width = float(value["width"])
        height = float(value["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if value.get("space") == "normalized":
        x *= output_width
        y *= output_height
        width *= output_width
        height *= output_height
    elif value.get("space") != "pixels":
        return None
    if (
        not all(math.isfinite(part) for part in (x, y, width, height))
        or x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > output_width + 1e-6
        or y + height > output_height + 1e-6
    ):
        return None
    return x, y, width, height


def source_display_dimensions(source_record: dict[str, Any] | None) -> tuple[float, float] | None:
    if not isinstance(source_record, dict):
        return None
    video = source_record.get("video")
    if not isinstance(video, dict):
        return None
    try:
        width = float(video.get("display_width", video.get("width", 0)))
        height = float(video.get("display_height", video.get("height", 0)))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(width + height) or width <= 0 or height <= 0:
        return None
    return width, height


def mapped_important_screen_box(
    *,
    important: tuple[float, float, float, float],
    screen: tuple[float, float, float, float],
    screen_box: tuple[float, float, float, float],
    source_dimensions: tuple[float, float],
) -> tuple[float, float, float, float]:
    """Map a source-normalized protected ROI through crop + contain + pad."""
    ix, iy, iw, ih = important
    sx, sy, sw, sh = screen
    box_x, box_y, box_width, box_height = screen_box
    source_width, source_height = source_dimensions
    crop_width = sw * source_width
    crop_height = sh * source_height
    scale = min(box_width / crop_width, box_height / crop_height)
    rendered_width = crop_width * scale
    rendered_height = crop_height * scale
    pad_x = box_x + (box_width - rendered_width) / 2.0
    pad_y = box_y + (box_height - rendered_height) / 2.0
    return (
        pad_x + (ix - sx) * source_width * scale,
        pad_y + (iy - sy) * source_height * scale,
        iw * source_width * scale,
        ih * source_height * scale,
    )


def rectangles_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    tolerance: float = 1.0,
) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return bool(
        min(ax + aw, bx + bw) - max(ax, bx) > tolerance
        and min(ay + ah, by + bh) - max(ay, by) > tolerance
    )


def platform_safe_rect(project: dict[str, Any], output: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        width = float(output["width"])
        height = float(output["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    vertical = height > width
    reference_width, reference_height = ((1080.0, 1920.0) if vertical else (1920.0, 1080.0))
    defaults = (
        {"top": 150, "right": 150, "bottom": 420, "left": 80}
        if vertical
        else {"top": 70, "right": 100, "bottom": 70, "left": 100}
    )
    qa = project.get("qa") if isinstance(project.get("qa"), dict) else {}
    key = "vertical_safe_area" if vertical else "horizontal_safe_area"
    configured = qa.get(key) if isinstance(qa.get(key), dict) else defaults
    try:
        top = float(configured.get("top", defaults["top"])) * height / reference_height
        right = float(configured.get("right", defaults["right"])) * width / reference_width
        bottom = float(configured.get("bottom", defaults["bottom"])) * height / reference_height
        left = float(configured.get("left", defaults["left"])) * width / reference_width
    except (TypeError, ValueError):
        return None
    safe_width = width - left - right
    safe_height = height - top - bottom
    if not all(math.isfinite(part) for part in (top, right, bottom, left)) or safe_width <= 0 or safe_height <= 0:
        return None
    return left, top, safe_width, safe_height


def rectangle_is_inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
    *,
    tolerance: float = 1.0,
) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return bool(
        ix >= ox - tolerance
        and iy >= oy - tolerance
        and ix + iw <= ox + ow + tolerance
        and iy + ih <= oy + oh + tolerance
    )


def split_audio_filter_chain(value: Any, *, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty filter string")
        return []
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
                errors.append(f"{label} has unbalanced parentheses")
                return []
        elif character == "," and depth == 0:
            parts.append(value[start:position].strip())
            start = position + 1
    if quote is not None or depth != 0 or escaped:
        errors.append(f"{label} has an unterminated expression")
        return []
    parts.append(value[start:].strip())
    if any(not part for part in parts):
        errors.append(f"{label} contains an empty filter")
        return []
    return parts


def validate_audio_cleanup_filters(edl: dict[str, Any], errors: list[str]) -> None:
    audio = edl.get("audio")
    raw_filters = audio.get("filters") if isinstance(audio, dict) else None
    if not isinstance(raw_filters, list):
        errors.append("EDL audio.filters must be an array")
        return
    for index, value in enumerate(raw_filters):
        label = f"audio.filters[{index}]"
        for part in split_audio_filter_chain(value, label=label, errors=errors):
            match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(?:=|$)", part)
            name = match.group(1).lower() if match else ""
            if name not in AUDIO_CLEANUP_FILTERS:
                errors.append(
                    f"{label} uses {name or part!r}; only duration/PTS-preserving cleanup "
                    f"filters are allowed: {sorted(AUDIO_CLEANUP_FILTERS)}"
                )


def speech_pause_thresholds(
    project: dict[str, Any], errors: list[str]
) -> tuple[float, float]:
    audio = project.get("audio") if isinstance(project.get("audio"), dict) else {}
    values: list[float] = []
    for field, default, low, high in (
        (
            "max_unapproved_boundary_silence_s",
            DEFAULT_MAX_BOUNDARY_SILENCE_S,
            0.05,
            2.0,
        ),
        (
            "max_unapproved_internal_silence_s",
            DEFAULT_MAX_INTERNAL_SILENCE_S,
            0.10,
            5.0,
        ),
    ):
        raw = audio.get(field, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
            errors.append(f"project.audio.{field} must be numeric")
        if not math.isfinite(value) or not low <= value <= high:
            errors.append(
                f"project.audio.{field} must be finite and between {low:.2f} and {high:.2f} seconds"
            )
            value = default
        values.append(value)
    return values[0], values[1]


def validate_approval(edit_dir: Path, plan_path: Path, errors: list[str]) -> str | None:
    approval = load(edit_dir / "approval.json", errors)
    if not plan_path.is_file():
        return None
    digest = sha256(plan_path)
    if approval.get("status") != "approved":
        errors.append("semantic approval status is not approved")
    if approval.get("proposal_sha256") != digest:
        errors.append("semantic plan changed after approval")
    scopes = set(approval.get("approved_scope") or [])
    if not REQUIRED_SCOPES.issubset(scopes):
        errors.append(f"approval scopes missing: {sorted(REQUIRED_SCOPES - scopes)}")
    if not nonempty(approval.get("user_quote")):
        errors.append("approval has no exact user quote")
    return digest


def renderer_output_intervals(
    ranges: list[Any], fps_value: Any, errors: list[str]
) -> list[dict[str, Any]]:
    """Build the exact cumulative, error-diffused frame timeline used by the renderer."""
    fps = fps_fraction(fps_value)
    if fps is None:
        errors.append("cannot bind subtitle/overlay timing without a valid positive output FPS")
        return []
    intervals: list[dict[str, Any]] = []
    cumulative_duration = Fraction(0)
    cumulative_frames = 0
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            return []
        try:
            start = Fraction(str(item["start"]))
            end = Fraction(str(item["end"]))
        except (KeyError, ValueError, ZeroDivisionError):
            return []
        if start < 0 or end <= start:
            return []
        cumulative_duration += end - start
        next_frames = max(cumulative_frames + 1, round(cumulative_duration * fps))
        intervals.append({
            "index": index,
            "start_frame": cumulative_frames,
            "end_frame": next_frames,
            "start_s": float(Fraction(cumulative_frames, 1) / fps),
            "end_s": float(Fraction(next_frames, 1) / fps),
            "section_id": item.get("section_id"),
            "audio_mode": item.get("audio_mode"),
        })
        cumulative_frames = next_frames
    return intervals


def interval_overlap_s(
    start: float, end: float, interval: dict[str, Any]
) -> float:
    return max(
        0.0,
        min(end, float(interval["end_s"])) - max(start, float(interval["start_s"])),
    )


def validate_subtitle_semantic_text(
    edit_dir: Path,
    edl: dict[str, Any],
    ranges: list[Any],
    range_modalities: dict[int, set[str]],
    output_intervals: list[dict[str, Any]],
    errors: list[str],
) -> None:
    mode = edl.get("subtitle_mode")
    if mode not in {"burned", "sidecar"}:
        return
    raw_path = edl.get("subtitles")
    if not isinstance(raw_path, str) or not raw_path.strip():
        errors.append(f"subtitle_mode={mode!r} requires an actual cue file")
        return
    subtitle_path = resolve_project_path(edit_dir, raw_path)
    try:
        _, cues = parse_caption_file(subtitle_path)
    except (CaptionParseError, OSError) as exc:
        errors.append(f"cannot parse subtitle cue file for semantic binding: {exc}")
        return
    expected_parts: list[str] = []
    expected_tokens: list[tuple[str, int]] = []
    for index, item in enumerate(ranges):
        if (
            isinstance(item, dict)
            and "speech" in range_modalities.get(index, set())
        ):
            quote = str(item.get("quote") or "")
            expected_parts.append(quote)
            expected_tokens.extend(
                (token, index) for token in normalize_quote(quote).split()
            )
    expected = normalize_quote(" ".join(expected_parts))
    actual = normalize_quote(" ".join(str(cue.text or "") for cue in cues))
    if actual != expected:
        errors.append(
            "subtitle visible cue text must exactly equal the concatenated speech-range quotes "
            "in output order; put editorial titles/CTA text in approved overlays instead"
        )

    if not output_intervals:
        return
    interval_by_index = {
        int(interval["index"]): interval for interval in output_intervals
    }
    programme_end = float(output_intervals[-1]["end_s"])
    previous_start: float | None = None
    previous_end: float | None = None
    token_cursor = 0
    for cue_number, cue in enumerate(cues, start=1):
        label = f"subtitle cue {cue_number} ({cue.source_ref})"
        start = finite_non_negative_time(cue.start)
        end = finite_non_negative_time(cue.end)
        if start is None or end is None:
            errors.append(f"{label} timing must be finite and non-negative")
            continue
        if end <= start:
            errors.append(f"{label} must end after it starts")
            continue
        if previous_start is not None and start < previous_start - 1e-9:
            errors.append(f"{label} is out of chronological order")
        if previous_end is not None and start < previous_end - 1e-9:
            errors.append(f"{label} overlaps the previous subtitle cue")
        previous_start, previous_end = start, end
        if end > programme_end + 1e-9:
            errors.append(
                f"{label} ends outside the frame-quantized programme at "
                f"{programme_end:.3f}s"
            )

        cue_tokens = normalize_quote(cue.text).split()
        if not cue_tokens:
            errors.append(f"{label} has no semantic text tokens to bind")
            continue
        token_slice = expected_tokens[token_cursor:token_cursor + len(cue_tokens)]
        token_cursor += len(cue_tokens)
        if len(token_slice) != len(cue_tokens) or [value for value, _ in token_slice] != cue_tokens:
            # The global exact-text error above is the canonical explanation;
            # avoid inventing a timing assignment for non-approved words.
            continue
        mapped_indices = {index for _, index in token_slice}
        mapped_intervals = [
            interval_by_index[index]
            for index in sorted(mapped_indices)
            if index in interval_by_index
        ]
        if not mapped_intervals:
            errors.append(f"{label} cannot be mapped to a speech-backed output range")
            continue
        mapped_start = float(mapped_intervals[0]["start_s"])
        mapped_end = float(mapped_intervals[-1]["end_s"])
        if (
            start < mapped_start - SEMANTIC_TIMING_TOLERANCE_S - 1e-9
            or end > mapped_end + SEMANTIC_TIMING_TOLERANCE_S + 1e-9
        ):
            errors.append(
                f"{label} timing [{start:.3f}, {end:.3f}] lies outside the "
                f"speech-range interval(s) that supplied its sequential text tokens "
                f"[{mapped_start:.3f}, {mapped_end:.3f}]"
            )

        for interval in output_intervals:
            overlap = interval_overlap_s(start, end, interval)
            if overlap <= SEMANTIC_TIMING_TOLERANCE_S + 1e-9:
                continue
            range_index = int(interval["index"])
            is_speech = "speech" in range_modalities.get(range_index, set())
            if interval.get("audio_mode") == "mute" or not is_speech:
                errors.append(
                    f"{label} overlaps muted/non-speech output range[{range_index}] by "
                    f"{overlap:.3f}s; maximum boundary tolerance is "
                    f"{SEMANTIC_TIMING_TOLERANCE_S:.3f}s"
                )
            elif range_index not in mapped_indices:
                errors.append(
                    f"{label} overlaps speech output range[{range_index}] that did not "
                    "supply its sequential text tokens"
                )

    if token_cursor != len(expected_tokens) and actual == expected:
        errors.append(
            "subtitle cue token partition does not cover every approved speech-range token"
        )


def validate_overlay_semantics(
    edit_dir: Path,
    edl: dict[str, Any],
    plan: dict[str, Any],
    scoped_section_ids: list[str],
    output_intervals: list[dict[str, Any]],
    fps_value: Any,
    errors: list[str],
) -> None:
    visual_map: dict[str, dict[str, Any]] = {}
    for item in plan.get("visual_plan") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            visual_map.setdefault(item["id"], item)
    overlays = edl.get("overlays") or []
    if not isinstance(overlays, list):
        return
    for index, overlay in enumerate(overlays):
        label = f"overlays[{index}]"
        if not isinstance(overlay, dict):
            continue
        visual_id = overlay.get("visual_id")
        approved = visual_map.get(visual_id) if isinstance(visual_id, str) else None
        if approved is None:
            errors.append(
                f"{label}.visual_id={visual_id!r} does not map to semantic_plan.visual_plan"
            )
            continue
        for field in ("purpose", "section_id", "meaning_ids"):
            if overlay.get(field) != approved.get(field):
                errors.append(
                    f"{label}.{field} must exactly match visual_plan item {visual_id!r}"
                )
        if overlay.get("semantic_text") != approved.get("approved_text"):
            errors.append(
                f"{label}.semantic_text must exactly match visual_plan item "
                f"{visual_id!r}.approved_text"
            )
        if approved.get("asset_type") == "none":
            errors.append(f"{label} cannot materialize visual_plan item {visual_id!r} with asset_type='none'")
        if overlay.get("section_id") not in scoped_section_ids:
            errors.append(
                f"{label}.section_id is outside the selected deliverable scope"
            )
        try:
            verify_visual_asset_provenance(
                edit_dir,
                Path(str(overlay.get("provenance") or "")),
                asset_path=Path(str(overlay.get("file") or "")),
                overlay=overlay,
            )
        except (OSError, VisualProvenanceError) as exc:
            errors.append(f"{label} visual provenance is invalid: {exc}")
        validate_overlay_section_timing(
            overlay,
            label=label,
            output_intervals=output_intervals,
            fps_value=fps_value,
            errors=errors,
        )


def resolve_overlay_frames(
    overlay: dict[str, Any],
    *,
    label: str,
    output_intervals: list[dict[str, Any]],
    fps: Fraction,
    errors: list[str],
) -> tuple[int, int] | None:
    """Resolve every canonical visual-overlay anchor exactly like render_edl.py."""
    if not output_intervals:
        return None
    fps_float = float(fps)
    try:
        duration = float(overlay.get("duration"))
        offset = float(overlay.get("offset_s", 0.0))
    except (TypeError, ValueError):
        errors.append(f"{label} duration/offset must be numeric")
        return None
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(offset):
        errors.append(f"{label} duration must be positive and duration/offset must be finite")
        return None
    duration_frames = max(1, round(duration * fps_float))
    total_frames = int(output_intervals[-1]["end_frame"])
    try:
        if "start_at_range_index" in overlay:
            index = int(overlay["start_at_range_index"])
            if not 0 <= index < len(output_intervals):
                raise IndexError
            start_frame = int(output_intervals[index]["start_frame"]) + round(offset * fps_float)
        elif "start_after_range_index" in overlay:
            index = int(overlay["start_after_range_index"])
            if not 0 <= index < len(output_intervals):
                raise IndexError
            start_frame = int(output_intervals[index]["end_frame"]) + round(offset * fps_float)
        elif overlay.get("align_to_end") is True:
            start_frame = total_frames - duration_frames - round(offset * fps_float)
        else:
            start_value = overlay.get("start_in_output", overlay.get("start"))
            if start_value is None:
                errors.append(f"{label} needs one supported timing anchor")
                return None
            start_number = float(start_value)
            if not math.isfinite(start_number):
                errors.append(f"{label} start anchor must be finite")
                return None
            start_frame = round(start_number * fps_float)
    except (TypeError, ValueError):
        errors.append(f"{label} range anchor must be an integer")
        return None
    except IndexError:
        errors.append(f"{label} range anchor is out of bounds")
        return None
    end_frame = start_frame + duration_frames
    if start_frame < 0 or end_frame > total_frames:
        errors.append(f"{label} frame-quantized timing is outside the programme")
        return None
    return start_frame, end_frame


def validate_overlay_section_timing(
    overlay: dict[str, Any],
    *,
    label: str,
    output_intervals: list[dict[str, Any]],
    fps_value: Any,
    errors: list[str],
) -> None:
    fps = fps_fraction(fps_value)
    if fps is None or not output_intervals:
        return
    resolved = resolve_overlay_frames(
        overlay,
        label=label,
        output_intervals=output_intervals,
        fps=fps,
        errors=errors,
    )
    if resolved is None:
        return
    start_frame, end_frame = resolved
    start_s = float(Fraction(start_frame, 1) / fps)
    end_s = float(Fraction(end_frame, 1) / fps)
    section_id = overlay.get("section_id")

    blocks: list[tuple[float, float]] = []
    current: tuple[float, float] | None = None
    for interval in output_intervals:
        if interval.get("section_id") != section_id:
            if current is not None:
                blocks.append(current)
                current = None
            continue
        interval_start = float(interval["start_s"])
        interval_end = float(interval["end_s"])
        if current is not None and abs(current[1] - interval_start) <= 1e-9:
            current = (current[0], interval_end)
        else:
            if current is not None:
                blocks.append(current)
            current = (interval_start, interval_end)
    if current is not None:
        blocks.append(current)

    if not any(
        start_s >= block_start - SEMANTIC_TIMING_TOLERANCE_S - 1e-9
        and end_s <= block_end + SEMANTIC_TIMING_TOLERANCE_S + 1e-9
        for block_start, block_end in blocks
    ):
        rendered_blocks = ", ".join(
            f"[{block_start:.3f}, {block_end:.3f}]" for block_start, block_end in blocks
        ) or "<none>"
        errors.append(
            f"{label} frame-quantized interval [{start_s:.3f}, {end_s:.3f}] is outside "
            f"approved section {section_id!r} contiguous output interval(s) {rendered_blocks}; "
            f"maximum boundary tolerance is {SEMANTIC_TIMING_TOLERANCE_S:.3f}s"
        )


def validate_edl(
    edit_dir: Path,
    edl: dict[str, Any],
    plan_hash: str | None,
    meaning_ids: set[str],
    narrative_sections: dict[str, set[str]],
    deliverables: dict[str, dict[str, Any]],
    plan: dict[str, Any],
    manifest_sources: dict[str, Path],
    source_records: dict[str, dict[str, Any]],
    transcript_tokens: dict[str, list[TranscriptEntry]],
    project: dict[str, Any],
    errors: list[str],
) -> None:
    if edl.get("approval_plan_sha256") != plan_hash:
        errors.append("EDL approval_plan_sha256 does not match the approved plan")
    deliverable_id = edl.get("deliverable_id")
    selected_deliverable = deliverables.get(deliverable_id) if isinstance(deliverable_id, str) else None
    if selected_deliverable is None:
        errors.append(f"EDL deliverable_id {deliverable_id!r} is not an approved semantic deliverable")
        scoped_section_ids: list[str] = []
        selected_hook_id = None
        ending_section_id = None
    else:
        raw_scope = selected_deliverable.get("section_ids")
        scoped_section_ids = (
            [value for value in raw_scope if isinstance(value, str)]
            if isinstance(raw_scope, list)
            else []
        )
        selected_hook_id = selected_deliverable.get("hook_id")
        ending_section_id = selected_deliverable.get("ending_section_id")
    scoped_narrative_sections = {
        section_id: narrative_sections[section_id]
        for section_id in scoped_section_ids
        if section_id in narrative_sections
    }
    if selected_deliverable is not None and not scoped_narrative_sections:
        errors.append(f"deliverable {deliverable_id!r} has no valid narrative section scope")
    if edl.get("hook_id") != selected_hook_id:
        errors.append(
            f"EDL hook_id={edl.get('hook_id')!r} differs from approved "
            f"deliverable {deliverable_id!r} hook_id={selected_hook_id!r}"
        )
    selected_hook_meanings: set[str] = set()
    for hook in plan.get("hooks") or []:
        if isinstance(hook, dict) and hook.get("id") == selected_hook_id:
            selected_hook_meanings = set(hook.get("meaning_ids") or [])
            break
    evidence_map = build_evidence_map(plan)
    output = edl.get("output")
    if not isinstance(output, dict) or not all(key in output for key in ("width", "height", "fps")):
        errors.append("EDL output needs explicit width, height, and fps")
        output = {}
    validate_audio_cleanup_filters(edl, errors)
    max_boundary_silence_s, max_internal_silence_s = speech_pause_thresholds(
        project, errors
    )
    if selected_deliverable is not None:
        for dimension in ("width", "height"):
            if output.get(dimension) != selected_deliverable.get(dimension):
                errors.append(
                    f"EDL output.{dimension}={output.get(dimension)!r} differs from approved "
                    f"deliverable {deliverable_id!r} value {selected_deliverable.get(dimension)!r}"
                )
        if fps_fraction(output.get("fps")) != fps_fraction(selected_deliverable.get("fps")):
            errors.append(
                f"EDL output.fps={output.get('fps')!r} differs from approved deliverable "
                f"{deliverable_id!r} value {selected_deliverable.get('fps')!r}"
            )
        if edl.get("subtitle_mode") != selected_deliverable.get("subtitle_mode"):
            errors.append(
                f"EDL subtitle_mode={edl.get('subtitle_mode')!r} differs from approved deliverable "
                f"{deliverable_id!r} value {selected_deliverable.get('subtitle_mode')!r}"
            )
    edl_sources = edl.get("sources")
    if not isinstance(edl_sources, dict) or not edl_sources:
        errors.append("EDL sources must be a non-empty object")
        edl_sources = {}
    for source_id in edl_sources:
        if source_id not in manifest_sources:
            errors.append(f"EDL source {source_id!r} is absent from the hashed manifest")
    for source_id, manifest_path in manifest_sources.items():
        if source_id not in edl_sources:
            continue
        edl_path = Path(str(edl_sources[source_id])).expanduser()
        edl_path = edl_path.resolve() if edl_path.is_absolute() else (edit_dir / edl_path).resolve()
        if edl_path != manifest_path:
            errors.append(f"EDL source {source_id!r} does not match the hashed manifest path")
    ranges = edl.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        errors.append("EDL ranges must be non-empty")
        ranges = []
    represented_by_section: dict[str, set[str]] = {}
    range_modalities: dict[int, set[str]] = {}
    retained_duration_s = 0.0
    section_indices = {
        section_id: index for index, section_id in enumerate(scoped_narrative_sections)
    }
    previous_section_index = -1
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            errors.append(f"range[{index}] is not an object")
            continue
        if item.get("source") not in manifest_sources:
            errors.append(f"range[{index}] references unknown source")
        if item.get("source") not in edl_sources:
            errors.append(f"range[{index}] source has no EDL path mapping")
        range_source_record = source_records.get(item.get("source"))
        range_bounds: tuple[float, float] | None = None
        try:
            start, end = float(item["start"]), float(item["end"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError
            range_bounds = (start, end)
            retained_duration_s += end - start
            source_duration = (
                finite_non_negative_time(range_source_record.get("duration_s"))
                if range_source_record is not None
                else None
            )
            if source_duration is not None and end > source_duration + 1e-6:
                errors.append(
                    f"range[{index}] ends at {end:.3f}s beyond source duration {source_duration:.3f}s"
                )
        except (KeyError, TypeError, ValueError):
            errors.append(f"range[{index}] has invalid start/end")
        section_id = item.get("section_id")
        if section_id not in narrative_sections:
            errors.append(f"range[{index}] references unknown section_id")
        elif section_id not in scoped_narrative_sections:
            errors.append(
                f"range[{index}] section_id={section_id!r} is outside approved deliverable "
                f"{deliverable_id!r} scope {scoped_section_ids}"
            )
        else:
            section_index = section_indices[section_id]
            if section_index < previous_section_index:
                errors.append(
                    f"range[{index}] returns to narrative section {section_id!r}; "
                    "EDL range sections must follow the selected deliverable's approved section_ids order"
                )
            previous_section_index = section_index
        refs = set(item.get("meaning_ids") or [])
        if isinstance(section_id, str):
            represented_by_section.setdefault(section_id, set()).update(refs)
        if not refs or refs - meaning_ids:
            errors.append(f"range[{index}] has missing/unknown meaning_ids")
        if section_id in narrative_sections:
            misplaced = refs - narrative_sections[section_id]
            if misplaced:
                errors.append(
                    f"range[{index}] assigns meaning_ids {sorted(misplaced)} outside approved "
                    f"narrative section {section_id!r}"
                )
        raw_evidence_ids = item.get("evidence_ids") or []
        if not isinstance(raw_evidence_ids, list) or not raw_evidence_ids:
            errors.append(f"range[{index}] needs evidence_ids")
            raw_evidence_ids = []
        selected_evidence: list[tuple[str, dict[str, Any]]] = []
        selected_meanings: set[str] = set()
        selected_modalities: set[str] = set()
        for evidence_id in raw_evidence_ids:
            record = evidence_map.get(evidence_id) if isinstance(evidence_id, str) else None
            if record is None:
                errors.append(f"range[{index}] references unknown evidence_id {evidence_id!r}")
                continue
            evidence_meaning_id, evidence = record
            selected_evidence.append(record)
            selected_meanings.add(evidence_meaning_id)
            modality = evidence.get("modality")
            if modality in {"speech", "visual"}:
                selected_modalities.add(modality)
            if evidence_meaning_id not in refs:
                errors.append(
                    f"range[{index}] evidence_id {evidence_id!r} belongs to meaning_id "
                    f"{evidence_meaning_id!r}, absent from the range"
                )
            if evidence.get("source") != item.get("source"):
                errors.append(
                    f"range[{index}] evidence_id {evidence_id!r} source={evidence.get('source')!r} "
                    f"does not match range source={item.get('source')!r}"
                )
            if range_bounds is not None:
                try:
                    evidence_start = float(evidence["start"])
                    evidence_end = float(evidence["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                range_start, range_end = range_bounds
                if range_end <= evidence_start or range_start >= evidence_end:
                    errors.append(f"range[{index}] does not intersect evidence_id {evidence_id!r}")
            content_field = "quote" if modality == "speech" else "description"
            if not normalized_quote_contains(evidence.get(content_field), item.get(content_field)):
                errors.append(
                    f"range[{index}] {content_field} does not contain normalized approved "
                    f"{content_field} from "
                    f"evidence_id {evidence_id!r}"
                )
        range_modalities[index] = set(selected_modalities)
        uncovered_meanings = refs - selected_meanings
        if uncovered_meanings:
            errors.append(
                f"range[{index}] meaning_ids have no selected evidence coverage: {sorted(uncovered_meanings)}"
            )
        audio_mode = item.get("audio_mode")
        if audio_mode not in {"source", "mute"}:
            errors.append(f"range[{index}].audio_mode must be 'source' or 'mute'")
        if range_source_record is not None and range_source_record.get("audio") is None:
            if audio_mode != "mute":
                errors.append(
                    f"range[{index}] uses a no-audio source and must set audio_mode='mute'"
                )
        if "speech" in selected_modalities:
            if audio_mode != "source":
                errors.append(
                    f"range[{index}] has speech evidence and must set audio_mode='source'"
                )
        elif audio_mode != "mute":
            errors.append(
                f"range[{index}] has no speech evidence and must set audio_mode='mute'; "
                "visual/B-roll ranges cannot leak unapproved source audio"
            )
        pause_reason = item.get("intentional_pause_reason")
        if pause_reason is not None and not nonempty(pause_reason):
            errors.append(
                f"range[{index}].intentional_pause_reason must be a non-empty explanation"
            )
        if range_bounds is not None and selected_evidence:
            envelope_starts: list[float] = []
            envelope_ends: list[float] = []
            for _, evidence in selected_evidence:
                try:
                    envelope_starts.append(float(evidence["start"]))
                    envelope_ends.append(float(evidence["end"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if envelope_starts and envelope_ends:
                range_start, range_end = range_bounds
                envelope_start = min(envelope_starts)
                envelope_end = max(envelope_ends)
                if (
                    range_start < envelope_start - EVIDENCE_BOUNDARY_PADDING_S - 1e-9
                    or range_end > envelope_end + EVIDENCE_BOUNDARY_PADDING_S + 1e-9
                ):
                    errors.append(
                        f"range[{index}] [{range_start:.3f}, {range_end:.3f}] lies outside selected "
                        f"evidence envelope [{envelope_start:.3f}, {envelope_end:.3f}] with "
                        f"{EVIDENCE_BOUNDARY_PADDING_S:.2f}s boundary padding"
                    )
        if "speech" in selected_modalities:
            if not nonempty(item.get("quote")) or not normalize_quote(item.get("quote")):
                errors.append(f"range[{index}] needs a searchable quote for speech evidence")
            elif range_bounds is not None:
                source_id = item.get("source")
                source_tokens = transcript_tokens.get(source_id) if isinstance(source_id, str) else None
                if source_tokens is None:
                    errors.append(
                        f"range[{index}] speech quote cannot be verified against a valid packed transcript"
                    )
                elif not retained_quote_matches_transcript(
                    item.get("quote"), range_bounds[0], range_bounds[1], source_tokens
                ):
                    errors.append(
                        f"range[{index}] full quote must exactly equal every transcript word retained "
                        f"by source {source_id!r} cut [{range_bounds[0]:.3f}, {range_bounds[1]:.3f}]"
                    )
                else:
                    fillers = vocalized_fillers(normalize_quote(item.get("quote")).split())
                    if fillers:
                        errors.append(
                            f"range[{index}] retains vocalized filler(s) {fillers}; split the cut around "
                            "the hesitation before rendering"
                        )
                    event_fillers = retained_filler_audio_events(
                        range_bounds[0], range_bounds[1], source_tokens
                    )
                    if event_fillers:
                        errors.append(
                            f"range[{index}] retains filler-like audio_event(s) {event_fillers}; "
                            "split the cut around the hesitation before rendering"
                        )
                    if not nonempty(pause_reason):
                        gaps = speech_silence_gaps(
                            range_bounds[0], range_bounds[1], source_tokens
                        )
                        longest: dict[str, tuple[float, float, float]] = {}
                        for gap_kind, gap_start, gap_end in gaps:
                            duration = gap_end - gap_start
                            previous = longest.get(gap_kind)
                            if previous is None or duration > previous[2]:
                                longest[gap_kind] = (gap_start, gap_end, duration)
                        for gap_kind, threshold in (
                            ("boundary", max_boundary_silence_s),
                            ("internal", max_internal_silence_s),
                        ):
                            gap = longest.get(gap_kind)
                            if gap is not None and gap[2] > threshold + 1e-9:
                                errors.append(
                                    f"range[{index}] retains unapproved {gap_kind} silence "
                                    f"{gap[2]:.3f}s at [{gap[0]:.3f}, {gap[1]:.3f}], above "
                                    f"project threshold {threshold:.3f}s; split the cut or add "
                                    "an explicit intentional_pause_reason approved with the EDL"
                                )
        if "visual" in selected_modalities and (
            not nonempty(item.get("description")) or not normalize_quote(item.get("description"))
        ):
            errors.append(f"range[{index}] needs a searchable description for visual evidence")
        if not nonempty(item.get("reason")):
            errors.append(f"range[{index}] needs reason")
        transition = item.get("transition_after", "hard_cut")
        if transition not in ALLOWED_TRANSITIONS:
            errors.append(f"range[{index}] has disallowed transition {transition!r}")
        if transition != "hard_cut" and not nonempty(item.get("transition_reason")):
            errors.append(f"range[{index}] transition {transition} needs transition_reason")
        if transition != "hard_cut":
            errors.append(
                f"range[{index}] requests {transition}, but the canonical renderer currently "
                "implements verified hard cuts only; use a full-frame overlay/card or a separately tested renderer"
            )

    output_intervals = renderer_output_intervals(ranges, output.get("fps"), errors)
    validate_subtitle_semantic_text(
        edit_dir, edl, ranges, range_modalities, output_intervals, errors
    )
    validate_overlay_semantics(
        edit_dir,
        edl,
        plan,
        scoped_section_ids,
        output_intervals,
        output.get("fps"),
        errors,
    )

    for section_id, expected_meanings in scoped_narrative_sections.items():
        if section_id not in represented_by_section:
            errors.append(
                f"deliverable-scoped narrative section {section_id!r} is absent from EDL ranges"
            )
        missing_meanings = expected_meanings - represented_by_section.get(section_id, set())
        if missing_meanings:
            errors.append(
                f"deliverable-scoped narrative section {section_id!r} drops meaning_ids from EDL ranges: "
                f"{sorted(missing_meanings)}"
            )

    if ranges and isinstance(ranges[0], dict):
        first_section_id = scoped_section_ids[0] if scoped_section_ids else None
        if ranges[0].get("section_id") != first_section_id:
            errors.append(
                f"first EDL range section_id={ranges[0].get('section_id')!r} differs from "
                f"deliverable opening section {first_section_id!r}"
            )
        first_refs = set(ranges[0].get("meaning_ids") or [])
        missing_hook_meanings = selected_hook_meanings - first_refs
        if missing_hook_meanings:
            errors.append(
                f"first EDL range drops selected hook meaning_ids: {sorted(missing_hook_meanings)}"
            )
    if ranges and isinstance(ranges[-1], dict):
        final_range = ranges[-1]
        if final_range.get("section_id") != ending_section_id:
            errors.append(
                f"final EDL range section_id={final_range.get('section_id')!r} differs from approved "
                f"deliverable ending_section_id={ending_section_id!r}"
            )

    if selected_deliverable is not None:
        try:
            target_duration_s = float(selected_deliverable["target_duration_s"])
        except (KeyError, TypeError, ValueError):
            target_duration_s = 0.0
        if math.isfinite(target_duration_s) and target_duration_s > 0 and retained_duration_s > 0:
            tolerance_s = max(2.0, target_duration_s * 0.05)
            if abs(retained_duration_s - target_duration_s) > tolerance_s:
                errors.append(
                    f"EDL retained duration {retained_duration_s:.3f}s differs materially from approved "
                    f"deliverable {deliverable_id!r} target {target_duration_s:.3f}s "
                    f"(allowed tolerance {tolerance_s:.3f}s)"
                )

    presenter = (project.get("presenter") or {}).get("mode", "auto")
    layouts = edl.get("layout_plan") or []
    if presenter != "auto" and presenter not in RESOLVED_PRESENTER:
        errors.append(f"project presenter.mode is invalid: {presenter}")
    if not isinstance(layouts, list) or not layouts:
        errors.append("EDL has no resolved layout_plan")
        layouts = []
    for index, layout in enumerate(layouts):
        if not isinstance(layout, dict) or layout.get("output_shape") not in RESOLVED_PRESENTER:
            errors.append(f"layout_plan[{index}] has unresolved presenter geometry")
            continue
        composition = layout.get("composition", "preserve_source")
        if composition not in LAYOUT_COMPOSITIONS:
            errors.append(f"layout_plan[{index}] has invalid composition {composition!r}")
        output_shape = layout.get("output_shape")
        source_class = layout.get("source_class")
        if source_class == "unknown":
            errors.append(f"layout_plan[{index}].source_class must be resolved before EDL/render")
        if composition == "preserve_source":
            allowed_shapes = PRESERVE_SOURCE_SHAPES.get(source_class, set())
            if output_shape not in allowed_shapes:
                errors.append(
                    f"layout_plan[{index}] preserve_source output_shape={output_shape!r} does not match "
                    f"source_class={source_class!r}; expected one of {sorted(allowed_shapes)}"
                )
        elif composition in COMPOSITION_OUTPUT_SHAPES:
            allowed_shapes = COMPOSITION_OUTPUT_SHAPES[composition]
            if output_shape not in allowed_shapes:
                errors.append(
                    f"layout_plan[{index}] composition={composition!r} requires output_shape in "
                    f"{sorted(allowed_shapes)}, got {output_shape!r}"
                )
        ignored_fields: list[str] = []
        if composition == "preserve_source":
            ignored_fields = [
                "presenter_roi",
                "screen_roi",
                "important_screen_roi",
                "presenter_box",
                "screen_box",
                "caption_safe_box",
            ]
        elif composition == "presenter_only":
            ignored_fields = ["screen_roi", "important_screen_roi", "screen_box"]
            if output_shape == "full_frame":
                ignored_fields.append("presenter_box")
        elif composition == "screen_only":
            ignored_fields = ["presenter_roi", "presenter_box"]
        for field in ignored_fields:
            if layout.get(field) is not None:
                errors.append(
                    f"layout_plan[{index}].{field} is ignored by composition={composition!r}; "
                    "remove it or choose a composition that materializes it"
                )
        if output_shape == "circle" and not (
            source_class in {"already_circular", "isolated_subject"}
            or layout.get("user_override") is True
        ):
            errors.append(f"layout_plan[{index}] violates circle policy")
        if (
            presenter in {"rectangle", "circle"}
            and composition in {"presenter_with_screen", "presenter_only"}
            and output_shape != presenter
        ):
            errors.append(f"layout_plan[{index}] output_shape conflicts with project presenter.mode={presenter}")
        if presenter in {"hidden", "none"} and composition != "screen_only":
            errors.append(
                f"layout_plan[{index}] can show a presenter while project presenter.mode={presenter}"
            )
        if presenter == "full_frame" and not (
            (composition == "preserve_source" and output_shape == "full_frame")
            or (composition == "presenter_only" and output_shape in {"full_frame", "rectangle"})
        ):
            errors.append(f"layout_plan[{index}] conflicts with project presenter.mode=full_frame")
        required_rois: list[str] = []
        required_boxes: list[str] = []
        if composition == "presenter_with_screen":
            required_rois = ["screen_roi", "presenter_roi"]
            required_boxes = ["presenter_box"]
        elif composition == "screen_only":
            required_rois = ["screen_roi"]
        elif composition == "presenter_only":
            required_rois = ["presenter_roi"]
            if output_shape == "circle":
                required_boxes = ["presenter_box"]
        for field in required_rois:
            if not valid_normalized_roi(layout.get(field)):
                errors.append(f"layout_plan[{index}].{field} has invalid bounds")
        for field in required_boxes:
            if output_box_dimensions(layout.get(field), output or {}) is None:
                errors.append(f"layout_plan[{index}].{field} has invalid bounds")
        for field in ("screen_box", "presenter_box"):
            if layout.get(field) is not None and output_box_dimensions(layout.get(field), output or {}) is None:
                errors.append(f"layout_plan[{index}].{field} has invalid output bounds")
        screen_box_value = layout.get("screen_box")
        presenter_box_value = layout.get("presenter_box")
        if isinstance(screen_box_value, dict) and isinstance(presenter_box_value, dict):
            try:
                screen_z = int(screen_box_value.get("z_index", 0))
                presenter_z = int(presenter_box_value.get("z_index", 1))
            except (TypeError, ValueError):
                errors.append(f"layout_plan[{index}] has invalid z_index")
            else:
                if composition == "presenter_with_screen" and presenter_z <= screen_z:
                    errors.append(f"layout_plan[{index}] presenter_box must be above screen_box")
        important = normalized_roi_values(layout.get("important_screen_roi")) if layout.get("important_screen_roi") is not None else None
        screen = normalized_roi_values(layout.get("screen_roi")) if layout.get("screen_roi") is not None else None
        if layout.get("important_screen_roi") is not None and important is None:
            errors.append(f"layout_plan[{index}].important_screen_roi has invalid bounds")
        if important and screen:
            ix, iy, iw, ih = important
            sx, sy, sw, sh = screen
            if ix < sx - 1e-6 or iy < sy - 1e-6 or ix + iw > sx + sw + 1e-6 or iy + ih > sy + sh + 1e-6:
                errors.append(f"layout_plan[{index}].important_screen_roi is outside screen_roi")
            elif composition in {"presenter_with_screen", "screen_only"}:
                output_width = finite_non_negative_time(output.get("width"))
                output_height = finite_non_negative_time(output.get("height"))
                if output_width is None or output_height is None or output_width <= 0 or output_height <= 0:
                    errors.append(
                        f"layout_plan[{index}].important_screen_roi cannot be mapped without valid output dimensions"
                    )
                else:
                    resolved_screen_box = (
                        output_box_values(layout.get("screen_box"), output)
                        if layout.get("screen_box") is not None
                        else (0.0, 0.0, output_width, output_height)
                    )
                    source_dimensions = source_display_dimensions(
                        source_records.get(layout.get("source"))
                    )
                    if resolved_screen_box is None:
                        errors.append(
                            f"layout_plan[{index}].important_screen_roi cannot be mapped through invalid screen_box"
                        )
                    elif source_dimensions is None:
                        errors.append(
                            f"layout_plan[{index}].important_screen_roi requires source video display dimensions"
                        )
                    else:
                        mapped_important = mapped_important_screen_box(
                            important=important,
                            screen=screen,
                            screen_box=resolved_screen_box,
                            source_dimensions=source_dimensions,
                        )
                        presenter_pixels = (
                            output_box_values(layout.get("presenter_box"), output)
                            if layout.get("presenter_box") is not None
                            else None
                        )
                        if presenter_pixels is not None and rectangles_intersect(
                            mapped_important, presenter_pixels
                        ):
                            errors.append(
                                f"layout_plan[{index}] presenter_box covers mapped important_screen_roi; "
                                "move or shrink the presenter, or enlarge the screen"
                            )
                        safe_rect = platform_safe_rect(project, output)
                        if safe_rect is None:
                            errors.append(
                                f"layout_plan[{index}].important_screen_roi cannot be checked against platform safe area"
                            )
                        elif not rectangle_is_inside(mapped_important, safe_rect):
                            errors.append(
                                f"layout_plan[{index}] mapped important_screen_roi leaves the platform safe area; "
                                "enlarge/reposition the screen or choose a full-screen interval"
                            )
        if layout.get("caption_safe_box") is not None:
            errors.append(
                f"layout_plan[{index}].caption_safe_box is not consumed by the canonical renderer; "
                "encode caption placement in a reviewed ASS file instead"
            )
        box_dimensions = output_box_dimensions(layout.get("presenter_box"), output or {})
        if output_shape == "circle" and box_dimensions:
            box_width, box_height = box_dimensions
            if abs(box_width - box_height) > 2.1:
                errors.append(f"layout_plan[{index}].presenter_box must be square for circle output")
    for range_index, item in enumerate(ranges):
        try:
            covered = sum(
                isinstance(layout, dict)
                and layout.get("source") == item.get("source")
                and float(layout.get("start", -1)) <= float(item.get("start", 0)) + 1e-6
                and float(layout.get("end", -1)) >= float(item.get("end", 0)) - 1e-6
                for layout in layouts
            )
        except (TypeError, ValueError):
            covered = 0
        if covered != 1:
            errors.append(f"range[{range_index}] must be covered by exactly one resolved layout entry; found {covered}")


def validate_asset_records(
    records: Any,
    errors: list[str],
    label: str,
    *,
    require_provenance: bool = False,
) -> None:
    if not isinstance(records, list):
        errors.append(f"preview approval {label} is invalid")
        return
    for index, item in enumerate(records):
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            errors.append(f"preview approval {label}[{index}] is incomplete")
            continue
        path = Path(str(item["path"])).expanduser().resolve()
        if not path.is_file() or sha256(path) != item["sha256"]:
            errors.append(f"approved {label} changed or is missing: {path}")
        provenance = item.get("provenance")
        if provenance is None:
            if require_provenance:
                errors.append(
                    f"preview approval {label}[{index}] has no semantic provenance"
                )
        else:
            if (
                not isinstance(provenance, dict)
                or not provenance.get("path")
                or not provenance.get("sha256")
            ):
                errors.append(
                    f"preview approval {label}[{index}].provenance is incomplete"
                )
                continue
            provenance_path = Path(str(provenance["path"])).expanduser().resolve()
            if (
                not provenance_path.is_file()
                or sha256(provenance_path) != provenance["sha256"]
            ):
                errors.append(
                    f"approved {label} provenance changed or is missing: {provenance_path}"
                )


def validate_preview_approval(
    edit_dir: Path,
    plan_hash: str | None,
    edl_path: Path,
    edl: dict[str, Any],
    manifest_sources: dict[str, Path],
    errors: list[str],
) -> None:
    deliverable_id = edl.get("deliverable_id")
    try:
        deliverable_artifact_key = artifact_key(deliverable_id)
    except ProvenanceError as exc:
        errors.append(str(exc))
        return
    approval_path = edit_dir / preview_approval_name(deliverable_id)
    if not approval_path.is_file():
        errors.append(f"missing {approval_path.name}")
        return
    approval = load(approval_path, errors)
    if approval.get("version") != 2:
        errors.append("preview approval version must be 2")
    if approval.get("deliverable_id") != deliverable_id:
        errors.append("preview approval belongs to a different EDL deliverable")
    if approval.get("artifact_key") != deliverable_artifact_key:
        errors.append("preview approval has a non-canonical artifact key")
    if not nonempty(approval.get("user_quote")):
        errors.append("preview approval has no exact user quote")
    preview_name = str(approval.get("preview_file") or "")
    preview_raw = Path(preview_name).expanduser()
    preview = (
        preview_raw.resolve()
        if preview_raw.is_absolute()
        else (edit_dir / preview_raw).resolve()
    )
    try:
        preview.relative_to(edit_dir)
    except ValueError:
        errors.append(f"approved preview escapes the edit directory: {preview}")
    if not preview.is_file():
        errors.append(f"approved preview not found: {preview_name}")
        return
    if approval.get("preview_sha256") != sha256(preview):
        errors.append("preview changed after user approval")
    manifest_raw = Path(str(approval.get("render_manifest_file") or "")).expanduser()
    manifest_path = (
        manifest_raw.resolve()
        if manifest_raw.is_absolute()
        else (edit_dir / manifest_raw).resolve()
    )
    canonical_manifest = (edit_dir / render_manifest_name(deliverable_id, "preview")).resolve()
    if manifest_path != canonical_manifest:
        errors.append(
            f"preview approval must reference canonical manifest {canonical_manifest.name}"
        )
    if not manifest_path.is_file() or approval.get("render_manifest_sha256") != sha256(manifest_path):
        errors.append("approved preview render manifest changed or is missing")
        manifest: dict[str, Any] = {}
    else:
        manifest = load(manifest_path, errors)
    if approval.get("renderer") != EXPECTED_RENDERER or manifest.get("renderer") != EXPECTED_RENDERER:
        errors.append("approved preview uses a different renderer version")
    if manifest.get("mode") != "preview":
        errors.append("approved preview manifest is not a preview render")
    if manifest.get("deliverable_id") != deliverable_id:
        errors.append("approved preview manifest belongs to a different deliverable")
    if manifest.get("artifact_key") != deliverable_artifact_key:
        errors.append("approved preview manifest has a non-canonical artifact key")
    try:
        current_identity = renderer_identity()
    except ProvenanceError as exc:
        errors.append(f"cannot establish current renderer identity: {exc}")
        current_identity = {}
    if approval.get("renderer_identity") != current_identity:
        errors.append("renderer implementation or toolchain changed after preview approval")
    if manifest.get("renderer_identity") != current_identity:
        errors.append("approved preview manifest renderer identity is no longer current")
    manifest_output = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    manifest_preview_raw = Path(str(manifest_output.get("path") or "")).expanduser()
    manifest_preview = (
        manifest_preview_raw.resolve()
        if manifest_preview_raw.is_absolute()
        else (edit_dir / manifest_preview_raw).resolve()
    )
    if manifest_preview != preview:
        errors.append("approved preview manifest output path differs from the approved preview")
    if manifest_output.get("sha256") != approval.get("preview_sha256"):
        errors.append("approved preview manifest output hash differs from preview approval")
    locked_sources = manifest.get("source_fingerprints")
    if not isinstance(locked_sources, dict) or not locked_sources:
        errors.append("approved preview manifest has no source fingerprints")
    else:
        if {str(item) for item in locked_sources} != {str(item) for item in manifest_sources}:
            errors.append("approved preview source IDs differ from the current source manifest")
        for source_id, locked_digest in locked_sources.items():
            source_path = manifest_sources.get(str(source_id))
            if source_path is None or not source_path.is_file():
                errors.append(f"approved preview source {source_id!r} is absent from the current manifest")
                continue
            if sha256(source_path) != locked_digest:
                errors.append(f"source {source_id!r} changed after preview approval")
    expected_subtitle_mode = str(edl.get("subtitle_mode") or ("burned" if edl.get("subtitles") else "none"))
    if manifest.get("subtitle_mode") != expected_subtitle_mode:
        errors.append("approved preview subtitle mode differs from the final EDL")
    if not edl_path.is_file() or approval.get("edl_sha256") != sha256(edl_path):
        errors.append("EDL changed after preview approval")
    if manifest.get("edl_sha256") != approval.get("edl_sha256"):
        errors.append("preview manifest EDL hash differs from preview approval")
    manifest_edl_raw = Path(str(manifest.get("edl") or "")).expanduser()
    manifest_edl = (
        manifest_edl_raw.resolve()
        if manifest_edl_raw.is_absolute()
        else (edit_dir / manifest_edl_raw).resolve()
    )
    if manifest_edl != edl_path:
        errors.append("preview manifest references a different EDL")
    project_path = edit_dir / "project.json"
    if not project_path.is_file() or approval.get("project_sha256") != sha256(project_path):
        errors.append("project configuration changed after preview approval")
    if manifest.get("project_sha256") != approval.get("project_sha256"):
        errors.append("preview manifest project hash differs from preview approval")
    if approval.get("approval_plan_sha256") != plan_hash:
        errors.append("semantic plan approval differs from the approved preview")
    if manifest.get("approval_plan_sha256") != approval.get("approval_plan_sha256"):
        errors.append("preview manifest semantic-plan hash differs from preview approval")
    if manifest.get("visual_assets") != (approval.get("visual_assets") or []):
        errors.append("preview approval visual assets differ from its render manifest")
    if manifest.get("audio_assets") != (approval.get("audio_assets") or []):
        errors.append("preview approval audio assets differ from its render manifest")
    validate_asset_records(
        approval.get("visual_assets") or [],
        errors,
        "visual asset",
        require_provenance=True,
    )
    validate_asset_records(approval.get("audio_assets") or [], errors, "audio asset")
    subtitle = approval.get("subtitle_asset")
    if manifest.get("subtitle_asset") != subtitle:
        errors.append("preview approval subtitle asset differs from its render manifest")
    if subtitle:
        validate_asset_records([subtitle], errors, "subtitle asset")
    font_assets = approval.get("font_assets")
    if not isinstance(font_assets, list):
        errors.append("preview approval font_assets is invalid")
        font_assets = []
    validate_asset_records(font_assets, errors, "font asset")
    if manifest.get("font_assets") != font_assets:
        errors.append("preview approval font assets differ from its render manifest")
    if expected_subtitle_mode == "burned":
        if not isinstance(subtitle, dict):
            errors.append("burned subtitles have no approved subtitle asset")
        else:
            subtitle_path = Path(str(subtitle.get("path") or "")).expanduser().resolve()
            try:
                current_fonts = resolve_subtitle_fonts(subtitle_path)
            except (OSError, ProvenanceError) as exc:
                errors.append(f"cannot resolve current burned-subtitle fonts: {exc}")
            else:
                if not current_fonts:
                    errors.append("burned subtitles have no resolved font provenance")
                if current_fonts != font_assets:
                    errors.append("burned-subtitle font resolution changed after preview approval")
    elif font_assets:
        errors.append("non-burned preview approval must not declare font assets")
    qa_value = approval.get("qa_report")
    if not qa_value:
        errors.append("preview approval has no QA report")
    else:
        qa_path = Path(str(qa_value)).expanduser().resolve()
        if not qa_path.is_file() or approval.get("qa_report_sha256") != sha256(qa_path):
            errors.append("approved preview QA report changed or is missing")
        else:
            qa = load(qa_path, errors)
            if qa.get("status") != "PASS":
                errors.append("approved preview QA status is not PASS")
            if qa.get("render_manifest_sha256") != approval.get("render_manifest_sha256"):
                errors.append("approved QA report belongs to different render-manifest bytes")
            if qa.get("output_sha256") != approval.get("preview_sha256"):
                errors.append("approved QA report belongs to a different preview file")
            if qa.get("deliverable_id") != deliverable_id:
                errors.append("approved QA report belongs to a different deliverable")
            if qa.get("artifact_key") != deliverable_artifact_key:
                errors.append("approved QA report has a non-canonical artifact key")
            qa_manifest = Path(str(qa.get("manifest") or "")).expanduser().resolve()
            if qa_manifest != manifest_path:
                errors.append("approved QA report references a different render manifest")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SPRUT editorial and render gates")
    parser.add_argument("--edit-dir", required=True, type=Path)
    parser.add_argument(
        "--phase",
        choices=("analysis", "asset", "edl", "render", "final"),
        required=True,
        help="asset validates approved source truth without requiring an EDL",
    )
    parser.add_argument(
        "--edl",
        type=Path,
        help="required for edl/render/final; pass the exact edl_<deliverable>.json",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.phase in {"edl", "render", "final"} and args.edl is None:
        parser.error("--edl is required for phase edl, render, or final")

    edit_dir = args.edit_dir.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    project = load(edit_dir / "project.json", errors)
    if project.get("paid_api_allowlist") != ["elevenlabs"]:
        errors.append("paid_api_allowlist must be exactly ['elevenlabs']")
    manifest_sources, source_records = validate_sources(edit_dir, project, errors)

    meaning_ids: set[str] = set()
    narrative_sections: dict[str, set[str]] = {}
    deliverables: dict[str, dict[str, Any]] = {}
    transcript_tokens: dict[str, list[TranscriptEntry]] = {}
    plan_path = edit_dir / "semantic_plan.json"
    plan_hash: str | None = None
    plan: dict[str, Any] = {}
    if args.phase != "analysis":
        plan = load(plan_path, errors)
        validate_schema(plan, "semantic-plan.schema.json", "semantic_plan", errors)
        meaning_ids, narrative_sections, deliverables = validate_plan(plan, errors, source_records)
        transcript_tokens = validate_packed_transcripts(edit_dir, source_records, errors)
        validate_plan_evidence_transcripts(plan, transcript_tokens, source_records, errors)
        validate_source_mode(project, plan, errors)
        plan_hash = validate_approval(edit_dir, plan_path, errors)
    edl_path = (
        args.edl.expanduser().resolve()
        if args.edl is not None
        else (edit_dir / "edl_unused.json").resolve()
    )
    edl: dict[str, Any] = {}
    if args.phase in {"edl", "render", "final"}:
        edl = load(edl_path, errors)
        validate_schema(edl, "edl.schema.json", "EDL", errors)
        validate_edl(
            edit_dir,
            edl,
            plan_hash,
            meaning_ids,
            narrative_sections,
            deliverables,
            plan,
            manifest_sources,
            source_records,
            transcript_tokens,
            project,
            errors,
        )
    if args.phase == "final":
        validate_preview_approval(edit_dir, plan_hash, edl_path, edl, manifest_sources, errors)

    report = {
        "phase": args.phase,
        "status": "PASS" if not errors else "FAIL",
        "checked_sources": len(manifest_sources),
        "errors": errors,
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"SPRUT gate {args.phase}: {report['status']}")
        for message in errors:
            print(f"ERROR: {message}")
        for message in warnings:
            print(f"warning: {message}")
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ProvenanceError, ValueError, json.JSONDecodeError) as exc:
        print(f"validate_gate: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
