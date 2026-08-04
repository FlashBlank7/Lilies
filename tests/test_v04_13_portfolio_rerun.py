from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import pytest

import scripts.run_v04_13_portfolio_rerun as portfolio


BASE_TIME = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _probable_prime(candidate: int) -> bool:
    small_primes = (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)
    if candidate < 3 or candidate % 2 == 0:
        return candidate == 2
    if any(candidate % prime == 0 for prime in small_primes):
        return candidate in small_primes
    exponent = candidate - 1
    shifts = 0
    while exponent % 2 == 0:
        exponent //= 2
        shifts += 1
    for _ in range(32):
        witness = secrets.randbelow(candidate - 3) + 2
        value = pow(witness, exponent, candidate)
        if value in {1, candidate - 1}:
            continue
        for _ in range(shifts - 1):
            value = pow(value, 2, candidate)
            if value == candidate - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int) -> int:
    while True:
        candidate = (
            secrets.randbits(bits)
            | (1 << (bits - 1))
            | (1 << (bits - 2))
            | 1
        )
        if _probable_prime(candidate):
            return candidate


def _ephemeral_rsa_key() -> tuple[portfolio.ReceiptTrustRoot, int]:
    exponent = 65_537
    while True:
        first = _generate_prime(1_024)
        second = _generate_prime(1_024)
        if first == second:
            continue
        modulus = first * second
        totient = (first - 1) * (second - 1)
        if math.gcd(exponent, totient) == 1:
            private_exponent = pow(exponent, -1, totient)
            return (
                portfolio.ReceiptTrustRoot(
                    issuer=f"task-author-{secrets.token_hex(8)}",
                    key_id=f"ephemeral-rsa-{secrets.token_hex(8)}",
                    rsa_modulus=modulus,
                    rsa_exponent=exponent,
                ),
                private_exponent,
            )


TEST_TRUST_ROOT, PRIVATE_EXPONENT = _ephemeral_rsa_key()


def _verifier() -> portfolio.ReceiptTrustVerifier:
    return portfolio.ReceiptTrustVerifier(TEST_TRUST_ROOT)


def _digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _uuid(project_number: int, sequence: int) -> str:
    return str(UUID(int=project_number * 10_000 + sequence))


def _iso(seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=seconds)).isoformat()


def _sign_envelope(
    receipt_id: str,
    semantic_type: str,
    semantic_payload: Mapping[str, Any],
    issued_at: float,
) -> portfolio.SignedReceiptEnvelope:
    unsigned = portfolio.SignedReceiptEnvelope(
        receipt_id=receipt_id,
        issuer=TEST_TRUST_ROOT.issuer,
        key_id=TEST_TRUST_ROOT.key_id,
        issued_at=_iso(issued_at),
        semantic_type=semantic_type,
        semantic_payload=dict(semantic_payload),
        payload_digest=portfolio._canonical_digest(semantic_payload),
        signature="pending",
    )
    modulus_bytes = (TEST_TRUST_ROOT.rsa_modulus.bit_length() + 7) // 8
    digest_info = portfolio.RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(
        portfolio._receipt_signing_bytes(unsigned)
    ).digest()
    padding_size = modulus_bytes - len(digest_info) - 3
    encoded = b"\x00\x01" + b"\xff" * padding_size + b"\x00" + digest_info
    signature = pow(
        int.from_bytes(encoded, "big"),
        PRIVATE_EXPONENT,
        TEST_TRUST_ROOT.rsa_modulus,
    ).to_bytes(modulus_bytes, "big")
    return replace(
        unsigned,
        signature=base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
    )


def _checkpoint(
    session_id: str,
    total: int,
    *,
    calls: int,
    runtime_cap: int = portfolio.MAX_SESSION_TOKENS,
) -> portfolio.TokenCheckpoint:
    groups: tuple[portfolio.TokenUsageGroup, ...] = ()
    if calls or total:
        groups = (
            portfolio.TokenUsageGroup(
                stage="builder-session",
                model="provider-model",
                recorded_calls=calls,
                input_tokens=total // 2,
                output_tokens=total - total // 2,
                total_tokens=total,
            ),
        )
    receipt_ids = tuple(
        f"model-call-{session_id[-12:]}-{index}" for index in range(1, calls + 1)
    )
    at_cap = total == portfolio.MAX_SESSION_TOKENS
    return portfolio.TokenCheckpoint(
        session_id=session_id,
        attempted_calls=calls,
        recorded_calls=calls,
        input_tokens=total // 2,
        output_tokens=total - total // 2,
        total_tokens=total,
        runtime_cap_tokens=runtime_cap,
        model_call_attempt_receipt_ids=receipt_ids,
        model_call_receipt_ids=receipt_ids,
        hard_stop_triggered=at_cap,
        cap_reached_at_receipt_id=receipt_ids[-1] if at_cap and receipt_ids else None,
        hard_stop_fence_receipt_id=receipt_ids[-1] if at_cap and receipt_ids else None,
        groups=groups,
    )


def _resolved_manifest(
    manifest: portfolio.ProjectManifest,
) -> portfolio.ProjectManifest:
    hooks = {
        name: (
            hook
            if hook.available
            else portfolio.HookSpec.broker_capability(
                f"testkit.{manifest.project_id.lower()}.{name}.v1"
            )
        )
        for name, hook in manifest.hooks.items()
    }
    return replace(manifest, hooks=hooks, real_adapter_gap=None)


def _resolved_manifests() -> tuple[portfolio.ProjectManifest, ...]:
    return tuple(_resolved_manifest(item) for item in portfolio.PROJECT_MANIFESTS.values())


def _adapter_receipt(
    manifest: portfolio.ProjectManifest,
    *,
    salt: str = "first",
    actor_profile: portfolio.BuilderActorProfile = portfolio.LILIES_ACTOR_PROFILE,
) -> portfolio.AdapterCapabilityReceipt:
    number = int(manifest.project_id[-3:])
    capabilities = portfolio.required_adapter_capabilities(manifest, actor_profile)
    adapter_id = f"portfolio-adapter-{number}-{salt}"
    adapter_digest = _digest(f"adapter:{number}:{salt}")
    payload = {
        "project_id": manifest.project_id,
        "adapter_id": adapter_id,
        "adapter_digest": adapter_digest,
        "capabilities": list(capabilities),
    }
    capability_digest = portfolio._canonical_digest(payload)
    sequence = 1 if salt == "first" else 900 + number
    return portfolio.AdapterCapabilityReceipt(
        project_id=manifest.project_id,
        adapter_id=adapter_id,
        adapter_digest=adapter_digest,
        capability_digest=capability_digest,
        capabilities=capabilities,
        envelope=_sign_envelope(
            _uuid(number, sequence),
            "adapter_capability",
            payload,
            number * 100 - 1,
        ),
    )


def _event(
    *,
    kind: str,
    name: str,
    summary: str,
    entity_id: str,
    receipt_digest: str,
    at: float,
    aggregate: bool = False,
) -> portfolio.ObservableEvent:
    return portfolio.ObservableEvent(
        kind=kind,  # type: ignore[arg-type]
        name=name,  # type: ignore[arg-type]
        summary=summary,
        entity_id=entity_id,
        receipt_digest=receipt_digest,
        visibility="aggregate_only" if aggregate else "public",
        safe_projection="aggregate_receipt" if aggregate else "platform_public",
        at=_iso(at),
    )


@dataclass(frozen=True)
class TrustedScenario:
    manifest: portfolio.ProjectManifest
    evidence: portfolio.SafeExecutionEvidence
    envelopes: tuple[portfolio.SignedReceiptEnvelope, ...]
    phase_results: dict[str, portfolio.PhaseExecution]


def _trusted_scenario(
    manifest: portfolio.ProjectManifest,
    *,
    offset: float,
    attempt_id: str,
) -> TrustedScenario:
    number = int(manifest.project_id[-3:])
    application_id = _uuid(number, 10)
    assignment_id = _uuid(number, 16)
    session_id = _uuid(number, 21)
    archive_id = _uuid(number, 19)
    connection_id = _uuid(number, 17)
    content_hash = _digest(f"published:{manifest.project_id}")
    archive_digest = _digest(f"archive:{manifest.project_id}")
    fresh_hash = _digest(f"fresh:{manifest.project_id}")
    daemon_fingerprint = _digest(f"daemon:{manifest.project_id}")
    credential_digest = _digest(f"credential:{manifest.project_id}")
    receipt_context = {
        "project_id": manifest.project_id,
        "attempt_id": attempt_id,
    }
    environment_receipt_id = _uuid(number, 23)

    fresh = portfolio.FreshDraftReceipt(
        receipt_id=_uuid(number, 11),
        application_id=application_id,
        draft_revision=0,
        node_count=0,
        edge_count=0,
        draft_content_hash=fresh_hash,
        observed_at=_iso(offset + 0.4),
    )
    access = portfolio.DaemonAccessReceipt(
        discovery_receipt_id=_uuid(number, 12),
        pairing_receipt_id=_uuid(number, 13),
        task_credential_receipt_id=_uuid(number, 14),
        connection_id=connection_id,
        daemon_fingerprint=daemon_fingerprint,
        daemon_base_url=f"http://127.0.0.1:{9000 + number}",
        task_credential_digest=credential_digest,
        exact_discovery_match=True,
        exact_pairing_match=True,
    )
    publication = portfolio.PublicationReceipt(
        receipt_id=_uuid(number, 18),
        application_id=application_id,
        published_version=2,
        published_content_hash=content_hash,
        published_at=_iso(offset + 4.8),
    )
    assignment_receipt_id = _uuid(number, 15)
    archive_receipt_id = _uuid(number, 20)

    case_receipts: list[portfolio.AcceptanceCaseReceipt] = []
    case_events: list[portfolio.ObservableEvent] = []
    for index, case_id in enumerate(("debug", *manifest.seed_ids)):
        run_id = _uuid(number, 30 + index)
        aggregate_digest = _digest(f"case:{manifest.project_id}:{case_id}")
        started = offset + 5.01 + index * 0.2
        finished = started + 0.08
        case_receipts.append(
            portfolio.AcceptanceCaseReceipt(
                case_id=case_id,
                run_id=run_id,
                receipt_id=_uuid(number, 40 + index),
                environment_generation=(
                    f"{manifest.project_id.lower()}-{case_id}-generation"
                ),
                published_version=2,
                published_content_hash=content_hash,
                status="passed",
                aggregate_receipt_digest=aggregate_digest,
                started_at=_iso(started),
                finished_at=_iso(finished),
            )
        )
        case_events.append(
            _event(
                kind="run",
                name="acceptance_case_completed",
                summary=f"case {index + 1} passed under aggregate check",
                entity_id=run_id,
                receipt_digest=aggregate_digest,
                at=finished,
                aggregate=True,
            )
        )

    phase_envelopes: dict[str, list[portfolio.SignedReceiptEnvelope]] = {
        phase: [] for phase in portfolio.PHASES
    }
    fresh_envelope = _sign_envelope(
        fresh.receipt_id,
        "fresh_application",
        {**receipt_context, "receipt": asdict(fresh)},
        offset + 0.4,
    )
    environment_envelope = _sign_envelope(
        environment_receipt_id,
        "environment_generation",
        {
            **receipt_context,
            "application_id": application_id,
            "environment_generation": (
                f"{manifest.project_id.lower()}-attempt-generation"
            ),
        },
        offset + 0.45,
    )
    discovery_envelope = _sign_envelope(
        access.discovery_receipt_id,
        "daemon_discovery",
        {
            **receipt_context,
            "connection_id": connection_id,
            "daemon_fingerprint": daemon_fingerprint,
            "daemon_base_url": access.daemon_base_url,
        },
        offset + 1.4,
    )
    pairing_envelope = _sign_envelope(
        access.pairing_receipt_id,
        "explicit_pairing",
        {
            **receipt_context,
            "connection_id": connection_id,
            "daemon_fingerprint": daemon_fingerprint,
        },
        offset + 2.4,
    )
    credential_envelope = _sign_envelope(
        access.task_credential_receipt_id,
        "task_credential",
        {
            **receipt_context,
            "assignment_id": assignment_id,
            "task_credential_digest": credential_digest,
        },
        offset + 3.2,
    )
    assignment_envelope = _sign_envelope(
        assignment_receipt_id,
        "assignment",
        {
            **receipt_context,
            "application_id": application_id,
            "assignment_id": assignment_id,
            "session_id": session_id,
            "formal_builder_actor": portfolio.LILIES_BUILDER_ACTOR,
            "builder_actor": portfolio.BUILDER_ACTOR,
            "sibling_commit": "a" * 40,
            "sibling_package_digest": _digest("standalone-lilies-package"),
            "fallback_eligibility": None,
        },
        offset + 3.4,
    )
    publication_envelope = _sign_envelope(
        publication.receipt_id,
        "publication",
        {**receipt_context, "receipt": asdict(publication)},
        offset + 4.8,
    )
    case_envelopes = [
        _sign_envelope(
            receipt.receipt_id,
            "acceptance_case",
            {**receipt_context, "receipt": asdict(receipt)},
            (
                datetime.fromisoformat(receipt.finished_at) - BASE_TIME
            ).total_seconds(),
        )
        for receipt in case_receipts
    ]
    archive_envelope = _sign_envelope(
        archive_receipt_id,
        "archive",
        {
            **receipt_context,
            "application_id": application_id,
            "archive_id": archive_id,
            "archive_digest": archive_digest,
            "published_version": 2,
            "published_content_hash": content_hash,
        },
        offset + 6.5,
    )
    cleanup_envelope = _sign_envelope(
        _uuid(number, 22),
        "cleanup",
        {
            **receipt_context,
            "application_id": application_id,
            "outcome": "completed",
        },
        offset + 7.5,
    )
    phase_envelopes["environment_bootstrap"].extend(
        (fresh_envelope, environment_envelope)
    )
    phase_envelopes["daemon_discovery"].append(discovery_envelope)
    phase_envelopes["explicit_pairing"].append(pairing_envelope)
    phase_envelopes["assignment_provision"].extend(
        (credential_envelope, assignment_envelope)
    )

    lifecycle: portfolio.MLLifecycleEvidence | None = None
    if manifest.project_id == "EXP-LILIES-006":
        common = {
            **receipt_context,
            "application_id": application_id,
            "published_version": 2,
            "workflow_content_hash": content_hash,
            "archive_id": archive_id,
            "immutable_model_version": "replenishment-model-v1",
            "model_content_digest": _digest("model-content-v1"),
        }
        stage_specs = {
            "chronological_split": {
                **common,
                "stage": "chronological_split",
                "occurred_at": _iso(offset - 86_400),
                "training_window_end": _iso(offset - 172_800),
                "evaluation_window_start": _iso(offset - 86_400),
            },
            "fit_train": {
                **common,
                "stage": "fit_train",
                "occurred_at": _iso(offset + 4.12),
            },
            "evaluation": {
                **common,
                "stage": "evaluation",
                "occurred_at": _iso(offset + 4.26),
            },
            "backtest": {
                **common,
                "stage": "backtest",
                "occurred_at": _iso(offset + 4.26),
            },
            "promotion": {
                **common,
                "stage": "promotion",
                "occurred_at": _iso(offset + 4.36),
            },
            "deployment": {
                **common,
                "stage": "deployment",
                "occurred_at": _iso(offset + 4.44),
            },
            "inference": {
                **common,
                "stage": "inference",
                "occurred_at": _iso(offset + 5.72),
                "run_id": case_receipts[0].run_id,
            },
            "retraining_trigger": {
                **common,
                "stage": "retraining_trigger",
                "occurred_at": _iso(offset + 5.82),
            },
        }
        issued = {
            "chronological_split": offset + 4.05,
            "fit_train": offset + 4.12,
            "evaluation": offset + 4.19,
            "backtest": offset + 4.26,
            "promotion": offset + 4.36,
            "deployment": offset + 4.44,
            "inference": offset + 5.72,
            "retraining_trigger": offset + 5.82,
        }
        stage_envelopes = {
            stage: _sign_envelope(
                _uuid(number, 50 + index),
                "ml_stage",
                payload,
                issued[stage],
            )
            for index, (stage, payload) in enumerate(stage_specs.items())
        }
        phase_envelopes["builder_execution"].extend(
            stage_envelopes[stage]
            for stage in (
                "chronological_split",
                "fit_train",
                "evaluation",
                "backtest",
                "promotion",
                "deployment",
            )
        )
        lifecycle = portfolio.MLLifecycleEvidence(
            application_id=application_id,
            published_version=2,
            workflow_content_hash=content_hash,
            archive_id=archive_id,
            chronological_split_receipt_digest=stage_envelopes[
                "chronological_split"
            ].payload_digest,
            training_window_end=_iso(offset - 172_800),
            evaluation_window_start=_iso(offset - 86_400),
            fit_train_receipt_digest=stage_envelopes["fit_train"].payload_digest,
            trained_at=_iso(offset + 4.12),
            evaluation_receipt_digest=stage_envelopes["evaluation"].payload_digest,
            backtest_receipt_digest=stage_envelopes["backtest"].payload_digest,
            evaluated_at=_iso(offset + 4.26),
            immutable_model_version="replenishment-model-v1",
            model_content_digest=_digest("model-content-v1"),
            promotion_receipt_digest=stage_envelopes["promotion"].payload_digest,
            promoted_at=_iso(offset + 4.36),
            deployment_receipt_digest=stage_envelopes["deployment"].payload_digest,
            deployed_at=_iso(offset + 4.44),
            inference_run_id=case_receipts[0].run_id,
            inference_receipt_digest=stage_envelopes["inference"].payload_digest,
            inferred_at=_iso(offset + 5.72),
            retraining_trigger_receipt_digest=stage_envelopes[
                "retraining_trigger"
            ].payload_digest,
            retraining_evaluated_at=_iso(offset + 5.82),
        )
        phase_envelopes["host_result_verification"].extend(case_envelopes)
        phase_envelopes["host_result_verification"].extend(
            (
                stage_envelopes["inference"],
                stage_envelopes["retraining_trigger"],
            )
        )
    else:
        phase_envelopes["host_result_verification"].extend(case_envelopes)

    phase_envelopes["builder_execution"].append(publication_envelope)
    phase_envelopes["platform_archive_verification"].append(archive_envelope)
    phase_envelopes["cleanup_reporting"].append(cleanup_envelope)
    checkpoints = {
        "assignment_provision": _checkpoint(session_id, 100, calls=1),
        "builder_execution": _checkpoint(session_id, 200, calls=2),
        "host_result_verification": _checkpoint(session_id, 300, calls=3),
        "platform_archive_verification": _checkpoint(session_id, 400, calls=4),
        "cleanup_reporting": _checkpoint(session_id, 400, calls=4),
    }
    for phase in portfolio.PHASES[3:]:
        phase_envelopes[phase].append(
            _sign_envelope(
                _uuid(number, 80 + portfolio.PHASES.index(phase)),
                "token_checkpoint",
                {
                    **receipt_context,
                    "checkpoint": asdict(checkpoints[phase]),
                    "final": phase == portfolio.PHASES[-1],
                },
                offset
                + portfolio.PHASES.index(phase)
                + (0.7 if phase == portfolio.PHASES[-1] else 0.9),
            )
        )

    evidence = portfolio.SafeExecutionEvidence(
        attempt_id=attempt_id,
        builder_actor=portfolio.BUILDER_ACTOR,
        formal_builder_actor=portfolio.LILIES_BUILDER_ACTOR,
        fallback_eligibility=None,
        trusted_verifier_id=TEST_TRUST_ROOT.issuer,
        trusted_verifier_digest=TEST_TRUST_ROOT.verifier_digest,
        receipt_chain_digest=_digest("pending-chain"),
        sibling_commit="a" * 40,
        sibling_package_digest=_digest("standalone-lilies-package"),
        application_id=application_id,
        assignment_receipt_id=assignment_receipt_id,
        published_version=2,
        published_content_hash=content_hash,
        assignment_id=assignment_id,
        session_id=session_id,
        environment_generation=f"{manifest.project_id.lower()}-attempt-generation",
        environment_receipt_id=environment_receipt_id,
        archive_id=archive_id,
        archive_receipt_id=archive_receipt_id,
        archive_digest=archive_digest,
        public_material_digests=portfolio.public_material_digest_receipts(manifest),
        public_interface_digest=_digest("public-platform-interface-v1"),
        fresh_empty_draft=fresh,
        task_access=portfolio.TaskAccessReceipt(
            task_credential_receipt_id=access.task_credential_receipt_id,
            task_credential_digest=access.task_credential_digest,
        ),
        daemon_access=access,
        publication=publication,
        mutation_guard=portfolio.MutationGuardReceipt(
            published_version=2,
            published_content_hash=content_hash,
            post_acceptance_version=2,
            post_acceptance_content_hash=content_hash,
            mutations_after_publish=0,
        ),
        acceptance_receipts=tuple(case_receipts),
        ml_lifecycle=lifecycle,
    )
    phase_events: dict[str, tuple[portfolio.ObservableEvent, ...]] = {
        "environment_bootstrap": (
            _event(
                kind="tool_result",
                name="fresh_application",
                summary="fresh empty application observed",
                entity_id=application_id,
                receipt_digest=fresh_hash,
                at=offset + 0.5,
            ),
        ),
        "daemon_discovery": (
            _event(
                kind="tool_result",
                name="daemon_discovered",
                summary="exact loopback daemon discovered",
                entity_id=connection_id,
                receipt_digest=daemon_fingerprint,
                at=offset + 1.5,
            ),
        ),
        "explicit_pairing": (
            _event(
                kind="tool_result",
                name="pairing_completed",
                summary="explicit pairing completed",
                entity_id=connection_id,
                receipt_digest=daemon_fingerprint,
                at=offset + 2.5,
            ),
        ),
        "assignment_provision": (
            _event(
                kind="tool_result",
                name="task_credential_bound",
                summary="task scope bound to assignment",
                entity_id=assignment_id,
                receipt_digest=credential_digest,
                at=offset + 3.35,
            ),
            _event(
                kind="tool_result",
                name="assignment_created",
                summary="fresh Builder assignment created",
                entity_id=assignment_id,
                receipt_digest=assignment_envelope.payload_digest,
                at=offset + 3.55,
            ),
        ),
        "builder_execution": (
            _event(
                kind="message",
                name="builder_message",
                summary="Builder reported public progress",
                entity_id=session_id,
                receipt_digest=_digest(f"message:{manifest.project_id}"),
                at=offset + 4.1,
            ),
            _event(
                kind="tool_call",
                name="tool_called",
                summary="public platform operation requested",
                entity_id=assignment_id,
                receipt_digest=_digest(f"tool-call:{manifest.project_id}"),
                at=offset + 4.2,
            ),
            _event(
                kind="tool_result",
                name="tool_completed",
                summary="public platform operation completed",
                entity_id=assignment_id,
                receipt_digest=_digest(f"tool-result:{manifest.project_id}"),
                at=offset + 4.3,
            ),
            _event(
                kind="tool_result",
                name="publication_completed",
                summary="immutable workflow version published",
                entity_id=application_id,
                receipt_digest=content_hash,
                at=offset + 4.8,
            ),
        ),
        "host_result_verification": tuple(case_events),
        "platform_archive_verification": (
            _event(
                kind="artifact",
                name="archive_completed",
                summary="aggregate archive committed",
                entity_id=archive_id,
                receipt_digest=archive_digest,
                at=offset + 6.5,
                aggregate=True,
            ),
        ),
        "cleanup_reporting": (
            _event(
                kind="token_usage",
                name="usage_checkpoint",
                summary="usage counters committed",
                entity_id=session_id,
                receipt_digest=portfolio._canonical_digest(
                    asdict(checkpoints["cleanup_reporting"])
                ),
                at=offset + 7.3,
                aggregate=True,
            ),
            _event(
                kind="tool_result",
                name="cleanup_completed",
                summary="project services stopped safely",
                entity_id=application_id,
                receipt_digest=cleanup_envelope.payload_digest,
                at=offset + 7.6,
            ),
        ),
    }
    event_sequence = 120
    for phase in portfolio.PHASES:
        for event in phase_events[phase]:
            projected = replace(event, phase=phase)
            issued_at = (
                datetime.fromisoformat(event.at) - BASE_TIME
            ).total_seconds()
            phase_envelopes[phase].append(
                _sign_envelope(
                    _uuid(number, event_sequence),
                    "observable_event",
                    {
                        **receipt_context,
                        "event": asdict(projected),
                    },
                    issued_at,
                )
            )
            event_sequence += 1
        phase_envelopes[phase].sort(
            key=lambda envelope: datetime.fromisoformat(envelope.issued_at)
        )
    root_envelope = _sign_envelope(
        _uuid(number, 99),
        "execution_evidence",
        portfolio._execution_evidence_semantic_payload(manifest.project_id, evidence),
        offset + 7.95,
    )
    phase_envelopes["cleanup_reporting"].append(root_envelope)
    envelopes = tuple(
        envelope
        for phase in portfolio.PHASES
        for envelope in phase_envelopes[phase]
    )
    evidence = replace(
        evidence,
        receipt_chain_digest=portfolio._receipt_chain_digest(envelopes),
    )
    phase_results = {
        phase: portfolio.PhaseExecution(
            outcome="completed",
            events=phase_events[phase],
            token_checkpoint=checkpoints.get(phase),
            signed_receipts=tuple(phase_envelopes[phase]),
        )
        for phase in portfolio.PHASES
    }
    return TrustedScenario(manifest, evidence, envelopes, phase_results)


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def wall(self) -> datetime:
        return BASE_TIME + timedelta(seconds=self.value)

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


class ScenarioExecutor:
    def __init__(
        self,
        scenarios: Mapping[str, TrustedScenario],
        clock: ManualClock,
        phase_durations: Mapping[str, float] | None = None,
    ) -> None:
        self.scenarios = scenarios
        self.clock = clock
        self.phase_durations = phase_durations or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        manifest: portfolio.ProjectManifest,
        phase: str,
        plan: Mapping[str, Any],
    ) -> portfolio.PhaseExecution:
        assert plan["project_id"] == manifest.project_id
        assert plan["attempt_id"] == self.scenarios[manifest.project_id].evidence.attempt_id
        self.calls.append((manifest.project_id, phase))
        self.clock.advance(self.phase_durations.get(phase, 1.0))
        return self.scenarios[manifest.project_id].phase_results[phase]


def _attempt_ids(epoch: int = 1) -> tuple[str, ...]:
    return tuple(str(UUID(int=900_000 + epoch * 100 + index)) for index in range(6))


def _trusted_inputs(epoch: int = 1) -> tuple[
    tuple[portfolio.ProjectManifest, ...],
    dict[str, TrustedScenario],
    dict[str, portfolio.AdapterCapabilityReceipt],
    tuple[str, ...],
]:
    manifests = _resolved_manifests()
    attempt_ids = _attempt_ids(epoch)
    scenarios = {
        manifest.project_id: _trusted_scenario(
            manifest,
            offset=index * 8.0,
            attempt_id=attempt_ids[index],
        )
        for index, manifest in enumerate(manifests)
    }
    adapters = {
        manifest.project_id: _adapter_receipt(manifest) for manifest in manifests
    }
    return manifests, scenarios, adapters, attempt_ids


def _fallback_inputs(epoch: int = 3) -> tuple[
    tuple[portfolio.ProjectManifest, ...],
    dict[str, TrustedScenario],
    dict[str, portfolio.AdapterCapabilityReceipt],
    tuple[str, ...],
]:
    manifests, lilies_scenarios, _, attempt_ids = _trusted_inputs(epoch=epoch)
    scenarios = {
        project_id: _codex_fallback_scenario(scenario)
        for project_id, scenario in lilies_scenarios.items()
    }
    adapters = {
        manifest.project_id: _adapter_receipt(
            manifest,
            salt="fallback",
            actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
        )
        for manifest in manifests
    }
    return manifests, scenarios, adapters, attempt_ids


def _ledger(tmp_path: Path) -> portfolio.AttemptLedger:
    return portfolio.AttemptLedger(tmp_path / "attempts.json")


def _attempt_factory(attempt_ids: tuple[str, ...]) -> Any:
    return iter(attempt_ids).__next__


def _bounded_lilies_report_body() -> dict[str, Any]:
    attempt_id = str(UUID(int=777_001))
    session_id = str(UUID(int=777_002))
    phases = [
        asdict(
            portfolio.PhaseSpan(
                phase=phase,
                started_at=_iso(-3.0 + index * 0.125),
                finished_at=_iso(-3.0 + (index + 1) * 0.125),
                duration_seconds=0.125,
                duration_percentage=12.5,
                outcome="failed" if index == len(portfolio.PHASES) - 1 else "completed",
            )
        )
        for index, phase in enumerate(portfolio.PHASES)
    ]
    return {
        "schema_version": "v0.4.13-portfolio-rerun-report-body-r8-1",
        "attempt_id": attempt_id,
        "project_id": "EXP-LILIES-001",
        "formal_builder_actor": "lilies",
        "builder_actor": "lilies",
        "status": "failed",
        "timing_complete": True,
        "phases": phases,
        "total_elapsed_seconds": 1.0,
        "max_session_tokens": portfolio.MAX_SESSION_TOKENS,
        "token_usage_authoritativeness": "exact",
        "final_token_checkpoint": asdict(
            _checkpoint(session_id, 21_051, calls=1)
        ),
        "final_codex_token_usage": None,
        "failure": "bounded upstream failure",
    }


def _fallback_eligibility(
    project_id: str,
    evidence: portfolio.SafeExecutionEvidence,
) -> portfolio.CodexFallbackEligibility:
    number = int(project_id[-3:])
    bounded_attempt_id = str(UUID(int=777_001))
    bounded_report_digest = portfolio._canonical_digest(_bounded_lilies_report_body())
    terminal_payload = {
        "project_id": "EXP-LILIES-001",
        "attempt_id": bounded_attempt_id,
        "formal_builder_actor": "lilies",
        "builder_actor": "lilies",
        "status": "failed",
        "report_digest": bounded_report_digest,
    }
    provisional = portfolio.CodexFallbackEligibility(
        contract_revision=portfolio.CONTRACT_REVISION,
        prerequisite_receipt_id=str(UUID(int=778_000 + number * 10 + 3)),
        prerequisite_payload_digest=_digest("pending-prerequisite"),
        bounded_lilies_attempt_id=bounded_attempt_id,
        bounded_lilies_attempt_report_digest=bounded_report_digest,
        bounded_lilies_terminal_receipt_id=str(UUID(int=777_004)),
        bounded_lilies_terminal_receipt_digest=portfolio._canonical_digest(
            terminal_payload
        ),
        isolated_context_id=f"codex-fallback-context-{number:03d}",
        public_material_allowlist_digest=portfolio._canonical_digest(
            [asdict(item) for item in evidence.public_material_digests]
        ),
        forbidden_assistance_scan_receipt_id=str(UUID(int=778_000 + number * 10 + 2)),
        forbidden_assistance_scan_digest=_digest("pending-scan"),
        freshness_identity_digest=_digest("pending-freshness"),
    )
    projected = replace(
        evidence,
        formal_builder_actor=portfolio.CODEX_FORMAL_BUILDER_ACTOR,
        builder_actor=portfolio.CODEX_FALLBACK_BUILDER_ACTOR,
        fallback_eligibility=provisional,
        sibling_commit=None,
        sibling_package_digest=None,
        daemon_access=None,
    )
    with_freshness = replace(
        provisional,
        freshness_identity_digest=portfolio._canonical_digest(
            portfolio._fallback_freshness_identities(projected)
        ),
    )
    projected = replace(projected, fallback_eligibility=with_freshness)
    with_scan = replace(
        with_freshness,
        forbidden_assistance_scan_digest=portfolio._canonical_digest(
            portfolio._fallback_scan_payload(project_id, projected)
        ),
    )
    projected = replace(projected, fallback_eligibility=with_scan)
    return replace(
        with_scan,
        prerequisite_payload_digest=portfolio._canonical_digest(
            portfolio._fallback_prerequisite_payload(project_id, projected)
        ),
    )


def _codex_fallback_evidence(
    project_id: str,
    evidence: portfolio.SafeExecutionEvidence,
) -> portfolio.SafeExecutionEvidence:
    return replace(
        evidence,
        formal_builder_actor=portfolio.CODEX_FORMAL_BUILDER_ACTOR,
        builder_actor=portfolio.CODEX_FALLBACK_BUILDER_ACTOR,
        fallback_eligibility=_fallback_eligibility(project_id, evidence),
        sibling_commit=None,
        sibling_package_digest=None,
        daemon_access=None,
    )


def _seed_bounded_lilies_failure(ledger: portfolio.AttemptLedger) -> None:
    attempt_id = str(UUID(int=777_001))
    report_body = _bounded_lilies_report_body()
    report_digest = portfolio._canonical_digest(report_body)
    ledger.start_attempt(
        attempt_id=attempt_id,
        project_id="EXP-LILIES-001",
        manifest_revision=28,
        started_at=_iso(-3.0),
    )
    ledger.bind_identities(
        attempt_id,
        {"session_id": str(UUID(int=777_002))},
    )
    ledger.finalize_attempt(
        attempt_id=attempt_id,
        status_value="failed",
        finished_at=_iso(-2.0),
        failure="bounded upstream failure",
        cleanup_failure=None,
        report_digest=report_digest,
        report_body=report_body,
    )
    terminal_payload = {
        "project_id": "EXP-LILIES-001",
        "attempt_id": attempt_id,
        "formal_builder_actor": "lilies",
        "builder_actor": "lilies",
        "status": "failed",
        "report_digest": report_digest,
    }
    ledger.record_signed_terminal_attempt(
        _sign_envelope(
            str(UUID(int=777_004)),
            "attempt_terminal",
            terminal_payload,
            -1.0,
        ),
        _verifier(),
    )


def _codex_fallback_scenario(scenario: TrustedScenario) -> TrustedScenario:
    manifest = scenario.manifest
    evidence = _codex_fallback_evidence(manifest.project_id, scenario.evidence)
    assert evidence.fallback_eligibility is not None
    eligibility = evidence.fallback_eligibility
    offset = (
        datetime.fromisoformat(evidence.fresh_empty_draft.observed_at) - BASE_TIME
    ).total_seconds() - 0.4
    usage = portfolio.CodexTokenUsageEvidence(
        session_id=evidence.session_id,
        availability="unavailable",
        authoritative_source="codex_runtime_counter",
        attempted_calls=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        reason="authoritative usage counter was not exposed by this runtime",
    )
    scan_envelope = _sign_envelope(
        eligibility.forbidden_assistance_scan_receipt_id,
        "public_only_forbidden_assistance_scan",
        portfolio._fallback_scan_payload(manifest.project_id, evidence),
        offset + 3.0,
    )
    prerequisite_envelope = _sign_envelope(
        eligibility.prerequisite_receipt_id,
        "codex_fallback_prerequisite",
        portfolio._fallback_prerequisite_payload(manifest.project_id, evidence),
        offset + 3.1,
    )
    phase_results: dict[str, portfolio.PhaseExecution] = {}
    for phase in portfolio.PHASES:
        old = scenario.phase_results[phase]
        if phase in portfolio.ACTOR_INAPPLICABLE_PHASES:
            phase_results[phase] = portfolio.PhaseExecution(outcome="not_applicable")
            continue
        events = list(old.events)
        non_observable = [
            envelope
            for envelope in old.signed_receipts
            if envelope.semantic_type
            not in {"observable_event", "token_checkpoint", "execution_evidence"}
        ]
        if phase == "assignment_provision":
            old_assignment = next(
                envelope
                for envelope in non_observable
                if envelope.semantic_type == "assignment"
            )
            assignment_payload = {
                "project_id": manifest.project_id,
                "attempt_id": evidence.attempt_id,
                "application_id": evidence.application_id,
                "assignment_id": evidence.assignment_id,
                "session_id": evidence.session_id,
                "formal_builder_actor": "codex",
                "builder_actor": "codex_fallback",
                "sibling_commit": None,
                "sibling_package_digest": None,
                "fallback_eligibility": asdict(eligibility),
            }
            replacement_assignment = _sign_envelope(
                old_assignment.receipt_id,
                "assignment",
                assignment_payload,
                (datetime.fromisoformat(old_assignment.issued_at) - BASE_TIME).total_seconds(),
            )
            non_observable = [
                replacement_assignment
                if envelope.semantic_type == "assignment"
                else envelope
                for envelope in non_observable
            ]
            events = [
                replace(event, receipt_digest=replacement_assignment.payload_digest)
                if event.name == "assignment_created"
                else event
                for event in events
            ]
            non_observable.extend((scan_envelope, prerequisite_envelope))
        if phase == "cleanup_reporting":
            events = [
                replace(
                    event,
                    receipt_digest=portfolio._canonical_digest(asdict(usage)),
                )
                if event.name == "usage_checkpoint"
                else event
                for event in events
            ]
        codex_usage: portfolio.CodexTokenUsageEvidence | None = None
        if phase in portfolio.TOKEN_MONITORED_PHASES:
            old_token = next(
                envelope
                for envelope in old.signed_receipts
                if envelope.semantic_type == "token_checkpoint"
            )
            codex_usage = usage
            non_observable.append(
                _sign_envelope(
                    old_token.receipt_id,
                    "codex_token_usage",
                    {
                        "project_id": manifest.project_id,
                        "attempt_id": evidence.attempt_id,
                        "usage": asdict(usage),
                        "final": phase == portfolio.PHASES[-1],
                    },
                    (
                        datetime.fromisoformat(old_token.issued_at) - BASE_TIME
                    ).total_seconds(),
                )
            )
        old_observable = [
            envelope
            for envelope in old.signed_receipts
            if envelope.semantic_type == "observable_event"
        ]
        observable = [
            _sign_envelope(
                envelope.receipt_id,
                "observable_event",
                {
                    "project_id": manifest.project_id,
                    "attempt_id": evidence.attempt_id,
                    "event": asdict(replace(event, phase=phase)),
                },
                (datetime.fromisoformat(envelope.issued_at) - BASE_TIME).total_seconds(),
            )
            for event, envelope in zip(events, old_observable, strict=True)
        ]
        receipts = sorted(
            [*non_observable, *observable],
            key=lambda envelope: datetime.fromisoformat(envelope.issued_at),
        )
        phase_results[phase] = portfolio.PhaseExecution(
            outcome="completed",
            events=tuple(events),
            codex_token_usage=codex_usage,
            signed_receipts=tuple(receipts),
        )
    old_root = next(
        envelope
        for envelope in scenario.phase_results["cleanup_reporting"].signed_receipts
        if envelope.semantic_type == "execution_evidence"
    )
    root_envelope = _sign_envelope(
        old_root.receipt_id,
        "execution_evidence",
        portfolio._execution_evidence_semantic_payload(manifest.project_id, evidence),
        (datetime.fromisoformat(old_root.issued_at) - BASE_TIME).total_seconds(),
    )
    cleanup = phase_results["cleanup_reporting"]
    phase_results["cleanup_reporting"] = replace(
        cleanup,
        signed_receipts=(*cleanup.signed_receipts, root_envelope),
    )
    envelopes = tuple(
        envelope
        for phase in portfolio.PHASES
        for envelope in phase_results[phase].signed_receipts
    )
    evidence = replace(
        evidence,
        receipt_chain_digest=portfolio._receipt_chain_digest(envelopes),
    )
    return TrustedScenario(
        manifest=manifest,
        evidence=evidence,
        envelopes=envelopes,
        phase_results=phase_results,
    )


def _report_body(
    report: portfolio.ProjectExecutionReport,
    scenario: TrustedScenario,
) -> dict[str, Any]:
    envelopes = scenario.envelopes
    return {
        "schema_version": "v0.4.13-portfolio-rerun-report-body-r8-1",
        "attempt_id": report.attempt_id,
        "project_id": report.project_id,
        "manifest_revision": report.manifest_revision,
        "formal_builder_actor": report.formal_builder_actor,
        "builder_actor": report.builder_actor,
        "receipt_trust_root": asdict(TEST_TRUST_ROOT),
        "status": report.status,
        "timing_complete": len(report.phases) == len(portfolio.PHASES),
        "phases": [asdict(span) for span in report.phases],
        "total_elapsed_seconds": report.total_elapsed_seconds,
        "timing_residual_seconds": report.timing_residual_seconds,
        "observable_event_counts": dict(report.observable_event_counts),
        "observable_events": [asdict(event) for event in report.observable_events],
        "output_summaries": list(report.output_summaries),
        "max_session_tokens": (
            portfolio.MAX_SESSION_TOKENS
            if report.formal_builder_actor == "lilies"
            else None
        ),
        "token_usage_authoritativeness": (
            "exact"
            if report.final_token_checkpoint is not None
            else (
                None
                if report.final_codex_token_usage is None
                else report.final_codex_token_usage.availability
            )
        ),
        "final_token_checkpoint": (
            None
            if report.final_token_checkpoint is None
            else asdict(report.final_token_checkpoint)
        ),
        "final_codex_token_usage": (
            None
            if report.final_codex_token_usage is None
            else asdict(report.final_codex_token_usage)
        ),
        "execution_evidence": (
            None if report.execution_evidence is None else asdict(report.execution_evidence)
        ),
        "signed_receipts": [asdict(envelope) for envelope in envelopes],
        "signed_receipt_phases": [
            phase
            for phase in portfolio.PHASES
            for _ in scenario.phase_results[phase].signed_receipts
        ],
        "signed_receipt_chain_digest": portfolio._receipt_chain_digest(envelopes),
        "failure": report.failure,
        "cleanup_failure": report.cleanup_failure,
    }


def _replace_scenario_root(
    scenario: TrustedScenario,
    evidence: portfolio.SafeExecutionEvidence,
) -> TrustedScenario:
    cleanup = scenario.phase_results["cleanup_reporting"]
    old_root = next(
        item for item in cleanup.signed_receipts if item.semantic_type == "execution_evidence"
    )
    issued_at = (datetime.fromisoformat(old_root.issued_at) - BASE_TIME).total_seconds()
    new_root = _sign_envelope(
        old_root.receipt_id,
        "execution_evidence",
        portfolio._execution_evidence_semantic_payload(
            scenario.manifest.project_id,
            evidence,
        ),
        issued_at,
    )
    new_cleanup = replace(
        cleanup,
        signed_receipts=tuple(
            new_root if item.semantic_type == "execution_evidence" else item
            for item in cleanup.signed_receipts
        ),
    )
    phase_results = dict(scenario.phase_results)
    phase_results["cleanup_reporting"] = new_cleanup
    envelopes = tuple(
        envelope
        for phase in portfolio.PHASES
        for envelope in phase_results[phase].signed_receipts
    )
    evidence = replace(
        evidence,
        receipt_chain_digest=portfolio._receipt_chain_digest(envelopes),
    )
    return TrustedScenario(
        manifest=scenario.manifest,
        evidence=evidence,
        envelopes=envelopes,
        phase_results=phase_results,
    )


def test_manifests_are_structural_and_declare_the_real_adapter_gap() -> None:
    assert tuple(portfolio.PROJECT_MANIFESTS) == tuple(
        f"EXP-LILIES-{index:03d}" for index in range(1, 7)
    )
    assert len(portfolio.PHASES) == 8
    assert portfolio.BUILDER_ACTOR == "lilies"
    assert portfolio.CONTRACT_REVISION == 8
    for manifest in portfolio.PROJECT_MANIFESTS.values():
        assert portfolio.validate_manifest(manifest) == []
        assert manifest.real_adapter_gap is not None
        assert all(
            hook.route in {"commands", "broker_capability", "gap"}
            for hook in manifest.hooks.values()
        )
        assert all(
            bool(hook.commands)
            if hook.route == "commands"
            else hook.capability_id is not None
            if hook.route == "broker_capability"
            else bool(hook.capability_gap)
            for hook in manifest.hooks.values()
        )
        capabilities = portfolio.required_adapter_capabilities(manifest)
        assert "real_project_testkit_public_api" in capabilities
        assert any(
            item.startswith("real_project_testkit_digest:sha256:")
            for item in capabilities
        )

    runner = Path(portfolio.__file__).read_text(encoding="utf-8")
    assert "HookSpec()" not in runner
    assert "adapter_receipt_validated=True" not in runner
    assert "TRUSTED_RECEIPT" not in runner
    assert "PRIVATE_EXPONENT" not in runner
    registry = json.loads(
        (
            Path(portfolio.__file__).resolve().parents[1]
            / "docs/experiments/lilies-collaboration/portfolio-v04-13-t01h.json"
        ).read_text(encoding="utf-8")
    )
    assert registry["contract_revision"] == 8
    assert registry["selection_contract_revision"] == 3
    assert registry["r8_builder_actor_policy"]["codex_fallback"] == {
        "formal_builder_actor": "codex",
        "builder_actor": "codex_fallback",
        "daemon_access_required": False,
        "daemon_discovery_phase": "zero_duration_not_applicable",
        "explicit_pairing_phase": "zero_duration_not_applicable",
        "requires_bounded_failed_lilies_attempt": True,
        "requires_fresh_empty_application_environment_assignment_session": True,
        "requires_fresh_isolated_public_only_context": True,
    }


def test_actor_profiles_preserve_raw_identity_and_are_mutually_exclusive() -> None:
    manifest = _resolved_manifests()[0]
    scenario = _trusted_scenario(
        manifest,
        offset=0.0,
        attempt_id=str(UUID(int=701_001)),
    )
    lilies = scenario.evidence
    assert portfolio.actor_profile_for_evidence(lilies) == portfolio.LILIES_ACTOR_PROFILE

    fallback = _codex_fallback_evidence(manifest.project_id, lilies)
    profile = portfolio.actor_profile_for_evidence(fallback)
    assert profile == portfolio.CODEX_FALLBACK_ACTOR_PROFILE
    assert fallback.formal_builder_actor == "codex"
    assert fallback.builder_actor == "codex_fallback"

    with pytest.raises(portfolio.PortfolioRerunError, match="must not fabricate daemon"):
        portfolio.actor_profile_for_evidence(
            replace(fallback, daemon_access=lilies.daemon_access)
        )
    with pytest.raises(portfolio.PortfolioRerunError, match="cannot carry"):
        portfolio.actor_profile_for_evidence(
            replace(
                lilies,
                fallback_eligibility=_fallback_eligibility(
                    manifest.project_id,
                    lilies,
                ),
            )
        )


def test_codex_fallback_requires_r8_freshness_and_rejects_historical_codex() -> None:
    manifest = _resolved_manifests()[0]
    scenario = _trusted_scenario(
        manifest,
        offset=0.0,
        attempt_id=str(UUID(int=701_002)),
    )
    fallback = _codex_fallback_evidence(manifest.project_id, scenario.evidence)
    assert portfolio.actor_profile_for_evidence(fallback).builder_actor == "codex_fallback"

    assert fallback.fallback_eligibility is not None
    stale = replace(fallback.fallback_eligibility, contract_revision=7)
    with pytest.raises(portfolio.PortfolioRerunError, match="contract r8"):
        portfolio.actor_profile_for_evidence(replace(fallback, fallback_eligibility=stale))

    with pytest.raises(portfolio.PortfolioRerunError, match="historical Codex"):
        portfolio.actor_profile_for_evidence(
            replace(
                fallback,
                builder_actor="codex",
                fallback_eligibility=None,
            )
        )


def test_codex_fallback_phase_sum_uses_zero_duration_not_applicable_phases() -> None:
    clock = ManualClock()
    timeline = portfolio.PhaseTimeline(monotonic=clock, wall_time=clock.wall)
    timeline.start("environment_bootstrap")
    clock.advance()
    timeline.transition("completed", "daemon_discovery")
    timeline.transition_not_applicable("explicit_pairing")
    timeline.transition_not_applicable("assignment_provision")
    for next_phase in portfolio.PHASES[4:]:
        clock.advance()
        timeline.transition("completed", next_phase)
    clock.advance()
    timeline.finish("completed")
    spans = timeline.spans()

    portfolio.validate_completed_actor_phases(
        portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
        spans,
    )
    assert [(span.phase, span.outcome, span.duration_seconds) for span in spans[1:3]] == [
        ("daemon_discovery", "not_applicable", 0.0),
        ("explicit_pairing", "not_applicable", 0.0),
    ]
    assert math.fsum(span.duration_percentage for span in spans) == 100.0
    assert math.fsum(span.duration_seconds for span in spans) == timeline.total_elapsed_seconds


def test_lilies_plan_and_capabilities_remain_daemon_backed() -> None:
    manifest = _resolved_manifests()[0]
    plan = portfolio.project_plan(manifest)
    assert plan["builder"]["formal_builder_actor"] == "lilies"
    assert plan["builder"]["actor"] == "lilies"
    assert plan["builder"]["max_session_tokens"] == 1_000_000
    assert all(item["applicability"] == "required" for item in plan["phases"])
    capabilities = portfolio.required_adapter_capabilities(manifest)
    assert "daemon_discovery_receipt" in capabilities
    assert "explicit_pairing_receipt" in capabilities


def test_codex_fallback_plan_excludes_daemon_capabilities_and_events() -> None:
    manifest = _resolved_manifests()[0]
    plan = portfolio.project_plan(
        manifest,
        actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )
    assert plan["builder"]["formal_builder_actor"] == "codex"
    assert plan["builder"]["actor"] == "codex_fallback"
    assert plan["builder"]["source_root"] is None
    phases = {item["phase"]: item for item in plan["phases"]}
    for phase in ("daemon_discovery", "explicit_pairing"):
        assert phases[phase]["applicability"] == "not_applicable"
        assert phases[phase]["actions"] == []
    capabilities = portfolio.required_adapter_capabilities(
        manifest,
        portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )
    assert "daemon_discovery_receipt" not in capabilities
    assert "explicit_pairing_receipt" not in capabilities
    fabricated = _event(
        kind="tool_result",
        name="daemon_discovered",
        summary="fabricated daemon event",
        entity_id=str(UUID(int=701_003)),
        receipt_digest=_digest("fabricated-daemon"),
        at=0.0,
    )
    with pytest.raises(portfolio.PortfolioRerunError, match="must not fabricate"):
        portfolio.validate_actor_observable_events(
            portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
            (replace(fabricated, phase="daemon_discovery"),),
        )


def test_codex_usage_never_turns_unavailable_into_zero_or_loses_exact_counts() -> None:
    session_id = str(UUID(int=701_004))
    unavailable = portfolio.CodexTokenUsageEvidence(
        session_id=session_id,
        availability="unavailable",
        authoritative_source="codex_runtime_counter",
        attempted_calls=0,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        reason="runtime did not expose an authoritative counter",
    )
    with pytest.raises(portfolio.PortfolioRerunError, match="never zero estimates"):
        portfolio.validate_codex_token_usage(unavailable)

    exact = replace(
        unavailable,
        availability="exact",
        attempted_calls=2,
        input_tokens=70,
        output_tokens=30,
        total_tokens=100,
        reason=None,
    )
    portfolio.validate_codex_token_usage(exact)
    with pytest.raises(portfolio.PortfolioRerunError, match="became unavailable"):
        portfolio.validate_codex_token_progress(
            exact,
            replace(
                unavailable,
                attempted_calls=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
            ),
        )
    with pytest.raises(portfolio.PortfolioRerunError, match="does not match"):
        portfolio.validate_codex_token_usage(replace(exact, total_tokens=99))
    with pytest.raises(portfolio.PortfolioRerunError, match="invalid counters"):
        portfolio.validate_codex_token_usage(replace(exact, attempted_calls=1.5))


def test_real_adapter_interface_calls_real_project_testkit_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def run_workflow(**kwargs: Any) -> dict[str, Any]:
        calls.append("run_workflow")
        return {"run_id": "run-1", "status": "passed", **kwargs}

    def wait_run(**kwargs: Any) -> dict[str, Any]:
        calls.append("wait_run")
        return {"run_id": kwargs["run_id"], "status": "passed"}

    def run_trace(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append("run_trace")
        return [{"run_id": kwargs["run_id"], "kind": "public"}]

    def write_report(path: Path, report: dict[str, Any]) -> None:
        calls.append("write_report")
        path.write_text(json.dumps(report), encoding="utf-8")

    monkeypatch.setattr(portfolio.real_project_testkit, "run_workflow", run_workflow)
    monkeypatch.setattr(portfolio.real_project_testkit, "wait_run", wait_run)
    monkeypatch.setattr(portfolio.real_project_testkit, "run_trace", run_trace)
    monkeypatch.setattr(portfolio.real_project_testkit, "write_report", write_report)
    adapter = portfolio.RealProjectTestkitAPI.load()
    run = adapter.run_workflow(
        base_url="http://127.0.0.1:9000",
        token="redacted",
        application_id="app-1",
        version=1,
        inputs={},
    )
    assert adapter.wait_run(
        base_url="http://127.0.0.1:9000",
        token="redacted",
        run_id=run["run_id"],
    )["status"] == "passed"
    assert adapter.run_trace(
        base_url="http://127.0.0.1:9000",
        token="redacted",
        run_id=run["run_id"],
    )[0]["kind"] == "public"
    adapter.write_report(tmp_path / "report.json", {"status": "passed"})
    assert calls == ["run_workflow", "wait_run", "run_trace", "write_report"]


def test_empty_or_ambiguous_hooks_and_forbidden_shortcuts_fail() -> None:
    original = portfolio.PROJECT_MANIFESTS["EXP-LILIES-001"]
    bad_shapes = (
        portfolio.HookSpec(route="commands"),
        portfolio.HookSpec(route="broker_capability"),
        portfolio.HookSpec(route="gap"),
        portfolio.HookSpec(
            route="commands",
            commands=(
                portfolio.CommandShape(
                    argv=(
                        ".venv/bin/python",
                        "scripts/experiments/exp_lilies_001/build_workflow_via_api.py",
                    ),
                    moment="before_platform_run",
                    purpose="invalid graph shortcut",
                ),
            ),
        ),
    )
    for shape in bad_shapes:
        hooks = dict(original.hooks)
        hooks["public_debug"] = shape
        errors = portfolio.validate_manifest(
            replace(original, hooks=hooks),
            read_materials=False,
        )
        assert errors
    assert any(
        "forbidden Builder route" in item
        for item in portfolio.validate_manifest(
            replace(
                original,
                hooks={
                    **original.hooks,
                    "public_debug": bad_shapes[-1],
                },
            ),
            read_materials=False,
        )
    )


def test_runtime_trust_root_binds_adapter_semantics_and_capabilities() -> None:
    manifest = _resolved_manifests()[0]
    verifier = _verifier()
    receipt = _adapter_receipt(manifest)
    portfolio.validate_adapter_capability_receipt(manifest, receipt, verifier)
    assert portfolio.project_plan(
        manifest,
        receipt,
        verifier,
    )["real_adapter"]["available"] is True

    corrupted_signature = replace(
        receipt.envelope,
        signature=("A" if receipt.envelope.signature[0] != "A" else "B")
        + receipt.envelope.signature[1:],
    )
    with pytest.raises(portfolio.PortfolioRerunError, match="trust root"):
        portfolio.validate_adapter_capability_receipt(
            manifest,
            replace(receipt, envelope=corrupted_signature),
            verifier,
        )
    with pytest.raises(portfolio.PortfolioRerunError, match="canonical"):
        portfolio.validate_adapter_capability_receipt(
            manifest,
            replace(receipt, capabilities=receipt.capabilities[:-1]),
            verifier,
        )
    tampered_payload = {
        **receipt.envelope.semantic_payload,
        "adapter_id": "untrusted-adapter",
    }
    with pytest.raises(portfolio.PortfolioRerunError, match="canonical"):
        portfolio.validate_adapter_capability_receipt(
            manifest,
            replace(
                receipt,
                envelope=replace(
                    receipt.envelope,
                    semantic_payload=tampered_payload,
                ),
            ),
            verifier,
        )


def test_exact_order_and_runtime_verifier_are_checked_before_execution(
    tmp_path: Path,
) -> None:
    manifests, _, adapters, _ = _trusted_inputs()
    calls: list[str] = []

    def executor(*args: object) -> portfolio.PhaseExecution:
        calls.append("called")
        return portfolio.PhaseExecution(outcome="completed")

    with pytest.raises(portfolio.PortfolioRerunError, match="exact six-project"):
        portfolio.execute_portfolio(
            manifests[:1],
            executor,
            receipt_verifier=_verifier(),
            adapter_receipts=adapters,
            attempt_ledger=_ledger(tmp_path),
        )
    with pytest.raises(portfolio.PortfolioRerunError, match="exact six-project"):
        portfolio.execute_portfolio(
            tuple(reversed(manifests)),
            executor,
            receipt_verifier=_verifier(),
            adapter_receipts=adapters,
            attempt_ledger=_ledger(tmp_path),
        )

    class AcceptAllVerifier(portfolio.ReceiptTrustVerifier):
        def verify_envelope(self, envelope: portfolio.SignedReceiptEnvelope) -> bool:
            return True

    with pytest.raises(portfolio.PortfolioRerunError, match="task-author configured"):
        portfolio.execute_portfolio(
            manifests,
            executor,
            receipt_verifier=AcceptAllVerifier(TEST_TRUST_ROOT),
            adapter_receipts=adapters,
            attempt_ledger=_ledger(tmp_path),
        )
    assert calls == []


def test_token_attempt_settlement_and_hard_stop_fence_fail_closed() -> None:
    session_id = _uuid(1, 700)
    at_cap = _checkpoint(
        session_id,
        portfolio.MAX_SESSION_TOKENS,
        calls=2,
    )
    portfolio.validate_token_checkpoint(at_cap)
    portfolio.validate_token_progress(at_cap, at_cap)
    with pytest.raises(portfolio.PortfolioRerunError, match="attempt"):
        portfolio.validate_token_checkpoint(
            replace(at_cap, attempted_calls=3)
        )
    with pytest.raises(portfolio.PortfolioRerunError, match="after the token hard stop"):
        portfolio.validate_token_checkpoint(
            replace(at_cap, post_hard_stop_attempts=1)
        )
    with pytest.raises(portfolio.PortfolioRerunError, match="after the token hard stop"):
        portfolio.validate_token_progress(
            at_cap,
            replace(
                at_cap,
                attempted_calls=3,
                recorded_calls=3,
                model_call_attempt_receipt_ids=(
                    *at_cap.model_call_attempt_receipt_ids,
                    "post-fence-call",
                ),
                model_call_receipt_ids=(
                    *at_cap.model_call_receipt_ids,
                    "post-fence-call",
                ),
                cap_reached_at_receipt_id="post-fence-call",
                hard_stop_fence_receipt_id="post-fence-call",
                groups=(
                    replace(
                        at_cap.groups[0],
                        recorded_calls=3,
                    ),
                ),
            ),
        )


def test_signed_execution_semantics_and_p6_stage_bindings() -> None:
    manifest = _resolved_manifests()[-1]
    scenario = _trusted_scenario(
        manifest,
        offset=0.0,
        attempt_id=_attempt_ids()[5],
    )
    verifier = _verifier()
    evidence = verifier.derive_execution_evidence(manifest, scenario.envelopes)
    checkpoint = scenario.phase_results["cleanup_reporting"].token_checkpoint
    assert checkpoint is not None
    portfolio.validate_execution_evidence(
        manifest,
        evidence,
        checkpoint,
        scenario.envelopes,
        verifier,
    )
    lifecycle = evidence.ml_lifecycle
    assert lifecycle is not None
    swapped = replace(
        lifecycle,
        fit_train_receipt_digest=lifecycle.deployment_receipt_digest,
        deployment_receipt_digest=lifecycle.fit_train_receipt_digest,
    )
    tampered = _replace_scenario_root(
        scenario,
        replace(evidence, ml_lifecycle=swapped),
    )
    with pytest.raises(portfolio.PortfolioRerunError, match="ML fit_train"):
        portfolio.validate_execution_evidence(
            manifest,
            tampered.evidence,
            checkpoint,
            tampered.envelopes,
            verifier,
        )


def test_six_projects_complete_in_order_with_positive_exact_timing(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    clock = ManualClock()
    executor = ScenarioExecutor(scenarios, clock)
    ledger = _ledger(tmp_path)
    reports = portfolio.execute_portfolio(
        manifests,
        executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=ledger,
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )
    assert [report.project_id for report in reports] == list(portfolio.PROJECT_MANIFESTS)
    assert all(report.status == "completed" for report in reports)
    assert executor.calls == [
        (manifest.project_id, phase)
        for manifest in manifests
        for phase in portfolio.PHASES
    ]
    for report in reports:
        assert len(report.phases) == 8
        assert all(span.duration_seconds > 0 for span in report.phases)
        assert math.fsum(span.duration_percentage for span in report.phases) == 100.0
        assert math.isclose(
            math.fsum(span.duration_seconds for span in report.phases),
            report.total_elapsed_seconds,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        expected = portfolio._canonical_digest(
            _report_body(report, scenarios[report.project_id])
        )
        assert report.serialized_report_body_digest == expected
    rows = ledger.snapshot()["attempts"]
    assert len(rows) == 6
    assert all(row["status"] == "completed" for row in rows)
    assert [row["project_id"] for row in rows] == list(portfolio.PROJECT_MANIFESTS)
    assert all(row["report_body"]["observable_events"] for row in rows)
    assert all(row["report_body"]["signed_receipts"] for row in rows)
    assert all(
        portfolio._canonical_digest(row["report_body"]) == row["report_digest"]
        for row in rows
    )


def test_six_codex_fallback_projects_complete_with_unavailable_usage(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _fallback_inputs()
    clock = ManualClock()
    executor = ScenarioExecutor(
        scenarios,
        clock,
        phase_durations={"assignment_provision": 3.0},
    )
    ledger = _ledger(tmp_path)
    _seed_bounded_lilies_failure(ledger)

    reports = portfolio.execute_portfolio(
        manifests,
        executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=ledger,
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
        actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )

    assert len(reports) == 6
    assert all(report.status == "completed" for report in reports)
    assert executor.calls == [
        (manifest.project_id, phase)
        for manifest in manifests
        for phase in portfolio.PHASES
        if phase not in portfolio.ACTOR_INAPPLICABLE_PHASES
    ]
    for report in reports:
        assert report.formal_builder_actor == "codex"
        assert report.builder_actor == "codex_fallback"
        assert report.final_token_checkpoint is None
        usage = report.final_codex_token_usage
        assert usage is not None
        assert usage.availability == "unavailable"
        assert (
            usage.attempted_calls,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        ) == (None, None, None, None)
        assert [
            (span.phase, span.outcome, span.duration_seconds)
            for span in report.phases[1:3]
        ] == [
            ("daemon_discovery", "not_applicable", 0.0),
            ("explicit_pairing", "not_applicable", 0.0),
        ]
        assert math.fsum(span.duration_percentage for span in report.phases) == 100.0
        assert math.isclose(
            math.fsum(span.duration_seconds for span in report.phases),
            report.total_elapsed_seconds,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        assert report.serialized_report_body_digest == portfolio._canonical_digest(
            _report_body(report, scenarios[report.project_id])
        )

    snapshot = ledger.snapshot()
    assert len(snapshot["fallback_prerequisites"]) == 6
    assert snapshot["attempts"][0]["formal_builder_actor"] == "lilies"
    assert all(
        row["formal_builder_actor"] == "codex"
        and row["builder_actor"] == "codex_fallback"
        and row["status"] == "completed"
        for row in snapshot["attempts"][1:]
    )


def test_codex_fallback_fails_without_persisted_lilies_terminal_provenance(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _fallback_inputs(epoch=4)
    clock = ManualClock()
    report = portfolio.execute_portfolio(
        manifests,
        ScenarioExecutor(
            scenarios,
            clock,
            phase_durations={"assignment_provision": 3.0},
        ),
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=_ledger(tmp_path),
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
        actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )[0]

    assert report.status == "failed"
    assert "persisted failed Lilies attempt" in f"{report.failure} {report.cleanup_failure}"


def test_codex_fallback_rejects_incomplete_lilies_failure_report() -> None:
    incomplete = {
        "attempt_id": str(UUID(int=777_001)),
        "project_id": "EXP-LILIES-001",
        "formal_builder_actor": "lilies",
        "builder_actor": "lilies",
        "status": "failed",
        "report_body": {
            "formal_builder_actor": "lilies",
            "builder_actor": "lilies",
            "status": "failed",
            "failure": "bounded upstream failure",
        },
        "identity_bindings": {},
    }
    incomplete["report_digest"] = portfolio._canonical_digest(
        incomplete["report_body"]
    )

    with pytest.raises(portfolio.PortfolioRerunError, match="report is incomplete"):
        portfolio.validate_bounded_lilies_failure_report(incomplete)


def test_bounded_lilies_prerequisite_requires_cap_exact_usage_and_eight_phases() -> None:
    report = _bounded_lilies_report_body()
    session_id = report["final_token_checkpoint"]["session_id"]

    def prior_row(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": str(UUID(int=777_001)),
            "project_id": "EXP-LILIES-001",
            "started_at": _iso(-3.0),
            "finished_at": _iso(-2.0),
            "formal_builder_actor": "lilies",
            "builder_actor": "lilies",
            "status": "failed",
            "report_body": body,
            "report_digest": portfolio._canonical_digest(body),
            "identity_bindings": {"session_id": session_id},
        }

    portfolio.validate_bounded_lilies_failure_report(prior_row(report))

    bad_cap = json.loads(json.dumps(report))
    bad_cap["max_session_tokens"] = 999_999
    with pytest.raises(portfolio.PortfolioRerunError, match="report is incomplete"):
        portfolio.validate_bounded_lilies_failure_report(prior_row(bad_cap))

    incomplete_usage = json.loads(json.dumps(report))
    incomplete_usage["final_token_checkpoint"]["unknown_calls"] = 1
    with pytest.raises(portfolio.PortfolioRerunError, match="accounting is incomplete"):
        portfolio.validate_bounded_lilies_failure_report(prior_row(incomplete_usage))

    missing_phase = json.loads(json.dumps(report))
    missing_phase["phases"].pop()
    with pytest.raises(portfolio.PortfolioRerunError, match="all eight phases"):
        portfolio.validate_bounded_lilies_failure_report(prior_row(missing_phase))


def test_codex_fallback_revalidates_persisted_signed_lilies_terminal(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _fallback_inputs(epoch=5)
    clock = ManualClock()
    ledger = _ledger(tmp_path)
    _seed_bounded_lilies_failure(ledger)
    base_executor = ScenarioExecutor(
        scenarios,
        clock,
        phase_durations={"assignment_provision": 3.0},
    )
    tampered = False

    def executor(
        manifest: portfolio.ProjectManifest,
        phase: str,
        plan: Mapping[str, Any],
    ) -> portfolio.PhaseExecution:
        nonlocal tampered
        if phase == "builder_execution" and not tampered:
            payload = ledger.snapshot()
            payload["signed_terminal_attempts"][0]["envelope"]["semantic_payload"][
                "status"
            ] = "completed"
            ledger._persist(payload)
            tampered = True
        return base_executor(manifest, phase, plan)

    report = portfolio.execute_portfolio(
        manifests,
        executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=ledger,
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
        actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )[0]

    assert report.status == "failed"
    assert "signed receipt payload digest" in f"{report.failure} {report.cleanup_failure}"


def test_codex_fallback_rechecks_prerequisite_chronology_at_final_validation(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _fallback_inputs(epoch=6)
    clock = ManualClock()
    ledger = _ledger(tmp_path)
    _seed_bounded_lilies_failure(ledger)
    base_executor = ScenarioExecutor(
        scenarios,
        clock,
        phase_durations={"assignment_provision": 3.0},
    )
    tampered = False

    def executor(
        manifest: portfolio.ProjectManifest,
        phase: str,
        plan: Mapping[str, Any],
    ) -> portfolio.PhaseExecution:
        nonlocal tampered
        if phase == "builder_execution" and not tampered:
            payload = ledger.snapshot()
            payload["attempts"][-1]["started_at"] = _iso(-1.5)
            ledger._persist(payload)
            tampered = True
        return base_executor(manifest, phase, plan)

    report = portfolio.execute_portfolio(
        manifests,
        executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=ledger,
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
        actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )[0]

    assert report.status == "failed"
    assert "not prior to the Codex attempt" in f"{report.failure} {report.cleanup_failure}"


def test_wall_time_not_monotonic_clock_is_the_phase_denominator(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    clock = ManualClock()
    reports = portfolio.execute_portfolio(
        manifests,
        ScenarioExecutor(scenarios, clock),
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=_ledger(tmp_path),
        monotonic=lambda: 0.0,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )
    assert len(reports) == 6
    assert all(report.status == "completed" for report in reports)
    assert all(
        span.duration_seconds == 1.0
        for report in reports
        for span in report.phases
    )


def test_timing_exception_runs_cleanup_once_and_persists_failed_report(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    clock = ManualClock()
    base_executor = ScenarioExecutor(scenarios, clock)
    wall_calls = 0

    def broken_wall() -> datetime:
        nonlocal wall_calls
        wall_calls += 1
        if wall_calls == 2:
            raise KeyboardInterrupt
        return clock.wall()

    reports = portfolio.execute_portfolio(
        manifests,
        base_executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=_ledger(tmp_path),
        monotonic=clock,
        wall_time=broken_wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )
    assert len(reports) == 1
    assert reports[0].status == "failed"
    assert "KeyboardInterrupt" in (reports[0].failure or "")
    assert [phase for _, phase in base_executor.calls].count("cleanup_reporting") == 1
    assert len(reports[0].phases) < 8
    assert _ledger(tmp_path).snapshot()["attempts"][0]["status"] == "failed"


def test_phase_and_cleanup_base_exceptions_are_both_persisted_once(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    clock = ManualClock()
    calls: list[str] = []

    def executor(
        manifest: portfolio.ProjectManifest,
        phase: str,
        plan: Mapping[str, Any],
    ) -> portfolio.PhaseExecution:
        calls.append(phase)
        clock.advance()
        if phase == "builder_execution":
            raise KeyboardInterrupt
        if phase == "cleanup_reporting":
            raise SystemExit
        return scenarios[manifest.project_id].phase_results[phase]

    report = portfolio.execute_portfolio(
        manifests,
        executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=_ledger(tmp_path),
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )[0]
    assert report.status == "failed"
    assert "KeyboardInterrupt" in (report.failure or "")
    assert "SystemExit" in (report.cleanup_failure or "")
    assert calls.count("cleanup_reporting") == 1
    row = _ledger(tmp_path).snapshot()["attempts"][0]
    assert row["status"] == "failed"
    assert "SystemExit" in row["cleanup_failure"]


def test_terminal_validation_precedes_the_full_report_digest(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    first_id = manifests[0].project_id
    first = scenarios[first_id]
    cleanup = first.phase_results["cleanup_reporting"]
    broken_cleanup = replace(
        cleanup,
        signed_receipts=tuple(
            envelope
            for envelope in cleanup.signed_receipts
            if envelope.semantic_type != "cleanup"
        ),
    )
    broken_results = {**first.phase_results, "cleanup_reporting": broken_cleanup}
    broken_envelopes = tuple(
        envelope
        for phase in portfolio.PHASES
        for envelope in broken_results[phase].signed_receipts
    )
    scenarios[first_id] = replace(
        first,
        envelopes=broken_envelopes,
        phase_results=broken_results,
    )
    clock = ManualClock()
    report = portfolio.execute_portfolio(
        manifests,
        ScenarioExecutor(scenarios, clock),
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=_ledger(tmp_path),
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )[0]
    assert report.status == "failed"
    assert report.phases[-1].outcome == "failed"
    assert "signed attempt/application receipt" in (report.cleanup_failure or "")
    assert report.serialized_report_body_digest == portfolio._canonical_digest(
        _report_body(report, scenarios[first_id])
    )


def test_report_serialization_base_exception_is_persisted_after_one_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    clock = ManualClock()
    executor = ScenarioExecutor(scenarios, clock)

    def interrupted_serialization(value: Mapping[str, Any]) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        portfolio,
        "_serialize_report_body_digest",
        interrupted_serialization,
    )
    report = portfolio.execute_portfolio(
        manifests,
        executor,
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=_ledger(tmp_path),
        monotonic=clock,
        wall_time=clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )[0]
    assert report.status == "failed"
    assert "KeyboardInterrupt" in (report.cleanup_failure or "")
    assert [phase for _, phase in executor.calls].count("cleanup_reporting") == 1
    assert _ledger(tmp_path).snapshot()["attempts"][0]["status"] == "failed"
    assert report.serialized_report_body_digest == portfolio._canonical_digest(
        _report_body(report, scenarios[report.project_id])
    )


def test_durable_ledger_rejects_reused_credential_and_receipt_history(
    tmp_path: Path,
) -> None:
    manifests, scenarios, adapters, attempt_ids = _trusted_inputs()
    ledger = _ledger(tmp_path)
    first_clock = ManualClock()
    first = portfolio.execute_portfolio(
        manifests,
        ScenarioExecutor(scenarios, first_clock),
        receipt_verifier=_verifier(),
        adapter_receipts=adapters,
        attempt_ledger=ledger,
        monotonic=first_clock,
        wall_time=first_clock.wall,
        attempt_id_factory=_attempt_factory(attempt_ids),
    )
    assert all(report.status == "completed" for report in first)

    fresh_adapters = {
        manifest.project_id: _adapter_receipt(manifest, salt="second")
        for manifest in manifests
    }
    _, second_scenarios, _, second_attempt_ids = _trusted_inputs(epoch=2)
    second_clock = ManualClock()
    second = portfolio.execute_portfolio(
        manifests,
        ScenarioExecutor(second_scenarios, second_clock),
        receipt_verifier=_verifier(),
        adapter_receipts=fresh_adapters,
        attempt_ledger=ledger,
        monotonic=second_clock,
        wall_time=second_clock.wall,
        attempt_id_factory=_attempt_factory(second_attempt_ids),
    )
    assert len(second) == 1
    assert second[0].status == "failed"
    reasons = f"{second[0].failure} {second[0].cleanup_failure}"
    assert "durable freshness collision" in reasons
    rows = ledger.snapshot()["attempts"]
    assert len(rows) == 7
    assert rows[-1]["identity_bindings"]["application_id"] == (
        scenarios[manifests[0].project_id].evidence.application_id
    )
    assert "environment_generation" in rows[-1]["identity_bindings"]


def test_attempt_ledger_is_secure_and_records_running_before_side_effects(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _uuid(1, 999)
    ledger.start_attempt(
        attempt_id=attempt_id,
        project_id="EXP-LILIES-001",
        manifest_revision=1,
        started_at=_iso(0),
    )
    row = ledger.snapshot()["attempts"][0]
    assert row["status"] == "running"
    assert ledger.path.stat().st_mode & 0o077 == 0
    assert ledger.bind_identities(
        attempt_id,
        {"task_credential_digest": _digest("credential")},
    ) == ()
    report_body = {"status": "failed", "events": []}
    report_digest = portfolio._canonical_digest(report_body)
    ledger.finalize_attempt(
        attempt_id=attempt_id,
        status_value="failed",
        finished_at=_iso(1),
        failure="controlled failure",
        cleanup_failure=None,
        report_digest=report_digest,
        report_body=report_body,
    )
    with pytest.raises(portfolio.PortfolioRerunError, match="more than once"):
        ledger.finalize_attempt(
            attempt_id=attempt_id,
            status_value="failed",
            finished_at=_iso(2),
            failure="again",
            cleanup_failure=None,
            report_digest=report_digest,
            report_body=report_body,
        )
    second_attempt = _uuid(1, 1_000)
    reused = {
        "application_id": _uuid(1, 1_001),
        "environment_generation": "environment-generation-one",
        "assignment_id": _uuid(1, 1_002),
        "session_id": _uuid(1, 1_003),
    }
    first_attempt = _uuid(1, 1_004)
    separate = portfolio.AttemptLedger(tmp_path / "freshness.json")
    separate.start_attempt(
        attempt_id=first_attempt,
        project_id="EXP-LILIES-001",
        manifest_revision=1,
        started_at=_iso(0),
    )
    assert separate.bind_identities(first_attempt, reused) == ()
    separate.finalize_attempt(
        attempt_id=first_attempt,
        status_value="failed",
        finished_at=_iso(1),
        failure="controlled",
        cleanup_failure=None,
        report_digest=report_digest,
        report_body=report_body,
    )
    separate.start_attempt(
        attempt_id=second_attempt,
        project_id="EXP-LILIES-001",
        manifest_revision=1,
        started_at=_iso(2),
    )
    assert separate.bind_identities(second_attempt, reused) == tuple(sorted(reused))


def test_cli_returns_nonzero_when_execution_is_not_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert portfolio.main(["--validate"]) == 2
    validation = json.loads(capsys.readouterr().out)
    assert validation["structurally_valid"] is True
    assert validation["execution_ready"] is False
    assert portfolio.main(["--dry-run"]) == 2
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["execution_performed"] is False
    assert portfolio.main(["--list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["project_order"] == list(portfolio.PROJECT_MANIFESTS)


def test_public_inputs_and_observable_events_reject_hidden_shapes() -> None:
    manifest = portfolio.PROJECT_MANIFESTS["EXP-LILIES-001"]
    prompt = portfolio.build_public_requirement(manifest)
    assert "You are Lilies, the Builder" in prompt
    assert "Codex acting as the r8 fallback Builder" not in prompt
    assert "--- PUBLIC MATERIAL: fixtures/" not in prompt
    fallback_prompt = portfolio.build_public_requirement(
        manifest,
        actor_profile=portfolio.CODEX_FALLBACK_ACTOR_PROFILE,
    )
    assert "Codex acting as the r8 fallback Builder" in fallback_prompt
    assert "raw protocol identity remains codex" in fallback_prompt
    assert "Do not claim to be Lilies" in fallback_prompt
    assert "You are Lilies, the Builder" not in fallback_prompt
    assert "--- PUBLIC MATERIAL: fixtures/" not in fallback_prompt
    event = _event(
        kind="tool_result",
        name="tool_completed",
        summary="raw output payload exposed",
        entity_id="entity-1",
        receipt_digest=_digest("event"),
        at=0.5,
    )
    with pytest.raises(portfolio.PortfolioRerunError, match="non-public"):
        portfolio._validated_observable_event(
            event,
            phase="builder_execution",
            previous_at=None,
        )


def test_default_validation_truthfully_explains_remaining_adapter_gap() -> None:
    payload = portfolio._validation_payload(tuple(portfolio.PROJECT_MANIFESTS.values()))
    assert payload["structurally_valid"] is True
    assert payload["execution_ready"] is False
    real_adapter_gaps = [
        item for item in payload["capability_gaps"] if item["hook"] == "real_adapter"
    ]
    assert len(real_adapter_gaps) == 6
    assert all("no real platform/daemon/process adapter" in item["gap"].lower() for item in real_adapter_gaps)
