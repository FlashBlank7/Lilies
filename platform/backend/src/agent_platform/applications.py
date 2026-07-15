from __future__ import annotations

from typing import Any

from .blocks import BlockRegistry
from .models import AgentSpec
from .workflow_models import (
    ApplicationMode,
    ApplicationSnapshot,
    DeliveryMode,
    DraftOperation,
    EdgeSpec,
    NodeSpec,
    WorkflowTestCase,
)
from .workflow_storage import RevisionConflict, WorkflowStorage
from .tools import ToolRegistry


class ApplicationService:
    def __init__(self, store: WorkflowStorage, blocks: BlockRegistry, tools: ToolRegistry) -> None:
        self.store = store
        self.blocks = blocks
        self.tools = tools

    async def apply_operation(self, application_id: str, operation: DraftOperation) -> dict[str, Any]:
        draft = await self.store.get_draft(application_id)
        snapshot: ApplicationSnapshot = draft["snapshot"].model_copy(deep=True)
        data = operation.data
        self._apply_to_snapshot(snapshot, operation.op, data)

        snapshot = ApplicationSnapshot.model_validate(snapshot.model_dump(mode="json"))
        result = await self.store.save_draft(
            application_id,
            snapshot,
            expected_revision=operation.expected_revision,
            idempotency_key=operation.idempotency_key,
            change_context=self._change_context(operation.op, data),
        )
        result["operation"] = operation.op
        return result

    async def apply_operations_atomically(
        self,
        application_id: str,
        *,
        expected_revision: int,
        expected_content_hash: str,
        operations: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not operations:
            raise ValueError("acceptance repair has no operations")
        draft = await self.store.get_draft(application_id)
        if int(draft["revision"]) != expected_revision:
            raise RevisionConflict(
                f"repair revision conflict: expected {expected_revision}, current {draft['revision']}"
            )
        if draft["content_hash"] != expected_content_hash:
            raise RevisionConflict("repair content hash no longer matches the current draft")
        snapshot: ApplicationSnapshot = draft["snapshot"].model_copy(deep=True)
        operation_names: list[str] = []
        for raw in operations:
            operation_revision = int(raw.get("expected_revision", expected_revision))
            if operation_revision != expected_revision:
                raise RevisionConflict(
                    f"repair operation revision conflict: expected {expected_revision}, got {operation_revision}"
                )
            operation_name = str(raw.get("op", ""))
            data = raw.get("data")
            if not isinstance(data, dict):
                raise ValueError("repair operation data must be an object")
            self._apply_to_snapshot(snapshot, operation_name, data)
            operation_names.append(operation_name)
        snapshot = ApplicationSnapshot.model_validate(snapshot.model_dump(mode="json"))
        result = await self.store.save_draft(
            application_id,
            snapshot,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            change_context={
                "operation": "acceptance_repair",
                "operation_count": len(operation_names),
                "operation_types": operation_names[:20],
            },
        )
        result.update({
            "operations_applied": len(operation_names),
            "previous_content_hash": expected_content_hash,
        })
        return result

    def _apply_to_snapshot(
        self,
        snapshot: ApplicationSnapshot,
        operation: str,
        data: dict[str, Any],
    ) -> None:

        if operation == "add_node":
            node = NodeSpec.model_validate(data["node"])
            if any(item.id == node.id for item in snapshot.workflow.nodes):
                raise ValueError(f"node already exists: {node.id}")
            self.blocks.validate_node(node)
            snapshot.workflow.nodes.append(node)
        elif operation == "update_node":
            node = self._node(snapshot, str(data["node_id"]))
            changes = dict(data.get("changes") or {})
            if "config" in changes and data.get("merge_config", True):
                changes["config"] = self._deep_merge(node.config, changes["config"])
            updated = node.model_copy(update=changes)
            self.blocks.validate_node(updated)
            index = snapshot.workflow.nodes.index(node)
            snapshot.workflow.nodes[index] = updated
        elif operation == "remove_node":
            node_id = str(data["node_id"])
            self._node(snapshot, node_id)
            snapshot.workflow.nodes = [node for node in snapshot.workflow.nodes if node.id != node_id]
            snapshot.workflow.edges = [
                edge for edge in snapshot.workflow.edges if node_id not in {edge.source, edge.target}
            ]
        elif operation == "add_edge":
            edge = EdgeSpec.model_validate(data["edge"])
            if any(item.id == edge.id for item in snapshot.workflow.edges):
                raise ValueError(f"edge already exists: {edge.id}")
            self._node(snapshot, edge.source)
            self._node(snapshot, edge.target)
            snapshot.workflow.edges.append(edge)
        elif operation == "remove_edge":
            edge_id = str(data["edge_id"])
            if not any(edge.id == edge_id for edge in snapshot.workflow.edges):
                raise KeyError(f"edge not found: {edge_id}")
            snapshot.workflow.edges = [edge for edge in snapshot.workflow.edges if edge.id != edge_id]
        elif operation == "set_metadata":
            for field in (
                "name",
                "description",
                "mode",
                "delivery_mode",
                "governed_hard_gate",
                "requirement",
            ):
                if field in data:
                    value = data[field]
                    if field == "mode":
                        value = ApplicationMode(value)
                    elif field == "delivery_mode":
                        value = DeliveryMode(value)
                    elif field == "governed_hard_gate" and not isinstance(value, bool):
                        raise ValueError("governed_hard_gate must be a boolean")
                    setattr(snapshot, field, value)
        elif operation == "upsert_agent":
            agent = AgentSpec.model_validate(data["agent"])
            snapshot.agents[agent.id] = agent
        elif operation == "add_test":
            test = WorkflowTestCase.model_validate(data["test"])
            snapshot.tests = [item for item in snapshot.tests if item.id != test.id]
            snapshot.tests.append(test)
        elif operation == "remove_test":
            test_id = str(data["test_id"])
            snapshot.tests = [item for item in snapshot.tests if item.id != test_id]
        else:
            raise ValueError(f"unsupported draft operation: {operation}")

    @staticmethod
    def _change_context(operation: str, data: dict[str, Any]) -> dict[str, Any]:
        context: dict[str, Any] = {
            "operation": operation,
            "fields": sorted(str(key) for key in data),
        }
        for key in ("node_id", "edge_id", "test_id"):
            if data.get(key):
                context[key] = str(data[key])[:200]
        nested_key = {
            "add_node": "node",
            "add_edge": "edge",
            "add_test": "test",
            "upsert_agent": "agent",
        }.get(operation)
        nested = data.get(nested_key) if nested_key else None
        if isinstance(nested, dict) and nested.get("id"):
            context["target_id"] = str(nested["id"])[:200]
        return context

    async def validate_draft(self, application_id: str) -> dict[str, Any]:
        draft = await self.store.get_draft(application_id)
        snapshot: ApplicationSnapshot = draft["snapshot"]
        errors = self.blocks.validate_workflow(snapshot.workflow)
        known_tools = set(self.tools.names())
        for agent in snapshot.agents.values():
            unknown_tools = set(agent.tools) - known_tools
            if unknown_tools:
                errors.append(
                    f"agent {agent.id} references unknown tools: {sorted(unknown_tools)}; "
                    f"available tools: {sorted(known_tools)}"
                )
        for node in snapshot.workflow.nodes:
            if node.type == "claude_agent":
                agent_id = str(node.config.get("agent_id", ""))
                if agent_id not in snapshot.agents:
                    try:
                        await self.store.storage.get_agent(agent_id, node.config.get("version"))
                    except KeyError:
                        errors.append(f"{node.id}: agent binding not found: {agent_id}")
            if node.type == "tool":
                tool_name = str(node.config.get("tool_name", ""))
                if tool_name and not tool_name.startswith("workflow:") and tool_name not in known_tools:
                    errors.append(
                        f"{node.id}: tool binding not found: {tool_name}; "
                        f"available tools: {sorted(known_tools)}"
                    )
            if node.type == "tool_executor":
                tool_name = str(node.config.get("settings", {}).get("tool_name", ""))
                if tool_name and tool_name not in known_tools:
                    errors.append(
                        f"{node.id}: tool binding not found: {tool_name}; "
                        f"available tools: {sorted(known_tools)}"
                    )
        mandatory_tests = [test for test in snapshot.tests if test.mandatory]
        if not mandatory_tests:
            errors.append("at least one mandatory acceptance test is required")
        node_types = [node.type for node in snapshot.workflow.nodes]
        tool_node_names = [
            str(node.config.get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool" and node.config.get("tool_name")
        ]
        tool_node_names.extend(
            str(node.config.get("settings", {}).get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool_executor" and node.config.get("settings", {}).get("tool_name")
        )
        for test in mandatory_tests:
            missing_node_types = [item for item in test.required_node_types if item not in node_types]
            if missing_node_types:
                errors.append(f"test {test.id} missing required node types: {missing_node_types}")
            missing_tool_nodes = [item for item in test.required_tool_nodes if item not in tool_node_names]
            if missing_tool_nodes:
                errors.append(f"test {test.id} missing required tool nodes: {missing_tool_nodes}")
        warnings = self._input_warnings(snapshot)
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "revision": draft["revision"],
            "content_hash": draft["content_hash"],
            "test_count": len(snapshot.tests),
        }

    @staticmethod
    def _node(snapshot: ApplicationSnapshot, node_id: str) -> NodeSpec:
        try:
            return next(node for node in snapshot.workflow.nodes if node.id == node_id)
        except StopIteration as error:
            raise KeyError(f"node not found: {node_id}") from error

    @classmethod
    def _deep_merge(cls, original: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        merged = dict(original)
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = cls._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    @classmethod
    def _input_warnings(cls, snapshot: ApplicationSnapshot) -> list[str]:
        warnings: list[str] = []
        for start in [node for node in snapshot.workflow.nodes if node.type == "start"]:
            fields = [
                str(item.get("name", ""))
                for item in start.config.get("inputs", [])
                if isinstance(item, dict) and item.get("name")
            ]
            if not fields:
                continue
            used = cls._used_start_inputs(snapshot, start.id)
            missing = [name for name in fields if name not in used]
            if missing:
                warnings.append(
                    f"{start.id}: workflow inputs are not connected to downstream nodes: {missing}"
                )
        return warnings

    @classmethod
    def _used_start_inputs(cls, snapshot: ApplicationSnapshot, start_id: str) -> set[str]:
        used: set[str] = set()
        downstream_configs = [
            node.config for node in snapshot.workflow.nodes if node.id != start_id
        ]
        for reference in cls._iter_refs(downstream_configs):
            node_id = reference.get("node_id")
            path = reference.get("path") or []
            if node_id == "$inputs":
                if path:
                    used.add(str(path[0]))
                else:
                    used.add("*")
            elif node_id == start_id:
                if not path or path == ["output"]:
                    used.add("*")
                elif path[0] == "output" and len(path) > 1:
                    used.add(str(path[1]))
                else:
                    used.add(str(path[0]))
        if "*" in used:
            return {
                str(item.get("name", ""))
                for node in snapshot.workflow.nodes if node.id == start_id
                for item in node.config.get("inputs", [])
                if isinstance(item, dict) and item.get("name")
            }
        return used

    @classmethod
    def _iter_refs(cls, value: Any) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def visit(item: Any) -> None:
            if isinstance(item, list):
                for child in item:
                    visit(child)
                return
            if not isinstance(item, dict):
                return
            reference = item.get("$ref")
            if isinstance(reference, dict):
                refs.append(reference)
            for child in item.values():
                visit(child)

        visit(value)
        return refs
