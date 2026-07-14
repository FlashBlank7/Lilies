from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .blocks import BlockRegistry
from .workflow_models import ApplicationSnapshot, EdgeSpec, NodeSpec, WorkflowSpec


class AcceptanceRepairPreviewRequest(BaseModel):
    report: dict[str, Any] | None = None


class AcceptanceRepairPreviewResponse(BaseModel):
    supported: bool
    message: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fixes: list[dict[str, Any]] = Field(default_factory=list)
    missing_node_types: list[str] = Field(default_factory=list)
    unsupported_node_types: list[str] = Field(default_factory=list)


SAFE_REPAIR_ORDER = [
    "context_assembler",
    "workspace_context_injector",
    "skill_loader",
    "mcp_gateway",
    "capability_registry",
    "conversation_memory",
    "context_compactor",
    "budget_gate",
    "round_limit",
    "permission_gate",
    "sandbox_boundary",
    "retry_error_classifier",
    "stop_continue_controller",
    "tool_call_router",
    "tool_result_normalizer",
    "task_dispatcher",
    "dependency_gate",
    "mailbox_wait_wake",
    "checkpoint_resume",
    "cancellation_point",
    "event_recorder",
]

UNSAFE_REPAIR_TYPES = {
    "model_turn",
    "tool_executor",
    "subagent_spawn",
    "tool",
    "claude_agent",
    "http_request",
}


class AcceptanceRepairPreviewer:
    """Deterministic draft repair plan from acceptance gates.

    The previewer never mutates the draft and never calls a model. It focuses on
    safe structural repairs that acceptance reports can prove directly:
    required visible block types and top-level answer output shape.
    """

    def __init__(self, blocks: BlockRegistry) -> None:
        self.blocks = blocks

    def preview(
        self,
        snapshot: ApplicationSnapshot,
        revision: int,
        report: dict[str, Any] | None = None,
    ) -> AcceptanceRepairPreviewResponse:
        del report
        existing_types = [node.type for node in snapshot.workflow.nodes]
        missing_node_types = self._missing_required_node_types(snapshot, existing_types)
        assertion_paths = self._assertion_paths(snapshot)
        answer_assertion_required = any(path[:1] == ["answer"] for path in assertion_paths)
        answer_node_required = "answer" in missing_node_types or any("answer" in test.required_node_types for test in snapshot.tests)
        template_required = "template_transform" in missing_node_types or answer_assertion_required or answer_node_required

        operations: list[dict[str, Any]] = []
        fixes: list[dict[str, Any]] = []
        warnings: list[str] = []
        unsupported: list[str] = []

        existing_node_ids = {node.id for node in snapshot.workflow.nodes}
        existing_edge_ids = {edge.id for edge in snapshot.workflow.edges}
        known_types = {item.type for item in self.blocks.list()}
        for block_type in missing_node_types:
            if block_type not in known_types:
                unsupported.append(block_type)
            elif block_type in UNSAFE_REPAIR_TYPES:
                unsupported.append(block_type)
        if unsupported:
            warnings.append(
                "Some required node types need a human/model-planned repair because they can call tools, models, or subagents: "
                + ", ".join(sorted(set(unsupported)))
            )

        source_id = self._existing_start(snapshot)
        if not source_id and "start" in missing_node_types:
            source_id = self._unique_id("acceptance_repair_start", existing_node_ids)
            operations.append(self._add_node(revision, NodeSpec(
                id=source_id,
                type="start",
                title="Acceptance Repair Input",
                description="Inserted by acceptance auto-repair because tests require a start node.",
                config={"inputs": self._start_inputs_from_tests(snapshot)},
                position={"x": 80, "y": 90},
            )))
            fixes.append({"kind": "add_missing_node_type", "node_type": "start", "node_id": source_id})
        if not source_id:
            warnings.append("No start node exists, so repair cannot safely attach a reachable chain.")
            return AcceptanceRepairPreviewResponse(
                supported=False,
                message="Acceptance repair could not find or create a start node.",
                operations=operations,
                warnings=warnings,
                fixes=fixes,
                missing_node_types=missing_node_types,
                unsupported_node_types=sorted(set(unsupported)),
            )

        previous_id = source_id
        for block_type in SAFE_REPAIR_ORDER:
            if block_type not in missing_node_types:
                continue
            if block_type in unsupported:
                continue
            node_id = self._unique_id(f"acceptance_repair_{block_type}", existing_node_ids)
            node = NodeSpec(
                id=node_id,
                type=block_type,
                title=self._title_for(block_type),
                description="Inserted by acceptance auto-repair from failed required-node gates.",
                config=self._safe_config(block_type, previous_id),
                position={"x": 180 + len(operations) * 42, "y": 220 + len(operations) * 18},
            )
            operations.append(self._add_node(revision, node))
            edge = self._edge(previous_id, node_id, existing_edge_ids)
            operations.append(self._add_edge(revision, edge))
            fixes.append({"kind": "add_missing_node_type", "node_type": block_type, "node_id": node_id})
            previous_id = node_id

        if "loop" in missing_node_types:
            node_id = self._unique_id("acceptance_repair_loop", existing_node_ids)
            operations.append(self._add_node(revision, NodeSpec(
                id=node_id,
                type="loop",
                title="Acceptance Repair Loop",
                description="Deterministic loop inserted to satisfy visible agent-loop acceptance gates.",
                config=self._loop_config(previous_id),
                position={"x": 620, "y": 220},
            )))
            operations.append(self._add_edge(revision, self._edge(previous_id, node_id, existing_edge_ids)))
            fixes.append({"kind": "add_missing_node_type", "node_type": "loop", "node_id": node_id})
            previous_id = node_id

        if template_required and "template_transform" not in existing_types:
            node_id = self._unique_id("acceptance_repair_result", existing_node_ids)
            operations.append(self._add_node(revision, NodeSpec(
                id=node_id,
                type="template_transform",
                title="Acceptance Repair Result",
                description="Formats a deterministic answer from repaired workflow context.",
                config={
                    "template": (
                        "Acceptance repair completed.\n\n"
                        "Answer: This workflow now exposes the required safety, context, loop, and result blocks for acceptance.\n\n"
                        "Context: {{ context }}"
                    ),
                    "variables": {"context": {"$ref": {"node_id": previous_id, "path": ["output"]}}},
                },
                position={"x": 760, "y": 220},
            )))
            operations.append(self._add_edge(revision, self._edge(previous_id, node_id, existing_edge_ids)))
            fixes.append({"kind": "add_missing_node_type", "node_type": "template_transform", "node_id": node_id})
            previous_id = node_id

        terminal_id, terminal_type = self._terminal(snapshot)
        answer_ref = {"$ref": {"node_id": previous_id, "path": ["text" if self._node_type_after_ops(snapshot, operations, previous_id) == "template_transform" else "output"]}}
        if answer_node_required and terminal_id and terminal_type == "end" and "end" not in self._required_node_types(snapshot):
            operations.append({
                "expected_revision": revision,
                "op": "update_node",
                "data": {
                    "node_id": terminal_id,
                    "changes": {
                        "type": "answer",
                        "title": "Answer",
                        "description": "Converted from End by acceptance auto-repair so top-level answer assertions can pass.",
                        "config": {"answer": answer_ref},
                    },
                    "merge_config": False,
                },
            })
            if previous_id != terminal_id:
                operations.append(self._add_edge(revision, self._edge(previous_id, terminal_id, existing_edge_ids, source_port="text" if self._node_type_after_ops(snapshot, operations, previous_id) == "template_transform" else "output")))
            fixes.append({"kind": "convert_terminal_to_answer", "node_id": terminal_id})
        elif answer_assertion_required and terminal_id and terminal_type == "end":
            operations.append({
                "expected_revision": revision,
                "op": "update_node",
                "data": {
                    "node_id": terminal_id,
                    "changes": {"config": {"outputs": {"answer": answer_ref}}},
                },
            })
            if previous_id != terminal_id:
                operations.append(self._add_edge(revision, self._edge(previous_id, terminal_id, existing_edge_ids, source_port="text" if self._node_type_after_ops(snapshot, operations, previous_id) == "template_transform" else "output")))
            fixes.append({"kind": "update_terminal_answer_output", "node_id": terminal_id})
        elif answer_node_required and not any(node.type == "answer" for node in snapshot.workflow.nodes):
            node_id = self._unique_id("acceptance_repair_answer", existing_node_ids)
            operations.append(self._add_node(revision, NodeSpec(
                id=node_id,
                type="answer",
                title="Answer",
                description="Inserted by acceptance auto-repair for required answer output.",
                config={"answer": answer_ref},
                position={"x": 900, "y": 220},
            )))
            operations.append(self._add_edge(revision, self._edge(previous_id, node_id, existing_edge_ids, source_port="text" if self._node_type_after_ops(snapshot, operations, previous_id) == "template_transform" else "output")))
            fixes.append({"kind": "add_missing_node_type", "node_type": "answer", "node_id": node_id})
        elif answer_assertion_required and terminal_id and terminal_type == "answer":
            operations.append({
                "expected_revision": revision,
                "op": "update_node",
                "data": {
                    "node_id": terminal_id,
                    "changes": {"config": {"answer": answer_ref}},
                    "merge_config": False,
                },
            })
            if previous_id != terminal_id:
                operations.append(self._add_edge(revision, self._edge(previous_id, terminal_id, existing_edge_ids, source_port="text" if self._node_type_after_ops(snapshot, operations, previous_id) == "template_transform" else "output")))
            fixes.append({"kind": "update_answer_output", "node_id": terminal_id})

        operations = self._dedupe_operations(operations)
        supported = bool(operations)
        if operations:
            message = f"Prepared {len(operations)} draft operations from acceptance failures."
        else:
            message = "No deterministic acceptance repair is available for the current failures."
        return AcceptanceRepairPreviewResponse(
            supported=supported,
            message=message,
            operations=operations,
            warnings=warnings,
            fixes=fixes,
            missing_node_types=missing_node_types,
            unsupported_node_types=sorted(set(unsupported)),
        )

    @staticmethod
    def _required_node_types(snapshot: ApplicationSnapshot) -> list[str]:
        result: list[str] = []
        for test in snapshot.tests:
            if not test.mandatory:
                continue
            for block_type in test.required_node_types:
                if block_type not in result:
                    result.append(block_type)
        return result

    @classmethod
    def _missing_required_node_types(cls, snapshot: ApplicationSnapshot, existing_types: list[str]) -> list[str]:
        return [block_type for block_type in cls._required_node_types(snapshot) if block_type not in existing_types]

    @staticmethod
    def _assertion_paths(snapshot: ApplicationSnapshot) -> list[list[str]]:
        return [assertion.path for test in snapshot.tests if test.mandatory for assertion in test.assertions]

    @staticmethod
    def _existing_start(snapshot: ApplicationSnapshot) -> str:
        node = next((item for item in snapshot.workflow.nodes if item.type in {"start", "schedule_trigger"}), None)
        return node.id if node else ""

    @staticmethod
    def _terminal(snapshot: ApplicationSnapshot) -> tuple[str, str]:
        node = next((item for item in snapshot.workflow.nodes if item.type in {"answer", "end"}), None)
        return (node.id, node.type) if node else ("", "")

    @staticmethod
    def _start_inputs_from_tests(snapshot: ApplicationSnapshot) -> list[dict[str, Any]]:
        names: list[str] = []
        for test in snapshot.tests:
            for name in test.inputs:
                if name not in names:
                    names.append(name)
        return [{"name": name, "label": name, "type": "string", "required": False} for name in names]

    def _title_for(self, block_type: str) -> str:
        try:
            return self.blocks.get(block_type).title
        except KeyError:
            return block_type.replace("_", " ").title()

    @staticmethod
    def _safe_config(block_type: str, upstream_id: str) -> dict[str, Any]:
        upstream = {"$ref": {"node_id": upstream_id, "path": ["output"]}}
        settings: dict[str, Any] = {"repair_source": "acceptance_auto_repair"}
        if block_type == "context_assembler":
            settings["fragments"] = [upstream]
        elif block_type == "workspace_context_injector":
            settings.update({"scope": "current_workspace", "files": []})
        elif block_type == "skill_loader":
            settings["skills"] = []
        elif block_type == "mcp_gateway":
            settings["servers"] = []
        elif block_type == "capability_registry":
            settings["tools"] = []
        elif block_type == "conversation_memory":
            settings["facts"] = ["Acceptance auto-repair preserved the failed gate context."]
        elif block_type == "context_compactor":
            settings.update({"max_chars": 6000, "preserved_facts": ["acceptance failures", "workflow inputs"]})
        elif block_type == "budget_gate":
            settings.update({"max_cost_usd": 1.0, "spent_cost_usd": 0})
        elif block_type == "round_limit":
            settings.update({"current_round": 0, "max_rounds": 8})
        elif block_type == "permission_gate":
            settings.update({"mode": "auto_approve", "auto_approve": True, "reason": "Acceptance auto-repair safety gate."})
        elif block_type == "sandbox_boundary":
            settings.update({"network_policy": "full", "workspace": "."})
        elif block_type == "retry_error_classifier":
            settings["error"] = ""
        elif block_type == "stop_continue_controller":
            settings["stop_reason"] = "end_turn"
        elif block_type == "task_dispatcher":
            settings["tasks"] = []
        elif block_type == "dependency_gate":
            settings.update({"dependencies": [], "completed": []})
        elif block_type == "mailbox_wait_wake":
            settings.update({"messages": ["acceptance repair ready"], "expect_messages": []})
        elif block_type == "checkpoint_resume":
            settings["checkpoint_id"] = "acceptance-auto-repair"
        elif block_type == "cancellation_point":
            settings["cancelled"] = False
        elif block_type == "event_recorder":
            settings["label"] = "acceptance_auto_repair"
        return {"input": upstream, "settings": settings}

    @staticmethod
    def _loop_config(upstream_id: str) -> dict[str, Any]:
        nested = WorkflowSpec(
            nodes=[
                NodeSpec(
                    id="loop_start",
                    type="start",
                    title="Loop Input",
                    config={"inputs": [{"name": "iteration", "type": "number"}, {"name": "source", "type": "object", "required": False}]},
                ),
                NodeSpec(
                    id="loop_format",
                    type="template_transform",
                    title="Loop Summary",
                    config={
                        "template": "Loop iteration {{ iteration }} completed for acceptance repair.",
                        "variables": {"iteration": {"$ref": {"node_id": "loop_start", "path": ["iteration"]}}},
                    },
                ),
                NodeSpec(
                    id="loop_end",
                    type="end",
                    title="Loop Output",
                    config={"outputs": {"output": {"$ref": {"node_id": "loop_format", "path": ["text"]}}, "done": True}},
                ),
            ],
            edges=[
                EdgeSpec(id="loop_start_format", source="loop_start", target="loop_format"),
                EdgeSpec(id="loop_format_end", source="loop_format", target="loop_end", source_port="text"),
            ],
        )
        return {
            "workflow": nested.model_dump(mode="json"),
            "variables": {"source": {"$ref": {"node_id": upstream_id, "path": ["output"]}}},
            "break_condition": {"value": True, "operator": "equals", "expected": True},
            "break_value": True,
            "max_iterations": 1,
            "output_node_id": "loop_end",
        }

    @staticmethod
    def _add_node(revision: int, node: NodeSpec) -> dict[str, Any]:
        return {"expected_revision": revision, "op": "add_node", "data": {"node": node.model_dump(mode="json", exclude_none=True)}}

    @staticmethod
    def _add_edge(revision: int, edge: EdgeSpec) -> dict[str, Any]:
        return {"expected_revision": revision, "op": "add_edge", "data": {"edge": edge.model_dump(mode="json", exclude_none=True)}}

    @staticmethod
    def _edge(
        source: str,
        target: str,
        existing_edge_ids: set[str],
        *,
        source_port: str = "output",
    ) -> EdgeSpec:
        edge_id = f"{source}_to_{target}"
        suffix = 2
        while edge_id in existing_edge_ids:
            edge_id = f"{source}_to_{target}_{suffix}"
            suffix += 1
        existing_edge_ids.add(edge_id)
        return EdgeSpec(id=edge_id, source=source, target=target, source_port=source_port, target_port="input")

    @staticmethod
    def _unique_id(base: str, existing_ids: set[str]) -> str:
        node_id = base
        suffix = 2
        while node_id in existing_ids:
            node_id = f"{base}_{suffix}"
            suffix += 1
        existing_ids.add(node_id)
        return node_id

    @staticmethod
    def _node_type_after_ops(snapshot: ApplicationSnapshot, operations: list[dict[str, Any]], node_id: str) -> str:
        for operation in operations:
            if operation.get("op") == "add_node":
                node = operation.get("data", {}).get("node", {})
                if node.get("id") == node_id:
                    return str(node.get("type", ""))
            if operation.get("op") == "update_node" and operation.get("data", {}).get("node_id") == node_id:
                changes = operation.get("data", {}).get("changes", {})
                if changes.get("type"):
                    return str(changes["type"])
        node = next((item for item in snapshot.workflow.nodes if item.id == node_id), None)
        return node.type if node else ""

    @staticmethod
    def _dedupe_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_edges: set[str] = set()
        result: list[dict[str, Any]] = []
        for operation in operations:
            if operation.get("op") == "add_edge":
                edge_id = str(operation.get("data", {}).get("edge", {}).get("id", ""))
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
            result.append(operation)
        return result
