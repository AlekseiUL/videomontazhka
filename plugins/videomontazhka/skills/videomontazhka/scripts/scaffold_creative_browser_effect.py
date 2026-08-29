#!/usr/bin/env python3
"""Scaffold one approval-bound, offline creative-browser HyperFrames source."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from runtime_paths import APP_HOME, CREATIVE_BROWSER_RUNTIME, HYPERFRAMES_RUNTIME
from schema_check import SchemaDefinitionError, Validator
from visual_asset_provenance import (
    VisualProvenanceError,
    atomic_write_json,
    file_sha256,
    load_approved_visual_plan_item,
    normalized_words,
)


SCAFFOLDER_VERSION = "sprut-creative-browser-scaffold-1"
PINNED_GSAP_VERSION = "3.14.2"
SKILL_ROOT = SCRIPT_DIR.parent
ASSET_ROOT = SKILL_ROOT / "assets" / "creative-browser-effects"
TEMPLATE_ROOT = ASSET_ROOT / "templates"
CONFIG_SCHEMA = ASSET_ROOT / "creative-browser-effect.schema.v1.json"
SOURCE_MANIFEST_SCHEMA = ASSET_ROOT / "source-manifest.schema.v1.json"
CATALOG_FILE = ASSET_ROOT / "effects.catalog.v1.json"
FONT_ROOT = SKILL_ROOT / "assets" / "fonts"
FONT_MANIFEST = FONT_ROOT / "manifest.json"
STUDIO_ROOT = APP_HOME  # Backward-compatible discovery field name.
DEFAULT_CREATIVE_RUNTIME = CREATIVE_BROWSER_RUNTIME
DEFAULT_GSAP_BUNDLE = (
    HYPERFRAMES_RUNTIME
    / "node_modules"
    / "gsap"
    / "dist"
    / "gsap.min.js"
)
CALLABLE_EFFECTS = (
    "pixi-semantic-accent",
    "rough-screen-annotation",
    "lottie-local-icon",
    "three-spatial-system",
)
BLOCKED_EFFECTS = {
    "shader-transition": "no_audited_seek_safe_compositor_and_boundary_qa_binding",
}
SAFE_VISUAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REMOTE_HTML_REFERENCE = re.compile(
    r"<(?:script|link|img|video|audio|source)\b[^>]*(?:src|href)\s*=\s*['\"](?:https?:)?//",
    re.IGNORECASE,
)
REMOTE_CSS_REFERENCE = re.compile(
    r"@import\s+url\s*\(\s*['\"]?(?:https?:)?//|url\s*\(\s*['\"]?(?:https?:)?//",
    re.IGNORECASE,
)
REMOTE_VALUE = re.compile(r"^(?:https?:|wss?:|ftp:|file:|//|data:)", re.IGNORECASE)
ALLOWED_FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}
TEMPLATE_KEYS = {
    "version",
    "id",
    "audited",
    "engine",
    "text_mode",
    "asset_types",
    "default_duration_s",
    "transparent",
    "experimental",
    "default_enabled",
    "deterministic",
    "required_local_asset",
    "required_flags",
    "runtime_files",
    "license_files",
}
ENGINE_CONTRACTS: dict[str, dict[str, Any]] = {
    "pixi-semantic-accent": {
        "engine": "pixi.js",
        "bundle": "vendor/sprut-pixi.js",
        "licenses": ["licenses/pixi.js-MIT.txt", "licenses/pixi-filters-MIT.txt"],
        "packages": [("pixi.js", "8.19.0"), ("pixi-filters", "6.1.5")],
    },
    "rough-screen-annotation": {
        "engine": "rough-notation",
        "bundle": "vendor/rough-notation.iife.js",
        "licenses": ["licenses/rough-notation-MIT.txt"],
        "packages": [("rough-notation", "0.5.1")],
    },
    "lottie-local-icon": {
        "engine": "lottie-web",
        "bundle": "vendor/lottie-light.min.js",
        "licenses": ["licenses/lottie-web-MIT.txt"],
        "packages": [("lottie-web", "5.13.0")],
    },
    "three-spatial-system": {
        "engine": "three",
        "bundle": "vendor/sprut-three.js",
        "licenses": ["licenses/three-MIT.txt"],
        "packages": [("three", "0.185.1")],
    },
}


class CreativeEffectError(RuntimeError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CreativeEffectError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CreativeEffectError(f"non-finite JSON number is prohibited: {value}")


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except CreativeEffectError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CreativeEffectError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CreativeEffectError(f"{label} must be a JSON object: {path}")
    return value


def safe_source_file(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.expanduser().resolve())
    except ValueError as exc:
        raise CreativeEffectError(f"{label} escapes its audited source root: {resolved}") from exc
    if path.is_symlink() or not resolved.is_file():
        raise CreativeEffectError(f"{label} must be a regular non-symlink file: {resolved}")
    return resolved


def validate_schema(instance: Any, schema_path: Path, label: str) -> None:
    schema = load_json_object(schema_path, f"{label} schema")
    try:
        errors = Validator(schema).validate(instance)
    except SchemaDefinitionError as exc:
        raise CreativeEffectError(f"invalid {label} schema: {exc}") from exc
    if errors:
        rendered = "; ".join(error.render() for error in errors[:8])
        raise CreativeEffectError(f"{label} does not match its strict schema: {rendered}")


def validate_catalog() -> None:
    catalog = load_json_object(CATALOG_FILE, "creative effect catalog")
    if set(catalog) != {"version", "runtime_id", "policy", "effects", "deferred_effects"}:
        raise CreativeEffectError("creative effect catalog fields are not canonical")
    if catalog.get("version") != 1 or catalog.get("runtime_id") != "sprut-creative-browser-v1":
        raise CreativeEffectError("creative effect catalog is not the approved v1 runtime catalog")
    effects = catalog.get("effects")
    if not isinstance(effects, list) or [item.get("id") for item in effects if isinstance(item, dict)] != list(CALLABLE_EFFECTS):
        raise CreativeEffectError("creative effect catalog callable allowlist differs from code")
    deferred = catalog.get("deferred_effects")
    if not isinstance(deferred, list) or len(deferred) != 1 or not isinstance(deferred[0], dict):
        raise CreativeEffectError("creative effect catalog must contain one fail-closed deferred record")
    shader = deferred[0]
    if (
        shader.get("id") != "shader-transition"
        or shader.get("status") != "blocked"
        or shader.get("reason_code") != BLOCKED_EFFECTS["shader-transition"]
    ):
        raise CreativeEffectError("shader-transition must remain fail-closed in the catalog")


def template_contract(effect_id: str) -> tuple[dict[str, Any], Path, Path]:
    if effect_id in BLOCKED_EFFECTS:
        raise CreativeEffectError(
            f"effect {effect_id!r} is blocked: {BLOCKED_EFFECTS[effect_id]}"
        )
    if effect_id not in CALLABLE_EFFECTS:
        raise CreativeEffectError(f"effect is not in the audited callable allowlist: {effect_id!r}")
    contract = ENGINE_CONTRACTS[effect_id]
    directory = (TEMPLATE_ROOT / effect_id).resolve()
    try:
        directory.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise CreativeEffectError("template path escapes the audited template root") from exc
    metadata_path = safe_source_file(directory / "template.json", TEMPLATE_ROOT, "template metadata")
    source_path = safe_source_file(directory / "index.html", TEMPLATE_ROOT, "template source")
    metadata = load_json_object(metadata_path, "template metadata")
    if set(metadata) != TEMPLATE_KEYS:
        raise CreativeEffectError(f"template metadata fields are not canonical: {effect_id}")
    if (
        metadata.get("version") != 1
        or metadata.get("id") != effect_id
        or metadata.get("audited") is not True
        or metadata.get("deterministic") is not True
        or metadata.get("transparent") is not True
        or metadata.get("engine") != contract["engine"]
        or metadata.get("runtime_files") != [contract["bundle"]]
        or metadata.get("license_files") != contract["licenses"]
    ):
        raise CreativeEffectError(f"template does not match the audited engine contract: {effect_id}")
    if metadata.get("text_mode") not in {"required", "optional", "forbidden"}:
        raise CreativeEffectError(f"template text_mode is invalid: {effect_id}")
    if not isinstance(metadata.get("asset_types"), list) or not metadata["asset_types"]:
        raise CreativeEffectError(f"template asset_types are invalid: {effect_id}")
    if effect_id == "three-spatial-system" and (
        metadata.get("experimental") is not True
        or metadata.get("default_enabled") is not False
        or metadata.get("required_flags") != ["--enable-experimental-three"]
    ):
        raise CreativeEffectError("Three template must be experimental, off by default, and flag-gated")
    if effect_id == "lottie-local-icon" and (
        metadata.get("required_local_asset") is not True
        or metadata.get("required_flags") != ["--confirm-user-owned-lottie"]
    ):
        raise CreativeEffectError("Lottie template must require a local user-owned asset")
    raw_html = source_path.read_text(encoding="utf-8")
    if REMOTE_HTML_REFERENCE.search(raw_html) or REMOTE_CSS_REFERENCE.search(raw_html):
        raise CreativeEffectError(f"template contains a remote runtime/media reference: {effect_id}")
    if "Math.random" in raw_html or "Date.now" in raw_html or "setTimeout" in raw_html:
        raise CreativeEffectError(f"template contains a nondeterministic timing/randomness primitive: {effect_id}")
    for required in (
        "./config.js",
        "./vendor/gsap.min.js",
        "./sprut-creative-browser-runtime.js",
        f"./{contract['bundle']}",
    ):
        if required not in raw_html:
            raise CreativeEffectError(f"template does not invoke copied local dependency {required}: {effect_id}")
    return metadata, source_path, metadata_path


def load_font_pack() -> list[dict[str, Any]]:
    manifest = load_json_object(FONT_MANIFEST, "font manifest")
    if manifest.get("version") != 1 or manifest.get("policy") != "local_only":
        raise CreativeEffectError("font manifest is not the approved local-only v1 pack")
    families = manifest.get("families")
    if not isinstance(families, list):
        raise CreativeEffectError("font manifest families must be an array")
    by_role: dict[str, dict[str, Any]] = {}
    for item in families:
        if not isinstance(item, dict):
            raise CreativeEffectError("font manifest family entry must be an object")
        role = item.get("role")
        if role not in {"expressive_display", "readable_body", "technical_labels_and_data"}:
            continue
        font_rel = Path(str(item.get("file") or ""))
        license_rel = Path(str(item.get("license_file") or ""))
        font_file = safe_source_file(FONT_ROOT / font_rel, FONT_ROOT, f"{role} font")
        license_file = safe_source_file(FONT_ROOT / license_rel, FONT_ROOT, f"{role} license")
        if font_file.suffix.lower() not in ALLOWED_FONT_SUFFIXES:
            raise CreativeEffectError(f"unsupported bundled font format: {font_file}")
        if item.get("license") != "SIL Open Font License 1.1" or item.get("cyrillic_basic") is not True:
            raise CreativeEffectError(f"bundled font license/Cyrillic contract failed: {font_file}")
        if file_sha256(font_file) != item.get("sha256") or file_sha256(license_file) != item.get("license_sha256"):
            raise CreativeEffectError(f"bundled font hash differs from manifest: {font_file}")
        by_role[str(role)] = {
            **item,
            "source_file": font_file,
            "source_license": license_file,
            "relative_file": font_rel,
            "relative_license": license_rel,
        }
    required = {"expressive_display", "readable_body", "technical_labels_and_data"}
    if set(by_role) != required:
        raise CreativeEffectError("font pack roles are incomplete or duplicated")
    return [
        by_role["expressive_display"],
        by_role["readable_body"],
        by_role["technical_labels_and_data"],
    ]


def verify_gsap_bundle(value: Path) -> tuple[Path, dict[str, Any], Path, Path | None]:
    path = value.expanduser().resolve()
    if value.is_symlink() or not path.is_file() or path.suffix.lower() not in {".js", ".mjs"}:
        raise CreativeEffectError(f"--gsap-bundle must be a local regular JavaScript file: {path}")
    if path.stat().st_size < 128 or path.stat().st_size > 5 * 1024 * 1024:
        raise CreativeEffectError("--gsap-bundle has an implausible file size")
    if b"gsap" not in path.read_bytes()[:262_144].lower():
        raise CreativeEffectError("--gsap-bundle does not identify itself as GSAP")
    package_root = path.parent.parent if path.parent.name == "dist" else path.parent
    package_json = safe_source_file(package_root / "package.json", package_root, "GSAP package.json")
    package = load_json_object(package_json, "GSAP package.json")
    if package.get("name") != "gsap" or package.get("version") != PINNED_GSAP_VERSION:
        raise CreativeEffectError(f"GSAP package must be exactly gsap@{PINNED_GSAP_VERSION}")
    license_text = package.get("license")
    if not isinstance(license_text, str) or not license_text.strip():
        raise CreativeEffectError("GSAP package.json must contain a non-empty license string")
    if not gsap_license_url(package):
        raise CreativeEffectError("GSAP package.json license metadata must contain an https terms URL")
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
    match = re.search(r"https://[^\s'\"<>]+", str(package.get("license") or ""))
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


def runtime_file_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise CreativeEffectError("creative runtime manifest files must be an array")
    result: dict[str, dict[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise CreativeEffectError("creative runtime file record is not canonical")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or relative in result:
            raise CreativeEffectError("creative runtime file record path is invalid or duplicated")
        result[relative] = item
    return result


def verify_creative_runtime(
    runtime_dir: Path,
    effect_id: str,
) -> tuple[Path, dict[str, Any], list[Path], list[dict[str, str]]]:
    root = runtime_dir.expanduser().resolve()
    if runtime_dir.is_symlink() or not root.is_dir():
        raise CreativeEffectError(f"creative runtime directory not found or is a symlink: {root}")
    manifest_path = safe_source_file(root / "RUNTIME_MANIFEST.json", root, "creative runtime manifest")
    inventory_path = safe_source_file(root / "THIRD_PARTY_PACKAGES.json", root, "creative package inventory")
    manifest = load_json_object(manifest_path, "creative runtime manifest")
    if manifest.get("version") != 1 or manifest.get("runtime_id") != "sprut-creative-browser-v1":
        raise CreativeEffectError("creative runtime is not the pinned v1 runtime")
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or (
        policy.get("local_only") is not True
        or policy.get("network_required_for_render") is not False
        or policy.get("remote_media_inputs") != "prohibited"
        or policy.get("remotion") != "prohibited"
    ):
        raise CreativeEffectError("creative runtime offline/remotion policy is not fail-closed")
    files = runtime_file_map(manifest)
    contract = ENGINE_CONTRACTS[effect_id]
    required_relatives = [
        contract["bundle"],
        *contract["licenses"],
        "RUNTIME_MANIFEST.json",
        "THIRD_PARTY_PACKAGES.json",
    ]
    verified: list[Path] = []
    for relative in required_relatives:
        path = safe_source_file(root / relative, root, f"creative runtime {relative}")
        if relative not in {"RUNTIME_MANIFEST.json"}:
            record = files.get(relative)
            if record is None or record.get("sha256") != file_sha256(path) or record.get("size_bytes") != path.stat().st_size:
                raise CreativeEffectError(f"creative runtime file differs from its pinned manifest: {relative}")
        verified.append(path)
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, dict):
        raise CreativeEffectError("creative runtime dependencies are missing")
    inventory = load_json_object(inventory_path, "creative package inventory")
    inventory_items = inventory.get("packages")
    if inventory.get("version") != 1 or not isinstance(inventory_items, list):
        raise CreativeEffectError("creative package inventory is not v1")
    direct_by_name = {
        item.get("name"): item
        for item in inventory_items
        if isinstance(item, dict) and item.get("direct") is True
    }
    packages: list[dict[str, str]] = []
    for name, version in contract["packages"]:
        item = direct_by_name.get(name)
        if dependencies.get(name) != version or not isinstance(item, dict):
            raise CreativeEffectError(f"creative runtime package is absent or unpinned: {name}@{version}")
        if item.get("version") != version or item.get("license") != "MIT":
            raise CreativeEffectError(f"creative runtime package license/version contract failed: {name}@{version}")
        packages.append({"name": name, "version": version, "license": "MIT"})
    return root, manifest, verified, packages


def validate_approved_visual(approved: Any, metadata: Mapping[str, Any]) -> None:
    if approved.asset_type not in metadata["asset_types"]:
        raise CreativeEffectError(
            f"effect {metadata['id']!r} requires approved asset_type in "
            f"{metadata['asset_types']!r}, got {approved.asset_type!r}"
        )
    text_mode = metadata["text_mode"]
    has_text = approved.approved_text is not None
    if text_mode == "required" and not has_text:
        raise CreativeEffectError(f"effect {metadata['id']!r} requires non-null approved_text")
    if text_mode == "forbidden" and has_text:
        raise CreativeEffectError(f"effect {metadata['id']!r} requires approved_text=null")
    if has_text and not normalized_words(approved.approved_text):
        raise CreativeEffectError("approved_text contains no visible words")


def bounded_number(value: float, label: str, low: float, high: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise CreativeEffectError(f"{label} must be between {low:g} and {high:g}")
    return number


def bounded_integer(value: int, label: str, low: int, high: int) -> int:
    number = int(value)
    if not low <= number <= high:
        raise CreativeEffectError(f"{label} must be between {low} and {high}")
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


def content_lines(approved_text: str | None) -> list[str]:
    if approved_text is None:
        return []
    lines = [line.strip() for line in approved_text.splitlines() if line.strip()]
    return (lines or [approved_text.strip()])[:8]


def deterministic_seed(visual_id: str, effect_id: str, requested: int | None) -> int:
    if requested is not None:
        return bounded_integer(requested, "seed", 1, 2_147_483_646)
    digest = hashlib.sha256(f"{visual_id}\0{effect_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 2_147_483_646 + 1


def normalized_region(args: argparse.Namespace) -> dict[str, float]:
    region = {
        "x": bounded_number(args.target_x, "target-x", 0, 1),
        "y": bounded_number(args.target_y, "target-y", 0, 1),
        "width": bounded_number(args.target_width, "target-width", 0.01, 1),
        "height": bounded_number(args.target_height, "target-height", 0.01, 1),
    }
    if region["x"] + region["width"] > 1 or region["y"] + region["height"] > 1:
        raise CreativeEffectError("rough target rectangle must remain inside the composition")
    return region


def _walk_json(value: Any) -> Sequence[Any]:
    stack = [value]
    flattened: list[Any] = []
    while stack:
        current = stack.pop()
        flattened.append(current)
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return flattened


def validate_lottie_asset(edit_dir: Path, value: Path | None, confirmed: bool) -> tuple[Path, dict[str, Any]]:
    if value is None:
        raise CreativeEffectError("lottie-local-icon requires --lottie-json")
    if confirmed is not True:
        raise CreativeEffectError("lottie-local-icon requires --confirm-user-owned-lottie")
    candidate = value.expanduser() if value.is_absolute() else edit_dir / value.expanduser()
    if candidate.is_symlink():
        raise CreativeEffectError(f"Lottie input must not be a symlink: {candidate}")
    path = path_under_edit(edit_dir, candidate, "Lottie JSON")
    if not path.is_file() or path.suffix.lower() != ".json":
        raise CreativeEffectError(f"Lottie input must be a regular non-symlink .json under edit/: {path}")
    if path.stat().st_size < 2 or path.stat().st_size > 5 * 1024 * 1024:
        raise CreativeEffectError("Lottie JSON must be between 2 bytes and 5 MiB")
    data = load_json_object(path, "user-owned Lottie JSON")
    required = {"v", "fr", "ip", "op", "w", "h", "layers"}
    if not required.issubset(data):
        raise CreativeEffectError(f"Lottie JSON is missing required fields: {sorted(required - set(data))}")
    frame_rate = bounded_number(data["fr"], "Lottie fr", 1, 120)
    in_frame = bounded_number(data["ip"], "Lottie ip", 0, 100_000)
    out_frame = bounded_number(data["op"], "Lottie op", 0.001, 100_000)
    if out_frame <= in_frame or out_frame - in_frame > 3_600:
        raise CreativeEffectError("Lottie frame range must be positive and no longer than 3600 frames")
    bounded_integer(data["w"], "Lottie width", 1, 4096)
    bounded_integer(data["h"], "Lottie height", 1, 4096)
    assets = data.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 100:
        raise CreativeEffectError("Lottie assets must be an array with at most 100 entries")
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise CreativeEffectError(f"Lottie assets[{index}] must be an object")
        if any(key in asset for key in ("p", "u", "e")):
            raise CreativeEffectError("Lottie image/footage assets are prohibited; use pure vector JSON")
    layer_lists: list[list[Any]] = []
    if not isinstance(data["layers"], list):
        raise CreativeEffectError("Lottie layers must be an array")
    layer_lists.append(data["layers"])
    for asset in assets:
        if "layers" in asset:
            if not isinstance(asset["layers"], list):
                raise CreativeEffectError("Lottie precomposition layers must be an array")
            layer_lists.append(asset["layers"])
    layers = [layer for group in layer_lists for layer in group]
    if len(layers) > 250:
        raise CreativeEffectError("Lottie JSON contains more than 250 layers")
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or layer.get("ty") not in {0, 1, 3, 4}:
            raise CreativeEffectError(
                f"Lottie layer {index} is not an allowed vector/precomp/solid/null layer"
            )
    for item in _walk_json(data):
        if isinstance(item, str):
            stripped = item.strip()
            if REMOTE_VALUE.match(stripped) or "<script" in stripped.lower() or "javascript:" in stripped.lower():
                raise CreativeEffectError("Lottie JSON contains a URL, data URI, or executable markup")
        elif isinstance(item, dict):
            expression = item.get("x")
            if isinstance(expression, str) and expression.strip():
                raise CreativeEffectError("Lottie expressions are prohibited in local icon assets")
    data.setdefault("assets", [])
    return path, {
        "data": data,
        "source_sha256": file_sha256(path),
        "in_frame": in_frame,
        "out_frame": out_frame,
        "frame_rate": frame_rate,
    }


def effect_config(
    args: argparse.Namespace,
    edit_dir: Path,
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    if args.effect == "lottie-local-icon":
        if args.seed is not None:
            raise CreativeEffectError("--seed is not used by lottie-local-icon")
        lottie_path, lottie = validate_lottie_asset(
            edit_dir, args.lottie_json, args.confirm_user_owned_lottie
        )
        return (
            {
                "type": args.effect,
                "source_file": "assets/lottie-source.json",
                "source_sha256": lottie["source_sha256"],
                "rights_attestation": "user_owned",
                "renderer": "svg",
                "animation_data_file": "lottie-data.js",
                "in_frame": lottie["in_frame"],
                "out_frame": lottie["out_frame"],
                "frame_rate": lottie["frame_rate"],
            },
            (lottie_path, lottie),
        )
    if args.lottie_json is not None or args.confirm_user_owned_lottie:
        raise CreativeEffectError("Lottie input/attestation flags are only valid for lottie-local-icon")
    seed = deterministic_seed(args.visual_id, args.effect, args.seed)
    if args.effect == "pixi-semantic-accent":
        return (
            {
                "type": args.effect,
                "seed": seed,
                "mode": args.pixi_mode,
                "center": {
                    "x": bounded_number(args.center_x, "center-x", 0, 1),
                    "y": bounded_number(args.center_y, "center-y", 0, 1),
                },
                "particle_count": bounded_integer(args.particle_count, "particle-count", 8, 96),
            },
            None,
        )
    if args.effect == "rough-screen-annotation":
        return (
            {
                "type": args.effect,
                "seed": seed,
                "annotation_type": args.annotation_type,
                "target": normalized_region(args),
                "stroke_width": bounded_integer(args.stroke_width, "stroke-width", 2, 16),
            },
            None,
        )
    if args.effect == "three-spatial-system":
        if args.enable_experimental_three is not True:
            raise CreativeEffectError(
                "three-spatial-system is experimental and off by default; pass --enable-experimental-three"
            )
        return (
            {
                "type": args.effect,
                "seed": seed,
                "enabled": True,
                "experimental": True,
                "node_count": bounded_integer(args.three_node_count, "three-node-count", 4, 24),
            },
            None,
        )
    raise CreativeEffectError(f"unhandled audited effect: {args.effect}")


def file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    relative = resolved.relative_to(root.resolve())
    return {
        "path": relative.as_posix(),
        "sha256": file_sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def assert_hashes_current(records: Mapping[Path, str]) -> None:
    for path, expected in records.items():
        if not path.is_file() or path.is_symlink() or file_sha256(path) != expected:
            raise CreativeEffectError(f"scaffold input changed during operation: {path}")


def write_config_js(path: Path, config: Mapping[str, Any]) -> None:
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("window.SPRUT_CREATIVE_EFFECT_CONFIG = " + encoded + ";\n", encoding="utf-8")


def write_lottie_data_js(path: Path, data: Mapping[str, Any]) -> None:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("window.SPRUT_LOTTIE_DATA = " + encoded + ";\n", encoding="utf-8")


def scaffold(args: argparse.Namespace) -> Path:
    if not getattr(args, "accept_gsap_terms", False):
        raise CreativeEffectError(
            "refusing to copy GSAP bytes without explicit --accept-gsap-terms"
        )
    edit_dir = canonical_edit_dir(args.edit_dir)
    if not SAFE_VISUAL_ID.fullmatch(args.visual_id):
        raise CreativeEffectError("--visual-id is not a safe filesystem identifier")
    validate_catalog()
    metadata, template_source, template_metadata = template_contract(args.effect)
    runtime_root, runtime_manifest, runtime_inputs, packages = verify_creative_runtime(
        args.runtime_dir, args.effect
    )
    gsap_bundle, gsap_package, gsap_package_json, gsap_readme = verify_gsap_bundle(
        args.gsap_bundle
    )
    gsap_terms = gsap_terms_record(gsap_package, gsap_package_json, gsap_readme)
    font_pack = load_font_pack()
    common_sources = [
        safe_source_file(ASSET_ROOT / "README.md", ASSET_ROOT, "README"),
        safe_source_file(ASSET_ROOT / "DESIGN.md", ASSET_ROOT, "DESIGN"),
        safe_source_file(ASSET_ROOT / "creative-browser-effect.css", ASSET_ROOT, "effect CSS"),
        safe_source_file(ASSET_ROOT / "sprut-creative-browser-runtime.js", ASSET_ROOT, "effect runtime"),
        safe_source_file(CONFIG_SCHEMA, ASSET_ROOT, "config schema"),
        safe_source_file(SOURCE_MANIFEST_SCHEMA, ASSET_ROOT, "source manifest schema"),
        safe_source_file(CATALOG_FILE, ASSET_ROOT, "effect catalog"),
        safe_source_file(FONT_MANIFEST, FONT_ROOT, "font manifest"),
    ]

    # No output directory is created until the semantic/asset gate has passed.
    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    validate_approved_visual(approved, metadata)

    width = int(args.width)
    height = int(args.height)
    if width < 320 or width > 7680 or width % 2 or height < 320 or height > 7680 or height % 2:
        raise CreativeEffectError("width and height must be even integers in 320..7680")
    fps = bounded_number(args.fps, "fps", 20, 60)
    duration = (
        bounded_number(args.duration, "duration", 0.5, 30)
        if args.duration is not None
        else bounded_number(metadata["default_duration_s"], "effect duration", 0.5, 30)
    )
    selected_effect, lottie_asset = effect_config(args, edit_dir)

    instances_root = path_under_edit(
        edit_dir,
        edit_dir / "animations" / "hyperframes" / "creative-browser",
        "creative-browser instances directory",
    )
    target = path_under_edit(
        instances_root,
        instances_root / args.visual_id,
        "creative-browser source instance",
    )
    if target.exists():
        raise CreativeEffectError(
            f"creative-browser source instance already exists and was left untouched: {target}"
        )

    input_hashes: dict[Path, str] = {
        Path(__file__).resolve(): file_sha256(Path(__file__).resolve()),
        template_source: file_sha256(template_source),
        template_metadata: file_sha256(template_metadata),
        gsap_bundle: file_sha256(gsap_bundle),
        gsap_package_json: file_sha256(gsap_package_json),
        approved.plan_snapshot.path: approved.plan_snapshot.sha256,
        approved.approval_snapshot.path: approved.approval_snapshot.sha256,
    }
    if gsap_readme:
        input_hashes[gsap_readme] = file_sha256(gsap_readme)
    for source in [*common_sources, *runtime_inputs]:
        input_hashes[source] = file_sha256(source)
    for item in font_pack:
        input_hashes[item["source_file"]] = file_sha256(item["source_file"])
        input_hashes[item["source_license"]] = file_sha256(item["source_license"])
    if lottie_asset is not None:
        input_hashes[lottie_asset[0]] = lottie_asset[1]["source_sha256"]

    role_names = ("display", "body", "mono")
    fonts: dict[str, dict[str, str]] = {}
    for role_name, item in zip(role_names, font_pack, strict=True):
        fonts[role_name] = {
            "family": str(item["family"]),
            "file": (Path("fonts") / item["relative_file"]).as_posix(),
            "license": "SIL Open Font License 1.1",
            "license_file": (Path("fonts") / item["relative_license"]).as_posix(),
            "license_sha256": str(item["license_sha256"]),
        }
    contract = ENGINE_CONTRACTS[args.effect]
    config = {
        "version": 1,
        "visual_id": args.visual_id,
        "composition": {
            "width": width,
            "height": height,
            "fps": fps,
            "duration_s": duration,
            "transparent": True,
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
            "lines": content_lines(approved.approved_text),
        },
        "layout": safe_area(width, height),
        "effect": selected_effect,
        "runtime": {
            "runtime_id": "sprut-creative-browser-v1",
            "runtime_manifest_file": "runtime/RUNTIME_MANIFEST.json",
            "runtime_manifest_sha256": file_sha256(runtime_root / "RUNTIME_MANIFEST.json"),
            "engine_file": contract["bundle"],
            "engine_license_files": contract["licenses"],
            "gsap_file": "vendor/gsap.min.js",
            "gsap_terms": gsap_terms,
            "network_allowed": False,
            "paid_apis": [],
            "remotion": False,
        },
    }
    schema_config = {
        **config,
        "runtime": {key: value for key, value in config["runtime"].items() if key != "gsap_terms"},
    }
    validate_schema(schema_config, CONFIG_SCHEMA, "creative effect config")

    instances_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{args.visual_id}-", dir=str(instances_root)))
    try:
        index_path = temporary / "index.html"
        shutil.copy2(template_source, index_path)
        index_source = index_path.read_text(encoding="utf-8")
        duration_text = f"{duration:.9f}".rstrip("0").rstrip(".")
        duration_pattern = re.compile(
            rf'(data-composition-id="{re.escape(args.effect)}"[^>]*\bdata-duration=")[^"]+("[^>]*>)'
        )
        index_source, replacements = duration_pattern.subn(
            rf"\g<1>{duration_text}\g<2>", index_source, count=1
        )
        if replacements != 1:
            raise CreativeEffectError(
                "audited template root duration could not be bound to the approved instance"
            )
        index_path.write_text(index_source, encoding="utf-8")
        shutil.copy2(template_metadata, temporary / "template.json")
        shutil.copy2(ASSET_ROOT / "README.md", temporary / "CREATIVE_BROWSER_README.md")
        shutil.copy2(ASSET_ROOT / "DESIGN.md", temporary / "DESIGN.md")
        shutil.copy2(ASSET_ROOT / "creative-browser-effect.css", temporary / "creative-browser-effect.css")
        shutil.copy2(ASSET_ROOT / "sprut-creative-browser-runtime.js", temporary / "sprut-creative-browser-runtime.js")
        shutil.copy2(CONFIG_SCHEMA, temporary / "creative-browser-effect.schema.v1.json")
        shutil.copy2(SOURCE_MANIFEST_SCHEMA, temporary / "source-manifest.schema.v1.json")
        shutil.copy2(CATALOG_FILE, temporary / "effects.catalog.v1.json")
        vendor_dir = temporary / "vendor"
        vendor_dir.mkdir()
        shutil.copy2(gsap_bundle, vendor_dir / "gsap.min.js")
        shutil.copy2(gsap_package_json, vendor_dir / "gsap-package.json")
        if gsap_readme:
            shutil.copy2(gsap_readme, vendor_dir / "GSAP_README.md")
        bundle_source = runtime_root / contract["bundle"]
        bundle_destination = temporary / contract["bundle"]
        bundle_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle_source, bundle_destination)
        licenses_dir = temporary / "licenses"
        licenses_dir.mkdir()
        license_destinations: list[Path] = []
        for relative in contract["licenses"]:
            destination = temporary / relative
            shutil.copy2(runtime_root / relative, destination)
            license_destinations.append(destination)
        runtime_destination = temporary / "runtime"
        runtime_destination.mkdir()
        shutil.copy2(runtime_root / "RUNTIME_MANIFEST.json", runtime_destination / "RUNTIME_MANIFEST.json")
        shutil.copy2(runtime_root / "THIRD_PARTY_PACKAGES.json", runtime_destination / "THIRD_PARTY_PACKAGES.json")
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
        local_asset_manifest: dict[str, Any] | None = None
        if lottie_asset is not None:
            asset_source, lottie = lottie_asset
            asset_destination = temporary / "assets" / "lottie-source.json"
            asset_destination.parent.mkdir()
            shutil.copy2(asset_source, asset_destination)
            write_lottie_data_js(temporary / "lottie-data.js", lottie["data"])
            local_asset_manifest = {
                "kind": "lottie_json",
                "source_path": str(asset_source),
                "source_sha256": lottie["source_sha256"],
                "copied_file": "assets/lottie-source.json",
                "rights_attestation": "user_owned",
            }
        atomic_write_json(temporary / "config.json", config)
        write_config_js(temporary / "config.js", config)

        manifest = {
            "version": 1,
            "type": "sprut_creative_browser_source_manifest",
            "generator": {
                "version": SCAFFOLDER_VERSION,
                "path": str(Path(__file__).resolve()),
                "sha256": input_hashes[Path(__file__).resolve()],
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
            "effect": {
                "id": args.effect,
                "version": metadata["version"],
                "experimental": metadata["experimental"],
                "default_enabled": metadata["default_enabled"],
                "deterministic": True,
                "source_sha256": input_hashes[template_source],
                "metadata_sha256": input_hashes[template_metadata],
                "config_sha256": file_sha256(temporary / "config.json"),
                "local_asset": local_asset_manifest,
            },
            "runtime": {
                "runtime_id": runtime_manifest["runtime_id"],
                "source_manifest": {
                    "path": str(runtime_root / "RUNTIME_MANIFEST.json"),
                    "sha256": file_sha256(runtime_root / "RUNTIME_MANIFEST.json"),
                },
                "packages": packages,
                "copied_bundles": [file_record(bundle_destination, temporary)],
                "copied_licenses": [file_record(path, temporary) for path in license_destinations],
                "gsap": file_record(vendor_dir / "gsap.min.js", temporary),
                "gsap_terms": gsap_terms,
                "offline": True,
                "network_allowed": False,
                "paid_apis": [],
                "remotion": False,
            },
            "restrictions": {
                "remote_media": "prohibited",
                "lottie_url_loading": "prohibited",
                "shader_transition": "blocked_no_audited_compositor",
                "three_default": "off_requires_explicit_flag",
            },
            "files": [],
            "review_requirement": "full_preview_user_approval",
        }
        copied_files = sorted(
            path
            for path in temporary.rglob("*")
            if path.is_file() and path.name != "source-manifest.json"
        )
        manifest["files"] = [file_record(path, temporary) for path in copied_files]
        schema_manifest = {
            **manifest,
            "runtime": {
                key: value for key, value in manifest["runtime"].items() if key != "gsap_terms"
            },
        }
        validate_schema(schema_manifest, SOURCE_MANIFEST_SCHEMA, "creative source manifest")
        assert_hashes_current(input_hashes)
        atomic_write_json(temporary / "source-manifest.json", manifest)
        assert_hashes_current(input_hashes)
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def discovery_payload() -> dict[str, Any]:
    """Return a side-effect-free machine contract for routers and agents."""
    validate_catalog()
    catalog = load_json_object(CATALOG_FILE, "creative effect catalog")
    effects: list[dict[str, Any]] = []
    for catalog_item in catalog["effects"]:
        effect_id = catalog_item["id"]
        metadata, _source, _metadata_path = template_contract(effect_id)
        effects.append(
            {
                "id": effect_id,
                "callable": True,
                "status": catalog_item["status"],
                "engine": catalog_item["engine"],
                "semantic_use": catalog_item["semantic_use"],
                "asset_types": metadata["asset_types"],
                "text_mode": metadata["text_mode"],
                "experimental": metadata["experimental"],
                "default_enabled": metadata["default_enabled"],
                "deterministic": metadata["deterministic"],
                "required_local_asset": metadata["required_local_asset"],
                "required_flags": metadata["required_flags"],
                "runtime_files": metadata["runtime_files"],
                "license_files": metadata["license_files"],
                "default_duration_s": metadata["default_duration_s"],
            }
        )
    deferred = [
        {
            **item,
            "callable": False,
        }
        for item in catalog["deferred_effects"]
    ]
    return {
        "version": 1,
        "type": "sprut_creative_browser_scaffolder_discovery",
        "generator_version": SCAFFOLDER_VERSION,
        "effects": effects,
        "deferred_effects": deferred,
        "constraints": {
            "production_required_args": ["--edit-dir", "--visual-id", "--effect"],
            "asset_gate_required": True,
            "approved_visual_binding_required": True,
            "output_root": "<edit-dir>/animations/hyperframes/creative-browser/<visual-id>",
            "overwrite": "prohibited",
            "network_allowed": False,
            "paid_apis": [],
            "remotion": False,
            "lottie": "user-owned pure-vector JSON under edit/; URL loading prohibited",
            "three": "experimental, off by default, explicit flag required",
            "shader_transition": "blocked until an audited seek-safe compositor and boundary QA exist",
            "review_requirement": "full_preview_user_approval",
        },
        "defaults": {
            "studio_root": str(STUDIO_ROOT),
            "creative_runtime": str(DEFAULT_CREATIVE_RUNTIME),
            "gsap_bundle": str(DEFAULT_GSAP_BUNDLE),
            "paths_are_independent_of_skill_install_location": True,
        },
        "schemas": {
            "config": str(CONFIG_SCHEMA),
            "source_manifest": str(SOURCE_MANIFEST_SCHEMA),
            "catalog": str(CATALOG_FILE),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create one approval-bound offline creative-browser HyperFrames source without rendering"
    )
    parser.add_argument(
        "--describe-json",
        action="store_true",
        help="print the side-effect-free machine-readable effect catalog and constraints",
    )
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--visual-id")
    parser.add_argument(
        "--effect",
        help="audited effect id: " + ", ".join(CALLABLE_EFFECTS),
    )
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_CREATIVE_RUNTIME)
    parser.add_argument("--gsap-bundle", type=Path, default=DEFAULT_GSAP_BUNDLE)
    parser.add_argument(
        "--accept-gsap-terms",
        action="store_true",
        help="confirm acceptance of the GSAP terms recorded in its package.json",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--pixi-mode", choices=("shockwave", "particles", "glow", "combined"), default="combined")
    parser.add_argument("--center-x", type=float, default=0.58)
    parser.add_argument("--center-y", type=float, default=0.42)
    parser.add_argument("--particle-count", type=int, default=32)
    parser.add_argument("--annotation-type", choices=("underline", "box", "circle", "highlight", "bracket"), default="box")
    parser.add_argument("--target-x", type=float, default=0.12)
    parser.add_argument("--target-y", type=float, default=0.16)
    parser.add_argument("--target-width", type=float, default=0.56)
    parser.add_argument("--target-height", type=float, default=0.46)
    parser.add_argument("--stroke-width", type=int, default=6)
    parser.add_argument("--lottie-json", type=Path)
    parser.add_argument("--confirm-user-owned-lottie", action="store_true")
    parser.add_argument("--enable-experimental-three", action="store_true")
    parser.add_argument("--three-node-count", type=int, default=10)
    args = parser.parse_args()
    if args.describe_json:
        conflicting = [
            name
            for name, value in (
                ("--edit-dir", args.edit_dir),
                ("--visual-id", args.visual_id),
                ("--effect", args.effect),
                ("--lottie-json", args.lottie_json),
                ("--seed", args.seed),
                ("--duration", args.duration),
            )
            if value is not None
        ]
        if args.confirm_user_owned_lottie:
            conflicting.append("--confirm-user-owned-lottie")
        if args.enable_experimental_three:
            conflicting.append("--enable-experimental-three")
        if args.accept_gsap_terms:
            conflicting.append("--accept-gsap-terms")
        if conflicting:
            parser.error("--describe-json cannot be combined with production arguments: " + ", ".join(conflicting))
        print(json.dumps(discovery_payload(), ensure_ascii=False, sort_keys=True))
        return 0
    missing = [
        name
        for name, value in (
            ("--edit-dir", args.edit_dir),
            ("--visual-id", args.visual_id),
            ("--effect", args.effect),
        )
        if value is None
    ]
    if missing:
        parser.error("production mode requires " + ", ".join(missing) + "; use --describe-json for discovery")
    target = scaffold(args)
    print(f"creative-browser source scaffolded: {target}")
    print("rendered assets: none | network calls: 0 | paid APIs: none | Remotion: disabled")
    print("next gate: visual preview sheet and full-preview user approval")
    if args.effect == "three-spatial-system":
        print("experimental: Three.js source was explicitly enabled; keep release QA mandatory")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssetGateError,
        CreativeEffectError,
        OSError,
        SchemaDefinitionError,
        VisualProvenanceError,
        ValueError,
    ) as exc:
        print(f"scaffold_creative_browser_effect: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
