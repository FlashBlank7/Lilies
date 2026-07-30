from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.knowledge_rag import (
    GroundedAnswerRequest,
    KnowledgeDocument,
    KnowledgeIndexConflict,
    KnowledgeIndexCreateRequest,
    KnowledgeIndexService,
    KnowledgeRetrieveRequest,
    KnowledgeSyncRequest,
)
from tests.test_runtime import ScriptedProvider


async def _service(tmp_path: Path) -> KnowledgeIndexService:
    service = KnowledgeIndexService(tmp_path / "knowledge.db")
    await service.initialize()
    await service.create_index(
        KnowledgeIndexCreateRequest(
            name="company-handbook",
            chunk_size=300,
            chunk_overlap=40,
            idempotency_key="create-company-handbook-001",
        )
    )
    return service


def _documents() -> list[KnowledgeDocument]:
    return [
        KnowledgeDocument(
            source_id="safety",
            title="Warehouse safety",
            content=(
                "Forklift operators must complete annual certification. "
                "A spotter is required whenever a load blocks forward visibility."
            ),
            revision="1",
            url="https://kb.example/safety",
            allowed_roles=["warehouse"],
        ),
        KnowledgeDocument(
            source_id="finance",
            title="Finance approvals",
            content=(
                "Acquisitions above 50000 dollars require CFO approval. "
                "The confidential code phrase is BLUE-MAGNOLIA."
            ),
            revision="1",
            url="https://kb.example/finance",
            allowed_roles=["finance"],
        ),
        KnowledgeDocument(
            source_id="holidays",
            title="Company holidays",
            content="The company closes on January 1 and December 25.",
            revision="1",
            url="https://kb.example/holidays",
            allowed_roles=["*"],
        ),
    ]


@pytest.mark.asyncio
async def test_acl_first_retrieval_update_delete_and_idempotency(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    request = KnowledgeSyncRequest(
        documents=_documents(),
        event_id="handbook-snapshot-001",
    )
    synchronized = await service.sync("company-handbook", request)
    replayed = await service.sync("company-handbook", request)

    assert synchronized["inserted"] == ["finance", "holidays", "safety"]
    assert synchronized["index_revision"] == 1
    assert replayed["replayed"] is True
    assert replayed["index_revision"] == 1

    warehouse = await service.retrieve(
        "company-handbook",
        KnowledgeRetrieveRequest(
            query="When is a forklift spotter required?",
            principal_roles=["warehouse"],
            minimum_score=0.01,
        ),
    )
    assert warehouse["results"][0]["source_id"] == "safety"
    assert warehouse["forbidden_chunk_count"] == 0
    assert warehouse["acl_decision"]["filtered_source_ids"] == ["finance"]
    assert "BLUE-MAGNOLIA" not in str(warehouse)

    denied = await service.answer(
        "company-handbook",
        GroundedAnswerRequest(
            query="What is the confidential finance code phrase?",
            principal_roles=["warehouse"],
            minimum_score=0.2,
        ),
    )
    assert denied["status"] == "refused"
    assert denied["supported"] is False
    assert denied["citations"] == []
    assert "BLUE-MAGNOLIA" not in str(denied)

    finance = await service.answer(
        "company-handbook",
        GroundedAnswerRequest(
            query="Who approves acquisitions above 50000 dollars?",
            principal_roles=["finance"],
            minimum_score=0.01,
        ),
    )
    assert finance["status"] == "answered"
    assert finance["supported"] is True
    assert finance["citations"][0]["source_id"] == "finance"
    assert finance["citations"][0]["revision"] == "1"

    updated = _documents()
    updated[0] = updated[0].model_copy(
        update={
            "revision": "2",
            "content": (
                "Forklift operators must complete annual certification. "
                "Two spotters are required whenever a load blocks forward visibility."
            ),
        }
    )
    update_receipt = await service.sync(
        "company-handbook",
        KnowledgeSyncRequest(
            documents=updated,
            deleted_source_ids=[],
            event_id="handbook-snapshot-002",
        ),
    )
    assert update_receipt["updated"] == ["safety"]
    assert update_receipt["unchanged"] == ["finance", "holidays"]
    assert update_receipt["index_revision"] == 2

    deletion_receipt = await service.sync(
        "company-handbook",
        KnowledgeSyncRequest(
            deleted_source_ids=["finance"],
            event_id="handbook-delete-finance-003",
        ),
    )
    assert deletion_receipt["deleted"] == ["finance"]
    assert deletion_receipt["document_count"] == 2
    after_deletion = await service.retrieve(
        "company-handbook",
        KnowledgeRetrieveRequest(
            query="BLUE-MAGNOLIA",
            principal_roles=["finance"],
            minimum_score=0,
        ),
    )
    assert all(item["source_id"] != "finance" for item in after_deletion["results"])

    with pytest.raises(KnowledgeIndexConflict, match="different payload"):
        await service.sync(
            "company-handbook",
            KnowledgeSyncRequest(
                documents=[updated[0]],
                event_id="handbook-snapshot-001",
            ),
        )


@pytest.mark.asyncio
async def test_stale_revision_is_rejected_without_partial_changes(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    original = _documents()[0].model_copy(update={"revision": "7"})
    await service.sync(
        "company-handbook",
        KnowledgeSyncRequest(
            documents=[original],
            event_id="safety-version-seven-001",
        ),
    )
    with pytest.raises(KnowledgeIndexConflict, match="stale revision"):
        await service.sync(
            "company-handbook",
            KnowledgeSyncRequest(
                documents=[original.model_copy(update={"revision": "6", "content": "stale"})],
                event_id="safety-version-six-002",
            ),
        )
    current = await service.get_index("company-handbook")
    assert current["revision"] == 1
    assert current["document_count"] == 1


@pytest.mark.asyncio
async def test_retrieval_discounts_stop_words_and_ranks_specific_evidence(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    await service.sync(
        "company-handbook",
        KnowledgeSyncRequest(
            documents=[
                KnowledgeDocument(
                    source_id="public-contact",
                    title="Emergency contacts",
                    content=(
                        "For an emergency, the public security desk is extension 7000."
                    ),
                    revision="1",
                    allowed_roles=["*"],
                ),
                KnowledgeDocument(
                    source_id="payroll",
                    title="Payroll corrections",
                    content=(
                        "Emergency payroll correction requests have a Tuesday deadline and "
                        "require the payroll manager."
                    ),
                    revision="1",
                    allowed_roles=["hr"],
                ),
            ],
            event_id="specific-ranking-snapshot-001",
        ),
    )

    unsupported = await service.answer(
        "company-handbook",
        GroundedAnswerRequest(
            query="What is the 2028 cafeteria lunch menu?",
            principal_roles=["visitor"],
            minimum_score=0.2,
        ),
    )
    assert unsupported["status"] == "refused"
    assert unsupported["citations"] == []

    payroll = await service.retrieve(
        "company-handbook",
        KnowledgeRetrieveRequest(
            query="What is the emergency payroll correction deadline?",
            principal_roles=["hr"],
            minimum_score=0.2,
        ),
    )
    assert payroll["results"][0]["source_id"] == "payroll"
    assert payroll["results"][0]["matched_terms"] == [
        "correction",
        "deadline",
        "emergency",
        "payroll",
    ]


def test_public_api_and_workflow_blocks_form_a_grounded_rag_path(
    tmp_path: Path,
) -> None:
    token = "knowledge-test-token"
    app = create_app(
        Settings(
            api_token=token,
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            scheduler_poll_seconds=3600,
        ),
        ScriptedProvider(),
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with TestClient(app) as client:
        created_index = client.post(
            "/api/v1/knowledge-indexes",
            headers=headers,
            json={
                "name": "field-manual",
                "chunk_size": 300,
                "chunk_overlap": 40,
                "idempotency_key": "create-field-manual-001",
            },
        )
        assert created_index.status_code == 200, created_index.text

        blocks = {
            item["type"]: item
            for item in client.get("/api/v1/blocks", headers=headers).json()
        }
        assert {
            "knowledge_index_sync",
            "knowledge_retrieval",
            "grounded_answer",
        }.issubset(blocks)

        application = client.post(
            "/api/v1/applications",
            headers=headers,
            json={
                "name": "Field manual answer",
                "requirement": (
                    "Synchronize governed source revisions, retrieve only authorized evidence, "
                    "and return a cited answer or refusal."
                ),
            },
        )
        assert application.status_code == 201, application.text
        application_id = application.json()["id"]
        revision = 0

        def mutate(op: str, data: dict) -> None:
            nonlocal revision
            response = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=headers,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": op,
                    "data": data,
                },
            )
            assert response.status_code == 200, response.text
            revision = response.json()["revision"]

        mutate(
            "add_node",
            {
                "node": {
                    "id": "request",
                    "type": "start",
                    "title": "Governed request",
                    "config": {
                        "inputs": [
                            {"name": "documents", "type": "array"},
                            {"name": "deleted_source_ids", "type": "array"},
                            {"name": "event_id", "type": "string"},
                            {"name": "query", "type": "string"},
                            {"name": "principal_roles", "type": "array"},
                        ]
                    },
                }
            },
        )
        mutate(
            "add_node",
            {
                "node": {
                    "id": "synchronize",
                    "type": "knowledge_index_sync",
                    "title": "Synchronize source revisions",
                    "config": {
                        "index_name": "field-manual",
                        "documents": {
                            "$ref": {"node_id": "request", "path": ["documents"]}
                        },
                        "deleted_source_ids": {
                            "$ref": {
                                "node_id": "request",
                                "path": ["deleted_source_ids"],
                            }
                        },
                        "event_id": {
                            "$ref": {"node_id": "request", "path": ["event_id"]}
                        },
                    },
                }
            },
        )
        mutate(
            "add_node",
            {
                "node": {
                    "id": "retrieve",
                    "type": "knowledge_retrieval",
                    "title": "Retrieve authorized evidence",
                    "config": {
                        "index_name": "field-manual",
                        "query": {"$ref": {"node_id": "request", "path": ["query"]}},
                        "principal_roles": {
                            "$ref": {
                                "node_id": "request",
                                "path": ["principal_roles"],
                            }
                        },
                        "minimum_score": 0.01,
                    },
                }
            },
        )
        mutate(
            "add_node",
            {
                "node": {
                    "id": "answer",
                    "type": "grounded_answer",
                    "title": "Answer only from evidence",
                    "config": {
                        "query": {"$ref": {"node_id": "request", "path": ["query"]}},
                        "retrieval": {
                            "$ref": {"node_id": "retrieve", "path": ["output"]}
                        },
                    },
                }
            },
        )
        mutate(
            "add_node",
            {
                "node": {
                    "id": "result",
                    "type": "end",
                    "title": "Auditable result",
                    "config": {
                        "outputs": {
                            "answer": {
                                "$ref": {"node_id": "answer", "path": ["answer"]}
                            },
                            "status": {
                                "$ref": {"node_id": "answer", "path": ["status"]}
                            },
                            "citations": {
                                "$ref": {"node_id": "answer", "path": ["citations"]}
                            },
                            "forbidden_chunk_count": {
                                "$ref": {
                                    "node_id": "retrieve",
                                    "path": ["forbidden_chunk_count"],
                                }
                            },
                            "sync_revision": {
                                "$ref": {
                                    "node_id": "synchronize",
                                    "path": ["index_revision"],
                                }
                            },
                        }
                    },
                }
            },
        )
        for edge_id, source, target in [
            ("a", "request", "synchronize"),
            ("b", "synchronize", "retrieve"),
            ("c", "retrieve", "answer"),
            ("d", "answer", "result"),
        ]:
            mutate(
                "add_edge",
                {"edge": {"id": edge_id, "source": source, "target": target}},
            )

        mutate(
            "add_test",
            {
                "test": {
                    "name": "Authorized maintenance question",
                    "requirement": (
                        "A technician receives the current cited procedure while a restricted "
                        "personnel document never reaches retrieval."
                    ),
                    "inputs": {
                        "documents": [
                            {
                                "source_id": "pump",
                                "title": "Pump restart",
                                "content": "Close valve A before restarting pump 7.",
                                "revision": "1",
                                "url": "https://manual.example/pump",
                                "allowed_roles": ["technician"],
                            },
                            {
                                "source_id": "personnel",
                                "title": "Personnel note",
                                "content": "The private layoff code is RED-LANTERN.",
                                "revision": "1",
                                "allowed_roles": ["hr"],
                            },
                        ],
                        "deleted_source_ids": [],
                        "event_id": "field-manual-seed-001",
                        "query": "What must happen before restarting pump 7?",
                        "principal_roles": ["technician"],
                    },
                    "assertions": [
                        {"path": ["status"], "operator": "equals", "expected": "answered"},
                        {
                            "path": ["forbidden_chunk_count"],
                            "operator": "equals",
                            "expected": 0,
                        },
                        {"path": ["sync_revision"], "operator": "equals", "expected": 1},
                    ],
                    "required_node_types": [
                        "knowledge_index_sync",
                        "knowledge_retrieval",
                        "grounded_answer",
                    ],
                }
            },
        )

        validation = client.post(
            f"/api/v1/applications/{application_id}/draft/validate",
            headers=headers,
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True, validation.text
        test_run = client.post(
            f"/api/v1/applications/{application_id}/tests/run",
            headers=headers,
        )
        assert test_run.status_code == 200, test_run.text
        assert test_run.json()["passed"] is True, test_run.text
