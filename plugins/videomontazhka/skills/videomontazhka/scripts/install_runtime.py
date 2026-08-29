#!/usr/bin/env python3
"""Explicitly install or offline-verify Videomontazhka's base Python runtime.

``--verify-only`` never performs a network call. ``--install`` is the only mode
that may resolve packages, and it installs exactly the skill's requirements
into a new isolated environment. Existing targets are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_paths import PYTHON_RUNTIME, venv_executable  # noqa: E402


SKILL_ROOT = SCRIPT_DIR.parent
REQUIREMENTS = SKILL_ROOT / "requirements.txt"
DEFAULT_RUNTIME = PYTHON_RUNTIME
REQUIRED_IMPORTS = ("PIL", "numpy", "requests")
REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(.*)$")
MINIMUM_PYTHON = (3, 11)


class RuntimeInstallError(RuntimeError):
    pass


def ensure_supported_python(version_info: tuple[int, ...] | None = None) -> None:
    observed = tuple((version_info or sys.version_info)[:2])
    if observed < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        actual = ".".join(str(part) for part in observed)
        raise RuntimeInstallError(
            f"Python {required}+ is required; installer is running under Python {actual}. "
            "Invoke this script with a supported interpreter (for example python3.12)."
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeInstallError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed


def requirement_names() -> list[str]:
    names: list[str] = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = REQUIREMENT_RE.fullmatch(line)
        if not match:
            raise RuntimeInstallError(f"unsupported requirement syntax: {raw!r}")
        names.append(match.group(1))
    if not names or len(names) != len(set(name.lower() for name in names)):
        raise RuntimeInstallError("requirements must contain a non-empty unique package set")
    return names


def requirement_constraints() -> dict[str, tuple[tuple[str, str], ...]]:
    """Parse the deliberately small PEP 440 subset used by requirements.txt."""

    constraints: dict[str, tuple[tuple[str, str], ...]] = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = REQUIREMENT_RE.fullmatch(line)
        if not match:
            raise RuntimeInstallError(f"unsupported requirement syntax: {raw!r}")
        clauses: list[tuple[str, str]] = []
        suffix = match.group(2).replace(" ", "")
        for item in filter(None, suffix.split(",")):
            spec = re.fullmatch(r"(==|!=|>=|<=|>|<)([0-9]+(?:\.[0-9]+)*)", item)
            if not spec:
                raise RuntimeInstallError(f"unsupported version constraint: {item!r}")
            clauses.append((spec.group(1), spec.group(2)))
        constraints[match.group(1)] = tuple(clauses)
    return constraints


def _release_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^[vV]?([0-9]+(?:\.[0-9]+)*)", value)
    if not match:
        raise RuntimeInstallError(f"cannot compare installed version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def _satisfies(installed: str, clauses: tuple[tuple[str, str], ...]) -> bool:
    current = _release_tuple(installed)
    for operator, required_text in clauses:
        required = _release_tuple(required_text)
        width = max(len(current), len(required))
        left = current + (0,) * (width - len(current))
        right = required + (0,) * (width - len(required))
        comparisons = {
            "==": left == right,
            "!=": left != right,
            ">=": left >= right,
            "<=": left <= right,
            ">": left > right,
            "<": left < right,
        }
        if not comparisons[operator]:
            return False
    return True


def runtime_python(runtime: Path) -> Path:
    return venv_executable(runtime.expanduser().resolve(strict=False), "python")


def inspect(runtime: Path) -> dict[str, Any]:
    target = runtime.expanduser().resolve(strict=False)
    python = runtime_python(target)
    if not python.is_file():
        raise RuntimeInstallError(f"runtime Python is missing: {python}")
    names = requirement_names()
    probe_source = (
        "import importlib.metadata as m,json,platform,sys;"
        f"names={names!r};"
        "print(json.dumps({'base_prefix':sys.base_prefix,'machine':platform.machine(),"
        "'packages':{name:m.version(name) for name in names},"
        "'prefix':sys.prefix,'python_version':platform.python_version()},sort_keys=True))"
    )
    probe = run([str(python), "-c", probe_source])
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeInstallError("runtime metadata probe returned invalid JSON") from exc
    if payload.get("prefix") == payload.get("base_prefix"):
        raise RuntimeInstallError("runtime is not an isolated virtual environment")

    imports = run([str(python), "-c", ";".join(f"import {name}" for name in REQUIRED_IMPORTS)])
    if imports.stdout.strip() or imports.stderr.strip():
        # Imports are allowed to be noisy, but recording no output keeps the
        # verification result deterministic and avoids leaking local details.
        pass
    packages = payload.get("packages")
    if not isinstance(packages, dict) or set(packages) != set(names):
        raise RuntimeInstallError("installed package inventory differs from requirements")
    constraints = requirement_constraints()
    violations = {
        name: str(packages[name])
        for name in names
        if not _satisfies(str(packages[name]), constraints[name])
    }
    if violations:
        raise RuntimeInstallError(f"installed package versions violate requirements: {violations}")
    return {
        "version": 1,
        "type": "videomontazhka_python_runtime",
        "runtime": str(target),
        "python": str(Path(os.path.abspath(python))),
        "python_version": payload.get("python_version"),
        "system": platform.system(),
        "machine": payload.get("machine"),
        "requirements": {
            "path": "requirements.txt",
            "sha256": sha256(REQUIREMENTS),
        },
        "packages": [
            {"name": name, "version": str(packages[name])}
            for name in sorted(names, key=str.lower)
        ],
        "imports": list(REQUIRED_IMPORTS),
        "policy": {
            "isolated_product_runtime": True,
            "network_required_for_verification": False,
            "project_install_prohibited": True,
        },
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
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
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def verify_manifest(runtime: Path, observed: dict[str, Any]) -> None:
    manifest = runtime.expanduser().resolve(strict=False) / "RUNTIME_MANIFEST.json"
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeInstallError(f"runtime manifest is missing or invalid: {manifest}") from exc
    if recorded != observed:
        raise RuntimeInstallError("runtime manifest is stale or differs from the installed environment")


def _safe_new_target(runtime: Path) -> Path:
    target = runtime.expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if target in {Path(target.anchor), home} or len(target.parts) < 4:
        raise RuntimeInstallError(f"refusing unsafe runtime target: {target}")
    if target.exists():
        raise RuntimeInstallError(f"runtime already exists and was left untouched: {target}")
    return target


def install(runtime: Path, *, prefer_uv: bool = True) -> dict[str, Any]:
    target = _safe_new_target(runtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = True
    try:
        uv = shutil.which("uv") if prefer_uv else None
        if uv:
            run([uv, "venv", "--python", sys.executable, str(target)])
            run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(runtime_python(target)),
                    "--requirement",
                    str(REQUIREMENTS),
                ]
            )
        else:
            run([sys.executable, "-m", "venv", str(target)])
            run(
                [
                    str(runtime_python(target)),
                    "-m",
                    "pip",
                    "install",
                    "--requirement",
                    str(REQUIREMENTS),
                ]
            )
        observed = inspect(target)
        atomic_json(target / "RUNTIME_MANIFEST.json", observed)
        verify_manifest(target, observed)
        return observed
    except Exception:
        if created and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        raise


def main() -> int:
    ensure_supported_python()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--stdlib-venv",
        action="store_true",
        help="use stdlib venv and pip instead of uv during explicit installation",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--install",
        action="store_true",
        help="create a new runtime; this mode may access package indexes",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="offline verification; never installs or downloads anything",
    )
    args = parser.parse_args()

    if args.verify_only and args.stdlib_venv:
        parser.error("--stdlib-venv applies only to --install")
    observed = (
        install(args.runtime, prefer_uv=not args.stdlib_venv)
        if args.install
        else inspect(args.runtime)
    )
    if args.verify_only:
        verify_manifest(args.runtime, observed)
    result = {
        "status": "PASS",
        "mode": "install" if args.install else "verify-only",
        "network_calls_made": 0 if args.verify_only else "package-installer-controlled",
        "runtime": observed["runtime"],
        "python": observed["python"],
        "packages": observed["packages"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"PASS {result['mode']} {result['runtime']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeInstallError) as exc:
        print(f"videomontazhka-runtime: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
