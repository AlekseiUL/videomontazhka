from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import transcribe_safe  # noqa: E402
from transcription_safety import (  # noqa: E402
    TranscriptionPathError,
    validate_source_id,
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_manifest(base: Path, source_id: str, *, audio: bool = True) -> tuple[Path, Path, Path]:
    root = base / "videos"
    edit = root / "edit"
    video = root / "source.mov"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"immutable source\n")
    stat_info = video.stat()
    write_json(edit / "project.json", {"source_manifest": "source_manifest.json"})
    write_json(edit / "source_manifest.json", {
        "version": 1,
        "root": "..",
        "sources": [{
            "id": source_id,
            "path": video.name,
            "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "size_bytes": stat_info.st_size,
            "mtime_ns": stat_info.st_mtime_ns,
            "audio": {"codec": "aac"} if audio else None,
        }],
    })
    return root, edit, video


class TranscriptionPathSafetyTests(unittest.TestCase):
    def test_source_ids_reject_absolute_dot_and_nested_paths(self) -> None:
        unsafe_ids = (
            "/tmp/transcript-target",
            "..",
            "../transcript-target",
            "nested/transcript-target",
            r"nested\transcript-target",
            r"C:\transcript-target",
        )
        for source_id in unsafe_ids:
            with self.subTest(source_id=source_id):
                with self.assertRaises(TranscriptionPathError):
                    validate_source_id(source_id)

    def test_transcribe_rejects_malicious_manifest_before_cache_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-transcribe-path-") as temp:
            base = Path(temp)
            target_stem = base / "outside-target"
            target = target_stem.with_suffix(".json")
            target.write_text("leave me unchanged\n", encoding="utf-8")
            _, edit, video = build_manifest(base, str(target_stem))

            with self.assertRaises(TranscriptionPathError):
                transcribe_safe.transcribe(
                    video,
                    edit,
                    language=None,
                    speakers=None,
                    api_key="unused",
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "leave me unchanged\n")
            self.assertFalse((edit / "transcripts").exists())

    def test_transcribe_cli_rejects_manifest_before_api_key_lookup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-transcribe-cli-path-") as temp:
            _, edit, video = build_manifest(Path(temp), "nested/source")
            environment = dict(os.environ)
            environment.pop("ELEVENLABS_API_KEY", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "transcribe_safe.py"),
                    str(video),
                    "--edit-dir",
                    str(edit),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("unsafe source manifest entry 0 id", result.stderr)
            self.assertNotIn("ELEVENLABS_API_KEY", result.stderr)
            self.assertFalse((edit / "transcripts").exists())

    def test_batch_rejects_malicious_manifest_before_api_key_or_workers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-transcribe-batch-path-") as temp:
            root, edit, _ = build_manifest(Path(temp), "../outside-target")
            environment = dict(os.environ)
            environment.pop("ELEVENLABS_API_KEY", None)
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "transcribe_batch_safe.py"), str(root)],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("unsafe source manifest entry 0 id", result.stderr)
            self.assertNotIn("ELEVENLABS_API_KEY", result.stderr)
            self.assertFalse((edit / "transcripts").exists())

    def test_transcribe_rejects_symlinked_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-transcribe-symlink-") as temp:
            base = Path(temp)
            _, edit, video = build_manifest(base, "source")
            outside = base / "outside-cache"
            outside.mkdir()
            (edit / "transcripts").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(TranscriptionPathError):
                transcribe_safe.transcribe(
                    video,
                    edit,
                    language=None,
                    speakers=None,
                    api_key="unused",
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_transcribe_rejects_video_outside_initialized_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-transcribe-unlisted-") as temp:
            root, edit, _ = build_manifest(Path(temp), "source")
            unlisted = root / "another-source.mov"
            unlisted.write_bytes(b"unlisted source\n")

            with self.assertRaisesRegex(
                transcribe_safe.TranscriptError, "not listed in the initialized source manifest"
            ):
                transcribe_safe.transcribe(
                    unlisted,
                    edit,
                    language=None,
                    speakers=None,
                    api_key="unused",
                )

            self.assertFalse((edit / "transcripts").exists())

    def test_batch_rejects_changed_source_before_api_key_lookup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sprut-transcribe-changed-") as temp:
            root, edit, video = build_manifest(Path(temp), "source")
            video.write_bytes(b"substituted source bytes\n")
            environment = dict(os.environ)
            environment.pop("ELEVENLABS_API_KEY", None)

            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "transcribe_batch_safe.py"), str(root)],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("source changed since ingest", result.stderr)
            self.assertNotIn("ELEVENLABS_API_KEY", result.stderr)
            self.assertFalse((edit / "transcripts").exists())


if __name__ == "__main__":
    unittest.main()
