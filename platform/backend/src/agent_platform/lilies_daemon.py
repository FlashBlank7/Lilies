from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lilies_config import LiliesSettings


DAEMON_INFO_FIELDS = frozenset({"pid", "address", "started_at", "daemon_fingerprint"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_daemon_info(settings: LiliesSettings, *, pid: int | None = None) -> dict[str, Any]:
    """Atomically write the non-secret daemon rendezvous record with mode 0600."""

    info: dict[str, Any] = {
        "pid": pid if pid is not None else os.getpid(),
        "address": settings.base_url,
        "started_at": utc_now(),
        "daemon_fingerprint": settings.daemon_fingerprint(),
    }
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".daemon.", dir=settings.data_dir)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(info, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, settings.daemon_file)
        os.chmod(settings.daemon_file, 0o600)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return info


def read_daemon_info(settings: LiliesSettings) -> dict[str, Any]:
    raw = json.loads(settings.daemon_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != DAEMON_INFO_FIELDS:
        raise ValueError("invalid daemon.json schema")
    if not isinstance(raw["pid"], int) or raw["pid"] <= 0:
        raise ValueError("invalid daemon pid")
    if not isinstance(raw["address"], str) or not raw["address"].startswith("http://"):
        raise ValueError("invalid daemon address")
    if raw["daemon_fingerprint"] != settings.daemon_fingerprint():
        raise ValueError("daemon fingerprint does not match the local identity key")
    mode = settings.daemon_file.stat().st_mode & 0o777
    if mode != 0o600:
        raise PermissionError(f"daemon.json must have mode 0600, found {mode:04o}")
    return raw


def remove_daemon_info(settings: LiliesSettings, *, expected_pid: int | None = None) -> bool:
    try:
        current = read_daemon_info(settings)
    except FileNotFoundError:
        return False
    if expected_pid is not None and current["pid"] != expected_pid:
        return False
    settings.daemon_file.unlink(missing_ok=True)
    return True


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_record_is_live(settings: LiliesSettings) -> bool:
    try:
        return process_is_alive(int(read_daemon_info(settings)["pid"]))
    except (FileNotFoundError, ValueError, PermissionError, json.JSONDecodeError):
        return False


def is_loopback_address(value: str | None) -> bool:
    if value is None:
        return False
    host = value.rsplit("%", 1)[0]
    return host in {"127.0.0.1", "::1", "localhost", "testclient"} or host.startswith(
        "127."
    )
