from __future__ import annotations

import ctypes
import hashlib
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_platform.kernel_boot_identity import (
    DARWIN_BOOT_SESSION_SCHEME,
    LEGACY_DARWIN_BOOT_TIME_SCHEME,
    LINUX_BOOT_ID_SCHEME,
    KernelBootIdentity,
    generation_follows_activation,
    legacy_darwin_boot_digest,
    prove_generation_follows_activation,
    read_current_kernel_boot_identity,
    read_darwin_boot_identity,
    read_darwin_sysctl,
)


BOOT_UUID = "12345678-1234-4234-9234-123456789abc"
OTHER_BOOT_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BOOT_STARTED = datetime(2026, 7, 26, 1, 2, 3, 456_789, tzinfo=timezone.utc)
ACTIVATED_AT = BOOT_STARTED + timedelta(hours=1)


class FakeSysctl:
    def __init__(
        self,
        values: dict[str, bytes],
        *,
        first_error: set[str] | None = None,
        second_error: set[str] | None = None,
        reported_growth: set[str] | None = None,
        reported_shrink: set[str] | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self.values = values
        self.first_error = first_error or set()
        self.second_error = second_error or set()
        self.reported_growth = reported_growth or set()
        self.reported_shrink = reported_shrink or set()
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, bool]] = []

    def __call__(
        self,
        name: bytes,
        old_value: object | None,
        old_size: object,
        new_value: object | None,
        new_size: int,
    ) -> int:
        assert new_value is None
        assert new_size == 0
        decoded_name = name.decode("ascii")
        is_value_call = old_value is not None
        self.calls.append((decoded_name, is_value_call))
        if decoded_name in self.raise_on:
            raise OSError("synthetic sysctl failure")
        payload = self.values[decoded_name]
        size = old_size._obj
        if not is_value_call:
            size.value = len(payload)
            return 1 if decoded_name in self.first_error else 0
        if decoded_name in self.reported_growth:
            size.value = len(payload) + 1
            return 0
        if decoded_name in self.reported_shrink:
            ctypes.memmove(old_value, payload, len(payload) - 1)
            size.value = len(payload) - 1
            return 0
        if decoded_name in self.second_error:
            return 1
        ctypes.memmove(old_value, payload, len(payload))
        size.value = len(payload)
        return 0


def _darwin_boottime(epoch_second: int, microsecond: int) -> bytes:
    return struct.pack("=qi4x", epoch_second, microsecond)


def _darwin_reader(
    *,
    boot_uuid: str = BOOT_UUID,
    epoch_second: int = int(BOOT_STARTED.timestamp()),
    microsecond: int = BOOT_STARTED.microsecond,
) -> FakeSysctl:
    return FakeSysctl(
        {
            "kern.bootsessionuuid": boot_uuid.encode("ascii") + b"\x00",
            "kern.boottime": _darwin_boottime(epoch_second, microsecond),
        }
    )


def _stable_digest(prefix: str, value: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"{prefix}:{value}".encode("utf-8")
    ).hexdigest()


def _darwin_identity(
    *,
    boot_uuid: str = BOOT_UUID,
    epoch_second: int = int(BOOT_STARTED.timestamp()),
) -> KernelBootIdentity:
    return KernelBootIdentity(
        scheme=DARWIN_BOOT_SESSION_SCHEME,
        digest=_stable_digest("darwin-bootsessionuuid", boot_uuid),
        started_at=datetime.fromtimestamp(epoch_second, tz=timezone.utc),
        boot_epoch_second=epoch_second,
    )


def test_darwin_bootsessionuuid_is_stable_when_boottime_microseconds_drift() -> None:
    first = read_darwin_boot_identity(
        sysctlbyname=_darwin_reader(microsecond=111_111)
    )
    second = read_darwin_boot_identity(
        sysctlbyname=_darwin_reader(microsecond=999_999)
    )

    assert first == second
    assert first is not None
    assert first.scheme == DARWIN_BOOT_SESSION_SCHEME
    assert first.started_at == BOOT_STARTED.replace(microsecond=0)
    assert first.digest == _stable_digest(
        "darwin-bootsessionuuid",
        BOOT_UUID,
    )


def test_stable_identity_accepts_same_boot_time_evidence_drift() -> None:
    activation_started = BOOT_STARTED
    current_identity = _darwin_identity()

    proof = prove_generation_follows_activation(
        activation_identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        activation_boot_digest=current_identity.digest,
        activation_boot_started_at=activation_started,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=current_identity,
        current_monotonic_ns=101,
    )

    assert proof is not None
    assert proof.identity_match == "stable-kernel-identity"
    assert proof.identity_scheme == DARWIN_BOOT_SESSION_SCHEME
    assert proof.order_basis == "process-start-monotonic-ns"


def test_exact_legacy_darwin_fence_accepts_only_same_epoch_second() -> None:
    legacy_digest = legacy_darwin_boot_digest(BOOT_STARTED)
    assert legacy_digest == _stable_digest(
        "darwin",
        f"{int(BOOT_STARTED.timestamp())}:{BOOT_STARTED.microsecond}",
    )

    proof = prove_generation_follows_activation(
        activation_identity_scheme=LEGACY_DARWIN_BOOT_TIME_SCHEME,
        activation_boot_digest=legacy_digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=_darwin_identity(),
        current_monotonic_ns=101,
    )
    cross_boot = generation_follows_activation(
        activation_identity_scheme=LEGACY_DARWIN_BOOT_TIME_SCHEME,
        activation_boot_digest=legacy_digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=_darwin_identity(
            epoch_second=int(BOOT_STARTED.timestamp()) + 1
        ),
        current_monotonic_ns=101,
    )

    assert proof is not None
    assert proof.identity_match == "legacy-darwin-boottime"
    assert not cross_boot


def test_observed_legacy_microsecond_drift_is_exactly_bridged() -> None:
    activation_started = datetime.fromtimestamp(
        1_784_713_852,
        tz=timezone.utc,
    ).replace(microsecond=781_734)
    current_identity = _darwin_identity(epoch_second=1_784_713_852)

    assert legacy_darwin_boot_digest(activation_started) == (
        "sha256:"
        "72c2e7c4e9b3c6c0952d735899a1f41d4a820a67aa9d1e64261210bd62b59ede"
    )
    assert generation_follows_activation(
        activation_identity_scheme=LEGACY_DARWIN_BOOT_TIME_SCHEME,
        activation_boot_digest=legacy_darwin_boot_digest(activation_started),
        activation_boot_started_at=activation_started,
        activated_at=activation_started + timedelta(minutes=1),
        activation_monotonic_ns=302_429_367_464_458,
        current_identity=current_identity,
        current_monotonic_ns=303_407_307_311_208,
    )


def test_same_stable_identity_does_not_equate_boot_time_evidence() -> None:
    identity = _darwin_identity(
        epoch_second=int(ACTIVATED_AT.timestamp()) + 1,
    )
    assert generation_follows_activation(
        activation_identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        activation_boot_digest=identity.digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=identity,
        current_monotonic_ns=101,
    )


def test_stable_identity_rejects_activation_before_boot_evidence() -> None:
    identity = _darwin_identity()
    assert not generation_follows_activation(
        activation_identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        activation_boot_digest=identity.digest,
        activation_boot_started_at=ACTIVATED_AT + timedelta(seconds=1),
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=identity,
        current_monotonic_ns=101,
    )


@pytest.mark.parametrize(
    ("digest", "current_identity", "current_monotonic_ns"),
    [
        (
            _stable_digest("darwin-bootsessionuuid", BOOT_UUID),
            _darwin_identity(),
            100,
        ),
        (
            _stable_digest("darwin-bootsessionuuid", BOOT_UUID),
            _darwin_identity(boot_uuid=OTHER_BOOT_UUID),
            101,
        ),
        (
            "sha256:" + "0" * 64,
            _darwin_identity(),
            101,
        ),
    ],
)
def test_old_process_cross_boot_and_unrecognized_digest_fail_closed(
    digest: str,
    current_identity: KernelBootIdentity,
    current_monotonic_ns: int,
) -> None:
    assert not generation_follows_activation(
        activation_identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        activation_boot_digest=digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=current_identity,
        current_monotonic_ns=current_monotonic_ns,
    )


def test_legacy_darwin_never_falls_back_to_wall_clock_tolerance() -> None:
    almost_legacy = _stable_digest(
        "darwin",
        f"{int(BOOT_STARTED.timestamp())}:{BOOT_STARTED.microsecond + 1}",
    )
    assert not generation_follows_activation(
        activation_identity_scheme=LEGACY_DARWIN_BOOT_TIME_SCHEME,
        activation_boot_digest=almost_legacy,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=_darwin_identity(),
        current_monotonic_ns=101,
    )


@pytest.mark.parametrize(
    ("activation_monotonic_ns", "current_monotonic_ns"),
    [
        (-1, 101),
        (100, -1),
        (True, 101),
        (100, False),
        (100.0, 101),
        (100, "101"),
    ],
)
def test_invalid_monotonic_order_values_fail_closed(
    activation_monotonic_ns: object,
    current_monotonic_ns: object,
) -> None:
    identity = _darwin_identity()
    assert not generation_follows_activation(
        activation_identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        activation_boot_digest=identity.digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=activation_monotonic_ns,
        current_identity=identity,
        current_monotonic_ns=current_monotonic_ns,
    )


@pytest.mark.parametrize(
    "name",
    ["", "KERN.bootsessionuuid", "kern.boot session", "kern.boot\x00time"],
)
def test_darwin_sysctl_reader_rejects_invalid_names(name: str) -> None:
    reader = _darwin_reader()
    assert read_darwin_sysctl(name, sysctlbyname=reader) is None
    assert reader.calls == []


@pytest.mark.parametrize(
    "reader",
    [
        FakeSysctl({"kern.bootsessionuuid": b""}),
        FakeSysctl({"kern.bootsessionuuid": b"x" * 4097}),
        FakeSysctl(
            {"kern.bootsessionuuid": b"ok"},
            first_error={"kern.bootsessionuuid"},
        ),
        FakeSysctl(
            {"kern.bootsessionuuid": b"ok"},
            second_error={"kern.bootsessionuuid"},
        ),
        FakeSysctl(
            {"kern.bootsessionuuid": b"ok"},
            reported_growth={"kern.bootsessionuuid"},
        ),
        FakeSysctl(
            {"kern.bootsessionuuid": b"ok"},
            reported_shrink={"kern.bootsessionuuid"},
        ),
        FakeSysctl(
            {"kern.bootsessionuuid": b"ok"},
            raise_on={"kern.bootsessionuuid"},
        ),
    ],
)
def test_darwin_sysctl_reader_fails_closed_at_abi_boundaries(
    reader: FakeSysctl,
) -> None:
    assert (
        read_darwin_sysctl(
            "kern.bootsessionuuid",
            sysctlbyname=reader,
        )
        is None
    )


@pytest.mark.parametrize(
    "raw_uuid",
    [
        b"12345678-1234-4234-9234-123456789abc",
        b"12345678-1234-4234-9234-123456789abc\n",
        b"{12345678-1234-4234-9234-123456789abc}\x00",
        b"00000000-0000-0000-0000-000000000000\x00",
        b"12345678-1234-4234-9234-123456789abc\x00junk",
    ],
)
def test_darwin_bootsessionuuid_parser_is_strict(raw_uuid: bytes) -> None:
    reader = FakeSysctl(
        {
            "kern.bootsessionuuid": raw_uuid,
            "kern.boottime": _darwin_boottime(
                int(BOOT_STARTED.timestamp()),
                BOOT_STARTED.microsecond,
            ),
        }
    )
    assert read_darwin_boot_identity(sysctlbyname=reader) is None


@pytest.mark.parametrize(
    "raw_boottime",
    [
        _darwin_boottime(int(BOOT_STARTED.timestamp()), 1_000_000),
        _darwin_boottime(-1, 0),
        b"\x00" * 15,
        b"\x00" * 17,
    ],
)
def test_invalid_darwin_boottime_is_not_used_as_identity_or_time_evidence(
    raw_boottime: bytes,
) -> None:
    reader = FakeSysctl(
        {
            "kern.bootsessionuuid": BOOT_UUID.encode("ascii") + b"\x00",
            "kern.boottime": raw_boottime,
        }
    )

    identity = read_darwin_boot_identity(sysctlbyname=reader)

    assert identity is not None
    assert identity.started_at is None
    assert identity.boot_epoch_second is None
    assert identity.digest == _stable_digest(
        "darwin-bootsessionuuid",
        BOOT_UUID,
    )


def test_darwin_boottime_fixture_locks_the_64_bit_timeval_abi() -> None:
    payload = _darwin_boottime(0x0102_0304_0506_0708, 0x1112_1314)
    assert len(payload) == 16
    assert payload[:8] == bytes.fromhex("0807060504030201")
    assert payload[8:12] == bytes.fromhex("14131211")
    assert payload[12:] == b"\x00" * 4


def test_linux_boot_identity_and_successor_comparison_do_not_regress(
    tmp_path: Path,
) -> None:
    boot_id = tmp_path / "boot_id"
    proc_stat = tmp_path / "stat"
    boot_id.write_text(BOOT_UUID + "\n", encoding="ascii")
    proc_stat.write_text(
        f"cpu 1 2 3 4\nbtime {int(BOOT_STARTED.timestamp())}\n",
        encoding="ascii",
    )

    identity = read_current_kernel_boot_identity(
        platform_name="linux",
        linux_boot_id_path=boot_id,
        linux_proc_stat_path=proc_stat,
    )

    assert identity == KernelBootIdentity(
        scheme=LINUX_BOOT_ID_SCHEME,
        digest=_stable_digest("linux", BOOT_UUID),
        started_at=BOOT_STARTED.replace(microsecond=0),
        boot_epoch_second=int(BOOT_STARTED.timestamp()),
    )
    assert generation_follows_activation(
        activation_identity_scheme=LINUX_BOOT_ID_SCHEME,
        activation_boot_digest=identity.digest,
        activation_boot_started_at=BOOT_STARTED.replace(microsecond=0),
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=identity,
        current_monotonic_ns=101,
    )
    assert not generation_follows_activation(
        activation_identity_scheme=LINUX_BOOT_ID_SCHEME,
        activation_boot_digest=identity.digest,
        activation_boot_started_at=BOOT_STARTED.replace(microsecond=0),
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=101,
        current_identity=identity,
        current_monotonic_ns=100,
    )


def test_linux_bad_boot_time_evidence_preserves_the_stable_identity(
    tmp_path: Path,
) -> None:
    boot_id = tmp_path / "boot_id"
    proc_stat = tmp_path / "stat"
    boot_id.write_text(BOOT_UUID + "\n", encoding="ascii")
    proc_stat.write_text("btime invalid\n", encoding="ascii")

    identity = read_current_kernel_boot_identity(
        platform_name="linux",
        linux_boot_id_path=boot_id,
        linux_proc_stat_path=proc_stat,
    )

    assert identity == KernelBootIdentity(
        scheme=LINUX_BOOT_ID_SCHEME,
        digest=_stable_digest("linux", BOOT_UUID),
        started_at=None,
        boot_epoch_second=None,
    )


@pytest.mark.parametrize(
    ("scheme", "activation_digest", "current_identity"),
    [
        (
            DARWIN_BOOT_SESSION_SCHEME,
            _stable_digest("darwin-bootsessionuuid", BOOT_UUID),
            _darwin_identity(
                boot_uuid=OTHER_BOOT_UUID,
                epoch_second=int(ACTIVATED_AT.timestamp()) + 1,
            ),
        ),
        (
            LINUX_BOOT_ID_SCHEME,
            _stable_digest("linux", BOOT_UUID),
            KernelBootIdentity(
                scheme=LINUX_BOOT_ID_SCHEME,
                digest=_stable_digest("linux", OTHER_BOOT_UUID),
                started_at=datetime.fromtimestamp(
                    int(ACTIVATED_AT.timestamp()) + 1,
                    tz=timezone.utc,
                ),
                boot_epoch_second=int(ACTIVATED_AT.timestamp()) + 1,
            ),
        ),
    ],
)
def test_different_stable_identity_accepts_a_proven_later_boot(
    scheme: str,
    activation_digest: str,
    current_identity: KernelBootIdentity,
) -> None:
    proof = prove_generation_follows_activation(
        activation_identity_scheme=scheme,
        activation_boot_digest=activation_digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=current_identity,
        current_monotonic_ns=1,
    )

    assert proof is not None
    assert proof.identity_match == "later-stable-kernel-boot"
    assert proof.order_basis == "later-stable-kernel-boot"


def test_different_stable_identity_without_a_later_boot_fails_closed() -> None:
    assert not generation_follows_activation(
        activation_identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        activation_boot_digest=_stable_digest(
            "darwin-bootsessionuuid",
            BOOT_UUID,
        ),
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=_darwin_identity(boot_uuid=OTHER_BOOT_UUID),
        current_monotonic_ns=101,
    )


@pytest.mark.parametrize(
    ("activation_scheme", "current_identity"),
    [
        (LINUX_BOOT_ID_SCHEME, _darwin_identity()),
        (
            DARWIN_BOOT_SESSION_SCHEME,
            KernelBootIdentity(
                scheme=LINUX_BOOT_ID_SCHEME,
                digest=_stable_digest("linux", BOOT_UUID),
                started_at=BOOT_STARTED.replace(microsecond=0),
                boot_epoch_second=int(BOOT_STARTED.timestamp()),
            ),
        ),
        ("unknown-v1", _darwin_identity()),
    ],
)
def test_stable_scheme_mismatch_fails_closed(
    activation_scheme: str,
    current_identity: KernelBootIdentity,
) -> None:
    assert not generation_follows_activation(
        activation_identity_scheme=activation_scheme,
        activation_boot_digest=current_identity.digest,
        activation_boot_started_at=BOOT_STARTED,
        activated_at=ACTIVATED_AT,
        activation_monotonic_ns=100,
        current_identity=current_identity,
        current_monotonic_ns=101,
    )
