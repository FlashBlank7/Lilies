#!/usr/bin/env python3
"""Build EXP-LILIES-006 using only the public Lilies API."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def ref(node_id: str, *path: str, optional: bool = False) -> dict[str, Any]:
    return {
        "$ref": {
            "node_id": node_id,
            "path": list(path),
            "optional": optional,
        }
    }


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
        "description": "",
        "position": {"x": x, "y": y},
        "config": config,
    }


def edge(
    edge_id: str,
    source: str,
    target: str,
    branch: str | None = None,
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


def input_schema() -> dict[str, Any]:
    point = {
        "type": "object",
        "properties": {
            "timestamp": {"type": "string", "minLength": 10},
            "value": {"type": "number", "minimum": 0},
        },
        "required": ["timestamp", "value"],
        "additionalProperties": False,
    }
    series = {
        "type": "object",
        "properties": {
            "series_id": {"type": "string", "minLength": 1},
            "points": {
                "type": "array",
                "minItems": 14,
                "maxItems": 5000,
                "items": point,
            },
        },
        "required": ["series_id", "points"],
        "additionalProperties": False,
    }
    item = {
        "type": "object",
        "properties": {
            "item_code": {"type": "string", "minLength": 1},
            "warehouse": {"type": "string", "minLength": 1},
            "inbound": {"type": "number", "minimum": 0},
            "safety_stock": {"type": "number", "minimum": 0},
            "moq": {"type": "number", "minimum": 0},
            "lot_size": {"type": "number", "exclusiveMinimum": 0},
            "unit_cost": {"type": "number", "minimum": 0},
            "minimum_fulfillment": {"type": "number", "minimum": 0, "maximum": 1},
            "priority_weight": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": [
            "item_code",
            "warehouse",
            "inbound",
            "safety_stock",
            "moq",
            "lot_size",
            "unit_cost",
            "minimum_fulfillment",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "batch_id": {"type": "string", "minLength": 3},
            "unit": {"type": "string", "minLength": 1},
            "forecast_horizon_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
            },
            "history": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1000,
                "items": series,
            },
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": item,
            },
            "constraints": {
                "type": "object",
                "properties": {
                    "capacity": {"type": "number", "exclusiveMinimum": 0},
                    "budget": {"type": "number", "exclusiveMinimum": 0},
                },
                "required": ["capacity", "budget"],
                "additionalProperties": False,
            },
            "company": {"type": "string", "minLength": 1},
            "schedule_date": {"type": "string", "minLength": 10},
        },
        "required": [
            "batch_id",
            "unit",
            "forecast_horizon_days",
            "history",
            "items",
            "constraints",
            "company",
            "schedule_date",
        ],
        "additionalProperties": False,
    }


def enrich_items_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            node(
                "item_input",
                "start",
                "One planning item and ERPNext inventory",
                0,
                0,
                {"inputs": [{"name": "item", "type": "object"}, {"name": "bins", "type": "array"}]},
            ),
            node(
                "match_bin",
                "record_match",
                "Match item and warehouse to one ERPNext bin",
                260,
                0,
                {
                    "source": ref("item_input", "item"),
                    "candidates": ref("item_input", "bins"),
                    "conditions": [
                        {
                            "name": "same-item",
                            "source_path": ["item_code"],
                            "candidate_path": ["item_code"],
                            "comparator": "exact",
                            "weight": 1,
                            "required": True,
                        },
                        {
                            "name": "same-warehouse",
                            "source_path": ["warehouse"],
                            "candidate_path": ["warehouse"],
                            "comparator": "exact",
                            "weight": 1,
                            "required": True,
                        },
                    ],
                    "conflict_checks": [],
                    "min_score": 1,
                    "ambiguity_threshold": 0,
                    "result_limit": 5,
                },
            ),
            node(
                "enrich_item",
                "variable_assigner",
                "Use ERPNext actual quantity in the planning item",
                520,
                0,
                {
                    "assignments": {
                        "item_code": ref("item_input", "item", "item_code"),
                        "warehouse": ref("item_input", "item", "warehouse"),
                        "inventory": ref("match_bin", "match", "candidate", "actual_qty"),
                        "inbound": ref("item_input", "item", "inbound"),
                        "safety_stock": ref("item_input", "item", "safety_stock"),
                        "moq": ref("item_input", "item", "moq"),
                        "lot_size": ref("item_input", "item", "lot_size"),
                        "unit_cost": ref("item_input", "item", "unit_cost"),
                        "minimum_fulfillment": ref("item_input", "item", "minimum_fulfillment"),
                        "priority_weight": {
                            "$coalesce": [
                                ref(
                                    "item_input",
                                    "item",
                                    "priority_weight",
                                    optional=True,
                                ),
                                1,
                            ]
                        },
                    }
                },
            ),
            node(
                "item_end",
                "end",
                "Return the inventory-backed planning item",
                780,
                0,
                {"outputs": {"item": ref("enrich_item", "output")}},
            ),
        ],
        "edges": [
            edge("item-start-match", "item_input", "match_bin"),
            edge("item-match-enrich", "match_bin", "enrich_item"),
            edge("item-enrich-end", "enrich_item", "item_end"),
        ],
    }


def delivery_lines_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            node(
                "line_input",
                "start",
                "One optimized plan line",
                0,
                0,
                {
                    "inputs": [
                        {"name": "line", "type": "object"},
                        {"name": "schedule_date", "type": "string"},
                    ]
                },
            ),
            node(
                "map_line",
                "variable_assigner",
                "Map a host-neutral line to one ERPNext draft item",
                260,
                0,
                {
                    "assignments": {
                        "item_code": ref("line_input", "line", "item_code"),
                        "qty": ref("line_input", "line", "order_quantity"),
                        "warehouse": ref("line_input", "line", "warehouse"),
                        "schedule_date": ref("line_input", "schedule_date"),
                    }
                },
            ),
            node(
                "line_end",
                "end",
                "Return one ERPNext item",
                520,
                0,
                {"outputs": {"item": ref("map_line", "output")}},
            ),
        ],
        "edges": [
            edge("line-start-map", "line_input", "map_line"),
            edge("line-map-end", "map_line", "line_end"),
        ],
    }


def desired_nodes(deployment_name: str) -> list[dict[str, Any]]:
    connector = {
        "connector_id": "erpnext-planning",
        "connector_version": 1,
        "tenant_id": "exp006-tenant",
        "actor_id": "workflow-planner",
        "actor_roles": ["planner"],
        "profile_id": "exp006-local",
        "authorization_id": "",
        "authorization_mode": "runtime_exact",
        "execution_mode": "execute",
    }
    return [
        node(
            "start",
            "start",
            "Planning batch",
            0,
            240,
            {
                "inputs": [
                    {"name": "batch_id", "type": "string"},
                    {"name": "unit", "type": "string"},
                    {"name": "forecast_horizon_days", "type": "number"},
                    {"name": "history", "type": "array"},
                    {"name": "items", "type": "array"},
                    {"name": "constraints", "type": "object"},
                    {"name": "company", "type": "string"},
                    {"name": "schedule_date", "type": "string"},
                ]
            },
        ),
        node(
            "prepare",
            "variable_assigner",
            "Assemble one validated planning request",
            240,
            240,
            {
                "assignments": {
                    "batch_id": ref("start", "batch_id"),
                    "unit": ref("start", "unit"),
                    "forecast_horizon_days": ref("start", "forecast_horizon_days"),
                    "history": ref("start", "history"),
                    "items": ref("start", "items"),
                    "constraints": ref("start", "constraints"),
                    "company": ref("start", "company"),
                    "schedule_date": ref("start", "schedule_date"),
                }
            },
        ),
        node(
            "validate",
            "json_schema_validate",
            "Reject invalid history, units, and constraints",
            480,
            240,
            {"value": ref("prepare", "output"), "schema": input_schema()},
        ),
        node(
            "validation_gate",
            "if_else",
            "Continue only with a valid planning request",
            720,
            240,
            {
                "cases": [
                    {
                        "id": "valid",
                        "conditions": [
                            {
                                "value": ref("validate", "valid"),
                                "operator": "equals",
                                "expected": True,
                            }
                        ],
                    }
                ],
                "default_branch": "invalid",
            },
        ),
        node(
            "invalid_result",
            "variable_assigner",
            "Return input validation evidence without side effects",
            960,
            40,
            {
                "assignments": {
                    "status": "invalid_input",
                    "batch_id": ref("start", "batch_id", optional=True),
                    "validation": ref("validate", "output"),
                    "host_write": False,
                }
            },
        ),
        node(
            "read_bins",
            "connector_action",
            "Read current inventory bins from ERPNext",
            960,
            240,
            {
                **connector,
                "operation_id": "listBins",
                "payload": {
                    "fields": '["item_code","warehouse","actual_qty"]',
                    "limit_page_length": 1000,
                },
                "idempotency_key": ref("start", "batch_id"),
            },
        ),
        node(
            "normalize_bins",
            "record_collection_normalize",
            "Normalize ERPNext inventory response",
            1200,
            240,
            {
                "value": ref("read_bins", "response"),
                "record_paths": [["data"]],
                "single_object_policy": "error",
                "empty_policy": "error",
            },
        ),
        node(
            "enrich_items",
            "iteration",
            "Bind every planning item to live ERPNext inventory",
            1440,
            240,
            {
                "items": ref("start", "items"),
                "variables": {"bins": ref("normalize_bins", "records")},
                "workflow": enrich_items_workflow(),
                "item_name": "item",
                "output_node_id": "item_end",
                "output_path": ["item"],
                "parallelism": 4,
            },
        ),
        node(
            "forecast",
            "deployed_forecast",
            "Forecast demand with the approved deployment",
            1680,
            240,
            {
                "deployment_name": deployment_name,
                "series": ref("start", "history"),
                "unit": ref("start", "unit"),
                "horizon": ref("start", "forecast_horizon_days"),
            },
        ),
        node(
            "ready_to_forecast",
            "variable_assigner",
            "Confirm live inventory enrichment before forecasting",
            1560,
            240,
            {
                "assignments": {
                    "inventory_backed_item_count": {"$length": ref("enrich_items", "items")}
                }
            },
        ),
        node(
            "plan",
            "replenishment_planner",
            "Optimize lot-sized replenishment under budget and capacity",
            1920,
            240,
            {
                "forecasts": ref("forecast", "forecasts"),
                "items": ref("enrich_items", "items"),
                "capacity": ref("start", "constraints", "capacity"),
                "budget": ref("start", "constraints", "budget"),
                "solver_version": "bounded-planner-v1",
            },
        ),
        node(
            "plan_gate",
            "if_else",
            "Write only a feasible plan",
            2160,
            240,
            {
                "cases": [
                    {
                        "id": "feasible",
                        "conditions": [
                            {
                                "value": ref("plan", "status"),
                                "operator": "equals",
                                "expected": "feasible",
                            }
                        ],
                    }
                ],
                "default_branch": "infeasible",
            },
        ),
        node(
            "infeasible_result",
            "variable_assigner",
            "Explain infeasibility without creating an ERPNext draft",
            2400,
            40,
            {
                "assignments": {
                    "status": "infeasible",
                    "batch_id": ref("start", "batch_id"),
                    "plan": ref("plan", "output"),
                    "host_write": False,
                }
            },
        ),
        node(
            "human_review",
            "human_input",
            "Inventory planner approval",
            2400,
            240,
            {
                "title": "Review replenishment plan",
                "description": "Review forecasts, cost, capacity, binding constraints, and model monitoring before creating an ERPNext draft.",
                "fields": [
                    {
                        "name": "decision",
                        "label": "Decision",
                        "type": "string",
                        "required": True,
                        "options": ["approve", "reject"],
                    },
                    {
                        "name": "reviewer",
                        "label": "Reviewer",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "name": "comment",
                        "label": "Comment",
                        "type": "string",
                        "required": True,
                    },
                ],
            },
        ),
        node(
            "approval_gate",
            "if_else",
            "Respect the planner decision",
            2640,
            240,
            {
                "cases": [
                    {
                        "id": "approved",
                        "conditions": [
                            {
                                "value": ref("human_review", "decision"),
                                "operator": "equals",
                                "expected": "approve",
                            }
                        ],
                    }
                ],
                "default_branch": "rejected",
            },
        ),
        node(
            "rejected_result",
            "variable_assigner",
            "Record rejection without writing ERPNext",
            2880,
            40,
            {
                "assignments": {
                    "status": "rejected",
                    "batch_id": ref("start", "batch_id"),
                    "review": ref("human_review", "output"),
                    "host_write": False,
                }
            },
        ),
        node(
            "map_delivery_lines",
            "iteration",
            "Map approved order lines to ERPNext draft items",
            2880,
            240,
            {
                "items": ref("plan", "lines"),
                "variables": {"schedule_date": ref("start", "schedule_date")},
                "workflow": delivery_lines_workflow(),
                "item_name": "line",
                "output_node_id": "line_end",
                "output_path": ["item"],
                "parallelism": 4,
            },
        ),
        node(
            "approved_lines",
            "variable_aggregator",
            "Carry only an approved plan into ERPNext mapping",
            2760,
            240,
            {
                "variables": [ref("plan", "lines")],
                "mode": "first_non_null",
            },
        ),
        node(
            "plan_identity",
            "variable_assigner",
            "Create stable host identity and read-before-write filter",
            3120,
            240,
            {
                "assignments": {
                    "title": {
                        "$concat": [
                            "Lilies replenishment ",
                            ref("start", "batch_id"),
                        ]
                    },
                    "filters": {
                        "$concat": [
                            '[["Material Request","title","=","Lilies replenishment ',
                            ref("start", "batch_id"),
                            '"]]',
                        ]
                    },
                }
            },
        ),
        node(
            "lookup_existing",
            "connector_action",
            "Read before write to prevent a duplicate Material Request",
            3360,
            240,
            {
                **connector,
                "operation_id": "listMaterialRequests",
                "payload": {
                    "fields": '["name","docstatus","title","status"]',
                    "filters": ref("plan_identity", "output", "filters"),
                    "limit_page_length": 2,
                },
                "idempotency_key": ref("plan_identity", "output", "title"),
            },
        ),
        node(
            "normalize_existing",
            "record_collection_normalize",
            "Normalize existing Material Requests",
            3600,
            240,
            {
                "value": ref("lookup_existing", "response"),
                "record_paths": [["data"]],
                "single_object_policy": "error",
                "empty_policy": "allow",
            },
        ),
        node(
            "existing_gate",
            "if_else",
            "Reuse an existing draft or create exactly one",
            3840,
            240,
            {
                "cases": [
                    {
                        "id": "create",
                        "conditions": [
                            {
                                "value": ref("normalize_existing", "records"),
                                "operator": "empty",
                            }
                        ],
                    }
                ],
                "default_branch": "reuse",
            },
        ),
        node(
            "create_draft",
            "connector_action",
            "Create one ERPNext Material Request draft",
            4080,
            160,
            {
                **connector,
                "operation_id": "createMaterialRequestDraft",
                "payload": {
                    "body": {
                        "material_request_type": "Purchase",
                        "company": ref("start", "company"),
                        "schedule_date": ref("start", "schedule_date"),
                        "title": ref("plan_identity", "output", "title"),
                        "docstatus": 0,
                        "items": ref("map_delivery_lines", "items"),
                    }
                },
                "idempotency_key": ref("plan_identity", "output", "title"),
            },
        ),
        node(
            "read_created",
            "connector_action",
            "Read back the created ERPNext draft",
            4320,
            160,
            {
                **connector,
                "operation_id": "getMaterialRequest",
                "payload": {"name": ref("create_draft", "response", "data", "name")},
                "idempotency_key": ref("plan_identity", "output", "title"),
            },
        ),
        node(
            "read_reused",
            "connector_action",
            "Read back the existing ERPNext draft",
            4080,
            360,
            {
                **connector,
                "operation_id": "getMaterialRequest",
                "payload": {"name": ref("normalize_existing", "records", "0", "name")},
                "idempotency_key": ref("plan_identity", "output", "title"),
            },
        ),
        node(
            "created_result",
            "variable_assigner",
            "Record the created draft and readback",
            4560,
            160,
            {
                "assignments": {
                    "status": "draft_created",
                    "batch_id": ref("start", "batch_id"),
                    "review": ref("human_review", "output"),
                    "write_receipt": ref("create_draft", "receipt"),
                    "readback": ref("read_created", "response", "data"),
                    "host_write": True,
                }
            },
        ),
        node(
            "reused_result",
            "variable_assigner",
            "Record idempotent reuse and readback",
            4320,
            360,
            {
                "assignments": {
                    "status": "draft_reused",
                    "batch_id": ref("start", "batch_id"),
                    "review": ref("human_review", "output"),
                    "write_receipt": None,
                    "readback": ref("read_reused", "response", "data"),
                    "host_write": False,
                }
            },
        ),
        node(
            "result",
            "variable_aggregator",
            "Select the one active business outcome",
            4800,
            240,
            {
                "variables": [
                    ref("created_result", "output", optional=True),
                    ref("reused_result", "output", optional=True),
                    ref("rejected_result", "output", optional=True),
                    ref("infeasible_result", "output", optional=True),
                    ref("invalid_result", "output", optional=True),
                ],
                "mode": "first_non_null",
            },
        ),
        node(
            "forecast_artifact",
            "typed_json_artifact",
            "Write forecast, model lineage, and retraining evidence",
            5040,
            180,
            {
                "value": {
                    "batch_id": ref("start", "batch_id", optional=True),
                    "forecast": ref("forecast", "output", optional=True),
                    "production_training_started": False,
                },
                "filename": "forecast-evidence.json",
                "lineage": [
                    {"source_type": "workflow_input", "reference": "history"},
                    {
                        "source_type": "external_resource",
                        "reference": f"forecast-deployment:{deployment_name}",
                    },
                ],
            },
        ),
        node(
            "plan_artifact",
            "typed_json_artifact",
            "Write constraint balances, objective, and infeasibility evidence",
            5280,
            240,
            {
                "value": {
                    "batch_id": ref("start", "batch_id", optional=True),
                    "inventory_receipt": ref("read_bins", "receipt", optional=True),
                    "plan": ref("plan", "output", optional=True),
                },
                "filename": "replenishment-plan.json",
                "lineage": [
                    {"source_type": "connector_receipt", "reference": "read_bins.receipt"},
                    {"source_type": "node_output", "reference": "plan.output"},
                ],
            },
        ),
        node(
            "delivery_artifact",
            "typed_json_artifact",
            "Write planner decision and ERPNext readback",
            5520,
            300,
            {
                "value": ref("result", "output"),
                "filename": "erpnext-delivery.json",
                "lineage": [
                    {"source_type": "node_output", "reference": "result.output"},
                    {
                        "source_type": "connector_receipt",
                        "reference": "create_draft.receipt",
                    },
                ],
            },
        ),
        node(
            "end",
            "end",
            "Return the business outcome and three audit artifacts",
            5760,
            240,
            {
                "outputs": {
                    "result": ref("result", "output"),
                    "forecast": ref("forecast", "output", optional=True),
                    "plan": ref("plan", "output", optional=True),
                    "forecast_artifact": ref("forecast_artifact", "artifact"),
                    "plan_artifact": ref("plan_artifact", "artifact"),
                    "delivery_artifact": ref("delivery_artifact", "artifact"),
                }
            },
        ),
    ]


def desired_edges() -> list[dict[str, Any]]:
    values = [
        edge("start-prepare", "start", "prepare"),
        edge("prepare-validate", "prepare", "validate"),
        edge("validate-gate", "validate", "validation_gate"),
        edge("gate-invalid", "validation_gate", "invalid_result", "invalid"),
        edge("gate-read", "validation_gate", "read_bins", "valid"),
        edge("read-normalize", "read_bins", "normalize_bins"),
        edge(
            "normalize-enrich",
            "normalize_bins",
            "enrich_items",
            source_port="records",
        ),
        edge(
            "enrich-ready",
            "enrich_items",
            "ready_to_forecast",
            source_port="items",
        ),
        edge("ready-forecast", "ready_to_forecast", "forecast"),
        edge("forecast-plan", "forecast", "plan"),
        edge("plan-gate", "plan", "plan_gate"),
        edge("plan-infeasible", "plan_gate", "infeasible_result", "infeasible"),
        edge("plan-review", "plan_gate", "human_review", "feasible"),
        edge("review-gate", "human_review", "approval_gate"),
        edge("gate-rejected", "approval_gate", "rejected_result", "rejected"),
        edge("gate-approved", "approval_gate", "approved_lines", "approved"),
        edge("approved-map", "approved_lines", "map_delivery_lines"),
        edge(
            "map-identity",
            "map_delivery_lines",
            "plan_identity",
            source_port="items",
        ),
        edge("identity-lookup", "plan_identity", "lookup_existing"),
        edge("lookup-normalize", "lookup_existing", "normalize_existing"),
        edge("normalize-gate", "normalize_existing", "existing_gate"),
        edge("gate-create", "existing_gate", "create_draft", "create"),
        edge("create-read", "create_draft", "read_created"),
        edge("read-created-result", "read_created", "created_result"),
        edge("gate-reuse", "existing_gate", "read_reused", "reuse"),
        edge("read-reused-result", "read_reused", "reused_result"),
    ]
    for source in (
        "created_result",
        "reused_result",
        "rejected_result",
        "infeasible_result",
        "invalid_result",
    ):
        values.append(edge(f"{source}-result", source, "result"))
    values.extend(
        [
            edge("result-forecast-artifact", "result", "forecast_artifact"),
            edge("forecast-plan-artifact", "forecast_artifact", "plan_artifact"),
            edge("plan-delivery-artifact", "plan_artifact", "delivery_artifact"),
            edge("delivery-end", "delivery_artifact", "end"),
        ]
    )
    return values


class PublicApi:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            if body is not None
            else None
        )
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
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


def apply_workflow(
    api: PublicApi,
    application_id: str,
    deployment_name: str,
) -> dict[str, Any]:
    draft = api.request("GET", f"/api/v1/applications/{application_id}/draft")
    workflow = draft["snapshot"]["workflow"]
    current_nodes = {item["id"]: item for item in workflow["nodes"]}
    current_edges = {item["id"]: item for item in workflow["edges"]}
    revision = int(draft["revision"])
    path = f"/api/v1/applications/{application_id}/draft"
    for item in desired_nodes(deployment_name):
        current = current_nodes.get(item["id"])
        operation = "add_node"
        data: dict[str, Any] = {"node": item}
        if current is not None:
            comparable = {
                key: current[key]
                for key in ("id", "type", "title", "description", "position", "config")
            }
            if comparable == item:
                continue
            operation = "update_node"
            data = {
                "node_id": item["id"],
                "changes": {key: value for key, value in item.items() if key != "id"},
                "merge_config": False,
            }
        response = api.request(
            "POST",
            path,
            {
                "expected_revision": revision,
                "idempotency_key": f"exp006-r1-{operation}-{item['id']}-v3",
                "op": operation,
                "data": data,
            },
        )
        revision = int(response["revision"])
    for item in desired_edges():
        if item["id"] in current_edges:
            continue
        response = api.request(
            "POST",
            path,
            {
                "expected_revision": revision,
                "idempotency_key": f"exp006-r1-edge-{item['id']}",
                "op": "add_edge",
                "data": {"edge": item},
            },
        )
        revision = int(response["revision"])
    validation = api.request("POST", f"/api/v1/applications/{application_id}/draft/validate")
    return {
        "application_id": application_id,
        "revision": revision,
        "node_count": len(desired_nodes(deployment_name)),
        "edge_count": len(desired_edges()),
        "validation": validation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8018")
    parser.add_argument("--token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--deployment-name", default="exp006-demand-production")
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()
    result = apply_workflow(
        PublicApi(args.base_url, args.token),
        args.application_id,
        args.deployment_name,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
