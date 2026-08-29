#!/usr/bin/env python3
"""Validate the canonical human Gate 3 approval before creative production."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from asset_gate import canonical_edit_dir, path_under_edit


APPROVAL_NAME = "creative_approval.json"
TREATMENT_NAME = "creative_treatment_plan.json"
MAX_JSON_BYTES = 16 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_FIELDS = {
    "version",
    "type",
    "status",
    "creative_treatment_plan",
    "creative_treatment_plan_sha256",
    "user_quote",
}


class CreativeApprovalError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise CreativeApprovalError(f"{label} is missing or not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise CreativeApprovalError(f"{label} has an invalid size: {resolved}")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CreativeApprovalError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CreativeApprovalError(f"{label} must be a JSON object")
    return value


def require_creative_approval(edit_dir: Path) -> dict[str, Any]:
    """Fail closed unless Gate 3 approves the exact current treatment bytes."""

    canonical = canonical_edit_dir(edit_dir)
    treatment_path = path_under_edit(
        canonical, canonical / TREATMENT_NAME, "creative treatment plan"
    )
    approval_path = path_under_edit(
        canonical, canonical / APPROVAL_NAME, "creative approval"
    )
    treatment = _load_object(treatment_path, "creative treatment plan")
    approval = _load_object(approval_path, "creative approval")
    if set(approval) != EXPECTED_FIELDS:
        raise CreativeApprovalError(
            f"creative approval fields must be exactly {sorted(EXPECTED_FIELDS)}"
        )
    if approval.get("version") != 1 or approval.get("type") != "videomontazhka_creative_approval":
        raise CreativeApprovalError("creative approval identity is invalid")
    if approval.get("status") != "approved":
        raise CreativeApprovalError("creative approval status is not approved")
    quote = approval.get("user_quote")
    if not isinstance(quote, str) or not quote.strip() or quote != quote.strip():
        raise CreativeApprovalError("creative approval requires an exact non-empty user quote")
    if treatment.get("version") != 1 or treatment.get("type") != "sprut_creative_treatment_plan":
        raise CreativeApprovalError("creative treatment plan identity is invalid")
    reference = approval.get("creative_treatment_plan")
    if reference != TREATMENT_NAME:
        raise CreativeApprovalError(
            f"creative approval must reference canonical {TREATMENT_NAME}"
        )
    expected = approval.get("creative_treatment_plan_sha256")
    if not isinstance(expected, str) or SHA256_RE.fullmatch(expected) is None:
        raise CreativeApprovalError("creative approval treatment SHA-256 is invalid")
    if sha256_file(treatment_path) != expected:
        raise CreativeApprovalError("creative treatment plan changed after human approval")
    return approval


__all__ = [
    "APPROVAL_NAME",
    "CreativeApprovalError",
    "TREATMENT_NAME",
    "require_creative_approval",
    "sha256_file",
]
