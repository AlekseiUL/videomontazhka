#!/usr/bin/env python3
"""Install or verify Videomontazhka's pinned local browser runtime."""

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

from runtime_paths import (  # noqa: E402
    CACHE_HOME,
    CREATIVE_BROWSER_RUNTIME,
    HYPERFRAMES_RUNTIME,
)


SCRIPT = Path(__file__).resolve()
SKILL_ROOT = SCRIPT.parent.parent
ASSETS = SKILL_ROOT / "assets"
DEFAULT_RUNTIME = CREATIVE_BROWSER_RUNTIME
OFFICIAL_REGISTRY = "https://registry.npmjs.org/"
ALLOWED_LICENSES = {"MIT", "BSD-3-Clause", "ISC"}
EXPECTED_ESBUILD_VERSION = "0.25.12"
EXPECTED_ESBUILD_WRAPPER_SHA256 = "3d1037d9a128440856daec17f4358ebb465b73823858a88a5fe1ce702cc17944"
EXPECTED_BUNDLE_SHA256 = {
    "vendor/sprut-pixi.js": "0c1f5c124694222ba1ad1376fd9428399891e893bc68daddac9826e422b19f7b",
    "vendor/sprut-three.js": "01c0113cf0060714e437be19fbc370d02b7462d3160c773db05dd66f2c96b616",
}
SOURCE_FILES = {
    "package.json": (ASSETS / "creative-browser-package.v1.json", "56e6c37266399f68a9a501f4c997cb919220b41e84cca6ac548b3ad79dce87a2"),
    "package-lock.json": (ASSETS / "creative-browser-package-lock.v1.json", "f97405abc7598f16ba3bd90f4f191bb8b74fa0949e2ba178a99db3926c2777dd"),
    ".npmrc": (ASSETS / "creative-browser.npmrc", "fdfcc4e5bbc5bce2fb4bdca3c97460c57607b6cc6c31bdde97118e8f8ed4fc68"),
    "sources/pixi-entry.mjs": (ASSETS / "creative-browser-pixi-entry.v1.mjs", "05bb8bc5363accdb732ecc60dc677aa72d73348ca2fe3f4623f3aad738024c49"),
    "sources/three-entry.mjs": (ASSETS / "creative-browser-three-entry.v1.mjs", "120a42dc75c140337a0b5c2f33741a23e139e0076d50ff7f268224822e168143"),
    "README.md": (ASSETS / "creative-browser-runtime.README.md", "804227ac3baa1eb167bfccc55b4e825447d15c7d5aef14c9a032244f9475e950"),
}
TOP_LEVEL = {
    "pixi.js": "8.19.0",
    "pixi-filters": "6.1.5",
    "rough-notation": "0.5.1",
    "lottie-web": "5.13.0",
    "three": "0.185.1",
    "gl-transitions": "1.71.0",
}
VENDOR_COPIES = {
    "vendor/rough-notation.iife.js": (
        "node_modules/rough-notation/lib/rough-notation.iife.js",
        "f90aa0090e361ff48694b74e0319e109856decb3e757c280dfe25e3607f35fac",
    ),
    "vendor/lottie-light.min.js": (
        "node_modules/lottie-web/build/player/lottie_light.min.js",
        "9588432bec30c8ef8200bac4a67d8aaad881047bc2a6c9fa624d90ec96402410",
    ),
}
LICENSE_COPIES = {
    "licenses/pixi.js-MIT.txt": ("node_modules/pixi.js/LICENSE", "5ce7447bc57f7349ffc48338782fbcabe613696e00712b20d66bc58e780f9473"),
    "licenses/pixi-filters-MIT.txt": ("node_modules/pixi-filters/LICENSE", "d554721b2cd409d9bc075d743bb34f960e05ba55de1818a62fa60caf21ca6188"),
    "licenses/rough-notation-MIT.txt": ("node_modules/rough-notation/LICENSE", "e9754a00aebde654e80f40bcf41dab667d6a41dfbbd2912bcf14fb07d468bf71"),
    "licenses/lottie-web-MIT.txt": ("node_modules/lottie-web/LICENSE.md", "9d8d4d8b4bb99572ee8b51025a30b8493d949798cac465c6c15b1b29420bcb06"),
    "licenses/three-MIT.txt": ("node_modules/three/LICENSE", "8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc"),
    "licenses/gl-transitions-MIT.txt": ("node_modules/gl-transitions/LICENSE", "0db684e0150546743ae96d8ecf083be716786d834af7dbf8170ee8b6c247978d"),
}
GL_TRANSITIONS = {
    "fade": ("a16074d9812e440fb84e829a68ba7b078dd32182370d48bef05dbe71358e732e", "Neutral continuity or an actual time/place change"),
    "crosswarp": ("f3bb6ddca7dcaa1b9ec7e6c8673969f4e19b61cae6a3daaccfe163c0ebd3637c", "Concept-to-concept spatial handoff"),
    "directionalwipe": ("24e80bc7acc356f59f537d094c85640c7c425b30b51e2abfb354216010af635d", "Directional process or navigation change"),
    "circleopen": ("2dd91ce847cd8c2a8aeaeef8f58dfc80fc7bb693a8eac080e1deb6de321eb376", "Reveal one focal object or answer"),
    "pixelize": ("6fa7ae826e07a3d45fb7a0f8309310d38858d88fd48a098120d0207b66fbf2e1", "Digital-state change only"),
    "DefocusBlur": ("240b8de3f8ef08ba29445c8c3a37ba4861bd9fd7daab7dd16cae8ae76eb16aac", "Shift attention between contexts"),
    "FilmBurn": ("95f464e637f6a455543f608eab2417996e94f22539b9de7711a972fcfba19a9c", "Rare major chapter or memory/time shift"),
    "GlitchDisplace": ("7ebe05478a603b570fc8fd22abebe7501a09beddc249c17b433d9184a5dd5545", "Rare error, failure, or digital disruption beat"),
}
FORBIDDEN_REMOTE_MARKERS = (b"cdn.jsdelivr.net", b"unpkg.com", b"cdnjs.cloudflare.com")


class InstallError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise InstallError(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")
    return completed


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"JSON root must be an object: {path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_host() -> tuple[Path, Path, str, str]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise InstallError("this audited runtime build is restricted to Apple Silicon macOS")
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise InstallError("Node.js and npm are required")
    node_version = run([node, "--version"]).stdout.strip()
    match = re.fullmatch(r"v(\d+)\..*", node_version)
    if not match or int(match.group(1)) < 22:
        raise InstallError(f"Node.js 22+ is required, found {node_version!r}")
    npm_version = run([npm, "--version"]).stdout.strip()
    esbuild = (HYPERFRAMES_RUNTIME / "node_modules" / ".bin" / "esbuild").resolve()
    if not esbuild.is_file():
        raise InstallError(f"pinned HyperFrames esbuild is missing: {esbuild}")
    if run([str(esbuild), "--version"]).stdout.strip() != EXPECTED_ESBUILD_VERSION:
        raise InstallError("the local esbuild version differs from the audited creative build")
    if sha256(esbuild) != EXPECTED_ESBUILD_WRAPPER_SHA256:
        raise InstallError("the local esbuild wrapper hash differs from the audited creative build")
    return Path(node).resolve(), Path(npm).resolve(), node_version, npm_version


def check_sources() -> None:
    for _, (source, expected) in SOURCE_FILES.items():
        if not source.is_file() or source.is_symlink() or sha256(source) != expected:
            raise InstallError(f"audited installer input is missing or changed: {source}")


def copy_sources(work: Path) -> None:
    for relative, (source, _) in SOURCE_FILES.items():
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def audit_registry(npm: Path) -> None:
    configured = run([str(npm), "config", "get", "registry"]).stdout.strip()
    if configured.rstrip("/") + "/" != OFFICIAL_REGISTRY:
        raise InstallError(f"npm registry must be {OFFICIAL_REGISTRY}, found {configured!r}")
    run([str(npm), "ping", f"--registry={OFFICIAL_REGISTRY}", "--json"])


def validate_packages(work: Path) -> list[dict[str, Any]]:
    lock = load_json(work / "package-lock.json")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise InstallError("package lock has no canonical packages table")
    if packages[""].get("dependencies") != TOP_LEVEL:
        raise InstallError("top-level dependency set differs from the audited allowlist")
    records = []
    for lock_path, locked in sorted(packages.items()):
        if not lock_path:
            continue
        if not isinstance(locked, dict) or not lock_path.startswith("node_modules/"):
            raise InstallError(f"unexpected package-lock record: {lock_path!r}")
        resolved = str(locked.get("resolved") or "")
        integrity = str(locked.get("integrity") or "")
        declared_license = str(locked.get("license") or "")
        if not resolved.startswith(OFFICIAL_REGISTRY) or not integrity.startswith("sha512-"):
            raise InstallError(f"package is not integrity-pinned to the official registry: {lock_path}")
        if declared_license not in ALLOWED_LICENSES:
            raise InstallError(f"package license is not approved: {lock_path} ({declared_license!r})")
        package_dir = work / lock_path
        package_json = load_json(package_dir / "package.json")
        if package_json.get("version") != locked.get("version"):
            raise InstallError(f"installed version differs from package lock: {lock_path}")
        if package_json.get("license") != declared_license:
            raise InstallError(f"installed license metadata differs from package lock: {lock_path}")
        license_files = sorted(
            item.name for item in package_dir.iterdir() if item.is_file() and re.match(r"(?i)^licen[cs]e", item.name)
        )
        records.append(
            {
                "name": package_json.get("name"),
                "version": package_json.get("version"),
                "license": declared_license,
                "integrity": integrity,
                "resolved": resolved,
                "license_files_in_package": license_files,
                "direct": package_json.get("name") in TOP_LEVEL,
            }
        )
    installed_direct = {item["name"]: item["version"] for item in records if item["direct"]}
    if installed_direct != TOP_LEVEL:
        raise InstallError("installed direct package set differs from the audited allowlist")
    return records


def checked_copy(work: Path, source_relative: str, destination_relative: str, expected: str) -> None:
    source = work / source_relative
    if not source.is_file() or source.is_symlink() or sha256(source) != expected:
        raise InstallError(f"installed package artifact differs from the audited hash: {source_relative}")
    destination = work / destination_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def build_bundles(work: Path, esbuild: Path) -> None:
    builds = (
        ("sources/pixi-entry.mjs", "vendor/sprut-pixi.js", "SPRUT_PIXI"),
        ("sources/three-entry.mjs", "vendor/sprut-three.js", "SPRUT_THREE"),
    )
    for entry, output, global_name in builds:
        destination = work / output
        destination.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                str(esbuild),
                entry,
                "--bundle",
                "--format=iife",
                f"--global-name={global_name}",
                "--platform=browser",
                "--target=chrome120",
                "--minify",
                "--legal-comments=none",
                f"--outfile={output}",
            ],
            cwd=work,
        )
        if sha256(destination) != EXPECTED_BUNDLE_SHA256[output]:
            raise InstallError(f"deterministic bundle hash differs from the audited build: {output}")
        payload = destination.read_bytes()
        if any(marker in payload for marker in FORBIDDEN_REMOTE_MARKERS):
            raise InstallError(f"curated bundle contains a prohibited CDN marker: {output}")


def copy_vendor(work: Path) -> list[dict[str, Any]]:
    for destination, (source, expected) in VENDOR_COPIES.items():
        checked_copy(work, source, destination, expected)
    for destination, (source, expected) in LICENSE_COPIES.items():
        checked_copy(work, source, destination, expected)
    allowlist = []
    for name, (expected, semantic_use) in GL_TRANSITIONS.items():
        source = f"node_modules/gl-transitions/transitions/{name}.glsl"
        destination = f"vendor/transitions/{name}.glsl"
        checked_copy(work, source, destination, expected)
        header = (work / destination).read_text(encoding="utf-8")[:1024]
        if not re.search(r"(?im)^\s*//\s*License:\s*MIT\s*$", header):
            raise InstallError(f"shader has no reviewed MIT header: {name}")
        if not re.search(r"(?im)^\s*//\s*Author:", header):
            raise InstallError(f"shader has no reviewed author header: {name}")
        allowlist.append(
            {
                "id": name,
                "file": destination,
                "sha256": expected,
                "license": "MIT",
                "header_reviewed": True,
                "semantic_use": semantic_use,
            }
        )
    return allowlist


def npm_audit(work: Path, npm: Path) -> dict[str, int]:
    completed = run([str(npm), "audit", "--omit=dev", "--json"], cwd=work, check=False)
    try:
        report = json.loads(completed.stdout)
        counts = report["metadata"]["vulnerabilities"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InstallError(f"npm audit did not return a usable report: {completed.stderr.strip()}") from exc
    expected_keys = {"info", "low", "moderate", "high", "critical", "total"}
    if set(counts) != expected_keys or any(not isinstance(counts[key], int) for key in expected_keys):
        raise InstallError("npm audit vulnerability counts are malformed")
    if counts["total"] != 0 or completed.returncode != 0:
        raise InstallError(f"npm audit found vulnerabilities: {counts}")
    return counts


def locate_chrome() -> Path:
    configured = os.environ.get("VIDEOMONTAZHKA_CHROME_BIN")
    if configured:
        explicit = Path(configured).expanduser().resolve(strict=False)
        if explicit.is_file() and os.access(explicit, os.X_OK):
            return explicit
        raise InstallError(f"VIDEOMONTAZHKA_CHROME_BIN is not executable: {explicit}")

    roots = [
        CACHE_HOME / "hyperframes" / "chrome",
        HYPERFRAMES_RUNTIME / "chrome",
        # HyperFrames' own historical cache remains a read-only discovery
        # fallback so an existing audited installation keeps working.
        Path.home() / ".cache" / "hyperframes" / "chrome",
    ]
    candidates = [
        item
        for cache in roots
        if cache.is_dir()
        for item in cache.rglob("chrome-headless-shell")
        if item.is_file() and os.access(item, os.X_OK)
    ]
    if not candidates:
        raise InstallError("the pinned local HyperFrames Chrome runtime is missing")
    return max(candidates, key=lambda item: item.stat().st_mtime_ns).resolve()


def browser_smoke(work: Path, node: Path, chrome: Path) -> dict[str, Any]:
    result = run(
        [
            str(node),
            str(SKILL_ROOT / "scripts" / "smoke_creative_browser_runtime.mjs"),
            "--runtime",
            str(work),
            "--chrome",
            str(chrome),
            "--puppeteer-root",
            str(HYPERFRAMES_RUNTIME),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(f"browser smoke test returned invalid JSON: {result.stdout!r}") from exc
    if payload.get("ok") is not True or payload.get("external_requests") != []:
        raise InstallError(f"browser smoke test failed: {payload}")
    return payload


def runtime_file_records(root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(root.rglob("*")):
        if path.name == "RUNTIME_MANIFEST.json":
            continue
        if path.is_symlink():
            raise InstallError(f"runtime must not contain symlinks: {path}")
        if path.is_file():
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return records


def verify_runtime(runtime: Path) -> dict[str, Any]:
    runtime = runtime.expanduser().resolve()
    if not runtime.is_dir() or runtime.is_symlink():
        raise InstallError(f"runtime is not a regular directory: {runtime}")
    manifest = load_json(runtime / "RUNTIME_MANIFEST.json")
    required = {
        "version",
        "runtime_id",
        "policy",
        "host",
        "tools",
        "dependencies",
        "packages",
        "capabilities",
        "gl_transition_policy",
        "deferred_or_blocked",
        "security_audit",
        "browser_smoke",
        "files",
    }
    if set(manifest) != required or manifest.get("version") != 1 or manifest.get("runtime_id") != "sprut-creative-browser-v1":
        raise InstallError("runtime manifest contract is not canonical v1")
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or policy.get("network_required_for_render") is not False:
        raise InstallError("runtime manifest does not enforce offline rendering")
    if policy.get("remote_media_inputs") != "prohibited" or policy.get("remotion") != "prohibited":
        raise InstallError("runtime manifest does not enforce media/Remotion exclusions")
    dependencies = manifest.get("dependencies")
    if dependencies != TOP_LEVEL:
        raise InstallError("runtime manifest dependency set differs from the allowlist")
    expected_records = manifest.get("files")
    if not isinstance(expected_records, list) or not expected_records:
        raise InstallError("runtime manifest has no file inventory")
    expected = {}
    for record in expected_records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size_bytes"}:
            raise InstallError("runtime file record is malformed")
        relative = record["path"]
        if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
            raise InstallError("runtime file record has an unsafe path")
        if relative in expected:
            raise InstallError("runtime file inventory contains a duplicate path")
        expected[relative] = record
    current_records = runtime_file_records(runtime)
    current = {record["path"]: record for record in current_records}
    if current != expected:
        raise InstallError("runtime file inventory or hashes differ from RUNTIME_MANIFEST.json")
    if (runtime / "node_modules").exists():
        raise InstallError("minimal runtime must not retain the npm build tree")
    for path in (runtime / "vendor").rglob("*"):
        if path.is_file() and any(marker in path.read_bytes() for marker in FORBIDDEN_REMOTE_MARKERS):
            raise InstallError(f"runtime artifact contains a prohibited CDN marker: {path}")
        if path.is_file() and b"remotion" in path.read_bytes().lower():
            raise InstallError(f"runtime artifact unexpectedly refers to Remotion: {path}")
    transition_policy = manifest.get("gl_transition_policy")
    if not isinstance(transition_policy, dict) or transition_policy.get("unlisted_shader_default") != "blocked":
        raise InstallError("shader default must fail closed")
    allowlist = transition_policy.get("allowlist")
    if not isinstance(allowlist, list) or {item.get("id") for item in allowlist if isinstance(item, dict)} != set(GL_TRANSITIONS):
        raise InstallError("shader allowlist differs from the audited set")
    return manifest


def create_runtime(target: Path) -> dict[str, Any]:
    check_sources()
    node, npm, node_version, npm_version = check_host()
    audit_registry(npm)
    target = target.expanduser().resolve()
    if target == Path.home().resolve() or target == Path("/") or len(target.parts) < 4:
        raise InstallError(f"refusing unsafe runtime target: {target}")
    if target.exists():
        manifest = verify_runtime(target)
        print(f"READY {target}")
        return manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{target.name}.install-", dir=target.parent))
    try:
        copy_sources(work)
        run(
            [str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund", f"--registry={OFFICIAL_REGISTRY}"],
            cwd=work,
        )
        package_records = validate_packages(work)
        audit_counts = npm_audit(work, npm)
        esbuild = (HYPERFRAMES_RUNTIME / "node_modules" / ".bin" / "esbuild").resolve()
        build_bundles(work, esbuild)
        allowlist = copy_vendor(work)
        third_party = {
            "version": 1,
            "policy": "approved licenses only; package metadata is bound to npm integrity",
            "packages": package_records,
        }
        atomic_json(work / "THIRD_PARTY_PACKAGES.json", third_party)
        chrome = locate_chrome()
        smoke = browser_smoke(work, node, chrome)
        shutil.rmtree(work / "node_modules")
        manifest = {
            "version": 1,
            "runtime_id": "sprut-creative-browser-v1",
            "policy": {
                "local_only": True,
                "network_required_for_render": False,
                "remote_media_inputs": "prohibited",
                "cdn": "prohibited",
                "telemetry": "not_configured_or_required",
                "remotion": "prohibited",
                "lottie_input": "user-owned local or inline animationData only",
                "new_effect_default": "blocked pending semantic use, license, preview, and QA review",
            },
            "host": {"system": "Darwin", "architecture": "arm64", "node": node_version, "npm": npm_version},
            "tools": {
                "esbuild": {
                    "version": EXPECTED_ESBUILD_VERSION,
                    "path_at_install": str(esbuild),
                    "sha256": sha256(esbuild),
                },
                "chrome": {
                    "path_at_install": str(chrome),
                    "version": run([str(chrome), "--version"]).stdout.strip(),
                    "sha256": sha256(chrome),
                },
            },
            "dependencies": TOP_LEVEL,
            "packages": {"inventory": "THIRD_PARTY_PACKAGES.json", "package_lock_sha256": sha256(work / "package-lock.json")},
            "capabilities": {
                "pixi": {
                    "role": "short GPU-accelerated 2D accents and particles",
                    "filters": ["advanced-bloom", "glitch", "motion-blur", "outline", "pixelate", "rgb-split", "shockwave", "zoom-blur"],
                    "restrictions": ["semantic motivation required", "do not substitute for program color grade", "one dominant accent action at a time"],
                },
                "rough_notation": {"role": "hand-drawn local underline, circle, box, highlight, or bracket annotations"},
                "lottie_web": {"role": "local vector icons and micro-animations", "url_loading": "prohibited"},
                "three": {"role": "short approved 3D typography, depth, or diagram insert", "default": "off"},
                "gl_transitions": {"role": "licensed full-frame transition source", "default": "hard cut unless meaning motivates transition"},
            },
            "gl_transition_policy": {
                "collection_license": "MIT",
                "collection_package_does_not_authorize_all_shaders": True,
                "unlisted_shader_default": "blocked",
                "new_shader_requirements": ["review exact file header and license", "pin file SHA-256", "document semantic use", "pass visual sheet and transition QA"],
                "allowlist": allowlist,
            },
            "deferred_or_blocked": [
                {
                    "component": "@lottiefiles/dotlottie-web",
                    "version_checked": "0.79.0",
                    "status": "blocked",
                    "reason_code": "default_bundle_embeds_remote_wasm_fallback_urls",
                    "replacement": "lottie-web@5.13.0 with inline/local animationData",
                },
                {"component": "Remotion", "status": "prohibited", "reason_code": "SPRUT license and cost policy"},
                {"component": "G'MIC", "status": "not_installed", "reason_code": "outside approved minimal browser runtime"},
                {"component": "Blender", "status": "not_installed", "reason_code": "outside approved minimal browser runtime"},
                {"component": "Depth Anything model", "status": "not_installed", "reason_code": "model runtime deferred to a separately approved pilot"},
            ],
            "security_audit": {"npm_registry": OFFICIAL_REGISTRY, "ignore_scripts": True, "vulnerabilities": audit_counts},
            "browser_smoke": smoke,
            "files": runtime_file_records(work),
        }
        atomic_json(work / "RUNTIME_MANIFEST.json", manifest)
        verify_runtime(work)
        work.replace(target)
        manifest = verify_runtime(target)
        print(f"INSTALLED {target}")
        return manifest
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="rerun the offline headless-browser smoke test")
    args = parser.parse_args()
    try:
        if args.verify_only:
            verify_runtime(args.runtime_dir)
            print(f"PASS {args.runtime_dir.expanduser().resolve()}")
            if args.smoke:
                node, _, _, _ = check_host()
                smoke = browser_smoke(args.runtime_dir.expanduser().resolve(), node, locate_chrome())
                print(f"SMOKE PASS {json.dumps(smoke, ensure_ascii=False, sort_keys=True)}")
        else:
            create_runtime(args.runtime_dir)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
