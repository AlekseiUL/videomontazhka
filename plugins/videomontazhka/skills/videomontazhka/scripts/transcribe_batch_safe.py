#!/usr/bin/env python3
# Portions derived from video-use, Copyright (c) 2026 Browser Use, MIT License.
# Modifications Copyright 2026 Алексей Ульянов, Apache-2.0.
# See repository NOTICE and third_party/licenses/video-use-MIT.txt.
"""Transcribe all manifest sources behind a cost/privacy approval gate."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from transcription_preflight import (
    MODEL_ID,
    PROVIDER,
    PreflightError,
    validate_request,
)
from transcription_safety import (
    AttemptLedgerError,
    TranscriptionLockError,
    TranscriptionPathError,
    project_transcription_lock,
    validate_source_id,
)


class TranscriptError(RuntimeError):
    pass


DEFAULT_WORKERS = 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch ElevenLabs Scribe transcription with explicit cost/privacy approval"
    )
    parser.add_argument("videos_dir", type=Path)
    parser.add_argument("--language")
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--provider", choices=(PROVIDER,), default=PROVIDER)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="concurrent uploads (default 1; maximum 4)",
    )
    args = parser.parse_args()
    root = args.videos_dir.expanduser().resolve()
    if args.workers < 1 or args.workers > 4:
        raise TranscriptError("workers must be between 1 and 4")
    edit = root / "edit"
    project_path = edit / "project.json"
    manifest_value = "source_manifest.json"
    if project_path.is_file():
        project = json.loads(project_path.read_text(encoding="utf-8"))
        manifest_value = str(project.get("source_manifest") or manifest_value)
    raw_manifest = Path(manifest_value).expanduser()
    manifest_path = raw_manifest.resolve() if raw_manifest.is_absolute() else (edit / raw_manifest).resolve()
    if not manifest_path.is_file():
        raise TranscriptError("run init_project.py before batch transcription")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_root = Path(str(manifest.get("root") or "..")).expanduser()
    source_root = raw_root.resolve() if raw_root.is_absolute() else (manifest_path.parent / raw_root).resolve()
    entries = manifest.get("sources") or []
    if not isinstance(entries, list) or not entries:
        raise TranscriptError("source manifest is empty")
    videos: list[Path] = []
    skipped_visual: list[tuple[str, Path]] = []
    seen_source_ids: set[str] = set()
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
        path = raw if raw.is_absolute() else source_root / raw
        if not path.is_file():
            raise TranscriptError(f"source not found: {path}")
        resolved = path.resolve()
        if item.get("audio") is None:
            skipped_visual.append((source_id, resolved))
            print(
                f"SKIP: {source_id} ({resolved.name}) has no audio stream; "
                "kept as a visual-only source",
                flush=True,
            )
            continue
        videos.append(resolved)
    if not videos:
        print(
            f"batch transcription complete: 0 transcribed, "
            f"{len(skipped_visual)} visual-only source(s) skipped"
        )
        return 0
    try:
        import transcribe_safe
    except ModuleNotFoundError as exc:
        raise TranscriptError(
            f"audio sources require the transcription client dependency: {exc}"
        ) from exc
    # Hash every upload candidate against the immutable ingest manifest before
    # looking up credentials or starting workers. transcribe() repeats the
    # check immediately before extraction to catch changes during setup.
    try:
        for video in videos:
            transcribe_safe.source_identity_for(video, edit)
    except transcribe_safe.TranscriptError as exc:
        raise TranscriptError(str(exc)) from exc
    failures: list[str] = []
    with project_transcription_lock(edit):
        authorization = validate_request(
            root,
            preflight_path=args.preflight,
            approval_path=args.approval,
            provider=args.provider,
            model_id=args.model,
            language=args.language,
            num_speakers=args.num_speakers,
        )
        approved_paths = {source.path for source in authorization.assessment.sources}
        if set(videos) != approved_paths:
            raise TranscriptError(
                "approved transcription inventory differs from the current audio-source manifest"
            )
        needs_upload = bool(authorization.assessment.billable_sources)
        try:
            key = transcribe_safe.load_api_key(args.env_file) if needs_upload else None
        except transcribe_safe.TranscriptError as exc:
            raise TranscriptError(str(exc)) from exc
        workers = min(args.workers, len(videos))
        print(
            f"approved uncached audio: {authorization.assessment.billable_minutes:.6f} "
            f"minute(s) / cap {authorization.approved_max_billable_minutes:.6f}; "
            f"workers={workers}",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    transcribe_safe.transcribe,
                    video,
                    edit,
                    language=authorization.assessment.language,
                    speakers=authorization.assessment.num_speakers,
                    api_key=key,
                    authorization=authorization,
                ): video
                for video in videos
            }
            for future in as_completed(futures):
                video = futures[future]
                try:
                    future.result()
                except Exception as exc:  # keep other independent sources resumable
                    failures.append(f"{video.name}: {exc}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print(
        f"batch transcription complete: {len(videos)} transcribed, "
        f"{len(skipped_visual)} visual-only source(s) skipped"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        PreflightError,
        AttemptLedgerError,
        TranscriptError,
        TranscriptionLockError,
        TranscriptionPathError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"transcribe_batch_safe: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
