#!/usr/bin/env python3
"""Create an approval-gated local beat/onset map with librosa.

This map supplies timing evidence to the creative router; it never decides
where an edit, transition, graphic, or SFX belongs. Semantic timing stays
authoritative. Production inputs must either be an immutable manifest source
or a file already stored under the canonical ``edit/`` tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate
from schema_check import SchemaDefinitionError, Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "assets" / "rhythm-map.schema.v1.json"
GENERATOR = Path(__file__).resolve()
HOP_LENGTH = 512


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def resolved_project_path(edit_dir: Path, raw: object) -> Path:
    value = Path(str(raw)).expanduser()
    return value.resolve() if value.is_absolute() else (edit_dir / value).resolve()


def input_binding(edit_dir: Path, input_path: Path) -> dict[str, Any]:
    resolved = input_path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"rhythm input not found: {resolved}")
    try:
        resolved.relative_to(edit_dir)
    except ValueError:
        pass
    else:
        return {
            "path": str(resolved),
            "sha256": sha256_file(resolved),
            "scope": "edit_asset",
            "source_id": None,
        }

    project = read_json(edit_dir / "project.json", "project")
    manifest_path = resolved_project_path(
        edit_dir, project.get("source_manifest") or "source_manifest.json"
    )
    manifest = read_json(manifest_path, "source manifest")
    raw_root = Path(str(manifest.get("root") or "..")).expanduser()
    source_root = raw_root.resolve() if raw_root.is_absolute() else (manifest_path.parent / raw_root).resolve()
    for item in manifest.get("sources") or []:
        if not isinstance(item, dict):
            continue
        raw_path = Path(str(item.get("path") or "")).expanduser()
        candidate = raw_path.resolve() if raw_path.is_absolute() else (source_root / raw_path).resolve()
        if candidate != resolved:
            continue
        expected = str(item.get("sha256") or "")
        if len(expected) != 64:
            raise ValueError(f"manifest source has no valid SHA-256: {item.get('id')}")
        return {
            "path": str(resolved),
            "sha256": expected,
            "scope": "source_manifest",
            "source_id": str(item.get("id") or ""),
        }
    raise AssetGateError(
        "rhythm input must be an immutable source_manifest item or a local asset under edit/: "
        f"{resolved}"
    )


def ffmpeg_identity(ffmpeg: str) -> dict[str, str]:
    result = subprocess.run(
        [ffmpeg, "-version"], text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError(f"cannot identify FFmpeg: {result.stderr.strip()[-1000:]}")
    first = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if not first:
        raise ValueError("FFmpeg returned no version")
    return {"path": str(Path(ffmpeg).resolve()), "version": first}


def decode_mono_f32(
    edit_dir: Path,
    input_path: Path,
    sample_rate: int,
    start_s: float,
    duration_s: float | None,
) -> tuple[Path, int, dict[str, str]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("FFmpeg is required for local rhythm analysis")
    work = path_under_edit(edit_dir, edit_dir / "work" / "rhythm", "rhythm work directory")
    work.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".decoded-", suffix=".f32", dir=work)
    os.close(descriptor)
    temporary = Path(temporary_name)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
    ]
    if start_s > 0:
        command.extend(["-ss", f"{start_s:.9f}"])
    if duration_s is not None:
        command.extend(["-t", f"{duration_s:.9f}"])
    command.extend(
        ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
    )
    try:
        with temporary.open("wb") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            details = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"FFmpeg audio decode failed: {details[-1600:]}")
        size = temporary.stat().st_size
        if size % 4:
            raise ValueError("FFmpeg produced a malformed float32 audio stream")
        samples = size // 4
        if samples < HOP_LENGTH * 2:
            raise ValueError("rhythm input window is too short or has no decoded audio")
        return temporary, samples, ffmpeg_identity(ffmpeg)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def normalized_strengths(envelope: np.ndarray) -> tuple[np.ndarray, float]:
    finite = np.asarray(envelope, dtype=np.float64)
    if not len(finite):
        return finite, 0.0
    reference = float(np.percentile(finite, 95.0))
    if not math.isfinite(reference) or reference <= 1e-12:
        return np.zeros_like(finite), 0.0
    return np.clip(finite / reference, 0.0, 1.0), reference


def event_records(
    frames: np.ndarray,
    normalized_envelope: np.ndarray,
    sample_rate: int,
    start_s: float,
    decoded_duration_s: float,
) -> list[dict[str, Any]]:
    import librosa

    records: list[dict[str, Any]] = []
    for frame in np.asarray(frames, dtype=np.int64).tolist():
        relative_time = float(librosa.frames_to_time(frame, sr=sample_rate, hop_length=HOP_LENGTH))
        if relative_time > decoded_duration_s + 0.05:
            continue
        strength = float(normalized_envelope[min(max(frame, 0), len(normalized_envelope) - 1)])
        records.append(
            {
                "index": len(records),
                "time_s": round(start_s + max(0.0, relative_time), 6),
                "relative_time_s": round(max(0.0, relative_time), 6),
                "strength": round(min(1.0, max(0.0, strength)), 6),
            }
        )
    return records


def choose_accent_candidates(
    onsets: list[dict[str, Any]], beats: list[dict[str, Any]], max_accents: int
) -> list[dict[str, Any]]:
    strong = [item for item in onsets if float(item["strength"]) >= 0.62]
    clustered: list[dict[str, Any]] = []
    for item in strong:
        if clustered and float(item["relative_time_s"]) - float(clustered[-1]["relative_time_s"]) < 0.18:
            if float(item["strength"]) > float(clustered[-1]["strength"]):
                clustered[-1] = item
        else:
            clustered.append(item)
    if len(clustered) > max_accents:
        selected = sorted(clustered, key=lambda item: (-float(item["strength"]), float(item["time_s"])))[:max_accents]
        clustered = sorted(selected, key=lambda item: float(item["time_s"]))

    beat_times = np.asarray([float(item["time_s"]) for item in beats], dtype=np.float64)
    result: list[dict[str, Any]] = []
    for item in clustered:
        nearest: float | None = None
        distance: float | None = None
        if len(beat_times):
            insertion = int(np.searchsorted(beat_times, float(item["time_s"])))
            indexes = [index for index in (insertion - 1, insertion) if 0 <= index < len(beat_times)]
            nearest_index = min(indexes, key=lambda index: abs(float(beat_times[index]) - float(item["time_s"])))
            nearest = float(beat_times[nearest_index])
            distance = abs(nearest - float(item["time_s"]))
        result.append(
            {
                "index": len(result),
                "time_s": item["time_s"],
                "relative_time_s": item["relative_time_s"],
                "strength": item["strength"],
                "aligned_to_beat": bool(distance is not None and distance <= 0.08),
                "nearest_beat_time_s": None if nearest is None else round(nearest, 6),
                "beat_distance_s": None if distance is None else round(distance, 6),
                "status": "candidate_only_requires_semantic_router",
            }
        )
    return result


def analyze_signal(
    signal: np.ndarray,
    sample_rate: int,
    start_s: float,
    max_accents: int,
) -> dict[str, Any]:
    import librosa

    if not np.all(np.isfinite(signal)):
        raise ValueError("decoded rhythm signal contains non-finite samples")
    decoded_duration = len(signal) / sample_rate
    onset_envelope = librosa.onset.onset_strength(
        y=signal, sr=sample_rate, hop_length=HOP_LENGTH
    )
    normalized, reference = normalized_strengths(onset_envelope)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        hop_length=HOP_LENGTH,
        units="frames",
        backtrack=False,
    )
    tempo_raw, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        hop_length=HOP_LENGTH,
        units="frames",
        sparse=True,
    )
    tempo_values = np.asarray(tempo_raw, dtype=np.float64).reshape(-1)
    tempo = float(tempo_values[0]) if len(tempo_values) and math.isfinite(float(tempo_values[0])) else None
    beats = event_records(beat_frames, normalized, sample_rate, start_s, decoded_duration)
    onsets = event_records(onset_frames, normalized, sample_rate, start_s, decoded_duration)
    accents = choose_accent_candidates(onsets, beats, max_accents)
    beat_times = np.asarray([float(item["relative_time_s"]) for item in beats], dtype=np.float64)
    intervals = np.diff(beat_times)
    median = float(np.median(intervals)) if len(intervals) else None
    mad = float(np.median(np.abs(intervals - median))) if median is not None else None
    return {
        "decoded_duration_s": decoded_duration,
        "tempo_bpm": tempo,
        "statistics": {
            "beat_count": len(beats),
            "onset_count": len(onsets),
            "suggested_accent_count": len(accents),
            "beat_interval_median_s": median,
            "beat_interval_mad_s": mad,
            "onset_strength_reference": reference,
        },
        "beats": beats,
        "onsets": onsets,
        "suggested_accents": accents,
    }


def rounded_optional(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def validate_map(value: dict[str, Any]) -> None:
    schema = read_json(SCHEMA, "rhythm-map schema")
    errors = Validator(schema).validate(value)
    if errors:
        details = "; ".join(error.render() for error in errors[:10])
        raise ValueError(f"generated rhythm map does not match schema: {details}")


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".part.json", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def describe() -> dict[str, Any]:
    return {
        "version": 1,
        "tool_id": "sprut.audio.rhythm_map.v1",
        "command": "scripts/analyze_rhythm.py",
        "availability": {
            "network_required": False,
            "paid_api_required": False,
            "runtime": "local_librosa_ffmpeg",
        },
        "production_contract": {
            "semantic_approval_required": True,
            "input_scope": ["source_manifest", "canonical_edit_tree"],
            "output_scope": "canonical_edit_tree",
            "deterministic_for_same_input_window_and_runtime": True,
            "schema": str(SCHEMA),
        },
        "capabilities": [
            "tempo estimate",
            "beat timing map",
            "onset timing and normalized strength map",
            "strong-onset candidates with nearest-beat distance",
        ],
        "routing": {
            "use_when": [
                "approved motion or B-roll should land near real musical attacks",
                "a chapter bridge has an approved music bed",
                "SFX timing needs a candidate beat or onset after the semantic moment is chosen",
            ],
            "do_not_use_when": [
                "speech semantics have not selected the moment",
                "the track is ambient or arrhythmic and the map is unstable",
                "a detected beat would move a truthful speech boundary",
            ],
            "precedence": "meaning and intelligibility first; rhythm only refines timing",
        },
        "explicit_limitations": [
            "does not infer downbeats, bars, musical sections, emotion, or edit meaning",
            "suggested accents are candidates, never automatic edit instructions",
        ],
        "sample_rates_hz": [11025, 22050, 44100],
        "hop_length_samples": HOP_LENGTH,
    }


def produce_map(
    edit_dir_value: Path,
    input_value: Path,
    output_value: Path,
    sample_rate: int,
    start_s: float,
    duration_s: float | None,
    max_accents: int,
    force: bool,
) -> dict[str, Any]:
    if not math.isfinite(start_s) or start_s < 0:
        raise ValueError("start must be a finite non-negative number")
    if duration_s is not None and (not math.isfinite(duration_s) or duration_s <= 0 or duration_s > 14_400):
        raise ValueError("duration must be between 0 and 14400 seconds")
    if not 1 <= max_accents <= 5000:
        raise ValueError("max-accents must be between 1 and 5000")
    edit_dir = canonical_edit_dir(edit_dir_value)
    output = path_under_edit(edit_dir, output_value, "rhythm-map output")
    if output.suffix.lower() != ".json":
        raise ValueError("rhythm-map output must use .json")
    require_asset_gate(edit_dir)
    plan = edit_dir / "semantic_plan.json"
    approval = edit_dir / "approval.json"
    control_hashes = {
        "plan": sha256_file(plan),
        "approval": sha256_file(approval),
        "generator": sha256_file(GENERATOR),
        "schema": sha256_file(SCHEMA),
    }
    input_path = input_value.expanduser().resolve()
    binding = input_binding(edit_dir, input_path)
    if output == input_path:
        raise ValueError("rhythm-map output cannot replace its input")
    if output.exists() and not force:
        raise ValueError(f"output exists; use --force to replace: {output}")

    stat_before = input_path.stat()
    decoded_path, sample_count, ffmpeg = decode_mono_f32(
        edit_dir, input_path, sample_rate, start_s, duration_s
    )
    try:
        signal = np.memmap(decoded_path, dtype="<f4", mode="r", shape=(sample_count,))
        analysis = analyze_signal(signal, sample_rate, start_s, max_accents)
        del signal
    finally:
        decoded_path.unlink(missing_ok=True)
    stat_after = input_path.stat()
    if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns):
        raise AssetGateError("rhythm input changed during analysis; no map was written")
    if binding["scope"] == "edit_asset" and sha256_file(input_path) != binding["sha256"]:
        raise AssetGateError("edit audio asset changed during analysis; no map was written")
    require_asset_gate(edit_dir)
    current_hashes = {
        "plan": sha256_file(plan),
        "approval": sha256_file(approval),
        "generator": sha256_file(GENERATOR),
        "schema": sha256_file(SCHEMA),
    }
    if current_hashes != control_hashes:
        raise AssetGateError("rhythm-map controls changed during analysis; no map was written")

    import librosa

    decoded_duration = float(analysis.pop("decoded_duration_s"))
    statistics = analysis["statistics"]
    statistics["beat_interval_median_s"] = rounded_optional(statistics["beat_interval_median_s"])
    statistics["beat_interval_mad_s"] = rounded_optional(statistics["beat_interval_mad_s"])
    statistics["onset_strength_reference"] = round(float(statistics["onset_strength_reference"]), 9)
    end_s = start_s + decoded_duration
    value = {
        "version": 1,
        "type": "sprut_rhythm_map",
        "tool_id": "sprut.audio.rhythm_map.v1",
        "source": binding,
        "semantic_contract": {
            "plan": {
                "path": plan.relative_to(edit_dir).as_posix(),
                "sha256": control_hashes["plan"],
            },
            "approval": {
                "path": approval.relative_to(edit_dir).as_posix(),
                "sha256": control_hashes["approval"],
            },
        },
        "analysis_window": {
            "start_s": round(start_s, 6),
            "end_s": round(end_s, 6),
            "duration_s": round(decoded_duration, 6),
            "sample_rate_hz": sample_rate,
            "hop_length_samples": HOP_LENGTH,
            "decoded_channels": 1,
        },
        "tempo_bpm": rounded_optional(analysis["tempo_bpm"]),
        "statistics": statistics,
        "beats": analysis["beats"],
        "onsets": analysis["onsets"],
        "suggested_accents": analysis["suggested_accents"],
        "limitations": [
            "Beat and onset detection supplies timing candidates only; approved meaning remains authoritative.",
            "No downbeat, bar, phrase, emotion, or musical-section label is inferred.",
            "Do not move speech cuts or obscure dialogue merely to follow this map.",
        ],
        "runtime": {
            "generator": {"path": str(GENERATOR), "sha256": control_hashes["generator"]},
            "schema": {"path": str(SCHEMA), "sha256": control_hashes["schema"]},
            "librosa": str(librosa.__version__),
            "numpy": str(np.__version__),
            "ffmpeg": ffmpeg,
        },
    }
    validate_map(value)
    write_json_atomic(output, value)
    return value


def self_test() -> dict[str, Any]:
    import librosa

    sample_rate = 22050
    duration = 4.0
    signal = np.zeros(round(sample_rate * duration), dtype=np.float32)
    for time_s in np.arange(0.25, duration, 0.5):
        start = round(float(time_s) * sample_rate)
        length = min(round(0.02 * sample_rate), len(signal) - start)
        signal[start : start + length] += np.hanning(length).astype(np.float32)
    analysis = analyze_signal(signal, sample_rate, 0.0, 64)
    if len(analysis["onsets"]) < 5:
        raise ValueError("rhythm self-test detected too few pulse onsets")
    with tempfile.TemporaryDirectory(prefix="sprut-rhythm-selftest-", dir="/tmp") as temporary:
        if not Path(temporary).resolve().is_relative_to(Path("/tmp").resolve()):
            raise ValueError("self-test escaped /tmp")
    return {
        "status": "PASS",
        "librosa": str(librosa.__version__),
        "detected_beats": len(analysis["beats"]),
        "detected_onsets": len(analysis["onsets"]),
        "tempo_bpm": rounded_optional(analysis["tempo_bpm"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a local approval-gated beat/onset map; semantics remain authoritative"
    )
    parser.add_argument("--edit-dir", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--sample-rate", type=int, choices=(11025, 22050, 44100), default=22050)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--max-accents", type=int, default=512)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--describe-json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.describe_json and args.self_test:
        parser.error("choose only one of --describe-json or --self-test")
    if args.describe_json:
        print(json.dumps(describe(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.edit_dir is None or args.input is None or args.output is None:
        parser.error("production analysis requires --edit-dir, --input, and --output")
    value = produce_map(
        args.edit_dir,
        args.input,
        args.output,
        args.sample_rate,
        args.start,
        args.duration,
        args.max_accents,
        args.force,
    )
    print(
        json.dumps(
            {
                "generated": str(args.output.expanduser().resolve()),
                "tempo_bpm": value["tempo_bpm"],
                "beats": len(value["beats"]),
                "onsets": len(value["onsets"]),
                "suggested_accents": len(value["suggested_accents"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, OSError, ValueError, SchemaDefinitionError, ImportError) as exc:
        print(f"analyze_rhythm: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
