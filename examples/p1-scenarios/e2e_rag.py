"""P1-2 RAG E2E (independent run): documents -> knowledge_index_sync -> knowledge_retrieval -> grounded_answer -> end.

Real platform, DeepSeek provider configured (egress enabled), fresh data dir.
Documents: 5 real company-policy documents for a fictional firm "Nimbus Labs".
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, "/home/jiangzhijun/Lilies/platform/backend/src")

from dotenv import load_dotenv  # noqa: E402

load_dotenv("/home/jiangzhijun/Lilies/.env")
os.environ["MODEL_EGRESS_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402

ROOT = Path("/home/jiangzhijun/Lilies")
DATA_DIR = ROOT / ".tmp" / "p12_data"
WORKSPACE_ROOT = ROOT / ".tmp" / "p12_workspace"

TOKEN = "workflow-test"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

INDEX_NAME = "nimbus-employee-handbook"
EVENT_ID = "lilies-p12-nimbus-0001"

DOCUMENTS = [
    {
        "source_id": "travel-policy",
        "title": "Nimbus Labs Travel Reimbursement Policy",
        "content": (
            "Nimbus Labs reimburses business travel expenses up to 1500 US dollars per trip. "
            "Airfare must be booked at least 14 days in advance of departure. "
            "Hotel stays are reimbursed up to 220 US dollars per night. "
            "All expense receipts must be submitted within 30 days of returning. "
            "Meals are reimbursed up to 60 US dollars per day while traveling."
        ),
        "revision": "3",
        "url": "https://policies.nimbus.example/travel",
        "allowed_roles": ["*"],
        "metadata": {"category": "finance"},
    },
    {
        "source_id": "leave-policy",
        "title": "Nimbus Labs Annual Leave Policy",
        "content": (
            "Full time employees accrue 20 days of annual leave per year and must request "
            "leave at least 5 working days in advance. Unused leave may be carried over "
            "for up to 90 days after the end of the calendar year. Sick leave is separate "
            "and does not reduce the annual leave balance. Managers approve leave requests "
            "within 3 working days."
        ),
        "revision": "3",
        "url": "https://policies.nimbus.example/leave",
        "allowed_roles": ["*"],
        "metadata": {"category": "hr"},
    },
    {
        "source_id": "remote-work",
        "title": "Nimbus Labs Remote Work Policy",
        "content": (
            "Employees may work remotely up to 3 days per week with manager approval. "
            "Remote employees must maintain an approved internet connection of at least "
            "50 megabits per second. A company issued laptop is required for all remote "
            "work. Employees working abroad for more than 30 days must notify People Ops."
        ),
        "revision": "2",
        "url": "https://policies.nimbus.example/remote",
        "allowed_roles": ["*"],
        "metadata": {"category": "workplace"},
    },
    {
        "source_id": "security-policy",
        "title": "Nimbus Labs Security Policy",
        "content": (
            "All employees must use multi factor authentication for every company system. "
            "Passwords must be rotated every 90 days. Data classified as confidential must "
            "be encrypted at rest. Third party access to company systems requires a signed "
            "non disclosure agreement."
        ),
        "revision": "1",
        "url": "https://policies.nimbus.example/security",
        "allowed_roles": ["*"],
        "metadata": {"category": "security"},
    },
    {
        "source_id": "benefits-policy",
        "title": "Nimbus Labs Benefits Policy",
        "content": (
            "The company provides health insurance, a 4 percent retirement match, and 10 "
            "weeks of paid parental leave. Employees become eligible for benefits after 90 "
            "days of continuous employment. Flexible spending accounts open each January. "
            "Benefits coverage renews on the first day of every calendar year."
        ),
        "revision": "1",
        "url": "https://policies.nimbus.example/benefits",
        "allowed_roles": ["*"],
        "metadata": {"category": "hr"},
    },
]

QUESTION = (
    "How many days of annual leave do full time employees accrue per year and "
    "how far in advance must leave be requested?"
)


def build_and_run() -> dict:
    settings = Settings(
        api_token=TOKEN,
        model_egress_enabled=True,
        data_dir=DATA_DIR,
        workspace_root=WORKSPACE_ROOT,
        scheduler_poll_seconds=3600,
    )
    app = create_app(settings, provider=None)  # no provider -> real DeepSeek MultiProvider
    with TestClient(app) as client:
        blocks = {b["type"]: b for b in client.get("/api/v1/blocks", headers=H).json()}
        required = {"knowledge_index_sync", "knowledge_retrieval", "grounded_answer"}
        assert required.issubset(blocks), f"missing blocks: {required - set(blocks)}"

        app_resp = client.post(
            "/api/v1/applications",
            headers=H,
            json={
                "name": "P1-2 RAG document Q&A (independent)",
                "requirement": (
                    "Index employee-handbook documents, retrieve relevant passages, and "
                    "answer the question with citations."
                ),
            },
        )
        assert app_resp.status_code == 201, app_resp.text
        application_id = app_resp.json()["id"]
        revision = 0

        def mutate(op: str, data: dict) -> None:
            nonlocal revision
            resp = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=H,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": op,
                    "data": data,
                },
            )
            assert resp.status_code == 200, f"{op} failed: {resp.text}"
            revision = resp.json()["revision"]

        mutate("add_node", {
            "node": {
                "id": "request",
                "type": "start",
                "title": "Request",
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
        })
        mutate("add_node", {
            "node": {
                "id": "synchronize",
                "type": "knowledge_index_sync",
                "title": "Index documents",
                "config": {
                    "index_name": INDEX_NAME,
                    "documents": {"$ref": {"node_id": "request", "path": ["documents"]}},
                    "deleted_source_ids": {
                        "$ref": {"node_id": "request", "path": ["deleted_source_ids"]}
                    },
                    "event_id": {"$ref": {"node_id": "request", "path": ["event_id"]}},
                    "replace": True,
                },
            }
        })
        mutate("add_node", {
            "node": {
                "id": "retrieve",
                "type": "knowledge_retrieval",
                "title": "Retrieve passages",
                "config": {
                    "index_name": INDEX_NAME,
                    "query": {"$ref": {"node_id": "request", "path": ["query"]}},
                    "principal_roles": {
                        "$ref": {"node_id": "request", "path": ["principal_roles"]}
                    },
                    "top_k": 5,
                    "minimum_score": 0.01,
                },
            }
        })
        mutate("add_node", {
            "node": {
                "id": "answer",
                "type": "grounded_answer",
                "title": "Grounded answer",
                "config": {
                    "query": {"$ref": {"node_id": "request", "path": ["query"]}},
                    "retrieval": {"$ref": {"node_id": "retrieve", "path": ["output"]}},
                },
            }
        })
        mutate("add_node", {
            "node": {
                "id": "result",
                "type": "end",
                "title": "Result",
                "config": {
                    "outputs": {
                        "answer": {"$ref": {"node_id": "answer", "path": ["answer"]}},
                        "status": {"$ref": {"node_id": "answer", "path": ["status"]}},
                        "supported": {"$ref": {"node_id": "answer", "path": ["supported"]}},
                        "citations": {"$ref": {"node_id": "answer", "path": ["citations"]}},
                        "retrieved_count": {
                            "$ref": {"node_id": "retrieve", "path": ["retrieved_count"]}
                        },
                        "retrieval_results": {
                            "$ref": {"node_id": "retrieve", "path": ["results"]}
                        },
                        "acl_decision": {
                            "$ref": {"node_id": "answer", "path": ["acl_decision"]}
                        },
                        "sync_document_count": {
                            "$ref": {"node_id": "synchronize", "path": ["document_count"]}
                        },
                        "sync_chunk_count": {
                            "$ref": {"node_id": "synchronize", "path": ["chunk_count"]}
                        },
                        "sync_revision": {
                            "$ref": {"node_id": "synchronize", "path": ["index_revision"]}
                        },
                        "model_versions": {
                            "$ref": {"node_id": "answer", "path": ["model_versions"]}
                        },
                    }
                },
            }
        })
        for edge_id, source, target in [
            ("a", "request", "synchronize"),
            ("b", "synchronize", "retrieve"),
            ("c", "retrieve", "answer"),
            ("d", "answer", "result"),
        ]:
            mutate("add_edge", {"edge": {"id": edge_id, "source": source, "target": target}})

        # Mandatory acceptance test (valid draft requires at least one).
        mutate("add_test", {
            "test": {
                "name": "Cited answer from indexed employee handbook",
                "requirement": (
                    "Index the handbook documents, retrieve the relevant passages, and "
                    "produce a grounded answer with citations."
                ),
                "inputs": {
                    "documents": DOCUMENTS,
                    "deleted_source_ids": [],
                    "event_id": EVENT_ID,
                    "query": QUESTION,
                    "principal_roles": ["engineer"],
                },
                "assertions": [
                    {"path": ["status"], "operator": "equals", "expected": "answered"},
                    {"path": ["supported"], "operator": "equals", "expected": True},
                    {"path": ["citations"], "operator": "min_length", "expected": 1},
                    {"path": ["retrieved_count"], "operator": "exists"},
                    {"path": ["answer"], "operator": "min_length", "expected": 20},
                    {"path": ["sync_revision"], "operator": "equals", "expected": 1},
                ],
                "required_node_types": [
                    "knowledge_index_sync",
                    "knowledge_retrieval",
                    "grounded_answer",
                ],
            }
        })

        validation = client.post(
            f"/api/v1/applications/{application_id}/draft/validate", headers=H
        )
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True, validation.text
        print(f"draft validation: valid={validation.json()['valid']}")

        run_resp = client.post(
            f"/api/v1/applications/{application_id}/runs",
            headers=H,
            json={
                "inputs": {
                    "documents": DOCUMENTS,
                    "deleted_source_ids": [],
                    "event_id": EVENT_ID,
                    "query": QUESTION,
                    "principal_roles": ["engineer"],
                },
                "use_draft": True,
            },
        )
        assert run_resp.status_code == 202, run_resp.text
        run_id = run_resp.json()["run_id"]
        print(f"run_id={run_id}")

        run = None
        for _ in range(120):
            time.sleep(1.0)
            run = client.get(f"/api/v1/runs/{run_id}", headers=H).json()
            status = run.get("status")
            if status in ("succeeded", "failed", "cancelled"):
                break
        if run is None:
            raise RuntimeError("no run response")
        return {"application_id": application_id, "run": run, "run_id": run_id}


def main() -> None:
    result = build_and_run()
    run = result["run"]
    print("\n===== RUN =====")
    print(json.dumps(
        {"run_id": result["run_id"], "status": run.get("status"), "error": run.get("error")},
        indent=2,
        ensure_ascii=False,
    ))
    outputs = run.get("outputs") or {}
    print("\n===== TERMINAL OUTPUTS (end node) =====")
    print(json.dumps(outputs, indent=2, ensure_ascii=False)[:8000])

    status = run.get("status")
    answer = outputs.get("answer")
    citations = outputs.get("citations") or []
    retrieved = outputs.get("retrieved_count")
    supported = outputs.get("supported")
    print(f"\nstatus={status} supported={supported} retrieved_count={retrieved} citations={len(citations)}")
    print("\n===== ANSWER EXCERPT =====")
    print((answer or "")[:400])
    print("\n===== CITATION TITLES/SOURCES =====")
    for c in citations:
        print(f"  [{c.get('score')}] {c.get('source_id')}: {c.get('title')} (rev={c.get('revision')})")
    if status != "succeeded":
        raise SystemExit(1)
    if not supported or not citations:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
