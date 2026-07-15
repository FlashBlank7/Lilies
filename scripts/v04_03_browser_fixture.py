#!/usr/bin/env python3
"""Create deterministic v0.4.3 Studio fixtures through the public API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


def mutate(
    client: httpx.Client,
    application_id: str,
    revision: int,
    operation: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": operation,
            "data": data,
        },
    )
    response.raise_for_status()
    return response.json()


def create_application(
    client: httpx.Client,
    *,
    name: str,
    requirement: str,
    delivery_mode: str,
    governed_hard_gate: bool = False,
) -> str:
    response = client.post(
        "/api/v1/applications",
        json={
            "name": name,
            "requirement": requirement,
            "delivery_mode": delivery_mode,
            "governed_hard_gate": governed_hard_gate,
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


def stale_config_fixture(client: httpx.Client) -> str:
    application_id = create_application(
        client,
        name="v0.4.3 Stale Evidence and Block Forms",
        requirement="Let an operations user inspect a verified result, then configure a visible model, HTTP request, and bounded loop.",
        delivery_mode="guided",
    )
    revision = 0
    for node in (
        {"id": "start", "type": "start", "title": "Customer input", "config": {"inputs": []}},
        {
            "id": "end",
            "type": "end",
            "title": "Customer result",
            "config": {"outputs": {"answer": "ready"}},
        },
    ):
        changed = mutate(client, application_id, revision, "add_node", {"node": node})
        revision = changed["revision"]
    changed = mutate(
        client,
        application_id,
        revision,
        "add_edge",
        {"edge": {"id": "initial", "source": "start", "target": "end"}},
    )
    revision = changed["revision"]
    changed = mutate(
        client,
        application_id,
        revision,
        "add_test",
        {
            "test": {
                "id": "visible-result",
                "name": "Visible customer result",
                "requirement": "The customer can read a result.",
                "assertions": [{"path": ["answer"], "operator": "equals", "expected": "ready"}],
                "mandatory": True,
            }
        },
    )
    revision = changed["revision"]
    tested = client.post(f"/api/v1/applications/{application_id}/tests/run")
    tested.raise_for_status()
    if tested.json().get("passed") is not True:
        raise RuntimeError(f"fixture baseline did not pass: {tested.text}")

    changed = mutate(client, application_id, revision, "remove_edge", {"edge_id": "initial"})
    revision = changed["revision"]
    nested = {
        "nodes": [
            {"id": "loop-start", "type": "start", "title": "Iteration", "config": {"inputs": []}},
            {
                "id": "loop-end",
                "type": "end",
                "title": "Iteration result",
                "config": {"outputs": {"done": True}},
            },
        ],
        "edges": [{"id": "nested", "source": "loop-start", "target": "loop-end"}],
    }
    for node in (
        {
            "id": "model",
            "type": "llm",
            "title": "Summarize request",
            "description": "Turn the customer request into a concise operational summary.",
            "config": {
                "system": "Return a concise operational summary.",
                "prompt": "Summarize the current customer request.",
                "model": "",
                "temperature": 0.2,
                "x_fixture_extension": {"preserve": True},
            },
        },
        {
            "id": "http",
            "type": "http_request",
            "title": "Send approved summary",
            "description": "Demonstrates human-readable HTTP settings without running the external call.",
            "config": {
                "method": "POST",
                "url": "https://example.com/customer-summary",
                "headers": {"X-Workflow": "lilies-v043"},
                "query": {},
                "body": {"status": "prepared"},
                "timeout_seconds": 20,
            },
        },
        {
            "id": "loop",
            "type": "loop",
            "title": "Bounded review loop",
            "description": "Stop after the first successful nested review and save its checkpoint.",
            "config": {
                "workflow": nested,
                "variables": {},
                "break_condition": {"value": False, "operator": "equals", "expected": True},
                "break_value": True,
                "max_iterations": 3,
                "output_node_id": "loop-end",
                "checkpoint_each_iteration": True,
            },
        },
    ):
        changed = mutate(client, application_id, revision, "add_node", {"node": node})
        revision = changed["revision"]
    for edge in (
        {"id": "start-model", "source": "start", "target": "model"},
        {"id": "model-http", "source": "model", "target": "http"},
        {"id": "http-loop", "source": "http", "target": "loop"},
        {"id": "loop-end", "source": "loop", "target": "end"},
    ):
        changed = mutate(client, application_id, revision, "add_edge", {"edge": edge})
        revision = changed["revision"]
    return application_id


def repair_fixture(client: httpx.Client) -> str:
    application_id = create_application(
        client,
        name="v0.4.3 Failed Acceptance Repair",
        requirement="Show a failed safety acceptance case and let the operator preview and apply a visible repair.",
        delivery_mode="quick",
    )
    revision = 0
    for node in (
        {"id": "start", "type": "start", "title": "Input", "config": {"inputs": []}},
        {"id": "end", "type": "end", "title": "Result", "config": {"outputs": {"answer": "ready"}}},
    ):
        changed = mutate(client, application_id, revision, "add_node", {"node": node})
        revision = changed["revision"]
    changed = mutate(
        client,
        application_id,
        revision,
        "add_edge",
        {"edge": {"id": "start-end", "source": "start", "target": "end"}},
    )
    revision = changed["revision"]
    mutate(
        client,
        application_id,
        revision,
        "add_test",
        {
            "test": {
                "id": "permission-boundary",
                "name": "Permission boundary is visible",
                "requirement": "A permission gate must be visible before customer output.",
                "required_node_types": ["permission_gate"],
                "assertions": [{"path": ["answer"], "operator": "exists"}],
                "mandatory": True,
            }
        },
    )
    failed = client.post(f"/api/v1/applications/{application_id}/tests/run")
    failed.raise_for_status()
    if failed.json().get("passed") is not False or not failed.json().get("tests"):
        raise RuntimeError(f"fixture preflight failure was not structured: {failed.text}")
    return application_id


def governed_fixture(client: httpx.Client) -> str:
    return create_application(
        client,
        name="v0.4.3 Governed Publication Block",
        requirement="Block publication until governed evidence is current.",
        delivery_mode="governed",
        governed_hard_gate=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--token", default="mambaout")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"},
        timeout=30,
    ) as client:
        fixtures = {
            "schema_version": "1.0",
            "base_url": args.base_url,
            "stale_config_application_id": stale_config_fixture(client),
            "repair_application_id": repair_fixture(client),
            "governed_application_id": governed_fixture(client),
        }
    payload = json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
