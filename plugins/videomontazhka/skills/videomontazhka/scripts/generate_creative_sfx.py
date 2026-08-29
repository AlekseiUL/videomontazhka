#!/usr/bin/env python3
"""Render deterministic, asset-free creative SFX from a strict JSON spec.

The production path is deliberately project scoped and fail closed: the spec,
WAV, and provenance sidecar must live under the canonical ``edit/`` tree and
the current semantic approval must pass before any output directory is made.
Discovery modes do not write files and let the creative router validate this
tool without opening a project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from schema_check import SchemaDefinitionError, Validator


RATE = 48_000
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "assets" / "creative-sfx.schema.v1.json"
GENERATOR = Path(__file__).resolve()


PRESETS: dict[str, dict[str, Any]] = {
    "semantic_hit": {
        "label": "Semantic hit",
        "default_duration_s": 0.34,
        "duration_range_s": [0.18, 0.75],
        "default_gain_db": -16.0,
        "recommended_for": [
            "a source-backed conclusion lands",
            "a large approved keyword finishes its entrance",
            "a comparison resolves to the chosen side",
        ],
        "avoid_when": [
            "the sentence is still building",
            "another low-frequency hit occurred within roughly one second",
        ],
        "motion_pairing": ["punch-in hold", "kinetic keyword landing", "stat payoff"],
        "speech_safe": False,
    },
    "soft_pop": {
        "label": "Soft pop",
        "default_duration_s": 0.18,
        "duration_range_s": [0.10, 0.40],
        "default_gain_db": -20.0,
        "recommended_for": [
            "a small approved label or icon appears",
            "one step in a process becomes active",
        ],
        "avoid_when": ["several elements appear in rapid succession", "the speech is intimate or quiet"],
        "motion_pairing": ["icon scale-in", "step activation", "small callout"],
        "speech_safe": True,
    },
    "ui_tick": {
        "label": "UI tick",
        "default_duration_s": 0.09,
        "duration_range_s": [0.06, 0.20],
        "default_gain_db": -24.0,
        "recommended_for": [
            "a real interface selection is shown",
            "a cursor or diagram node reaches a meaningful target",
        ],
        "avoid_when": ["there is no visible interaction", "ticks would imitate every word or cut"],
        "motion_pairing": ["screen callout", "cursor arrival", "node selection"],
        "speech_safe": True,
    },
    "digital_reveal": {
        "label": "Digital reveal",
        "default_duration_s": 0.46,
        "duration_range_s": [0.22, 0.90],
        "default_gain_db": -19.0,
        "recommended_for": [
            "technical text, a code fragment, or a data object is revealed",
            "a conceptual object changes state",
        ],
        "avoid_when": ["the visual is organic or emotional", "the reveal has no semantic state change"],
        "motion_pairing": ["scramble text", "data-node reveal", "diagram state change"],
        "speech_safe": True,
    },
    "transition_whoosh": {
        "label": "Transition whoosh",
        "default_duration_s": 0.66,
        "duration_range_s": [0.32, 1.20],
        "default_gain_db": -20.0,
        "recommended_for": [
            "an approved full-frame chapter bridge changes topic",
            "a motivated directional wipe follows visible motion",
        ],
        "avoid_when": ["an ordinary hard cut is sufficient", "the transition has no direction or chapter purpose"],
        "motion_pairing": ["chapter bridge", "directional wipe", "fast diagram travel"],
        "speech_safe": False,
    },
    "reverse_swell": {
        "label": "Reverse swell",
        "default_duration_s": 0.92,
        "duration_range_s": [0.45, 1.60],
        "default_gain_db": -22.0,
        "recommended_for": [
            "a reveal is anticipated before its payoff word",
            "an ending card needs a restrained lead-in",
        ],
        "avoid_when": ["it would telegraph a weak or unsupported payoff", "speech has no room for a lead-in"],
        "motion_pairing": ["masked reveal", "end-card entrance", "pre-payoff build"],
        "speech_safe": False,
    },
    "sub_drop": {
        "label": "Sub drop",
        "default_duration_s": 0.72,
        "duration_range_s": [0.38, 1.20],
        "default_gain_db": -20.0,
        "recommended_for": [
            "one exceptional correction or consequence needs weight",
            "a chapter opens on a genuinely high-stakes contrast",
        ],
        "avoid_when": ["mobile playback is the only target and no headphones check is possible", "more than once per short"],
        "motion_pairing": ["strong punch-in", "contrast inversion", "freeze-frame payoff"],
        "speech_safe": False,
    },
    "glitch_accent": {
        "label": "Glitch accent",
        "default_duration_s": 0.24,
        "duration_range_s": [0.12, 0.48],
        "default_gain_db": -22.0,
        "recommended_for": [
            "the meaning itself concerns an error, failure, corruption, or broken state",
            "a technical before/after explicitly switches into the failed state",
        ],
        "avoid_when": ["used only to make a cut look exciting", "applied to a person without an error metaphor"],
        "motion_pairing": ["error-state reveal", "brief RGB split", "digital discontinuity"],
        "speech_safe": False,
    },
    "marker_stroke": {
        "label": "Marker stroke",
        "default_duration_s": 0.42,
        "duration_range_s": [0.20, 0.90],
        "default_gain_db": -23.0,
        "recommended_for": [
            "an orange underline, circle, or arrow is visibly drawn",
            "one existing screen detail is annotated for comprehension",
        ],
        "avoid_when": ["there is no visible drawing motion", "the annotation is purely decorative"],
        "motion_pairing": ["rough underline", "hand-drawn circle", "diagram arrow"],
        "speech_safe": True,
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def discovery(presets_only: bool = False) -> dict[str, Any]:
    presets = [
        {"id": preset_id, **value}
        for preset_id, value in sorted(PRESETS.items())
    ]
    if presets_only:
        return {"version": 1, "tool_id": "sprut.audio.procedural_sfx.v1", "presets": presets}
    return {
        "version": 1,
        "tool_id": "sprut.audio.procedural_sfx.v1",
        "command": "scripts/generate_creative_sfx.py",
        "availability": {
            "network_required": False,
            "paid_api_required": False,
            "external_audio_assets_required": False,
            "runtime": "local_numpy",
        },
        "production_contract": {
            "semantic_approval_required": True,
            "spec_scope": "canonical_edit_tree",
            "output_scope": "canonical_edit_tree",
            "deterministic_for_same_spec_and_generator": True,
            "sample_rate_hz": RATE,
            "channels": 2,
            "sample_format": "pcm_s16le",
            "provenance_sidecar": "<output>.provenance.json",
        },
        "edl_adapter": {
            "provenance_field": "edl_audio_overlay_template",
            "without_rhythm_anchor": "router must add exactly one approved EDL timing anchor",
            "with_rhythm_anchor": (
                "rhythm_anchor_s is an approved output-timeline candidate; anchor_alignment "
                "converts it to start_in_output"
            ),
            "warning": "a rhythm candidate never authorizes an effect or overrides speech meaning",
        },
        "routing": {
            "decision_order": [
                "semantic purpose exists in the approved edit",
                "a visual or editorial landing point needs an accent",
                "the matching preset's avoid_when rules do not apply",
                "speech intelligibility remains dominant at preview loudness",
            ],
            "default_policy": "silence; add an SFX only when it clarifies or gives weight to a real event",
            "maximum_density_guidance": "prefer one meaningful accent over repeated decoration",
        },
        "spec_schema": str(SCHEMA),
        "presets": presets,
    }


def load_and_validate_spec(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read creative SFX spec/schema: {exc}") from exc
    errors = Validator(schema).validate(value)
    if errors:
        rendered = "; ".join(error.render() for error in errors[:8])
        raise ValueError(f"creative SFX spec does not match schema: {rendered}")
    if not isinstance(value, dict):
        raise ValueError("creative SFX spec must be an object")
    if not str(value["purpose"]).strip():
        raise ValueError("creative SFX purpose cannot be blank")
    if value.get("section_id") is not None and not str(value["section_id"]).strip():
        raise ValueError("creative SFX section_id cannot be blank")
    has_anchor = value.get("rhythm_anchor_s") is not None
    has_alignment = value.get("anchor_alignment") is not None
    if has_anchor != has_alignment:
        raise ValueError("rhythm_anchor_s and anchor_alignment must be supplied together")
    preset = PRESETS[str(value["preset"])]
    duration = float(value.get("duration_s", preset["default_duration_s"]))
    minimum, maximum = (float(item) for item in preset["duration_range_s"])
    if not minimum <= duration <= maximum:
        raise ValueError(
            f"duration_s for {value['preset']} must be between {minimum:g} and {maximum:g} seconds"
        )
    if has_anchor and value["anchor_alignment"] == "end_at_anchor":
        if float(value["rhythm_anchor_s"]) + 1e-9 < duration:
            raise ValueError("end_at_anchor would place the SFX before programme time zero")
    return value, hashlib.sha256(payload).hexdigest()


def moving_average(signal: np.ndarray, width: int) -> np.ndarray:
    width = max(1, min(int(width), len(signal)))
    if width == 1:
        return signal.copy()
    padded = np.pad(signal, (width // 2, width - 1 - width // 2), mode="edge")
    cumulative = np.cumsum(np.insert(padded, 0, 0.0))
    return (cumulative[width:] - cumulative[:-width]) / width


def phase_from_frequency(frequency: np.ndarray) -> np.ndarray:
    return 2.0 * math.pi * np.cumsum(frequency, dtype=np.float64) / RATE


def fade(signal: np.ndarray, fade_in_s: float, fade_out_s: float) -> np.ndarray:
    result = np.array(signal, dtype=np.float64, copy=True)
    fade_in = min(len(result), round(fade_in_s * RATE))
    fade_out = min(len(result), round(fade_out_s * RATE))
    if fade_in:
        result[:fade_in] *= np.sin(np.linspace(0.0, math.pi / 2.0, fade_in)) ** 2
    if fade_out:
        result[-fade_out:] *= np.cos(np.linspace(0.0, math.pi / 2.0, fade_out)) ** 2
    return result


def stereo_pan(mono: np.ndarray, pan: np.ndarray, width: float) -> np.ndarray:
    controlled = np.clip(pan * width, -1.0, 1.0)
    angle = (controlled + 1.0) * math.pi / 4.0
    return np.column_stack((mono * np.cos(angle), mono * np.sin(angle))) * math.sqrt(2.0)


def synthesize(spec: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    preset_id = str(spec["preset"])
    preset = PRESETS[preset_id]
    duration = float(spec.get("duration_s", preset["default_duration_s"]))
    count = max(1, round(duration * RATE))
    t = np.arange(count, dtype=np.float64) / RATE
    progress = np.clip(t / duration, 0.0, 1.0)
    intensity = float(spec.get("intensity", 0.60))
    brightness = float(spec.get("brightness", 0.55))
    width = float(spec.get("stereo_width", 0.35))
    rng = np.random.default_rng(int(spec["seed"]))
    noise = rng.normal(0.0, 1.0, count)
    pan = np.zeros(count, dtype=np.float64)

    if preset_id == "semantic_hit":
        low_frequency = 118.0 - (50.0 + 22.0 * intensity) * (1.0 - np.exp(-progress * 5.0))
        low = np.sin(phase_from_frequency(low_frequency)) * np.exp(-t * (7.5 - intensity * 2.0))
        mid = np.sin(phase_from_frequency(np.full(count, 330.0 + 210.0 * brightness)))
        mid *= np.exp(-t * 16.0)
        attack = (noise - moving_average(noise, 22 + round(30 * (1.0 - brightness)))) * np.exp(-t * 55.0)
        mono = 0.72 * low + (0.12 + 0.13 * intensity) * mid + 0.17 * attack
        mono = fade(mono, 0.0015, min(0.10, duration * 0.35))
    elif preset_id == "soft_pop":
        frequency = (610.0 + 460.0 * brightness) * np.exp(-progress * (2.2 + intensity)) + 120.0
        body = np.sin(phase_from_frequency(frequency)) * np.exp(-t * (22.0 - 7.0 * intensity))
        click = (noise - moving_average(noise, 12)) * np.exp(-t * 80.0)
        mono = fade(0.88 * body + 0.08 * click, 0.001, min(0.05, duration * 0.35))
    elif preset_id == "ui_tick":
        frequency = np.linspace(1900.0 + 1100.0 * brightness, 850.0, count)
        tone = np.sin(phase_from_frequency(frequency)) * np.exp(-t * 52.0)
        overtone = np.sin(phase_from_frequency(frequency * 1.93)) * np.exp(-t * 75.0)
        mono = fade(0.82 * tone + 0.14 * overtone, 0.0005, min(0.025, duration * 0.30))
    elif preset_id == "digital_reveal":
        base_frequency = 330.0 + 340.0 * brightness
        chirp_frequency = base_frequency + 1150.0 * progress**1.7
        chirp = np.sin(phase_from_frequency(chirp_frequency))
        pulse_rate = 17.0 + round(19.0 * intensity)
        gate = (np.sin(2.0 * math.pi * pulse_rate * t) > (-0.35 + 0.45 * intensity)).astype(float)
        envelope = np.sin(math.pi * progress) ** 1.1
        granular = moving_average(noise, 5 + round(10 * (1.0 - brightness))) * gate
        mono = fade((0.47 * chirp * gate + 0.18 * granular) * envelope, 0.008, 0.045)
        pan = np.sin(2.0 * math.pi * (1.5 + intensity) * progress)
    elif preset_id == "transition_whoosh":
        low_noise = moving_average(noise, 110 - round(70 * brightness))
        high_noise = noise - moving_average(noise, 18 + round(44 * (1.0 - brightness)))
        envelope = np.sin(math.pi * progress) ** (1.8 - 0.6 * intensity)
        sweep_frequency = 130.0 + (760.0 + 1250.0 * brightness) * progress**2
        sweep = np.sin(phase_from_frequency(sweep_frequency))
        mono = fade((0.58 * low_noise + 0.12 * high_noise + 0.10 * sweep) * envelope, 0.025, 0.07)
        pan = np.linspace(-1.0, 1.0, count)
    elif preset_id == "reverse_swell":
        smooth = moving_average(noise, 95 - round(58 * brightness))
        air = noise - moving_average(noise, 16 + round(26 * (1.0 - brightness)))
        envelope = progress ** (1.55 - 0.55 * intensity)
        frequency = 170.0 + (520.0 + 780.0 * brightness) * progress**1.8
        tone = np.sin(phase_from_frequency(frequency))
        mono = fade((0.52 * smooth + 0.10 * air + 0.10 * tone) * envelope, 0.035, 0.018)
        pan = np.linspace(-0.30, 0.30, count)
    elif preset_id == "sub_drop":
        frequency = 102.0 - (50.0 + 18.0 * intensity) * progress**0.65
        fundamental = np.sin(phase_from_frequency(frequency))
        harmonic = np.sin(phase_from_frequency(frequency * 2.02))
        envelope = np.exp(-t * (3.8 - 1.2 * intensity))
        mono = fade((0.88 * fundamental + 0.10 * harmonic) * envelope, 0.006, min(0.14, duration * 0.30))
    elif preset_id == "glitch_accent":
        block = max(12, round(RATE * (0.0025 + 0.005 * (1.0 - intensity))))
        held = np.repeat(noise[::block], block)[:count]
        carrier_frequency = 250.0 + 1800.0 * brightness + 700.0 * (progress > 0.52)
        carrier = np.sign(np.sin(phase_from_frequency(carrier_frequency)))
        gate = (np.sin(2.0 * math.pi * (28.0 + 24.0 * intensity) * t) > 0.1).astype(float)
        envelope = np.sin(math.pi * progress) ** 0.65
        mono = fade((0.34 * held + 0.21 * carrier) * gate * envelope, 0.003, 0.015)
        pan = np.where((np.arange(count) // block) % 2 == 0, -1.0, 1.0)
    elif preset_id == "marker_stroke":
        scratch = noise - moving_average(noise, 30 + round(42 * (1.0 - brightness)))
        texture = 0.62 + 0.18 * np.sin(2.0 * math.pi * (31.0 + 20.0 * intensity) * t)
        envelope = np.sin(math.pi * progress) ** 0.35
        low_body = moving_average(noise, 180) * 0.20
        mono = fade((0.37 * scratch * texture + low_body) * envelope, 0.012, 0.028)
        pan = np.linspace(-0.35, 0.35, count)
    else:  # pragma: no cover - schema and lookup make this unreachable.
        raise ValueError(f"unknown creative SFX preset: {preset_id}")

    stereo = stereo_pan(mono, pan, width)
    stereo -= np.mean(stereo, axis=0, keepdims=True)
    peak = float(np.max(np.abs(stereo)))
    if not math.isfinite(peak) or peak <= 1e-12:
        raise ValueError(f"preset {preset_id} produced an invalid silent signal")
    target_peak = 10.0 ** (float(spec["gain_db"]) / 20.0)
    stereo = np.clip(stereo / peak * target_peak, -1.0, 1.0)
    metadata = {
        "duration_s": count / RATE,
        "frames": count,
        "sample_rate_hz": RATE,
        "channels": 2,
        "sample_format": "pcm_s16le",
        "peak_dbfs": 20.0 * math.log10(float(np.max(np.abs(stereo)))),
    }
    return stereo, metadata


def write_wav_atomic(path: Path, stereo: np.ndarray) -> None:
    samples = np.round(np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".part.wav", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes(samples.tobytes())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".part.json", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relative(edit_dir: Path, path: Path) -> str:
    return path.relative_to(edit_dir).as_posix()


def render(edit_dir_value: Path, spec_value: Path, output_value: Path, force: bool) -> dict[str, Any]:
    edit_dir = canonical_edit_dir(edit_dir_value)
    spec_path = path_under_edit(edit_dir, spec_value, "creative SFX spec")
    output = path_under_edit(edit_dir, output_value, "creative SFX output")
    sidecar = path_under_edit(
        edit_dir, output.with_name(f"{output.name}.provenance.json"), "creative SFX provenance"
    )
    if output.suffix.lower() != ".wav":
        raise ValueError("creative SFX output must use .wav")
    if not spec_path.is_file():
        raise ValueError(f"creative SFX spec not found: {spec_path}")
    require_asset_gate(edit_dir)
    plan = edit_dir / "semantic_plan.json"
    approval = edit_dir / "approval.json"
    control_hashes = {
        "plan": sha256_file(plan),
        "approval": sha256_file(approval),
        "generator": sha256_file(GENERATOR),
        "schema": sha256_file(SCHEMA),
    }
    spec, spec_sha256 = load_and_validate_spec(spec_path)
    try:
        plan_value = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read approved semantic plan: {exc}") from exc
    if spec.get("section_id") is not None:
        narrative_ids = {
            str(item.get("id"))
            for item in plan_value.get("narrative") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        if spec["section_id"] not in narrative_ids:
            raise ValueError(
                f"creative SFX section_id is not in the approved narrative: {spec['section_id']}"
            )
    existing = [path for path in (output, sidecar) if path.exists()]
    if existing and not force:
        raise ValueError(f"output exists; use --force to replace: {existing[0]}")

    stereo, audio = synthesize(spec)
    require_asset_gate(edit_dir)
    current_hashes = {
        "plan": sha256_file(plan),
        "approval": sha256_file(approval),
        "generator": sha256_file(GENERATOR),
        "schema": sha256_file(SCHEMA),
    }
    if current_hashes != control_hashes:
        raise AssetGateError("creative SFX controls changed during synthesis; no output was written")
    if sha256_file(spec_path) != spec_sha256:
        raise AssetGateError("creative SFX spec changed during synthesis; no output was written")
    write_wav_atomic(output, stereo)
    try:
        edl_template: dict[str, Any] = {
            "file": relative(edit_dir, output),
            "duration": round(float(audio["duration_s"]), 6),
            "gain_db": 0.0,
            "purpose": spec["purpose"],
            **({"section_id": spec["section_id"]} if spec.get("section_id") else {}),
        }
        if spec.get("rhythm_anchor_s") is not None:
            anchor = float(spec["rhythm_anchor_s"])
            if spec["anchor_alignment"] == "end_at_anchor":
                anchor -= float(audio["duration_s"])
            edl_template["start_in_output"] = round(max(0.0, anchor), 6)
        provenance = {
            "version": 1,
            "type": "sprut_procedural_sfx",
            "asset_origin": "deterministic_local_synthesis",
            "third_party_audio_assets": [],
            "network_used": False,
            "paid_api_used": False,
            "generator": {
                "path": str(GENERATOR),
                "sha256": control_hashes["generator"],
                "tool_id": "sprut.audio.procedural_sfx.v1",
            },
            "schema": {"path": str(SCHEMA), "sha256": control_hashes["schema"]},
            "spec": {
                "path": relative(edit_dir, spec_path),
                "sha256": spec_sha256,
                "value": spec,
            },
            "semantic_contract": {
                "plan": {"path": relative(edit_dir, plan), "sha256": control_hashes["plan"]},
                "approval": {
                    "path": relative(edit_dir, approval),
                    "sha256": control_hashes["approval"],
                },
            },
            "output": {
                "path": relative(edit_dir, output),
                "sha256": sha256_file(output),
                **audio,
            },
            "edl_audio_overlay_template": edl_template,
        }
        write_json_atomic(sidecar, provenance)
    except Exception:
        output.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise
    return provenance


def self_test() -> dict[str, Any]:
    results: dict[str, str] = {}
    metadata: dict[str, Any] | None = None
    for index, (preset_id, preset) in enumerate(sorted(PRESETS.items())):
        spec = {
            "version": 1,
            "preset": preset_id,
            "gain_db": float(preset["default_gain_db"]),
            "seed": 6800 + index,
            "purpose": "determinism self-test",
        }
        first, current_metadata = synthesize(spec)
        second, _ = synthesize(spec)
        if not np.array_equal(first, second):
            raise ValueError(f"deterministic synthesis self-test failed for {preset_id}")
        if not np.all(np.isfinite(first)) or float(np.max(np.abs(first))) <= 0:
            raise ValueError(f"signal validity self-test failed for {preset_id}")
        results[preset_id] = hashlib.sha256(first.tobytes()).hexdigest()
        metadata = current_metadata
    # If a future self-test needs files, this guarantees the only permitted root.
    with tempfile.TemporaryDirectory(prefix="sprut-sfx-selftest-", dir="/tmp") as temporary:
        if not Path(temporary).resolve().is_relative_to(Path("/tmp").resolve()):
            raise ValueError("self-test escaped /tmp")
    return {
        "status": "PASS",
        "presets_tested": len(results),
        "signal_sha256": results,
        "last_signal_metadata": metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate approval-gated deterministic creative SFX with no external audio assets"
    )
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list-presets", action="store_true")
    parser.add_argument("--describe-json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    discovery_modes = sum(bool(value) for value in (args.list_presets, args.describe_json, args.self_test))
    if discovery_modes > 1:
        parser.error("choose only one of --list-presets, --describe-json, or --self-test")
    if args.list_presets:
        print(json.dumps(discovery(presets_only=True), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.describe_json:
        print(json.dumps(discovery(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.edit_dir is None or args.spec is None or args.output is None:
        parser.error("production rendering requires --edit-dir, --spec, and --output")
    provenance = render(args.edit_dir, args.spec, args.output, args.force)
    print(
        json.dumps(
            {
                "generated": str((canonical_edit_dir(args.edit_dir) / provenance["output"]["path"])),
                "provenance": f"{provenance['output']['path']}.provenance.json",
                "preset": provenance["spec"]["value"]["preset"],
                "duration_s": provenance["output"]["duration_s"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, OSError, ValueError, SchemaDefinitionError) as exc:
        print(f"generate_creative_sfx: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
