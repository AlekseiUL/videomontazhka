#!/usr/bin/env python3
"""Offline preflight for the Videomontazhka toolchain.

The check never calls a network service and never prints secret values.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_paths import (  # noqa: E402
    APP_HOME,
    CACHE_HOME,
    HYPERFRAMES_RUNTIME,
    MANIM_RUNTIME,
    PYTHON_RUNTIME,
    RUNTIME_ROOT,
    configured_python,
    settings_env_file,
    venv_executable,
)


REQUIRED_FILTERS = {
    "afade",
    "adelay",
    "aformat",
    "alimiter",
    "amix",
    "apad",
    "aresample",
    "asetpts",
    "atrim",
    "crop",
    "eq",
    "format",
    "fps",
    "geq",
    "highpass",
    "loudnorm",
    "overlay",
    "pad",
    "scale",
    "setparams",
    "setpts",
    "split",
    "tonemap",
    "tpad",
    "trim",
}
CONDITIONAL_FILTERS = {
    "burned_subtitles": {"subtitles"},
    "hdr_tonemapping": {"zscale"},
}
OPTIONAL_FILTERS = {
    "acompressor",
    "adeclick",
    "afftdn",
    "alimiter",
    "arnndn",
    "deesser",
    "dialoguenhance",
    "ebur128",
    "vidstabdetect",
    "vidstabtransform",
    "xfade",
    "zoompan",
}


def command_output(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as exc:
        return False, str(exc)
    text = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, text


def python_runtime_status(python: Path) -> dict[str, Any]:
    """Prove interpreter identity and compatibility instead of trusting a path."""

    command = [
        str(python),
        "-I",
        "-c",
        (
            "import json,sys; "
            "print(json.dumps({'probe':'videomontazhka-python-v1',"
            "'version_info':list(sys.version_info[:3]),'version':sys.version}))"
        ),
    ]
    status: dict[str, Any] = {"ok": False, "path": str(python)}
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status["error"] = f"not a compatible Python: probe failed: {exc}"
        return status
    if completed.returncode != 0:
        status["error"] = f"not a compatible Python: probe exited {completed.returncode}"
        return status
    try:
        payload = json.loads(completed.stdout)
        version_info = payload["version_info"]
    except (json.JSONDecodeError, KeyError, TypeError):
        status["error"] = "not a compatible Python: identity probe returned invalid JSON"
        return status
    if (
        not isinstance(payload, dict)
        or payload.get("probe") != "videomontazhka-python-v1"
        or not isinstance(version_info, list)
        or len(version_info) != 3
        or not all(isinstance(value, int) for value in version_info)
        or tuple(version_info[:2]) < (3, 11)
    ):
        status["error"] = "not a compatible Python: version 3.11 or newer is required"
        return status
    status.update(
        ok=True,
        version_info=version_info,
        version=str(payload.get("version", "")),
    )
    return status


def first_line(command: list[str]) -> str | None:
    ok, output = command_output(command)
    if not ok or not output:
        return None
    return output.splitlines()[0].strip()


def find_hyperframes_cli() -> Path | None:
    """Find the pinned local HyperFrames CLI without making a network call."""

    candidates: list[Path] = []
    configured = os.environ.get("VIDEOMONTAZHKA_HYPERFRAMES_BIN") or os.environ.get(
        "SPRUT_HYPERFRAMES_BIN"
    )
    if configured:
        candidates.append(Path(configured).expanduser())
    on_path = shutil.which("hyperframes")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(HYPERFRAMES_RUNTIME / "node_modules" / ".bin" / "hyperframes")
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    return None


def env_file_has_key(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*ELEVENLABS_API_KEY\s*=\s*\S+", line):
                return True
    except OSError:
        return False
    return False


def creative_registry_report() -> tuple[dict[str, Any] | None, str | None]:
    """Read the local creative registry without installing or contacting anything."""

    registry = Path(__file__).resolve().parent / "creative_tool_registry.py"
    if not registry.is_file():
        return None, f"missing registry: {registry}"
    ok, output = command_output([sys.executable, str(registry), "--json"])
    if not ok:
        return None, output or "creative registry failed"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        return None, f"creative registry returned invalid JSON: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("engines"), dict):
        return None, "creative registry contract is incomplete"
    return payload, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Videomontazhka dependency check")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a readable report")
    args = parser.parse_args()

    required: dict[str, Any] = {}
    optional: dict[str, Any] = {}
    warnings: list[str] = []

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    required["ffmpeg"] = {"ok": bool(ffmpeg), "path": ffmpeg, "version": first_line([ffmpeg, "-version"]) if ffmpeg else None}
    required["ffprobe"] = {"ok": bool(ffprobe), "path": ffprobe, "version": first_line([ffprobe, "-version"]) if ffprobe else None}

    found_filters: set[str] = set()
    if ffmpeg:
        ok, output = command_output([ffmpeg, "-hide_banner", "-filters"])
        if ok:
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_]+", parts[1]):
                    found_filters.add(parts[1])
    missing_filters = sorted(REQUIRED_FILTERS - found_filters)
    required["ffmpeg_filters"] = {
        "ok": not missing_filters,
        "missing": missing_filters,
        "optional_available": sorted(OPTIONAL_FILTERS & found_filters),
    }
    optional["ffmpeg_conditional_capabilities"] = {
        name: {
            "ready": not (filters - found_filters),
            "missing": sorted(filters - found_filters),
        }
        for name, filters in CONDITIONAL_FILTERS.items()
    }

    runtime_python = configured_python()
    managed_python = runtime_python.is_relative_to(PYTHON_RUNTIME)
    runtime_installer = SCRIPT_DIR / "install_runtime.py"
    required["python_runtime"] = python_runtime_status(runtime_python)
    required["python_runtime"].update(
        managed=managed_python,
        note="isolated product runtime" if managed_python else "current interpreter fallback",
        recommended_install_command=[sys.executable, str(runtime_installer), "--install"],
    )
    if not managed_python:
        warnings.append(
            "using the current Python interpreter; run scripts/install_runtime.py --install "
            "for a reproducible isolated runtime"
        )

    module_status: dict[str, bool] = {}
    module_python = str(runtime_python)
    for module in ("PIL", "numpy", "requests"):
        ok = False
        if required["python_runtime"]["ok"]:
            ok, _ = command_output([module_python, "-I", "-c", f"import {module}"])
        module_status[module] = ok
    required["python_modules"] = {"ok": all(module_status.values()), "modules": module_status}

    local_audio_modules: dict[str, bool] = {}
    for module in ("librosa", "soundfile"):
        ok, _ = command_output([module_python, "-c", f"import {module}"])
        local_audio_modules[module] = ok
    optional["python_audio_analysis"] = {
        "ready": all(local_audio_modules.values()),
        "modules": local_audio_modules,
        "note": "optional local analysis helpers; canonical audio processing uses FFmpeg",
    }

    key_in_env = bool(os.environ.get("ELEVENLABS_API_KEY"))
    key_file = settings_env_file()
    key_in_file = env_file_has_key(key_file)
    optional["elevenlabs_transcription"] = {
        "ready": key_in_env or key_in_file,
        "configured": key_in_env or key_in_file,
        "source": "environment" if key_in_env else ("product settings file" if key_in_file else None),
        "settings_file": str(key_file),
        "note": "optional paid provider; transcription approval is required before upload",
    }
    if key_file.exists():
        mode = stat.S_IMODE(key_file.stat().st_mode)
        if mode & 0o077:
            warnings.append("product .env is readable by group/others; recommended mode is 0600")

    node = shutil.which("node")
    node_version = first_line([node, "--version"]) if node else None
    node_major = None
    if node_version:
        match = re.match(r"^v?(\d+)", node_version)
        node_major = int(match.group(1)) if match else None
    hyperframes = find_hyperframes_cli()
    hyperframes_version = first_line([str(hyperframes), "--version"]) if hyperframes else None
    optional["hyperframes_local"] = {
        "ready": bool(node and node_major is not None and node_major >= 22 and hyperframes and ffmpeg),
        "installed": bool(hyperframes),
        "path": str(hyperframes) if hyperframes else None,
        "version": hyperframes_version,
        "node": node_version,
        "license": "Apache-2.0; local CLI only",
        "network_required_for_render": False,
    }
    manim_runtime = MANIM_RUNTIME
    manim_cli = venv_executable(manim_runtime, "manim")
    optional["manim_local"] = {
        "ready": manim_cli.is_file(),
        "path": str(manim_cli) if manim_cli.is_file() else None,
        "runtime_manifest": str(manim_runtime / "RUNTIME_MANIFEST.json"),
        "equations_with_tex": bool(shutil.which("latex") or shutil.which("pdflatex")),
        "license": "MIT",
    }
    tracker_source = Path(__file__).resolve().parent / "track_presenter.m"
    segmenter_source = Path(__file__).resolve().parent / "segment_person.m"
    person_matte_wrapper = Path(__file__).resolve().parent / "person_matte.py"
    optional["apple_vision"] = {
        "ready": bool(
            Path("/System/Library/Frameworks/Vision.framework").exists()
            and tracker_source.is_file()
            and segmenter_source.is_file()
            and person_matte_wrapper.is_file()
            and shutil.which("xcrun")
        ),
        "framework": "/System/Library/Frameworks/Vision.framework",
        "tracker_source": str(tracker_source),
        "person_segmenter_source": str(segmenter_source),
        "person_matte_wrapper": str(person_matte_wrapper),
        "local_processing": True,
    }
    optional["mediapipe"] = {
        "ready": importlib.util.find_spec("mediapipe") is not None,
        "license": "Apache-2.0",
        "local_processing": True,
    }
    yt_dlp = shutil.which("yt-dlp")
    managed_yt_dlp = venv_executable(PYTHON_RUNTIME, "yt-dlp")
    optional["yt_dlp"] = {
        "ready": bool(yt_dlp or managed_yt_dlp.is_file()),
        "path": yt_dlp or (str(managed_yt_dlp) if managed_yt_dlp.is_file() else None),
    }

    creative_registry, creative_error = creative_registry_report()
    creative_engines = creative_registry.get("engines", {}) if creative_registry else {}
    ready_engines = sorted(
        name for name, item in creative_engines.items()
        if isinstance(item, dict) and item.get("status") == "ready"
    )
    optional["creative_arsenal"] = {
        "ready": bool(
            creative_registry
            and {
                "browser_fx", "browser_creative_adapter", "gsap_motion",
                "gsap_creative_adapter", "shot_aware_camera", "ffmpeg_fx_audio",
                "manim",
            }
            <= set(ready_engines)
        ),
        "registry": str(Path(__file__).resolve().parent / "creative_tool_registry.py"),
        "ready_engines": ready_engines,
        "capability_ids": creative_registry.get("capability_ids", []) if creative_registry else [],
        "paid_api_allowlist": creative_registry.get("paid_api_allowlist", []) if creative_registry else [],
        "network_calls_made": creative_registry.get("network_calls_made") if creative_registry else None,
        "error": creative_error,
        "note": "meaning router may choose only capabilities reported ready; experimental tools remain opt-in",
    }

    required_ok = all(bool(item.get("ok")) for item in required.values())
    report = {
        "required_ok": required_ok,
        "paid_api_allowlist": ["elevenlabs"],
        "network_calls_made": 0,
        "paths": {
            "application_home": str(APP_HOME),
            "runtime_root": str(RUNTIME_ROOT),
            "cache_home": str(CACHE_HOME),
        },
        "required": required,
        "optional": optional,
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Videomontazhka doctor: {'PASS' if required_ok else 'FAIL'}")
        for name, item in required.items():
            print(f"  {'OK' if item.get('ok') else 'MISSING'}  {name}")
        print("Optional local features:")
        for name, item in optional.items():
            state = item.get("ready", item.get("installed", False))
            print(f"  {'READY' if state else 'ON DEMAND'}  {name}")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
