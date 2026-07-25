from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 128 * 1024 * 1024
TASK_ID = "EXP-LILIES-001"
REVISION = 20


class HostSnapshotVerificationError(RuntimeError):
    """The verifier input or output boundary is unsafe."""


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _read_json(path: Path, *, private: bool) -> tuple[dict[str, Any], bytes]:
    path = Path(path)
    if path.is_symlink():
        raise HostSnapshotVerificationError("verifier input cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HostSnapshotVerificationError("verifier input is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        allowed_modes = {0o600} if private else {0o400, 0o600, 0o644}
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
            or metadata.st_size > MAX_INPUT_BYTES
        ):
            raise HostSnapshotVerificationError("verifier input boundary is unsafe")
        payload = os.read(descriptor, metadata.st_size + 1)
        if len(payload) != metadata.st_size:
            raise HostSnapshotVerificationError("verifier input changed while read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostSnapshotVerificationError("verifier input is not JSON") from error
    if not isinstance(value, dict):
        raise HostSnapshotVerificationError("verifier input is not an object")
    return value, payload


def _resolve_pointer(value: Any, pointer: str) -> Any:
    current = value
    if not pointer.startswith("/"):
        raise HostSnapshotVerificationError("host oracle JSON pointer is invalid")
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdecimal() or int(part) >= len(current):
                raise KeyError(pointer)
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(pointer)
    return current


def _difference(
    check_id: str,
    *,
    expected: Any,
    actual: Any,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "expected": expected,
        "actual": actual,
    }


def verify_snapshot(
    snapshot: dict[str, Any],
    oracle: dict[str, Any],
    *,
    snapshot_digest: str,
    oracle_digest: str,
) -> dict[str, Any]:
    if (
        snapshot.get("schema_version") != "1.0"
        or snapshot.get("task_id") != TASK_ID
        or snapshot.get("revision") != REVISION
        or snapshot.get("phase") != "final"
        or oracle.get("schema_version") != "1.0"
        or oracle.get("task_id") != TASK_ID
        or oracle.get("revision") != REVISION
        or oracle.get("validation_mode") != "real_host"
        or oracle.get("snapshot_phase") != "final"
    ):
        raise HostSnapshotVerificationError(
            "host snapshot and oracle do not share the frozen binding"
        )
    checks = oracle.get("checks")
    records = snapshot.get("records")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(item, dict) for item in checks)
        or not isinstance(records, list)
        or any(not isinstance(item, dict) for item in records)
    ):
        raise HostSnapshotVerificationError("host oracle denominator is invalid")
    differences: list[dict[str, Any]] = []
    check_ids: list[str] = []
    record_bindings = 0
    fault_gates = 0
    for check in checks:
        check_id = check.get("check_id")
        pointer = check.get("json_pointer")
        kind = check.get("kind")
        if (
            not isinstance(check_id, str)
            or not check_id
            or not isinstance(pointer, str)
            or kind not in {"json_equals", "json_length"}
        ):
            raise HostSnapshotVerificationError("host oracle check is invalid")
        check_ids.append(check_id)
        try:
            actual = _resolve_pointer(snapshot, pointer)
        except KeyError:
            actual = {"missing": pointer}
        if kind == "json_length":
            actual = len(actual) if isinstance(actual, (dict, list, str)) else None
        expected = check.get("expected")
        if actual != expected:
            differences.append(
                _difference(check_id, expected=expected, actual=actual)
            )
        record_id = check.get("record_id")
        scenario = check.get("scenario")
        if record_id is None and scenario is None:
            continue
        if (
            not isinstance(record_id, str)
            or scenario
            not in {
                "exact_match",
                "low_confidence",
                "duplicate",
                "conflict",
                "transient_error",
                "permission_denied",
            }
            or not pointer.startswith("/records/")
        ):
            raise HostSnapshotVerificationError("host record binding is invalid")
        index = int(pointer.split("/")[2])
        if index >= len(records):
            continue
        record = records[index]
        record_bindings += 1
        if record.get("record_id") != record_id or record.get("scenario") != scenario:
            differences.append(
                _difference(
                    f"{check_id}-binding",
                    expected={"record_id": record_id, "scenario": scenario},
                    actual={
                        "record_id": record.get("record_id"),
                        "scenario": record.get("scenario"),
                    },
                )
            )
        transient_count = record.get("injected_transient_failures")
        permission_count = record.get("injected_permission_denials")
        expected_transient = 1 if scenario == "transient_error" else 0
        expected_permission = 1 if scenario == "permission_denied" else 0
        fault_gates += 2
        if transient_count != expected_transient:
            differences.append(
                _difference(
                    f"{check_id}-transient-gate",
                    expected=expected_transient,
                    actual=transient_count,
                )
            )
        if permission_count != expected_permission:
            differences.append(
                _difference(
                    f"{check_id}-permission-gate",
                    expected=expected_permission,
                    actual=permission_count,
                )
            )
    if len(check_ids) != len(set(check_ids)):
        raise HostSnapshotVerificationError("host oracle check IDs are not unique")
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": snapshot.get("seed"),
        "oracle_id": oracle.get("oracle_id"),
        "oracle_digest": oracle_digest,
        "snapshot_digest": snapshot_digest,
        "check_count": len(checks),
        "passed_check_count": len(checks)
        - sum(
            not str(item["check_id"]).endswith(("-binding", "-gate"))
            for item in differences
        ),
        "record_binding_gate_count": record_bindings,
        "fault_gate_count": fault_gates,
        "differences": differences,
        "verdict": "independently_verified" if not differences else "verification_failed",
    }


def _write_new_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a verifier-only EXP-LILIES-001 final host snapshot."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        snapshot, snapshot_payload = _read_json(args.snapshot, private=True)
        oracle, oracle_payload = _read_json(args.oracle, private=False)
        result = verify_snapshot(
            snapshot,
            oracle,
            snapshot_digest=_digest(snapshot_payload),
            oracle_digest=_digest(oracle_payload),
        )
        _write_new_private(args.output, result)
        print(
            json.dumps(
                {
                    "verdict": result["verdict"],
                    "check_count": result["check_count"],
                    "passed_check_count": result["passed_check_count"],
                    "difference_count": len(result["differences"]),
                    "output": str(args.output),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0 if result["verdict"] == "independently_verified" else 3
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": type(error).__name__,
                    "message": "host snapshot verification was rejected",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
