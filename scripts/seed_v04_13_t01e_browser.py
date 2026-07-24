#!/usr/bin/env python3
"""Seed a controlled V04-13-T01E collaboration browser fixture.

This is deliberately a local UI fixture, not production acceptance evidence.  It
uses the public collaboration state-machine service against a real SQLite store;
it never edits state tables directly and never prints the issued channel secret.

The collaboration contract permits exactly one channel per assignment and
Lilies session.  Consequently the fixture creates one channel bound to the real
T01E assignment and four independent report causal chains inside that channel.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.collaboration_models import (  # noqa: E402
    ApprovalDecisionRequest,
    CollaborationReportPayload,
    DeveloperResponsePayload,
    DeveloperResponseRequest,
    LeaseAcquireRequest,
    LiliesReprobeResultPayload,
    LiliesReprobeResultRequest,
    ReportSubmitRequest,
    SenderRole,
    VerificationClaimPayload,
    VerificationClaimRequest,
    VerificationResultPayload,
    VerificationResultRequest,
)
from agent_platform.collaboration_service import (  # noqa: E402
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.collaboration_storage import (  # noqa: E402
    CollaborationNotFound as StorageNotFound,
)
from agent_platform.collaboration_storage import CollaborationStore  # noqa: E402
from agent_platform.lilies_models import (  # noqa: E402
    AssignmentMode,
    CollaborationScope,
)


DEFAULT_DATABASE = ROOT / "data" / "agent_platform.db"
APPLICATION_ID = UUID("93a0b339-29ab-4e1e-a091-347ce88a0c24")
ASSIGNMENT_ID = UUID("079a3b03-080f-5b8d-8891-153a919d8f5e")
SESSION_ID = UUID("cb283cf7-4c1f-5bd9-a943-71973ad61edd")
TASK_ID = "EXP-LILIES-T01E-BROWSER-FIXTURE"
TASK_REVISION = 1
FIXTURE_TIME = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
FIXTURE_COMMIT = "c" * 40
MAX_REPORT_EVIDENCE_ROUNDS = 4
SCENARIOS = (
    "verification_failed",
    "awaiting_user_review",
    "needs_more_evidence",
    "developer_response",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_uuid(kind: str, marker: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"lilies:v0.4.13:t01e:browser-fixture:{kind}:{marker}",
    )


def _digest(marker: str) -> str:
    return f"sha256:{hashlib.sha256(marker.encode()).hexdigest()}"


def _evidence(
    marker: str,
    *,
    kind: str = "trace",
    digest: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": f"evidence:t01e-browser-fixture:{marker}",
        "kind": kind,
        "digest": digest or _digest(f"evidence:{marker}"),
        "media_type": "application/json",
        "label": label
        or "Controlled T01E browser fixture evidence; not a production result",
        "captured_at": FIXTURE_TIME.isoformat(),
    }


def _report_payload(scenario: str) -> CollaborationReportPayload:
    evidence = _evidence(
        f"{scenario}:report",
        label=(
            f"Controlled T01E browser fixture trace for {scenario}; "
            "not production evidence"
        ),
    )
    display = {
        "verification_failed": "independent verification failure",
        "awaiting_user_review": "capability approval awaiting user review",
        "needs_more_evidence": "capability report requiring more evidence",
        "developer_response": "substantive DeveloperResponse arrival",
    }[scenario]
    return CollaborationReportPayload.model_validate(
        {
            "schema_version": "1.0",
            "report_id": str(_stable_uuid("report", scenario)),
            "category": "platform_capability_gap",
            "phase": "preflight",
            "severity": "blocking",
            "summary": f"T01E browser fixture — {display}",
            "original_goal": (
                "Exercise the collaboration Studio presentation with a controlled "
                "fixture; this text is not a production capability claim."
            ),
            "requirement_digest": _digest(f"requirement:{scenario}"),
            "platform_contract_digest": _digest("fixture-contract-before"),
            "manuals_checked": [
                {
                    "manual_id": "manual:t01e-browser-fixture",
                    "version": "v0.4.13-fixture",
                    "digest": _digest("fixture-manual"),
                }
            ],
            "attempted_routes": [
                {
                    "attempt_id": str(_stable_uuid("attempt", scenario)),
                    "route": "controlled collaboration Studio browser scenario",
                    "input_digest": _digest(f"attempt-input:{scenario}"),
                    "outcome": (
                        "Synthetic state-machine input prepared only for local UI "
                        "inspection."
                    ),
                    "evidence_refs": [evidence],
                    "attempted_at": FIXTURE_TIME.isoformat(),
                }
            ],
            "expected": f"Studio clearly presents the {display} scenario.",
            "actual": (
                "The controlled fixture begins from a simulated missing capability "
                "so the requested lifecycle state can be inspected."
            ),
            "missing_contract": (
                "A simulated typed capability contract used only to render this "
                "browser fixture."
            ),
            "blocking_scope": (
                "Only this controlled fixture chain is marked blocking; it does not "
                "describe production behavior."
            ),
            "independent_work": [
                "Inspect the other independent fixture report causal chains."
            ],
            "workaround_considered": [
                "Use deterministic backend tests without opening the browser."
            ],
            "workaround_loss": (
                "Backend-only evidence cannot demonstrate the Studio interaction."
            ),
            "requested_outcome": (
                f"Render the controlled {scenario} lifecycle without treating it as "
                "production evidence."
            ),
            "confidence": 1.0,
            "secret_redactions": ["fixture contains no user or provider secrets"],
            "evidence_refs": [evidence],
        }
    )


def _developer_response(scenario: str) -> DeveloperResponsePayload:
    evidence = _evidence(
        f"{scenario}:developer-response",
        kind="test_run",
        label=(
            "Controlled fixture-only DeveloperResponse test record; "
            "not a real implementation result"
        ),
    )
    return DeveloperResponsePayload.model_validate(
        {
            "schema_version": "1.0",
            "response_id": str(_stable_uuid("developer-response", scenario)),
            "outcome": "implemented",
            "commit_sha": FIXTURE_COMMIT,
            "generic_capability_changes": [
                "Fixture-only generic change used to exercise the T01E Studio; "
                "no production implementation is claimed."
            ],
            "new_contract_digest": _digest("fixture-contract-after"),
            "tests_run": [
                {
                    "test_id": f"test:t01e-browser-fixture:{scenario}",
                    "command": "fixture-only:no-command-executed",
                    "exit_code": 0,
                    "summary": (
                        "Synthetic passing result for browser presentation only; "
                        "no production test was executed."
                    ),
                    "evidence_ref": evidence,
                }
            ],
            "browser_or_live_evidence": [],
            "known_limits": [
                "All response data in this chain is a controlled browser fixture."
            ],
            "reprobe_steps": [
                {
                    "order": 1,
                    "action": "Open the selected fixture report in collaboration Studio.",
                    "expected": "The response and its fixture warning are visible.",
                }
            ],
        }
    )


def _reprobe_payload() -> LiliesReprobeResultPayload:
    return LiliesReprobeResultPayload.model_validate(
        {
            "schema_version": "1.0",
            "reprobe_id": str(
                _stable_uuid("reprobe", "verification_failed")
            ),
            "outcome": "lilies_verified",
            "contract_digest": _digest("fixture-contract-after"),
            "steps": [
                {
                    "order": 1,
                    "action": (
                        "Open the selected fixture report in collaboration Studio."
                    ),
                    "expected": (
                        "The response and its fixture warning are visible."
                    ),
                }
            ],
            "expected": "The controlled reprobe reaches the verifier handoff.",
            "actual": (
                "The fixture reprobe reached the verifier handoff; this is not a "
                "production probe."
            ),
            "evidence_refs": [
                _evidence(
                    "verification_failed:reprobe",
                    kind="test_run",
                    digest=_digest("fixture-contract-after"),
                )
            ],
        }
    )


def _claim_payload(
    report_id: UUID,
    *,
    draft_revision: int,
    content_hash: str,
) -> VerificationClaimPayload:
    return VerificationClaimPayload.model_validate(
        {
            "schema_version": "1.0",
            "claim_id": str(_stable_uuid("claim", "verification_failed")),
            "application_id": str(APPLICATION_ID),
            "draft_revision": draft_revision,
            "content_hash": content_hash,
            "published_version": 1,
            "test_run_ids": ["test-run:t01e-browser-fixture:verification"],
            "business_run_ids": ["business-run:t01e-browser-fixture:verification"],
            "artifact_refs": [
                _evidence(
                    "verification_failed:artifact",
                    kind="artifact",
                )
            ],
            "host_receipt_refs": [
                _evidence(
                    "verification_failed:host-receipt",
                    kind="host_receipt",
                )
            ],
            "resolved_report_ids": [str(report_id)],
            "remaining_limits": [
                "This claim exists only to present the controlled failure state."
            ],
            "claim": "ready_for_independent_verification",
        }
    )


def _verification_failure() -> VerificationResultPayload:
    difference_evidence = _evidence(
        "verification_failed:difference",
        kind="browser",
        label=(
            "Controlled expected/actual browser fixture difference; "
            "not production evidence"
        ),
    )
    return VerificationResultPayload.model_validate(
        {
            "schema_version": "1.0",
            "verification_id": str(
                _stable_uuid("verification", "verification_failed")
            ),
            "verdict": "verification_failed",
            "oracle_digest": _digest("fixture-verifier-oracle"),
            "differences": [
                {
                    "check_id": "check:t01e-browser-fixture:refresh-persistence",
                    "expected": (
                        "The capability approval card remains visible after refresh."
                    ),
                    "actual": (
                        "The controlled fixture oracle reports that the approval card "
                        "is missing after refresh."
                    ),
                    "evidence_refs": [difference_evidence],
                }
            ],
            "evidence_refs": [difference_evidence],
        }
    )


async def _get_report_or_none(
    store: CollaborationStore,
    report_id: UUID,
) -> dict[str, Any] | None:
    try:
        return await store.get_report(report_id)
    except StorageNotFound:
        return None


async def _get_claim_or_none(
    store: CollaborationStore,
    claim_id: UUID,
) -> dict[str, Any] | None:
    try:
        return await store.get_claim(claim_id)
    except StorageNotFound:
        return None


def _current_draft_or_fixture(database: Path) -> dict[str, Any]:
    """Read the workflow binding needed by the public claim contract.

    This is intentionally read-only.  All collaboration mutations still flow
    through ``CollaborationService`` and ``CollaborationStore``.
    """

    with sqlite3.connect(database) as connection:
        managed = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='application_drafts'
            """
        ).fetchone()
        current = (
            connection.execute(
                """
                SELECT revision,content_hash FROM application_drafts
                WHERE application_id=?
                """,
                (str(APPLICATION_ID),),
            ).fetchone()
            if managed is not None
            else None
        )
    if current is None:
        return {
            "revision": 0,
            "content_hash": _digest("fixture-draft"),
        }
    raw_hash = str(current[1])
    return {
        "revision": int(current[0]),
        "content_hash": (
            raw_hash if raw_hash.startswith("sha256:") else f"sha256:{raw_hash}"
        ),
    }


async def _submit_report_if_missing(
    *,
    service: CollaborationService,
    store: CollaborationStore,
    lilies: CollaborationPrincipal,
    channel_id: UUID,
    scenario: str,
) -> dict[str, Any]:
    payload = _report_payload(scenario)
    existing = await _get_report_or_none(store, payload.report_id)
    if existing is not None:
        if UUID(str(existing["channel_id"])) != channel_id:
            raise RuntimeError(
                f"fixture report {payload.report_id} belongs to another channel"
            )
        return existing
    channel = await store.get_channel(channel_id)
    return await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key=f"t01e-browser-fixture-report-{scenario}-v1",
            expected_channel_revision=int(channel["revision"]),
            report=payload,
        ),
    )


async def _approve_report(
    *,
    service: CollaborationService,
    store: CollaborationStore,
    user: CollaborationPrincipal,
    report_id: UUID,
    scenario: str,
) -> dict[str, Any]:
    report = await store.get_report(report_id)
    if report["status"] != "awaiting_user_review":
        return report
    return await service.decide_report(
        principal=user,
        report_id=report_id,
        request=ApprovalDecisionRequest(
            idempotency_key=f"t01e-browser-fixture-approve-{scenario}-v1",
            expected_report_revision=int(report["revision"]),
            decision="approve",
        ),
    )


async def _advance_to_developer_response(
    *,
    service: CollaborationService,
    store: CollaborationStore,
    user: CollaborationPrincipal,
    developer: CollaborationPrincipal,
    report_id: UUID,
    scenario: str,
) -> dict[str, Any]:
    await _approve_report(
        service=service,
        store=store,
        user=user,
        report_id=report_id,
        scenario=scenario,
    )
    report = await store.get_report(report_id)
    lease: dict[str, Any] | None = None
    if report["status"] == "approved_for_codex":
        lease = await service.acquire_developer_lease(
            principal=developer,
            report_id=report_id,
            request=LeaseAcquireRequest(
                idempotency_key=f"t01e-browser-fixture-lease-{scenario}-v1",
                expected_report_revision=int(report["revision"]),
                owner_id=developer.sender_id,
                ttl_seconds=900,
            ),
        )
        report = await store.get_report(report_id)
    elif report["status"] == "implementing":
        lease = await store.get_active_lease(report_id, now=_utc_now())
        report = await store.get_report(report_id)
        if lease is None and report["status"] == "approved_for_codex":
            return await _advance_to_developer_response(
                service=service,
                store=store,
                user=user,
                developer=developer,
                report_id=report_id,
                scenario=scenario,
            )
    if report["status"] == "implementing":
        if lease is None:
            raise RuntimeError(f"fixture report {report_id} has no active lease")
        await service.submit_developer_response(
            principal=developer,
            report_id=report_id,
            request=DeveloperResponseRequest(
                idempotency_key=(
                    f"t01e-browser-fixture-developer-response-{scenario}-v1"
                ),
                lease_id=UUID(str(lease["lease_id"])),
                lease_owner_id=developer.sender_id,
                expected_report_revision=int(report["revision"]),
                response=_developer_response(scenario),
            ),
        )
    return await store.get_report(report_id)


async def _seed_verification_failure(
    *,
    service: CollaborationService,
    store: CollaborationStore,
    lilies: CollaborationPrincipal,
    user: CollaborationPrincipal,
    developer: CollaborationPrincipal,
    verifier: CollaborationPrincipal,
    channel_id: UUID,
    draft_state: dict[str, Any],
) -> dict[str, Any]:
    scenario = "verification_failed"
    report = await _submit_report_if_missing(
        service=service,
        store=store,
        lilies=lilies,
        channel_id=channel_id,
        scenario=scenario,
    )
    report_id = UUID(str(report["report_id"]))
    report = await _advance_to_developer_response(
        service=service,
        store=store,
        user=user,
        developer=developer,
        report_id=report_id,
        scenario=scenario,
    )
    if report["status"] == "ready_for_lilies_verification":
        await service.submit_lilies_reprobe(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=LiliesReprobeResultRequest(
                idempotency_key="t01e-browser-fixture-reprobe-verification-failed-v1",
                expected_report_revision=int(report["revision"]),
                result=_reprobe_payload(),
            ),
        )
        report = await store.get_report(report_id)

    claim_id = _stable_uuid("claim", scenario)
    claim = await _get_claim_or_none(store, claim_id)
    if report["status"] == "lilies_verified" and claim is None:
        channel = await store.get_channel(channel_id)
        claim = await service.submit_verification_claim(
            principal=lilies,
            channel_id=channel_id,
            request=VerificationClaimRequest(
                idempotency_key=(
                    "t01e-browser-fixture-verification-claim-failed-v1"
                ),
                expected_channel_revision=int(channel["revision"]),
                claim=_claim_payload(
                    report_id,
                    draft_revision=int(draft_state["revision"]),
                    content_hash=str(draft_state["content_hash"]),
                ),
            ),
        )
    if claim is None:
        claim = await _get_claim_or_none(store, claim_id)
    if claim is not None and claim["status"] == "frozen":
        await service.submit_verification_result(
            principal=verifier,
            claim_id=claim_id,
            request=VerificationResultRequest(
                idempotency_key=(
                    "t01e-browser-fixture-verification-result-failed-v1"
                ),
                expected_claim_revision=int(claim["claim_revision"]),
                result=_verification_failure(),
            ),
        )
    return await store.get_report(report_id)


async def _seed_other_scenarios(
    *,
    service: CollaborationService,
    store: CollaborationStore,
    lilies: CollaborationPrincipal,
    user: CollaborationPrincipal,
    developer: CollaborationPrincipal,
    channel_id: UUID,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    awaiting = await _submit_report_if_missing(
        service=service,
        store=store,
        lilies=lilies,
        channel_id=channel_id,
        scenario="awaiting_user_review",
    )
    records.append(awaiting)

    needs = await _submit_report_if_missing(
        service=service,
        store=store,
        lilies=lilies,
        channel_id=channel_id,
        scenario="needs_more_evidence",
    )
    if needs["status"] == "awaiting_user_review":
        await service.decide_report(
            principal=user,
            report_id=UUID(str(needs["report_id"])),
            request=ApprovalDecisionRequest(
                idempotency_key=(
                    "t01e-browser-fixture-needs-more-evidence-v1"
                ),
                expected_report_revision=int(needs["revision"]),
                decision="needs_more_evidence",
                reason=(
                    "Controlled fixture decision: show the evidence request and "
                    "keep it distinct from runtime permission."
                ),
            ),
        )
        needs = await store.get_report(UUID(str(needs["report_id"])))
    records.append(needs)

    response = await _submit_report_if_missing(
        service=service,
        store=store,
        lilies=lilies,
        channel_id=channel_id,
        scenario="developer_response",
    )
    response = await _advance_to_developer_response(
        service=service,
        store=store,
        user=user,
        developer=developer,
        report_id=UUID(str(response["report_id"])),
        scenario="developer_response",
    )
    records.append(response)
    return records


async def seed_fixture(database: Path = DEFAULT_DATABASE) -> dict[str, Any]:
    """Create or verify the one-channel, four-report browser fixture."""

    store = CollaborationStore(database)
    await store.initialize()
    draft_state = _current_draft_or_fixture(database)
    service = CollaborationService(
        store=store,
        enabled=True,
        now=_utc_now,
        draft_state_provider=lambda application_id: (
            draft_state if application_id == str(APPLICATION_ID) else {}
        ),
        developer_commit_resolver=lambda commit_sha: commit_sha == FIXTURE_COMMIT,
        developer_evidence_resolver=lambda commit_sha, evidence: (
            commit_sha == FIXTURE_COMMIT
            and str(evidence.evidence_id).startswith(
                "evidence:t01e-browser-fixture:"
            )
        ),
    )
    channel_id = uuid5(
        NAMESPACE_URL,
        f"lilies:collaboration:{TASK_ID}:{TASK_REVISION}:{ASSIGNMENT_ID}",
    )
    try:
        channel = await store.get_channel(channel_id)
    except StorageNotFound:
        conflicting = [
            item
            for item in await store.list_channels(limit=5_000)
            if str(item["assignment_id"]) == str(ASSIGNMENT_ID)
            or str(item["lilies_session_id"]) == str(SESSION_ID)
        ]
        if conflicting:
            raise RuntimeError(
                "the real T01E assignment/session is already bound to a different "
                "collaboration channel; refusing to bypass the uniqueness contract"
            )
        issued = await service.create_formal_channel(
            assignment_mode=AssignmentMode.formal_experiment,
            task_id=TASK_ID,
            task_revision=TASK_REVISION,
            assignment_id=ASSIGNMENT_ID,
            lilies_session_id=SESSION_ID,
            application_ids=[APPLICATION_ID],
            collaboration_enabled=True,
            user_notified=True,
            expires_at=_utc_now() + timedelta(days=7),
            retention_until=_utc_now() + timedelta(days=30),
            idempotency_key="t01e-browser-fixture-channel-activation-v1",
            max_report_evidence_rounds=MAX_REPORT_EVIDENCE_ROUNDS,
        )
        channel = issued.channel.model_dump(mode="json", exclude_none=True)
        # The one-time value remains inside the service boundary and is never
        # copied into the return value or process output.
        del issued
    expected_bindings = {
        "task_id": TASK_ID,
        "task_revision": TASK_REVISION,
        "assignment_id": str(ASSIGNMENT_ID),
        "lilies_session_id": str(SESSION_ID),
        "application_ids": [str(APPLICATION_ID)],
    }
    if any(channel.get(key) != value for key, value in expected_bindings.items()):
        raise RuntimeError(
            "existing fixture channel does not match the required real bindings"
        )

    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(SESSION_ID),
        scopes=frozenset(
            {
                CollaborationScope.report_write.value,
                CollaborationScope.response_read.value,
            }
        ),
        channel_id=channel_id,
        assignment_id=ASSIGNMENT_ID,
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )
    developer = CollaborationPrincipal(
        role=SenderRole.codex,
        sender_id="codex-developer",
        scopes=frozenset({"collaboration.developer"}),
    )
    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="t01e-browser-fixture-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=channel_id,
        assignment_id=ASSIGNMENT_ID,
    )

    # The verifier claim must account for every report present when it is
    # frozen, so this complete chain is created before the three deliberately
    # unresolved presentation scenarios.
    failed = await _seed_verification_failure(
        service=service,
        store=store,
        lilies=lilies,
        user=user,
        developer=developer,
        verifier=verifier,
        channel_id=channel_id,
        draft_state=draft_state,
    )
    others = await _seed_other_scenarios(
        service=service,
        store=store,
        lilies=lilies,
        user=user,
        developer=developer,
        channel_id=channel_id,
    )
    reports = [failed, *others]
    expected_statuses = {
        "verification_failed": "verification_failed",
        "awaiting_user_review": "awaiting_user_review",
        "needs_more_evidence": "needs_more_evidence",
        "developer_response": "ready_for_lilies_verification",
    }
    projected_reports = []
    for scenario in SCENARIOS:
        report = next(
            item
            for item in reports
            if str(item["report_id"]) == str(_stable_uuid("report", scenario))
        )
        if report["status"] != expected_statuses[scenario]:
            raise RuntimeError(
                f"fixture scenario {scenario} ended in {report['status']}, "
                f"expected {expected_statuses[scenario]}"
            )
        projected_reports.append(
            {
                "scenario": scenario,
                "report_id": str(report["report_id"]),
                "status": str(report["status"]),
                "studio_url": (
                    "http://127.0.0.1:3000/developer/collaboration"
                    f"?channel={channel_id}&report={report['report_id']}"
                ),
            }
        )
    return {
        "schema_version": "1.0",
        "fixture": "V04-13-T01E controlled browser fixture; not production evidence",
        "database": str(database.resolve()),
        "channel_id": str(channel_id),
        "task_id": TASK_ID,
        "task_revision": TASK_REVISION,
        "application_id": str(APPLICATION_ID),
        "assignment_id": str(ASSIGNMENT_ID),
        "lilies_session_id": str(SESSION_ID),
        "reports": projected_reports,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite collaboration database (default: data/agent_platform.db)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = asyncio.run(seed_fixture(args.database))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
