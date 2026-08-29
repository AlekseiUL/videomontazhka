#!/usr/bin/env python3
"""Scaffold one approval-bound, fully offline HyperFrames motion instance."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from visual_asset_provenance import (
    VisualProvenanceError,
    atomic_write_json,
    file_sha256,
    load_approved_visual_plan_item,
    normalized_words,
)


SCAFFOLDER_VERSION = "sprut-motion-kit-scaffold-1"
PINNED_GSAP_VERSION = "3.14.2"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
MOTION_KIT_ROOT = SKILL_ROOT / "assets" / "motion-kit"
TEMPLATE_ROOT = MOTION_KIT_ROOT / "templates"
FONT_ROOT = SKILL_ROOT / "assets" / "fonts"
FONT_MANIFEST = FONT_ROOT / "manifest.json"
SCHEMA_FILE = MOTION_KIT_ROOT / "motion-kit.schema.v1.json"
AUDITED_TEMPLATES = (
    "kinetic-keyword",
    "lower-third",
    "screen-callout",
    "diagram-focus",
    "stat-hit",
    "chapter-bridge-premium",
    "cover-wipe-transition",
    "text-behind-subject",
)
SAFE_VISUAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOCAL_SCRIPT_REFERENCE = re.compile(
    r"<(?:script|link|img|video|audio)\b[^>]*(?:src|href)\s*=\s*['\"](?:https?:)?//",
    re.IGNORECASE,
)
ALLOWED_FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}


class MotionKitError(RuntimeError):
    pass


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MotionKitError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MotionKitError(f"{label} must be a JSON object: {path}")
    return value


def safe_source_file(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise MotionKitError(f"{label} escapes its audited source root: {resolved}") from exc
    if path.is_symlink() or not resolved.is_file():
        raise MotionKitError(f"{label} must be a regular non-symlink file: {resolved}")
    return resolved


def template_contract(template_id: str) -> tuple[dict[str, Any], Path, Path]:
    if template_id not in AUDITED_TEMPLATES:
        raise MotionKitError(f"template is not audited: {template_id!r}")
    directory = (TEMPLATE_ROOT / template_id).resolve()
    try:
        directory.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise MotionKitError("template path escapes the audited template root") from exc
    metadata_path = safe_source_file(directory / "template.json", TEMPLATE_ROOT, "template metadata")
    source_path = safe_source_file(directory / "index.html", TEMPLATE_ROOT, "template source")
    metadata = load_json_object(metadata_path, "template metadata")
    expected_keys = {
        "version",
        "id",
        "audited",
        "text_mode",
        "asset_types",
        "default_duration_s",
        "transparent",
        "experimental",
        "required_media",
    }
    if set(metadata) != expected_keys:
        raise MotionKitError(f"template metadata fields are not canonical: {template_id}")
    if metadata.get("version") != 1 or metadata.get("id") != template_id or metadata.get("audited") is not True:
        raise MotionKitError(f"template metadata is not an audited v1 contract: {template_id}")
    if metadata.get("text_mode") not in {"required", "optional", "forbidden"}:
        raise MotionKitError(f"template text_mode is invalid: {template_id}")
    asset_types = metadata.get("asset_types")
    if not isinstance(asset_types, list) or not asset_types or any(
        not isinstance(item, str) or not item for item in asset_types
    ):
        raise MotionKitError(f"template asset_types are invalid: {template_id}")
    raw_html = source_path.read_text(encoding="utf-8")
    if LOCAL_SCRIPT_REFERENCE.search(raw_html) or re.search(r"@import\s+url\s*\(\s*['\"]?(?:https?:)?//", raw_html, re.I):
        raise MotionKitError(f"template contains a remote runtime/media reference: {template_id}")
    if "./vendor/gsap.min.js" not in raw_html:
        raise MotionKitError(f"template does not use the copied local GSAP bundle: {template_id}")
    return metadata, source_path, metadata_path


def verify_gsap_bundle(value: Path) -> tuple[Path, dict[str, Any], Path, Path | None]:
    path = value.expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in {".js", ".mjs"}:
        raise MotionKitError(f"--gsap-bundle must be a local JavaScript file: {path}")
    if path.stat().st_size < 128 or path.stat().st_size > 5 * 1024 * 1024:
        raise MotionKitError("--gsap-bundle has an implausible file size")
    sample = path.read_bytes()[:262_144].lower()
    if b"gsap" not in sample:
        raise MotionKitError("--gsap-bundle does not identify itself as GSAP")
    package_root = path.parent.parent if path.parent.name == "dist" else path.parent
    package_json = safe_source_file(package_root / "package.json", package_root, "GSAP package.json")
    package = load_json_object(package_json, "GSAP package.json")
    if package.get("name") != "gsap" or package.get("version") != PINNED_GSAP_VERSION:
        raise MotionKitError(f"GSAP package must be exactly gsap@{PINNED_GSAP_VERSION}")
    license_text = package.get("license")
    if not isinstance(license_text, str) or not license_text.strip():
        raise MotionKitError("GSAP package.json must contain a non-empty license string")
    if not gsap_license_url(package):
        raise MotionKitError("GSAP package.json license metadata must contain an https terms URL")
    readme_candidate = package_root / "README.md"
    readme = (
        safe_source_file(readme_candidate, package_root, "GSAP README")
        if readme_candidate.exists()
        else None
    )
    return path, package, package_json, readme


def gsap_license_url(package: Mapping[str, Any]) -> str:
    explicit = package.get("licenseUrl")
    if isinstance(explicit, str) and explicit.startswith("https://"):
        return explicit.rstrip(".,)")
    license_text = str(package.get("license") or "")
    match = re.search(r"https://[^\s'\"<>]+", license_text)
    return match.group(0).rstrip(".,)") if match else ""


def gsap_terms_record(
    package: Mapping[str, Any], package_json: Path, readme: Path | None
) -> dict[str, Any]:
    return {
        "version": str(package["version"]),
        "license": str(package["license"]),
        "license_url": gsap_license_url(package),
        "package_json_file": "vendor/gsap-package.json",
        "package_json_sha256": file_sha256(package_json),
        "readme_file": "vendor/GSAP_README.md" if readme else None,
        "readme_sha256": file_sha256(readme) if readme else None,
        "terms_explicitly_accepted": True,
    }


def load_font_pack() -> list[dict[str, Any]]:
    manifest = load_json_object(FONT_MANIFEST, "font manifest")
    if manifest.get("version") != 1 or manifest.get("policy") != "local_only":
        raise MotionKitError("font manifest is not the approved local-only v1 pack")
    raw_families = manifest.get("families")
    if not isinstance(raw_families, list):
        raise MotionKitError("font manifest families must be an array")
    by_role: dict[str, dict[str, Any]] = {}
    for item in raw_families:
        if not isinstance(item, dict):
            raise MotionKitError("font manifest family entry must be an object")
        role = item.get("role")
        if role not in {"expressive_display", "readable_body", "technical_labels_and_data"}:
            continue
        font_rel = Path(str(item.get("file") or ""))
        license_rel = Path(str(item.get("license_file") or ""))
        font_file = safe_source_file(FONT_ROOT / font_rel, FONT_ROOT, f"{role} font")
        license_file = safe_source_file(FONT_ROOT / license_rel, FONT_ROOT, f"{role} license")
        if font_file.suffix.lower() not in ALLOWED_FONT_SUFFIXES:
            raise MotionKitError(f"unsupported bundled font format: {font_file}")
        if item.get("license") != "SIL Open Font License 1.1":
            raise MotionKitError(f"bundled font does not use OFL-1.1: {font_file}")
        if item.get("cyrillic_basic") is not True:
            raise MotionKitError(f"bundled font is not declared Cyrillic-capable: {font_file}")
        if file_sha256(font_file) != item.get("sha256"):
            raise MotionKitError(f"bundled font hash differs from manifest: {font_file}")
        if file_sha256(license_file) != item.get("license_sha256"):
            raise MotionKitError(f"bundled font license hash differs from manifest: {license_file}")
        if role in by_role:
            raise MotionKitError(f"duplicate bundled font role: {role}")
        by_role[role] = {
            **item,
            "source_file": font_file,
            "source_license": license_file,
            "relative_file": font_rel,
            "relative_license": license_rel,
        }
    required = {"expressive_display", "readable_body", "technical_labels_and_data"}
    if set(by_role) != required:
        raise MotionKitError(f"font pack roles are incomplete: {sorted(set(by_role))}")
    return [
        by_role["expressive_display"],
        by_role["readable_body"],
        by_role["technical_labels_and_data"],
    ]


def validate_approved_visual(approved: Any, metadata: Mapping[str, Any]) -> None:
    if approved.asset_type not in metadata["asset_types"]:
        raise MotionKitError(
            f"template {metadata['id']!r} requires approved asset_type in "
            f"{metadata['asset_types']!r}, got {approved.asset_type!r}"
        )
    text_mode = metadata["text_mode"]
    has_text = approved.approved_text is not None
    if text_mode == "required" and not has_text:
        raise MotionKitError(f"template {metadata['id']!r} requires non-null approved_text")
    if text_mode == "forbidden" and has_text:
        raise MotionKitError(f"template {metadata['id']!r} requires approved_text=null")
    if has_text and not normalized_words(approved.approved_text):
        raise MotionKitError("approved_text contains no visible words")


def bounded_number(value: float, label: str, low: float, high: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise MotionKitError(f"{label} must be between {low:g} and {high:g}")
    return number


def safe_area(width: int, height: int) -> dict[str, int]:
    if height > width:
        return {
            "safe_top": round(150 * height / 1920),
            "safe_right": round(150 * width / 1080),
            "safe_bottom": round(420 * height / 1920),
            "safe_left": round(80 * width / 1080),
        }
    return {
        "safe_top": round(70 * height / 1080),
        "safe_right": round(100 * width / 1920),
        "safe_bottom": round(70 * height / 1080),
        "safe_left": round(100 * width / 1920),
    }


def content_lines(template_id: str, approved_text: str | None) -> list[str]:
    if approved_text is None:
        return []
    lines = [line.strip() for line in approved_text.splitlines() if line.strip()]
    if not lines:
        lines = [approved_text.strip()]
    if template_id in {"lower-third", "stat-hit"} and len(lines) > 2:
        return [lines[0], " ".join(lines[1:])]
    return lines[:8]


def file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved.relative_to(root.resolve())),
        "sha256": file_sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def assert_hashes_current(records: Mapping[Path, str]) -> None:
    for path, expected in records.items():
        if not path.is_file() or file_sha256(path) != expected:
            raise MotionKitError(f"scaffold input changed during operation: {path}")


def write_config_js(path: Path, config: Mapping[str, Any]) -> None:
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("window.SPRUT_MOTION_CONFIG = " + encoded + ";\n", encoding="utf-8")


def scaffold(args: argparse.Namespace) -> Path:
    if not getattr(args, "accept_gsap_terms", False):
        raise MotionKitError(
            "refusing to copy GSAP bytes without explicit --accept-gsap-terms"
        )
    edit_dir = canonical_edit_dir(args.edit_dir)
    if not SAFE_VISUAL_ID.fullmatch(args.visual_id):
        raise MotionKitError("--visual-id is not a safe filesystem identifier")
    metadata, template_source, template_metadata = template_contract(args.template)
    gsap_bundle, gsap_package, gsap_package_json, gsap_readme = verify_gsap_bundle(
        args.gsap_bundle
    )
    gsap_terms = gsap_terms_record(gsap_package, gsap_package_json, gsap_readme)
    font_pack = load_font_pack()
    common_sources = [
        safe_source_file(MOTION_KIT_ROOT / "DESIGN.md", MOTION_KIT_ROOT, "DESIGN.md"),
        safe_source_file(MOTION_KIT_ROOT / "README.md", MOTION_KIT_ROOT, "Motion Kit README"),
        safe_source_file(MOTION_KIT_ROOT / "motion-kit.css", MOTION_KIT_ROOT, "Motion Kit CSS"),
        safe_source_file(MOTION_KIT_ROOT / "sprut-motion-runtime.js", MOTION_KIT_ROOT, "Motion Kit runtime"),
        safe_source_file(SCHEMA_FILE, MOTION_KIT_ROOT, "Motion Kit schema"),
        safe_source_file(FONT_MANIFEST, FONT_ROOT, "font manifest"),
    ]

    # The gate must pass before this writer creates even a parent directory.
    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    validate_approved_visual(approved, metadata)

    width = int(args.width)
    height = int(args.height)
    if width < 320 or width > 7680 or width % 2 or height < 320 or height > 7680 or height % 2:
        raise MotionKitError("width and height must be even integers in 320..7680")
    fps = bounded_number(args.fps, "fps", 20, 60)
    duration = (
        bounded_number(args.duration, "duration", 0.5, 30)
        if args.duration is not None
        else bounded_number(metadata["default_duration_s"], "template duration", 0.5, 30)
    )

    instances_root = path_under_edit(
        edit_dir,
        edit_dir / "animations" / "hyperframes" / "instances",
        "motion-kit instances directory",
    )
    target = path_under_edit(instances_root, instances_root / args.visual_id, "motion-kit instance")
    if target.exists():
        raise MotionKitError(f"motion-kit instance already exists and was left untouched: {target}")

    input_hashes: dict[Path, str] = {
        template_source: file_sha256(template_source),
        template_metadata: file_sha256(template_metadata),
        gsap_bundle: file_sha256(gsap_bundle),
        gsap_package_json: file_sha256(gsap_package_json),
        approved.plan_snapshot.path: approved.plan_snapshot.sha256,
        approved.approval_snapshot.path: approved.approval_snapshot.sha256,
    }
    if gsap_readme:
        input_hashes[gsap_readme] = file_sha256(gsap_readme)
    for source in common_sources:
        input_hashes[source] = file_sha256(source)
    for item in font_pack:
        input_hashes[item["source_file"]] = file_sha256(item["source_file"])
        input_hashes[item["source_license"]] = file_sha256(item["source_license"])

    layout = {
        **safe_area(width, height),
        "anchor_x": 0.58,
        "anchor_y": 0.42,
        "focus_x": 0.16,
        "focus_y": 0.18,
        "focus_width": 0.58,
        "focus_height": 0.48,
    }
    role_names = ("display", "body", "mono")
    fonts: dict[str, dict[str, str]] = {}
    for role_name, item in zip(role_names, font_pack, strict=True):
        fonts[role_name] = {
            "family": str(item["family"]),
            "file": str(Path("fonts") / item["relative_file"]),
            "license": "SIL Open Font License 1.1",
            "license_file": str(Path("fonts") / item["relative_license"]),
            "license_sha256": str(item["license_sha256"]),
        }
    config = {
        "version": 1,
        "template": args.template,
        "visual_id": args.visual_id,
        "composition": {
            "width": width,
            "height": height,
            "fps": fps,
            "duration_s": duration,
            "transparent": bool(metadata["transparent"]),
        },
        "brand": {
            "background": "#070707",
            "panel": "#121212",
            "accent": "#FF6A00",
            "primary": "#FFFFFF",
            "secondary": "#A8A8A8",
        },
        "fonts": fonts,
        "content": {
            "approved_text": approved.approved_text,
            "lines": content_lines(args.template, approved.approved_text),
        },
        "layout": layout,
        "media": {"background": None, "foreground": None},
        "runtime": {
            "gsap_file": "vendor/gsap.min.js",
            "gsap_terms": gsap_terms,
            "network_allowed": False,
            "paid_apis": [],
        },
    }

    instances_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{args.visual_id}-", dir=str(instances_root)))
    try:
        shutil.copy2(template_source, temporary / "index.html")
        shutil.copy2(template_metadata, temporary / "template.json")
        shutil.copy2(MOTION_KIT_ROOT / "DESIGN.md", temporary / "DESIGN.md")
        shutil.copy2(MOTION_KIT_ROOT / "README.md", temporary / "MOTION_KIT_README.md")
        shutil.copy2(MOTION_KIT_ROOT / "motion-kit.css", temporary / "motion-kit.css")
        shutil.copy2(MOTION_KIT_ROOT / "sprut-motion-runtime.js", temporary / "sprut-motion-runtime.js")
        shutil.copy2(SCHEMA_FILE, temporary / "motion-kit.schema.v1.json")
        vendor_dir = temporary / "vendor"
        vendor_dir.mkdir()
        shutil.copy2(gsap_bundle, vendor_dir / "gsap.min.js")
        shutil.copy2(gsap_package_json, vendor_dir / "gsap-package.json")
        if gsap_readme:
            shutil.copy2(gsap_readme, vendor_dir / "GSAP_README.md")
        fonts_dir = temporary / "fonts"
        fonts_dir.mkdir()
        shutil.copy2(FONT_MANIFEST, fonts_dir / "manifest.json")
        for item in font_pack:
            destination_font = fonts_dir / item["relative_file"]
            destination_license = fonts_dir / item["relative_license"]
            destination_font.parent.mkdir(parents=True, exist_ok=True)
            destination_license.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item["source_file"], destination_font)
            shutil.copy2(item["source_license"], destination_license)
        atomic_write_json(temporary / "config.json", config)
        write_config_js(temporary / "config.js", config)

        copied_files = sorted(
            path for path in temporary.rglob("*") if path.is_file() and path.name != "source-manifest.json"
        )
        manifest = {
            "version": 1,
            "type": "sprut_hyperframes_source_manifest",
            "generator": {
                "version": SCAFFOLDER_VERSION,
                "path": str(Path(__file__).resolve()),
                "sha256": file_sha256(Path(__file__).resolve()),
            },
            "visual": {
                "visual_id": approved.visual_id,
                "section_id": approved.section_id,
                "meaning_ids": list(approved.meaning_ids),
                "purpose": approved.purpose,
                "treatment": approved.treatment,
                "asset_type": approved.asset_type,
                "approved_text": approved.approved_text,
                "semantic_plan": {
                    "path": str(approved.plan_snapshot.path),
                    "sha256": approved.plan_snapshot.sha256,
                },
                "approval": {
                    "path": str(approved.approval_snapshot.path),
                    "sha256": approved.approval_snapshot.sha256,
                },
            },
            "template": {
                "id": args.template,
                "version": metadata["version"],
                "experimental": metadata["experimental"],
                "required_media": metadata["required_media"],
                "source_sha256": input_hashes[template_source],
                "metadata_sha256": input_hashes[template_metadata],
            },
            "runtime": {
                "offline": True,
                "network_allowed": False,
                "paid_apis": [],
                "remotion": False,
                "gsap": file_record(temporary / "vendor" / "gsap.min.js", temporary),
                "gsap_terms": gsap_terms,
            },
            "files": [file_record(path, temporary) for path in copied_files],
            "review_requirement": "full_preview_user_approval",
        }
        assert_hashes_current(input_hashes)
        atomic_write_json(temporary / "source-manifest.json", manifest)
        assert_hashes_current(input_hashes)
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create an approval-bound offline HyperFrames source instance without rendering"
    )
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument("--visual-id", required=True)
    parser.add_argument("--template", choices=AUDITED_TEMPLATES, required=True)
    parser.add_argument(
        "--gsap-bundle",
        type=Path,
        required=True,
        help="local GSAP browser bundle from the pinned runtime; copied and hash-bound",
    )
    parser.add_argument(
        "--accept-gsap-terms",
        action="store_true",
        help="confirm acceptance of the GSAP terms recorded in its package.json",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--duration", type=float)
    args = parser.parse_args()
    target = scaffold(args)
    print(f"motion-kit source scaffolded: {target}")
    print("rendered assets: none | network calls: 0 | paid APIs: none")
    if args.template == "text-behind-subject":
        print("experimental: add reviewed local background/foreground media before lint or render")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, MotionKitError, OSError, VisualProvenanceError, ValueError) as exc:
        print(f"scaffold_motion_kit: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
