from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Sequence

from .collaboration_models import VerificationClaim
from .stable_verification import StableVerificationRejected
from .stable_verification_coordinator import StableVerificationCoordinator


def _read_admin_file(path: Path, *, secret: bool = False) -> bytes:
    path = Path(path)
    if path.is_symlink():
        raise StableVerificationRejected("admin input cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StableVerificationRejected("admin input is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        allowed_modes = {0o400, 0o600} if not secret else {0o400}
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
        ):
            raise StableVerificationRejected(
                "admin input has an unsafe read boundary"
            )
        if metadata.st_size > 16 * 1024 * 1024:
            raise StableVerificationRejected("admin input exceeds its limit")
        payload = os.read(descriptor, metadata.st_size + 1)
        if len(payload) != metadata.st_size:
            raise StableVerificationRejected("admin input changed while read")
        return payload
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o400)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _json(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lilies-stability")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--state-root", type=Path, required=True)
    record.add_argument("--broker-root", type=Path, required=True)
    record.add_argument("--task-id", required=True)
    record.add_argument("--revision", type=int, required=True)
    record.add_argument("--claim-file", type=Path, required=True)
    record.add_argument("--seed-identity-key-file", type=Path, required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("--state-root", type=Path, required=True)
    replay.add_argument("--task-id", required=True)
    replay.add_argument("--revision", type=int, required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--state-root", type=Path, required=True)
    export.add_argument("--task-id", required=True)
    export.add_argument("--revision", type=int, required=True)
    export.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "record":
            claim = VerificationClaim.model_validate_json(
                _read_admin_file(args.claim_file)
            )
            seed_key = _read_admin_file(
                args.seed_identity_key_file,
                secret=True,
            )
            if len(seed_key) < 32:
                raise StableVerificationRejected(
                    "platform hidden-seed key is invalid"
                )
            coordinator = StableVerificationCoordinator(
                state_root=args.state_root,
                broker_root=args.broker_root,
                platform_seed_key_resolver=lambda _context: seed_key,
            )
            result = coordinator.verify_and_record(
                task_id=args.task_id,
                revision=args.revision,
                claim=claim,
            )
            sys.stdout.buffer.write(_json(result) + b"\n")
            return 0
        coordinator = StableVerificationCoordinator(state_root=args.state_root)
        if args.command == "replay":
            result = coordinator.replay(
                task_id=args.task_id,
                revision=args.revision,
            )
            sys.stdout.buffer.write(_json(result) + b"\n")
            return 0
        bundle = coordinator.export_qualification_bundle(
            task_id=args.task_id,
            revision=args.revision,
        )
        _write_new(args.output, _json(bundle))
        sys.stdout.buffer.write(
            _json(
                {
                    "status": "qualification_bundle_written",
                    "bundle_digest": bundle.bundle_digest,
                }
            )
            + b"\n"
        )
        return 0
    except Exception as error:
        sys.stderr.write(
            json.dumps(
                {
                    "error": type(error).__name__,
                    "message": "stable verification command was rejected",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
