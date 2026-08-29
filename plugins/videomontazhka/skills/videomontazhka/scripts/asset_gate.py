#!/usr/bin/env python3
"""Shared fail-closed helpers for semantic-approved asset writers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
VALIDATE_GATE = SCRIPT_DIR / "validate_gate.py"


class AssetGateError(RuntimeError):
    pass


def canonical_edit_dir(value: Path) -> Path:
    edit_dir = value.expanduser().resolve()
    if not edit_dir.is_dir():
        raise AssetGateError(f"edit directory not found: {edit_dir}")
    return edit_dir


def path_under_edit(edit_dir: Path, value: Path, label: str) -> Path:
    path = value.expanduser().resolve()
    try:
        path.relative_to(edit_dir)
    except ValueError as exc:
        raise AssetGateError(f"{label} must be under the canonical edit directory: {path}") from exc
    return path


def require_asset_gate(edit_dir: Path) -> None:
    command = [
        sys.executable,
        str(VALIDATE_GATE),
        "--edit-dir",
        str(edit_dir),
        "--phase",
        "asset",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        if not details:
            details = "validator returned no diagnostic output"
        raise AssetGateError(f"asset gate failed ({result.returncode}):\n{details}")
