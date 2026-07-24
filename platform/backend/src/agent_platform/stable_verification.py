from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

from .collaboration_models import (
    VerificationClaim,
    VerificationResultPayload,
    VerificationVerdict,
)
from .lilies_models import Digest, OpaqueReference
from .task_packages import ArchiveStatus, TaskPackageManager, ValidationMode
from .task_packages import EnvironmentReady


class StableVerificationError(RuntimeError):
    """Stable hidden-seed qualification could not be recorded or replayed."""


class StableVerificationRejected(StableVerificationError):
    """One result could not contribute to a stable qualification streak."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


def _canonical_json(value: Any) -> bytes:
    value = to_jsonable_python(
        value,
        exclude_none=True,
    )
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_json_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _task_key(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _read_regular(path: Path, *, expected_mode: int, limit: int = 16 * 1024 * 1024) -> bytes:
    if path.is_symlink():
        raise StableVerificationRejected("stable evidence cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StableVerificationRejected("stable evidence is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
        ):
            raise StableVerificationRejected(
                "stable evidence has an unsafe file boundary"
            )
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise StableVerificationRejected("stable evidence exceeds its limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise StableVerificationRejected(
                "stable evidence changed while being read"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_atomic_read_only(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_new_read_only(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o400)
    except FileExistsError:
        raise
    except OSError as error:
        raise StableVerificationRejected(
            "protected evidence could not be created safely"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


class HiddenSeedEvidence(_FrozenModel):
    """Platform-owned seed identity binding; never projected into Lilies workspaces."""

    schema_version: Literal["1.0"] = "1.0"
    evidence_id: OpaqueReference
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    archive_manifest_digest: Digest
    environment_ready_digest: Digest
    environment_instance_id: OpaqueReference
    attestation_binding_digest: Digest
    sealed_package_digest: Digest
    verification_process_digest: Digest
    hidden_seed_digest: Digest
    issued_at: datetime
    evidence_digest: Digest

    @field_validator("issued_at")
    @classmethod
    def issued_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def evidence_digest_matches(self) -> HiddenSeedEvidence:
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"evidence_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.evidence_digest):
            raise ValueError("hidden seed evidence digest changed")
        return self


class PlatformHiddenSeedContext(_FrozenModel):
    """Verified environment identity supplied only to a platform secret resolver."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    archive_manifest_digest: Digest
    environment_ready_digest: Digest
    environment_instance_id: OpaqueReference
    environment_lock_digest: Digest
    sealed_package_digest: Digest
    verification_process_digest: Digest
    attestation_binding_digest: Digest


class StableVerificationEntry(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    claim_id: UUID
    assignment_id: UUID
    run_id: OpaqueReference
    archive_manifest_digest: Digest
    frozen_context_digest: Digest
    verification_id: UUID
    verification_result_digest: Digest
    hidden_seed_evidence_digest: Digest
    hidden_seed_digest: Digest
    accepted_at: datetime
    entry_digest: Digest

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def entry_digest_matches(self) -> StableVerificationEntry:
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"entry_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.entry_digest):
            raise ValueError("stable verification entry digest changed")
        return self


class StableVerificationEvent(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    sequence: int = Field(ge=1)
    outcome: Literal["accepted", "reset_failed", "reset_rejected"]
    reason: Literal[
        "independent_verification_passed",
        "independent_verification_failed",
        "duplicate_hidden_seed",
        "duplicate_assignment",
        "duplicate_archive",
        "duplicate_claim",
        "package_or_verifier_drift",
        "protected_seed_binding_mismatch",
        "claim_or_result_binding_mismatch",
    ]
    entry: StableVerificationEntry | None = None
    consecutive_passes_after: int | None = Field(default=None, ge=1, le=100)
    stable_verdict_digest_after: Digest | None = None
    recorded_at: datetime
    event_digest: Digest

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def event_is_coherent(self) -> StableVerificationEvent:
        accepted = self.outcome == "accepted"
        if accepted != (self.entry is not None):
            raise ValueError("only accepted stability events carry an entry")
        if accepted != (self.consecutive_passes_after is not None):
            raise ValueError("accepted stability events bind their resulting progress")
        if not accepted and self.stable_verdict_digest_after is not None:
            raise ValueError("reset events cannot preserve a stable verdict")
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"event_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.event_digest):
            raise ValueError("stable verification event digest changed")
        return self


class StableVerificationLedger(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    public_summary_digest: Digest
    sealed_package_digest: Digest
    budget_digest: Digest
    verification_process_digest: Digest
    stable_hidden_runs: int = Field(ge=1, le=100)
    events: list[StableVerificationEvent] = Field(default_factory=list, max_length=100_000)
    current_streak: list[StableVerificationEntry] = Field(
        default_factory=list,
        max_length=100,
    )
    latest_stable_verdict_digest: Digest | None = None
    ledger_digest: Digest

    @model_validator(mode="after")
    def ledger_is_replayable(self) -> StableVerificationLedger:
        if [event.sequence for event in self.events] != list(
            range(1, len(self.events) + 1)
        ):
            raise ValueError("stable verification event sequence changed")
        accepted = [
            event.entry
            for event in self.events
            if event.outcome == "accepted" and event.entry is not None
        ]
        reset_index = max(
            (
                index
                for index, event in enumerate(self.events)
                if event.outcome != "accepted"
            ),
            default=-1,
        )
        expected_streak = [
            event.entry
            for event in self.events[reset_index + 1 :]
            if event.entry is not None
        ][-self.stable_hidden_runs :]
        if [item.entry_digest for item in self.current_streak] != [
            item.entry_digest for item in expected_streak
        ]:
            raise ValueError("stable verification streak is not replayable")
        if any(item is None for item in accepted):
            raise ValueError("accepted stability event is incomplete")
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"ledger_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.ledger_digest):
            raise ValueError("stable verification ledger digest changed")
        return self


class StableVerificationVerdict(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    verdict: Literal["stably_independently_verified"]
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    public_summary_digest: Digest
    sealed_package_digest: Digest
    budget_digest: Digest
    verification_process_digest: Digest
    stable_hidden_runs: int = Field(ge=1, le=100)
    entry_digests: list[Digest] = Field(min_length=1, max_length=100)
    qualification_digest: Digest
    created_at: datetime
    verdict_digest: Digest

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def verdict_digest_matches(self) -> StableVerificationVerdict:
        if len(self.entry_digests) != self.stable_hidden_runs:
            raise ValueError("stable verdict does not contain its frozen threshold")
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"verdict_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.verdict_digest):
            raise ValueError("stable verification verdict digest changed")
        return self


class StableVerificationProgress(_FrozenModel):
    stable_hidden_runs: int = Field(ge=1, le=100)
    consecutive_passes: int = Field(ge=0, le=100)
    progress_digest: Digest
    stable_verdict: StableVerificationVerdict | None = None


class StableVerificationManager:
    """Consume isolated results into a protected, replayable N-seed qualification."""

    def __init__(
        self,
        state_root: Path,
        *,
        trusted_result_resolver: (
            Callable[[str, int, VerificationClaim], VerificationResultPayload]
            | None
        ) = None,
        platform_seed_key_resolver: (
            Callable[[PlatformHiddenSeedContext], bytes] | None
        ) = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.package_manager = TaskPackageManager(self.state_root, read_only=True)
        self.trusted_result_resolver = trusted_result_resolver
        self.platform_seed_key_resolver = platform_seed_key_resolver

    def _root(self, task_id: str, revision: int) -> Path:
        return (
            self.state_root
            / "stable-verification"
            / _task_key(task_id)
            / str(revision)
        )

    def _seed_root(self, task_id: str, revision: int) -> Path:
        return (
            self.state_root
            / "protected-hidden-seeds"
            / _task_key(task_id)
            / str(revision)
        )

    def _ledger_path(self, task_id: str, revision: int) -> Path:
        return self._root(task_id, revision) / "ledger.json"

    def _lock_path(self, task_id: str, revision: int) -> Path:
        return self._root(task_id, revision) / ".ledger.lock"

    def _verdict_path(
        self,
        task_id: str,
        revision: int,
        verdict_digest: str,
    ) -> Path:
        return (
            self._root(task_id, revision)
            / "verdicts"
            / f"{verdict_digest.removeprefix('sha256:')}.json"
        )

    def _ensure_private_directory(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.state_root)
        except ValueError as error:
            raise StableVerificationRejected(
                "stable verification storage escaped platform state"
            ) from error
        current = self.state_root
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                metadata = current.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise StableVerificationRejected(
                    "stable verification storage has an unsafe directory boundary"
                )
            os.chmod(current, 0o700)

    def _hidden_seed_context(
        self,
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
    ) -> tuple[PlatformHiddenSeedContext, str]:
        package = self.package_manager.load_frozen(task_id, revision)
        archive_root, manifest = self.package_manager.find_archive_by_digest(
            task_id,
            revision,
            str(claim.archive_manifest_digest),
        )
        if (
            manifest.status is not ArchiveStatus.succeeded
            or manifest.validation_mode is not ValidationMode.real_host
            or manifest.claim_binding is None
            or manifest.environment_ready_digest is None
            or manifest.claim_binding.claim_id != claim.claim_id
            or manifest.claim_binding.assignment_id != claim.assignment_id
        ):
            raise StableVerificationRejected(
                "hidden seed evidence requires a successful real-host archive"
            )
        ready_entry = next(
            (
                entry
                for entry in manifest.files
                if entry.path == "environment-ready.json"
            ),
            None,
        )
        if ready_entry is None:
            raise StableVerificationRejected(
                "stable qualification requires archived environment readiness"
            )
        ready_payload = _read_regular(
            archive_root / "environment-ready.json",
            expected_mode=0o400,
        )
        if (
            len(ready_payload) != ready_entry.size_bytes
            or not hmac.compare_digest(_digest(ready_payload), ready_entry.digest)
            or not hmac.compare_digest(
                _digest(ready_payload),
                manifest.environment_ready_digest,
            )
            or not hmac.compare_digest(
                _digest(ready_payload),
                str(claim.environment_ready_digest),
            )
        ):
            raise StableVerificationRejected(
                "archived environment readiness binding changed"
            )
        try:
            ready = EnvironmentReady.model_validate_json(ready_payload)
        except ValueError as error:
            raise StableVerificationRejected(
                "archived environment readiness is invalid"
            ) from error
        identity_checks = [
            {
                "check_id": check.check_id,
                "kind": check.kind,
                "evidence_digest": check.evidence_digest,
                "attestation_challenge_digest": (
                    check.attestation_challenge_digest
                ),
            }
            for check in ready.checks
            if check.identity_authenticated
        ]
        if (
            ready.task_id != task_id
            or ready.revision != revision
            or ready.run_id != manifest.run_id
            or ready.assignment_id != claim.assignment_id
            or ready.environment_lock_digest
            != package.record.environment_lock_digest
            or ready.sealed_package_digest
            != package.record.sealed_package_digest
            or ready.provenance != "real_host"
            or not ready.ready
            or not identity_checks
        ):
            raise StableVerificationRejected(
                "environment readiness does not prove this archived run"
            )
        attestation_binding_digest = _digest(
            _canonical_json(
                {
                    "schema_version": "1.0",
                    "task_id": task_id,
                    "revision": revision,
                    "run_id": manifest.run_id,
                    "assignment_id": str(claim.assignment_id),
                    "environment_instance_id": ready.environment_instance_id,
                    "environment_ready_digest": manifest.environment_ready_digest,
                    "identity_checks": identity_checks,
                }
            )
        )
        return (
            PlatformHiddenSeedContext(
                task_id=task_id,
                revision=revision,
                run_id=manifest.run_id,
                assignment_id=claim.assignment_id,
                archive_manifest_digest=str(claim.archive_manifest_digest),
                environment_ready_digest=manifest.environment_ready_digest,
                environment_instance_id=ready.environment_instance_id,
                environment_lock_digest=package.record.environment_lock_digest,
                sealed_package_digest=package.record.sealed_package_digest,
                verification_process_digest=(
                    package.record.verification_process_digest
                ),
                attestation_binding_digest=attestation_binding_digest,
            ),
            manifest.run_id,
        )

    def resolve_hidden_seed_evidence(
        self,
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
    ) -> Path:
        """Resolve a hidden seed from platform-owned environment identity."""

        if self.platform_seed_key_resolver is None:
            raise StableVerificationRejected(
                "stable qualification requires a platform hidden-seed resolver"
            )
        context, run_id = self._hidden_seed_context(
            task_id=task_id,
            revision=revision,
            claim=claim,
        )
        try:
            platform_seed_key = self.platform_seed_key_resolver(context)
        except Exception as error:
            raise StableVerificationRejected(
                "platform hidden-seed resolver failed"
            ) from error
        if (
            not isinstance(platform_seed_key, bytes)
            or len(platform_seed_key) < 32
        ):
            raise StableVerificationRejected(
                "platform hidden-seed resolver returned an invalid key"
            )
        seed_identity_payload = _canonical_json(
            {
                "schema_version": "1.0",
                "task_id": context.task_id,
                "revision": context.revision,
                "sealed_package_digest": context.sealed_package_digest,
            }
        )
        seed_digest = (
            "sha256:"
            + hmac.new(
                platform_seed_key,
                b"lilies:hidden-environment-seed:v1\0"
                + seed_identity_payload,
                hashlib.sha256,
            ).hexdigest()
        )
        evidence_id = (
            "hidden-seed:"
            + hashlib.sha256(
                str(claim.archive_manifest_digest).encode("ascii")
            ).hexdigest()
        )
        target = (
            self._seed_root(task_id, revision)
            / f"{hashlib.sha256(evidence_id.encode('utf-8')).hexdigest()}.json"
        )
        self._ensure_private_directory(target.parent)
        if target.exists():
            existing_payload = _read_regular(target, expected_mode=0o400)
            existing = HiddenSeedEvidence.model_validate_json(existing_payload)
            if not hmac.compare_digest(
                existing_payload,
                _canonical_json(existing),
            ):
                raise StableVerificationRejected(
                    "hidden seed evidence bytes changed"
                )
            expected_bindings = (
                existing.evidence_id == evidence_id,
                existing.task_id == task_id,
                existing.revision == revision,
                existing.run_id == run_id,
                existing.assignment_id == claim.assignment_id,
                hmac.compare_digest(
                    existing.archive_manifest_digest,
                    context.archive_manifest_digest,
                ),
                hmac.compare_digest(
                    existing.environment_ready_digest,
                    context.environment_ready_digest,
                ),
                existing.environment_instance_id
                == context.environment_instance_id,
                hmac.compare_digest(
                    existing.attestation_binding_digest,
                    context.attestation_binding_digest,
                ),
                hmac.compare_digest(
                    existing.sealed_package_digest,
                    context.sealed_package_digest,
                ),
                hmac.compare_digest(
                    existing.verification_process_digest,
                    context.verification_process_digest,
                ),
                hmac.compare_digest(existing.hidden_seed_digest, seed_digest),
            )
            if not all(expected_bindings):
                raise StableVerificationRejected(
                    "hidden seed evidence identity was reused"
                )
            return target
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "evidence_id": evidence_id,
            "task_id": task_id,
            "revision": revision,
            "run_id": run_id,
            "assignment_id": str(claim.assignment_id),
            "archive_manifest_digest": context.archive_manifest_digest,
            "environment_ready_digest": context.environment_ready_digest,
            "environment_instance_id": context.environment_instance_id,
            "attestation_binding_digest": context.attestation_binding_digest,
            "sealed_package_digest": context.sealed_package_digest,
            "verification_process_digest": (
                context.verification_process_digest
            ),
            "hidden_seed_digest": seed_digest,
            "issued_at": _utc_json_now(),
        }
        payload["evidence_digest"] = _digest(_canonical_json(payload))
        evidence = HiddenSeedEvidence.model_validate(payload)
        try:
            _write_new_read_only(target, _canonical_json(evidence))
        except FileExistsError:
            return self.resolve_hidden_seed_evidence(
                task_id=task_id,
                revision=revision,
                claim=claim,
            )
        return target

    def load_hidden_seed_evidence_for_claim(
        self,
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
    ) -> HiddenSeedEvidence:
        """Load the redacted protected seed receipt without resolving its key."""

        self._hidden_seed_context(
            task_id=task_id,
            revision=revision,
            claim=claim,
        )
        evidence_id = (
            "hidden-seed:"
            + hashlib.sha256(
                str(claim.archive_manifest_digest).encode("ascii")
            ).hexdigest()
        )
        path = (
            self._seed_root(task_id, revision)
            / f"{hashlib.sha256(evidence_id.encode('utf-8')).hexdigest()}.json"
        )
        evidence = self._load_seed_evidence(task_id, revision, path)
        if (
            evidence.task_id != task_id
            or evidence.revision != revision
            or evidence.assignment_id != claim.assignment_id
            or not hmac.compare_digest(
                evidence.archive_manifest_digest,
                str(claim.archive_manifest_digest),
            )
        ):
            raise StableVerificationRejected(
                "protected hidden-seed receipt belongs to another claim"
            )
        return evidence

    def _new_ledger(self, task_id: str, revision: int) -> StableVerificationLedger:
        package = self.package_manager.load_frozen(task_id, revision)
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": task_id,
            "revision": revision,
            "public_summary_digest": package.record.public_summary_digest,
            "sealed_package_digest": package.record.sealed_package_digest,
            "budget_digest": package.record.budget_digest,
            "verification_process_digest": (
                package.record.verification_process_digest
            ),
            "stable_hidden_runs": package.budget.stable_hidden_runs,
            "events": [],
            "current_streak": [],
            "latest_stable_verdict_digest": None,
        }
        payload["ledger_digest"] = _digest(
            _canonical_json(
                {key: value for key, value in payload.items() if value is not None}
            )
        )
        return StableVerificationLedger.model_validate(payload)

    def _load_ledger(self, task_id: str, revision: int) -> StableVerificationLedger:
        path = self._ledger_path(task_id, revision)
        if not path.exists():
            return self._new_ledger(task_id, revision)
        try:
            payload = _read_regular(path, expected_mode=0o400)
            ledger = StableVerificationLedger.model_validate_json(payload)
            if not hmac.compare_digest(payload, _canonical_json(ledger)):
                raise StableVerificationRejected(
                    "stable verification ledger bytes changed"
                )
            return ledger
        except (ValueError, StableVerificationRejected) as error:
            raise StableVerificationRejected(
                "stable verification ledger is invalid"
            ) from error

    @staticmethod
    def _event(
        *,
        ledger: StableVerificationLedger,
        outcome: Literal["accepted", "reset_failed", "reset_rejected"],
        reason: Literal[
            "independent_verification_passed",
            "independent_verification_failed",
            "duplicate_hidden_seed",
            "duplicate_assignment",
            "duplicate_archive",
            "duplicate_claim",
            "package_or_verifier_drift",
            "protected_seed_binding_mismatch",
            "claim_or_result_binding_mismatch",
        ],
        entry: StableVerificationEntry | None = None,
        consecutive_passes_after: int | None = None,
        stable_verdict_digest_after: str | None = None,
    ) -> StableVerificationEvent:
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "sequence": len(ledger.events) + 1,
            "outcome": outcome,
            "reason": reason,
            "entry": (
                entry.model_dump(mode="json", exclude_none=True)
                if entry is not None
                else None
            ),
            "consecutive_passes_after": consecutive_passes_after,
            "stable_verdict_digest_after": stable_verdict_digest_after,
            "recorded_at": _utc_json_now(),
        }
        payload["event_digest"] = _digest(
            _canonical_json({key: value for key, value in payload.items() if value is not None})
        )
        return StableVerificationEvent.model_validate(payload)

    @staticmethod
    def _replace_ledger(
        ledger: StableVerificationLedger,
        *,
        events: list[StableVerificationEvent],
        current_streak: list[StableVerificationEntry],
        latest_stable_verdict_digest: str | None,
    ) -> StableVerificationLedger:
        payload = ledger.model_dump(
            mode="json",
            exclude={"events", "current_streak", "latest_stable_verdict_digest", "ledger_digest"},
        )
        payload.update(
            {
                "events": [
                    event.model_dump(mode="json", exclude_none=True)
                    for event in events
                ],
                "current_streak": [
                    entry.model_dump(mode="json")
                    for entry in current_streak
                ],
                "latest_stable_verdict_digest": latest_stable_verdict_digest,
            }
        )
        payload["ledger_digest"] = _digest(
            _canonical_json(
                {key: value for key, value in payload.items() if value is not None}
            )
        )
        return StableVerificationLedger.model_validate(payload)

    def _persist_ledger(self, ledger: StableVerificationLedger) -> None:
        _write_atomic_read_only(
            self._ledger_path(ledger.task_id, ledger.revision),
            _canonical_json(ledger),
        )

    def _reset(
        self,
        ledger: StableVerificationLedger,
        *,
        outcome: Literal["reset_failed", "reset_rejected"],
        reason: Literal[
            "independent_verification_failed",
            "duplicate_hidden_seed",
            "duplicate_assignment",
            "duplicate_archive",
            "duplicate_claim",
            "package_or_verifier_drift",
            "protected_seed_binding_mismatch",
            "claim_or_result_binding_mismatch",
        ],
    ) -> StableVerificationLedger:
        event = self._event(
            ledger=ledger,
            outcome=outcome,
            reason=reason,
        )
        updated = self._replace_ledger(
            ledger,
            events=[*ledger.events, event],
            current_streak=[],
            latest_stable_verdict_digest=None,
        )
        self._persist_ledger(updated)
        return updated

    def _validate_lineage(
        self,
        ledger: StableVerificationLedger,
    ) -> bool:
        package = self.package_manager.load_frozen(
            ledger.task_id,
            ledger.revision,
        )
        return all(
            (
                hmac.compare_digest(
                    ledger.public_summary_digest,
                    package.record.public_summary_digest,
                ),
                hmac.compare_digest(
                    ledger.sealed_package_digest,
                    package.record.sealed_package_digest,
                ),
                hmac.compare_digest(
                    ledger.budget_digest,
                    package.record.budget_digest,
                ),
                hmac.compare_digest(
                    ledger.verification_process_digest,
                    package.record.verification_process_digest,
                ),
                ledger.stable_hidden_runs == package.budget.stable_hidden_runs,
            )
        )

    def _load_seed_evidence(
        self,
        task_id: str,
        revision: int,
        path: Path,
    ) -> HiddenSeedEvidence:
        root = self._seed_root(task_id, revision).resolve()
        target = Path(path).resolve()
        if target.parent != root:
            raise StableVerificationRejected(
                "hidden seed evidence is outside platform-owned protected storage"
            )
        try:
            payload = _read_regular(target, expected_mode=0o400)
            evidence = HiddenSeedEvidence.model_validate_json(payload)
            if not hmac.compare_digest(payload, _canonical_json(evidence)):
                raise StableVerificationRejected(
                    "hidden seed evidence bytes changed"
                )
            return evidence
        except (ValueError, StableVerificationRejected) as error:
            raise StableVerificationRejected(
                "hidden seed evidence is invalid"
            ) from error

    @staticmethod
    def _result_matches_claim(
        claim: VerificationClaim,
        result: VerificationResultPayload,
    ) -> bool:
        return (
            claim.schema_version == "1.1"
            and result.schema_version == "1.1"
            and result.validation_mode == "real_host"
            and hmac.compare_digest(
                str(result.task_package_digest),
                str(claim.task_package_digest),
            )
            and hmac.compare_digest(
                str(result.environment_ready_digest),
                str(claim.environment_ready_digest),
            )
            and hmac.compare_digest(
                str(result.archive_manifest_digest),
                str(claim.archive_manifest_digest),
            )
            and hmac.compare_digest(
                str(result.frozen_context_digest),
                str(claim.frozen_context_digest),
            )
            and hmac.compare_digest(
                str(result.verification_process_digest),
                str(claim.verification_process_digest),
            )
        )

    @staticmethod
    def _qualification_digest(
        ledger: StableVerificationLedger,
        entries: list[StableVerificationEntry],
    ) -> str:
        return _digest(
            _canonical_json(
                {
                    "schema_version": "1.0",
                    "task_id": ledger.task_id,
                    "revision": ledger.revision,
                    "public_summary_digest": ledger.public_summary_digest,
                    "sealed_package_digest": ledger.sealed_package_digest,
                    "budget_digest": ledger.budget_digest,
                    "verification_process_digest": (
                        ledger.verification_process_digest
                    ),
                    "stable_hidden_runs": ledger.stable_hidden_runs,
                    "entries": [
                        item.model_dump(mode="json") for item in entries
                    ],
                }
            )
        )

    def record(
        self,
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
        result: VerificationResultPayload,
    ) -> StableVerificationProgress:
        """Record one independent result; only N clean hidden seeds emit stable."""

        if self.trusted_result_resolver is None:
            raise StableVerificationRejected(
                "stable qualification requires an isolated trusted result resolver"
            )
        root = self._root(task_id, revision)
        self._ensure_private_directory(root)
        lock_path = self._lock_path(task_id, revision)
        try:
            lock_descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as error:
            raise StableVerificationRejected(
                "stable verification lock has an unsafe file boundary"
            ) from error
        try:
            lock_stat = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise StableVerificationRejected(
                    "stable verification lock has an unsafe file boundary"
                )
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            ledger = self._load_ledger(task_id, revision)
            if not self._validate_lineage(ledger):
                ledger = self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason="package_or_verifier_drift",
                )
                raise StableVerificationRejected(
                    "package, budget, or verifier lineage changed"
                )
            try:
                trusted_result = self.trusted_result_resolver(
                    task_id,
                    revision,
                    claim,
                )
            except Exception as error:
                self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason="claim_or_result_binding_mismatch",
                )
                raise StableVerificationRejected(
                    "isolated trusted verification could not be resolved"
                ) from error
            if not hmac.compare_digest(
                _digest(_canonical_json(result)),
                _digest(_canonical_json(trusted_result)),
            ):
                self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason="claim_or_result_binding_mismatch",
                )
                raise StableVerificationRejected(
                    "supplied result does not match the isolated verifier receipt"
                )
            result = trusted_result
            if not self._result_matches_claim(claim, result):
                ledger = self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason="claim_or_result_binding_mismatch",
                )
                raise StableVerificationRejected(
                    "independent result does not match the frozen claim"
                )
            try:
                manifest = self.package_manager.validate_claim_binding(
                    task_id=task_id,
                    revision=revision,
                    claim=claim,
                )
            except Exception as error:
                self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason="claim_or_result_binding_mismatch",
                )
                raise StableVerificationRejected(
                    "claim is not bound to a claimable archive"
                ) from error
            hidden_seed_evidence_path = self.resolve_hidden_seed_evidence(
                task_id=task_id,
                revision=revision,
                claim=claim,
            )
            evidence = self._load_seed_evidence(
                task_id,
                revision,
                hidden_seed_evidence_path,
            )
            seed_context, _ = self._hidden_seed_context(
                task_id=task_id,
                revision=revision,
                claim=claim,
            )
            if (
                manifest.claim_binding is None
                or evidence.task_id != task_id
                or evidence.revision != revision
                or evidence.run_id != manifest.run_id
                or evidence.assignment_id != claim.assignment_id
                or not hmac.compare_digest(
                    evidence.archive_manifest_digest,
                    str(claim.archive_manifest_digest),
                )
                or not hmac.compare_digest(
                    evidence.environment_ready_digest,
                    seed_context.environment_ready_digest,
                )
                or evidence.environment_instance_id
                != seed_context.environment_instance_id
                or not hmac.compare_digest(
                    evidence.attestation_binding_digest,
                    seed_context.attestation_binding_digest,
                )
                or not hmac.compare_digest(
                    evidence.sealed_package_digest,
                    ledger.sealed_package_digest,
                )
                or not hmac.compare_digest(
                    evidence.verification_process_digest,
                    ledger.verification_process_digest,
                )
            ):
                self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason="protected_seed_binding_mismatch",
                )
                raise StableVerificationRejected(
                    "hidden seed evidence does not match the frozen run"
                )
            if result.verdict is VerificationVerdict.verification_failed:
                updated = self._reset(
                    ledger,
                    outcome="reset_failed",
                    reason="independent_verification_failed",
                )
                return StableVerificationProgress(
                    stable_hidden_runs=updated.stable_hidden_runs,
                    consecutive_passes=0,
                    progress_digest=updated.events[-1].event_digest,
                )
            if result.verdict is not VerificationVerdict.independently_verified:
                raise StableVerificationRejected(
                    "unsupported independent verification verdict"
                )
            result_digest = _digest(_canonical_json(result))
            entry_payload: dict[str, Any] = {
                "schema_version": "1.0",
                "claim_id": str(claim.claim_id),
                "assignment_id": str(claim.assignment_id),
                "run_id": manifest.run_id,
                "archive_manifest_digest": str(claim.archive_manifest_digest),
                "frozen_context_digest": str(claim.frozen_context_digest),
                "verification_id": str(result.verification_id),
                "verification_result_digest": result_digest,
                "hidden_seed_evidence_digest": evidence.evidence_digest,
                "hidden_seed_digest": evidence.hidden_seed_digest,
                "accepted_at": _utc_json_now(),
            }
            entry_payload["entry_digest"] = _digest(_canonical_json(entry_payload))
            entry = StableVerificationEntry.model_validate(entry_payload)
            matching_event = next(
                (
                    event
                    for event in ledger.events
                    if event.entry is not None
                    and event.entry.claim_id == entry.claim_id
                    and hmac.compare_digest(
                        event.entry.verification_result_digest,
                        entry.verification_result_digest,
                    )
                    and hmac.compare_digest(
                        event.entry.hidden_seed_evidence_digest,
                        entry.hidden_seed_evidence_digest,
                    )
                ),
                None,
            )
            if matching_event is not None:
                # An exact retry is a no-op, but its response must describe the
                # *current* ledger.  Returning the historical event receipt
                # here could resurrect a stable verdict that a later failed
                # hidden run already invalidated.
                stable_verdict = (
                    self.replay_current_stable_verdict(
                        task_id=task_id,
                        revision=revision,
                    )
                    if ledger.latest_stable_verdict_digest is not None
                    and len(ledger.current_streak)
                    == ledger.stable_hidden_runs
                    else None
                )
                return StableVerificationProgress(
                    stable_hidden_runs=ledger.stable_hidden_runs,
                    consecutive_passes=len(ledger.current_streak),
                    progress_digest=(
                        ledger.events[-1].event_digest
                        if ledger.events
                        else ledger.ledger_digest
                    ),
                    stable_verdict=stable_verdict,
                )
            duplicate_checks = (
                (
                    "duplicate_hidden_seed",
                    any(
                        hmac.compare_digest(
                            item.hidden_seed_digest,
                            entry.hidden_seed_digest,
                        )
                        for item in ledger.current_streak
                    ),
                ),
                (
                    "duplicate_assignment",
                    any(
                        item.assignment_id == entry.assignment_id
                        for item in ledger.current_streak
                    ),
                ),
                (
                    "duplicate_archive",
                    any(
                        hmac.compare_digest(
                            item.archive_manifest_digest,
                            entry.archive_manifest_digest,
                        )
                        for item in ledger.current_streak
                    ),
                ),
                (
                    "duplicate_claim",
                    any(
                        item.claim_id == entry.claim_id
                        for item in ledger.current_streak
                    ),
                ),
            )
            duplicate_reason = next(
                (reason for reason, duplicate in duplicate_checks if duplicate),
                None,
            )
            if duplicate_reason is not None:
                self._reset(
                    ledger,
                    outcome="reset_rejected",
                    reason=duplicate_reason,
                )
                raise StableVerificationRejected(
                    f"stable verification rejected {duplicate_reason}"
                )
            streak = [
                *ledger.current_streak,
                entry,
            ][-ledger.stable_hidden_runs :]
            verdict: StableVerificationVerdict | None = None
            if len(streak) == ledger.stable_hidden_runs:
                qualification_digest = self._qualification_digest(
                    ledger,
                    streak,
                )
                verdict_payload: dict[str, Any] = {
                    "schema_version": "1.0",
                    "verdict": "stably_independently_verified",
                    "task_id": task_id,
                    "revision": revision,
                    "public_summary_digest": ledger.public_summary_digest,
                    "sealed_package_digest": ledger.sealed_package_digest,
                    "budget_digest": ledger.budget_digest,
                    "verification_process_digest": (
                        ledger.verification_process_digest
                    ),
                    "stable_hidden_runs": ledger.stable_hidden_runs,
                    "entry_digests": [item.entry_digest for item in streak],
                    "qualification_digest": qualification_digest,
                    "created_at": _utc_json_now(),
                }
                verdict_payload["verdict_digest"] = _digest(
                    _canonical_json(verdict_payload)
                )
                verdict = StableVerificationVerdict.model_validate(
                    verdict_payload
                )
                verdict_path = self._verdict_path(
                    task_id,
                    revision,
                    verdict.verdict_digest,
                )
                self._ensure_private_directory(verdict_path.parent)
                if verdict_path.exists():
                    existing_payload = _read_regular(
                        verdict_path,
                        expected_mode=0o400,
                    )
                    existing = StableVerificationVerdict.model_validate_json(
                        existing_payload
                    )
                    if (
                        not hmac.compare_digest(
                            existing_payload,
                            _canonical_json(existing),
                        )
                        or existing.verdict_digest != verdict.verdict_digest
                    ):
                        raise StableVerificationRejected(
                            "stable verdict identity was reused"
                        )
                else:
                    _write_atomic_read_only(
                        verdict_path,
                        _canonical_json(verdict),
                    )
            event = self._event(
                ledger=ledger,
                outcome="accepted",
                reason="independent_verification_passed",
                entry=entry,
                consecutive_passes_after=len(streak),
                stable_verdict_digest_after=(
                    verdict.verdict_digest if verdict is not None else None
                ),
            )
            updated = self._replace_ledger(
                ledger,
                events=[*ledger.events, event],
                current_streak=streak,
                latest_stable_verdict_digest=(
                    verdict.verdict_digest if verdict is not None else None
                ),
            )
            self._persist_ledger(updated)
            return StableVerificationProgress(
                stable_hidden_runs=updated.stable_hidden_runs,
                consecutive_passes=len(streak),
                progress_digest=event.event_digest,
                stable_verdict=verdict,
            )
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def replay_current_stable_verdict(
        self,
        *,
        task_id: str,
        revision: int,
    ) -> StableVerificationVerdict:
        ledger = self._load_ledger(task_id, revision)
        if not self._validate_lineage(ledger):
            raise StableVerificationRejected(
                "stable verdict package, budget, or verifier lineage changed"
            )
        if (
            ledger.latest_stable_verdict_digest is None
            or len(ledger.current_streak) != ledger.stable_hidden_runs
        ):
            raise StableVerificationRejected(
                "there is no current stable hidden-seed verdict"
            )
        verdict_payload = _read_regular(
            self._verdict_path(
                task_id,
                revision,
                ledger.latest_stable_verdict_digest,
            ),
            expected_mode=0o400,
        )
        verdict = StableVerificationVerdict.model_validate_json(verdict_payload)
        if (
            not hmac.compare_digest(verdict_payload, _canonical_json(verdict))
            or not hmac.compare_digest(
                verdict.verdict_digest,
                ledger.latest_stable_verdict_digest,
            )
            or verdict.task_id != task_id
            or verdict.revision != revision
            or verdict.stable_hidden_runs != ledger.stable_hidden_runs
            or not hmac.compare_digest(
                verdict.public_summary_digest,
                ledger.public_summary_digest,
            )
            or not hmac.compare_digest(
                verdict.sealed_package_digest,
                ledger.sealed_package_digest,
            )
            or not hmac.compare_digest(
                verdict.budget_digest,
                ledger.budget_digest,
            )
            or not hmac.compare_digest(
                verdict.verification_process_digest,
                ledger.verification_process_digest,
            )
            or not hmac.compare_digest(
                verdict.qualification_digest,
                self._qualification_digest(
                    ledger,
                    ledger.current_streak,
                ),
            )
            or verdict.entry_digests
            != [item.entry_digest for item in ledger.current_streak]
        ):
            raise StableVerificationRejected(
                "stable verdict does not match the replayed streak"
            )
        for entry in ledger.current_streak:
            _, manifest = self.package_manager.find_archive_by_digest(
                task_id,
                revision,
                entry.archive_manifest_digest,
            )
            if (
                manifest.status is not ArchiveStatus.succeeded
                or manifest.validation_mode is not ValidationMode.real_host
                or manifest.run_id != entry.run_id
                or manifest.claim_binding is None
                or manifest.claim_binding.claim_id != entry.claim_id
                or manifest.claim_binding.assignment_id != entry.assignment_id
            ):
                raise StableVerificationRejected(
                    "stable verdict archive replay changed"
                )
        return verdict
