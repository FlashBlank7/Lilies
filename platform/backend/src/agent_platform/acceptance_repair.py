from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .blocks import BlockRegistry
from .capability_contracts import evaluate_capability_contract
from .workflow_models import ApplicationSnapshot, EdgeSpec, NodeSpec, WorkflowSpec


class AcceptanceRepairPreviewRequest(BaseModel):
    report: dict[str, Any] | None = None
    test_id: str | None = None
    instruction: str | None = Field(default=None, max_length=4000)
    reference_node_ids: list[str] = Field(default_factory=list, max_length=50)


class AcceptanceRepairApplyRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    expected_content_hash: str = Field(min_length=1, max_length=128)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


class AcceptanceRepairContext(BaseModel):
    test_id: str = ""
    test_name: str = ""
    requirement: str = ""
    failed_assertions: list[dict[str, Any]] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    required_node_types: list[str] = Field(default_factory=list)
    required_tool_nodes: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    run_id: str = ""
    trace_excerpts: list[str] = Field(default_factory=list)
    relevant_node_ids: list[str] = Field(default_factory=list)
    current_revision: int = 0
    current_content_hash: str = ""
    capability_ids: list[str] = Field(default_factory=list)
    evidence_target: dict[str, Any] | None = None
    capability_contract_id: str = ""
    required_envelope: str = ""
    claim_ceiling: str = ""
    external_contract_gaps: list[str] = Field(default_factory=list)


class AcceptanceRepairPreviewResponse(BaseModel):
    supported: bool
    message: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fixes: list[dict[str, Any]] = Field(default_factory=list)
    missing_node_types: list[str] = Field(default_factory=list)
    unsupported_node_types: list[str] = Field(default_factory=list)
    expected_revision: int = 0
    expected_content_hash: str = ""
    instruction: str = ""
    rationale_markdown: str = ""
    repair_context: AcceptanceRepairContext = Field(default_factory=AcceptanceRepairContext)
    reference_node_ids: list[str] = Field(default_factory=list)
    preview_source: str = "acceptance_structural"
    workflow_edit_preview: dict[str, Any] | None = None


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
        *,
        content_hash: str = "",
        test_id: str | None = None,
        instruction: str | None = None,
        reference_node_ids: list[str] | None = None,
        trace_excerpts: list[str] | None = None,
    ) -> AcceptanceRepairPreviewResponse:
        context = self._repair_context(
            snapshot,
            revision,
            content_hash,
            report or {},
            test_id=test_id,
            reference_node_ids=reference_node_ids or [],
            trace_excerpts=trace_excerpts or [],
        )
        repair_instruction = (instruction or "").strip() or self._repair_instruction(context)
        rationale_markdown = self._repair_rationale(context)
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
                expected_revision=revision,
                expected_content_hash=content_hash,
                instruction=repair_instruction,
                rationale_markdown=rationale_markdown,
                repair_context=context,
                reference_node_ids=context.relevant_node_ids,
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
        if previous_id == source_id and terminal_id:
            previous_id = self._terminal_input_source(snapshot, terminal_id) or previous_id
        source_port = (
            "text"
            if self._node_type_after_ops(snapshot, operations, previous_id)
            in {"llm", "question_classifier", "template_transform"}
            else "output"
        )
        answer_ref = {"$ref": {"node_id": previous_id, "path": [source_port]}}
        terminal_connected = bool(
            terminal_id
            and self._edge_exists(snapshot, operations, previous_id, terminal_id)
        )
        terminal_answer_matches = bool(
            terminal_id
            and self._terminal_answer_matches(
                snapshot,
                terminal_id,
                terminal_type,
                answer_ref,
            )
        )
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
            if previous_id != terminal_id and not terminal_connected:
                operations.append(self._add_edge(
                    revision,
                    self._edge(previous_id, terminal_id, existing_edge_ids, source_port=source_port),
                ))
            fixes.append({"kind": "convert_terminal_to_answer", "node_id": terminal_id})
        elif answer_assertion_required and terminal_id and terminal_type == "end" and (
            not terminal_answer_matches or not terminal_connected
        ):
            if not terminal_answer_matches:
                operations.append({
                    "expected_revision": revision,
                    "op": "update_node",
                    "data": {
                        "node_id": terminal_id,
                        "changes": {"config": {"outputs": {"answer": answer_ref}}},
                    },
                })
            if previous_id != terminal_id and not terminal_connected:
                operations.append(self._add_edge(
                    revision,
                    self._edge(previous_id, terminal_id, existing_edge_ids, source_port=source_port),
                ))
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
            operations.append(self._add_edge(
                revision,
                self._edge(previous_id, node_id, existing_edge_ids, source_port=source_port),
            ))
            fixes.append({"kind": "add_missing_node_type", "node_type": "answer", "node_id": node_id})
        elif answer_assertion_required and terminal_id and terminal_type == "answer" and (
            not terminal_answer_matches or not terminal_connected
        ):
            if not terminal_answer_matches:
                operations.append({
                    "expected_revision": revision,
                    "op": "update_node",
                    "data": {
                        "node_id": terminal_id,
                        "changes": {"config": {"answer": answer_ref}},
                        "merge_config": False,
                    },
                })
            if previous_id != terminal_id and not terminal_connected:
                operations.append(self._add_edge(
                    revision,
                    self._edge(previous_id, terminal_id, existing_edge_ids, source_port=source_port),
                ))
            fixes.append({"kind": "update_answer_output", "node_id": terminal_id})

        if terminal_id and previous_id != source_id:
            for edge in snapshot.workflow.edges:
                if edge.source == source_id and edge.target == terminal_id and edge.branch is None:
                    operations.append({
                        "expected_revision": revision,
                        "op": "remove_edge",
                        "data": {"edge_id": edge.id},
                    })
                    fixes.append({
                        "kind": "remove_redundant_start_terminal_edge",
                        "edge_id": edge.id,
                    })

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
            expected_revision=revision,
            expected_content_hash=content_hash,
            instruction=repair_instruction,
            rationale_markdown=rationale_markdown,
            repair_context=context,
            reference_node_ids=context.relevant_node_ids,
        )

    @staticmethod
    def _dict_items(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @classmethod
    def _repair_context(
        cls,
        snapshot: ApplicationSnapshot,
        revision: int,
        content_hash: str,
        report: dict[str, Any],
        *,
        test_id: str | None,
        reference_node_ids: list[str],
        trace_excerpts: list[str],
    ) -> AcceptanceRepairContext:
        report_tests = cls._dict_items(report.get("tests"))
        selected_result = next(
            (item for item in report_tests if test_id and str(item.get("test_id")) == test_id),
            None,
        )
        if selected_result is None:
            selected_result = next(
                (item for item in report_tests if item.get("passed") is False),
                None,
            )
        selected_id = str((selected_result or {}).get("test_id") or test_id or "")
        test_spec = next((item for item in snapshot.tests if item.id == selected_id), None)
        if test_spec is None and not selected_result:
            test_spec = next((item for item in snapshot.tests if item.mandatory), None)
            selected_id = test_spec.id if test_spec else "validation"

        readable = (selected_result or {}).get("readable_report")
        readable = readable if isinstance(readable, dict) else {}
        failed_assertions = [
            item
            for item in cls._dict_items((selected_result or {}).get("assertions"))
            if item.get("passed") is False
        ]
        failed_checks = [str(item) for item in readable.get("failed_checks", [])]
        validation = report.get("validation")
        if isinstance(validation, dict):
            failed_checks.extend(str(item) for item in validation.get("errors", []))
        required_node_types = list(test_spec.required_node_types) if test_spec else []
        required_tool_nodes = list(test_spec.required_tool_nodes) if test_spec else []
        required_tools = list(test_spec.required_tools) if test_spec else []
        if not test_spec:
            required_node_types = cls._required_node_types(snapshot)

        known_nodes = {node.id: node for node in snapshot.workflow.nodes}
        relevant: list[str] = []
        for node_id in reference_node_ids:
            value = str(node_id)
            if value in known_nodes and value not in relevant:
                relevant.append(value)
        for node in snapshot.workflow.nodes:
            if node.type in required_node_types and node.id not in relevant:
                relevant.append(node.id)
        failure_target = str(readable.get("failure_target") or "")
        if failure_target in known_nodes and failure_target not in relevant:
            relevant.append(failure_target)

        combined_trace = [str(item)[:500] for item in trace_excerpts]
        combined_trace.extend(item[:500] for item in failed_checks if item[:500] not in combined_trace)
        contract = snapshot.capability_build_contract
        closure = evaluate_capability_contract(contract) if contract is not None else None
        return AcceptanceRepairContext(
            test_id=selected_id,
            test_name=str((selected_result or {}).get("name") or (test_spec.name if test_spec else "Draft validation")),
            requirement=str(test_spec.requirement if test_spec else snapshot.requirement),
            failed_assertions=failed_assertions,
            failed_checks=failed_checks,
            required_node_types=required_node_types,
            required_tool_nodes=required_tool_nodes,
            required_tools=required_tools,
            run_id=str((selected_result or {}).get("run_id") or ""),
            trace_excerpts=combined_trace[:20],
            relevant_node_ids=relevant[:50],
            current_revision=revision,
            current_content_hash=content_hash,
            capability_ids=list(test_spec.capability_ids) if test_spec else [],
            evidence_target=(
                test_spec.evidence_target.model_dump(mode="json")
                if test_spec and test_spec.evidence_target
                else None
            ),
            capability_contract_id=contract.contract_id if contract else "",
            required_envelope=contract.required_envelope.value if contract else "",
            claim_ceiling=closure.claim_ceiling.value if closure else "",
            external_contract_gaps=(
                closure.unavailable_external_contracts if closure else []
            ),
        )

    @staticmethod
    def _repair_instruction(context: AcceptanceRepairContext) -> str:
        requirements = ", ".join(context.required_node_types) or "the failed output contract"
        failures = "; ".join(context.failed_checks[:4]) or f"{len(context.failed_assertions)} failed assertions"
        capabilities = ", ".join(context.capability_ids) or "no capability ids declared"
        return (
            f"Repair acceptance case '{context.test_name}' ({context.test_id}) across the whole workflow. "
            f"Requirement: {context.requirement} Required workflow elements: {requirements}. "
            f"Observed failures: {failures}. Capability scope: {capabilities}. "
            f"Claim ceiling: {context.claim_ceiling or 'not declared'}. "
            "Use referenced nodes as context, preserve unrelated behavior, "
            "and return an inspectable draft edit preview rather than mutating the workflow automatically."
        )

    @staticmethod
    def _repair_rationale(context: AcceptanceRepairContext) -> str:
        node_types = ", ".join(context.required_node_types) or "None declared"
        tools = ", ".join(context.required_tools) or "None declared"
        capabilities = ", ".join(context.capability_ids) or "None declared"
        return (
            f"## Failed acceptance\n\n"
            f"**{context.test_name or context.test_id}**\n\n"
            f"{context.requirement}\n\n"
            f"- Required blocks: {node_types}\n"
            f"- Required tools: {tools}\n"
            f"- Capability ids: {capabilities}\n"
            f"- Evidence target: {context.evidence_target or 'None declared'}\n"
            f"- Contract/envelope: {context.capability_contract_id or 'None'} / {context.required_envelope or 'None'}\n"
            f"- Claim ceiling: {context.claim_ceiling or 'None'}\n"
            f"- External contract gaps: {', '.join(context.external_contract_gaps) or 'None'}\n"
            f"- Failed checks: {len(context.failed_checks)}\n"
            f"- Failed assertions: {len(context.failed_assertions)}\n"
            f"- Run: {context.run_id or 'No run record'}"
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
    def _terminal_input_source(snapshot: ApplicationSnapshot, terminal_id: str) -> str:
        node_types = {node.id: node.type for node in snapshot.workflow.nodes}
        sources = [
            edge.source
            for edge in snapshot.workflow.edges
            if edge.target == terminal_id and edge.branch is None
        ]
        non_start_sources = [
            source
            for source in sources
            if node_types.get(source) not in {"start", "schedule_trigger"}
        ]
        return (non_start_sources or sources or [""])[-1]

    @staticmethod
    def _terminal_answer_matches(
        snapshot: ApplicationSnapshot,
        terminal_id: str,
        terminal_type: str,
        answer_ref: dict[str, Any],
    ) -> bool:
        node = next((item for item in snapshot.workflow.nodes if item.id == terminal_id), None)
        if not node:
            return False
        if terminal_type == "answer":
            return node.config.get("answer") == answer_ref
        outputs = node.config.get("outputs")
        return isinstance(outputs, dict) and outputs.get("answer") == answer_ref

    @staticmethod
    def _edge_exists(
        snapshot: ApplicationSnapshot,
        operations: list[dict[str, Any]],
        source: str,
        target: str,
    ) -> bool:
        if any(edge.source == source and edge.target == target for edge in snapshot.workflow.edges):
            return True
        return any(
            operation.get("op") == "add_edge"
            and operation.get("data", {}).get("edge", {}).get("source") == source
            and operation.get("data", {}).get("edge", {}).get("target") == target
            for operation in operations
        )

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
