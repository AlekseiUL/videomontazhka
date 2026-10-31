#!/usr/bin/env python3
"""Link this checkout into Codex without replacing any existing entry."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def install_skill(home: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    if not (source / "SKILL.md").is_file():
        raise ValueError("SKILL.md is missing; use a complete checkout.")
    destination = home / ".codex" / "skills" / "videomontazhka"
    for directory in (destination.parent.parent, destination.parent):
        if directory.is_symlink():
            raise ValueError(f"Refusing symlinked install directory: {directory}. Inspect it manually first.")
        directory.mkdir(exist_ok=True)
    try:
        # symlink creation fails atomically if any entry already owns this name.
        # Never unlink/replace it, even if it appeared after our checks.
        destination.symlink_to(source, target_is_directory=True)
    except FileExistsError:
        if destination.is_symlink():
            try:
                if destination.resolve(strict=True) == source:
                    return destination
            except (OSError, RuntimeError):
                pass
        raise ValueError(
            f"Refusing to replace existing entry: {destination}. "
            "Inspect it and move it to a backup location yourself if you want "
            "to replace it, then rerun this command. Nothing was overwritten."
        ) from None
    return destination


def main() -> int:
    home = os.environ.get("HOME")
    if not home or not Path(home).is_absolute() or not Path(home).is_dir():
        print("HOME must name an existing absolute directory.", file=sys.stderr)
        return 1
    try:
        destination = install_skill(Path(home))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Skill ready: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
