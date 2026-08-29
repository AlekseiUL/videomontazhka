#!/usr/bin/env python3
# Portions derived from video-use, Copyright (c) 2026 Browser Use, MIT License.
# Modifications Copyright 2026 SPRUT_AI contributors, Apache-2.0.
# See repository NOTICE and third_party/licenses/video-use-MIT.txt.
"""Approval-gated ElevenLabs Scribe transcription with collision-safe caching."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcription_safety import (
    AttemptLedgerError,
    TranscriptionLockError,
    TranscriptionPathError,
    append_attempt_event,
    assert_external_attempt_available,
    assert_attempt_available,
    contained_child,
    consume_external_attempt,
    project_transcription_lock,
    validate_source_id,
    write_json_atomic,
)
from transcription_preflight import PreflightError, source_attempt_identity


SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v1"


class TranscriptError(RuntimeError):
    pass


class ScribeAttemptError(TranscriptError):
    """A consumed HTTP attempt with a safe, non-secret terminal classification."""

    def __init__(self, message: str, *, consumed_status: str, outcome_code: str):
        super().__init__(message)
        self.consumed_status = consumed_status
        self.outcome_code = outcome_code


@dataclass(frozen=True)
class ManifestSourceIdentity:
    source_id: str
    sha256: str
    size_bytes: int
    mtime_ns: int


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_api_key(env_file: Path | None = None) -> str:
    """Load a secret only from the process environment or an explicit file.

    Never scan other Codex skills or user folders: that made usage provenance
    impossible to audit and could silently spend the wrong account's quota.
    """
    value = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if value:
        return value
    if env_file is None:
        raise TranscriptError(
            "ELEVENLABS_API_KEY is not configured; export it or pass an explicit --env-file"
        )
    selected = env_file.expanduser()
    if selected.is_symlink():
        raise TranscriptError("explicit environment file must not be a symlink")
    candidate = selected if selected.is_absolute() else Path.cwd() / selected
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise TranscriptError(f"explicit environment file not found: {candidate}") from exc
    except OSError as exc:
        raise TranscriptError("explicit environment file could not be opened safely") from exc
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(descriptor)
        raise TranscriptError("explicit environment file must be a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        os.close(descriptor)
        raise TranscriptError(
            "explicit environment file permissions are too broad; require mode 0600"
        )
    with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
        lines = handle.read().splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, raw = stripped.split("=", 1)
        if key.strip() == "ELEVENLABS_API_KEY" and raw.strip():
            return raw.strip().strip('"').strip("'")
    raise TranscriptError(f"ELEVENLABS_API_KEY is not set in the explicit environment file")


def source_identity_for(video: Path, edit_dir: Path) -> ManifestSourceIdentity:
    project_path = edit_dir / "project.json"
    manifest_value = "source_manifest.json"
    if project_path.is_file():
        project = json.loads(project_path.read_text(encoding="utf-8"))
        manifest_value = str(project.get("source_manifest") or manifest_value)
    raw_manifest = Path(manifest_value).expanduser()
    manifest_path = raw_manifest.resolve() if raw_manifest.is_absolute() else (edit_dir / raw_manifest).resolve()
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_root = Path(str(manifest.get("root") or "..")).expanduser()
        project_root = raw_root.resolve() if raw_root.is_absolute() else (manifest_path.parent / raw_root).resolve()
        entries = manifest.get("sources") or []
        if not isinstance(entries, list):
            raise TranscriptError("source manifest sources must be an array")
        matched_source: ManifestSourceIdentity | None = None
        seen_source_ids: set[str] = set()
        seen_source_paths: set[Path] = set()
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise TranscriptError(f"source manifest entry {index} is invalid")
            source_id = validate_source_id(
                item.get("id"), label=f"source manifest entry {index} id"
            )
            if source_id in seen_source_ids:
                raise TranscriptError(f"duplicate source id in manifest: {source_id!r}")
            seen_source_ids.add(source_id)
            raw = Path(str(item.get("path") or ""))
            candidate = raw if raw.is_absolute() else project_root / raw
            resolved_candidate = candidate.resolve()
            if resolved_candidate in seen_source_paths:
                raise TranscriptError(
                    f"source manifest maps the same file more than once: {resolved_candidate}"
                )
            seen_source_paths.add(resolved_candidate)
            if resolved_candidate == video.resolve():
                if matched_source is not None:
                    raise TranscriptError(
                        f"source manifest maps the same file to multiple ids: {video}"
                    )
                expected_sha256 = item.get("sha256")
                expected_size = item.get("size_bytes")
                expected_mtime = item.get("mtime_ns")
                if not isinstance(expected_sha256, str) or re.fullmatch(
                    r"[0-9a-f]{64}", expected_sha256
                ) is None:
                    raise TranscriptError(
                        f"source manifest entry {index} has no valid sha256"
                    )
                if type(expected_size) is not int or type(expected_mtime) is not int:
                    raise TranscriptError(
                        f"source manifest entry {index} has invalid size/mtime identity"
                    )
                stat_info = video.stat()
                if (
                    stat_info.st_size != expected_size
                    or stat_info.st_mtime_ns != expected_mtime
                ):
                    raise TranscriptError(
                        f"source changed since ingest: {video.name}; re-initialize before transcription"
                    )
                current_sha256 = file_sha256(video)
                if current_sha256 != expected_sha256:
                    raise TranscriptError(
                        f"source content hash changed since ingest: {video.name}; "
                        "re-initialize before transcription"
                    )
                matched_source = ManifestSourceIdentity(
                    source_id=source_id,
                    sha256=current_sha256,
                    size_bytes=stat_info.st_size,
                    mtime_ns=stat_info.st_mtime_ns,
                )
        if matched_source is not None:
            return matched_source
        raise TranscriptError(
            f"video is not listed in the initialized source manifest: {video}"
        )
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", video.stem).strip("_") or "source"
    source_id = validate_source_id(safe.lower(), label="derived source id")
    stat_info = video.stat()
    return ManifestSourceIdentity(
        source_id=source_id,
        sha256=file_sha256(video),
        size_bytes=stat_info.st_size,
        mtime_ns=stat_info.st_mtime_ns,
    )


def source_id_for(video: Path, edit_dir: Path) -> str:
    return source_identity_for(video, edit_dir).source_id


def extract_audio(video: Path, output: Path) -> None:
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output),
    ], check=False)
    if result.returncode:
        raise TranscriptError(f"ffmpeg audio extraction failed for {video}")


def ensure_transcription_client() -> None:
    try:
        import requests  # noqa: F401
    except ModuleNotFoundError as exc:
        raise TranscriptError(f"transcription client dependency is missing: {exc}") from exc


def snapshot_approved_source(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> Path:
    """Copy one opened source fd to private storage and verify bytes before ffmpeg."""
    before = os.lstat(source)
    source_fd = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    output_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    digest = hashlib.sha256()
    copied = 0
    try:
        opened = os.fstat(source_fd)
        current = os.lstat(source)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or (hasattr(os, "getuid") and (opened.st_uid != os.getuid() or current.st_uid != os.getuid()))
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise TranscriptError("source inode changed before private snapshot")
        while chunk := os.read(source_fd, 4 * 1024 * 1024):
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                view = view[written:]
        os.fsync(output_fd)
        final_source = os.fstat(source_fd)
        if (
            final_source.st_size != opened.st_size
            or final_source.st_mtime_ns != opened.st_mtime_ns
        ):
            raise TranscriptError("source changed while creating private snapshot")
    finally:
        os.close(source_fd)
        os.close(output_fd)
    if copied != expected_size or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise TranscriptError("private source snapshot differs from the approved source bytes")
    os.chmod(destination, 0o600)
    return destination


def call_scribe(audio: Path, api_key: str, language: str | None, speakers: int | None) -> dict[str, Any]:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise TranscriptError(f"transcription client dependency is missing: {exc}") from exc
    data: dict[str, str] = {
        "model_id": MODEL_ID,
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language
    if speakers:
        data["num_speakers"] = str(speakers)
    try:
        with audio.open("rb") as handle:
            response = requests.post(
                SCRIBE_URL,
                headers={"xi-api-key": api_key},
                files={"file": (audio.name, handle, "audio/wav")},
                data=data,
                timeout=1800,
            )
    except requests.RequestException as exc:
        raise ScribeAttemptError(
            "Scribe request outcome is ambiguous; this approval/source attempt is consumed",
            consumed_status="ambiguous_consumed",
            outcome_code="request_exception",
        ) from exc
    if response.status_code != 200:
        raise ScribeAttemptError(
            f"Scribe returned HTTP {response.status_code}; response body suppressed and "
            "this approval/source attempt is consumed",
            consumed_status="failed_consumed",
            outcome_code=f"http_{response.status_code}",
        )
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ScribeAttemptError(
            "Scribe returned an unreadable success response; outcome is ambiguous and this "
            "approval/source attempt is consumed",
            consumed_status="ambiguous_consumed",
            outcome_code="invalid_json_response",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("words"), list):
        raise ScribeAttemptError(
            "Scribe response has no word-level transcript; outcome is ambiguous and this "
            "approval/source attempt is consumed",
            consumed_status="ambiguous_consumed",
            outcome_code="invalid_word_payload",
        )
    return payload


def _read_word_payload(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("words"), list):
        return None
    return value


def _embedded_identity(payload: dict[str, Any]) -> dict[str, Any] | None:
    marker = payload.get("_videomontazhka_cache")
    if not isinstance(marker, dict) or marker.get("version") != 1:
        return None
    identity = marker.get("identity")
    return identity if isinstance(identity, dict) else None


def _metadata_identity(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    identity = value.get("identity") if isinstance(value, dict) else None
    return identity if isinstance(identity, dict) else None


def recover_cached_result(
    *,
    output: Path,
    metadata: Path,
    legacy_metadata: Path,
    temporary: Path,
    identity: dict[str, Any],
    digest: str,
) -> bool:
    """Recover complete or post-response interrupted cache state without an upload."""
    payload = _read_word_payload(output)
    cached_metadata = metadata if metadata.is_file() else legacy_metadata
    if payload is not None and _metadata_identity(cached_metadata) == identity:
        if cached_metadata == legacy_metadata and not metadata.exists():
            write_json_atomic(
                metadata,
                json.loads(cached_metadata.read_text(encoding="utf-8")),
            )
        print(f"cached: {output.name} ({digest[:12]})")
        return True

    recovered_from: Path | None = None
    if payload is not None and _embedded_identity(payload) == identity:
        recovered_from = output
    else:
        partial_payload = _read_word_payload(temporary)
        if partial_payload is not None and _embedded_identity(partial_payload) == identity:
            temporary.replace(output)
            payload = partial_payload
            recovered_from = temporary
    if recovered_from is None or payload is None:
        return False
    write_json_atomic(
        metadata,
        {
            "version": 1,
            "identity": identity,
            "transcript": output.name,
            "words": len(payload["words"]),
            "elapsed_s": 0.0,
            "recovered_after_interruption": True,
        },
    )
    print(f"recovered cached response: {output.name} ({digest[:12]})")
    return True


def transcribe(
    video: Path,
    edit_dir: Path,
    *,
    language: str | None,
    speakers: int | None,
    api_key: str | None,
    authorization: Any | None = None,
) -> Path:
    video = video.expanduser().resolve()
    edit_dir = edit_dir.expanduser().resolve()
    source_identity = source_identity_for(video, edit_dir)
    source_id = source_identity.source_id
    # Resolve every destination before the first mkdir/write.  This keeps a
    # malformed manifest or pre-existing symlink from redirecting cache output.
    transcript_dir = contained_child(edit_dir, "transcripts", label="transcript directory")
    metadata_dir = contained_child(transcript_dir, ".metadata", label="transcript metadata directory")
    output = contained_child(transcript_dir, f"{source_id}.json", label="transcript output")
    metadata = contained_child(metadata_dir, f"{source_id}.json", label="transcript metadata")
    legacy_metadata = contained_child(
        transcript_dir, f"{source_id}.meta.json", label="legacy transcript metadata"
    )
    temporary = contained_child(
        transcript_dir, f"{source_id}.part.json", label="temporary transcript output"
    )
    transcript_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    digest = source_identity.sha256
    identity = {
        "source": str(video.resolve()),
        "source_sha256": digest,
        "source_size": source_identity.size_bytes,
        "source_mtime_ns": source_identity.mtime_ns,
        "model_id": MODEL_ID,
        "language": language,
        "num_speakers": speakers,
        "timestamps_granularity": "word",
        "diarize": True,
        "tag_audio_events": True,
    }
    if recover_cached_result(
        output=output,
        metadata=metadata,
        legacy_metadata=legacy_metadata,
        temporary=temporary,
        identity=identity,
        digest=digest,
    ):
        return output

    if authorization is None:
        raise TranscriptError(
            "uncached ElevenLabs transcription requires a current transcription preflight "
            "and explicit approval"
        )
    assessment = getattr(authorization, "assessment", None)
    authorized_sources = getattr(assessment, "sources", ())
    authorized = next(
        (
            source
            for source in authorized_sources
            if getattr(source, "source_id", None) == source_id
            and getattr(source, "sha256", None) == digest
        ),
        None,
    )
    if authorized is None:
        raise TranscriptError("transcription approval does not cover this source identity")
    if getattr(authorized, "cached", False):
        raise TranscriptError(
            "source cache disappeared after validation; this run is not authorized to upload it"
        )
    if not api_key:
        raise TranscriptError("ELEVENLABS_API_KEY is required for an uncached approved upload")
    approval_id = getattr(authorization, "approval_id", None)
    if not isinstance(approval_id, str):
        raise TranscriptError("uncached upload authorization has no unique approval_id")
    preflight_path = getattr(authorization, "preflight_path", None)
    if not isinstance(preflight_path, Path) or not preflight_path.is_file():
        raise TranscriptError("uncached upload authorization has no current preflight artifact")
    preflight_digest = file_sha256(preflight_path)
    approval_path = getattr(authorization, "approval_path", None)
    if not isinstance(approval_path, Path) or not approval_path.is_file():
        raise TranscriptError("uncached upload authorization has no approval artifact")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    if not isinstance(approval, dict) or approval.get("approval_id") != approval_id:
        raise TranscriptError("uncached upload approval artifact changed after validation")
    attempt_identity = source_attempt_identity(authorized)
    assert_external_attempt_available(
        approval=approval,
        edit_dir=edit_dir,
        source_identity=attempt_identity,
    )
    assert_attempt_available(
        edit_dir,
        approval_id=approval_id,
        source_identity=attempt_identity,
    )

    # Validate the client dependency and prepare the exact upload before the
    # one-shot attempt is consumed. No provider call has happened yet.
    ensure_transcription_client()

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="sprut-scribe-") as temp:
        snapshot = contained_child(
            Path(temp),
            f"{source_id}{video.suffix or '.media'}",
            label="private source snapshot",
        )
        snapshot_approved_source(
            video,
            snapshot,
            expected_sha256=digest,
            expected_size=source_identity.size_bytes,
        )
        audio = contained_child(Path(temp), f"{source_id}.wav", label="temporary extracted audio")
        print(f"extracting audio: {video.name}", flush=True)
        extract_audio(snapshot, audio)
        if not audio.is_file() or audio.stat().st_size <= 44:
            raise TranscriptError("extracted audio is empty; network attempt was not consumed")
        print(f"uploading to ElevenLabs Scribe: {audio.stat().st_size / 1024 / 1024:.1f} MiB", flush=True)
        consume_external_attempt(
            approval=approval,
            edit_dir=edit_dir,
            source_identity=attempt_identity,
        )
        append_attempt_event(
            edit_dir,
            approval_id=approval_id,
            preflight_sha256=preflight_digest,
            source_identity=attempt_identity,
            status="attempt_started",
        )
        try:
            payload = call_scribe(audio, api_key, language, speakers)
            payload["_videomontazhka_cache"] = {"version": 1, "identity": identity}
            write_json_atomic(temporary, payload)
            temporary.replace(output)
            write_json_atomic(
                metadata,
                {
                    "version": 1,
                    "identity": identity,
                    "transcript": output.name,
                    "words": len(payload["words"]),
                    "elapsed_s": round(time.time() - started, 3),
                },
            )
        except ScribeAttemptError as exc:
            append_attempt_event(
                edit_dir,
                approval_id=approval_id,
                preflight_sha256=preflight_digest,
                source_identity=attempt_identity,
                status=exc.consumed_status,
                outcome_code=exc.outcome_code,
            )
            raise
        except Exception:
            append_attempt_event(
                edit_dir,
                approval_id=approval_id,
                preflight_sha256=preflight_digest,
                source_identity=attempt_identity,
                status="ambiguous_consumed",
                outcome_code="post_start_unclassified_failure",
            )
            raise
        else:
            append_attempt_event(
                edit_dir,
                approval_id=approval_id,
                preflight_sha256=preflight_digest,
                source_identity=attempt_identity,
                status="succeeded",
                outcome_code="word_transcript_persisted",
            )
    print(f"saved: {output} | words={len(payload['words'])} | source={digest[:12]}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely cache one explicitly approved ElevenLabs word-level transcript"
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--language")
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise TranscriptError(f"video not found: {video}")
    edit_dir = (args.edit_dir or video.parent / "edit").expanduser().resolve()
    # Validate the manifest ID before API-key lookup; transcribe() repeats this
    # check so a manifest change between the two operations still fails closed.
    source_id_for(video, edit_dir)
    try:
        from transcription_preflight import validate_request
    except ModuleNotFoundError as exc:
        raise TranscriptError(f"transcription preflight module is unavailable: {exc}") from exc
    with project_transcription_lock(edit_dir):
        authorization = validate_request(
            edit_dir.parent,
            preflight_path=args.preflight,
            approval_path=args.approval,
            language=args.language,
            num_speakers=args.num_speakers,
        )
        selected = next(
            (source for source in authorization.assessment.sources if source.path == video),
            None,
        )
        if selected is None:
            raise TranscriptError("video is not included in the approved transcription request")
        key = None if selected.cached else load_api_key(args.env_file)
        transcribe(
            video,
            edit_dir,
            language=authorization.assessment.language,
            speakers=authorization.assessment.num_speakers,
            api_key=key,
            authorization=authorization,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        TranscriptError,
        AttemptLedgerError,
        PreflightError,
        TranscriptionLockError,
        TranscriptionPathError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"transcribe_safe: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
