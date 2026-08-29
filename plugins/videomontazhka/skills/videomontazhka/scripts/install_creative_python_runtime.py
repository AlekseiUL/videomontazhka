#!/usr/bin/env python3
"""Install or verify Videomontazhka's isolated shot-analysis Python runtime.

The runtime is deliberately isolated from other tools so OpenCV and
PySceneDetect cannot upgrade or downgrade unrelated media packages.
Verification is offline. Installation uses the exact pinned requirements file
and must be requested explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_paths import CREATIVE_PYTHON_RUNTIME, venv_executable  # noqa: E402


SCRIPT = Path(__file__).resolve()
SKILL_ROOT = SCRIPT.parent.parent
REQUIREMENTS = SKILL_ROOT / "assets" / "creative-python-requirements.v1.txt"
DEFAULT_RUNTIME = CREATIVE_PYTHON_RUNTIME
EXPECTED = {
    "click": "8.2.1",
    "numpy": "2.2.6",
    "opencv-python-headless": "4.12.0.88",
    "platformdirs": "4.11.2",
    "scenedetect": "0.6.7.1",
    "tqdm": "4.70.0",
}
LICENSES = {
    "click": "BSD-3-Clause",
    "numpy": "BSD-3-Clause",
    "opencv-python-headless": "Apache-2.0",
    "platformdirs": "MIT",
    "scenedetect": "BSD-3-Clause",
    "tqdm": "MPL-2.0 AND MIT",
}


class RuntimeErrorChecked(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeErrorChecked(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def runtime_python(runtime: Path) -> Path:
    return venv_executable(runtime.expanduser().resolve(), "python")


def inspect(runtime: Path) -> dict[str, Any]:
    python = runtime_python(runtime)
    if not python.is_file():
        raise RuntimeErrorChecked(f"runtime Python is missing: {python}")
    probe = r'''
import importlib.metadata as m, json, platform, sys
names = ["click", "numpy", "opencv-python-headless", "platformdirs", "scenedetect", "tqdm"]
print(json.dumps({
  "machine": platform.machine(),
  "python": platform.python_version(),
  "prefix": sys.prefix,
  "base_prefix": sys.base_prefix,
  "packages": {name: m.version(name) for name in names},
}, sort_keys=True))
'''
    try:
        payload = json.loads(run([str(python), "-c", probe]).stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeErrorChecked("runtime probe returned invalid JSON") from exc
    if payload.get("machine") != "arm64":
        raise RuntimeErrorChecked(f"creative Python must be arm64, found {payload.get('machine')!r}")
    if payload.get("prefix") == payload.get("base_prefix"):
        raise RuntimeErrorChecked("creative Python is not an isolated virtual environment")
    if payload.get("packages") != EXPECTED:
        raise RuntimeErrorChecked(
            f"creative Python package set changed: expected {EXPECTED}, found {payload.get('packages')}"
        )
    imports = run([str(python), "-c", "import cv2, numpy, scenedetect; print(cv2.__version__)"])
    return {
        "version": 1,
        "type": "sprut_creative_python_runtime",
        "runtime": str(runtime.expanduser().resolve()),
        "python": str(python.resolve()),
        "python_version": payload["python"],
        "machine": payload["machine"],
        "packages": [
            {"name": name, "version": EXPECTED[name], "license": LICENSES[name]}
            for name in sorted(EXPECTED)
        ],
        "requirements": {
            # Keep the recorded identity portable between the development copy
            # and the installed Codex skill.  The bytes, not either checkout's
            # absolute path, are the runtime contract.
            "path": "assets/creative-python-requirements.v1.txt",
            "sha256": sha256(REQUIREMENTS),
        },
        "smoke": {
            "imports": ["cv2", "numpy", "scenedetect"],
            "opencv_version": imports.stdout.strip(),
            "status": "PASS",
        },
        "policy": {
            "local_only_after_install": True,
            "network_required_for_analysis": False,
            "isolated_product_runtime": True,
            "editorial_use": "detect shot resets; never cut or add camera motion solely from detector output",
        },
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(path)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def verify_manifest(runtime: Path, observed: dict[str, Any]) -> None:
    path = runtime.expanduser().resolve() / "RUNTIME_MANIFEST.json"
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeErrorChecked(f"runtime manifest is missing or invalid: {path}") from exc
    if recorded != observed:
        raise RuntimeErrorChecked("creative Python runtime manifest is stale or does not match the installed runtime")


def install(runtime: Path) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeErrorChecked("this pinned runtime is restricted to Apple Silicon macOS")
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).is_file():
        raise RuntimeErrorChecked("uv is required for the isolated pinned install")
    runtime = runtime.expanduser().resolve()
    if runtime.exists():
        raise RuntimeErrorChecked(f"runtime already exists and was left untouched: {runtime}")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    run([uv, "venv", "--python", "3.12", str(runtime)])
    run([uv, "pip", "install", "--python", str(runtime_python(runtime)), "--requirement", str(REQUIREMENTS)])
    atomic_json(runtime / "RUNTIME_MANIFEST.json", inspect(runtime))


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or verify isolated SPRUT creative Python")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--record-manifest", action="store_true")
    args = parser.parse_args()
    if args.install:
        install(args.runtime)
    observed = inspect(args.runtime)
    if args.record_manifest:
        atomic_json(args.runtime.expanduser().resolve() / "RUNTIME_MANIFEST.json", observed)
    else:
        verify_manifest(args.runtime, observed)
    print(json.dumps({"status": "PASS", "runtime": observed["runtime"], "packages": EXPECTED}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeErrorChecked) as exc:
        print(f"creative-python-runtime: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
