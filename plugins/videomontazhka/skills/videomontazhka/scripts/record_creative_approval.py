#!/usr/bin/env python3
"""Record exact human approval for the current Gate 3 creative treatment."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from asset_gate import canonical_edit_dir
from creative_approval import (
    APPROVAL_NAME,
    TREATMENT_NAME,
    CreativeApprovalError,
    require_creative_approval,
    sha256_file,
)


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument("--quote", required=True, help="exact human approval text")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    quote = args.quote.strip()
    if not quote or quote != args.quote:
        parser.error("--quote must be an exact trimmed non-empty string")
    edit_dir = canonical_edit_dir(args.edit_dir)
    treatment = edit_dir / TREATMENT_NAME
    if not treatment.is_file() or treatment.is_symlink():
        raise CreativeApprovalError(f"creative treatment plan is missing: {treatment}")
    output = edit_dir / APPROVAL_NAME
    if output.exists() and not args.replace:
        raise CreativeApprovalError(
            f"creative approval already exists; use --replace only after a new human decision: {output}"
        )
    approval: dict[str, object] = {
        "version": 1,
        "type": "videomontazhka_creative_approval",
        "status": "approved",
        "creative_treatment_plan": TREATMENT_NAME,
        "creative_treatment_plan_sha256": sha256_file(treatment),
        "user_quote": quote,
    }
    atomic_json(output, approval)
    require_creative_approval(edit_dir)
    print(f"creative approval recorded: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CreativeApprovalError, OSError) as exc:
        print(f"record_creative_approval: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
