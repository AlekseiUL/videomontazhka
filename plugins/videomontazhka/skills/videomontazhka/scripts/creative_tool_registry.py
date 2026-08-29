#!/usr/bin/env python3
"""Report Videomontazhka's local, no-paid-API creative capabilities.

The registry is deliberately read-only and offline. It never installs, updates,
or downloads a component. A capability is ready only when its executable/module
or pinned runtime record can be verified locally.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_paths import (  # noqa: E402
    CREATIVE_BROWSER_RUNTIME,
    CREATIVE_PYTHON_RUNTIME,
    HYPERFRAMES_RUNTIME,
    MANIM_RUNTIME as DEFAULT_MANIM_RUNTIME,
    configured_python,
    venv_executable,
)


REGISTRY_VERSION = 1
SKILL_ROOT = SCRIPT_DIR.parent
HF_RUNTIME = HYPERFRAMES_RUNTIME
CREATIVE_RUNTIME = CREATIVE_BROWSER_RUNTIME
CREATIVE_MANIFEST = CREATIVE_RUNTIME / "RUNTIME_MANIFEST.json"
GSAP_DIST = HF_RUNTIME / "node_modules" / "gsap" / "dist"
MANIM_RUNTIME = DEFAULT_MANIM_RUNTIME
MANIM_CLI = venv_executable(MANIM_RUNTIME, "manim")
MANIM_MANIFEST = MANIM_RUNTIME / "RUNTIME_MANIFEST.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


def python_module(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def external_python_modules(python: Path, modules: tuple[str, ...]) -> bool:
    if not python.is_file():
        return False
    expression = ";".join(f"import {module}" for module in modules)
    ok, _ = command([str(python), "-c", expression])
    return ok


def local_manifest() -> tuple[bool, dict[str, Any] | None, str | None]:
    if not CREATIVE_MANIFEST.is_file():
        return False, None, "pinned creative runtime manifest is absent"
    try:
        value = json.loads(CREATIVE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, None, f"cannot read creative runtime manifest: {exc}"
    if not isinstance(value, dict) or value.get("version") != 1:
        return False, None, "creative runtime manifest is not canonical v1"
    package_lock = CREATIVE_RUNTIME / "package-lock.json"
    recorded_lock = value.get("packages", {}).get("package_lock_sha256") if isinstance(value.get("packages"), dict) else None
    if not package_lock.is_file() or recorded_lock != sha256(package_lock):
        return False, value, "creative runtime package-lock hash is stale"
    return True, value, None


def manim_runtime_ready() -> tuple[bool, dict[str, Any] | None, str | None]:
    if not MANIM_CLI.is_file() or not MANIM_MANIFEST.is_file():
        return False, None, "pinned Manim runtime or manifest is absent"
    try:
        manifest = json.loads(MANIM_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, None, f"cannot read Manim runtime manifest: {exc}"
    if (
        not isinstance(manifest, dict)
        or manifest.get("type") != "sprut_manim_runtime"
        or manifest.get("manim_version") != "0.20.1"
        or manifest.get("machine") != "arm64"
        or manifest.get("runtime") != str(MANIM_RUNTIME)
        or manifest.get("cli") != str(MANIM_CLI.resolve())
    ):
        return False, manifest if isinstance(manifest, dict) else None, "Manim manifest contract is stale"
    requirements = SKILL_ROOT / "assets" / "manim-runtime-requirements.v1.txt"
    recorded_requirements = manifest.get("requirements")
    if (
        not requirements.is_file()
        or not isinstance(recorded_requirements, dict)
        or recorded_requirements.get("path") != "assets/manim-runtime-requirements.v1.txt"
        or recorded_requirements.get("sha256") != sha256(requirements)
    ):
        return False, manifest, "Manim requirements hash is stale"
    ok, output = command([str(MANIM_CLI), "--version"])
    if not ok or "0.20.1" not in output:
        return False, manifest, f"Manim CLI smoke failed: {output}"
    return True, manifest, None


def gsap_plugins() -> dict[str, Any]:
    files = {
        "split_text": "SplitText.min.js",
        "morph_svg": "MorphSVGPlugin.min.js",
        "draw_svg": "DrawSVGPlugin.min.js",
        "motion_path": "MotionPathPlugin.min.js",
        "scramble_text": "ScrambleTextPlugin.min.js",
        "physics_2d": "Physics2DPlugin.min.js",
        "custom_bounce": "CustomBounce.min.js",
        "custom_wiggle": "CustomWiggle.min.js",
        "flip": "Flip.min.js",
        "pixi_plugin": "PixiPlugin.min.js",
    }
    present = {name: (GSAP_DIST / filename).is_file() for name, filename in files.items()}
    return {
        "status": "ready" if all(present.values()) else "unavailable",
        "engine": "hyperframes_gsap",
        "capabilities": [
            "kinetic_typography", "svg_draw", "svg_morph", "motion_path",
            "deterministic_physics", "state_flip",
        ],
        "local_only": True,
        "paid_api": False,
        "license": "GSAP standard no-charge package license; not OSI open source",
        "details": present,
    }


def ffmpeg_capabilities() -> tuple[dict[str, Any], set[str]]:
    ffmpeg = shutil.which("ffmpeg")
    filters: set[str] = set()
    version = None
    if ffmpeg:
        ok, output = command([ffmpeg, "-hide_banner", "-filters"])
        if ok:
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_]+", parts[1]):
                    filters.add(parts[1])
        ok_version, version_output = command([ffmpeg, "-version"])
        if ok_version and version_output:
            version = version_output.splitlines()[0]
    wanted = {
        "frei0r", "vidstabdetect", "vidstabtransform", "rubberband",
        "sidechaincompress", "arnndn", "minterpolate", "xfade",
    }
    present = sorted(wanted & filters)
    return ({
        "status": "ready" if ffmpeg and {"frei0r", "sidechaincompress", "rubberband"} <= filters else "limited",
        "engine": "ffmpeg",
        "capabilities": [
            "curated_frei0r_fx", "dialogue_ducking", "pitch_time_design",
            "stabilization", "frame_interpolation", "audio_finishing",
        ],
        "local_only": True,
        "paid_api": False,
        "license": "local FFmpeg build; inspect full -version flags before redistribution",
        "path": ffmpeg,
        "version": version,
        "filters": present,
    }, filters)


def frei0r_plugins() -> list[str]:
    candidates = [Path("/opt/homebrew/lib/frei0r-1"), Path("/usr/local/lib/frei0r-1")]
    names: set[str] = set()
    for root in candidates:
        if not root.exists():
            continue
        for path in root.glob("*"):
            if path.is_file() or path.is_symlink():
                names.add(path.stem)
    allowlist = {
        "glitch0r", "rgbsplit0r", "elastic_scale", "pixeliz0r", "vertigo",
        "colorhalftone", "edgeglow", "filmgrain", "perspective", "softglow",
        "gateweave", "cartoon",
    }
    return sorted(names & allowlist)


def browser_runtime() -> dict[str, Any]:
    ok, manifest, error = local_manifest()
    packages = manifest.get("dependencies", {}) if ok and manifest else {}
    ready = sorted(
        name for name, item in packages.items()
        if isinstance(name, str) and isinstance(item, str) and item
    )
    capabilities: list[str] = []
    mapping = {
        "pixi.js": ["gpu_particles", "gpu_2d"],
        "pixi-filters": ["shockwave", "displacement", "rgb_split", "zoom_blur", "glow"],
        "rough-notation": ["rough_annotation"],
        "@lottiefiles/dotlottie-web": ["lottie_playback"],
        "lottie-web": ["lottie_playback"],
        "three": ["procedural_3d"],
        "gl-transitions": ["shader_transition_source"],
    }
    for name in ready:
        capabilities.extend(mapping.get(name, []))
    return {
        "status": "ready" if ok and {"pixi.js", "pixi-filters", "rough-notation"} <= set(ready) else "limited",
        "engine": "creative_browser_runtime",
        "capabilities": sorted(set(capabilities)),
        "local_only": True,
        "paid_api": False,
        "license": "per-package licenses and hashes recorded in RUNTIME_MANIFEST.json",
        "manifest": str(CREATIVE_MANIFEST),
        "packages": ready,
        "error": error,
    }


def local_adapter_status(script_name: str, capabilities: list[str]) -> dict[str, Any]:
    script = SCRIPT_DIR / script_name
    ok, output = command([sys.executable, str(script), "--describe-json"]) if script.is_file() else (False, "missing")
    try:
        discovery = json.loads(output) if ok else None
    except json.JSONDecodeError:
        discovery = None
    return {
        "status": "ready" if isinstance(discovery, dict) else "unavailable",
        "engine": script_name.removesuffix(".py"),
        "capabilities": capabilities if isinstance(discovery, dict) else [],
        "local_only": True,
        "paid_api": False,
        "license": "checked-in SPRUT adapter; underlying runtime licenses reported separately",
        "path": str(script),
        "error": None if isinstance(discovery, dict) else output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline SPRUT creative capability registry")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--require", action="append", default=[], help="require a capability id")
    args = parser.parse_args()

    ffmpeg, _ = ffmpeg_capabilities()
    audio_python = configured_python()
    audio_ready = external_python_modules(audio_python, ("librosa", "soundfile"))
    py = {
        "status": "ready" if audio_ready else "limited",
        "engine": "python_audio_analysis",
        "capabilities": ["rhythm_map", "onset_map"] if audio_ready else [],
        "local_only": True,
        "paid_api": False,
        "license": "librosa ISC; dependency licenses remain recorded by the Python environment",
        "python": str(audio_python),
    }
    creative_python = venv_executable(CREATIVE_PYTHON_RUNTIME, "python")
    scene_ready = external_python_modules(creative_python, ("scenedetect", "cv2"))
    scene = {
        "status": "ready" if scene_ready else "limited",
        "engine": "shot_aware_camera",
        "capabilities": ["shot_detection", "subpixel_camera"] if scene_ready else [],
        "local_only": True,
        "paid_api": False,
        "license": "PySceneDetect BSD-3-Clause; OpenCV Apache-2.0",
        "python": str(creative_python) if creative_python.is_file() else None,
    }
    vision_ready = Path("/System/Library/Frameworks/Vision.framework").exists()
    vision = {
        "status": "ready" if vision_ready else "unavailable",
        "engine": "apple_vision",
        "capabilities": ["presenter_tracking", "person_matte"] if vision_ready else [],
        "local_only": True,
        "paid_api": False,
        "license": "local macOS framework",
    }
    frei0r = frei0r_plugins()
    manim_ready, manim_manifest, manim_error = manim_runtime_ready()
    engines = {
        "gsap_motion": gsap_plugins(),
        "browser_fx": browser_runtime(),
        "ffmpeg_fx_audio": ffmpeg,
        "frei0r_recipes": {
            "status": "ready" if frei0r else "unavailable",
            "engine": "frei0r",
            "capabilities": ["curated_frei0r_fx"] if frei0r else [],
            "local_only": True,
            "paid_api": False,
            "license": "frei0r project GPL-compatible; audit individual plugin source when distributing",
            "allowlisted_plugins": frei0r,
        },
        "rhythm_analysis": py,
        "shot_aware_camera": scene,
        "apple_vision": vision,
        "manim": {
            "status": "ready" if manim_ready else "on_demand",
            "engine": "manim",
            "capabilities": ["technical_diagram"] if manim_ready else [],
            "local_only": True,
            "paid_api": False,
            "license": "MIT",
            "path": str(MANIM_CLI) if manim_ready else None,
            "manifest": str(MANIM_MANIFEST) if manim_ready else None,
            "equations_with_tex": bool(
                manim_manifest
                and isinstance(manim_manifest.get("capabilities"), dict)
                and manim_manifest["capabilities"].get("equations_with_tex")
            ),
            "error": manim_error,
        },
        "depth_parallax": {
            "status": "pilot_not_installed",
            "engine": "coreml_depth_anything_v2_small",
            "capabilities": [],
            "local_only": True,
            "paid_api": False,
            "license": "Small model only: Apache-2.0; larger variants excluded",
        },
        "gsap_creative_adapter": local_adapter_status(
            "scaffold_gsap_creative_effect.py",
            ["kinetic_typography", "svg_draw", "svg_morph", "motion_path", "state_flip"],
        ),
        "browser_creative_adapter": local_adapter_status(
            "scaffold_creative_browser_effect.py",
            ["gpu_2d", "gpu_particles", "rough_annotation", "lottie_playback", "procedural_3d"],
        ),
    }
    capability_ids = sorted({cap for item in engines.values() for cap in item.get("capabilities", [])})
    missing = sorted(set(args.require) - set(capability_ids))
    report = {
        "version": REGISTRY_VERSION,
        "type": "sprut_creative_tool_registry",
        "host": {"system": platform.system(), "machine": platform.machine()},
        "network_calls_made": 0,
        "paid_api_allowlist": ["elevenlabs"],
        "capability_ids": capability_ids,
        "engines": engines,
        "experimental_not_default": [
            "depth_parallax", "three_hero_scene", "gmic_treatment", "blender_hero",
            "gyroflow", "source_separation", "neural_denoise",
        ],
        "requirements": {"requested": args.require, "missing": missing, "ok": not missing},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("SPRUT creative registry")
        for name, item in engines.items():
            print(f"{item['status']:>20}  {name}: {', '.join(item.get('capabilities', [])) or 'no ready capabilities'}")
        if missing:
            print("MISSING: " + ", ".join(missing), file=sys.stderr)
    return 0 if not missing else 2


if __name__ == "__main__":
    raise SystemExit(main())
