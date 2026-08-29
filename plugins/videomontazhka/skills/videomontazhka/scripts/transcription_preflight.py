#!/usr/bin/env python3
"""Create and validate a source-bound transcription cost/privacy preflight."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from transcription_safety import (
    AttemptLedgerError,
    TranscriptionLockError,
    TranscriptionPathError,
    attempt_key,
    attempt_source_identity,
    assert_external_attempt_available,
    canonical_json_sha256,
    contained_child,
    consumed_attempt_keys,
    project_transcription_lock,
    sha256_file,
    validate_source_id,
    validate_approval_anchor,
    write_json_atomic,
)


PROVIDER = "elevenlabs"
MODEL_ID = "scribe_v1"
PREFLIGHT_NAME = "transcription_preflight.json"
APPROVAL_NAME = "transcription_approval.json"
UPLOAD_DISCLOSURE = (
    "Each uncached audio source listed in this preflight will be extracted to a temporary "
    "mono WAV and uploaded to ElevenLabs for speech-to-text processing. Cached sources are "
    "not uploaded again. ElevenLabs usage may be billable under the user's account."
)


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceAssessment:
    source_id: str
    path: Path
    manifest_path: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    duration_s: float
    outside_project: bool
    cached: bool
    cache_reason: str


@dataclass(frozen=True)
class Assessment:
    videos_dir: Path
    edit_dir: Path
    manifest_path: Path
    manifest_sha256: str
    provider: str
    model_id: str
    language: str | None
    num_speakers: int | None
    sources: tuple[SourceAssessment, ...]
    visual_only_count: int
    request: dict[str, Any]
    request_sha256: str

    @property
    def billable_sources(self) -> tuple[SourceAssessment, ...]:
        return tuple(source for source in self.sources if not source.cached)

    @property
    def billable_seconds(self) -> float:
        return sum(source.duration_s for source in self.billable_sources)

    @property
    def billable_minutes(self) -> float:
        return self.billable_seconds / 60.0


@dataclass(frozen=True)
class ValidatedTranscriptionRequest:
    assessment: Assessment
    preflight_path: Path
    approval_path: Path | None
    approved_max_billable_minutes: float
    approval_id: str | None
    approval_nonce: str | None
    approved_upload_source_ids: tuple[str, ...]


DurationProbe = Callable[[Path], float]


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PreflightError(f"{label} not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreflightError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"{label} JSON root must be an object: {path}")
    return value


def finite_duration(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PreflightError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise PreflightError(f"{label} must be finite and positive")
    return number


def finite_cap(value: Any, label: str = "max billable minutes") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PreflightError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise PreflightError(f"{label} must be finite and non-negative")
    return number


def probe_audio_duration(path: Path) -> float:
    """Measure current audio duration with ffprobe before estimating uploads."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip()[-1000:]
        raise PreflightError(f"ffprobe failed for {path.name}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError(f"ffprobe returned invalid JSON for {path.name}") from exc
    streams = payload.get("streams") or []
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    if audio_stream is None:
        raise PreflightError(
            f"manifest reports audio but ffprobe found no audio stream: {path.name}; re-initialize"
        )
    raw_duration = audio_stream.get("duration")
    if raw_duration in (None, "N/A", ""):
        raw_duration = (payload.get("format") or {}).get("duration")
    return finite_duration(raw_duration, f"ffprobe duration for {path.name}")


def resolve_manifest(videos_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = videos_dir.expanduser().resolve()
    if not root.is_dir():
        raise PreflightError(f"videos directory not found: {root}")
    edit_dir = root / "edit"
    project = load_json_object(edit_dir / "project.json", "project")
    manifest_value = project.get("source_manifest") or "source_manifest.json"
    if not isinstance(manifest_value, str) or not manifest_value.strip():
        raise PreflightError("project source_manifest must be a non-empty path")
    raw_manifest = Path(manifest_value).expanduser()
    manifest_path = (
        raw_manifest.resolve()
        if raw_manifest.is_absolute()
        else (edit_dir / raw_manifest).resolve()
    )
    manifest = load_json_object(manifest_path, "source manifest")
    return edit_dir.resolve(), manifest_path, manifest


def transcript_identity(
    source: SourceAssessment,
    *,
    language: str | None,
    num_speakers: int | None,
) -> dict[str, Any]:
    """Mirror the stable cache identity used by transcribe_safe.py."""
    return {
        "source": str(source.path.resolve()),
        "source_sha256": source.sha256,
        "source_size": source.size_bytes,
        "source_mtime_ns": source.mtime_ns,
        "model_id": MODEL_ID,
        "language": language,
        "num_speakers": num_speakers,
        "timestamps_granularity": "word",
        "diarize": True,
        "tag_audio_events": True,
    }


def cache_state(
    edit_dir: Path,
    source: SourceAssessment,
    *,
    language: str | None,
    num_speakers: int | None,
) -> tuple[bool, str]:
    transcripts = contained_child(edit_dir, "transcripts", label="transcript directory")
    metadata_dir = contained_child(
        transcripts,
        ".metadata",
        label="transcript metadata directory",
    )
    transcript = contained_child(
        transcripts,
        f"{source.source_id}.json",
        label="transcript output",
    )
    metadata = contained_child(
        metadata_dir,
        f"{source.source_id}.json",
        label="transcript metadata",
    )
    legacy = contained_child(
        transcripts,
        f"{source.source_id}.meta.json",
        label="legacy transcript metadata",
    )
    partial = contained_child(
        transcripts,
        f"{source.source_id}.part.json",
        label="recoverable transcript response",
    )
    selected_metadata = metadata if metadata.is_file() else legacy
    expected_identity = transcript_identity(
        source,
        language=language,
        num_speakers=num_speakers,
    )
    transcript_value: dict[str, Any] | None = None
    if transcript.is_file():
        try:
            transcript_value = load_json_object(transcript, "cached transcript")
        except PreflightError:
            transcript_value = None
    if transcript_value is not None and isinstance(transcript_value.get("words"), list):
        if selected_metadata.is_file():
            try:
                metadata_value = load_json_object(
                    selected_metadata,
                    "cached transcript metadata",
                )
            except PreflightError:
                metadata_value = {}
            if metadata_value.get("identity") == expected_identity:
                return True, "hash-bound word-level cache hit"
        marker = transcript_value.get("_videomontazhka_cache")
        if (
            isinstance(marker, dict)
            and marker.get("version") == 1
            and marker.get("identity") == expected_identity
        ):
            return True, "recoverable completed response; metadata rebuild required"
    if partial.is_file():
        try:
            partial_value = load_json_object(partial, "recoverable transcript response")
        except PreflightError:
            partial_value = {}
        marker = partial_value.get("_videomontazhka_cache")
        if (
            isinstance(partial_value.get("words"), list)
            and isinstance(marker, dict)
            and marker.get("version") == 1
            and marker.get("identity") == expected_identity
        ):
            return True, "recoverable post-response partial; no re-upload needed"
    return False, "missing or invalid hash-bound word-level cache"


def request_payload(
    *,
    manifest_sha256: str,
    provider: str,
    model_id: str,
    language: str | None,
    num_speakers: int | None,
    sources: list[SourceAssessment],
) -> dict[str, Any]:
    return {
        "version": 1,
        "source_manifest_sha256": manifest_sha256,
        "provider": provider,
        "model_id": model_id,
        "language": language,
        "num_speakers": num_speakers,
        "sources": [
            {
                "id": source.source_id,
                "manifest_path": source.manifest_path,
                "resolved_path": str(source.path),
                "outside_project": source.outside_project,
                "sha256": source.sha256,
                "size_bytes": source.size_bytes,
                "mtime_ns": source.mtime_ns,
                "duration_s": round(source.duration_s, 6),
                "cached": source.cached,
                "will_upload": not source.cached,
                "cache_reason": source.cache_reason,
            }
            for source in sources
        ],
    }


def build_assessment(
    videos_dir: Path,
    *,
    provider: str = PROVIDER,
    model_id: str = MODEL_ID,
    language: str | None = None,
    num_speakers: int | None = None,
    duration_probe: DurationProbe = probe_audio_duration,
) -> Assessment:
    if provider != PROVIDER:
        raise PreflightError(f"unsupported transcription provider: {provider!r}")
    if model_id != MODEL_ID:
        raise PreflightError(f"unsupported {provider} model: {model_id!r}")
    if num_speakers is not None and not 1 <= num_speakers <= 32:
        raise PreflightError("num_speakers must be between 1 and 32")

    edit_dir, manifest_path, manifest = resolve_manifest(videos_dir)
    entries = manifest.get("sources")
    if not isinstance(entries, list) or not entries:
        raise PreflightError("source manifest is empty")
    raw_root = Path(str(manifest.get("root") or "..")).expanduser()
    source_root = (
        raw_root.resolve()
        if raw_root.is_absolute()
        else (manifest_path.parent / raw_root).resolve()
    )
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    assessed: list[SourceAssessment] = []
    visual_only_count = 0
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise PreflightError(f"source manifest entry {index} is invalid")
        source_id = validate_source_id(
            item.get("id"),
            label=f"source manifest entry {index} id",
        )
        if source_id in seen_ids:
            raise PreflightError(f"duplicate source id in manifest: {source_id!r}")
        seen_ids.add(source_id)
        raw_path_value = item.get("path")
        if not isinstance(raw_path_value, str) or not raw_path_value:
            raise PreflightError(f"source manifest entry {index} has no path")
        raw_path = Path(raw_path_value).expanduser()
        source_path = (raw_path if raw_path.is_absolute() else source_root / raw_path).resolve()
        if source_path in seen_paths:
            raise PreflightError(f"source manifest maps the same file more than once: {source_path}")
        seen_paths.add(source_path)
        if not source_path.is_file():
            raise PreflightError(f"source not found: {source_path}")
        if item.get("audio") is None:
            visual_only_count += 1
            continue

        expected_sha = item.get("sha256")
        if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
            raise PreflightError(f"source manifest entry {index} has no valid sha256")
        expected_size = item.get("size_bytes")
        expected_mtime = item.get("mtime_ns")
        if type(expected_size) is not int or type(expected_mtime) is not int:
            raise PreflightError(f"source manifest entry {index} has invalid size/mtime identity")
        stat_info = source_path.stat()
        if stat_info.st_size != expected_size or stat_info.st_mtime_ns != expected_mtime:
            raise PreflightError(
                f"source changed since ingest: {source_path.name}; re-initialize before transcription"
            )
        current_sha = sha256_file(source_path)
        if current_sha != expected_sha:
            raise PreflightError(
                f"source content hash changed since ingest: {source_path.name}; re-initialize"
            )
        duration_s = duration_probe(source_path)
        manifest_duration = finite_duration(
            item.get("duration_s"),
            f"manifest duration for {source_path.name}",
        )
        tolerance = max(2.0, manifest_duration * 0.01)
        if abs(duration_s - manifest_duration) > tolerance:
            raise PreflightError(
                f"ffprobe duration for {source_path.name} differs from ingest by more than "
                f"{tolerance:.3f}s; re-initialize"
            )
        initial = SourceAssessment(
            source_id=source_id,
            path=source_path,
            manifest_path=raw_path_value,
            sha256=current_sha,
            size_bytes=stat_info.st_size,
            mtime_ns=stat_info.st_mtime_ns,
            duration_s=duration_s,
            outside_project=not source_path.is_relative_to(
                videos_dir.expanduser().resolve()
            ),
            cached=False,
            cache_reason="not checked",
        )
        cached, reason = cache_state(
            edit_dir,
            initial,
            language=language,
            num_speakers=num_speakers,
        )
        assessed.append(
            SourceAssessment(
                **{
                    **initial.__dict__,
                    "cached": cached,
                    "cache_reason": reason,
                }
            )
        )

    manifest_digest = sha256_file(manifest_path)
    request = request_payload(
        manifest_sha256=manifest_digest,
        provider=provider,
        model_id=model_id,
        language=language,
        num_speakers=num_speakers,
        sources=assessed,
    )
    return Assessment(
        videos_dir=videos_dir.expanduser().resolve(),
        edit_dir=edit_dir,
        manifest_path=manifest_path,
        manifest_sha256=manifest_digest,
        provider=provider,
        model_id=model_id,
        language=language,
        num_speakers=num_speakers,
        sources=tuple(assessed),
        visual_only_count=visual_only_count,
        request=request,
        request_sha256=canonical_json_sha256(request),
    )


def artifact_payload(assessment: Assessment) -> dict[str, Any]:
    rounded_up_minutes = math.ceil(assessment.billable_minutes * 1_000_000) / 1_000_000
    return {
        "version": 1,
        "schema_version": "1.1.0",
        "type": "transcription_preflight",
        "status": "awaiting_explicit_approval",
        "preflight_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": assessment.provider,
        "model_id": assessment.model_id,
        "language": assessment.language,
        "num_speakers": assessment.num_speakers,
        "source_manifest": {
            "path": str(assessment.manifest_path),
            "sha256": assessment.manifest_sha256,
        },
        "request": assessment.request,
        "request_sha256": assessment.request_sha256,
        "privacy": {
            "external_upload": True,
            "disclosure": UPLOAD_DISCLOSURE,
            "disclosure_sha256": canonical_json_sha256(UPLOAD_DISCLOSURE),
        },
        "usage_estimate": {
            "unit": "source_audio_minute",
            "currency_quote_included": False,
            "billable_source_ids": [source.source_id for source in assessment.billable_sources],
            "cached_source_ids": [source.source_id for source in assessment.sources if source.cached],
            "billable_audio_seconds": round(assessment.billable_seconds, 6),
            "estimated_billable_minutes": rounded_up_minutes,
            "note": (
                "This is source audio duration, not a monetary quote. Provider-side rounding, "
                "plan pricing, taxes, and account credits are not inferred."
            ),
        },
        "inventory": [
            {
                "id": source.source_id,
                "filename": source.path.name,
                "resolved_path": str(source.path),
                "outside_project": source.outside_project,
                "duration_s": round(source.duration_s, 6),
                "sha256": source.sha256,
                "cached": source.cached,
                "will_upload": not source.cached,
                "cache_reason": source.cache_reason,
            }
            for source in assessment.sources
        ],
        "visual_only_sources_skipped": assessment.visual_only_count,
        "next_action": (
            "Ask the user to acknowledge the upload disclosure and approve a numeric maximum "
            "billable-minute cap, then run record_transcription_approval.py."
        ),
    }


def approval_binding_payload(approval: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "version",
        "schema_version",
        "type",
        "status",
        "approval_id",
        "approval_nonce",
        "preflight_id",
        "preflight_sha256",
        "request_sha256",
        "provider",
        "model_id",
        "max_billable_minutes",
        "upload_disclosure_acknowledged",
        "disclosure_sha256",
        "user_quote",
        "user_quote_sha256",
        "approved_upload_source_ids",
    )
    return {field: approval.get(field) for field in fields}


def canonical_artifact_path(edit_dir: Path, supplied: Path | None, name: str, label: str) -> Path:
    canonical = contained_child(edit_dir, name, label=label).resolve(strict=False)
    selected = canonical if supplied is None else supplied.expanduser().resolve(strict=False)
    if selected != canonical:
        raise PreflightError(f"{label} must use the canonical path: {canonical}")
    if selected.is_symlink():
        raise PreflightError(f"{label} must not be a symlink: {selected}")
    return selected


SOURCE_BINDING_FIELDS = (
    "id",
    "manifest_path",
    "resolved_path",
    "outside_project",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "duration_s",
)


def request_source_map(request: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    raw_sources = request.get("sources")
    if not isinstance(raw_sources, list):
        raise PreflightError(f"{label} sources must be an array")
    mapped: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            raise PreflightError(f"{label} source {index} is invalid")
        source_id = validate_source_id(raw.get("id"), label=f"{label} source {index} id")
        if source_id in mapped:
            raise PreflightError(f"{label} has duplicate source id {source_id!r}")
        if type(raw.get("cached")) is not bool or type(raw.get("will_upload")) is not bool:
            raise PreflightError(f"{label} source {source_id!r} has invalid cache/upload decision")
        if raw["cached"] == raw["will_upload"]:
            raise PreflightError(
                f"{label} source {source_id!r} cache/upload decision is contradictory"
            )
        if type(raw.get("outside_project")) is not bool:
            raise PreflightError(f"{label} source {source_id!r} outside marker is invalid")
        mapped[source_id] = raw
    return mapped


def stable_source_binding(source: dict[str, Any]) -> dict[str, Any]:
    return {field: source.get(field) for field in SOURCE_BINDING_FIELDS}


def source_attempt_identity(source: SourceAssessment) -> dict[str, Any]:
    return attempt_source_identity(
        source_id=source.source_id,
        resolved_path=str(source.path),
        sha256=source.sha256,
        duration_s=source.duration_s,
        outside_project=source.outside_project,
    )


def validate_request(
    videos_dir: Path,
    *,
    preflight_path: Path | None = None,
    approval_path: Path | None = None,
    provider: str = PROVIDER,
    model_id: str = MODEL_ID,
    language: str | None = None,
    num_speakers: int | None = None,
    duration_probe: DurationProbe = probe_audio_duration,
) -> ValidatedTranscriptionRequest:
    edit_dir, _, _ = resolve_manifest(videos_dir)
    selected_preflight = canonical_artifact_path(
        edit_dir,
        preflight_path,
        PREFLIGHT_NAME,
        "transcription preflight",
    )
    preflight = load_json_object(selected_preflight, "transcription preflight")
    if (
        preflight.get("version") != 1
        or preflight.get("schema_version") != "1.1.0"
        or preflight.get("type") != "transcription_preflight"
        or preflight.get("status") != "awaiting_explicit_approval"
        or not isinstance(preflight.get("preflight_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", preflight["preflight_id"]) is None
    ):
        raise PreflightError("transcription preflight has an unsupported version or status")
    if preflight.get("provider") != provider or preflight.get("model_id") != model_id:
        raise PreflightError("transcription preflight provider/model differs from this request")
    effective_language = preflight.get("language")
    effective_speakers = preflight.get("num_speakers")
    if effective_language is not None and not isinstance(effective_language, str):
        raise PreflightError("transcription preflight language is invalid")
    if effective_speakers is not None and type(effective_speakers) is not int:
        raise PreflightError("transcription preflight num_speakers is invalid")
    if language is not None and language != effective_language:
        raise PreflightError("requested language differs from the approved preflight")
    if num_speakers is not None and num_speakers != effective_speakers:
        raise PreflightError("requested num_speakers differs from the approved preflight")
    privacy = preflight.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("external_upload") is not True:
        raise PreflightError("transcription preflight privacy disclosure is invalid")
    if privacy.get("disclosure") != UPLOAD_DISCLOSURE or privacy.get(
        "disclosure_sha256"
    ) != canonical_json_sha256(UPLOAD_DISCLOSURE):
        raise PreflightError("transcription preflight upload disclosure changed")
    preflight_request = preflight.get("request")
    if not isinstance(preflight_request, dict):
        raise PreflightError("transcription preflight request is invalid")
    request_digest = canonical_json_sha256(preflight_request)
    if preflight.get("request_sha256") != request_digest:
        raise PreflightError("transcription preflight request binding is invalid")

    current = build_assessment(
        videos_dir,
        provider=provider,
        model_id=model_id,
        language=effective_language,
        num_speakers=effective_speakers,
        duration_probe=duration_probe,
    )
    original_sources = request_source_map(preflight_request, "preflight request")
    current_sources = request_source_map(current.request, "current request")
    original_header = dict(preflight_request)
    current_header = dict(current.request)
    original_header.pop("sources", None)
    current_header.pop("sources", None)
    if original_header != current_header or set(original_sources) != set(current_sources):
        raise PreflightError(
            "sources, manifest, or transcription settings changed after preflight"
        )
    for source_id, original in original_sources.items():
        current_source = current_sources[source_id]
        if stable_source_binding(original) != stable_source_binding(current_source):
            raise PreflightError(
                f"source identity/path/duration changed after preflight: {source_id!r}"
            )
        if current_source["will_upload"] and not original["will_upload"]:
            raise PreflightError(
                f"source {source_id!r} was cached and was not approved for upload; "
                "create a new preflight and obtain a new approval"
            )
    originally_approved_upload_ids = tuple(
        source_id
        for source_id, source in original_sources.items()
        if source["will_upload"]
    )
    # A fully cached request performs no upload and consumes no provider usage.
    if not current.billable_sources:
        return ValidatedTranscriptionRequest(
            assessment=current,
            preflight_path=selected_preflight,
            approval_path=None,
            approved_max_billable_minutes=0.0,
            approval_id=None,
            approval_nonce=None,
            approved_upload_source_ids=originally_approved_upload_ids,
        )

    selected_approval = canonical_artifact_path(
        edit_dir,
        approval_path,
        APPROVAL_NAME,
        "transcription approval",
    )
    approval = load_json_object(selected_approval, "transcription approval")
    if (
        approval.get("version") != 1
        or approval.get("schema_version") != "1.1.0"
        or approval.get("type") != "transcription_approval"
        or approval.get("status") != "approved"
    ):
        raise PreflightError("transcription approval has an unsupported version or status")
    if approval.get("preflight_sha256") != sha256_file(selected_preflight):
        raise PreflightError("transcription preflight changed after approval")
    if approval.get("preflight_id") != preflight.get("preflight_id"):
        raise PreflightError("transcription approval belongs to a different preflight id")
    if approval.get("request_sha256") != request_digest:
        raise PreflightError("transcription approval belongs to a different request")
    if approval.get("provider") != provider or approval.get("model_id") != model_id:
        raise PreflightError("transcription approval provider/model differs from this request")
    if approval.get("upload_disclosure_acknowledged") is not True:
        raise PreflightError("transcription upload disclosure was not acknowledged")
    if approval.get("disclosure_sha256") != canonical_json_sha256(UPLOAD_DISCLOSURE):
        raise PreflightError("transcription approval disclosure binding is invalid")
    quote = approval.get("user_quote")
    if not isinstance(quote, str) or not quote.strip():
        raise PreflightError("transcription approval has no exact user quote")
    if approval.get("user_quote_sha256") != canonical_json_sha256(quote):
        raise PreflightError("transcription approval quote hash is invalid")
    if approval.get("binding_sha256") != canonical_json_sha256(
        approval_binding_payload(approval)
    ):
        raise PreflightError("transcription approval binding hash is invalid")
    validate_approval_anchor(approval, edit_dir)
    approval_id = approval.get("approval_id")
    approval_nonce = approval.get("approval_nonce")
    if not isinstance(approval_id, str) or re.fullmatch(r"[0-9a-f]{32}", approval_id) is None:
        raise PreflightError("transcription approval_id is invalid")
    if not isinstance(approval_nonce, str) or re.fullmatch(r"[0-9a-f]{64}", approval_nonce) is None:
        raise PreflightError("transcription approval nonce is invalid")
    approved_ids = approval.get("approved_upload_source_ids")
    if (
        not isinstance(approved_ids, list)
        or any(not isinstance(value, str) for value in approved_ids)
        or tuple(approved_ids) != originally_approved_upload_ids
    ):
        raise PreflightError("transcription approval upload-source set differs from preflight")
    cap = finite_cap(approval.get("max_billable_minutes"))
    if current.billable_minutes > cap + 1e-9:
        raise PreflightError(
            f"current uncached audio is {current.billable_minutes:.6f} minutes, "
            f"above the approved cap of {cap:.6f} minutes"
        )
    consumed = consumed_attempt_keys(edit_dir)
    for source in current.billable_sources:
        assert_external_attempt_available(
            approval=approval,
            edit_dir=edit_dir,
            source_identity=source_attempt_identity(source),
        )
        key = attempt_key(approval_id, source_attempt_identity(source))
        if key in consumed:
            raise PreflightError(
                f"approval {approval_id} already consumed its one network attempt for "
                f"source {source.source_id!r}; create a new preflight and obtain a new approval"
            )
    return ValidatedTranscriptionRequest(
        assessment=current,
        preflight_path=selected_preflight,
        approval_path=selected_approval,
        approved_max_billable_minutes=cap,
        approval_id=approval_id,
        approval_nonce=approval_nonce,
        approved_upload_source_ids=originally_approved_upload_ids,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory initialized media and create an ElevenLabs upload/usage preflight"
    )
    parser.add_argument("videos_dir", type=Path)
    parser.add_argument("--provider", choices=(PROVIDER,), default=PROVIDER)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--language")
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing preflight (this invalidates its approval)",
    )
    args = parser.parse_args()

    edit_dir = args.videos_dir.expanduser().resolve() / "edit"
    with project_transcription_lock(edit_dir):
        return create_preflight(args)


def create_preflight(args: argparse.Namespace) -> int:

    assessment = build_assessment(
        args.videos_dir,
        provider=args.provider,
        model_id=args.model,
        language=args.language,
        num_speakers=args.num_speakers,
    )
    output = canonical_artifact_path(
        assessment.edit_dir,
        None,
        PREFLIGHT_NAME,
        "transcription preflight",
    )
    if output.exists() and not args.replace:
        raise PreflightError(
            f"preflight already exists: {output}; use --replace only before a new approval"
        )
    write_json_atomic(output, artifact_payload(assessment))
    print(f"transcription preflight: {output}")
    for source in assessment.sources:
        print(
            "source "
            f"id={source.source_id} "
            f"path={source.path} "
            f"sha256={source.sha256[:12]} "
            f"duration_s={source.duration_s:.6f} "
            f"will_upload={'yes' if not source.cached else 'no'} "
            f"cached={'yes' if source.cached else 'no'} "
            f"outside_project={'yes' if source.outside_project else 'no'}"
        )
    print(
        f"audio sources: {len(assessment.sources)} | cached: "
        f"{len(assessment.sources) - len(assessment.billable_sources)} | "
        f"uncached: {len(assessment.billable_sources)} | visual-only skipped: "
        f"{assessment.visual_only_count}"
    )
    print(f"estimated billable source audio: {assessment.billable_minutes:.6f} minutes")
    print("monetary price: not inferred; check the provider account/plan")
    if assessment.billable_sources:
        print(f"upload disclosure: {UPLOAD_DISCLOSURE}")
        print("next: obtain explicit user approval with a numeric maximum-minute cap")
    else:
        print("all audio sources are cached; no external upload is required")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        PreflightError,
        AttemptLedgerError,
        TranscriptionLockError,
        TranscriptionPathError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"transcription_preflight: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
