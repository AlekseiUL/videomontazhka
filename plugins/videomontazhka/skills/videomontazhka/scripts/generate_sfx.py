#!/usr/bin/env python3
"""Generate restrained deterministic WAV accents locally with NumPy."""

from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate


RATE = 48_000


def fade(signal: np.ndarray, fade_in_s: float, fade_out_s: float) -> np.ndarray:
    result = signal.copy()
    fi = min(len(result), round(fade_in_s * RATE))
    fo = min(len(result), round(fade_out_s * RATE))
    if fi:
        result[:fi] *= np.linspace(0, 1, fi, dtype=np.float64)
    if fo:
        result[-fo:] *= np.linspace(1, 0, fo, dtype=np.float64)
    return result


def synth(kind: str, duration: float, seed: int) -> np.ndarray:
    count = max(1, round(duration * RATE))
    t = np.arange(count, dtype=np.float64) / RATE
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, count)
    if kind == "hit":
        body = np.sin(2 * math.pi * (95 + 40 * np.exp(-t * 18)) * t)
        click = noise * np.exp(-t * 45)
        signal = (0.68 * body + 0.22 * click) * np.exp(-t * 10)
        return fade(signal, 0.002, min(0.08, duration / 2))
    if kind == "whoosh":
        envelope = np.sin(np.clip(t / duration, 0, 1) * math.pi) ** 1.8
        smooth = np.convolve(noise, np.ones(96) / 96, mode="same")
        tone = np.sin(2 * math.pi * (180 + 700 * (t / duration) ** 2) * t)
        return fade((0.75 * smooth + 0.18 * tone) * envelope, 0.02, 0.08)
    if kind == "riser":
        phase = 2 * math.pi * (120 * t + (980 - 120) * t**2 / (2 * duration))
        envelope = np.clip(t / duration, 0, 1) ** 1.6
        signal = (0.50 * np.sin(phase) + 0.13 * noise) * envelope
        return fade(signal, 0.04, min(0.10, duration / 3))
    raise ValueError(f"unknown kind: {kind}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a local brand SFX WAV after semantic approval",
        epilog=(
            "example: python generate_sfx.py --edit-dir /project/edit hit "
            "-o /project/edit/audio/brand-hit.wav --gain-db -16"
        ),
    )
    parser.add_argument("--edit-dir", type=Path, required=True)
    parser.add_argument("kind", choices=("hit", "whoosh", "riser"))
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--gain-db", type=float, default=-14.0)
    parser.add_argument("--seed", type=int, default=6800)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    edit_dir = canonical_edit_dir(args.edit_dir)
    output = path_under_edit(edit_dir, args.output, "output")
    require_asset_gate(edit_dir)

    defaults = {"hit": 0.32, "whoosh": 0.72, "riser": 1.25}
    duration = args.duration or defaults[args.kind]
    if not 0.08 <= duration <= 5:
        raise ValueError("duration must be between 0.08 and 5 seconds")
    if not math.isfinite(args.gain_db) or not -60 <= args.gain_db <= 0:
        raise ValueError("gain-db must be between -60 and 0 dBFS")
    signal = synth(args.kind, duration, args.seed)
    peak = float(np.max(np.abs(signal))) or 1.0
    target = 10 ** (args.gain_db / 20)
    pcm = np.clip(signal / peak * target, -1, 1)
    stereo = np.column_stack([pcm, pcm])
    samples = np.round(stereo * 32767).astype("<i2")
    if output.suffix.lower() != ".wav":
        raise ValueError("output must use .wav")
    if output.exists() and not args.force:
        raise ValueError(f"output exists; use --force to replace: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.part.wav")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    temporary.replace(output)
    print(f"generated: {output} ({duration:.3f}s, peak {args.gain_db:g} dBFS)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, OSError, ValueError) as exc:
        print(f"generate_sfx: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
