from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel



_PRIVATE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "chain_of_thought",
        "codex",
        "collaboration",
        "collaboration_report",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "developer_error",
        "developer_payload",
        "developer_response",
        "expected_answer",
        "hidden_oracle",
        "internal_error",
        "internal_prompt",
        "oracle",
        "oracle_path",
        "password",
        "private_key",
        "private_reason",
        "private_reasoning",
        "proxy_authorization",
        "raw_blocks",
        "refresh_token",
        "reasoning_tokens",
        "secret",
        "secrets",
        "set_cookie",
        "signature",
        "stack_trace",
        "system_prompt",
        "thinking",
        "thinking_blocks",
        "token",
        "traceback",
    }
)
_PRIVATE_EVENT_MARKERS = (
    "chain_of_thought",
    "codex",
    "collaboration",
    "developer",
    "private",
    "prompt",
    "signature",
    "thinking",
)
_PRIVATE_NODE_MARKERS = ("codex", "collaboration", "developer", "private_reason")
_EVENT_DATA_KEYS = frozenset(
    {
        "attempt",
        "behavior",
        "mode",
        "node_id",
        "request_id",
        "session_id",
        "status",
        "title",
        "tool",
        "type",
    }
)
_INPUT_KEYS = frozenset(
    {
        "default",
        "description",
        "example",
        "label",
        "name",
        "required",
        "title",
        "type",
    }
)
_INTERNAL_CONNECTOR_INPUTS = frozenset(
    {
        "actor_id",
        "actor_roles",
        "connector_authorization_id",
        "connector_idempotency_key",
        "connector_profile_id",
        "tenant_id",
        "write_mode",
    }
)
_AUTHORIZATION_TEXT = re.compile(
    r"(?i)\b(?:authorization|proxy[_-]?authorization)\b\s*[:=]\s*"
    r"[^\r\n,;]+"
)
_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"proxy[_-]?authorization|cookie|password|secret|credential|token)\b"
    r"\s*[:=]\s*(?!\*+|\[redacted\]|<redacted>)[^\s,;]+"
)
_PRIVATE_ERROR_MARKERS = (
    "chain_of_thought",
    "codex",
    "collaboration",
    "developer",
    "internal_prompt",
    "private_reason",
    "raw_blocks",
    "system_prompt",
    "thinking",
    "traceback",
)
_HIDDEN_RUNTIME_ERROR = "Runtime failed; private diagnostic details were hidden."


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _normalized_key(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _is_private_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _PRIVATE_KEYS or any(
        marker in normalized
        for marker in (
            "chain_of_thought",
            "private_reason",
            "raw_block",
            "reasoning_token",
            "thinking",
        )
    )


def project_public_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively retain business output while dropping private-runtime fields."""

    if _depth >= 20:
        return None
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {
            str(key): project_public_value(item, _depth=_depth + 1)
            for key, item in value.items()
            if not _is_private_key(str(key))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [project_public_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _CREDENTIAL_TEXT.sub(
            "[REDACTED]",
            _AUTHORIZATION_TEXT.sub("[REDACTED]", value),
        )
    return value


def _public_runtime_error(value: Any) -> str:
    raw = str(value or "")
    normalized = raw.casefold().replace("-", "_").replace(" ", "_")
    if any(marker in normalized for marker in _PRIVATE_ERROR_MARKERS):
        return _HIDDEN_RUNTIME_ERROR
    sanitized = project_public_value(raw)
    if not isinstance(sanitized, str) or sanitized != raw:
        return _HIDDEN_RUNTIME_ERROR
    return sanitized[:2_000]


def _public_input(value: Any) -> dict[str, Any] | None:
    item = _mapping(value)
    name = str(item.get("name") or "").strip()
    if not name or name in _INTERNAL_CONNECTOR_INPUTS:
        return None
    return {
        key: project_public_value(item[key])
        for key in _INPUT_KEYS
        if key in item
    }


def _public_trigger_config(config: Any) -> dict[str, Any]:
    source = _mapping(config)
    settings = _mapping(source.get("settings"))
    raw_inputs = settings.get("inputs")
    if not isinstance(raw_inputs, list):
        raw_inputs = source.get("inputs")
    if not isinstance(raw_inputs, list):
        raw_inputs = []
    inputs = [
        projected
        for item in raw_inputs
        if (projected := _public_input(item)) is not None
    ]
    return {"settings": {"inputs": inputs}}


def project_runtime_snapshot(snapshot: Any) -> dict[str, Any]:
    source = _mapping(snapshot)
    workflow = _mapping(source.get("workflow"))
    nodes: list[dict[str, Any]] = []
    visible_ids: set[str] = set()
    raw_nodes = workflow.get("nodes")
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    for value in raw_nodes:
        node = _mapping(value)
        node_type = str(node.get("type") or "")
        if not node_type or any(marker in node_type.casefold() for marker in _PRIVATE_NODE_MARKERS):
            continue
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        visible_ids.add(node_id)
        projected = {
            "id": node_id,
            "type": node_type,
            "block_version": int(node.get("block_version") or 1),
            "title": str(node.get("title") or node_type),
            "description": str(node.get("description") or ""),
            "config": (
                _public_trigger_config(node.get("config"))
                if node_type in {"start", "schedule_trigger"}
                else {}
            ),
            "position": project_public_value(node.get("position") or {"x": 0, "y": 0}),
        }
        nodes.append(projected)
    edges: list[dict[str, str]] = []
    raw_edges = workflow.get("edges")
    if not isinstance(raw_edges, list):
        raw_edges = []
    for value in raw_edges:
        edge = _mapping(value)
        source_id = str(edge.get("source") or "")
        target_id = str(edge.get("target") or "")
        if source_id not in visible_ids or target_id not in visible_ids:
            continue
        edges.append(
            {
                "id": str(edge.get("id") or f"{source_id}:{target_id}"),
                "source": source_id,
                "target": target_id,
                "source_port": str(edge.get("source_port") or "output"),
                "target_port": str(edge.get("target_port") or "input"),
            }
        )
    projected_snapshot: dict[str, Any] = {
        "name": str(source.get("name") or ""),
        "description": str(source.get("description") or ""),
        "mode": str(source.get("mode") or "workflow"),
        "delivery_mode": str(source.get("delivery_mode") or "guided"),
        "governed_hard_gate": bool(source.get("governed_hard_gate")),
        "requirement": str(source.get("requirement") or ""),
        "workflow": {
            "nodes": nodes,
            "edges": edges,
            "viewport": {"x": 0, "y": 0, "zoom": 0.8},
        },
        "agents": {},
        "tests": [],
    }
    return projected_snapshot


def project_runtime_application(application: Any) -> dict[str, Any]:
    source = _mapping(application)
    return {
        "id": str(source.get("id") or ""),
        "name": str(source.get("name") or ""),
        "description": str(source.get("description") or ""),
        "requirement": str(source.get("requirement") or ""),
        "active_version": source.get("active_version"),
    }


def project_runtime_definition(definition: Any) -> dict[str, Any]:
    source = _mapping(definition)
    return {
        "application_id": str(source.get("application_id") or ""),
        "source": str(source.get("source") or "draft"),
        "version": source.get("version"),
        "draft_revision": source.get("draft_revision"),
        "content_hash": str(source.get("content_hash") or ""),
        "snapshot": project_runtime_snapshot(source.get("snapshot")),
    }


def project_runtime_run(run: Any) -> dict[str, Any]:
    source = _mapping(run)
    state = _mapping(source.get("state"))
    result: dict[str, Any] = {
        "id": str(source.get("id") or state.get("run_id") or ""),
        "status": str(source.get("status") or "queued"),
        "outputs": project_public_value(source.get("outputs") or {}),
        "state": {
            "snapshot": project_runtime_snapshot(state.get("snapshot")),
            "waiting_node_id": state.get("waiting_node_id"),
            "completed": [
                str(value) for value in state.get("completed", []) if isinstance(value, str)
            ],
            "skipped": [
                str(value) for value in state.get("skipped", []) if isinstance(value, str)
            ],
        },
        "created_at": source.get("created_at"),
        "updated_at": source.get("updated_at"),
    }
    if source.get("error"):
        result["error"] = _public_runtime_error(source["error"])
    return result


def project_runtime_event(event: Any) -> dict[str, Any] | None:
    source = _mapping(event)
    event_type = str(source.get("type") or "")
    normalized = event_type.casefold()
    if not event_type or any(marker in normalized for marker in _PRIVATE_EVENT_MARKERS):
        return None
    raw_data = _mapping(source.get("data"))
    data = {
        key: project_public_value(raw_data[key])
        for key in _EVENT_DATA_KEYS
        if key in raw_data
    }
    return {
        "id": int(source.get("id") or 0),
        "type": event_type,
        "data": data,
        "created_at": source.get("created_at"),
    }


def project_runtime_events(events: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        projected
        for event in events
        if (projected := project_runtime_event(event)) is not None
    ]


# ── 界面方案：同一工作流按标注生成不同使用界面 ──
#
# 原则：隐藏发生在这里（服务端投影），被隐藏环节的输出根本不出后端；
# 终端节点（end/answer）是交付合同，永远可见。

_TERMINAL_TYPES = frozenset({"end", "answer"})
# 水管节点：默认视图里不值得给使用者看的结构性环节。
_PLUMBING_TYPES = frozenset(
    {
        "start",
        "end",
        "answer",
        "schedule_trigger",
        "template_transform",
        "variable_assigner",
        "tool_call_router",
        "stop_continue_controller",
        "retry_error_classifier",
    }
)


def _snapshot_nodes(snapshot: Any) -> list[dict[str, Any]]:
    workflow = _mapping(_mapping(snapshot).get("workflow"))
    raw = workflow.get("nodes")
    return [_mapping(node) for node in raw] if isinstance(raw, list) else []


def _ordered_node_ids(snapshot: Any) -> list[str]:
    """按边走一遍拓扑序（尽力而为），让过程环节按执行顺序排列。"""

    workflow = _mapping(_mapping(snapshot).get("workflow"))
    nodes = _snapshot_nodes(snapshot)
    ids = [str(node.get("id") or "") for node in nodes if node.get("id")]
    raw_edges = workflow.get("edges")
    edges = [_mapping(edge) for edge in raw_edges] if isinstance(raw_edges, list) else []
    incoming: dict[str, int] = {node_id: 0 for node_id in ids}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges:
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if source in incoming and target in incoming:
            adjacency[source].append(target)
            incoming[target] += 1
    queue = [node_id for node_id in ids if incoming[node_id] == 0]
    ordered: list[str] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for nxt in adjacency[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                queue.append(nxt)
    ordered.extend(node_id for node_id in ids if node_id not in ordered)
    return ordered


def default_hidden_nodes(snapshot: Any) -> list[str]:
    """零标注路径：水管环节自动隐藏，业务环节自动可见。"""

    return [
        str(node.get("id"))
        for node in _snapshot_nodes(snapshot)
        if str(node.get("type") or "") in _PLUMBING_TYPES and node.get("id")
    ]


def resolve_view_layout(snapshot: Any, layout: str) -> str:
    """auto → 有 answer 节点的工作流长成对话界面，其余是表单。"""

    if layout in ("form", "chat"):
        return layout
    has_answer = any(
        str(node.get("type") or "") == "answer" for node in _snapshot_nodes(snapshot)
    )
    return "chat" if has_answer else "form"


def project_view_definition(
    snapshot: Any, view: Mapping[str, Any] | None
) -> dict[str, Any]:
    """界面方案投影：可见过程环节清单 + 解析后的布局。"""

    hidden = set(
        str(item) for item in (view or {}).get("hidden_nodes", [])
    ) if view else set(default_hidden_nodes(snapshot))
    by_id = {str(node.get("id")): node for node in _snapshot_nodes(snapshot)}
    stage_nodes = [
        {
            "id": node_id,
            "title": str(by_id[node_id].get("title") or node_id),
            "type": str(by_id[node_id].get("type") or ""),
        }
        for node_id in _ordered_node_ids(snapshot)
        if node_id in by_id
        and node_id not in hidden
        and str(by_id[node_id].get("type") or "") not in _TERMINAL_TYPES
        and str(by_id[node_id].get("type") or "") != "start"
    ]
    return {
        "view_id": str((view or {}).get("view_id") or "default"),
        "name": str((view or {}).get("name") or "默认界面"),
        "layout": resolve_view_layout(snapshot, str((view or {}).get("layout") or "auto")),
        "stage_nodes": stage_nodes,
    }


def project_view_run(run: Any, view: Mapping[str, Any] | None) -> dict[str, Any]:
    """按界面方案过滤运行结果：终端输出 + 可见环节的过程输出，其余不出后端。

    快照与逐节点账本都从 run.state 里取（兼容 dict 与 Pydantic 状态对象），
    调用方不需要也不允许自己拆。
    """

    projected = project_runtime_run(run)
    run_state = _mapping(_mapping(run).get("state"))
    snapshot = run_state.get("snapshot")
    view_definition = project_view_definition(snapshot, view)
    visible_stage_ids = {node["id"] for node in view_definition["stage_nodes"]}
    terminal_ids = {
        str(node.get("id"))
        for node in _snapshot_nodes(snapshot)
        if str(node.get("type") or "") in _TERMINAL_TYPES
    }
    # 逐节点输出的真实来源是 state.outputs（运行时账本）；run.outputs 顶层
    # 在多数存储路径下已被扁平成终端字段，只能当候补。
    state_outputs = run_state.get("outputs")
    raw_per_node = (
        state_outputs if isinstance(state_outputs, Mapping) else _mapping(run).get("outputs")
    )
    candidate = project_public_value(raw_per_node) if isinstance(raw_per_node, Mapping) else {}
    node_ids = {str(node.get("id")) for node in _snapshot_nodes(snapshot)}
    per_node = (
        candidate
        if isinstance(candidate, dict)
        and candidate
        and all(
            key in node_ids and isinstance(value, (dict, type(None)))
            for key, value in candidate.items()
        )
        else {}
    )
    if per_node:
        by_id = {str(node.get("id")): node for node in _snapshot_nodes(snapshot)}
        result_outputs: dict[str, Any] = {}
        stages: list[dict[str, Any]] = []
        for node_id in _ordered_node_ids(snapshot):
            value = per_node.get(node_id)
            if value is None:
                continue
            if node_id in terminal_ids:
                if isinstance(value, dict):
                    result_outputs.update(value)
            elif node_id in visible_stage_ids:
                stages.append(
                    {
                        "node_id": node_id,
                        "title": str(by_id.get(node_id, {}).get("title") or node_id),
                        "type": str(by_id.get(node_id, {}).get("type") or ""),
                        "outputs": value,
                    }
                )
        projected["outputs"] = result_outputs
        projected["stages"] = stages
    else:
        projected["stages"] = []
    projected["view"] = view_definition
    return projected
