#!/usr/bin/env python3
"""Build EXP-LILIES-004 through the public Lilies application API."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any


FEATURE_UNITS = {
    "temperature_c": "Cel",
    "vibration_rms_mm_s": "mm/s",
    "current_a": "A",
    "pressure_bar": "bar",
    "rpm": "rpm",
}


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
    *,
    description: str = "",
    retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        "id": node_id,
        "type": block_type,
        "title": title,
        "description": description,
        "position": {"x": x, "y": y},
        "config": config,
    }
    if retry is not None:
        value["retry"] = retry
    return value


def edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    source_handle: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "source_port": "output",
        "target_port": "input",
    }
    if source_handle is not None:
        value["source_port"] = "branch"
        value["branch"] = source_handle
    return value


def desired_nodes() -> list[dict[str, Any]]:
    optional_telemetry = {
        feature: ref(
            "read_telemetry",
            "response",
            feature,
            "0",
            "value",
            optional=True,
        )
        for feature in FEATURE_UNITS
    }
    signal_schema = {
        "type": "object",
        "properties": {
            "device_id": {"type": "string", "minLength": 1},
            "event_id": {"type": "string", "minLength": 1},
            "event_ts": {"type": "number", "minimum": 1},
            "features": {
                "type": "object",
                "properties": {
                    "temperature_c": {
                        "type": "number",
                        "minimum": -20,
                        "maximum": 180,
                    },
                    "vibration_rms_mm_s": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 50,
                    },
                    "current_a": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "pressure_bar": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                    },
                    "rpm": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 5000,
                    },
                },
                "required": list(FEATURE_UNITS),
                "additionalProperties": False,
            },
            "units": {
                "type": "object",
                "properties": {
                    feature: {"type": "string", "enum": [unit]}
                    for feature, unit in FEATURE_UNITS.items()
                },
                "required": list(FEATURE_UNITS),
                "additionalProperties": False,
            },
        },
        "required": [
            "device_id",
            "event_id",
            "event_ts",
            "features",
            "units",
        ],
        "additionalProperties": False,
    }
    connector_base = {
        "connector_id": "thingsboard-rest",
        "connector_version": 2,
        "tenant_id": "exp004-tenant",
        "actor_id": "workflow-operator",
        "actor_roles": ["operator"],
        "profile_id": "exp004-fault",
        "authorization_id": "",
        "execution_mode": "execute",
    }
    prediction_details = {
        "event_id": ref("start", "event_id"),
        "probability": ref("risk_inference", "probability"),
        "confidence": ref("risk_inference", "confidence"),
        "model_id": ref("risk_inference", "model_id"),
        "model_version": ref("risk_inference", "version"),
        "model_digest": ref("risk_inference", "model_digest"),
    }
    nodes = [
        node(
            "start",
            "start",
            "Equipment event input",
            0,
            180,
            {
                "inputs": [
                    {
                        "name": "device_id",
                        "label": "ThingsBoard device id",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "name": "event_id",
                        "label": "Stable event id",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "name": "units",
                        "label": "Declared telemetry units",
                        "type": "object",
                        "required": True,
                    },
                    {
                        "name": "minimum_event_ts",
                        "label": "Oldest accepted timestamp",
                        "type": "number",
                        "required": True,
                    },
                    {
                        "name": "last_accepted_ts",
                        "label": "Last accepted timestamp",
                        "type": "number",
                        "required": True,
                    },
                    {
                        "name": "high_risk_threshold",
                        "label": "Automatic alarm probability threshold",
                        "type": "number",
                        "required": False,
                        "default": 0.8,
                    },
                    {
                        "name": "review_low_threshold",
                        "label": "No-alarm probability threshold",
                        "type": "number",
                        "required": False,
                        "default": 0.35,
                    },
                    {
                        "name": "auto_confidence_threshold",
                        "label": "Automatic action confidence threshold",
                        "type": "number",
                        "required": False,
                        "default": 0.8,
                    },
                    {
                        "name": "drift_observations",
                        "label": "Recent feature observations",
                        "type": "array",
                        "required": True,
                    },
                ]
            },
        ),
        node(
            "read_telemetry",
            "connector_action",
            "Read latest ThingsBoard telemetry",
            260,
            180,
            {
                **connector_base,
                "operation_id": "getLatestTimeseries",
                "payload": {
                    "entityType": "DEVICE",
                    "entityId": ref("start", "device_id"),
                    "keys": ",".join(FEATURE_UNITS),
                    "useStrictDataTypes": True,
                },
                "idempotency_key": ref("start", "event_id"),
                "authorization_mode": "explicit",
            },
            retry={
                "enabled": True,
                "max_attempts": 3,
                "delay_seconds": 0.2,
            },
        ),
        node(
            "prepare_signal",
            "variable_assigner",
            "Assemble governed model signal",
            520,
            180,
            {
                "assignments": {
                    "device_id": ref("start", "device_id"),
                    "event_id": ref("start", "event_id"),
                    "event_ts": ref(
                        "read_telemetry",
                        "response",
                        "temperature_c",
                        "0",
                        "ts",
                        optional=True,
                    ),
                    "features": optional_telemetry,
                    "units": ref("start", "units"),
                }
            },
        ),
        node(
            "validate_signal",
            "json_schema_validate",
            "Validate fields, ranges, and units",
            780,
            180,
            {
                "value": ref("prepare_signal", "output"),
                "schema": signal_schema,
                "max_errors": 25,
            },
        ),
        node(
            "structure_router",
            "if_else",
            "Accept or safely reject malformed signal",
            1040,
            180,
            {
                "cases": [
                    {
                        "id": "valid",
                        "conditions": [
                            {
                                "value": ref("validate_signal", "valid"),
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
            "invalid_decision",
            "variable_assigner",
            "Stop malformed or unit-conflicted event",
            1300,
            40,
            {
                "assignments": {
                    "action": "safe_stop",
                    "reason": "invalid_features_ranges_or_units",
                    "event_id": ref("start", "event_id"),
                    "validation_errors": ref("validate_signal", "errors"),
                    "external_write": False,
                }
            },
        ),
        node(
            "time_router",
            "if_else",
            "Reject stale or out-of-order telemetry",
            1300,
            260,
            {
                "cases": [
                    {
                        "id": "eligible",
                        "conditions": [
                            {
                                "value": ref(
                                    "prepare_signal",
                                    "output",
                                    "event_ts",
                                ),
                                "operator": "gte",
                                "expected": ref("start", "minimum_event_ts"),
                            },
                            {
                                "value": ref(
                                    "prepare_signal",
                                    "output",
                                    "event_ts",
                                ),
                                "operator": "gt",
                                "expected": ref("start", "last_accepted_ts"),
                            },
                        ],
                        "logical_operator": "and",
                    }
                ],
                "default_branch": "stale_or_out_of_order",
            },
        ),
        node(
            "time_rejected_decision",
            "variable_assigner",
            "Stop stale or out-of-order event",
            1560,
            100,
            {
                "assignments": {
                    "action": "safe_stop",
                    "reason": "stale_or_out_of_order",
                    "event_id": ref("start", "event_id"),
                    "event_ts": ref(
                        "prepare_signal",
                        "output",
                        "event_ts",
                    ),
                    "minimum_event_ts": ref("start", "minimum_event_ts"),
                    "last_accepted_ts": ref("start", "last_accepted_ts"),
                    "external_write": False,
                }
            },
        ),
        node(
            "eligible_signal",
            "variable_assigner",
            "Confirm eligible signal for inference",
            1560,
            300,
            {
                "assignments": {
                    "features": ref(
                        "prepare_signal",
                        "output",
                        "features",
                    ),
                    "units": ref(
                        "prepare_signal",
                        "output",
                        "units",
                    ),
                }
            },
        ),
        node(
            "risk_inference",
            "deployed_model_inference",
            "Run approved predictive-risk deployment",
            1820,
            340,
            {
                "deployment_name": "exp004-predictive-risk-debug",
                "features": ref(
                    "eligible_signal",
                    "output",
                    "features",
                ),
                "units": ref("eligible_signal", "output", "units"),
            },
        ),
        node(
            "risk_router",
            "if_else",
            "Choose automatic alarm, no alarm, or review",
            2080,
            340,
            {
                "cases": [
                    {
                        "id": "automatic_alarm",
                        "conditions": [
                            {
                                "value": ref(
                                    "risk_inference",
                                    "probability",
                                ),
                                "operator": "gte",
                                "expected": ref(
                                    "start",
                                    "high_risk_threshold",
                                ),
                            },
                            {
                                "value": ref(
                                    "risk_inference",
                                    "confidence",
                                ),
                                "operator": "gte",
                                "expected": ref(
                                    "start",
                                    "auto_confidence_threshold",
                                ),
                            },
                        ],
                        "logical_operator": "and",
                    },
                    {
                        "id": "no_alarm",
                        "conditions": [
                            {
                                "value": ref(
                                    "risk_inference",
                                    "probability",
                                ),
                                "operator": "lt",
                                "expected": ref(
                                    "start",
                                    "review_low_threshold",
                                ),
                            }
                        ],
                    },
                ],
                "default_branch": "human_review",
            },
        ),
        node(
            "automatic_alarm",
            "connector_action",
            "Create governed automatic alarm",
            2340,
            180,
            {
                **connector_base,
                "operation_id": "saveAlarm",
                "payload": {
                    "body": {
                        "type": "Predictive maintenance risk",
                        "originator": {
                            "entityType": "DEVICE",
                            "id": ref("start", "device_id"),
                        },
                        "severity": "CRITICAL",
                        "startTs": ref(
                            "prepare_signal",
                            "output",
                            "event_ts",
                        ),
                        "details": {
                            **prediction_details,
                            "decision": "automatic_high_risk",
                        },
                        "propagate": False,
                    }
                },
                "idempotency_key": ref("start", "event_id"),
                "authorization_mode": "runtime_exact",
            },
            retry={
                "enabled": True,
                "max_attempts": 3,
                "delay_seconds": 0.2,
            },
        ),
        node(
            "no_alarm_decision",
            "variable_assigner",
            "Record low-risk no-alarm decision",
            2340,
            320,
            {
                "assignments": {
                    "action": "no_alarm",
                    "reason": "risk_below_review_threshold",
                    "event_id": ref("start", "event_id"),
                    "probability": ref("risk_inference", "probability"),
                    "confidence": ref("risk_inference", "confidence"),
                    "external_write": False,
                }
            },
        ),
        node(
            "human_review",
            "human_input",
            "Review uncertain risk",
            2340,
            480,
            {
                "title": "Review uncertain predictive-maintenance risk",
                "description": (
                    "Inspect the traceable probability, confidence, telemetry, "
                    "and model evidence before deciding whether an alarm is needed."
                ),
                "fields": [
                    {
                        "name": "approved",
                        "label": "Create an alarm",
                        "type": "boolean",
                        "required": True,
                    },
                    {
                        "name": "review_note",
                        "label": "Review note",
                        "type": "string",
                        "required": False,
                    },
                ],
            },
        ),
        node(
            "review_router",
            "if_else",
            "Apply the human decision",
            2600,
            480,
            {
                "cases": [
                    {
                        "id": "approved",
                        "conditions": [
                            {
                                "value": ref(
                                    "human_review",
                                    "output",
                                    "approved",
                                ),
                                "operator": "equals",
                                "expected": True,
                            }
                        ],
                    }
                ],
                "default_branch": "rejected",
            },
        ),
        node(
            "reviewed_alarm",
            "connector_action",
            "Create human-approved alarm",
            2860,
            400,
            {
                **connector_base,
                "operation_id": "saveAlarm",
                "payload": {
                    "body": {
                        "type": "Predictive maintenance risk",
                        "originator": {
                            "entityType": "DEVICE",
                            "id": ref("start", "device_id"),
                        },
                        "severity": "MAJOR",
                        "startTs": ref(
                            "prepare_signal",
                            "output",
                            "event_ts",
                        ),
                        "details": {
                            **prediction_details,
                            "decision": "human_approved_uncertain_risk",
                            "review_note": ref(
                                "human_review",
                                "output",
                                "review_note",
                                optional=True,
                            ),
                        },
                        "propagate": False,
                    }
                },
                "idempotency_key": ref("start", "event_id"),
                "authorization_mode": "runtime_exact",
            },
            retry={
                "enabled": True,
                "max_attempts": 3,
                "delay_seconds": 0.2,
            },
        ),
        node(
            "review_rejected_decision",
            "variable_assigner",
            "Record reviewer rejection",
            2860,
            560,
            {
                "assignments": {
                    "action": "no_alarm",
                    "reason": "human_rejected_uncertain_risk",
                    "event_id": ref("start", "event_id"),
                    "probability": ref("risk_inference", "probability"),
                    "confidence": ref("risk_inference", "confidence"),
                    "review_note": ref(
                        "human_review",
                        "output",
                        "review_note",
                        optional=True,
                    ),
                    "external_write": False,
                }
            },
        ),
        node(
            "drift_monitor",
            "model_drift_monitor",
            "Measure feature drift without online learning",
            520,
            700,
            {
                "deployment_name": "exp004-predictive-risk-debug",
                "observations": ref("start", "drift_observations"),
                "warning_threshold": 1.0,
                "critical_threshold": 2.0,
            },
        ),
        node(
            "decision_aggregate",
            "variable_aggregator",
            "Join the mutually exclusive business outcome",
            3120,
            280,
            {
                "variables": [
                    ref("automatic_alarm", "response", optional=True),
                    ref("reviewed_alarm", "response", optional=True),
                    ref(
                        "no_alarm_decision",
                        "output",
                        optional=True,
                    ),
                    ref(
                        "review_rejected_decision",
                        "output",
                        optional=True,
                    ),
                    ref(
                        "invalid_decision",
                        "output",
                        optional=True,
                    ),
                    ref(
                        "time_rejected_decision",
                        "output",
                        optional=True,
                    ),
                ],
                "mode": "first_non_null",
            },
        ),
        node(
            "decision_artifact",
            "typed_json_artifact",
            "Write the event decision evidence",
            3380,
            280,
            {
                "value": {
                    "event_id": ref("start", "event_id"),
                    "device_id": ref("start", "device_id"),
                    "signal": ref(
                        "prepare_signal",
                        "output",
                        optional=True,
                    ),
                    "validation": ref(
                        "validate_signal",
                        "output",
                        optional=True,
                    ),
                    "prediction": ref(
                        "risk_inference",
                        "output",
                        optional=True,
                    ),
                    "decision": ref("decision_aggregate", "output"),
                    "drift": ref("drift_monitor", "output"),
                },
                "filename": "predictive-maintenance-decision.json",
                "lineage": [
                    {
                        "source_type": "connector_receipt",
                        "reference": "read_telemetry.receipt",
                    },
                    {
                        "source_type": "node_output",
                        "reference": "risk_inference.output",
                    },
                ],
            },
        ),
        node(
            "model_evidence_artifact",
            "typed_json_artifact",
            "Write approved model and evaluation evidence",
            3640,
            280,
            {
                "value": {
                    "deployment_name": "exp004-predictive-risk-debug",
                    "model_id": ref(
                        "risk_inference",
                        "model_id",
                        optional=True,
                    ),
                    "version": ref(
                        "risk_inference",
                        "version",
                        optional=True,
                    ),
                    "model_digest": ref(
                        "risk_inference",
                        "model_digest",
                        optional=True,
                    ),
                    "model_card": ref(
                        "risk_inference",
                        "model_card",
                        optional=True,
                    ),
                    "evaluation_metrics": ref(
                        "risk_inference",
                        "evaluation_metrics",
                        optional=True,
                    ),
                    "note": (
                        "Inference uses an explicitly promoted immutable version; "
                        "this workflow does not train or promote models."
                    ),
                },
                "filename": "approved-model-evidence.json",
                "lineage": [
                    {
                        "source_type": "external_resource",
                        "reference": (
                            "model-deployment:"
                            "exp004-predictive-risk-debug"
                        ),
                    }
                ],
            },
        ),
        node(
            "drift_artifact",
            "typed_json_artifact",
            "Write drift evidence",
            3900,
            280,
            {
                "value": {
                    "deployment_name": "exp004-predictive-risk-debug",
                    "report": ref("drift_monitor", "output"),
                    "online_learning_started": False,
                    "response_policy": (
                        "warning or critical drift requires a separate governed "
                        "evaluation and promotion process"
                    ),
                },
                "filename": "model-drift-report.json",
                "lineage": [
                    {
                        "source_type": "workflow_input",
                        "reference": "drift_observations",
                    },
                    {
                        "source_type": "node_output",
                        "reference": "drift_monitor.output",
                    },
                ],
            },
        ),
        node(
            "end",
            "end",
            "Return decision and audit artifacts",
            4160,
            280,
            {
                "outputs": {
                    "decision": ref("decision_aggregate", "output"),
                    "decision_artifact": ref(
                        "decision_artifact",
                        "artifact",
                    ),
                    "model_evidence_artifact": ref(
                        "model_evidence_artifact",
                        "artifact",
                    ),
                    "drift_artifact": ref(
                        "drift_artifact",
                        "artifact",
                    ),
                }
            },
        ),
    ]
    return nodes


def desired_edges() -> list[dict[str, Any]]:
    values = [
        edge("start-read", "start", "read_telemetry"),
        edge("read-prepare", "read_telemetry", "prepare_signal"),
        edge("prepare-validate", "prepare_signal", "validate_signal"),
        edge("validate-structure", "validate_signal", "structure_router"),
        edge(
            "structure-valid-time",
            "structure_router",
            "time_router",
            source_handle="valid",
        ),
        edge(
            "structure-invalid-stop",
            "structure_router",
            "invalid_decision",
            source_handle="invalid",
        ),
        edge(
            "time-eligible-signal",
            "time_router",
            "eligible_signal",
            source_handle="eligible",
        ),
        edge(
            "eligible-signal-inference",
            "eligible_signal",
            "risk_inference",
        ),
        edge(
            "time-rejected-stop",
            "time_router",
            "time_rejected_decision",
            source_handle="stale_or_out_of_order",
        ),
        edge("inference-risk", "risk_inference", "risk_router"),
        edge(
            "risk-auto-write",
            "risk_router",
            "automatic_alarm",
            source_handle="automatic_alarm",
        ),
        edge(
            "risk-low-record",
            "risk_router",
            "no_alarm_decision",
            source_handle="no_alarm",
        ),
        edge(
            "risk-review-human",
            "risk_router",
            "human_review",
            source_handle="human_review",
        ),
        edge("human-review-router", "human_review", "review_router"),
        edge(
            "review-approved-write",
            "review_router",
            "reviewed_alarm",
            source_handle="approved",
        ),
        edge(
            "review-rejected-record",
            "review_router",
            "review_rejected_decision",
            source_handle="rejected",
        ),
        edge("start-drift", "start", "drift_monitor"),
    ]
    for terminal in (
        "automatic_alarm",
        "reviewed_alarm",
        "no_alarm_decision",
        "review_rejected_decision",
        "invalid_decision",
        "time_rejected_decision",
        "drift_monitor",
    ):
        values.append(
            edge(
                f"{terminal}-decision-aggregate",
                terminal,
                "decision_aggregate",
            )
        )
    values.extend(
        [
            edge(
                "aggregate-decision-artifact",
                "decision_aggregate",
                "decision_artifact",
            ),
            edge(
                "decision-model-artifact",
                "decision_artifact",
                "model_evidence_artifact",
            ),
            edge(
                "model-drift-artifact",
                "model_evidence_artifact",
                "drift_artifact",
            ),
            edge("drift-artifact-end", "drift_artifact", "end"),
        ]
    )
    return values


class PublicApi:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{method} {path} returned HTTP {error.code}: {detail}"
            ) from error
        return json.loads(payload) if payload else None


def apply_workflow(
    api: PublicApi,
    application_id: str,
) -> dict[str, Any]:
    path = f"/api/v1/applications/{application_id}/draft"
    draft = api.request("GET", path)
    current_nodes = {
        item["id"]: item
        for item in draft["snapshot"]["workflow"]["nodes"]
    }
    current_edges = {
        item["id"]: item
        for item in draft["snapshot"]["workflow"]["edges"]
    }
    revision = int(draft["revision"])
    applied: list[str] = []
    for desired in desired_nodes():
        current = current_nodes.get(desired["id"])
        if current is None:
            operation = "add_node"
        else:
            comparable = {
                key: current[key]
                for key in (
                    "id",
                    "type",
                    "title",
                    "description",
                    "position",
                    "config",
                )
            }
            if comparable == desired:
                continue
            operation = "update_node"
        revision += 1
        api.request(
            "POST",
            path,
            {
                "idempotency_key": (
                    f"exp004-r1-{operation}-{desired['id']}-v4"
                ),
                "expected_revision": revision - 1,
                "op": operation,
                "data": (
                    {"node": desired}
                    if operation == "add_node"
                    else {
                        "node_id": desired["id"],
                        "changes": {
                            key: value
                            for key, value in desired.items()
                            if key != "id"
                        },
                        "merge_config": False,
                    }
                ),
            },
        )
        applied.append(f"{operation}:{desired['id']}")
    for desired in desired_edges():
        current = current_edges.get(desired["id"])
        if current is not None:
            comparable = {
                key: current.get(key)
                for key in (
                    "id",
                    "source",
                    "target",
                    "source_port",
                    "target_port",
                    "branch",
                )
                if current.get(key) is not None
            }
            if comparable == desired:
                continue
            raise RuntimeError(
                f"edge {desired['id']!r} already exists with different content"
            )
        revision += 1
        api.request(
            "POST",
            path,
            {
                "idempotency_key": (
                    f"exp004-r1-add-edge-{desired['id']}-v1"
                ),
                "expected_revision": revision - 1,
                "op": "add_edge",
                "data": {"edge": desired},
            },
        )
        applied.append(f"add_edge:{desired['id']}")
    return {
        "application_id": application_id,
        "revision": revision,
        "node_count": len(desired_nodes()),
        "edge_count": len(desired_edges()),
        "applied": applied,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8014")
    parser.add_argument("--token", required=True)
    parser.add_argument("--application-id", required=True)
    args = parser.parse_args()
    result = apply_workflow(
        PublicApi(args.base_url, args.token),
        args.application_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
