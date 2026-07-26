from __future__ import annotations

import hmac
import ipaddress
import json
import os
import stat
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


_DISCOVERY_FIELDS = frozenset({"pid", "address", "started_at", "daemon_fingerprint"})
_MAX_DISCOVERY_BYTES = 16_384
_STANDALONE_DISTRIBUTION_ID = "lilies-agent-standalone"


class LocalLiliesHealthClient(Protocol):
    async def health(self, base_url: str) -> dict[str, Any]: ...


def _process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
    }


def _is_loopback_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        loopback = ipaddress.ip_address(host).is_loopback
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and loopback
        and "%" not in host
        and port is not None
        and 1 <= port <= 65_535
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _read_discovery_record(path: Path) -> dict[str, Any]:
    parent = path.parent
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(parent, parent_flags)
    try:
        initial_parent = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(initial_parent.st_mode):
            raise PermissionError("discovery directory must be a real directory")
        if initial_parent.st_uid != os.getuid():
            raise PermissionError("discovery directory belongs to another user")
        if stat.S_IMODE(initial_parent.st_mode) & 0o077:
            raise PermissionError("discovery directory must not be accessible to other users")

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        try:
            initial = os.fstat(descriptor)
            if not stat.S_ISREG(initial.st_mode):
                raise ValueError("discovery record is not a regular file")
            if initial.st_uid != os.getuid():
                raise PermissionError("discovery record belongs to another user")
            if stat.S_IMODE(initial.st_mode) != 0o600:
                raise PermissionError("discovery record must use mode 0600")
            if initial.st_size > _MAX_DISCOVERY_BYTES:
                raise ValueError("discovery record exceeds the size limit")

            chunks: list[bytes] = []
            remaining = _MAX_DISCOVERY_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 4_096))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _MAX_DISCOVERY_BYTES or len(raw) != initial.st_size:
                raise ValueError("discovery record changed or exceeds the size limit")

            final = os.fstat(descriptor)
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            final_parent = os.fstat(parent_descriptor)
            current_parent = os.stat(parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(final.st_mode)
                or final.st_uid != os.getuid()
                or stat.S_IMODE(final.st_mode) != 0o600
                or final.st_dev != initial.st_dev
                or final.st_ino != initial.st_ino
                or final.st_size != initial.st_size
                or not stat.S_ISREG(current.st_mode)
                or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o600
                or current.st_dev != initial.st_dev
                or current.st_ino != initial.st_ino
                or current.st_size != initial.st_size
                or not stat.S_ISDIR(final_parent.st_mode)
                or final_parent.st_uid != os.getuid()
                or stat.S_IMODE(final_parent.st_mode) & 0o077
                or final_parent.st_dev != initial_parent.st_dev
                or final_parent.st_ino != initial_parent.st_ino
                or not stat.S_ISDIR(current_parent.st_mode)
                or current_parent.st_uid != os.getuid()
                or stat.S_IMODE(current_parent.st_mode) & 0o077
                or current_parent.st_dev != initial_parent.st_dev
                or current_parent.st_ino != initial_parent.st_ino
            ):
                raise PermissionError("discovery record or directory changed while being read")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != _DISCOVERY_FIELDS:
        raise ValueError("discovery record does not match the public schema")
    if (
        isinstance(value["pid"], bool)
        or not isinstance(value["pid"], int)
        or value["pid"] <= 0
        or value["pid"] > 2_147_483_647
    ):
        raise ValueError("discovery record contains an invalid pid")
    if not isinstance(value["address"], str):
        raise ValueError("discovery record contains an invalid address")
    if not _is_loopback_origin(value["address"]):
        raise ValueError("discovery record must contain a loopback HTTP origin")
    if not isinstance(value["started_at"], str) or not value["started_at"]:
        raise ValueError("discovery record contains an invalid start time")
    fingerprint = value["daemon_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 71
        or not fingerprint.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in fingerprint[7:])
    ):
        raise ValueError("discovery record contains an invalid daemon fingerprint")
    return value


async def discover_local_lilies(
    path: Path,
    client: LocalLiliesHealthClient,
) -> dict[str, Any]:
    """Discover a same-user loopback daemon without reading any daemon secret."""

    resolved = path.expanduser()
    try:
        if resolved.is_symlink():
            return _unavailable("unsafe_record")
        record = _read_discovery_record(resolved)
    except FileNotFoundError:
        return _unavailable("not_running")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _unavailable("unsafe_record")

    if not _process_is_live(record["pid"]):
        return _unavailable("stale_record")

    try:
        health = await client.health(record["address"])
    except Exception:
        return _unavailable("health_unreachable")
    if (
        not isinstance(health, dict)
        or health.get("service") != "lilies"
        or health.get("status") != "ok"
        or health.get("schema_version") != "1.0"
        or health.get("distribution_id") != _STANDALONE_DISTRIBUTION_ID
    ):
        return _unavailable("health_mismatch")
    health_fingerprint = health.get("daemon_fingerprint")
    if not isinstance(health_fingerprint, str) or not hmac.compare_digest(
        health_fingerprint,
        record["daemon_fingerprint"],
    ):
        return _unavailable("identity_mismatch")
    if not isinstance(health.get("model_egress_enabled"), bool):
        return _unavailable("health_mismatch")

    return {
        "status": "available",
        "base_url": record["address"],
        "daemon_fingerprint": record["daemon_fingerprint"],
        "pid": record["pid"],
        "started_at": record["started_at"],
        "daemon_version": health.get("daemon_version"),
        "model_egress_enabled": health["model_egress_enabled"],
    }
