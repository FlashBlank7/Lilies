from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_platform import local_lilies_discovery


FINGERPRINT = "sha256:" + "a" * 64


class HealthClient:
    def __init__(
        self,
        health: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.health_payload = health or {
            "schema_version": "1.0",
            "service": "lilies",
            "status": "ok",
            "distribution_id": "lilies-agent-standalone",
            "daemon_version": "0.4.13",
            "daemon_fingerprint": FINGERPRINT,
            "model_egress_enabled": False,
        }
        self.error = error
        self.calls: list[str] = []

    async def health(self, base_url: str) -> dict[str, Any]:
        self.calls.append(base_url)
        if self.error is not None:
            raise self.error
        return self.health_payload


def _write_record(
    path: Path,
    *,
    pid: int | None = None,
    fingerprint: str = FINGERPRINT,
    address: str = "http://127.0.0.1:8765",
    mode: int = 0o600,
) -> dict[str, Any]:
    record = {
        "pid": pid if pid is not None else os.getpid(),
        "address": address,
        "started_at": "2026-07-26T01:02:03+00:00",
        "daemon_fingerprint": fingerprint,
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return record


@pytest.mark.asyncio
async def test_discovery_returns_only_verified_public_daemon_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "daemon.json"
    record = _write_record(path)
    client = HealthClient()

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {
        "status": "available",
        "base_url": record["address"],
        "daemon_fingerprint": FINGERPRINT,
        "pid": record["pid"],
        "started_at": record["started_at"],
        "daemon_version": "0.4.13",
        "model_egress_enabled": False,
    }
    assert client.calls == [record["address"]]
    serialized = json.dumps(result, sort_keys=True).casefold()
    assert all(
        forbidden not in serialized
        for forbidden in (
            "access_token",
            "api_token",
            "authorization",
            "pairing_code",
            "private_key",
            "secret",
        )
    )


@pytest.mark.asyncio
async def test_missing_discovery_record_does_not_probe_or_scan_ports(
    tmp_path: Path,
) -> None:
    client = HealthClient()

    result = await local_lilies_discovery.discover_local_lilies(
        tmp_path / "missing.json",
        client,
    )

    assert result == {"status": "unavailable", "reason": "not_running"}
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_kind",
    [
        "mode",
        "symlink",
        "fifo",
        "schema",
        "remote_url",
        "dns_url",
        "scoped_ipv6",
        "bool_pid",
        "oversized_pid",
    ],
)
async def test_unsafe_discovery_records_fail_closed_before_health(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    path = tmp_path / "daemon.json"
    if unsafe_kind == "mode":
        _write_record(path, mode=0o644)
    elif unsafe_kind == "symlink":
        target = tmp_path / "target.json"
        _write_record(target)
        path.symlink_to(target)
    elif unsafe_kind == "fifo":
        os.mkfifo(path, mode=0o600)
    elif unsafe_kind == "remote_url":
        _write_record(path, address="https://example.invalid:8765")
    elif unsafe_kind == "dns_url":
        _write_record(path, address="http://localhost:8765")
    elif unsafe_kind == "scoped_ipv6":
        _write_record(path, address="http://[::1%25lo0]:8765")
    elif unsafe_kind == "bool_pid":
        _write_record(path, pid=True)
    elif unsafe_kind == "oversized_pid":
        _write_record(path, pid=2_147_483_648)
    else:
        path.write_text('{"address":"http://127.0.0.1:8765"}\n', encoding="utf-8")
        path.chmod(0o600)
    client = HealthClient()

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "unsafe_record"}
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_owner_target", ["directory", "record"])
async def test_foreign_owned_discovery_boundary_fails_before_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign_owner_target: str,
) -> None:
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    path = parent / "daemon.json"
    _write_record(path)
    client = HealthClient()
    original_fstat = os.fstat

    def foreign_owned_fstat(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        is_target = (
            foreign_owner_target == "directory"
            and local_lilies_discovery.stat.S_ISDIR(metadata.st_mode)
        ) or (
            foreign_owner_target == "record"
            and local_lilies_discovery.stat.S_ISREG(metadata.st_mode)
        )
        if not is_target:
            return metadata
        values = list(metadata)
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(
        local_lilies_discovery.os,
        "fstat",
        foreign_owned_fstat,
    )

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "unsafe_record"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_discovery_accumulates_short_reads_until_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "daemon.json"
    record = _write_record(path)
    client = HealthClient()
    original_read = os.read

    def short_read(descriptor: int, requested: int) -> bytes:
        return original_read(descriptor, min(requested, 3))

    monkeypatch.setattr(local_lilies_discovery.os, "read", short_read)

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result["status"] == "available"
    assert result["pid"] == record["pid"]
    assert client.calls == [record["address"]]


@pytest.mark.asyncio
async def test_replaced_discovery_record_fails_post_read_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "daemon.json"
    _write_record(path)
    replacement = tmp_path / "replacement.json"
    _write_record(replacement, fingerprint="sha256:" + "b" * 64)
    client = HealthClient()
    original_read = os.read
    swapped = False

    def replacing_read(descriptor: int, requested: int) -> bytes:
        nonlocal swapped
        chunk = original_read(descriptor, requested)
        if chunk and not swapped:
            swapped = True
            os.replace(replacement, path)
        return chunk

    monkeypatch.setattr(local_lilies_discovery.os, "read", replacing_read)

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "unsafe_record"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_growing_discovery_record_fails_bounded_complete_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "daemon.json"
    _write_record(path)
    client = HealthClient()
    original_read = os.read
    grown = False

    def growing_read(descriptor: int, requested: int) -> bytes:
        nonlocal grown
        chunk = original_read(descriptor, requested)
        if chunk and not grown:
            grown = True
            with path.open("ab") as handle:
                handle.write(b" ")
        return chunk

    monkeypatch.setattr(local_lilies_discovery.os, "read", growing_read)

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "unsafe_record"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_replaced_discovery_parent_fails_post_read_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    path = parent / "daemon.json"
    _write_record(path)
    moved_parent = tmp_path / "old-state"
    client = HealthClient()
    original_read = os.read
    swapped = False

    def replacing_parent_read(descriptor: int, requested: int) -> bytes:
        nonlocal swapped
        chunk = original_read(descriptor, requested)
        if chunk and not swapped:
            swapped = True
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)
            _write_record(parent / "daemon.json")
        return chunk

    monkeypatch.setattr(
        local_lilies_discovery.os,
        "read",
        replacing_parent_read,
    )

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "unsafe_record"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_symlinked_discovery_directory_fails_closed_before_health(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir(mode=0o700)
    _write_record(real_directory / "daemon.json")
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    client = HealthClient()

    result = await local_lilies_discovery.discover_local_lilies(
        linked_directory / "daemon.json",
        client,
    )

    assert result == {"status": "unavailable", "reason": "unsafe_record"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_stale_pid_fails_before_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "daemon.json"
    _write_record(path, pid=987_654)
    client = HealthClient()
    monkeypatch.setattr(local_lilies_discovery, "_process_is_live", lambda _pid: False)

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "stale_record"}
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "reason"),
    [
        (
            {
                "schema_version": "2.0",
                "service": "lilies",
                "status": "ok",
                "distribution_id": "lilies-agent-standalone",
                "daemon_fingerprint": FINGERPRINT,
            },
            "health_mismatch",
        ),
        (
            {
                "schema_version": "1.0",
                "service": "another-service",
                "status": "ok",
                "distribution_id": "lilies-agent-standalone",
                "daemon_fingerprint": FINGERPRINT,
            },
            "health_mismatch",
        ),
        (
            {
                "schema_version": "1.0",
                "service": "lilies",
                "status": "ok",
                "distribution_id": "legacy-embedded-lilies",
                "daemon_fingerprint": FINGERPRINT,
            },
            "health_mismatch",
        ),
        (
            {
                "schema_version": "1.0",
                "service": "lilies",
                "status": "ok",
                "distribution_id": "lilies-agent-standalone",
                "daemon_fingerprint": "sha256:" + "b" * 64,
            },
            "identity_mismatch",
        ),
    ],
)
async def test_health_and_identity_mismatch_never_become_available(
    tmp_path: Path,
    health: dict[str, Any],
    reason: str,
) -> None:
    path = tmp_path / "daemon.json"
    _write_record(path)
    client = HealthClient(health)

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": reason}
    assert client.calls == ["http://127.0.0.1:8765"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_value", ["false", 0, 1, None, [], {}])
async def test_health_model_egress_must_be_an_actual_boolean(
    tmp_path: Path,
    unsafe_value: Any,
) -> None:
    path = tmp_path / "daemon.json"
    _write_record(path)
    health = {
        "schema_version": "1.0",
        "service": "lilies",
        "status": "ok",
        "distribution_id": "lilies-agent-standalone",
        "daemon_version": "0.4.13",
        "daemon_fingerprint": FINGERPRINT,
        "model_egress_enabled": unsafe_value,
    }
    client = HealthClient(health)

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "health_mismatch"}
    assert client.calls == ["http://127.0.0.1:8765"]


@pytest.mark.asyncio
async def test_unreachable_health_is_not_reported_as_detected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "daemon.json"
    _write_record(path)
    client = HealthClient(error=RuntimeError("offline"))

    result = await local_lilies_discovery.discover_local_lilies(path, client)

    assert result == {"status": "unavailable", "reason": "health_unreachable"}
    assert client.calls == ["http://127.0.0.1:8765"]
