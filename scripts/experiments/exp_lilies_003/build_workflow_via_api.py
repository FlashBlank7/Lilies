#!/usr/bin/env python3
"""Build EXP-LILIES-003 from an empty application through public APIs."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


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
    error_strategy: str | None = None,
    retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": node_id,
        "type": block_type,
        "title": title,
        "position": {"x": x, "y": y},
        "config": config,
    }
    if error_strategy is not None:
        result["error_strategy"] = error_strategy
    if retry is not None:
        result["retry"] = retry
    return result


def edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    branch: str | None = None,
    source_port: str = "output",
    branch_port: str = "branch",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": edge_id,
        "source": source,
        "target": target,
        "source_port": source_port,
        "target_port": "input",
    }
    if branch is not None:
        result["source_port"] = branch_port
        result["branch"] = branch
    return result


def desired_nodes(application_id: str) -> list[dict[str, Any]]:
    trigger_inputs = [
        {
            "name": "event_kind",
            "label": "State event, timer due, or action request",
            "type": "string",
            "required": True,
        },
        {
            "name": "event_id",
            "label": "Stable source event identity",
            "type": "string",
            "required": True,
        },
        {
            "name": "entity_id",
            "label": "Authorized business subject",
            "type": "string",
            "required": True,
        },
        {
            "name": "old_state",
            "label": "Previous subject state",
            "type": "string",
            "required": False,
            "default": "",
        },
        {
            "name": "new_state",
            "label": "New subject state",
            "type": "string",
            "required": True,
        },
        {
            "name": "occurred_at",
            "label": "Source event time",
            "type": "string",
            "required": True,
        },
        {
            "name": "hold_for_seconds",
            "label": "Required open duration",
            "type": "number",
            "required": False,
            "default": 300,
        },
        {
            "name": "allowed_entities",
            "label": "Authorized entity identities",
            "type": "array",
            "required": True,
        },
        {
            "name": "action_name",
            "label": "Optional governed action",
            "type": "string",
            "required": False,
            "default": "",
        },
        {
            "name": "__event_automation",
            "label": "Platform event and timer receipt",
            "type": "object",
            "required": False,
            "default": {},
        },
    ]
    return [
        node(
            "event_source",
            "event_subscription_trigger",
            "Authorized facility event",
            0,
            300,
            {
                "subscription_name": "home-assistant-facility-state",
                "inputs": trigger_inputs,
            },
        ),
        node(
            "route_event_kind",
            "if_else",
            "Distinguish state, deadline, and action events",
            250,
            300,
            {
                "cases": [
                    {
                        "id": "state_changed",
                        "conditions": [
                            {
                                "value": ref("event_source", "event_kind"),
                                "operator": "equals",
                                "expected": "state_changed",
                            }
                        ],
                    },
                    {
                        "id": "timer_due",
                        "conditions": [
                            {
                                "value": ref("event_source", "event_kind"),
                                "operator": "equals",
                                "expected": "timer_due",
                            }
                        ],
                    },
                    {
                        "id": "action_request",
                        "conditions": [
                            {
                                "value": ref("event_source", "event_kind"),
                                "operator": "equals",
                                "expected": "action_request",
                            }
                        ],
                    },
                ],
                "default_branch": "unsupported",
            },
        ),
        node(
            "authorize_entity",
            "if_else",
            "Reject events outside the authorized entity set",
            500,
            80,
            {
                "cases": [
                    {
                        "id": "authorized",
                        "conditions": [
                            {
                                "value": ref("event_source", "allowed_entities"),
                                "operator": "contains",
                                "expected": ref("event_source", "entity_id"),
                            }
                        ],
                    }
                ],
                "default_branch": "unauthorized",
            },
        ),
        node(
            "make_timer_key",
            "template_transform",
            "Create stable per-entity timer identity",
            760,
            20,
            {
                "template": "facility-door:{{ entity_id }}",
                "variables": {
                    "entity_id": ref("event_source", "entity_id"),
                },
            },
        ),
        node(
            "apply_timer_state",
            "durable_event_timer",
            "Schedule or cancel durable wait",
            1020,
            20,
            {
                "operation": ref("event_source", "new_state"),
                "timer_key": ref("make_timer_key", "text"),
                "subject_id": ref("event_source", "entity_id"),
                "event_id": ref("event_source", "event_id"),
                "occurred_at": ref("event_source", "occurred_at"),
                "hold_for_seconds": ref("event_source", "hold_for_seconds"),
                "due_inputs": {
                    "event_kind": "timer_due",
                    "event_id": ref("event_source", "event_id"),
                    "entity_id": ref("event_source", "entity_id"),
                    "old_state": ref("event_source", "old_state"),
                    "new_state": "timer_due",
                    "occurred_at": ref("event_source", "occurred_at"),
                    "hold_for_seconds": ref(
                        "event_source",
                        "hold_for_seconds",
                    ),
                    "allowed_entities": ref(
                        "event_source",
                        "allowed_entities",
                    ),
                    "action_name": "",
                },
            },
        ),
        node(
            "timer_state_artifact",
            "typed_json_artifact",
            "Persist state-event timer receipt",
            1280,
            20,
            {
                "value": {
                    "event_kind": "state_changed",
                    "entity_id": ref("event_source", "entity_id"),
                    "timer": ref("apply_timer_state", "output"),
                },
                "filename": "timer-state-receipt.json",
                "lineage": [
                    {
                        "source_type": "workflow_input",
                        "reference": "Home Assistant WebSocket event",
                    },
                    {
                        "source_type": "node_output",
                        "reference": "apply_timer_state",
                    },
                ],
            },
        ),
        node(
            "state_end",
            "end",
            "State event result",
            1540,
            20,
            {
                "outputs": {
                    "decision": {
                        "kind": "state_changed",
                        "accepted": True,
                        "entity_id": ref("event_source", "entity_id"),
                    },
                    "timer_receipt": ref("apply_timer_state", "output"),
                    "timer_artifact": ref("timer_state_artifact", "output"),
                }
            },
        ),
        node(
            "unauthorized_artifact",
            "typed_json_artifact",
            "Persist unauthorized event refusal",
            760,
            170,
            {
                "value": {
                    "kind": "state_changed",
                    "accepted": False,
                    "reason": "entity_not_authorized",
                    "entity_id": ref("event_source", "entity_id"),
                },
                "filename": "unauthorized-event.json",
            },
        ),
        node(
            "unauthorized_end",
            "end",
            "Unauthorized event result",
            1020,
            170,
            {
                "outputs": {
                    "decision": ref("unauthorized_artifact", "output"),
                    "notification_count": 0,
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "make_state_url",
            "template_transform",
            "Address the current Home Assistant entity",
            500,
            420,
            {
                "template": (
                    "http://127.0.0.1:18030/api/states/{{ entity_id }}"
                ),
                "variables": {
                    "entity_id": ref("event_source", "entity_id"),
                },
            },
        ),
        node(
            "read_current_state",
            "http_request",
            "Recheck current Home Assistant state at deadline",
            760,
            420,
            {
                "method": "GET",
                "url": ref("make_state_url", "text"),
                "headers": {
                    "Authorization": {
                        "$secret": (
                            f"secret://{application_id}/"
                            "home_assistant_bearer"
                        )
                    }
                },
                "timeout_seconds": 20,
            },
            retry={"enabled": True, "max_attempts": 3, "delay_seconds": 0.1},
        ),
        node(
            "route_deadline_state",
            "if_else",
            "Notify only while the entity is still open",
            1020,
            420,
            {
                "cases": [
                    {
                        "id": "still_open",
                        "conditions": [
                            {
                                "value": ref(
                                    "read_current_state",
                                    "output",
                                    "state",
                                ),
                                "operator": "equals",
                                "expected": "on",
                            }
                        ],
                    }
                ],
                "default_branch": "already_closed",
            },
        ),
        node(
            "write_notification",
            "http_request",
            "Deliver one idempotent facility notification",
            1280,
            360,
            {
                "method": "POST",
                "url": "http://127.0.0.1:18031/notifications",
                "headers": {
                    "Authorization": {
                        "$secret": (
                            f"secret://{application_id}/"
                            "notification_sink_bearer"
                        )
                    }
                },
                "body": {
                    "idempotency_key": ref(
                        "event_source",
                        "__event_automation",
                        "source_event_id",
                    ),
                    "subject_id": ref("event_source", "entity_id"),
                    "title": "Facility door remained open",
                    "message": "The monitored door is still open at its deadline.",
                    "logical_deadline": ref(
                        "event_source",
                        "__event_automation",
                        "due_at",
                    ),
                    "workflow_run_id": ref(
                        "event_source",
                        "__event_automation",
                        "timer_key",
                    ),
                },
                "timeout_seconds": 20,
            },
            error_strategy="error_branch",
            retry={"enabled": True, "max_attempts": 3, "delay_seconds": 0.1},
        ),
        node(
            "make_due_event_id",
            "template_transform",
            "Create deadline completion identity",
            1280,
            500,
            {
                "template": "{{ source_event_id }}:due",
                "variables": {
                    "source_event_id": ref(
                        "event_source",
                        "__event_automation",
                        "source_event_id",
                    )
                },
            },
        ),
        node(
            "complete_timer",
            "durable_event_timer",
            "Complete the dispatched deadline",
            1540,
            430,
            {
                "operation": "complete",
                "timer_key": ref(
                    "event_source",
                    "__event_automation",
                    "timer_key",
                ),
                "subject_id": ref("event_source", "entity_id"),
                "event_id": ref("make_due_event_id", "text"),
                "occurred_at": ref(
                    "event_source",
                    "__event_automation",
                    "due_at",
                ),
                "hold_for_seconds": ref("event_source", "hold_for_seconds"),
                "due_inputs": {},
            },
        ),
        node(
            "deadline_artifact",
            "typed_json_artifact",
            "Persist notification and recovery decision",
            1800,
            430,
            {
                "value": {
                    "entity_id": ref("event_source", "entity_id"),
                    "current_state": ref(
                        "read_current_state",
                        "output",
                        "state",
                    ),
                    "notification": ref(
                        "write_notification",
                        "output",
                        optional=True,
                    ),
                    "timer": ref("complete_timer", "output"),
                    "recovery": ref(
                        "event_source",
                        "__event_automation",
                    ),
                    "device_service_action_count": 0,
                },
                "filename": "notification-decision.json",
                "lineage": [
                    {
                        "source_type": "external_resource",
                        "reference": "Home Assistant current entity state",
                    },
                    {
                        "source_type": "connector_receipt",
                        "reference": "scoped notification sink",
                    },
                ],
            },
        ),
        node(
            "deadline_end",
            "end",
            "Open-at-deadline result",
            2060,
            430,
            {
                "outputs": {
                    "decision": ref("deadline_artifact", "output"),
                    "timer_receipt": ref("complete_timer", "output"),
                    "notification_receipt": ref(
                        "write_notification",
                        "output",
                        optional=True,
                    ),
                    "notification_count": 1,
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "make_closed_due_event_id",
            "template_transform",
            "Create closed deadline completion identity",
            1280,
            520,
            {
                "template": "{{ source_event_id }}:closed",
                "variables": {
                    "source_event_id": ref(
                        "event_source",
                        "__event_automation",
                        "source_event_id",
                    )
                },
            },
        ),
        node(
            "complete_closed_timer",
            "durable_event_timer",
            "Complete a deadline whose entity is already closed",
            1540,
            520,
            {
                "operation": "complete",
                "timer_key": ref(
                    "event_source",
                    "__event_automation",
                    "timer_key",
                ),
                "subject_id": ref("event_source", "entity_id"),
                "event_id": ref("make_closed_due_event_id", "text"),
                "occurred_at": ref(
                    "event_source",
                    "__event_automation",
                    "due_at",
                ),
                "hold_for_seconds": ref("event_source", "hold_for_seconds"),
                "due_inputs": {},
            },
        ),
        node(
            "closed_deadline_artifact",
            "typed_json_artifact",
            "Persist closed-without-notification decision",
            1800,
            520,
            {
                "value": {
                    "entity_id": ref("event_source", "entity_id"),
                    "current_state": ref(
                        "read_current_state",
                        "output",
                        "state",
                    ),
                    "notified": False,
                    "reason": "closed_before_deadline",
                    "timer": ref("complete_closed_timer", "output"),
                    "recovery": ref(
                        "event_source",
                        "__event_automation",
                    ),
                    "device_service_action_count": 0,
                },
                "filename": "closed-deadline-decision.json",
            },
        ),
        node(
            "closed_deadline_end",
            "end",
            "Closed-at-deadline result",
            2060,
            520,
            {
                "outputs": {
                    "decision": ref("closed_deadline_artifact", "output"),
                    "timer_receipt": ref("complete_closed_timer", "output"),
                    "notification_count": 0,
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "make_error_due_event_id",
            "template_transform",
            "Create failed-notification completion identity",
            1280,
            610,
            {
                "template": "{{ source_event_id }}:notification-error",
                "variables": {
                    "source_event_id": ref(
                        "event_source",
                        "__event_automation",
                        "source_event_id",
                    )
                },
            },
        ),
        node(
            "complete_error_timer",
            "durable_event_timer",
            "Complete a safely stopped notification deadline",
            1540,
            610,
            {
                "operation": "complete",
                "timer_key": ref(
                    "event_source",
                    "__event_automation",
                    "timer_key",
                ),
                "subject_id": ref("event_source", "entity_id"),
                "event_id": ref("make_error_due_event_id", "text"),
                "occurred_at": ref(
                    "event_source",
                    "__event_automation",
                    "due_at",
                ),
                "hold_for_seconds": ref("event_source", "hold_for_seconds"),
                "due_inputs": {},
            },
        ),
        node(
            "notification_error_artifact",
            "typed_json_artifact",
            "Persist notification permission or delivery failure",
            1800,
            610,
            {
                "value": {
                    "entity_id": ref("event_source", "entity_id"),
                    "notified": False,
                    "error": ref("write_notification", "error"),
                    "timer": ref("complete_error_timer", "output"),
                    "device_service_action_count": 0,
                },
                "filename": "notification-error.json",
            },
        ),
        node(
            "notification_error_end",
            "end",
            "Notification failure result",
            2060,
            610,
            {
                "outputs": {
                    "decision": ref(
                        "notification_error_artifact",
                        "output",
                    ),
                    "timer_receipt": ref("complete_error_timer", "output"),
                    "notification_count": 0,
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "review_action",
            "human_input",
            "Approve or reject high-risk facility action",
            500,
            760,
            {
                "title": "High-risk facility action",
                "description": (
                    "Review the requested action before any customer-system call."
                ),
                "fields": [
                    {
                        "name": "approved",
                        "label": "Approve this action",
                        "type": "boolean",
                        "required": True,
                    },
                    {
                        "name": "comment",
                        "label": "Review note",
                        "type": "string",
                        "required": False,
                    },
                ],
            },
        ),
        node(
            "route_action_review",
            "if_else",
            "Apply the human action decision",
            760,
            760,
            {
                "cases": [
                    {
                        "id": "approved",
                        "conditions": [
                            {
                                "value": ref("review_action", "approved"),
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
            "write_action",
            "http_request",
            "Execute approved scoped action",
            1020,
            700,
            {
                "method": "POST",
                "url": "http://127.0.0.1:18031/actions",
                "headers": {
                    "Authorization": {
                        "$secret": (
                            f"secret://{application_id}/"
                            "notification_sink_bearer"
                        )
                    },
                    "X-Human-Approved": "true",
                },
                "body": {
                    "action": ref("event_source", "action_name"),
                    "entity_id": ref("event_source", "entity_id"),
                    "review_comment": ref(
                        "review_action",
                        "comment",
                        optional=True,
                    ),
                },
                "timeout_seconds": 20,
            },
            error_strategy="error_branch",
        ),
        node(
            "approved_action_artifact",
            "typed_json_artifact",
            "Persist approved action receipt",
            1280,
            700,
            {
                "value": {
                    "requested": True,
                    "approved": True,
                    "attempted": True,
                    "accepted": ref(
                        "write_action",
                        "output",
                        "accepted",
                    ),
                    "receipt": ref("write_action", "output"),
                },
                "filename": "approved-action-receipt.json",
            },
        ),
        node(
            "approved_action_end",
            "end",
            "Approved action result",
            1540,
            700,
            {
                "outputs": {
                    "action_receipt": {
                        "requested": True,
                        "approved": True,
                        "attempted": True,
                        "accepted": ref(
                            "write_action",
                            "output",
                            "accepted",
                        ),
                    },
                    "action_artifact": ref(
                        "approved_action_artifact",
                        "output",
                    ),
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "action_error_artifact",
            "typed_json_artifact",
            "Persist denied or failed approved action",
            1280,
            790,
            {
                "value": {
                    "requested": True,
                    "approved": True,
                    "attempted": True,
                    "accepted": False,
                    "error": ref("write_action", "error"),
                },
                "filename": "action-error-receipt.json",
            },
        ),
        node(
            "action_error_end",
            "end",
            "Approved action failure result",
            1540,
            790,
            {
                "outputs": {
                    "action_receipt": {
                        "requested": True,
                        "approved": True,
                        "attempted": True,
                        "accepted": False,
                        "error": ref("write_action", "error"),
                    },
                    "action_artifact": ref(
                        "action_error_artifact",
                        "output",
                    ),
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "rejected_action_artifact",
            "typed_json_artifact",
            "Persist rejected action decision",
            1020,
            840,
            {
                "value": {
                    "requested": True,
                    "approved": False,
                    "attempted": False,
                    "accepted": False,
                    "comment": ref(
                        "review_action",
                        "comment",
                        optional=True,
                    ),
                },
                "filename": "rejected-action-receipt.json",
            },
        ),
        node(
            "rejected_action_end",
            "end",
            "Rejected action result",
            1280,
            840,
            {
                "outputs": {
                    "action_receipt": {
                        "requested": True,
                        "approved": False,
                        "attempted": False,
                        "accepted": False,
                    },
                    "action_artifact": ref(
                        "rejected_action_artifact",
                        "output",
                    ),
                    "device_service_action_count": 0,
                }
            },
        ),
        node(
            "unsupported_end",
            "end",
            "Unsupported event result",
            500,
            950,
            {
                "outputs": {
                    "decision": {
                        "accepted": False,
                        "reason": "unsupported_event_kind",
                    },
                    "device_service_action_count": 0,
                }
            },
        ),
    ]


def desired_edges() -> list[dict[str, Any]]:
    return [
        edge("e01", "event_source", "route_event_kind"),
        edge(
            "e02",
            "route_event_kind",
            "authorize_entity",
            branch="state_changed",
        ),
        edge(
            "e03",
            "route_event_kind",
            "make_state_url",
            branch="timer_due",
        ),
        edge(
            "e04",
            "route_event_kind",
            "review_action",
            branch="action_request",
        ),
        edge(
            "e05",
            "route_event_kind",
            "unsupported_end",
            branch="unsupported",
        ),
        edge(
            "e06",
            "authorize_entity",
            "make_timer_key",
            branch="authorized",
        ),
        edge(
            "e07",
            "authorize_entity",
            "unauthorized_artifact",
            branch="unauthorized",
        ),
        edge(
            "e08",
            "make_timer_key",
            "apply_timer_state",
            source_port="text",
        ),
        edge("e09", "apply_timer_state", "timer_state_artifact"),
        edge("e10", "timer_state_artifact", "state_end"),
        edge("e11", "unauthorized_artifact", "unauthorized_end"),
        edge(
            "e12",
            "make_state_url",
            "read_current_state",
            source_port="text",
        ),
        edge("e13", "read_current_state", "route_deadline_state"),
        edge(
            "e14",
            "route_deadline_state",
            "write_notification",
            branch="still_open",
        ),
        edge(
            "e15",
            "route_deadline_state",
            "make_closed_due_event_id",
            branch="already_closed",
        ),
        edge("e16", "write_notification", "make_due_event_id"),
        edge(
            "e17",
            "write_notification",
            "make_error_due_event_id",
            branch="error",
            branch_port="output",
        ),
        edge(
            "e18",
            "make_due_event_id",
            "complete_timer",
            source_port="text",
        ),
        edge("e19", "complete_timer", "deadline_artifact"),
        edge("e20", "deadline_artifact", "deadline_end"),
        edge(
            "e20a",
            "make_closed_due_event_id",
            "complete_closed_timer",
            source_port="text",
        ),
        edge(
            "e20b",
            "complete_closed_timer",
            "closed_deadline_artifact",
        ),
        edge(
            "e20c",
            "closed_deadline_artifact",
            "closed_deadline_end",
        ),
        edge(
            "e20d",
            "make_error_due_event_id",
            "complete_error_timer",
            source_port="text",
        ),
        edge(
            "e20e",
            "complete_error_timer",
            "notification_error_artifact",
        ),
        edge(
            "e21",
            "notification_error_artifact",
            "notification_error_end",
        ),
        edge("e22", "review_action", "route_action_review"),
        edge(
            "e23",
            "route_action_review",
            "write_action",
            branch="approved",
        ),
        edge(
            "e24",
            "route_action_review",
            "rejected_action_artifact",
            branch="rejected",
        ),
        edge("e25", "write_action", "approved_action_artifact"),
        edge(
            "e26",
            "write_action",
            "action_error_artifact",
            branch="error",
            branch_port="output",
        ),
        edge(
            "e27",
            "approved_action_artifact",
            "approved_action_end",
        ),
        edge(
            "e28",
            "rejected_action_artifact",
            "rejected_action_end",
        ),
        edge("e29", "action_error_artifact", "action_error_end"),
    ]


def platform(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8016")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--ha-credential-file", type=Path, required=True)
    parser.add_argument("--workspace-path", type=Path, required=True)
    args = parser.parse_args()
    credential = json.loads(
        args.ha_credential_file.read_text(encoding="utf-8")
    )

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
        "event_subscription_trigger",
        "durable_event_timer",
        "if_else",
        "template_transform",
        "http_request",
        "human_input",
        "typed_json_artifact",
    }
    missing = sorted(required - set(blocks))
    if missing:
        raise RuntimeError(f"public block catalog is missing: {missing}")

    created = platform(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/applications",
        {
            "name": "Durable Home Assistant facility monitoring",
            "description": (
                "Receive authorized Home Assistant events, preserve per-door "
                "deadlines across restart, deduplicate notifications, and gate actions."
            ),
            "requirement": (
                "Build EXP-LILIES-003 revision 1 from the frozen public package."
            ),
        },
    )
    application_id = str(created["id"])
    for name, value, description in (
        (
            "home_assistant_bearer",
            f"Bearer {credential['workflow_token']}",
            "Controlled-local Home Assistant REST authorization",
        ),
        (
            "home_assistant_websocket_token",
            credential["workflow_token"],
            "Controlled-local Home Assistant WebSocket authorization",
        ),
        (
            "notification_sink_bearer",
            "Bearer exp003-local-write-token",
            "Controlled-local scoped notification sink authorization",
        ),
    ):
        platform(
            "POST",
            args.platform_base,
            args.platform_token,
            "/api/v1/platform/secrets",
            {
                "owner_id": application_id,
                "name": name,
                "value": value,
                "description": description,
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

    for item in desired_nodes(application_id):
        mutate("add_node", {"node": item})
    for item in desired_edges():
        mutate("add_edge", {"edge": item})

    future_time = "2099-07-30T00:00:00+00:00"
    base_inputs = {
        "event_id": "public-build-event-1",
        "entity_id": "binary_sensor.public_build_door",
        "old_state": "off",
        "new_state": "on",
        "occurred_at": future_time,
        "hold_for_seconds": 300,
        "allowed_entities": ["binary_sensor.public_build_door"],
        "action_name": "",
    }
    mutate(
        "add_test",
        {
            "test": {
                "name": "Authorized open schedules one durable timer",
                "requirement": (
                    "An authorized open event creates a restart-safe timer "
                    "without sending a notification or device action."
                ),
                "inputs": {
                    **base_inputs,
                    "event_kind": "state_changed",
                },
                "assertions": [
                    {
                        "path": ["timer_receipt", "status"],
                        "operator": "exists",
                    },
                    {
                        "path": ["timer_receipt", "durable"],
                        "operator": "equals",
                        "expected": True,
                    },
                    {
                        "path": ["timer_artifact"],
                        "operator": "exists",
                    },
                ],
                "required_node_types": sorted(required),
            }
        },
    )
    mutate(
        "add_test",
        {
            "test": {
                "name": "Rejected high-risk action performs no write",
                "requirement": (
                    "A reviewer rejection returns an action receipt and never "
                    "calls the scoped action endpoint."
                ),
                "inputs": {
                    **base_inputs,
                    "event_kind": "action_request",
                    "action_name": "emergency_ventilation_shutdown",
                },
                "simulated_human_inputs": {
                    "review_action": {
                        "approved": False,
                        "comment": "insufficient evidence",
                    }
                },
                "assertions": [
                    {
                        "path": ["action_receipt", "approved"],
                        "operator": "equals",
                        "expected": False,
                    },
                    {
                        "path": ["action_receipt", "attempted"],
                        "operator": "equals",
                        "expected": False,
                    },
                    {
                        "path": ["device_service_action_count"],
                        "operator": "equals",
                        "expected": 0,
                    },
                ],
                "required_node_types": sorted(required),
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
    result = {
        "application_id": application_id,
        "draft_revision": revision,
        "content_hash": validation["content_hash"],
        "node_count": len(desired_nodes(application_id)),
        "edge_count": len(desired_edges()),
        "platform_tests_passed": len(tests.get("tests", [])),
        "lilies_model_calls": 0,
        "lilies_tokens": 0,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
