#!/usr/bin/env python3
"""Report large intermediates without deleting anything."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


GROUPS = ("cache", "work", "clips_graded", "clips_preview", "clips_draft", "verify")


def directory_size(path: Path) -> tuple[int, int]:
    total = 0
    files = 0
    if not path.exists():
        return total, files
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
                files += 1
            except OSError:
                pass
    return total, files


def human_size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run report of reclaimable edit intermediates")
    parser.add_argument("edit_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    edit = args.edit_dir.expanduser().resolve()
    if not edit.is_dir():
        raise SystemExit(f"edit directory not found: {edit}")

    rows = []
    for name in GROUPS:
        path = edit / name
        size, files = directory_size(path)
        rows.append({"group": name, "path": str(path), "bytes": size, "files": files})
    report = {
        "mode": "report_only",
        "deleted": False,
        "total_reclaimable_bytes": sum(row["bytes"] for row in rows),
        "groups": rows,
        "protected": [
            "source media", "semantic_plan.json", "approval.json", "EDLs",
            "deliverable preview/final media", "render_manifest_<artifact-key>_*.json",
            "preview_approval_<artifact-key>.json", "release_manifest_<artifact-key>.json",
        ],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Cleanup report only; nothing was deleted.")
        for row in rows:
            print(f"  {row['group']:<16} {human_size(row['bytes']):>10}  {row['files']:>6} files")
        print(f"  {'TOTAL':<16} {human_size(report['total_reclaimable_bytes']):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
