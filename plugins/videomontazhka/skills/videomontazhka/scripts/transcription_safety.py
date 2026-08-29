#!/usr/bin/env python3
"""Safety primitives shared by the transcription entrypoints."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from runtime_paths import application_home


MAX_SOURCE_ID_LENGTH = 160
ATTEMPT_LEDGER_NAME = "transcription_attempts.jsonl"
ATTEMPT_STATUSES = {
    "attempt_started",
    "succeeded",
    "ambiguous_consumed",
    "failed_consumed",
}
APPROVAL_REGISTRY_DIR = "transcription-approvals"
CONSUMED_CAPABILITIES_DIR = "consumed"
REGISTRY_LOCK_NAME = ".registry.lock"


class TranscriptionPathError(ValueError):
    """Raised before an unsafe transcription cache path can be used."""


class TranscriptionLockError(RuntimeError):
    """Raised when another process already owns the project transcription lock."""


class AttemptLedgerError(RuntimeError):
    """Raised when an approval/source attempt is consumed or its ledger is invalid."""


def _current_uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def _validate_open_state_file(
    path: Path,
    descriptor: int,
    *,
    before: os.stat_result | None,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    try:
        current = os.lstat(path)
    except FileNotFoundError as exc:
        raise TranscriptionPathError(f"state file disappeared after open: {path}") from exc
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise TranscriptionPathError(f"state path must be a regular file: {path}")
    if opened.st_nlink != 1 or current.st_nlink != 1:
        raise TranscriptionPathError(f"state file hardlinks are forbidden: {path}")
    uid = _current_uid()
    if uid is not None and (opened.st_uid != uid or current.st_uid != uid):
        raise TranscriptionPathError(f"state file must be owned by the current user: {path}")
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise TranscriptionPathError(f"state file changed inode during open: {path}")
    if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
        raise TranscriptionPathError(f"state file changed before secure open: {path}")
    if stat.S_IMODE(opened.st_mode) & 0o077:
        raise TranscriptionPathError(f"state file permissions must be private (0600): {path}")
    return opened


def secure_open_state(
    path: Path,
    flags: int,
    *,
    create: bool = False,
    exclusive: bool = False,
) -> int:
    """Open a private state file and bind lstat/fstat to one owned inode."""
    path = path.expanduser()
    if path.is_symlink():
        raise TranscriptionPathError(f"state symlinks are forbidden: {path}")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    open_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if create:
        open_flags |= os.O_CREAT
    if exclusive:
        open_flags |= os.O_EXCL
    descriptor = os.open(path, open_flags, 0o600)
    try:
        _validate_open_state_file(path, descriptor, before=before)
        if before is None:
            os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise TranscriptionPathError(f"private state directory must not be a symlink: {path}")
    info = path.stat()
    uid = _current_uid()
    if not stat.S_ISDIR(info.st_mode) or (uid is not None and info.st_uid != uid):
        raise TranscriptionPathError(f"invalid private state directory: {path}")
    os.chmod(path, 0o700)
    return path


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def approval_registry_paths(approval_id: str) -> tuple[Path, Path]:
    if not isinstance(approval_id, str) or len(approval_id) != 32:
        raise AttemptLedgerError("approval_id is invalid for external registry")
    explicit_home = os.environ.get("VIDEOMONTAZHKA_HOME")
    if explicit_home and Path(explicit_home).expanduser().is_symlink():
        raise TranscriptionPathError("VIDEOMONTAZHKA_HOME must not be a symlink")
    root = _private_directory(application_home())
    registry = _private_directory(root / APPROVAL_REGISTRY_DIR)
    consumed = _private_directory(registry / CONSUMED_CAPABILITIES_DIR)
    return registry / f"{approval_id}.json", consumed


@contextlib.contextmanager
def approval_registry_lock():
    anchor_path, _ = approval_registry_paths("0" * 32)
    registry = anchor_path.parent
    lock_path = registry / REGISTRY_LOCK_NAME
    descriptor = secure_open_state(lock_path, os.O_RDWR, create=True)
    try:
        import fcntl
    except ModuleNotFoundError as exc:  # pragma: no cover
        os.close(descriptor)
        raise AttemptLedgerError("safe approval-registry locking is unavailable") from exc
    with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=True) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    descriptor = secure_open_state(
        path,
        os.O_WRONLY,
        create=True,
        exclusive=True,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def _read_secure_json(path: Path, label: str) -> dict[str, Any]:
    try:
        descriptor = secure_open_state(path, os.O_RDONLY)
    except FileNotFoundError as exc:
        raise AttemptLedgerError(f"missing external {label}: {path}") from exc
    with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
        try:
            value = json.load(handle)
        except json.JSONDecodeError as exc:
            raise AttemptLedgerError(f"external {label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise AttemptLedgerError(f"external {label} root must be an object")
    return value


def validate_source_id(value: Any, *, label: str = "source id") -> str:
    """Return one portable filename component or fail closed.

    Source IDs become transcript, metadata, and temporary-audio filenames.  Test
    both POSIX and Windows path semantics so a manifest cannot become unsafe when
    the project moves between platforms.
    """
    if not isinstance(value, str):
        raise TranscriptionPathError(f"unsafe {label}: expected a string")
    source_id = value
    if not source_id or source_id != source_id.strip():
        raise TranscriptionPathError(
            f"unsafe {label} {source_id!r}: it must be non-empty with no edge whitespace"
        )
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise TranscriptionPathError(
            f"unsafe {label} {source_id!r}: maximum length is {MAX_SOURCE_ID_LENGTH}"
        )
    if source_id in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in source_id):
        raise TranscriptionPathError(
            f"unsafe {label} {source_id!r}: dot names and control characters are forbidden"
        )

    posix = PurePosixPath(source_id)
    windows = PureWindowsPath(source_id)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or len(posix.parts) != 1
        or len(windows.parts) != 1
        or posix.name != source_id
        or windows.name != source_id
    ):
        raise TranscriptionPathError(
            f"unsafe {label} {source_id!r}: it must be one path-free filename component"
        )
    return source_id


def contained_child(directory: Path, name: str, *, label: str) -> Path:
    """Resolve a direct child without following an output symlink out of bounds."""
    base = directory.expanduser().resolve()
    candidate = base / name
    if candidate.is_symlink():
        raise TranscriptionPathError(f"unsafe {label}: symlink outputs are forbidden: {candidate}")
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(base)
    except ValueError as exc:
        raise TranscriptionPathError(
            f"unsafe {label}: path escapes its required directory: {candidate}"
        ) from exc
    if len(relative.parts) != 1:
        raise TranscriptionPathError(
            f"unsafe {label}: path must be a direct child of {base}: {candidate}"
        )
    return candidate


def sha256_file(path: Path) -> str:
    """Hash a file without loading a potentially large media source into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic JSON bytes suitable for approval bindings."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    """Atomically replace one private JSON artifact using an exclusive temp file."""
    selected = path.expanduser()
    if selected.is_symlink():
        raise TranscriptionPathError(f"unsafe JSON output: symlinks are forbidden: {selected}")
    path = selected.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".part",
        dir=path.parent,
        text=True,
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            descriptor = -1
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def attempt_source_identity(
    *,
    source_id: str,
    resolved_path: str,
    sha256: str,
    duration_s: float,
    outside_project: bool,
) -> dict[str, Any]:
    return {
        "id": validate_source_id(source_id),
        "resolved_path": str(Path(resolved_path).expanduser().resolve()),
        "outside_project": bool(outside_project),
        "sha256": sha256,
        "duration_s": round(float(duration_s), 6),
    }


def attempt_key(approval_id: str, source_identity: dict[str, Any]) -> str:
    return canonical_json_sha256(
        {"approval_id": approval_id, "source_identity": source_identity}
    )


def create_approval_anchor(
    *,
    approval_id: str,
    approval_nonce: str,
    preflight_id: str,
    preflight_sha256: str,
    request_sha256: str,
    edit_dir: Path,
) -> Path:
    anchor_path, _ = approval_registry_paths(approval_id)
    anchor = {
        "version": 1,
        "type": "transcription_approval_anchor",
        "approval_id": approval_id,
        "approval_nonce_sha256": canonical_json_sha256(approval_nonce),
        "preflight_id": preflight_id,
        "preflight_sha256": preflight_sha256,
        "request_sha256": request_sha256,
        "edit_dir": str(edit_dir.expanduser().resolve()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "consumed_attempts": {},
    }
    anchor["immutable_binding_sha256"] = canonical_json_sha256(
        {key: value for key, value in anchor.items() if key != "consumed_attempts"}
    )
    anchor["binding_sha256"] = canonical_json_sha256(anchor)
    try:
        with approval_registry_lock():
            _write_json_exclusive(anchor_path, anchor)
    except FileExistsError as exc:
        raise AttemptLedgerError("external approval_id already exists; generate a new approval") from exc
    return anchor_path


def _validate_anchor_contract(anchor: dict[str, Any]) -> None:
    claimed = anchor.get("binding_sha256")
    unsigned = dict(anchor)
    unsigned.pop("binding_sha256", None)
    if anchor.get("version") != 1 or anchor.get("type") != "transcription_approval_anchor":
        raise AttemptLedgerError("external approval anchor contract is invalid")
    if claimed != canonical_json_sha256(unsigned):
        raise AttemptLedgerError("external approval anchor binding is invalid")
    immutable = dict(unsigned)
    immutable.pop("immutable_binding_sha256", None)
    immutable.pop("consumed_attempts", None)
    if anchor.get("immutable_binding_sha256") != canonical_json_sha256(immutable):
        raise AttemptLedgerError("external approval anchor immutable binding is invalid")
    consumed = anchor.get("consumed_attempts")
    if not isinstance(consumed, dict):
        raise AttemptLedgerError("external approval anchor consumed state is invalid")
    for key, record in consumed.items():
        if not isinstance(record, dict) or key != record.get("attempt_key"):
            raise AttemptLedgerError("external approval anchor consumed record is invalid")
        _validate_consumed_marker(record, expected_approval_id=anchor.get("approval_id"))


def validate_approval_anchor(approval: dict[str, Any], edit_dir: Path) -> dict[str, Any]:
    approval_id = approval.get("approval_id")
    approval_nonce = approval.get("approval_nonce")
    if not isinstance(approval_id, str) or not isinstance(approval_nonce, str):
        raise AttemptLedgerError("approval has no external anchor identity")
    anchor_path, _ = approval_registry_paths(approval_id)
    anchor = _read_secure_json(anchor_path, "approval anchor")
    _validate_anchor_contract(anchor)
    expected = {
        "approval_id": approval_id,
        "approval_nonce_sha256": canonical_json_sha256(approval_nonce),
        "preflight_id": approval.get("preflight_id"),
        "preflight_sha256": approval.get("preflight_sha256"),
        "request_sha256": approval.get("request_sha256"),
        "edit_dir": str(edit_dir.expanduser().resolve()),
    }
    if any(anchor.get(field) != value for field, value in expected.items()):
        raise AttemptLedgerError(
            "external approval anchor is missing or belongs to a different project/approval"
        )
    return anchor


def external_attempt_marker_path(approval_id: str, source_identity: dict[str, Any]) -> Path:
    _, consumed = approval_registry_paths(approval_id)
    return consumed / f"{attempt_key(approval_id, source_identity)}.json"


def _validate_consumed_marker(
    marker: dict[str, Any],
    *,
    expected_approval_id: Any = None,
    expected_preflight_sha256: Any = None,
    expected_source_identity: dict[str, Any] | None = None,
) -> None:
    claimed = marker.get("binding_sha256")
    unsigned = dict(marker)
    unsigned.pop("binding_sha256", None)
    source = marker.get("source_identity")
    approval_id = marker.get("approval_id")
    if (
        marker.get("version") != 1
        or marker.get("type") != "transcription_attempt_consumed"
        or not isinstance(approval_id, str)
        or not isinstance(source, dict)
        or marker.get("attempt_key") != attempt_key(approval_id, source)
        or claimed != canonical_json_sha256(unsigned)
    ):
        raise AttemptLedgerError("external consumed attempt marker binding is invalid")
    if expected_approval_id is not None and approval_id != expected_approval_id:
        raise AttemptLedgerError("external consumed attempt marker approval is invalid")
    if expected_preflight_sha256 is not None and marker.get("preflight_sha256") != expected_preflight_sha256:
        raise AttemptLedgerError("external consumed attempt marker preflight is invalid")
    if expected_source_identity is not None and source != expected_source_identity:
        raise AttemptLedgerError("external consumed attempt marker source identity is invalid")


def external_preflight_was_consumed(preflight_sha256: str) -> bool:
    """Consult durable per-user markers, independent of the project audit ledger."""
    anchor_zero, consumed = approval_registry_paths("0" * 32)
    registry = anchor_zero.parent
    found = False
    for path in sorted(registry.glob("*.json")):
        anchor = _read_secure_json(path, "approval anchor")
        _validate_anchor_contract(anchor)
        if anchor.get("preflight_sha256") == preflight_sha256 and anchor["consumed_attempts"]:
            found = True
    for path in sorted(consumed.iterdir()):
        if path.name.startswith("."):
            continue
        if path.suffix != ".json":
            raise AttemptLedgerError(f"unexpected file in consumed-capability registry: {path}")
        marker = _read_secure_json(path, "consumed attempt marker")
        _validate_consumed_marker(marker)
        if marker.get("preflight_sha256") == preflight_sha256:
            found = True
    return found


def assert_external_attempt_available(
    *,
    approval: dict[str, Any],
    edit_dir: Path,
    source_identity: dict[str, Any],
) -> Path:
    anchor = validate_approval_anchor(approval, edit_dir)
    key = attempt_key(str(approval["approval_id"]), source_identity)
    if key in anchor["consumed_attempts"]:
        raise AttemptLedgerError(
            f"external approval anchor capability is already consumed for source {source_identity.get('id')!r}"
        )
    marker = external_attempt_marker_path(str(approval["approval_id"]), source_identity)
    if marker.exists() or marker.is_symlink():
        # Securely open an existing marker so hardlink/special-file rollback tricks fail closed.
        value = _read_secure_json(marker, "consumed attempt marker")
        _validate_consumed_marker(
            value,
            expected_approval_id=approval["approval_id"],
            expected_preflight_sha256=approval["preflight_sha256"],
            expected_source_identity=source_identity,
        )
        raise AttemptLedgerError(
            f"external approval capability is already consumed for source {source_identity.get('id')!r}"
        )
    return marker


def consume_external_attempt(
    *,
    approval: dict[str, Any],
    edit_dir: Path,
    source_identity: dict[str, Any],
) -> Path:
    value = {
        "version": 1,
        "type": "transcription_attempt_consumed",
        "approval_id": approval["approval_id"],
        "preflight_sha256": approval["preflight_sha256"],
        "attempt_key": attempt_key(str(approval["approval_id"]), source_identity),
        "source_identity": source_identity,
        "consumed_at": datetime.now(timezone.utc).isoformat(),
    }
    value["binding_sha256"] = canonical_json_sha256(value)
    with approval_registry_lock():
        marker = assert_external_attempt_available(
            approval=approval,
            edit_dir=edit_dir,
            source_identity=source_identity,
        )
        anchor_path, _ = approval_registry_paths(str(approval["approval_id"]))
        anchor = validate_approval_anchor(approval, edit_dir)
        key = value["attempt_key"]
        anchor["consumed_attempts"][key] = dict(value)
        anchor.pop("binding_sha256", None)
        anchor["binding_sha256"] = canonical_json_sha256(anchor)
        write_json_atomic(anchor_path, anchor)
        try:
            _write_json_exclusive(marker, value)
        except FileExistsError as exc:
            raise AttemptLedgerError(
                "external approval capability was already consumed; retry requires new preflight/approval"
            ) from exc
    return marker


def _parse_attempt_records(handle: Any, ledger_path: Path) -> list[dict[str, Any]]:
    handle.seek(0)
    records: list[dict[str, Any]] = []
    expected_previous: str | None = None
    for line_number, raw_line in enumerate(handle, start=1):
        if not raw_line.endswith("\n"):
            raise AttemptLedgerError(
                f"attempt ledger has an incomplete final record at line {line_number}: {ledger_path}"
            )
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise AttemptLedgerError(
                f"attempt ledger has invalid JSON at line {line_number}: {ledger_path}"
            ) from exc
        if not isinstance(record, dict):
            raise AttemptLedgerError(f"attempt ledger line {line_number} is not an object")
        if (
            record.get("version") != 1
            or record.get("schema_version") != "1.0.0"
            or record.get("type") != "transcription_attempt"
            or record.get("sequence") != line_number
            or record.get("status") not in ATTEMPT_STATUSES
            or record.get("previous_record_sha256") != expected_previous
            or not isinstance(record.get("preflight_sha256"), str)
            or len(record["preflight_sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in record["preflight_sha256"])
        ):
            raise AttemptLedgerError(f"attempt ledger contract is invalid at line {line_number}")
        claimed = record.get("record_sha256")
        unsigned = dict(record)
        unsigned.pop("record_sha256", None)
        if claimed != canonical_json_sha256(unsigned):
            raise AttemptLedgerError(f"attempt ledger hash is invalid at line {line_number}")
        expected_previous = claimed
        records.append(record)
    return records


def read_attempt_ledger(edit_dir: Path) -> list[dict[str, Any]]:
    """Read and verify the complete append-only attempt hash chain."""
    ledger_path = contained_child(
        edit_dir,
        ATTEMPT_LEDGER_NAME,
        label="transcription attempt ledger",
    )
    if not ledger_path.exists():
        return []
    descriptor = secure_open_state(ledger_path, os.O_RDONLY)
    with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
        return _parse_attempt_records(handle, ledger_path)


def consumed_attempt_keys(edit_dir: Path) -> set[str]:
    return {
        str(record["attempt_key"])
        for record in read_attempt_ledger(edit_dir)
        if record.get("status") == "attempt_started"
    }


def assert_attempt_available(
    edit_dir: Path,
    *,
    approval_id: str,
    source_identity: dict[str, Any],
) -> str:
    key = attempt_key(approval_id, source_identity)
    if key in consumed_attempt_keys(edit_dir):
        raise AttemptLedgerError(
            "this approval already authorized one network attempt for source "
            f"{source_identity.get('id')!r}; create a new preflight and obtain a new approval"
        )
    return key


def append_attempt_event(
    edit_dir: Path,
    *,
    approval_id: str,
    preflight_sha256: str,
    source_identity: dict[str, Any],
    status: str,
    outcome_code: str | None = None,
) -> dict[str, Any]:
    """Append one fsynced 0600 event while preserving a verifiable hash chain."""
    if status not in ATTEMPT_STATUSES:
        raise AttemptLedgerError(f"unsupported attempt status: {status!r}")
    if (
        not isinstance(preflight_sha256, str)
        or len(preflight_sha256) != 64
        or any(char not in "0123456789abcdef" for char in preflight_sha256)
    ):
        raise AttemptLedgerError("attempt preflight_sha256 is invalid")
    ledger_path = contained_child(
        edit_dir,
        ATTEMPT_LEDGER_NAME,
        label="transcription attempt ledger",
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    first_create = not ledger_path.exists()
    descriptor = secure_open_state(
        ledger_path,
        os.O_RDWR | os.O_APPEND,
        create=True,
    )
    try:
        import fcntl
    except ModuleNotFoundError as exc:  # pragma: no cover - same support boundary as lock
        os.close(descriptor)
        raise AttemptLedgerError("safe attempt-ledger locking is unavailable") from exc
    with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=True) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        records = _parse_attempt_records(handle, ledger_path)
        key = attempt_key(approval_id, source_identity)
        keyed = [record for record in records if record.get("attempt_key") == key]
        if status == "attempt_started":
            if keyed:
                raise AttemptLedgerError(
                    "this approval/source network attempt has already been consumed"
                )
        else:
            if not keyed or keyed[0].get("status") != "attempt_started":
                raise AttemptLedgerError("attempt outcome has no matching started event")
            if keyed[0].get("preflight_sha256") != preflight_sha256:
                raise AttemptLedgerError("attempt outcome preflight differs from started event")
            if any(record.get("status") != "attempt_started" for record in keyed):
                raise AttemptLedgerError("attempt already has a terminal outcome")
        event = {
            "version": 1,
            "schema_version": "1.0.0",
            "type": "transcription_attempt",
            "sequence": len(records) + 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "approval_id": approval_id,
            "preflight_sha256": preflight_sha256,
            "attempt_key": key,
            "source_identity": source_identity,
            "status": status,
            "outcome_code": outcome_code,
            "previous_record_sha256": records[-1]["record_sha256"] if records else None,
        }
        event["record_sha256"] = canonical_json_sha256(event)
        handle.seek(0, os.SEEK_END)
        handle.write(canonical_json_bytes(event).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if first_create:
            fsync_directory(ledger_path.parent)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event


@contextlib.contextmanager
def project_transcription_lock(edit_dir: Path):
    """Hold a non-blocking interprocess lock for one project's paid work.

    ``flock`` is intentionally used instead of a create-only sentinel: the OS
    releases the lock after a crash, so an interrupted run remains resumable.
    The product currently targets macOS/Linux; fail closed on platforms without
    advisory file locking instead of pretending concurrent uploads are safe.
    """
    try:
        import fcntl
    except ModuleNotFoundError as exc:  # pragma: no cover - non-POSIX fail-closed path
        raise TranscriptionLockError(
            "safe transcription locking is unavailable on this platform"
        ) from exc

    canonical_edit = edit_dir.expanduser().resolve()
    canonical_edit.mkdir(parents=True, exist_ok=True)
    lock_path = contained_child(
        canonical_edit,
        ".transcription.lock",
        label="project transcription lock",
    )
    descriptor = secure_open_state(lock_path, os.O_RDWR, create=True)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8", closefd=True)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = handle.read().strip()
            detail = f" ({owner})" if owner else ""
            raise TranscriptionLockError(
                f"another transcription process is already running for {canonical_edit}{detail}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        json.dump(
            {"pid": os.getpid(), "host": socket.gethostname()},
            handle,
            ensure_ascii=False,
            sort_keys=True,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield lock_path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
