#!/usr/bin/env python3
"""Scaffold one semantic-approved, offline GSAP creative effect source.

This command is intentionally a source writer, not a renderer. It copies only
the pinned local GSAP core and the plugin bundles required by the selected
meaning-specific template. Every input is hash-bound in source-manifest.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from runtime_paths import HYPERFRAMES_RUNTIME
from schema_check import SchemaDefinitionError, Validator
from visual_asset_provenance import (
    VisualProvenanceError,
    atomic_write_json,
    file_sha256,
    load_approved_visual_plan_item,
    load_json_object_snapshot,
    normalized_words,
)


SCAFFOLDER_VERSION = "sprut-gsap-creative-scaffold-1"
PINNED_GSAP_VERSION = "3.14.2"
SKILL_ROOT = SCRIPT_DIR.parent
ASSET_ROOT = SKILL_ROOT / "assets" / "gsap-creative-effects"
TEMPLATE_ROOT = ASSET_ROOT / "templates"
SPEC_SCHEMA = ASSET_ROOT / "gsap-creative-effect-spec.schema.v1.json"
CONFIG_SCHEMA = ASSET_ROOT / "gsap-creative-effect-config.schema.v1.json"
FONT_ROOT = SKILL_ROOT / "assets" / "fonts"
FONT_MANIFEST = FONT_ROOT / "manifest.json"
DEFAULT_GSAP_PACKAGE_ROOT = HYPERFRAMES_RUNTIME / "node_modules" / "gsap"
GSAP_PACKAGE_ROOT = Path(
    os.environ.get(
        "VIDEOMONTAZHKA_GSAP_PACKAGE_ROOT",
        os.environ.get("SPRUT_GSAP_PACKAGE_ROOT", str(DEFAULT_GSAP_PACKAGE_ROOT)),
    )
).expanduser().resolve(strict=False)
AUDITED_EFFECTS = (
    "kinetic_split_keyword",
    "morph_concept",
    "route_draw",
    "data_scramble",
    "flip_before_after",
)
EFFECT_PLUGIN_CONTRACT = {
    "kinetic_split_keyword": ("SplitText.min.js",),
    "morph_concept": ("MorphSVGPlugin.min.js",),
    "route_draw": ("DrawSVGPlugin.min.js", "MotionPathPlugin.min.js"),
    "data_scramble": ("ScrambleTextPlugin.min.js",),
    "flip_before_after": ("Flip.min.js",),
}
ALLOWED_ASSET_TYPES = {
    "none",
    "title",
    "chapter",
    "diagram",
    "comparison",
    "process",
    "quote",
    "cta",
    "b_roll",
}
SAFE_VISUAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REMOTE_REFERENCE = re.compile(
    r"<(?:script|link|img|video|audio)\b[^>]*(?:src|href)\s*=\s*['\"](?:https?:)?//"
    r"|@import\s+url\s*\(\s*['\"]?(?:https?:)?//",
    re.IGNORECASE,
)
BANNED_EXECUTABLE_PATTERNS = {
    "Math.random": re.compile(r"\bMath\.random\s*\("),
    "Date.now": re.compile(r"\bDate\.now\s*\("),
    "setTimeout": re.compile(r"\bsetTimeout\s*\("),
    "Promise": re.compile(r"\bnew\s+Promise\b"),
    "fetch": re.compile(r"\bfetch\s*\("),
    "XMLHttpRequest": re.compile(r"\bXMLHttpRequest\b"),
    "WebSocket": re.compile(r"\bWebSocket\b"),
    "EventSource": re.compile(r"\bEventSource\b"),
    "infinite repeat": re.compile(r"repeat\s*:\s*-1\b"),
}
ALLOWED_FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}


class GSAPCreativeError(RuntimeError):
    pass


@dataclass(frozen=True)
class EffectContract:
    effect_type: str
    metadata: dict[str, Any]
    template_source: Path
    metadata_source: Path


def safe_source_file(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.expanduser().resolve())
    except ValueError as exc:
        raise GSAPCreativeError(f"{label} escapes its audited source root: {resolved}") from exc
    if path.is_symlink() or not resolved.is_file():
        raise GSAPCreativeError(f"{label} must be a regular non-symlink file: {resolved}")
    return resolved


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value, _ = load_json_object_snapshot(path, label)
    except VisualProvenanceError as exc:
        raise GSAPCreativeError(str(exc)) from exc
    return value


def validate_schema_instance(schema_path: Path, value: Mapping[str, Any], label: str) -> None:
    schema = load_json_object(schema_path, f"{label} schema")
    try:
        errors = Validator(schema).validate(value)
    except SchemaDefinitionError as exc:
        raise GSAPCreativeError(f"invalid checked-in {label} schema: {exc}") from exc
    if errors:
        rendered = "\n".join(f"- {error.render()}" for error in errors[:30])
        raise GSAPCreativeError(f"{label} violates its strict schema ({len(errors)} error(s)):\n{rendered}")


def assert_executable_is_offline(path: Path, label: str) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GSAPCreativeError(f"cannot inspect {label} {path}: {exc}") from exc
    if REMOTE_REFERENCE.search(source):
        raise GSAPCreativeError(f"{label} contains a remote runtime or media reference: {path}")
    for name, pattern in BANNED_EXECUTABLE_PATTERNS.items():
        if pattern.search(source):
            raise GSAPCreativeError(f"{label} contains forbidden non-deterministic/network construct {name}: {path}")


def effect_contract(effect_type: str) -> EffectContract:
    if effect_type not in AUDITED_EFFECTS:
        raise GSAPCreativeError(f"unsupported effect_type: {effect_type!r}")
    directory = (TEMPLATE_ROOT / effect_type).resolve()
    try:
        directory.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise GSAPCreativeError("template path escapes the audited template root") from exc
    metadata_path = safe_source_file(directory / "template.json", TEMPLATE_ROOT, "template metadata")
    template_path = safe_source_file(directory / "index.html", TEMPLATE_ROOT, "template source")
    metadata = load_json_object(metadata_path, "template metadata")
    expected_keys = {
        "version",
        "id",
        "audited",
        "description",
        "asset_types",
        "max_words",
        "transparent",
        "plugin_files",
    }
    if set(metadata) != expected_keys:
        raise GSAPCreativeError(f"template metadata fields are not canonical: {effect_type}")
    if metadata.get("version") != 1 or metadata.get("id") != effect_type or metadata.get("audited") is not True:
        raise GSAPCreativeError(f"template metadata is not an audited v1 contract: {effect_type}")
    if metadata.get("transparent") is not True:
        raise GSAPCreativeError(f"GSAP creative template must remain a transparent overlay: {effect_type}")
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        raise GSAPCreativeError(f"template description is invalid: {effect_type}")
    asset_types = metadata.get("asset_types")
    if (
        not isinstance(asset_types, list)
        or not asset_types
        or any(item not in ALLOWED_ASSET_TYPES for item in asset_types)
        or len(asset_types) != len(set(asset_types))
    ):
        raise GSAPCreativeError(f"template asset_types are invalid: {effect_type}")
    max_words = metadata.get("max_words")
    if not isinstance(max_words, int) or isinstance(max_words, bool) or not 1 <= max_words <= 64:
        raise GSAPCreativeError(f"template max_words is invalid: {effect_type}")
    plugins = metadata.get("plugin_files")
    if plugins != list(EFFECT_PLUGIN_CONTRACT[effect_type]):
        raise GSAPCreativeError(f"template plugin_files differ from the audited contract: {effect_type}")
    assert_executable_is_offline(template_path, "template")
    raw_html = template_path.read_text(encoding="utf-8")
    required_scripts = ["./vendor/gsap.min.js", "./creative-effect-runtime.js"] + [
        f"./vendor/{name}" for name in plugins
    ]
    missing = [value for value in required_scripts if value not in raw_html]
    if missing:
        raise GSAPCreativeError(f"template omits required local bundles {missing!r}: {effect_type}")
    if f'data-composition-id="{effect_type}"' not in raw_html:
        raise GSAPCreativeError(f"template composition id does not match effect_type: {effect_type}")
    return EffectContract(effect_type, metadata, template_path, metadata_path)


def describe_catalog() -> dict[str, Any]:
    effects: list[dict[str, Any]] = []
    for effect_type in AUDITED_EFFECTS:
        contract = effect_contract(effect_type)
        effects.append(
            {
                "effect_type": effect_type,
                "description": contract.metadata["description"],
                "asset_types": contract.metadata["asset_types"],
                "max_words": contract.metadata["max_words"],
                "plugins": [name.removesuffix(".min.js") for name in contract.metadata["plugin_files"]],
                "example_options": {
                    "kinetic_split_keyword": {"split": "words", "accent_word_index": 0},
                    "morph_concept": {"target_shape": "arrow"},
                    "route_draw": {"path_style": "arc", "auto_rotate": False},
                    "data_scramble": {"charset": "upper_numeric", "reveal_order": "start"},
                    "flip_before_after": {"layout": "side_by_side"},
                }[effect_type],
            }
        )
    return {
        "version": 1,
        "generator": SCAFFOLDER_VERSION,
        "engine": {"name": "GSAP", "version": PINNED_GSAP_VERSION, "runtime_root": str(GSAP_PACKAGE_ROOT)},
        "effects": effects,
        "request_schema": str(SPEC_SCHEMA),
        "output": "edit/animations/hyperframes/gsap-creative/<visual-id>",
        "constraints": {
            "semantic_approval_required": True,
            "source_only": True,
            "network_allowed": False,
            "paid_apis": [],
            "existing_output_overwritten": False,
            "new_effect_preview_sheet_required": True,
        },
    }


def load_spec(edit_dir: Path, spec_path: Path) -> tuple[dict[str, Any], Path, str]:
    edit_dir = canonical_edit_dir(edit_dir)
    candidate = spec_path if spec_path.is_absolute() else edit_dir / spec_path
    resolved = path_under_edit(edit_dir, candidate, "GSAP creative effect spec")
    if resolved.is_symlink() or not resolved.is_file():
        raise GSAPCreativeError(f"effect spec must be a regular non-symlink file under edit/: {resolved}")
    try:
        value, snapshot = load_json_object_snapshot(resolved, "GSAP creative effect spec")
    except VisualProvenanceError as exc:
        raise GSAPCreativeError(str(exc)) from exc
    validate_schema_instance(SPEC_SCHEMA, value, "effect spec")
    return value, snapshot.path, snapshot.sha256


def verify_runtime(contract: EffectContract) -> tuple[dict[str, Any], list[Path]]:
    package_root = GSAP_PACKAGE_ROOT.expanduser().resolve()
    if GSAP_PACKAGE_ROOT.is_symlink() or not package_root.is_dir():
        raise GSAPCreativeError(f"pinned GSAP package root is unavailable: {package_root}")
    package_json = safe_source_file(package_root / "package.json", package_root, "GSAP package metadata")
    package_readme = safe_source_file(package_root / "README.md", package_root, "GSAP package README")
    package = load_json_object(package_json, "GSAP package metadata")
    if package.get("name") != "gsap" or package.get("version") != PINNED_GSAP_VERSION:
        raise GSAPCreativeError(
            f"GSAP runtime must be exactly {PINNED_GSAP_VERSION}, got {package.get('version')!r}"
        )
    license_value = package.get("license")
    if not isinstance(license_value, str) or "no charge" not in license_value.casefold():
        raise GSAPCreativeError("GSAP package metadata does not expose the approved no-charge license statement")
    bundle_names = ("gsap.min.js", *contract.metadata["plugin_files"])
    bundles: list[Path] = []
    for name in bundle_names:
        bundle = safe_source_file(package_root / "dist" / name, package_root, f"GSAP bundle {name}")
        size = bundle.stat().st_size
        if size < 1024 or size > 5 * 1024 * 1024:
            raise GSAPCreativeError(f"GSAP bundle has an implausible size: {bundle}")
        bundles.append(bundle)
    return package, [package_json, package_readme, *bundles]


def gsap_license_url(package: Mapping[str, Any]) -> str:
    explicit = package.get("licenseUrl")
    if isinstance(explicit, str) and explicit.startswith("https://"):
        return explicit.rstrip(".,)")
    match = re.search(r"https://[^\s'\"<>]+", str(package.get("license") or ""))
    return match.group(0).rstrip(".,)") if match else ""


def gsap_terms_record(package: Mapping[str, Any], package_root: Path) -> dict[str, Any]:
    package_json = package_root / "package.json"
    readme = package_root / "README.md"
    license_url = gsap_license_url(package)
    if not license_url:
        raise GSAPCreativeError("GSAP package.json license metadata must contain an https terms URL")
    return {
        "version": str(package["version"]),
        "license": str(package["license"]),
        "license_url": license_url,
        "package_json_file": "vendor/gsap-package.json",
        "package_json_sha256": file_sha256(package_json),
        "readme_file": "vendor/GSAP_README.md",
        "readme_sha256": file_sha256(readme),
        "terms_explicitly_accepted": True,
    }


def load_font_pack() -> list[dict[str, Any]]:
    manifest = load_json_object(FONT_MANIFEST, "font manifest")
    if manifest.get("version") != 1 or manifest.get("policy") != "local_only":
        raise GSAPCreativeError("font manifest is not the approved local-only v1 pack")
    families = manifest.get("families")
    if not isinstance(families, list):
        raise GSAPCreativeError("font manifest families must be an array")
    by_role: dict[str, dict[str, Any]] = {}
    allowed_roles = {"expressive_display", "readable_body", "technical_labels_and_data"}
    for item in families:
        if not isinstance(item, dict) or item.get("role") not in allowed_roles:
            continue
        role = str(item["role"])
        font_rel = Path(str(item.get("file") or ""))
        license_rel = Path(str(item.get("license_file") or ""))
        font = safe_source_file(FONT_ROOT / font_rel, FONT_ROOT, f"{role} font")
        license_file = safe_source_file(FONT_ROOT / license_rel, FONT_ROOT, f"{role} license")
        if font.suffix.lower() not in ALLOWED_FONT_SUFFIXES:
            raise GSAPCreativeError(f"unsupported bundled font format: {font}")
        if item.get("license") != "SIL Open Font License 1.1" or item.get("cyrillic_basic") is not True:
            raise GSAPCreativeError(f"bundled font is not the approved Cyrillic OFL asset: {font}")
        if file_sha256(font) != item.get("sha256") or file_sha256(license_file) != item.get("license_sha256"):
            raise GSAPCreativeError(f"bundled font or license hash differs from manifest: {font}")
        if role in by_role:
            raise GSAPCreativeError(f"duplicate bundled font role: {role}")
        by_role[role] = {
            **item,
            "source_file": font,
            "source_license": license_file,
            "relative_file": font_rel,
            "relative_license": license_rel,
        }
    if set(by_role) != allowed_roles:
        raise GSAPCreativeError(f"font pack roles are incomplete: {sorted(by_role)}")
    return [
        by_role["expressive_display"],
        by_role["readable_body"],
        by_role["technical_labels_and_data"],
    ]


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


def split_fragments(value: str, count: int) -> list[str]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 1:
        if len(lines) > count:
            lines = lines[: count - 1] + [" ".join(lines[count - 1 :])]
        return lines
    tokens = value.split()
    if not tokens:
        return []
    groups = min(count, len(tokens))
    base, remainder = divmod(len(tokens), groups)
    result: list[str] = []
    cursor = 0
    for index in range(groups):
        size = base + (1 if index < remainder else 0)
        result.append(" ".join(tokens[cursor : cursor + size]))
        cursor += size
    return result


def validate_approved_visual(approved: Any, contract: EffectContract, spec: Mapping[str, Any]) -> list[str]:
    if approved.asset_type not in contract.metadata["asset_types"]:
        raise GSAPCreativeError(
            f"effect {contract.effect_type!r} requires approved asset_type in "
            f"{contract.metadata['asset_types']!r}, got {approved.asset_type!r}"
        )
    if approved.approved_text is None:
        raise GSAPCreativeError(f"effect {contract.effect_type!r} requires non-null approved_text")
    words = list(normalized_words(approved.approved_text))
    if not words:
        raise GSAPCreativeError("approved_text contains no visible words")
    if len(words) > contract.metadata["max_words"]:
        raise GSAPCreativeError(
            f"effect {contract.effect_type!r} accepts at most {contract.metadata['max_words']} approved words; "
            f"got {len(words)}"
        )
    if contract.effect_type == "kinetic_split_keyword":
        accent = spec["options"]["accent_word_index"]
        if accent >= len(words):
            raise GSAPCreativeError(
                f"options.accent_word_index={accent} is outside the {len(words)} approved word(s)"
            )
    if contract.effect_type in {"route_draw", "flip_before_after"} and len(words) < 2:
        raise GSAPCreativeError(f"effect {contract.effect_type!r} requires at least two approved words")
    count = 4 if contract.effect_type == "route_draw" else 2 if contract.effect_type == "flip_before_after" else 1
    fragments = split_fragments(approved.approved_text, count)
    if normalized_words("\n".join(fragments)) != tuple(words):
        raise GSAPCreativeError("derived visible fragments do not preserve the exact approved word sequence")
    return fragments


def config_for(
    approved: Any,
    spec: Mapping[str, Any],
    contract: EffectContract,
    font_pack: Sequence[Mapping[str, Any]],
    gsap_terms: Mapping[str, Any],
) -> dict[str, Any]:
    fragments = validate_approved_visual(approved, contract, spec)
    fonts: dict[str, dict[str, str]] = {}
    for output_role, item in zip(("display", "body", "mono"), font_pack, strict=True):
        fonts[output_role] = {
            "family": str(item["family"]),
            "file": str(Path("fonts") / item["relative_file"]),
            "license": "SIL Open Font License 1.1",
            "license_file": str(Path("fonts") / item["relative_license"]),
            "license_sha256": str(item["license_sha256"]),
        }
    composition = dict(spec["composition"])
    config = {
        "version": 1,
        "visual_id": approved.visual_id,
        "effect_type": contract.effect_type,
        "composition": {**composition, "transparent": True},
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
            "fragments": fragments,
            "accent_word_index": int(spec["options"].get("accent_word_index", 0)),
        },
        "layout": safe_area(int(composition["width"]), int(composition["height"])),
        "options": dict(spec["options"]),
        "runtime": {
            "network_allowed": False,
            "paid_apis": [],
            "gsap_version": PINNED_GSAP_VERSION,
            "gsap_terms": dict(gsap_terms),
            "bundles": ["vendor/gsap.min.js", *[f"vendor/{name}" for name in contract.metadata["plugin_files"]]],
        },
    }
    schema_config = {
        **config,
        "runtime": {key: value for key, value in config["runtime"].items() if key != "gsap_terms"},
    }
    validate_schema_instance(CONFIG_SCHEMA, schema_config, "generated effect config")
    return config


def file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved.relative_to(root.resolve())),
        "sha256": file_sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def assert_hashes_current(records: Mapping[Path, str]) -> None:
    for path, expected in records.items():
        if not path.is_file() or path.is_symlink() or file_sha256(path) != expected:
            raise GSAPCreativeError(f"scaffold input changed during operation: {path}")


def write_config_js(path: Path, config: Mapping[str, Any]) -> None:
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("window.SPRUT_GSAP_CREATIVE_CONFIG = " + encoded + ";\n", encoding="utf-8")


def scaffold(args: argparse.Namespace) -> Path:
    if not getattr(args, "accept_gsap_terms", False):
        raise GSAPCreativeError(
            "refusing to copy GSAP bytes without explicit --accept-gsap-terms"
        )
    edit_dir = canonical_edit_dir(args.edit_dir)
    if not isinstance(args.visual_id, str) or not SAFE_VISUAL_ID.fullmatch(args.visual_id):
        raise GSAPCreativeError("--visual-id is not a safe filesystem identifier")
    spec, spec_path, spec_sha256 = load_spec(edit_dir, args.spec)
    if spec["visual_id"] != args.visual_id:
        raise GSAPCreativeError("effect spec visual_id must exactly equal --visual-id")
    contract = effect_contract(spec["effect_type"])
    package, runtime_sources = verify_runtime(contract)
    gsap_terms = gsap_terms_record(package, GSAP_PACKAGE_ROOT.resolve())
    font_pack = load_font_pack()
    common_sources = [
        safe_source_file(ASSET_ROOT / "DESIGN.md", ASSET_ROOT, "DESIGN.md"),
        safe_source_file(ASSET_ROOT / "README.md", ASSET_ROOT, "README.md"),
        safe_source_file(ASSET_ROOT / "creative-effect.css", ASSET_ROOT, "creative effect CSS"),
        safe_source_file(ASSET_ROOT / "creative-effect-runtime.js", ASSET_ROOT, "creative effect runtime"),
        safe_source_file(SPEC_SCHEMA, ASSET_ROOT, "effect spec schema"),
        safe_source_file(CONFIG_SCHEMA, ASSET_ROOT, "effect config schema"),
        safe_source_file(FONT_MANIFEST, FONT_ROOT, "font manifest"),
    ]
    for executable in common_sources:
        if executable.suffix.lower() in {".js", ".css", ".html"}:
            assert_executable_is_offline(executable, "shared executable asset")

    # The approval gate and exact visual lookup run before any output directory is created.
    require_asset_gate(edit_dir)
    approved = load_approved_visual_plan_item(edit_dir, args.visual_id)
    config = config_for(approved, spec, contract, font_pack, gsap_terms)

    instances_root = path_under_edit(
        edit_dir,
        edit_dir / "animations" / "hyperframes" / "gsap-creative",
        "GSAP creative instances directory",
    )
    target = path_under_edit(instances_root, instances_root / args.visual_id, "GSAP creative instance")
    if target.exists():
        raise GSAPCreativeError(f"GSAP creative instance already exists and was left untouched: {target}")

    input_paths = [
        Path(__file__).resolve(),
        spec_path,
        contract.template_source,
        contract.metadata_source,
        approved.plan_snapshot.path,
        approved.approval_snapshot.path,
        *common_sources,
        *runtime_sources,
    ]
    for item in font_pack:
        input_paths.extend([item["source_file"], item["source_license"]])
    input_hashes = {path: file_sha256(path) for path in input_paths}
    if input_hashes[spec_path] != spec_sha256:
        raise GSAPCreativeError("effect spec changed between schema validation and scaffolding")

    instances_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{args.visual_id}-", dir=str(instances_root)))
    try:
        shutil.copy2(contract.template_source, temporary / "index.html")
        shutil.copy2(contract.metadata_source, temporary / "template.json")
        shutil.copy2(ASSET_ROOT / "DESIGN.md", temporary / "DESIGN.md")
        shutil.copy2(ASSET_ROOT / "README.md", temporary / "GSAP_CREATIVE_README.md")
        shutil.copy2(ASSET_ROOT / "creative-effect.css", temporary / "creative-effect.css")
        shutil.copy2(ASSET_ROOT / "creative-effect-runtime.js", temporary / "creative-effect-runtime.js")
        shutil.copy2(SPEC_SCHEMA, temporary / SPEC_SCHEMA.name)
        shutil.copy2(CONFIG_SCHEMA, temporary / CONFIG_SCHEMA.name)
        shutil.copy2(spec_path, temporary / "effect-spec.json")
        atomic_write_json(temporary / "config.json", config)
        write_config_js(temporary / "config.js", config)

        vendor = temporary / "vendor"
        vendor.mkdir()
        package_root = GSAP_PACKAGE_ROOT.resolve()
        shutil.copy2(package_root / "package.json", vendor / "gsap-package.json")
        shutil.copy2(package_root / "README.md", vendor / "GSAP_README.md")
        for bundle in runtime_sources[2:]:
            shutil.copy2(bundle, vendor / bundle.name)

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

        copied_files = sorted(
            path for path in temporary.rglob("*") if path.is_file() and path.name != "source-manifest.json"
        )
        bundle_records = [
            {
                **file_record(temporary / "vendor" / source.name, temporary),
                "source_path": str(source),
                "source_sha256": input_hashes[source],
            }
            for source in runtime_sources[2:]
        ]
        manifest = {
            "version": 1,
            "type": "sprut_gsap_creative_source_manifest",
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
                "semantic_plan": {"path": str(approved.plan_snapshot.path), "sha256": approved.plan_snapshot.sha256},
                "approval": {"path": str(approved.approval_snapshot.path), "sha256": approved.approval_snapshot.sha256},
            },
            "effect": {
                "effect_type": contract.effect_type,
                "template_version": contract.metadata["version"],
                "description": contract.metadata["description"],
                "plugins": [name.removesuffix(".min.js") for name in contract.metadata["plugin_files"]],
                "template_sha256": input_hashes[contract.template_source],
                "metadata_sha256": input_hashes[contract.metadata_source],
            },
            "request": {
                "path": str(spec_path),
                "sha256": input_hashes[spec_path],
                "copied_path": "effect-spec.json",
                "schema_sha256": input_hashes[SPEC_SCHEMA.resolve()],
            },
            "runtime": {
                "offline": True,
                "network_allowed": False,
                "paid_apis": [],
                "gsap": {
                    "version": package["version"],
                    "license": package["license"],
                    "license_url": gsap_terms["license_url"],
                    "package_json_file": gsap_terms["package_json_file"],
                    "package_json_sha256": gsap_terms["package_json_sha256"],
                    "package_metadata_sha256": input_hashes[(GSAP_PACKAGE_ROOT.resolve() / "package.json")],
                    "readme_file": gsap_terms["readme_file"],
                    "readme_sha256": gsap_terms["readme_sha256"],
                    "package_readme_sha256": input_hashes[(GSAP_PACKAGE_ROOT.resolve() / "README.md")],
                    "terms_explicitly_accepted": True,
                    "bundles": bundle_records,
                },
            },
            "inputs": [
                {"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size}
                for path, digest in sorted(input_hashes.items(), key=lambda item: str(item[0]))
            ],
            "files": [file_record(path, temporary) for path in copied_files],
            "review_requirement": "full_preview_user_approval",
            "new_effect_requirement": "three_to_four_frame_visual_sheet_before_full_render",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create one approval-bound offline GSAP/HyperFrames source instance without rendering"
    )
    parser.add_argument("--describe-json", action="store_true", help="print the audited effect catalog and exit")
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--visual-id")
    parser.add_argument("--spec", type=Path, help="strict effect request JSON under the canonical edit directory")
    parser.add_argument(
        "--accept-gsap-terms",
        action="store_true",
        help="confirm acceptance of the GSAP terms recorded in its package.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.describe_json:
        if (
            args.edit_dir is not None
            or args.visual_id is not None
            or args.spec is not None
            or args.accept_gsap_terms
        ):
            parser.error("--describe-json cannot be combined with scaffolding arguments")
        print(json.dumps(describe_catalog(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.edit_dir is None or args.visual_id is None or args.spec is None:
        parser.error("--edit-dir, --visual-id, and --spec are required unless --describe-json is used")
    target = scaffold(args)
    print(f"GSAP creative source scaffolded: {target}")
    print("rendered assets: none | network calls: 0 | paid APIs: none")
    print("next: lint/inspect/render locally, record provenance, build a 3-4-frame visual sheet, request visual approval")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssetGateError,
        GSAPCreativeError,
        OSError,
        SchemaDefinitionError,
        VisualProvenanceError,
        ValueError,
    ) as exc:
        print(f"scaffold_gsap_creative_effect: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
