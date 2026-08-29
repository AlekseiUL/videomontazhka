#!/usr/bin/env python3
"""Canonical deliverable names and renderer/font provenance for SPRUT."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any


RENDERER_VERSION = "sprut-render-6"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
IDENTITY_FILES = {
    "scripts/render_edl.py": SCRIPT_DIR / "render_edl.py",
    "scripts/validate_gate.py": SCRIPT_DIR / "validate_gate.py",
    "scripts/schema_check.py": SCRIPT_DIR / "schema_check.py",
    "assets/edl.schema.json": SKILL_DIR / "assets" / "edl.schema.json",
    "assets/semantic-plan.schema.json": SKILL_DIR / "assets" / "semantic-plan.schema.json",
    "scripts/artifact_provenance.py": SCRIPT_DIR / "artifact_provenance.py",
    "scripts/record_approval.py": SCRIPT_DIR / "record_approval.py",
    "scripts/record_preview_approval.py": SCRIPT_DIR / "record_preview_approval.py",
    "scripts/qa_release.py": SCRIPT_DIR / "qa_release.py",
    "scripts/validate_caption_fit.py": SCRIPT_DIR / "validate_caption_fit.py",
    "scripts/qa_all_cuts.py": SCRIPT_DIR / "qa_all_cuts.py",
    "scripts/verify_exact_boundary_frames.py": SCRIPT_DIR / "verify_exact_boundary_frames.py",
    "scripts/visual_asset_provenance.py": SCRIPT_DIR / "visual_asset_provenance.py",
    "scripts/render_motion_card.py": SCRIPT_DIR / "render_motion_card.py",
    "scripts/record_visual_asset.py": SCRIPT_DIR / "record_visual_asset.py",
    "scripts/creative_tool_registry.py": SCRIPT_DIR / "creative_tool_registry.py",
    "scripts/creative_tool_router.py": SCRIPT_DIR / "creative_tool_router.py",
    "assets/creative-tool-router-map.v1.json": SKILL_DIR / "assets" / "creative-tool-router-map.v1.json",
    "schemas/creative_router_input.schema.json": SKILL_DIR / "schemas" / "creative_router_input.schema.json",
    "schemas/creative_decision.schema.json": SKILL_DIR / "schemas" / "creative_decision.schema.json",
    "scripts/compile_creative_treatment_plan.py": SCRIPT_DIR / "compile_creative_treatment_plan.py",
    "schemas/creative_treatment_plan.schema.json": SKILL_DIR / "schemas" / "creative_treatment_plan.schema.json",
    "scripts/generate_creative_sfx.py": SCRIPT_DIR / "generate_creative_sfx.py",
    "assets/creative-sfx.schema.v1.json": SKILL_DIR / "assets" / "creative-sfx.schema.v1.json",
    "scripts/scaffold_gsap_creative_effect.py": SCRIPT_DIR / "scaffold_gsap_creative_effect.py",
    "scripts/scaffold_creative_browser_effect.py": SCRIPT_DIR / "scaffold_creative_browser_effect.py",
    "scripts/plan_virtual_camera.py": SCRIPT_DIR / "plan_virtual_camera.py",
    "scripts/render_virtual_camera.py": SCRIPT_DIR / "render_virtual_camera.py",
    "scripts/install_manim_runtime.py": SCRIPT_DIR / "install_manim_runtime.py",
    "assets/manim-runtime-requirements.v1.txt": SKILL_DIR / "assets" / "manim-runtime-requirements.v1.txt",
}


class ProvenanceError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_key(deliverable_id: Any) -> str:
    """Return a stable path-safe key with a collision-resistant ID suffix."""
    if not isinstance(deliverable_id, str) or not deliverable_id.strip():
        raise ProvenanceError("EDL deliverable_id must be a non-empty string")
    if deliverable_id != deliverable_id.strip():
        raise ProvenanceError("EDL deliverable_id cannot have leading or trailing whitespace")
    ascii_id = (
        unicodedata.normalize("NFKD", deliverable_id)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9_-]+", "-", ascii_id).strip("-_")
    slug = re.sub(r"[-_]{2,}", "-", slug)[:48].rstrip("-_") or "deliverable"
    suffix = hashlib.sha256(deliverable_id.encode("utf-8")).hexdigest()[:12]
    key = f"{slug}-{suffix}"
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key) is None:
        raise ProvenanceError("could not derive a safe artifact key from deliverable_id")
    return key


def render_manifest_name(deliverable_id: Any, mode: str) -> str:
    if mode not in {"draft", "preview", "final"}:
        raise ProvenanceError(f"invalid render mode for artifact name: {mode!r}")
    return f"render_manifest_{artifact_key(deliverable_id)}_{mode}.json"


def preview_approval_name(deliverable_id: Any) -> str:
    return f"preview_approval_{artifact_key(deliverable_id)}.json"


def release_manifest_name(deliverable_id: Any) -> str:
    return f"release_manifest_{artifact_key(deliverable_id)}.json"


def invalidate_release_state(
    edit_dir: Path,
    deliverable_id: Any,
    reason: str,
    *,
    render_manifest: Path | None = None,
) -> Path:
    """Atomically replace a namespaced release PASS with a fail-closed state."""
    key = artifact_key(deliverable_id)
    target = edit_dir.resolve() / release_manifest_name(deliverable_id)
    payload: dict[str, Any] = {
        "version": 2,
        "status": "FAIL",
        "renderer": RENDERER_VERSION,
        "deliverable_id": deliverable_id,
        "artifact_key": key,
        "errors": [reason],
    }
    if render_manifest is not None:
        payload["render_manifest"] = str(render_manifest.resolve())
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def invalidate_release_state_from_manifest_path(manifest_path: Path, reason: str) -> Path | None:
    """Invalidate a canonical final namespace before attempting to parse its manifest.

    A corrupt final manifest cannot reveal its deliverable ID. In that case the
    existing namespaced release manifest is the only trustworthy reverse lookup:
    its ID must derive to the artifact key embedded in the requested filename.
    """
    manifest_path = manifest_path.expanduser().resolve()
    match = re.fullmatch(
        r"render_manifest_([a-z0-9][a-z0-9_-]{0,63})_final\.json",
        manifest_path.name,
    )
    if match is None:
        return None
    key = match.group(1)
    release_path = manifest_path.parent / f"release_manifest_{key}.json"
    if not release_path.is_file():
        return None
    try:
        existing = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(existing, dict):
        return None
    deliverable_id = existing.get("deliverable_id")
    try:
        if artifact_key(deliverable_id) != key:
            return None
    except ProvenanceError:
        return None
    return invalidate_release_state(
        manifest_path.parent,
        deliverable_id,
        reason,
        render_manifest=manifest_path,
    )


def default_qa_dir(edit_dir: Path, deliverable_id: Any, mode: str) -> Path:
    if mode not in {"draft", "preview", "final"}:
        raise ProvenanceError(f"invalid QA mode: {mode!r}")
    return edit_dir / "verify" / artifact_key(deliverable_id) / mode


def _tool_version(name: str) -> dict[str, str]:
    executable = shutil.which(name)
    if executable is None:
        raise ProvenanceError(f"missing executable: {name}")
    result = subprocess.run([executable, "-version"], capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise ProvenanceError(f"cannot identify {name}: {detail[-1000:]}")
    output = result.stdout + result.stderr
    lines = output.decode("utf-8", "replace").splitlines()
    version = lines[0].rstrip() if lines else ""
    if not version:
        raise ProvenanceError(f"{name} returned an empty version string")
    binary = Path(executable).resolve()
    return {
        "path": str(binary),
        "binary_sha256": file_sha256(binary),
        "version": version,
        "version_output_sha256": hashlib.sha256(output).hexdigest(),
    }


def _linked_libass(ffmpeg_path: str) -> dict[str, str] | None:
    """Bind the local dynamic libass used by FFmpeg when it is discoverable."""
    command: list[str] | None = None
    if sys.platform == "darwin" and shutil.which("otool"):
        command = ["otool", "-L", ffmpeg_path]
    elif sys.platform.startswith("linux") and shutil.which("ldd"):
        command = ["ldd", ffmpeg_path]
    if command is None:
        return None
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        if "libass" not in line.casefold():
            continue
        stripped = line.strip()
        if "=>" in stripped:
            candidate = stripped.split("=>", 1)[1].strip().split(" ", 1)[0]
        else:
            candidate = stripped.split(" ", 1)[0]
        library = Path(candidate).expanduser()
        if not library.is_absolute() or not library.exists():
            continue
        resolved = library.resolve()
        if resolved.is_file():
            return {
                "linked_path": str(library),
                "path": str(resolved),
                "sha256": file_sha256(resolved),
            }
    return None


def _macos_rpaths(path: Path, executable_dirs: list[Path]) -> list[Path]:
    if shutil.which("otool") is None:
        return []
    result = subprocess.run(
        ["otool", "-l", str(path)], text=True, capture_output=True, check=False
    )
    if result.returncode:
        return []
    rpaths: list[Path] = []
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate_line in lines[index + 1:index + 6]:
            stripped = candidate_line.strip()
            if not stripped.startswith("path "):
                continue
            raw = stripped.split(" ", 2)[1]
            candidates: list[Path] = []
            if raw.startswith("@loader_path/"):
                candidates.append(path.parent / raw.removeprefix("@loader_path/"))
            elif raw.startswith("@executable_path/"):
                suffix = raw.removeprefix("@executable_path/")
                candidates.extend(directory / suffix for directory in executable_dirs)
            elif Path(raw).is_absolute():
                candidates.append(Path(raw))
            for candidate in candidates:
                rpaths.append(candidate.resolve())
            break
    return rpaths


def _resolve_macos_dependency(
    raw: str, loader: Path, executable_dirs: list[Path]
) -> Path | None:
    candidates: list[Path] = []
    if raw.startswith("@loader_path/"):
        candidates.append(loader.parent / raw.removeprefix("@loader_path/"))
    elif raw.startswith("@executable_path/"):
        suffix = raw.removeprefix("@executable_path/")
        candidates.extend(directory / suffix for directory in executable_dirs)
    elif raw.startswith("@rpath/"):
        suffix = raw.removeprefix("@rpath/")
        candidates.extend(
            directory / suffix
            for directory in _macos_rpaths(loader, executable_dirs)
        )
        candidates.append(loader.parent / suffix)
    elif Path(raw).is_absolute():
        candidates.append(Path(raw))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _dynamic_dependency_closure(executables: list[str]) -> dict[str, Any]:
    """Hash the discoverable transitive shared-library closure for media tools."""
    roots = [Path(value).resolve() for value in executables]
    executable_dirs = [path.parent for path in roots]
    if sys.platform == "darwin" and shutil.which("otool"):
        scanner = "otool"
    elif sys.platform.startswith("linux") and shutil.which("ldd"):
        scanner = "ldd"
    else:
        return {"scanner": None, "libraries": [], "unresolved": []}

    pending = list(roots)
    scanned: set[Path] = set()
    libraries: dict[Path, dict[str, Any]] = {}
    unresolved: set[str] = set()
    while pending:
        current = pending.pop()
        if current in scanned:
            continue
        scanned.add(current)
        result = subprocess.run(
            [scanner, "-L", str(current)] if scanner == "otool" else [scanner, str(current)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            unresolved.add(f"{current}:<dependency-scan-failed>")
            continue
        raw_dependencies: list[str] = []
        if scanner == "otool":
            raw_dependencies = [
                line.strip().split(" (", 1)[0]
                for line in result.stdout.splitlines()[1:]
                if line.strip()
            ]
        else:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if "=>" in stripped:
                    target = stripped.split("=>", 1)[1].strip().split(" ", 1)[0]
                else:
                    target = stripped.split(" ", 1)[0]
                if target.startswith("/"):
                    raw_dependencies.append(target)
                elif "not found" in stripped:
                    unresolved.add(stripped)
        for raw in raw_dependencies:
            dependency = (
                _resolve_macos_dependency(raw, current, executable_dirs)
                if scanner == "otool" else Path(raw).resolve()
            )
            if dependency is None or not dependency.is_file():
                unresolved.add(raw)
                continue
            if dependency not in libraries and dependency not in roots:
                libraries[dependency] = {
                    "path": str(dependency),
                    "sha256": file_sha256(dependency),
                    "size_bytes": dependency.stat().st_size,
                }
            if dependency not in scanned:
                pending.append(dependency)
    return {
        "scanner": scanner,
        "libraries": [libraries[path] for path in sorted(libraries, key=str)],
        "unresolved": sorted(unresolved),
    }


def renderer_identity() -> dict[str, Any]:
    """Fingerprint the exact code and local media executables used by v6."""
    implementation: dict[str, str] = {}
    for label, path in IDENTITY_FILES.items():
        if not path.is_file():
            raise ProvenanceError(f"renderer identity file is missing: {path}")
        implementation[label] = file_sha256(path)
    ffmpeg = _tool_version("ffmpeg")
    ffprobe = _tool_version("ffprobe")
    identity: dict[str, Any] = {
        "version": 1,
        "renderer": RENDERER_VERSION,
        "implementation_sha256": implementation,
        "tools": {
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
        },
        "linked_libraries": {
            "libass": _linked_libass(ffmpeg["path"]),
            "dependency_closure": _dynamic_dependency_closure(
                [ffmpeg["path"], ffprobe["path"]]
            ),
        },
        "runtime": {
            "platform": sys.platform,
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "machine": platform.machine(),
        },
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    identity["identity_sha256"] = hashlib.sha256(canonical).hexdigest()
    return identity


def _subtitle_font_queries(path: Path) -> list[tuple[str, bool, bool]]:
    """Collect declared/default subtitle families and requested style variants."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    queries: list[tuple[str, bool, bool]] = []
    suffix = path.suffix.lower()
    if suffix in {".ass", ".ssa"}:
        in_styles = False
        fields: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                in_styles = line.casefold() in {"[v4+ styles]", "[v4 styles]"}
                fields = []
                continue
            if not in_styles:
                continue
            if line.casefold().startswith("format:"):
                fields = [part.strip().casefold() for part in line.split(":", 1)[1].split(",")]
            elif line.casefold().startswith("style:") and "fontname" in fields:
                values = [part.strip() for part in line.split(":", 1)[1].split(",", len(fields) - 1)]
                font_index = fields.index("fontname")
                if font_index < len(values) and values[font_index]:
                    bold = False
                    italic = False
                    for field, target in (("bold", "bold"), ("italic", "italic")):
                        if field not in fields or fields.index(field) >= len(values):
                            continue
                        try:
                            active = float(values[fields.index(field)]) != 0
                        except ValueError:
                            active = False
                        if target == "bold":
                            bold = active
                        else:
                            italic = active
                    queries.append((values[font_index], bold, italic))
        inline_families = {
            match.strip()
            for match in re.findall(r"\\fn([^\\}\r\n]+)", text)
            if match.strip()
        }
        # Inline family changes inherit/toggle event styling. Resolve every
        # regular/bold/italic combination so all possible libass face files are
        # bound even when override tags are interleaved in a dialogue event.
        for family in inline_families:
            for bold in (False, True):
                for italic in (False, True):
                    queries.append((family, bold, italic))
    else:
        families = {"Arial"}
        families.update(
            match.strip()
            for match in re.findall(
                r"<font\b[^>]*\bface\s*=\s*['\"]?([^'\">]+)", text,
                flags=re.IGNORECASE,
            )
            if match.strip()
        )
        bold_used = re.search(r"<b(?:\s|>)", text, flags=re.IGNORECASE) is not None
        italic_used = re.search(r"<i(?:\s|>)", text, flags=re.IGNORECASE) is not None
        for family in families:
            queries.append((family, False, False))
            if bold_used:
                queries.append((family, True, False))
            if italic_used:
                queries.append((family, False, True))
            if bold_used and italic_used:
                queries.append((family, True, True))
    if not queries:
        queries.append(("Arial", False, False))
    unique: dict[tuple[str, bool, bool], tuple[str, bool, bool]] = {}
    for value, bold, italic in queries:
        cleaned = value.strip().strip("'\"")
        if cleaned:
            unique.setdefault((cleaned.casefold(), bold, italic), (cleaned, bold, italic))
    return [unique[key] for key in sorted(unique)]


def resolve_subtitle_fonts(path: Path) -> list[dict[str, str]]:
    """Resolve every declared/default subtitle family through local fontconfig."""
    executable = shutil.which("fc-match")
    if executable is None:
        raise ProvenanceError("burned subtitles require local fc-match")
    records: list[dict[str, str]] = []
    for family, bold, italic in _subtitle_font_queries(path):
        pattern = family
        if bold:
            pattern += ":weight=bold"
        if italic:
            pattern += ":slant=italic"
        result = subprocess.run(
            [
                executable,
                "-f",
                "%{file}\x1f%{family}\x1f%{style}\n",
                "--",
                pattern,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise ProvenanceError(f"fc-match failed for {pattern!r}: {detail[-1000:]}")
        first = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
        parts = first.split("\x1f")
        if len(parts) != 3 or not parts[0].strip():
            raise ProvenanceError(f"fc-match returned no font file for {pattern!r}")
        font_path = Path(parts[0].strip()).expanduser().resolve()
        if not font_path.is_file():
            raise ProvenanceError(f"fc-match returned a missing font file: {font_path}")
        records.append({
            "query": family,
            "fontconfig_pattern": pattern,
            "requested_weight": "bold" if bold else "regular",
            "requested_slant": "italic" if italic else "roman",
            "matched_family": parts[1].strip(),
            "matched_style": parts[2].strip(),
            "path": str(font_path),
            "sha256": file_sha256(font_path),
        })
    return records
