#!/usr/bin/env python3
"""Add frozen public business tests to an EXP-LILIES-006 draft."""

from __future__ import annotations

import argparse
import copy
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def call(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if body is not None
        else None
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error
    return json.loads(payload) if payload else None


def workflow_inputs(fixture: dict[str, Any], batch_id: str) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "unit": fixture["unit"],
        "forecast_horizon_days": fixture["forecast_horizon_days"],
        "history": fixture["history"],
        "items": fixture["items"],
        "constraints": fixture["constraints"],
        "company": "Lilies Planning",
        "schedule_date": "2026-08-15",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8018")
    parser.add_argument("--token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--batch-prefix",
        default="PUBLIC-PLAN",
        help="Fresh stable business-identity prefix for side-effecting tests.",
    )
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    primary = workflow_inputs(fixture, f"{args.batch_prefix}-001")
    rejected = workflow_inputs(fixture, f"{args.batch_prefix}-REJECT")
    infeasible = workflow_inputs(fixture, f"{args.batch_prefix}-INFEASIBLE")
    infeasible["constraints"] = {"capacity": 10, "budget": 1000}
    tests = [
        {
            "id": "public-feasible-approved",
            "name": "可行计划经人工批准后只创建一个 ERPNext 草稿",
            "requirement": "使用实际 ERPNext 库存、批准的预测模型和冻结约束生成 30/20/50 补货量；人工批准后创建 docstatus=0 草稿并读回。",
            "inputs": primary,
            "simulated_human_inputs": {
                "human_review": {
                    "decision": "approve",
                    "reviewer": "public-planner",
                    "comment": "Forecast and constraints reviewed",
                }
            },
            "assertions": [
                {
                    "path": ["result", "status"],
                    "operator": "equals",
                    "expected": "draft_created",
                },
                {
                    "path": ["result", "readback", "docstatus"],
                    "operator": "equals",
                    "expected": 0,
                },
                {
                    "path": ["plan", "status"],
                    "operator": "equals",
                    "expected": "feasible",
                },
                {
                    "path": ["plan", "lines"],
                    "operator": "min_length",
                    "expected": 3,
                },
                {
                    "path": ["forecast", "monitoring", "automatic_training_triggered"],
                    "operator": "equals",
                    "expected": False,
                },
            ],
            "required_node_types": [
                "deployed_forecast",
                "replenishment_planner",
                "human_input",
                "connector_action",
                "typed_json_artifact",
            ],
            "mandatory": True,
        },
        {
            "id": "public-feasible-rejected",
            "name": "可行计划被人工拒绝后不写 ERPNext",
            "requirement": "计划虽然可行，但库存计划员拒绝时必须保留理由并安全停写。",
            "inputs": rejected,
            "simulated_human_inputs": {
                "human_review": {
                    "decision": "reject",
                    "reviewer": "public-planner",
                    "comment": "Hold for supplier negotiation",
                }
            },
            "assertions": [
                {
                    "path": ["result", "status"],
                    "operator": "equals",
                    "expected": "rejected",
                },
                {
                    "path": ["result", "host_write"],
                    "operator": "equals",
                    "expected": False,
                },
            ],
            "required_node_types": [
                "deployed_forecast",
                "replenishment_planner",
                "human_input",
            ],
            "mandatory": True,
        },
        {
            "id": "public-infeasible",
            "name": "预算和能力不足时解释缺口且不写 ERPNext",
            "requirement": "共享能力和预算低于最低履约量时返回两个资源缺口，不进入人工审批或客户系统写入。",
            "inputs": infeasible,
            "assertions": [
                {
                    "path": ["result", "status"],
                    "operator": "equals",
                    "expected": "infeasible",
                },
                {
                    "path": ["result", "host_write"],
                    "operator": "equals",
                    "expected": False,
                },
                {
                    "path": ["plan", "binding_constraints"],
                    "operator": "contains",
                    "expected": "capacity",
                },
                {
                    "path": ["plan", "binding_constraints"],
                    "operator": "contains",
                    "expected": "budget",
                },
            ],
            "required_node_types": [
                "deployed_forecast",
                "replenishment_planner",
            ],
            "mandatory": True,
        },
    ]
    draft = call(
        "GET",
        args.base_url,
        args.token,
        f"/api/v1/applications/{args.application_id}/draft",
    )
    revision = int(draft["revision"])
    existing = {item["id"] for item in draft["snapshot"]["tests"]}
    for test in tests:
        if test["id"] in existing:
            continue
        response = call(
            "POST",
            args.base_url,
            args.token,
            f"/api/v1/applications/{args.application_id}/draft",
            {
                "expected_revision": revision,
                "idempotency_key": f"exp006-r1-test-{test['id']}",
                "op": "add_test",
                "data": {"test": copy.deepcopy(test)},
            },
        )
        revision = int(response["revision"])
    validation = call(
        "POST",
        args.base_url,
        args.token,
        f"/api/v1/applications/{args.application_id}/draft/validate",
    )
    print(
        json.dumps(
            {
                "application_id": args.application_id,
                "revision": revision,
                "test_count": len(tests),
                "validation": validation,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
