from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCHEMAS = ROOT / "schemas"
sys.path.insert(0, str(SCRIPTS))

import transcribe_batch_safe  # noqa: E402
import transcribe_safe  # noqa: E402
import transcription_preflight  # noqa: E402
from transcription_preflight import PreflightError  # noqa: E402
from transcription_safety import (  # noqa: E402
    AttemptLedgerError,
    TranscriptionLockError,
    TranscriptionPathError,
    append_attempt_event,
    approval_registry_paths,
    consume_external_attempt,
    external_attempt_marker_path,
    project_transcription_lock,
    read_attempt_ledger,
    write_json_atomic,
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_project(base: Path, durations: tuple[float, ...] = (60.0,)) -> tuple[Path, Path, dict[str, float]]:
    root = base / "videos"
    edit = root / "edit"
    root.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    probes: dict[str, float] = {}
    for index, duration in enumerate(durations, start=1):
        path = root / f"source-{index}.mov"
        path.write_bytes(f"immutable source {index}\n".encode())
        stat_info = path.stat()
        probes[path.name] = duration
        entries.append(
            {
                "id": f"source_{index}",
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": stat_info.st_size,
                "mtime_ns": stat_info.st_mtime_ns,
                "duration_s": duration,
                "audio": {"codec": "aac"},
            }
        )
    write_json(edit / "project.json", {"source_manifest": "source_manifest.json"})
    write_json(
        edit / "source_manifest.json",
        {"version": 1, "root": "..", "sources": entries},
    )
    return root, edit, probes


def probe_from(values: dict[str, float]):
    def probe(path: Path) -> float:
        return values[path.name]

    return probe


def create_preflight(root: Path, probes: dict[str, float]) -> transcription_preflight.Assessment:
    assessment = transcription_preflight.build_assessment(
        root,
        duration_probe=probe_from(probes),
    )
    write_json_atomic(
        assessment.edit_dir / transcription_preflight.PREFLIGHT_NAME,
        transcription_preflight.artifact_payload(assessment),
    )
    return assessment


def record_approval(
    edit: Path,
    cap: float,
    *,
    acknowledge: bool = True,
    replace: bool = False,
) -> subprocess.CompletedProcess[str]:
    app_home = edit.parent.parent / "vdm-home"
    os.environ["VIDEOMONTAZHKA_HOME"] = str(app_home)
    command = [
        sys.executable,
        str(SCRIPTS / "record_transcription_approval.py"),
        "--edit-dir",
        str(edit),
        "--max-billable-minutes",
        str(cap),
        "--quote",
        f"Подтверждаю загрузку и лимит {cap} минут",
    ]
    if acknowledge:
        command.append("--acknowledge-upload")
    if replace:
        command.append("--replace")
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=dict(os.environ),
    )


def fake_extract(_video: Path, output: Path) -> None:
    output.write_bytes(b"RIFF" + b"\0" * 128)


def install_cache(
    assessment: transcription_preflight.Assessment,
    index: int,
) -> None:
    source = assessment.sources[index]
    transcript_dir = assessment.edit_dir / "transcripts"
    metadata_dir = transcript_dir / ".metadata"
    write_json(
        transcript_dir / f"{source.source_id}.json",
        {
            "text": "тест",
            "words": [
                {
                    "text": "тест",
                    "start": 0.0,
                    "end": 0.5,
                    "type": "word",
                    "speaker_id": "speaker_0",
                }
            ],
        },
    )
    write_json(
        metadata_dir / f"{source.source_id}.json",
        {
            "version": 1,
            "identity": transcription_preflight.transcript_identity(
                source,
                language=assessment.language,
                num_speakers=assessment.num_speakers,
            ),
            "transcript": f"{source.source_id}.json",
            "words": 1,
        },
    )


class TranscriptionApprovalGateTests(unittest.TestCase):
    def test_preflight_has_hash_bound_inventory_and_no_currency_guess(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-preflight-") as temp:
            root, edit, probes = build_project(Path(temp), (61.0, 59.0))
            assessment = create_preflight(root, probes)
            payload = json.loads(
                (edit / transcription_preflight.PREFLIGHT_NAME).read_text(encoding="utf-8")
            )

            self.assertEqual(payload["schema_version"], "1.1.0")
            self.assertEqual(payload["type"], "transcription_preflight")
            self.assertEqual(payload["request_sha256"], assessment.request_sha256)
            self.assertEqual(payload["usage_estimate"]["estimated_billable_minutes"], 2.0)
            self.assertIs(payload["usage_estimate"]["currency_quote_included"], False)
            self.assertEqual(payload["usage_estimate"]["cached_source_ids"], [])
            self.assertEqual(
                payload["privacy"]["disclosure"],
                transcription_preflight.UPLOAD_DISCLOSURE,
            )

    def test_approval_requires_disclosure_and_cap_at_least_estimate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-approval-") as temp:
            root, edit, probes = build_project(Path(temp), (90.0,))
            create_preflight(root, probes)

            missing_ack = record_approval(edit, 1.5, acknowledge=False)
            self.assertEqual(missing_ack.returncode, 2, missing_ack.stderr)
            self.assertIn("--acknowledge-upload", missing_ack.stderr)
            too_low = record_approval(edit, 1.0)
            self.assertEqual(too_low.returncode, 2, too_low.stderr)
            self.assertIn("below the preflight estimate", too_low.stderr)
            self.assertFalse((edit / transcription_preflight.APPROVAL_NAME).exists())

            accepted = record_approval(edit, 1.5)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(approval["type"], "transcription_approval")
            self.assertEqual(approval["max_billable_minutes"], 1.5)
            self.assertRegex(approval["user_quote_sha256"], r"^[0-9a-f]{64}$")

    def test_uncached_request_refuses_without_valid_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-refusal-") as temp:
            root, _, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            with self.assertRaisesRegex(PreflightError, "approval.*not found"):
                transcription_preflight.validate_request(
                    root,
                    duration_probe=probe_from(probes),
                )

    def test_current_valid_approval_authorizes_only_within_numeric_cap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-valid-approval-") as temp:
            root, edit, probes = build_project(Path(temp), (75.0,))
            create_preflight(root, probes)
            result = record_approval(edit, 1.25)
            self.assertEqual(result.returncode, 0, result.stderr)

            validated = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )
            self.assertAlmostEqual(validated.assessment.billable_minutes, 1.25)
            self.assertEqual(validated.approved_max_billable_minutes, 1.25)

            approval_path = edit / transcription_preflight.APPROVAL_NAME
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval["max_billable_minutes"] = 99
            write_json(approval_path, approval)
            with self.assertRaisesRegex(PreflightError, "binding hash"):
                transcription_preflight.validate_request(
                    root,
                    duration_probe=probe_from(probes),
                )

    def test_resume_reuses_completed_cache_under_original_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-resume-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0, 60.0))
            original = create_preflight(root, probes)
            result = record_approval(edit, 2.0)
            self.assertEqual(result.returncode, 0, result.stderr)
            install_cache(original, 0)

            resumed = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )
            self.assertNotEqual(resumed.assessment.request_sha256, original.request_sha256)
            self.assertEqual(
                resumed.approved_upload_source_ids,
                ("source_1", "source_2"),
            )
            self.assertEqual(
                [source.source_id for source in resumed.assessment.billable_sources],
                ["source_2"],
            )
            self.assertEqual(resumed.assessment.billable_minutes, 1.0)
            self.assertEqual(resumed.approved_max_billable_minutes, 2.0)

    def test_formerly_cached_source_can_never_become_a_new_upload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-lost-original-cache-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            initial = transcription_preflight.build_assessment(
                root,
                duration_probe=probe_from(probes),
            )
            install_cache(initial, 0)
            cached_preflight = create_preflight(root, probes)
            self.assertEqual(cached_preflight.billable_sources, ())
            (edit / "transcripts" / "source_1.json").unlink()
            (edit / "transcripts" / ".metadata" / "source_1.json").unlink()

            with self.assertRaisesRegex(PreflightError, "was cached and was not approved"):
                transcription_preflight.validate_request(
                    root,
                    duration_probe=probe_from(probes),
                )

    def test_ambiguous_attempt_is_consumed_and_second_invocation_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-ambiguous-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            result = record_approval(edit, 1.0)
            self.assertEqual(result.returncode, 0, result.stderr)
            authorization = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )
            source = authorization.assessment.sources[0]
            class RequestException(Exception):
                pass

            def timeout(*_args: Any, **_kwargs: Any) -> None:
                raise RequestException("simulated timeout")

            requests = types.SimpleNamespace(
                RequestException=RequestException,
                post=timeout,
            )
            with mock.patch.dict(sys.modules, {"requests": requests}), mock.patch.object(
                transcribe_safe, "extract_audio", side_effect=fake_extract
            ):
                with self.assertRaises(transcribe_safe.ScribeAttemptError):
                    transcribe_safe.transcribe(
                        source.path,
                        edit,
                        language=None,
                        speakers=None,
                        api_key="test-key",
                        authorization=authorization,
                    )

            records = read_attempt_ledger(edit)
            self.assertEqual(
                (edit / "transcription_attempts.jsonl").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                [record["status"] for record in records],
                ["attempt_started", "ambiguous_consumed"],
            )
            with mock.patch.object(transcribe_safe, "extract_audio") as extract, mock.patch.object(
                transcribe_safe, "call_scribe"
            ) as network:
                with self.assertRaises(AttemptLedgerError):
                    transcribe_safe.transcribe(
                        source.path,
                        edit,
                        language=None,
                        speakers=None,
                        api_key="test-key",
                        authorization=authorization,
                    )
            extract.assert_not_called()
            network.assert_not_called()
            with self.assertRaisesRegex(AttemptLedgerError, "already consumed"):
                transcription_preflight.validate_request(
                    root,
                    duration_probe=probe_from(probes),
                )
            same_preflight_reapproval = record_approval(edit, 1.0, replace=True)
            self.assertEqual(same_preflight_reapproval.returncode, 2)
            self.assertIn("create a new preflight", same_preflight_reapproval.stderr)
            previous_approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            create_preflight(root, probes)
            new_approval = record_approval(edit, 1.0, replace=True)
            self.assertEqual(new_approval.returncode, 0, new_approval.stderr)
            replacement = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            self.assertNotEqual(replacement["approval_id"], previous_approval["approval_id"])
            self.assertNotEqual(replacement["preflight_id"], previous_approval["preflight_id"])

    def test_successful_attempt_with_lost_cache_requires_new_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-lost-success-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            result = record_approval(edit, 1.0)
            self.assertEqual(result.returncode, 0, result.stderr)
            authorization = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )
            source = authorization.assessment.sources[0]
            with mock.patch.object(transcribe_safe, "ensure_transcription_client"), mock.patch.object(
                transcribe_safe, "extract_audio", side_effect=fake_extract
            ), mock.patch.object(
                transcribe_safe,
                "call_scribe",
                return_value={"words": [{"text": "ok", "start": 0.0, "end": 0.1}]},
            ):
                transcribe_safe.transcribe(
                    source.path,
                    edit,
                    language=None,
                    speakers=None,
                    api_key="test-key",
                    authorization=authorization,
                )
            self.assertEqual(
                [record["status"] for record in read_attempt_ledger(edit)],
                ["attempt_started", "succeeded"],
            )
            (edit / "transcripts" / "source_1.json").unlink()
            (edit / "transcripts" / ".metadata" / "source_1.json").unlink()
            with self.assertRaisesRegex(AttemptLedgerError, "already consumed"):
                transcription_preflight.validate_request(
                    root,
                    duration_probe=probe_from(probes),
                )

    def test_external_consumption_survives_project_ledger_deletion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-ledger-delete-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0).returncode, 0)
            authorization = transcription_preflight.validate_request(
                root, duration_probe=probe_from(probes)
            )
            approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            consume_external_attempt(
                approval=approval,
                edit_dir=edit,
                source_identity=transcription_preflight.source_attempt_identity(
                    authorization.assessment.sources[0]
                ),
            )
            ledger = edit / "transcription_attempts.jsonl"
            ledger.unlink(missing_ok=True)
            marker = external_attempt_marker_path(
                approval["approval_id"],
                transcription_preflight.source_attempt_identity(
                    authorization.assessment.sources[0]
                ),
            )
            marker.unlink()
            with self.assertRaisesRegex(AttemptLedgerError, "already consumed"):
                transcription_preflight.validate_request(
                    root, duration_probe=probe_from(probes)
                )
            replacement = record_approval(edit, 1.0, replace=True)
            self.assertEqual(replacement.returncode, 2)
            self.assertIn("create a new preflight", replacement.stderr)

    def test_missing_anchor_and_fresh_application_home_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-anchor-") as temp:
            base = Path(temp)
            root, edit, probes = build_project(base, (60.0,))
            create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0).returncode, 0)
            approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            anchor, _ = approval_registry_paths(approval["approval_id"])
            anchor.unlink()
            with self.assertRaisesRegex(AttemptLedgerError, "missing external"):
                transcription_preflight.validate_request(
                    root, duration_probe=probe_from(probes)
                )

            # Re-record under a new preflight/home, then copy the old project view
            # to a fresh application home with no registry state.
            create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0, replace=True).returncode, 0)
            os.environ["VIDEOMONTAZHKA_HOME"] = str(base / "fresh-vdm-home")
            with self.assertRaisesRegex(AttemptLedgerError, "missing external"):
                transcription_preflight.validate_request(
                    root, duration_probe=probe_from(probes)
                )

    def test_external_consume_before_project_ledger_blocks_crash_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-crash-gap-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0).returncode, 0)
            authorization = transcription_preflight.validate_request(
                root, duration_probe=probe_from(probes)
            )
            approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            consume_external_attempt(
                approval=approval,
                edit_dir=edit,
                source_identity=transcription_preflight.source_attempt_identity(
                    authorization.assessment.sources[0]
                ),
            )
            self.assertFalse((edit / "transcription_attempts.jsonl").exists())
            with self.assertRaisesRegex(AttemptLedgerError, "already consumed"):
                transcription_preflight.validate_request(
                    root, duration_probe=probe_from(probes)
                )

    def test_source_replacement_after_validation_never_uploads_mismatched_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-source-snapshot-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0).returncode, 0)
            authorization = transcription_preflight.validate_request(
                root, duration_probe=probe_from(probes)
            )
            source = authorization.assessment.sources[0]
            captured: dict[str, str] = {}

            def replace_after_snapshot(snapshot: Path, output: Path) -> None:
                captured["snapshot"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                source.path.write_bytes(b"replacement after validation\n")
                fake_extract(snapshot, output)

            with mock.patch.object(transcribe_safe, "ensure_transcription_client"), mock.patch.object(
                transcribe_safe, "extract_audio", side_effect=replace_after_snapshot
            ), mock.patch.object(
                transcribe_safe,
                "call_scribe",
                return_value={"words": [{"text": "safe", "start": 0.0, "end": 0.1}]},
            ):
                transcribe_safe.transcribe(
                    source.path,
                    edit,
                    language=None,
                    speakers=None,
                    api_key="test-key",
                    authorization=authorization,
                )
            self.assertEqual(captured["snapshot"], source.sha256)
            self.assertNotEqual(hashlib.sha256(source.path.read_bytes()).hexdigest(), source.sha256)

    def test_state_files_reject_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-state-inode-") as temp:
            base = Path(temp)
            edit = base / "edit"
            edit.mkdir()
            lock = edit / ".transcription.lock"
            lock.write_text("{}\n", encoding="utf-8")
            lock.chmod(0o600)
            os.link(lock, base / "lock-hardlink")
            with self.assertRaisesRegex(TranscriptionPathError, "hardlinks"):
                with project_transcription_lock(edit):
                    self.fail("hardlinked lock must not be accepted")

            ledger_edit = base / "ledger-edit"
            ledger_edit.mkdir()
            append_attempt_event(
                ledger_edit,
                approval_id="a" * 32,
                preflight_sha256="b" * 64,
                source_identity={
                    "id": "source",
                    "resolved_path": str(base / "source.mov"),
                    "outside_project": False,
                    "sha256": "c" * 64,
                    "duration_s": 1.0,
                },
                status="attempt_started",
            )
            os.link(
                ledger_edit / "transcription_attempts.jsonl",
                base / "ledger-hardlink",
            )
            with self.assertRaisesRegex(TranscriptionPathError, "hardlinks"):
                read_attempt_ledger(ledger_edit)

    def test_external_anchor_rejects_hardlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-anchor-inode-") as temp:
            base = Path(temp)
            root, edit, probes = build_project(base, (60.0,))
            create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0).returncode, 0)
            approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            anchor, _ = approval_registry_paths(approval["approval_id"])
            os.link(anchor, base / "anchor-hardlink")
            with self.assertRaisesRegex(TranscriptionPathError, "hardlinks"):
                transcription_preflight.validate_request(
                    root, duration_probe=probe_from(probes)
                )

    def test_partial_multi_source_success_resumes_only_unattempted_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-partial-multi-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0, 120.0))
            create_preflight(root, probes)
            result = record_approval(edit, 3.0)
            self.assertEqual(result.returncode, 0, result.stderr)
            authorization = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )
            first = authorization.assessment.sources[0]
            with mock.patch.object(transcribe_safe, "ensure_transcription_client"), mock.patch.object(
                transcribe_safe, "extract_audio", side_effect=fake_extract
            ), mock.patch.object(
                transcribe_safe,
                "call_scribe",
                return_value={"words": [{"text": "one", "start": 0.0, "end": 0.1}]},
            ):
                transcribe_safe.transcribe(
                    first.path,
                    edit,
                    language=None,
                    speakers=None,
                    api_key="test-key",
                    authorization=authorization,
                )

            resumed = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )
            self.assertEqual(
                [source.source_id for source in resumed.assessment.billable_sources],
                ["source_2"],
            )
            self.assertEqual(resumed.approved_upload_source_ids, ("source_1", "source_2"))

    def test_fully_cached_request_needs_no_key_or_paid_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-cached-") as temp:
            root, _, probes = build_project(Path(temp), (60.0,))
            initial = transcription_preflight.build_assessment(
                root,
                duration_probe=probe_from(probes),
            )
            install_cache(initial, 0)
            cached = create_preflight(root, probes)
            validated = transcription_preflight.validate_request(
                root,
                duration_probe=probe_from(probes),
            )

            self.assertEqual(cached.billable_sources, ())
            self.assertIsNone(validated.approval_path)
            with mock.patch.object(
                transcribe_safe,
                "call_scribe",
                side_effect=AssertionError("network must not run for a cache hit"),
            ):
                output = transcribe_safe.transcribe(
                    cached.sources[0].path,
                    cached.edit_dir,
                    language=None,
                    speakers=None,
                    api_key=None,
                    authorization=None,
                )
            self.assertTrue(output.is_file())

    def test_approved_uncached_request_that_becomes_fully_cached_needs_no_anchor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-late-cache-") as temp:
            base = Path(temp)
            root, edit, probes = build_project(base, (60.0,))
            assessment = create_preflight(root, probes)
            self.assertEqual(record_approval(edit, 1.0).returncode, 0)
            install_cache(assessment, 0)
            os.environ["VIDEOMONTAZHKA_HOME"] = str(base / "fresh-empty-home")
            validated = transcription_preflight.validate_request(
                root, duration_probe=probe_from(probes)
            )
            self.assertEqual(validated.assessment.billable_sources, ())
            self.assertIsNone(validated.approval_path)

    def test_post_response_partial_is_recovered_without_reupload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-partial-") as temp:
            root, _, probes = build_project(Path(temp), (60.0,))
            initial = transcription_preflight.build_assessment(
                root,
                duration_probe=probe_from(probes),
            )
            source = initial.sources[0]
            write_json(
                initial.edit_dir / "transcripts" / f"{source.source_id}.part.json",
                {
                    "words": [
                        {
                            "text": "сохранено",
                            "start": 0.0,
                            "end": 0.5,
                            "type": "word",
                        }
                    ],
                    "_videomontazhka_cache": {
                        "version": 1,
                        "identity": transcription_preflight.transcript_identity(
                            source,
                            language=None,
                            num_speakers=None,
                        ),
                    },
                },
            )
            recoverable = create_preflight(root, probes)
            self.assertEqual(recoverable.billable_sources, ())
            with mock.patch.object(
                transcribe_safe,
                "call_scribe",
                side_effect=AssertionError("recoverable response must not be uploaded again"),
            ):
                output = transcribe_safe.transcribe(
                    source.path,
                    initial.edit_dir,
                    language=None,
                    speakers=None,
                    api_key=None,
                    authorization=None,
                )
            self.assertTrue(output.is_file())
            self.assertFalse(
                (initial.edit_dir / "transcripts" / f"{source.source_id}.part.json").exists()
            )
            metadata = json.loads(
                (
                    initial.edit_dir
                    / "transcripts"
                    / ".metadata"
                    / f"{source.source_id}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIs(metadata["recovered_after_interruption"], True)

    def test_uncached_direct_call_refuses_before_extraction_or_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-direct-refusal-") as temp:
            root, _, probes = build_project(Path(temp), (60.0,))
            assessment = transcription_preflight.build_assessment(
                root,
                duration_probe=probe_from(probes),
            )
            with mock.patch.object(transcribe_safe, "extract_audio") as extract, mock.patch.object(
                transcribe_safe, "call_scribe"
            ) as network:
                with self.assertRaisesRegex(
                    transcribe_safe.TranscriptError,
                    "requires a current transcription preflight",
                ):
                    transcribe_safe.transcribe(
                        assessment.sources[0].path,
                        assessment.edit_dir,
                        language=None,
                        speakers=None,
                        api_key="unused",
                    )
            extract.assert_not_called()
            network.assert_not_called()

    def test_api_key_comes_only_from_environment_or_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-key-") as temp:
            env_file = Path(temp) / "chosen.env"
            env_file.write_text("ELEVENLABS_API_KEY=file-secret\n", encoding="utf-8")
            env_file.chmod(0o600)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(transcribe_safe.TranscriptError, "not configured"):
                    transcribe_safe.load_api_key()
                self.assertEqual(transcribe_safe.load_api_key(env_file), "file-secret")
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "process-secret"}, clear=True):
                self.assertEqual(transcribe_safe.load_api_key(env_file), "process-secret")

    def test_explicit_env_file_rejects_symlink_and_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-key-mode-") as temp:
            base = Path(temp)
            broad = base / "broad.env"
            broad.write_text("ELEVENLABS_API_KEY=secret\n", encoding="utf-8")
            broad.chmod(0o644)
            link = base / "linked.env"
            link.symlink_to(broad)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(transcribe_safe.TranscriptError, "permissions"):
                    transcribe_safe.load_api_key(broad)
                with self.assertRaisesRegex(transcribe_safe.TranscriptError, "symlink"):
                    transcribe_safe.load_api_key(link)

    def test_non_200_error_suppresses_provider_response_body(self) -> None:
        class Response:
            status_code = 500
            text = "DO-NOT-PRINT-PROVIDER-BODY"

        class RequestException(Exception):
            pass

        requests = types.SimpleNamespace(
            RequestException=RequestException,
            post=lambda *args, **kwargs: Response(),
        )

        with tempfile.TemporaryDirectory(prefix="videomontazhka-http-body-") as temp:
            audio = Path(temp) / "audio.wav"
            audio.write_bytes(b"RIFF" + b"\0" * 128)
            with mock.patch.dict(sys.modules, {"requests": requests}):
                with self.assertRaises(transcribe_safe.ScribeAttemptError) as caught:
                    transcribe_safe.call_scribe(audio, "secret", None, None)
            self.assertNotIn("DO-NOT-PRINT-PROVIDER-BODY", str(caught.exception))
            self.assertIn("body suppressed", str(caught.exception))
            self.assertEqual(caught.exception.consumed_status, "failed_consumed")

    def test_preflight_binds_resolved_outside_path_and_prints_exact_decision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-outside-") as temp:
            base = Path(temp)
            root, edit, probes = build_project(base, (60.0,))
            original = root / "source-1.mov"
            outside = base / "outside-source.mov"
            original.replace(outside)
            manifest_path = edit / "source_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["sources"][0]["path"] = str(outside)
            write_json(manifest_path, manifest)
            probes[outside.name] = probes.pop(original.name)
            assessment = transcription_preflight.build_assessment(
                root,
                duration_probe=probe_from(probes),
            )
            request_source = assessment.request["sources"][0]
            self.assertEqual(request_source["resolved_path"], str(outside.resolve()))
            self.assertIs(request_source["outside_project"], True)
            self.assertIs(request_source["will_upload"], True)
            self.assertIs(request_source["cached"], False)

            stream = io.StringIO()
            with mock.patch.object(
                transcription_preflight,
                "build_assessment",
                return_value=assessment,
            ), mock.patch.object(
                sys,
                "argv",
                ["transcription_preflight.py", str(root)],
            ), redirect_stdout(stream):
                self.assertEqual(transcription_preflight.main(), 0)
            stdout = stream.getvalue()
            self.assertIn("id=source_1", stdout)
            self.assertIn(f"path={outside.resolve()}", stdout)
            self.assertIn(f"sha256={assessment.sources[0].sha256[:12]}", stdout)
            self.assertIn("duration_s=60.000000", stdout)
            self.assertIn("will_upload=yes", stdout)
            self.assertIn("cached=no", stdout)
            self.assertIn("outside_project=yes", stdout)

    def test_project_lock_rejects_concurrent_process_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-lock-") as temp:
            edit = Path(temp) / "edit"
            with project_transcription_lock(edit):
                self.assertEqual((edit / ".transcription.lock").stat().st_mode & 0o777, 0o600)
                with self.assertRaises(TranscriptionLockError):
                    with project_transcription_lock(edit):
                        self.fail("second lock owner must not enter")

    def test_atomic_json_outputs_are_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="videomontazhka-private-json-") as temp:
            output = Path(temp) / "artifact.json"
            write_json_atomic(output, {"safe": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_batch_defaults_to_one_worker(self) -> None:
        self.assertEqual(transcribe_batch_safe.DEFAULT_WORKERS, 1)

    def test_generated_artifacts_match_committed_json_schemas(self) -> None:
        try:
            import jsonschema
        except ModuleNotFoundError:
            self.skipTest("jsonschema is not installed")
        with tempfile.TemporaryDirectory(prefix="videomontazhka-schema-") as temp:
            root, edit, probes = build_project(Path(temp), (60.0,))
            create_preflight(root, probes)
            result = record_approval(edit, 1.0)
            self.assertEqual(result.returncode, 0, result.stderr)
            preflight = json.loads(
                (edit / transcription_preflight.PREFLIGHT_NAME).read_text(encoding="utf-8")
            )
            approval = json.loads(
                (edit / transcription_preflight.APPROVAL_NAME).read_text(encoding="utf-8")
            )
            preflight_schema = json.loads(
                (SCHEMAS / "transcription_preflight.schema.json").read_text(encoding="utf-8")
            )
            approval_schema = json.loads(
                (SCHEMAS / "transcription_approval.schema.json").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(preflight_schema).validate(preflight)
            jsonschema.Draft202012Validator(approval_schema).validate(approval)
            assessment = transcription_preflight.build_assessment(
                root,
                duration_probe=probe_from(probes),
            )
            attempt = append_attempt_event(
                edit,
                approval_id=approval["approval_id"],
                preflight_sha256=approval["preflight_sha256"],
                source_identity=transcription_preflight.source_attempt_identity(
                    assessment.sources[0]
                ),
                status="attempt_started",
            )
            attempt_schema = json.loads(
                (SCHEMAS / "transcription_attempt.schema.json").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(attempt_schema).validate(attempt)


if __name__ == "__main__":
    unittest.main()
