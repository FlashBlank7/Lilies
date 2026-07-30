#!/usr/bin/env python3
"""Build EXP-LILIES-002 through public Lilies APIs only."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


def ref(node_id: str, *path: str, optional: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "$ref": {"node_id": node_id, "path": list(path)}
    }
    if optional:
        value["$ref"]["optional"] = True
    return value


def node(
    node_id: str,
    block_type: str,
    title: str,
    x: float,
    y: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": block_type,
        "title": title,
        "position": {"x": x, "y": y},
        "config": config,
    }


def edge(
    edge_id: str,
    source: str,
    target: str,
    branch: str | None = None,
    *,
    source_port: str = "output",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "source_port": source_port,
        "target_port": "input",
    }
    if branch is not None:
        value["source_port"] = "branch"
        value["branch"] = branch
    return value


def page_reader_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            node(
                "page_input",
                "start",
                "Authorized page source",
                0,
                0,
                {"inputs": [{"name": "source", "type": "object"}]},
            ),
            node(
                "read_page",
                "http_request",
                "Read the current BookStack page",
                260,
                0,
                {
                    "method": "GET",
                    "url": ref("page_input", "source", "url"),
                    "headers": {
                        "Authorization": {"$secret": "bookstack_api_authorization"},
                        "Accept": "application/json",
                    },
                    "timeout_seconds": 30,
                },
            ),
            node(
                "map_page",
                "variable_assigner",
                "Map BookStack fields to the generic knowledge contract",
                520,
                0,
                {
                    "assignments": {
                        "source_id": ref("page_input", "source", "source_id"),
                        "title": ref("read_page", "output", "name"),
                        "content": ref("read_page", "output", "html"),
                        "revision": ref("read_page", "output", "updated_at"),
                        "url": ref("page_input", "source", "browser_url"),
                        "allowed_roles": ref(
                            "page_input",
                            "source",
                            "allowed_roles",
                        ),
                        "metadata": {
                            "bookstack_page_id": ref(
                                "read_page",
                                "output",
                                "id",
                            ),
                            "bookstack_slug": ref(
                                "read_page",
                                "output",
                                "slug",
                            ),
                            "source_system": "BookStack",
                        },
                    }
                },
            ),
            node(
                "page_end",
                "end",
                "Return the mapped knowledge document",
                780,
                0,
                {"outputs": {"document": ref("map_page", "output")}},
            ),
        ],
        "edges": [
            edge("page-input-read", "page_input", "read_page"),
            edge("page-read-map", "read_page", "map_page"),
            edge("page-map-end", "map_page", "page_end"),
        ],
    }


def desired_nodes() -> list[dict[str, Any]]:
    return [
        node(
            "start",
            "start",
            "Knowledge request and authorized source scope",
            0,
            260,
            {
                "inputs": [
                    {"name": "sources", "type": "array"},
                    {"name": "deleted_source_ids", "type": "array"},
                    {"name": "sync_event_id", "type": "string"},
                    {"name": "question", "type": "string"},
                    {"name": "principal_roles", "type": "array"},
                    {
                        "name": "requires_review",
                        "type": "boolean",
                        "required": False,
                        "default": False,
                    },
                ]
            },
        ),
        node(
            "read_sources",
            "iteration",
            "Read every customer-authorized BookStack page",
            260,
            260,
            {
                "items": ref("start", "sources"),
                "workflow": page_reader_workflow(),
                "item_name": "source",
                "output_node_id": "page_end",
                "output_path": ["document"],
                "parallelism": 4,
            },
        ),
        node(
            "sync_index",
            "knowledge_index_sync",
            "Synchronize revisions, ACLs, updates, and deletions",
            520,
            260,
            {
                "index_name": "exp002-bookstack-r1",
                "documents": ref("read_sources", "items"),
                "deleted_source_ids": ref("start", "deleted_source_ids"),
                "event_id": ref("start", "sync_event_id"),
            },
        ),
        node(
            "retrieve",
            "knowledge_retrieval",
            "Filter unauthorized sources before evidence ranking",
            780,
            260,
            {
                "index_name": "exp002-bookstack-r1",
                "query": ref("start", "question"),
                "principal_roles": ref("start", "principal_roles"),
                "top_k": 4,
                "minimum_score": 0.2,
            },
        ),
        node(
            "ground_answer",
            "grounded_answer",
            "Answer only from cited authorized evidence",
            1040,
            260,
            {
                "query": ref("start", "question"),
                "retrieval": ref("retrieve", "output"),
                "refusal_message": (
                    "No authorized source contains enough evidence to answer this question."
                ),
            },
        ),
        node(
            "review_route",
            "if_else",
            "Require review for sensitive or low-evidence requests",
            1300,
            260,
            {
                "cases": [
                    {
                        "id": "requires_review",
                        "conditions": [
                            {
                                "value": ref("start", "requires_review"),
                                "operator": "equals",
                                "expected": True,
                            }
                        ],
                        "logical_operator": "and",
                    }
                ],
                "default_branch": "direct",
            },
        ),
        node(
            "human_review",
            "human_input",
            "Knowledge owner review",
            1560,
            100,
            {
                "title": "Review sensitive knowledge answer",
                "description": (
                    "Inspect the authorized citations, then approve delivery or route the "
                    "question to a specialist."
                ),
                "fields": [
                    {
                        "name": "approved",
                        "label": "Approve this cited answer?",
                        "type": "boolean",
                    },
                    {
                        "name": "note",
                        "label": "Review note",
                        "type": "string",
                    },
                ],
            },
        ),
        node(
            "review_decision",
            "if_else",
            "Apply the reviewer decision",
            1820,
            100,
            {
                "cases": [
                    {
                        "id": "approved",
                        "conditions": [
                            {
                                "value": ref("human_review", "output", "approved"),
                                "operator": "equals",
                                "expected": True,
                            }
                        ],
                        "logical_operator": "and",
                    }
                ],
                "default_branch": "rejected",
            },
        ),
        node(
            "approved_answer",
            "variable_assigner",
            "Deliver the approved cited answer",
            2080,
            20,
            {
                "assignments": {
                    "answer_record": ref("ground_answer", "output"),
                    "review": {
                        "status": "approved",
                        "note": ref("human_review", "output", "note"),
                    },
                }
            },
        ),
        node(
            "rejected_answer",
            "variable_assigner",
            "Refuse delivery after reviewer rejection",
            2080,
            180,
            {
                "assignments": {
                    "answer_record": {
                        "status": "refused_by_reviewer",
                        "supported": False,
                        "answer": ref("human_review", "output", "note"),
                        "citations": [],
                        "query": ref("start", "question"),
                        "index_revision": ref("retrieve", "index_revision"),
                        "forbidden_chunk_count": ref(
                            "retrieve",
                            "forbidden_chunk_count",
                        ),
                    },
                    "review": {
                        "status": "rejected",
                        "note": ref("human_review", "output", "note"),
                    },
                }
            },
        ),
        node(
            "direct_answer",
            "variable_assigner",
            "Deliver a non-sensitive answer or refusal",
            1560,
            420,
            {
                "assignments": {
                    "answer_record": ref("ground_answer", "output"),
                    "review": {"status": "not_required"},
                }
            },
        ),
        node(
            "decision",
            "variable_aggregator",
            "Join the mutually exclusive delivery outcome",
            2340,
            260,
            {
                "variables": [
                    ref("approved_answer", "output", optional=True),
                    ref("rejected_answer", "output", optional=True),
                    ref("direct_answer", "output", optional=True),
                ],
                "mode": "first_non_null",
            },
        ),
        node(
            "answer_artifact",
            "typed_json_artifact",
            "Write the answer and review record",
            2600,
            100,
            {
                "value": ref("decision", "output"),
                "filename": "bookstack-answer-record.json",
                "lineage": [
                    {
                        "source_type": "node_output",
                        "reference": "ground_answer.output",
                    },
                    {
                        "source_type": "workflow_input",
                        "reference": "principal_roles",
                    },
                ],
            },
        ),
        node(
            "retrieval_artifact",
            "typed_json_artifact",
            "Write the retrieval and ACL evidence",
            2600,
            260,
            {
                "value": ref("retrieve", "output"),
                "filename": "bookstack-retrieval-acl-trace.json",
                "lineage": [
                    {
                        "source_type": "node_output",
                        "reference": "read_sources.items",
                    },
                    {
                        "source_type": "workflow_input",
                        "reference": "principal_roles",
                    },
                ],
            },
        ),
        node(
            "sync_artifact",
            "typed_json_artifact",
            "Write the knowledge synchronization receipt",
            2600,
            420,
            {
                "value": ref("sync_index", "output"),
                "filename": "bookstack-index-sync-receipt.json",
                "lineage": [
                    {
                        "source_type": "external_resource",
                        "reference": "BookStack REST API pages",
                    },
                    {
                        "source_type": "workflow_input",
                        "reference": "sync_event_id",
                    },
                ],
            },
        ),
        node(
            "end",
            "end",
            "Return the governed answer and audit artifacts",
            2860,
            260,
            {
                "outputs": {
                    "decision": ref("decision", "output"),
                    "retrieval": ref("retrieve", "output"),
                    "sync_receipt": ref("sync_index", "output"),
                    "answer_artifact": ref("answer_artifact", "artifact"),
                    "retrieval_artifact": ref("retrieval_artifact", "artifact"),
                    "sync_artifact": ref("sync_artifact", "artifact"),
                }
            },
        ),
    ]


def desired_edges() -> list[dict[str, Any]]:
    return [
        edge("start-read", "start", "read_sources"),
        edge("read-sync", "read_sources", "sync_index", source_port="items"),
        edge("sync-retrieve", "sync_index", "retrieve"),
        edge("retrieve-answer", "retrieve", "ground_answer"),
        edge("answer-review-route", "ground_answer", "review_route"),
        edge(
            "route-review",
            "review_route",
            "human_review",
            "requires_review",
        ),
        edge("review-decision", "human_review", "review_decision"),
        edge(
            "decision-approved",
            "review_decision",
            "approved_answer",
            "approved",
        ),
        edge(
            "decision-rejected",
            "review_decision",
            "rejected_answer",
            "rejected",
        ),
        edge("route-direct", "review_route", "direct_answer", "direct"),
        edge("approved-join", "approved_answer", "decision"),
        edge("rejected-join", "rejected_answer", "decision"),
        edge("direct-join", "direct_answer", "decision"),
        edge("join-answer-artifact", "decision", "answer_artifact"),
        edge("join-retrieval-artifact", "decision", "retrieval_artifact"),
        edge("join-sync-artifact", "decision", "sync_artifact"),
        edge("answer-artifact-end", "answer_artifact", "end"),
        edge("retrieval-artifact-end", "retrieval_artifact", "end"),
        edge("sync-artifact-end", "sync_artifact", "end"),
    ]


def request_json(
    method: str,
    url: str,
    *,
    token: str,
    body: Any | None = None,
) -> Any:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} returned HTTP {error.code}: {detail}") from error
    return json.loads(payload) if payload else None


def platform(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    return request_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        token=token,
        body=body,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8015")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--sources-file", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--workspace-path", type=Path, required=True)
    args = parser.parse_args()
    sources = json.loads(args.sources_file.read_text(encoding="utf-8"))
    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))

    blocks = {
        item["type"]: item
        for item in platform(
            "GET",
            args.platform_base,
            args.platform_token,
            "/api/v1/blocks",
        )
    }
    required = {
        "iteration",
        "http_request",
        "knowledge_index_sync",
        "knowledge_retrieval",
        "grounded_answer",
        "if_else",
        "human_input",
        "variable_aggregator",
        "typed_json_artifact",
    }
    missing = sorted(required - set(blocks))
    if missing:
        raise RuntimeError(f"public block catalog is missing: {missing}")

    platform(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/knowledge-indexes",
        {
            "name": "exp002-bookstack-r1",
            "chunk_size": 700,
            "chunk_overlap": 80,
            "idempotency_key": "create-exp002-bookstack-r1",
        },
    )
    created = platform(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/applications",
        {
            "name": "BookStack governed knowledge assistant",
            "description": (
                "Read real BookStack pages, synchronize source revisions and ACLs, "
                "then return a cited authorized answer or safe refusal."
            ),
            "requirement": (
                "Build the EXP-LILIES-002 access-controlled enterprise knowledge "
                "assistant from the frozen revision-1 customer package."
            ),
        },
    )
    application_id = str(created["id"])
    platform(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/platform/secrets",
        {
            "owner_id": application_id,
            "name": "bookstack_api_authorization",
            "value": credential["api_authorization"],
            "description": "Controlled-local read-only BookStack API token",
        },
    )
    revision = 0

    def mutate(op: str, data: dict[str, Any]) -> None:
        nonlocal revision
        result = platform(
            "POST",
            args.platform_base,
            args.platform_token,
            f"/api/v1/applications/{application_id}/draft",
            {
                "expected_revision": revision,
                "idempotency_key": str(uuid4()),
                "op": op,
                "data": data,
            },
        )
        revision = int(result["revision"])

    for item in desired_nodes():
        mutate("add_node", {"node": item})
    for item in desired_edges():
        mutate("add_edge", {"edge": item})

    operation_source = next(
        item for item in sources if item["source_id"] == "ops-conveyor-lockout"
    )
    finance_source = next(
        item for item in sources if item["source_id"] == "finance-vendor-approval"
    )
    public_source = next(
        item for item in sources if item["source_id"] == "public-emergency-contacts"
    )
    mutate(
        "add_test",
        {
            "test": {
                "name": "Real BookStack authorized answer and ACL filter",
                "requirement": (
                    "Read three real host pages, answer from the operations source, and "
                    "filter finance content before retrieval."
                ),
                "inputs": {
                    "sources": [operation_source, finance_source, public_source],
                    "deleted_source_ids": [],
                    "sync_event_id": "exp002-platform-test-001",
                    "question": "What must happen before clearing a jam on conveyor C-17?",
                    "principal_roles": ["operations"],
                    "requires_review": False,
                },
                "assertions": [
                    {
                        "path": ["decision", "answer_record", "status"],
                        "operator": "equals",
                        "expected": "answered",
                    },
                    {
                        "path": ["retrieval", "forbidden_chunk_count"],
                        "operator": "equals",
                        "expected": 0,
                    },
                    {
                        "path": ["retrieval"],
                        "operator": "not_contains",
                        "expected": "SILVER-CEDAR",
                    },
                    {
                        "path": ["answer_artifact"],
                        "operator": "exists",
                    },
                    {
                        "path": ["retrieval_artifact"],
                        "operator": "exists",
                    },
                    {
                        "path": ["sync_artifact"],
                        "operator": "exists",
                    },
                ],
                "required_node_types": sorted(required - {"http_request"}),
            }
        },
    )
    validation = platform(
        "POST",
        args.platform_base,
        args.platform_token,
        f"/api/v1/applications/{application_id}/draft/validate",
    )
    if not validation["valid"]:
        raise RuntimeError(json.dumps(validation, ensure_ascii=False))

    args.workspace_path.mkdir(parents=True, exist_ok=True)
    tests = platform(
        "POST",
        args.platform_base,
        args.platform_token,
        f"/api/v1/applications/{application_id}/tests/run",
        {"workspace_path": str(args.workspace_path)},
    )
    if not tests["passed"]:
        raise RuntimeError(json.dumps(tests, ensure_ascii=False))
    test_items = tests.get("tests", tests.get("results", []))
    if not test_items:
        raise RuntimeError("platform test response did not include a test run record")
    result = {
        "application_id": application_id,
        "draft_revision": revision,
        "content_hash": validation["content_hash"],
        "node_count": len(desired_nodes()),
        "edge_count": len(desired_edges()),
        "platform_test_passed": True,
        "platform_test_run_id": test_items[0]["run_id"],
        "lilies_model_calls": 0,
        "lilies_tokens": 0,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
