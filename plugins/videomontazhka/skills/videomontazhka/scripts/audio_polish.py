#!/usr/bin/env python3
"""Analyze speech and create a local A/B polish preview before full processing."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_gate import AssetGateError, canonical_edit_dir, path_under_edit, require_asset_gate


class AudioError(RuntimeError):
    pass


AB_DECISION_NAME = "ab_decision.json"
AB_APPROVAL_NAME = "ab_approval.json"
AB_ARTIFACT_NAMES = {"A": "A_original.wav", "B": "B_processed.wav"}
AB_APPROVAL_BINDING_FIELDS = (
    "version",
    "status",
    "approved_at",
    "user_quote",
    "user_message_ref",
    "decision_file",
    "decision_sha256",
    "source",
    "source_sha256",
    "excerpt",
    "filter",
    "reason",
    "preview_artifacts",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise AudioError(f"{label} not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise AudioError(f"{label} root must be an object")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def approval_binding(value: dict[str, Any]) -> str:
    payload = {field: value.get(field) for field in AB_APPROVAL_BINDING_FIELDS}
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AudioError(f"A/B approval binding fields are invalid: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def canonical_reason(value: Any) -> str | None:
    if value is None:
        return None
    reason = str(value).strip()
    return reason or None


def validate_excerpt(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"start_s", "end_s", "duration_s"}:
        raise AudioError("A/B decision excerpt must contain exact start_s, end_s, and duration_s")
    numbers: dict[str, float] = {}
    for field in ("start_s", "end_s", "duration_s"):
        raw = value.get(field)
        if isinstance(raw, bool):
            raise AudioError(f"A/B decision excerpt.{field} must be numeric")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise AudioError(f"A/B decision excerpt.{field} must be numeric") from exc
        if not math.isfinite(number):
            raise AudioError(f"A/B decision excerpt.{field} must be finite")
        numbers[field] = number
    if numbers["start_s"] < 0 or numbers["duration_s"] <= 0:
        raise AudioError("A/B decision excerpt bounds are invalid")
    if abs(numbers["end_s"] - (numbers["start_s"] + numbers["duration_s"])) > 1e-9:
        raise AudioError("A/B decision excerpt end does not match start plus duration")
    return numbers


def recorded_edit_path(edit_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AudioError(f"{label} is missing")
    raw = Path(value).expanduser()
    path = raw.resolve() if raw.is_absolute() else (edit_dir / raw).resolve()
    return path_under_edit(edit_dir, path, label)


def validate_ab_decision(decision_path: Path, edit_dir: Path) -> dict[str, Any]:
    if decision_path.name != AB_DECISION_NAME:
        raise AudioError(f"A/B decision must use the canonical name {AB_DECISION_NAME}")
    decision = load_json_object(decision_path, "A/B decision")
    if decision.get("version") != 2:
        raise AudioError("A/B decision version must be 2")
    if decision.get("status") != "awaiting_user_ab_approval":
        raise AudioError("A/B decision status is not awaiting_user_ab_approval")

    raw_source = decision.get("source")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise AudioError("A/B decision source is missing")
    source = Path(raw_source).expanduser().resolve()
    if not source.is_file():
        raise AudioError(f"A/B decision source not found: {source}")
    source_digest = decision.get("source_sha256")
    if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise AudioError("A/B decision source_sha256 is invalid")
    if sha256(source) != source_digest:
        raise AudioError("A/B source changed after preview generation")

    excerpt = validate_excerpt(decision.get("excerpt"))
    filter_value = decision.get("filter")
    if not isinstance(filter_value, str) or not filter_value.strip():
        raise AudioError("A/B decision filter is missing")
    reason = canonical_reason(decision.get("reason"))
    if decision.get("reason") != reason:
        raise AudioError("A/B decision reason is not canonical")

    raw_artifacts = decision.get("preview_artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != set(AB_ARTIFACT_NAMES):
        raise AudioError("A/B decision preview_artifacts must contain exactly A and B")
    artifacts: dict[str, dict[str, str]] = {}
    for key, expected_name in AB_ARTIFACT_NAMES.items():
        record = raw_artifacts.get(key)
        if not isinstance(record, dict) or set(record) != {"file", "sha256"}:
            raise AudioError(f"A/B decision preview_artifacts.{key} is invalid")
        if record.get("file") != expected_name:
            raise AudioError(f"A/B decision preview_artifacts.{key} must be {expected_name}")
        digest = record.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AudioError(f"A/B decision preview_artifacts.{key}.sha256 is invalid")
        artifact_path = path_under_edit(
            edit_dir, decision_path.parent / expected_name, f"A/B artifact {key}"
        )
        if not artifact_path.is_file() or sha256(artifact_path) != digest:
            raise AudioError(f"A/B preview artifact {key} changed or is missing")
        artifacts[key] = {"file": expected_name, "sha256": digest}

    return {
        "source": str(source),
        "source_sha256": source_digest,
        "excerpt": excerpt,
        "filter": filter_value,
        "reason": reason,
        "preview_artifacts": artifacts,
    }


def validate_ab_approval(
    approval_path: Path,
    edit_dir: Path,
    source: Path,
    filter_value: str,
    reason: str | None,
) -> None:
    approval = load_json_object(approval_path, "A/B approval")
    if approval.get("version") != 1:
        raise AudioError("A/B approval version must be 1")
    if approval.get("status") != "approved":
        raise AudioError("A/B approval status is not approved")
    quote = approval.get("user_quote")
    if not isinstance(quote, str) or len(quote.strip()) < 2:
        raise AudioError("A/B approval has no exact user quote")
    binding = approval.get("binding_sha256")
    if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding):
        raise AudioError("A/B approval binding_sha256 is invalid")
    if approval_binding(approval) != binding:
        raise AudioError("A/B approval binding hash does not match its approved fields")

    decision_path = recorded_edit_path(
        edit_dir, approval.get("decision_file"), "A/B approval decision_file"
    )
    canonical_approval = (decision_path.parent / AB_APPROVAL_NAME).resolve()
    if approval_path != canonical_approval:
        raise AudioError(f"A/B approval must use the canonical path: {canonical_approval}")
    decision_digest = approval.get("decision_sha256")
    if not isinstance(decision_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", decision_digest):
        raise AudioError("A/B approval decision_sha256 is invalid")
    if not decision_path.is_file() or sha256(decision_path) != decision_digest:
        raise AudioError("A/B decision changed after user approval")
    state = validate_ab_decision(decision_path, edit_dir)

    for field in (
        "source",
        "source_sha256",
        "excerpt",
        "filter",
        "reason",
        "preview_artifacts",
    ):
        if approval.get(field) != state[field]:
            raise AudioError(f"A/B approval {field} differs from the approved decision")
    if source != Path(state["source"]):
        raise AudioError("A/B approval belongs to a different source")
    if filter_value != state["filter"]:
        raise AudioError("current audio filter arguments differ from the approved A/B preview")
    if reason != state["reason"]:
        raise AudioError("current audio reason differs from the approved A/B preview")


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=capture, check=False)
    if result.returncode:
        details = (result.stderr or result.stdout or "").strip()
        raise AudioError(f"command failed ({result.returncode}): {details[-2000:]}")
    return result


def last_float(pattern: str, text: str) -> float | None:
    matches = re.findall(pattern, text, flags=re.MULTILINE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def analyze(source: Path, output: Path, force: bool) -> None:
    if not source.is_file():
        raise AudioError(f"source not found: {source}")
    if output == source:
        raise AudioError("analysis output cannot overwrite the source")
    if output.exists() and not force:
        raise AudioError(f"output exists; use --force to replace: {output}")
    result = run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-i", str(source),
            "-map", "0:a:0", "-af",
            "ebur128=peak=true,astats=metadata=0:reset=0,silencedetect=noise=-42dB:d=0.6",
            "-vn", "-f", "null", "-",
        ],
        capture=True,
    )
    stderr = result.stderr
    silences = [float(value) for value in re.findall(r"silence_duration:\s*([-+0-9.]+)", stderr)]
    report: dict[str, Any] = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "integrated_lufs": last_float(r"^\s*I:\s*([-+0-9.]+)\s*LUFS", stderr),
        "loudness_range_lu": last_float(r"^\s*LRA:\s*([-+0-9.]+)\s*LU", stderr),
        "true_peak_dbtp": last_float(r"^\s*Peak:\s*([-+0-9.]+)\s*dBFS", stderr),
        "rms_level_db": last_float(r"RMS level dB:\s*([-+0-9.]+)", stderr),
        "noise_floor_db": last_float(r"Noise floor dB:\s*([-+0-9.]+)", stderr),
        "peak_level_db": last_float(r"Peak level dB:\s*([-+0-9.]+)", stderr),
        "silence_threshold_db": -42,
        "silences_over_600ms": len(silences),
        "silence_total_s": round(sum(silences), 3),
        "recommendations": [],
    }
    if report["integrated_lufs"] is not None and report["integrated_lufs"] < -20:
        report["recommendations"].append("Speech is quiet; normalize the assembled programme, not every clip.")
    if report["noise_floor_db"] is not None and report["noise_floor_db"] > -45:
        report["recommendations"].append("Noise floor may be audible; audition conservative denoise on an A/B excerpt.")
    if report["loudness_range_lu"] is not None and report["loudness_range_lu"] > 12:
        report["recommendations"].append("Speech dynamics are wide; audition gentle compression.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".part.json")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"analysis: {output}")


def build_filter(args: argparse.Namespace) -> str:
    filters: list[str] = []
    if args.highpass:
        filters.append(f"highpass=f={args.highpass}:poles=2")
    if args.denoise:
        filters.append(f"afftdn=nr={args.denoise}:nf=-45:tn=1:gs=4")
    if args.deesser:
        filters.append("deesser=i=0.18:m=0.45:f=0.55")
    if args.compress:
        filters.append("acompressor=threshold=0.125:ratio=2:attack=20:release=180:knee=2.8")
    filters.extend(["aresample=48000", "asetpts=N/SR/TB"])
    return ",".join(filters)


def require_reason(args: argparse.Namespace) -> None:
    restorative = args.denoise or args.deesser or args.compress
    if restorative and not (args.reason and len(args.reason.strip()) >= 8):
        raise AudioError("denoise/de-ess/compression requires --reason describing the heard problem")


def preview(args: argparse.Namespace, edit_dir: Path) -> None:
    out_dir = path_under_edit(edit_dir, args.output_dir, "preview output directory")
    require_asset_gate(edit_dir)
    if shutil.which("ffmpeg") is None:
        raise AudioError("ffmpeg is required")
    require_reason(args)
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise AudioError(f"source not found: {source}")
    if args.start < 0 or args.duration <= 0:
        raise AudioError("preview start must be non-negative and duration must be positive")
    filter_value = build_filter(args)
    reason = canonical_reason(args.reason)
    source_digest = sha256(source)
    original = out_dir / "A_original.wav"
    processed = out_dir / "B_processed.wav"
    decision_path = out_dir / AB_DECISION_NAME
    if any(path.exists() for path in (original, processed, decision_path)) and not args.force:
        raise AudioError("A/B files or decision exist; use --force to replace them")
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary_original = out_dir / ".A_original.part.wav"
    temporary_processed = out_dir / ".B_processed.part.wav"
    common = ["-ss", f"{args.start:.3f}", "-i", str(source), "-t", f"{args.duration:.3f}", "-map", "0:a:0"]
    run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *common, "-c:a", "pcm_s24le", "-ar", "48000", str(temporary_original)])
    run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *common, "-af", filter_value, "-c:a", "pcm_s24le", "-ar", "48000", str(temporary_processed)])
    if sha256(source) != source_digest:
        temporary_original.unlink(missing_ok=True)
        temporary_processed.unlink(missing_ok=True)
        raise AudioError("source changed while generating the A/B preview")
    temporary_original.replace(original)
    temporary_processed.replace(processed)
    decision = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "awaiting_user_ab_approval",
        "source": str(source),
        "source_sha256": source_digest,
        "excerpt": {
            "start_s": float(args.start),
            "end_s": float(args.start + args.duration),
            "duration_s": float(args.duration),
        },
        "filter": filter_value,
        "reason": reason,
        "preview_artifacts": {
            "A": {"file": original.name, "sha256": sha256(original)},
            "B": {"file": processed.name, "sha256": sha256(processed)},
        },
    }
    write_json_atomic(decision_path, decision)
    print(f"A/B ready: {original} | {processed} | decision: {decision_path}")


def approve_ab(args: argparse.Namespace, edit_dir: Path) -> None:
    decision_path = path_under_edit(edit_dir, args.decision, "A/B decision")
    output = path_under_edit(
        edit_dir, decision_path.parent / AB_APPROVAL_NAME, "A/B approval output"
    )
    require_asset_gate(edit_dir)
    quote = args.quote.strip()
    if len(quote) < 2:
        raise AudioError("A/B approval quote is empty")
    state = validate_ab_decision(decision_path, edit_dir)
    if output.exists() and not args.replace:
        raise AudioError(f"A/B approval exists; use --replace after new explicit approval: {output}")
    approval = {
        "version": 1,
        "status": "approved",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "user_quote": quote,
        "user_message_ref": args.message_ref,
        "decision_file": decision_path.relative_to(edit_dir).as_posix(),
        "decision_sha256": sha256(decision_path),
        **state,
    }
    approval["binding_sha256"] = approval_binding(approval)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, approval)
    print(f"A/B approval recorded: {output}")


def apply_full(args: argparse.Namespace, edit_dir: Path) -> None:
    output = path_under_edit(edit_dir, args.output, "polished output")
    approval_path = path_under_edit(edit_dir, args.approval, "A/B approval")
    require_asset_gate(edit_dir)
    if shutil.which("ffmpeg") is None:
        raise AudioError("ffmpeg is required")
    require_reason(args)
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise AudioError(f"source not found: {source}")
    if output == source:
        raise AudioError("polished output cannot overwrite the source")
    if output.suffix.lower() not in {".mov", ".mkv"}:
        raise AudioError("use .mov or .mkv for the PCM polished intermediate")
    filter_value = build_filter(args)
    reason = canonical_reason(args.reason)
    validate_ab_approval(approval_path, edit_dir, source, filter_value, reason)
    if output.exists() and not args.force:
        raise AudioError(f"output exists; use --force to replace: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.part{output.suffix}")
    run([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v?", "-map", "0:a:0", "-c:v", "copy",
        "-af", filter_value, "-c:a", "pcm_s24le", "-ar", "48000", str(temporary),
    ])
    temporary.replace(output)
    print(f"polished PCM intermediate: {output}")


def add_processing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", type=Path)
    parser.add_argument("--highpass", type=int, default=70, help="rumble cut in Hz; 0 disables")
    parser.add_argument("--denoise", type=float, default=0, metavar="DB", help="conservative afftdn reduction; 0 disables")
    parser.add_argument("--deesser", action="store_true")
    parser.add_argument("--compress", action="store_true")
    parser.add_argument("--reason", help="heard problem that justifies restoration")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze-first local speech polish with approval-gated preview/apply",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python audio_polish.py analyze --edit-dir /project/edit source.mov -o /project/edit/audio/analysis.json
  python audio_polish.py preview --edit-dir /project/edit source.mov --output-dir /project/edit/audio/ab
  python audio_polish.py approve --edit-dir /project/edit --decision /project/edit/audio/ab/ab_decision.json --quote 'I approve B'
  python audio_polish.py apply --edit-dir /project/edit source.mov -o /project/edit/audio/polished.mov --approval /project/edit/audio/ab/ab_approval.json

Analysis is allowed before semantic approval. Preview, approve, and apply require the asset gate.""",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("--edit-dir", type=Path, required=True)
    p_analyze.add_argument("source", type=Path)
    p_analyze.add_argument("-o", "--output", type=Path, required=True)
    p_analyze.add_argument("--force", action="store_true")
    p_preview = sub.add_parser("preview")
    p_preview.add_argument("--edit-dir", type=Path, required=True)
    add_processing_options(p_preview)
    p_preview.add_argument("--output-dir", type=Path, required=True)
    p_preview.add_argument("--start", type=float, default=0)
    p_preview.add_argument("--duration", type=float, default=20)
    p_preview.add_argument("--force", action="store_true")
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--edit-dir", type=Path, required=True)
    p_approve.add_argument("--decision", type=Path, required=True)
    p_approve.add_argument("--quote", required=True, help="exact user approval text")
    p_approve.add_argument("--message-ref", help="optional task/message reference")
    p_approve.add_argument("--replace", action="store_true")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--edit-dir", type=Path, required=True)
    add_processing_options(p_apply)
    p_apply.add_argument("-o", "--output", type=Path, required=True)
    p_apply.add_argument("--approval", type=Path, required=True)
    p_apply.add_argument("--force", action="store_true")
    args = parser.parse_args()
    edit_dir = canonical_edit_dir(args.edit_dir)
    if args.command == "analyze":
        output = path_under_edit(edit_dir, args.output, "analysis output")
        if shutil.which("ffmpeg") is None:
            raise AudioError("ffmpeg is required")
        analyze(args.source.expanduser().resolve(), output, args.force)
    elif args.command == "preview":
        preview(args, edit_dir)
    elif args.command == "approve":
        approve_ab(args, edit_dir)
    else:
        apply_full(args, edit_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssetGateError, AudioError, OSError) as exc:
        print(f"audio_polish: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
