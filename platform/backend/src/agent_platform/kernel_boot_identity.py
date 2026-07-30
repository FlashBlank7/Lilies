from __future__ import annotations

import ctypes
import hashlib
import hmac
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID


LINUX_BOOT_ID_SCHEME = "linux-boot-id-v1"
DARWIN_BOOT_SESSION_SCHEME = "darwin-bootsessionuuid-v1"
LEGACY_DARWIN_BOOT_TIME_SCHEME = "darwin-kern-boottime-v1"
PROCESS_MONOTONIC_ORDER_BASIS = "process-start-monotonic-ns"
LATER_STABLE_BOOT_ORDER_BASIS = "later-stable-kernel-boot"

_DARWIN_SYSCTL_MAX_BYTES = 4096
_DARWIN_SYSCTL_NAME = re.compile(r"[a-z][a-z0-9_.]{0,127}")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CANONICAL_UUID = re.compile(
    rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    rb"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

DarwinSysctlByName = Callable[
    [bytes, object | None, object, object | None, int],
    int,
]


class _DarwinTimeval(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_int),
    ]


@dataclass(frozen=True, slots=True)
class KernelBootIdentity:
    """A non-secret, kernel-scoped boot identity and optional time evidence."""

    scheme: Literal["linux-boot-id-v1", "darwin-bootsessionuuid-v1"]
    digest: str
    started_at: datetime | None
    boot_epoch_second: int | None


@dataclass(frozen=True, slots=True)
class GenerationSuccessorProof:
    """Internal proof details; callers need not persist these in legacy records."""

    identity_scheme: Literal[
        "linux-boot-id-v1",
        "darwin-bootsessionuuid-v1",
    ]
    identity_match: Literal[
        "stable-kernel-identity",
        "later-stable-kernel-boot",
        "legacy-darwin-boottime",
    ]
    order_basis: Literal[
        "process-start-monotonic-ns",
        "later-stable-kernel-boot",
    ]


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_digest(value: object) -> str | None:
    if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
        return None
    return value


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        return None
    if offset is None:
        return None
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _epoch_second(value: datetime) -> int | None:
    normalized = _aware_utc(value)
    if normalized is None or normalized < _UTC_EPOCH:
        return None
    delta = normalized - _UTC_EPOCH
    return (delta.days * 86_400) + delta.seconds


def _strict_monotonic_ns(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _resolve_darwin_sysctlbyname() -> DarwinSysctlByName | None:
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.sysctlbyname
        function.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        function.restype = ctypes.c_int
    except (AttributeError, OSError, TypeError):
        return None
    return function


def read_darwin_sysctl(
    name: str,
    *,
    sysctlbyname: DarwinSysctlByName | None = None,
    max_bytes: int = _DARWIN_SYSCTL_MAX_BYTES,
) -> bytes | None:
    """Read one Darwin sysctl with a strict two-call, bounded ABI contract.

    The function deliberately does not retry a size change between calls.
    A failed query, an invalid name, a zero/oversized result, a short-lived
    ABI race, or an unavailable Darwin libc surface all fail closed.
    """

    if (
        not isinstance(name, str)
        or _DARWIN_SYSCTL_NAME.fullmatch(name) is None
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= _DARWIN_SYSCTL_MAX_BYTES
    ):
        return None
    function = sysctlbyname or _resolve_darwin_sysctlbyname()
    if function is None:
        return None
    encoded_name = name.encode("ascii")
    required = ctypes.c_size_t(0)
    try:
        if function(encoded_name, None, ctypes.byref(required), None, 0) != 0:
            return None
        if not 1 <= required.value <= max_bytes:
            return None
        capacity = required.value
        buffer = (ctypes.c_ubyte * capacity)()
        returned = ctypes.c_size_t(capacity)
        if (
            function(
                encoded_name,
                ctypes.cast(buffer, ctypes.c_void_p),
                ctypes.byref(returned),
                None,
                0,
            )
            != 0
        ):
            return None
        if returned.value != capacity:
            return None
        return bytes(buffer[: returned.value])
    except (
        AttributeError,
        ctypes.ArgumentError,
        MemoryError,
        OSError,
        OverflowError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return None


def _parse_uuid_bytes(
    raw: bytes,
    *,
    require_trailing_nul: bool,
    permit_trailing_newline: bool,
) -> str | None:
    if not isinstance(raw, bytes):
        return None
    payload = raw
    if require_trailing_nul:
        if not payload.endswith(b"\x00"):
            return None
        payload = payload[:-1]
    elif permit_trailing_newline and payload.endswith(b"\n"):
        payload = payload[:-1]
    if (
        not payload
        or b"\x00" in payload
        or b"\r" in payload
        or b"\n" in payload
        or _CANONICAL_UUID.fullmatch(payload) is None
    ):
        return None
    try:
        value = UUID(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if value.int == 0:
        return None
    return str(value)


def _parse_darwin_boottime(raw: bytes) -> tuple[int, int] | None:
    if not isinstance(raw, bytes) or len(raw) != ctypes.sizeof(_DarwinTimeval):
        return None
    try:
        value = _DarwinTimeval.from_buffer_copy(raw)
    except (TypeError, ValueError):
        return None
    epoch_second = int(value.tv_sec)
    microsecond = int(value.tv_usec)
    if epoch_second < 0 or not 0 <= microsecond <= 999_999:
        return None
    return epoch_second, microsecond


def _datetime_from_epoch_second(epoch_second: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(epoch_second, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def read_darwin_boot_identity(
    *,
    sysctlbyname: DarwinSysctlByName | None = None,
) -> KernelBootIdentity | None:
    """Return the stable Darwin boot-session identity.

    ``kern.boottime`` is collected only as time evidence. Its microseconds are
    intentionally excluded from the stable identity and ``started_at`` is
    normalized to whole seconds.
    """

    raw_session = read_darwin_sysctl(
        "kern.bootsessionuuid",
        sysctlbyname=sysctlbyname,
    )
    if raw_session is None:
        return None
    session_uuid = _parse_uuid_bytes(
        raw_session,
        require_trailing_nul=True,
        permit_trailing_newline=False,
    )
    if session_uuid is None:
        return None

    epoch_second: int | None = None
    started_at: datetime | None = None
    raw_boottime = read_darwin_sysctl(
        "kern.boottime",
        sysctlbyname=sysctlbyname,
    )
    if raw_boottime is not None:
        parsed_boottime = _parse_darwin_boottime(raw_boottime)
        if parsed_boottime is not None:
            candidate_second, _microsecond = parsed_boottime
            candidate_started_at = _datetime_from_epoch_second(candidate_second)
            if candidate_started_at is not None:
                epoch_second = candidate_second
                started_at = candidate_started_at

    return KernelBootIdentity(
        scheme=DARWIN_BOOT_SESSION_SCHEME,
        digest=_sha256_text(f"darwin-bootsessionuuid:{session_uuid}"),
        started_at=started_at,
        boot_epoch_second=epoch_second,
    )


def _read_bounded_file(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except (OSError, ValueError):
        return None
    if not payload or len(payload) > max_bytes:
        return None
    return payload


def _read_linux_boot_started_at(path: Path) -> tuple[datetime, int] | None:
    payload = _read_bounded_file(path, max_bytes=1024 * 1024)
    if payload is None:
        return None
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        return None
    matches = re.findall(r"(?m)^btime ([0-9]+)$", text)
    if len(matches) != 1:
        return None
    try:
        epoch_second = int(matches[0])
    except ValueError:
        return None
    started_at = _datetime_from_epoch_second(epoch_second)
    if started_at is None:
        return None
    return started_at, epoch_second


def read_linux_boot_identity(
    *,
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
    proc_stat_path: Path = Path("/proc/stat"),
) -> KernelBootIdentity | None:
    """Read Linux's stable boot ID without changing its namespaced digest."""

    raw_boot_id = _read_bounded_file(boot_id_path, max_bytes=37)
    if raw_boot_id is None:
        return None
    boot_id = _parse_uuid_bytes(
        raw_boot_id,
        require_trailing_nul=False,
        permit_trailing_newline=True,
    )
    if boot_id is None:
        return None
    started_at: datetime | None = None
    epoch_second: int | None = None
    started_evidence = _read_linux_boot_started_at(proc_stat_path)
    if started_evidence is not None:
        started_at, epoch_second = started_evidence
    return KernelBootIdentity(
        scheme=LINUX_BOOT_ID_SCHEME,
        digest=_sha256_text(f"linux:{boot_id}"),
        started_at=started_at,
        boot_epoch_second=epoch_second,
    )


def read_current_kernel_boot_identity(
    *,
    platform_name: str | None = None,
    sysctlbyname: DarwinSysctlByName | None = None,
    linux_boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
    linux_proc_stat_path: Path = Path("/proc/stat"),
) -> KernelBootIdentity | None:
    """Read a supported kernel identity, returning ``None`` on uncertainty."""

    selected_platform = sys.platform if platform_name is None else platform_name
    if selected_platform == "darwin":
        return read_darwin_boot_identity(sysctlbyname=sysctlbyname)
    if selected_platform.startswith("linux"):
        return read_linux_boot_identity(
            boot_id_path=linux_boot_id_path,
            proc_stat_path=linux_proc_stat_path,
        )
    return None


def legacy_darwin_boot_digest(started_at: datetime) -> str | None:
    """Reconstruct the exact immutable v1 Darwin ``kern.boottime`` fence."""

    normalized = _aware_utc(started_at)
    if normalized is None:
        return None
    epoch_second = _epoch_second(normalized)
    if epoch_second is None:
        return None
    return _sha256_text(
        f"darwin:{epoch_second}:{normalized.microsecond}"
    )


def _identity_shape_is_valid(identity: KernelBootIdentity) -> bool:
    if identity.scheme not in {
        LINUX_BOOT_ID_SCHEME,
        DARWIN_BOOT_SESSION_SCHEME,
    }:
        return False
    if _canonical_digest(identity.digest) is None:
        return False
    if identity.boot_epoch_second is not None and (
        isinstance(identity.boot_epoch_second, bool)
        or not isinstance(identity.boot_epoch_second, int)
        or identity.boot_epoch_second < 0
    ):
        return False
    if identity.started_at is None:
        return identity.boot_epoch_second is None
    started_at = _aware_utc(identity.started_at)
    if started_at is None or started_at.microsecond != 0:
        return False
    if identity.boot_epoch_second is None:
        return False
    return _epoch_second(started_at) == identity.boot_epoch_second


def prove_generation_follows_activation(
    *,
    activation_identity_scheme: str,
    activation_boot_digest: str,
    activation_boot_started_at: datetime,
    activated_at: datetime,
    activation_monotonic_ns: int,
    current_identity: KernelBootIdentity | None,
    current_monotonic_ns: int,
) -> GenerationSuccessorProof | None:
    """Prove that a process generation strictly follows an activation.

    Stable boot identity equality requires a strictly newer monotonic process
    generation. A different identity with the same explicit stable scheme is
    accepted only when its boot time is strictly later than activation. A
    frozen Darwin v1 fence is accepted only when its digest is reconstructed
    exactly from its own timestamp and the current Darwin ``kern.boottime``
    epoch second is identical. There is no generic boot-time tolerance or
    wall-clock successor fallback for a legacy fence.
    """

    activation_digest = _canonical_digest(activation_boot_digest)
    activation_started = _aware_utc(activation_boot_started_at)
    activation_time = _aware_utc(activated_at)
    activation_generation = _strict_monotonic_ns(activation_monotonic_ns)
    current_generation = _strict_monotonic_ns(current_monotonic_ns)
    if (
        activation_identity_scheme
        not in {
            LINUX_BOOT_ID_SCHEME,
            DARWIN_BOOT_SESSION_SCHEME,
            LEGACY_DARWIN_BOOT_TIME_SCHEME,
        }
        or activation_digest is None
        or activation_started is None
        or activation_time is None
        or activation_generation is None
        or current_generation is None
        or current_identity is None
        or not _identity_shape_is_valid(current_identity)
        or activation_started > activation_time
    ):
        return None

    if activation_identity_scheme in {
        LINUX_BOOT_ID_SCHEME,
        DARWIN_BOOT_SESSION_SCHEME,
    }:
        if activation_identity_scheme != current_identity.scheme:
            return None
        if hmac.compare_digest(activation_digest, current_identity.digest):
            if current_generation <= activation_generation:
                return None
            return GenerationSuccessorProof(
                identity_scheme=current_identity.scheme,
                identity_match="stable-kernel-identity",
                order_basis=PROCESS_MONOTONIC_ORDER_BASIS,
            )
        current_started = _aware_utc(current_identity.started_at)
        if current_started is None or current_started <= activation_time:
            return None
        return GenerationSuccessorProof(
            identity_scheme=current_identity.scheme,
            identity_match="later-stable-kernel-boot",
            order_basis=LATER_STABLE_BOOT_ORDER_BASIS,
        )

    if (
        activation_identity_scheme != LEGACY_DARWIN_BOOT_TIME_SCHEME
        or current_identity.scheme != DARWIN_BOOT_SESSION_SCHEME
        or current_generation <= activation_generation
    ):
        return None
    reconstructed_legacy = legacy_darwin_boot_digest(activation_started)
    if (
        reconstructed_legacy is None
        or not hmac.compare_digest(activation_digest, reconstructed_legacy)
        or current_identity.boot_epoch_second is None
    ):
        return None
    legacy_epoch_second = _epoch_second(activation_started)
    if legacy_epoch_second is None:
        return None
    if current_identity.boot_epoch_second != legacy_epoch_second:
        return None
    return GenerationSuccessorProof(
        identity_scheme=DARWIN_BOOT_SESSION_SCHEME,
        identity_match="legacy-darwin-boottime",
        order_basis=PROCESS_MONOTONIC_ORDER_BASIS,
    )


def generation_follows_activation(
    *,
    activation_identity_scheme: str,
    activation_boot_digest: str,
    activation_boot_started_at: datetime,
    activated_at: datetime,
    activation_monotonic_ns: int,
    current_identity: KernelBootIdentity | None,
    current_monotonic_ns: int,
) -> bool:
    """Boolean wrapper for trust-root call sites."""

    return (
        prove_generation_follows_activation(
            activation_identity_scheme=activation_identity_scheme,
            activation_boot_digest=activation_boot_digest,
            activation_boot_started_at=activation_boot_started_at,
            activated_at=activated_at,
            activation_monotonic_ns=activation_monotonic_ns,
            current_identity=current_identity,
            current_monotonic_ns=current_monotonic_ns,
        )
        is not None
    )
