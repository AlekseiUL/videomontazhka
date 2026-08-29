#!/usr/bin/env python3
"""Record explicit, numeric, source-bound approval for paid transcription."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from transcription_preflight import (
    APPROVAL_NAME,
    MODEL_ID,
    PREFLIGHT_NAME,
    PROVIDER,
    UPLOAD_DISCLOSURE,
    PreflightError,
    approval_binding_payload,
    canonical_artifact_path,
    finite_cap,
    load_json_object,
    request_source_map,
)
from transcription_safety import (
    AttemptLedgerError,
    TranscriptionLockError,
    TranscriptionPathError,
    canonical_json_sha256,
    create_approval_anchor,
    external_preflight_was_consumed,
    read_attempt_ledger,
    project_transcription_lock,
    sha256_file,
    write_json_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind explicit upload acknowledgement and a numeric usage cap to a preflight"
    )
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument(
        "--max-billable-minutes",
        type=float,
        required=True,
        help="user-approved maximum uncached source-audio minutes",
    )
    parser.add_argument("--quote", required=True, help="exact user approval text")
    parser.add_argument(
        "--acknowledge-upload",
        action="store_true",
        help="record that the ElevenLabs upload/privacy disclosure was shown and accepted",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace the prior approval after a new explicit user decision",
    )
    args = parser.parse_args()

    edit_dir = args.edit_dir.expanduser().resolve()
    with project_transcription_lock(edit_dir):
        return record_approval(args, edit_dir)


def record_approval(args: argparse.Namespace, edit_dir: Path) -> int:
    if not edit_dir.is_dir():
        raise PreflightError(f"edit directory not found: {edit_dir}")
    if not args.acknowledge_upload:
        raise PreflightError(
            "refusing to approve: show the upload disclosure and pass --acknowledge-upload "
            "only after the user explicitly accepts it"
        )
    quote = args.quote.strip()
    if not quote:
        raise PreflightError("approval quote must be non-empty")
    cap = finite_cap(args.max_billable_minutes)

    preflight_path = canonical_artifact_path(
        edit_dir,
        None,
        PREFLIGHT_NAME,
        "transcription preflight",
    )
    preflight = load_json_object(preflight_path, "transcription preflight")
    if (
        preflight.get("version") != 1
        or preflight.get("schema_version") != "1.1.0"
        or preflight.get("type") != "transcription_preflight"
        or preflight.get("status") != "awaiting_explicit_approval"
        or not isinstance(preflight.get("preflight_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", preflight["preflight_id"]) is None
    ):
        raise PreflightError("transcription preflight has an unsupported version or status")
    if preflight.get("provider") != PROVIDER or preflight.get("model_id") != MODEL_ID:
        raise PreflightError("transcription preflight provider/model is unsupported")
    request = preflight.get("request")
    if not isinstance(request, dict):
        raise PreflightError("transcription preflight request is invalid")
    request_sha = canonical_json_sha256(request)
    if preflight.get("request_sha256") != request_sha:
        raise PreflightError("transcription preflight request binding is invalid")
    request_sources = request_source_map(request, "preflight request")
    approved_upload_source_ids = [
        source_id
        for source_id, source in request_sources.items()
        if source["will_upload"]
    ]
    privacy = preflight.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("disclosure") != UPLOAD_DISCLOSURE:
        raise PreflightError("transcription preflight upload disclosure is invalid")
    disclosure_sha = canonical_json_sha256(UPLOAD_DISCLOSURE)
    if privacy.get("disclosure_sha256") != disclosure_sha:
        raise PreflightError("transcription preflight disclosure binding is invalid")
    usage = preflight.get("usage_estimate")
    if not isinstance(usage, dict):
        raise PreflightError("transcription preflight usage estimate is invalid")
    estimate = finite_cap(
        usage.get("estimated_billable_minutes"),
        "preflight estimated billable minutes",
    )
    if estimate > cap + 1e-9:
        raise PreflightError(
            f"approved cap {cap:.6f} is below the preflight estimate of {estimate:.6f} minutes"
        )

    output = canonical_artifact_path(
        edit_dir,
        None,
        APPROVAL_NAME,
        "transcription approval",
    )
    preflight_digest = sha256_file(preflight_path)
    if external_preflight_was_consumed(preflight_digest):
        raise PreflightError(
            "this preflight consumed an external source capability; create a new preflight "
            "before obtaining a new explicit approval"
        )
    if any(
        record.get("preflight_sha256") == preflight_digest
        and record.get("status") == "attempt_started"
        for record in read_attempt_ledger(edit_dir)
    ):
        raise PreflightError(
            "this preflight already consumed at least one source attempt; create a new "
            "preflight before obtaining a new explicit approval"
        )
    if output.exists() and not args.replace:
        raise PreflightError(
            f"approval already exists: {output}; use --replace only after a new explicit decision"
        )
    approval = {
        "version": 1,
        "schema_version": "1.1.0",
        "type": "transcription_approval",
        "status": "approved",
        "approval_id": uuid.uuid4().hex,
        "approval_nonce": secrets.token_hex(32),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "preflight_id": preflight["preflight_id"],
        "preflight_file": preflight_path.name,
        "preflight_sha256": preflight_digest,
        "request_sha256": request_sha,
        "provider": PROVIDER,
        "model_id": MODEL_ID,
        "max_billable_minutes": cap,
        "upload_disclosure_acknowledged": True,
        "disclosure_sha256": disclosure_sha,
        "user_quote": quote,
        "user_quote_sha256": canonical_json_sha256(quote),
        "approved_upload_source_ids": approved_upload_source_ids,
    }
    approval["binding_sha256"] = canonical_json_sha256(
        approval_binding_payload(approval)
    )
    create_approval_anchor(
        approval_id=approval["approval_id"],
        approval_nonce=approval["approval_nonce"],
        preflight_id=approval["preflight_id"],
        preflight_sha256=approval["preflight_sha256"],
        request_sha256=approval["request_sha256"],
        edit_dir=edit_dir,
    )
    write_json_atomic(output, approval)
    print(f"transcription approval recorded: {output}")
    print(f"approved maximum billable source audio: {cap:.6f} minutes")
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
        print(f"record_transcription_approval: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
