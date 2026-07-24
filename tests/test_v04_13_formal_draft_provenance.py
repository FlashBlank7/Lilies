from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.applications import ApplicationService
from agent_platform.blocks import build_block_registry
from agent_platform.storage import Storage
from agent_platform.tools import ToolRegistry
from agent_platform.workflow_models import ApplicationCreateRequest, DraftOperation
from agent_platform.workflow_storage import RevisionConflict, WorkflowStorage


async def _formal_application(
    tmp_path: Path,
) -> tuple[Storage, WorkflowStorage, str, str, str]:
    storage = Storage(tmp_path / "platform")
    await storage.initialize()
    workflow = WorkflowStorage(storage)
    await workflow.initialize()
    application = await workflow.create_application(
        ApplicationCreateRequest(
            name="Formal provenance fixture",
            requirement=(
                "Build an auditable enterprise document review workflow with "
                "explicit human escalation."
            ),
        )
    )
    return (
        storage,
        workflow,
        str(application["id"]),
        str(uuid4()),
        str(uuid4()),
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@pytest.mark.asyncio
async def test_formal_baseline_and_blackbox_mutation_form_an_exact_immutable_chain(
    tmp_path: Path,
) -> None:
    storage, workflow, application_id, assignment_id, session_id = (
        await _formal_application(tmp_path)
    )
    baseline = await workflow.begin_formal_draft_provenance(
        assignment_id=assignment_id,
        session_id=session_id,
        application_id=application_id,
    )
    assert await workflow.begin_formal_draft_provenance(
        assignment_id=assignment_id,
        session_id=session_id,
        application_id=application_id,
    ) == baseline

    draft = await workflow.get_draft(application_id)
    snapshot = draft["snapshot"].model_copy(deep=True)
    snapshot.description = "Meaningful edit authored through the Lilies black-box API."
    request_id = str(uuid4())
    tool_call_id = "formal-tool-call-0001"
    request_payload = {
        "application_id": application_id,
        "expected_revision": int(draft["revision"]),
        "idempotency_key": "formal-edit-0001",
        "op": "set_metadata",
        "data": {"description": snapshot.description},
    }
    operation_payload = {
        "application_id": application_id,
        "expected_revision": int(draft["revision"]),
        "op": "set_metadata",
        "data": {"description": snapshot.description},
    }
    result = await workflow.save_draft(
        application_id,
        snapshot,
        expected_revision=int(draft["revision"]),
        idempotency_key="formal-edit-0001",
        change_context={"operation": "set_metadata"},
        idempotency_digest=_digest(operation_payload),
        formal_mutation_context={
            "assignment_id": assignment_id,
            "session_id": session_id,
            "application_id": application_id,
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "operation": "set_metadata",
            "request_payload_digest": _digest(request_payload),
        },
    )

    exported = await workflow.export_formal_run_snapshot(
        application_id,
        assignment_id=assignment_id,
        session_id=session_id,
    )
    assert exported["complete"] is True
    assert exported["counts"]["formal_draft_baselines"] == 1
    assert exported["counts"]["formal_draft_mutations"] == 1
    persisted_baseline = exported["formal_draft_provenance"]["baselines"][0]
    mutation = exported["formal_draft_provenance"]["mutations"][0]
    assert persisted_baseline["baseline_revision"] == 0
    assert persisted_baseline["baseline_content_hash"] == draft["content_hash"]
    assert mutation["actor_kind"] == "lilies_blackbox"
    assert mutation["request_id"] == request_id
    assert mutation["tool_call_id"] == tool_call_id
    assert mutation["before_revision"] == persisted_baseline["baseline_revision"]
    assert mutation["before_content_hash"] == persisted_baseline["baseline_content_hash"]
    assert mutation["after_revision"] == result["revision"] == 1
    assert mutation["after_content_hash"] == result["content_hash"]
    assert mutation["content_changed"] == 1

    with pytest.raises(sqlite3.IntegrityError, match="baseline is immutable"):
        with storage._connect() as connection:
            connection.execute(
                "UPDATE formal_draft_baselines SET baseline_revision=1 "
                "WHERE assignment_id=?",
                (assignment_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="mutation audit is immutable"):
        with storage._connect() as connection:
            connection.execute(
                "DELETE FROM formal_draft_mutations WHERE assignment_id=?",
                (assignment_id,),
            )


@pytest.mark.asyncio
async def test_direct_formal_draft_write_is_preserved_as_unattributed(
    tmp_path: Path,
) -> None:
    _, workflow, application_id, assignment_id, session_id = (
        await _formal_application(tmp_path)
    )
    await workflow.begin_formal_draft_provenance(
        assignment_id=assignment_id,
        session_id=session_id,
        application_id=application_id,
    )
    draft = await workflow.get_draft(application_id)
    snapshot = draft["snapshot"].model_copy(deep=True)
    snapshot.description = "A direct developer write that the verifier must reject."

    await workflow.save_draft(
        application_id,
        snapshot,
        expected_revision=int(draft["revision"]),
        idempotency_key="unattributed-edit-0001",
        change_context={"operation": "set_metadata"},
    )

    exported = await workflow.export_formal_run_snapshot(
        application_id,
        assignment_id=assignment_id,
        session_id=session_id,
    )
    mutation = exported["formal_draft_provenance"]["mutations"][0]
    assert mutation["actor_kind"] == "unattributed"
    assert mutation["content_changed"] == 1
    assert mutation["request_id"] is None
    assert mutation["tool_call_id"] is None
    assert mutation["request_payload_digest"] is None


@pytest.mark.asyncio
async def test_application_service_rejects_no_op_laundering_before_revision_change(
    tmp_path: Path,
) -> None:
    _, workflow, application_id, assignment_id, session_id = (
        await _formal_application(tmp_path)
    )
    await workflow.begin_formal_draft_provenance(
        assignment_id=assignment_id,
        session_id=session_id,
        application_id=application_id,
    )
    draft = await workflow.get_draft(application_id)
    service = ApplicationService(
        workflow,
        build_block_registry(),
        ToolRegistry(),
    )

    with pytest.raises(ValueError, match="would not change"):
        await service.apply_operation(
            application_id,
            DraftOperation(
                expected_revision=int(draft["revision"]),
                idempotency_key="no-op-laundering-0001",
                op="set_metadata",
                data={"description": draft["snapshot"].description},
            ),
            formal_mutation_context={
                "assignment_id": assignment_id,
                "session_id": session_id,
                "application_id": application_id,
                "request_id": str(uuid4()),
                "tool_call_id": "formal-no-op-tool-call",
                "operation": "set_metadata",
                "request_payload_digest": _digest({"no_op": True}),
            },
        )

    current = await workflow.get_draft(application_id)
    exported = await workflow.export_formal_run_snapshot(
        application_id,
        assignment_id=assignment_id,
        session_id=session_id,
    )
    assert current["revision"] == draft["revision"] == 0
    assert exported["formal_draft_provenance"]["mutations"] == []


@pytest.mark.asyncio
async def test_formal_baseline_identity_cannot_be_rebound(
    tmp_path: Path,
) -> None:
    _, workflow, application_id, assignment_id, session_id = (
        await _formal_application(tmp_path)
    )
    await workflow.begin_formal_draft_provenance(
        assignment_id=assignment_id,
        session_id=session_id,
        application_id=application_id,
    )

    with pytest.raises(RevisionConflict, match="another binding"):
        await workflow.begin_formal_draft_provenance(
            assignment_id=assignment_id,
            session_id=str(uuid4()),
            application_id=application_id,
        )
