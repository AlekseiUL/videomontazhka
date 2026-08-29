#!/usr/bin/env python3
"""Install or verify Videomontazhka's isolated local Manim runtime.

The renderer is kept in the product's user-data directory, outside project
folders. Verification is offline and checks the exact top-level version,
architecture, import, CLI, and recorded requirements hash. Ordinary
architecture diagrams need no LaTeX; equation rendering remains unavailable
until a local TeX toolchain is present.
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

from runtime_paths import MANIM_RUNTIME, venv_executable  # noqa: E402


SCRIPT = Path(__file__).resolve()
SKILL_ROOT = SCRIPT.parent.parent
REQUIREMENTS = SKILL_ROOT / "assets" / "manim-runtime-requirements.v1.txt"
DEFAULT_RUNTIME = MANIM_RUNTIME
EXPECTED_VERSION = "0.20.1"


class ManimRuntimeError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ManimRuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def runtime_python(runtime: Path) -> Path:
    return venv_executable(runtime.expanduser().resolve(), "python")


def runtime_cli(runtime: Path) -> Path:
    return venv_executable(runtime.expanduser().resolve(), "manim")


def render_smoke(cli: Path) -> dict[str, Any]:
    """Render one local vector-only still; no TeX, browser, or network."""

    with tempfile.TemporaryDirectory(prefix="sprut-manim-smoke-") as temporary:
        root = Path(temporary)
        source = root / "smoke.py"
        source.write_text(
            "from manim import *\n"
            "class SprutSmoke(Scene):\n"
            "    def construct(self):\n"
            "        self.camera.background_color = '#070707'\n"
            "        self.add(Circle(color='#FF6A00').set_fill('#FF6A00', opacity=0.35))\n",
            encoding="utf-8",
        )
        media = root / "media"
        run(
            [
                str(cli),
                "-ql",
                "-s",
                "--disable_caching",
                "--media_dir",
                str(media),
                str(source),
                "SprutSmoke",
            ]
        )
        candidates = list(media.rglob("SprutSmoke*.png"))
        if len(candidates) != 1 or candidates[0].stat().st_size <= 0:
            raise ManimRuntimeError("Manim vector render smoke did not publish exactly one PNG")
        return {
            "status": "PASS",
            "mode": "vector_only_png",
            "network_calls_made": 0,
            "output_bytes": candidates[0].stat().st_size,
        }


def inspect(runtime: Path) -> dict[str, Any]:
    python = runtime_python(runtime)
    cli = runtime_cli(runtime)
    if not python.is_file() or not cli.is_file():
        raise ManimRuntimeError(f"Manim runtime is incomplete: {runtime.expanduser().resolve()}")
    probe = run(
        [
            str(python),
            "-c",
            (
                "import json,manim,platform,sys;"
                "print(json.dumps({'version':manim.__version__,'machine':platform.machine(),"
                "'python_version':platform.python_version(),"
                "'prefix':sys.prefix,'base_prefix':sys.base_prefix},sort_keys=True))"
            ),
        ]
    )
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise ManimRuntimeError("Manim import probe returned invalid JSON") from exc
    if payload.get("version") != EXPECTED_VERSION:
        raise ManimRuntimeError(
            f"expected Manim {EXPECTED_VERSION}, found {payload.get('version')!r}"
        )
    if payload.get("machine") != "arm64":
        raise ManimRuntimeError(f"Manim runtime must be arm64, found {payload.get('machine')!r}")
    if payload.get("prefix") == payload.get("base_prefix"):
        raise ManimRuntimeError("Manim is not running in an isolated virtual environment")
    version_output = run([str(cli), "--version"]).stdout.strip()
    if EXPECTED_VERSION not in version_output:
        raise ManimRuntimeError(f"Manim CLI version is unexpected: {version_output!r}")
    tex = shutil.which("latex") or shutil.which("pdflatex")
    rendered = render_smoke(cli)
    return {
        "version": 1,
        "type": "sprut_manim_runtime",
        "runtime": str(runtime.expanduser().resolve()),
        "python": str(python.resolve()),
        "cli": str(cli.resolve()),
        "manim_version": EXPECTED_VERSION,
        "python_version": str(payload["python_version"]),
        "machine": payload["machine"],
        "requirements": {
            "path": "assets/manim-runtime-requirements.v1.txt",
            "sha256": sha256(REQUIREMENTS),
        },
        "capabilities": {
            "architecture_diagrams": True,
            "vector_text_and_shapes": True,
            "equations_with_tex": bool(tex),
            "tex_path": tex,
        },
        "smoke": {"import": "PASS", "cli": "PASS", "render": rendered},
        "policy": {
            "local_only_after_install": True,
            "network_required_for_render": False,
            "isolated_product_runtime": True,
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
    manifest = runtime.expanduser().resolve() / "RUNTIME_MANIFEST.json"
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManimRuntimeError(f"Manim runtime manifest is missing or invalid: {manifest}") from exc
    if recorded != observed:
        raise ManimRuntimeError("Manim runtime manifest is stale or does not match the runtime")


def install(runtime: Path) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ManimRuntimeError("this pinned runtime is restricted to Apple Silicon macOS")
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    pkg_config = shutil.which("pkg-config")
    if not Path(uv).is_file() or not pkg_config:
        raise ManimRuntimeError("uv and pkg-config are required for the pinned Manim install")
    runtime = runtime.expanduser().resolve()
    if runtime.exists():
        raise ManimRuntimeError(f"runtime already exists and was left untouched: {runtime}")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    run([uv, "venv", "--python", "3.12", str(runtime)])
    environment = dict(os.environ)
    environment["PKG_CONFIG_PATH"] = "/opt/homebrew/lib/pkgconfig:/opt/homebrew/share/pkgconfig"
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(runtime_python(runtime)),
            "--requirement",
            str(REQUIREMENTS),
        ],
        env=environment,
    )
    atomic_json(runtime / "RUNTIME_MANIFEST.json", inspect(runtime))


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or verify isolated SPRUT Manim")
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
    print(json.dumps({"status": "PASS", "runtime": observed["runtime"], "capabilities": observed["capabilities"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ManimRuntimeError) as exc:
        print(f"manim-runtime: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
