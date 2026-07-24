from __future__ import annotations

import hmac
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .collaboration_models import VerificationClaim, VerificationResultPayload
from .independent_verifier_broker import run_independent_verifier_subprocess
from .lilies_models import Digest
from .stable_verification import (
    HiddenSeedEvidence,
    PlatformHiddenSeedContext,
    StableVerificationLedger,
    StableVerificationManager,
    StableVerificationProgress,
    StableVerificationRejected,
    StableVerificationVerdict,
    _FrozenModel,
    _canonical_json,
    _digest,
    _read_regular,
    _utc_json_now,
    _write_new_read_only,
)


class StableVerificationPreparedAttempt(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: Digest
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    claim: VerificationClaim
    trusted_result: VerificationResultPayload
    hidden_seed_evidence_digest: Digest
    archive_manifest_digest: Digest
    prepared_at: str = Field(min_length=20, max_length=40)
    attempt_digest: Digest

    @model_validator(mode="after")
    def digests_match(self) -> StableVerificationPreparedAttempt:
        request_digest = _digest(
            _canonical_json(
                {
                    "schema_version": "1.0",
                    "task_id": self.task_id,
                    "revision": self.revision,
                    "claim": self.claim,
                    "trusted_result": self.trusted_result,
                    "hidden_seed_evidence_digest": (
                        self.hidden_seed_evidence_digest
                    ),
                    "archive_manifest_digest": self.archive_manifest_digest,
                }
            )
        )
        if not hmac.compare_digest(request_digest, self.attempt_id):
            raise ValueError("stable attempt ID changed")
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"attempt_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.attempt_digest):
            raise ValueError("stable prepared attempt digest changed")
        return self


class StableVerificationAttemptOutcome(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: Digest
    attempt_digest: Digest
    status: Literal["recorded"]
    progress: StableVerificationProgress
    ledger_digest: Digest
    recorded_at: str = Field(min_length=20, max_length=40)
    outcome_digest: Digest

    @model_validator(mode="after")
    def outcome_digest_matches(self) -> StableVerificationAttemptOutcome:
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"outcome_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.outcome_digest):
            raise ValueError("stable attempt outcome digest changed")
        return self


class StableQualificationBundle(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    public_summary_digest: Digest
    sealed_package_digest: Digest
    budget_digest: Digest
    verification_process_digest: Digest
    stable_hidden_runs: int = Field(ge=1, le=100)
    archive_manifest_digests: list[Digest] = Field(max_length=100_000)
    prepared_attempts: list[StableVerificationPreparedAttempt] = Field(
        max_length=100_000
    )
    hidden_seed_evidence: list[HiddenSeedEvidence] = Field(max_length=100_000)
    outcomes: list[StableVerificationAttemptOutcome] = Field(
        max_length=100_000
    )
    ledger: StableVerificationLedger
    current_stable_verdict: StableVerificationVerdict | None = None
    exported_at: str = Field(min_length=20, max_length=40)
    bundle_digest: Digest

    @field_validator("archive_manifest_digests")
    @classmethod
    def archives_are_canonical(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("qualification archive digests must be sorted and unique")
        return value

    @field_validator("hidden_seed_evidence")
    @classmethod
    def seed_evidence_is_canonical(
        cls,
        value: list[HiddenSeedEvidence],
    ) -> list[HiddenSeedEvidence]:
        digests = [item.evidence_digest for item in value]
        if digests != sorted(set(digests)):
            raise ValueError(
                "qualification hidden-seed evidence must be sorted and unique"
            )
        return value

    @model_validator(mode="after")
    def bundle_digest_matches(self) -> StableQualificationBundle:
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"bundle_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.bundle_digest):
            raise ValueError("stable qualification bundle digest changed")
        return self


class StableVerificationCoordinator:
    """Platform-owned broker, persistence, replay, and export authority."""

    def __init__(
        self,
        *,
        state_root: Path,
        broker_root: Path | None = None,
        platform_seed_key_resolver: (
            Callable[[PlatformHiddenSeedContext], bytes] | None
        ) = None,
        verifier_timeout_seconds: float = 60,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.broker_root = (
            Path(broker_root).resolve() if broker_root is not None else None
        )
        self.verifier_timeout_seconds = verifier_timeout_seconds

        def trusted_result(
            task_id: str,
            revision: int,
            claim: VerificationClaim,
        ) -> VerificationResultPayload:
            if self.broker_root is None:
                raise StableVerificationRejected(
                    "stable recording requires the isolated verifier broker"
                )
            return run_independent_verifier_subprocess(
                state_root=self.state_root,
                task_id=task_id,
                revision=revision,
                claim=claim,
                broker_root=self.broker_root,
                timeout_seconds=self.verifier_timeout_seconds,
            )

        self.manager = StableVerificationManager(
            self.state_root,
            trusted_result_resolver=trusted_result,
            platform_seed_key_resolver=platform_seed_key_resolver,
        )

    def _attempt_root(
        self,
        task_id: str,
        revision: int,
        attempt_id: str,
    ) -> Path:
        return (
            self.manager._root(task_id, revision)
            / "attempts"
            / attempt_id.removeprefix("sha256:")
        )

    @staticmethod
    def _prepared_attempt(
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
        result: VerificationResultPayload,
        seed: HiddenSeedEvidence,
    ) -> StableVerificationPreparedAttempt:
        request = {
            "schema_version": "1.0",
            "task_id": task_id,
            "revision": revision,
            "claim": claim,
            "trusted_result": result,
            "hidden_seed_evidence_digest": seed.evidence_digest,
            "archive_manifest_digest": str(claim.archive_manifest_digest),
        }
        attempt_id = _digest(_canonical_json(request))
        payload = {
            **request,
            "attempt_id": attempt_id,
            "prepared_at": _utc_json_now(),
        }
        payload["attempt_digest"] = _digest(_canonical_json(payload))
        return StableVerificationPreparedAttempt.model_validate(payload)

    @staticmethod
    def _load_exact(
        path: Path,
        model_type: type[
            StableVerificationPreparedAttempt
            | StableVerificationAttemptOutcome
        ],
    ) -> StableVerificationPreparedAttempt | StableVerificationAttemptOutcome:
        payload = _read_regular(path, expected_mode=0o400)
        value = model_type.model_validate_json(payload)
        if not hmac.compare_digest(payload, _canonical_json(value)):
            raise StableVerificationRejected(
                "stable coordinator evidence bytes changed"
            )
        return value

    def _persist_prepared(
        self,
        prepared: StableVerificationPreparedAttempt,
    ) -> StableVerificationPreparedAttempt:
        root = self._attempt_root(
            prepared.task_id,
            prepared.revision,
            prepared.attempt_id,
        )
        self.manager._ensure_private_directory(root)
        path = root / "prepared.json"
        if path.exists():
            existing = self._load_exact(
                path,
                StableVerificationPreparedAttempt,
            )
            assert isinstance(existing, StableVerificationPreparedAttempt)
            if (
                not hmac.compare_digest(
                    existing.attempt_digest,
                    prepared.attempt_digest,
                )
                and (
                    existing.claim != prepared.claim
                    or existing.trusted_result != prepared.trusted_result
                    or existing.hidden_seed_evidence_digest
                    != prepared.hidden_seed_evidence_digest
                )
            ):
                raise StableVerificationRejected(
                    "stable attempt identity was reused"
                )
            return existing
        _write_new_read_only(path, _canonical_json(prepared))
        return prepared

    def _persist_outcome(
        self,
        *,
        prepared: StableVerificationPreparedAttempt,
        progress: StableVerificationProgress,
        ledger: StableVerificationLedger,
    ) -> StableVerificationAttemptOutcome:
        payload = {
            "schema_version": "1.0",
            "attempt_id": prepared.attempt_id,
            "attempt_digest": prepared.attempt_digest,
            "status": "recorded",
            "progress": progress,
            "ledger_digest": ledger.ledger_digest,
            "recorded_at": _utc_json_now(),
        }
        payload["outcome_digest"] = _digest(_canonical_json(payload))
        outcome = StableVerificationAttemptOutcome.model_validate(payload)
        root = self._attempt_root(
            prepared.task_id,
            prepared.revision,
            prepared.attempt_id,
        )
        self.manager._ensure_private_directory(root / "outcomes")
        path = (
            root
            / "outcomes"
            / f"{progress.progress_digest.removeprefix('sha256:')}.json"
        )
        if path.exists():
            existing = self._load_exact(
                path,
                StableVerificationAttemptOutcome,
            )
            assert isinstance(existing, StableVerificationAttemptOutcome)
            if existing.progress != progress:
                raise StableVerificationRejected(
                    "stable outcome identity was reused"
                )
            return existing
        _write_new_read_only(path, _canonical_json(outcome))
        return outcome

    def verify_and_record(
        self,
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
    ) -> StableVerificationProgress:
        if self.broker_root is None:
            raise StableVerificationRejected(
                "stable recording requires the isolated verifier broker"
            )
        trusted_result = run_independent_verifier_subprocess(
            state_root=self.state_root,
            task_id=task_id,
            revision=revision,
            claim=claim,
            broker_root=self.broker_root,
            timeout_seconds=self.verifier_timeout_seconds,
        )
        seed_path = self.manager.resolve_hidden_seed_evidence(
            task_id=task_id,
            revision=revision,
            claim=claim,
        )
        seed = self.manager._load_seed_evidence(
            task_id,
            revision,
            seed_path,
        )
        prepared = self._prepared_attempt(
            task_id=task_id,
            revision=revision,
            claim=claim,
            result=trusted_result,
            seed=seed,
        )
        prepared = self._persist_prepared(prepared)
        progress = self.manager.record(
            task_id=task_id,
            revision=revision,
            claim=claim,
            result=trusted_result,
        )
        ledger = self.manager._load_ledger(task_id, revision)
        self._persist_outcome(
            prepared=prepared,
            progress=progress,
            ledger=ledger,
        )
        return progress

    def replay(
        self,
        *,
        task_id: str,
        revision: int,
    ) -> StableVerificationVerdict:
        return self.manager.replay_current_stable_verdict(
            task_id=task_id,
            revision=revision,
        )

    def export_qualification_bundle(
        self,
        *,
        task_id: str,
        revision: int,
    ) -> StableQualificationBundle:
        ledger = self.manager._load_ledger(task_id, revision)
        if not self.manager._validate_lineage(ledger):
            raise StableVerificationRejected(
                "stable qualification lineage changed"
            )
        current_verdict = (
            self.manager.replay_current_stable_verdict(
                task_id=task_id,
                revision=revision,
            )
            if ledger.latest_stable_verdict_digest is not None
            else None
        )
        attempts_root = self.manager._root(task_id, revision) / "attempts"
        prepared_attempts: list[StableVerificationPreparedAttempt] = []
        outcomes: list[StableVerificationAttemptOutcome] = []
        if attempts_root.exists():
            seen_attempt_ids: set[str] = set()
            seen_attempt_digests: set[str] = set()
            seen_outcome_digests: set[str] = set()
            for child in sorted(attempts_root.iterdir(), key=lambda item: item.name):
                if child.is_symlink() or not child.is_dir():
                    raise StableVerificationRejected(
                        "stable attempt storage has an unsafe entry"
                    )
                prepared = self._load_exact(
                    child / "prepared.json",
                    StableVerificationPreparedAttempt,
                )
                assert isinstance(prepared, StableVerificationPreparedAttempt)
                if (
                    child.name != prepared.attempt_id.removeprefix("sha256:")
                    or prepared.attempt_id in seen_attempt_ids
                    or prepared.attempt_digest in seen_attempt_digests
                    or {item.name for item in child.iterdir()}
                    - {"prepared.json", "outcomes"}
                ):
                    raise StableVerificationRejected(
                        "stable prepared attempt storage is not canonical"
                    )
                seen_attempt_ids.add(prepared.attempt_id)
                seen_attempt_digests.add(prepared.attempt_digest)
                prepared_attempts.append(prepared)
                outcome_root = child / "outcomes"
                if outcome_root.exists():
                    if outcome_root.is_symlink() or not outcome_root.is_dir():
                        raise StableVerificationRejected(
                            "stable outcome storage has an unsafe boundary"
                        )
                    for path in sorted(outcome_root.iterdir(), key=lambda item: item.name):
                        if path.is_symlink() or not path.is_file():
                            raise StableVerificationRejected(
                                "stable outcome storage has an unsafe entry"
                            )
                        outcome = self._load_exact(
                            path,
                            StableVerificationAttemptOutcome,
                        )
                        assert isinstance(
                            outcome,
                            StableVerificationAttemptOutcome,
                        )
                        if (
                            outcome.attempt_id != prepared.attempt_id
                            or outcome.attempt_digest
                            != prepared.attempt_digest
                        ):
                            raise StableVerificationRejected(
                                "stable outcome belongs to another attempt"
                            )
                        if (
                            path.name
                            != (
                                outcome.progress.progress_digest.removeprefix(
                                    "sha256:"
                                )
                                + ".json"
                            )
                            or outcome.outcome_digest in seen_outcome_digests
                        ):
                            raise StableVerificationRejected(
                                "stable outcome storage is not canonical"
                            )
                        seen_outcome_digests.add(outcome.outcome_digest)
                        outcomes.append(outcome)
        prepared_by_entry = {
            (
                item.claim.claim_id,
                _digest(_canonical_json(item.trusted_result)),
                item.hidden_seed_evidence_digest,
            )
            for item in prepared_attempts
        }
        for event in ledger.events:
            if event.entry is None:
                continue
            identity = (
                event.entry.claim_id,
                event.entry.verification_result_digest,
                event.entry.hidden_seed_evidence_digest,
            )
            if identity not in prepared_by_entry:
                raise StableVerificationRejected(
                    "stable ledger entry has no persisted trusted attempt"
                )
        package = self.manager.package_manager.load_frozen(task_id, revision)
        hidden_seed_by_digest: dict[str, HiddenSeedEvidence] = {}
        for prepared in prepared_attempts:
            seed = self.manager.load_hidden_seed_evidence_for_claim(
                task_id=task_id,
                revision=revision,
                claim=prepared.claim,
            )
            if not hmac.compare_digest(
                seed.evidence_digest,
                prepared.hidden_seed_evidence_digest,
            ):
                raise StableVerificationRejected(
                    "prepared attempt hidden-seed receipt changed"
                )
            hidden_seed_by_digest.setdefault(seed.evidence_digest, seed)
        hidden_seed_evidence = [
            hidden_seed_by_digest[digest]
            for digest in sorted(hidden_seed_by_digest)
        ]
        archive_digests = sorted(
            {
                str(item.claim.archive_manifest_digest)
                for item in prepared_attempts
            }
        )
        payload = {
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
            "archive_manifest_digests": archive_digests,
            "prepared_attempts": prepared_attempts,
            "hidden_seed_evidence": hidden_seed_evidence,
            "outcomes": outcomes,
            "ledger": ledger,
            "current_stable_verdict": current_verdict,
            "exported_at": (
                ledger.events[-1].recorded_at.isoformat().replace(
                    "+00:00",
                    "Z",
                )
                if ledger.events
                else package.record.frozen_at.isoformat().replace(
                    "+00:00",
                    "Z",
                )
            ),
        }
        payload["bundle_digest"] = _digest(
            _canonical_json(
                {
                    key: value
                    for key, value in payload.items()
                    if value is not None
                }
            )
        )
        return StableQualificationBundle.model_validate(payload)
