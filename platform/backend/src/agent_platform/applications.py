from __future__ import annotations

import hashlib
import json
from typing import Any

from .blocks import BlockRegistry
from .capability_contracts import (
    CapabilityBuildContract,
    EvidenceEnvironment,
    EvidenceLevel,
    VerificationStatus,
    evaluate_capability_contract,
)
from .models import AgentSpec
from .workflow_models import (
    ApplicationMode,
    ApplicationSnapshot,
    DraftOperation,
    EdgeSpec,
    NodeSpec,
    WorkflowSpec,
    WorkflowTestCase,
)
from .workflow_storage import RevisionConflict, WorkflowStorage
from .tools import ToolRegistry


class ApplicationService:
    def __init__(self, store: WorkflowStorage, blocks: BlockRegistry, tools: ToolRegistry) -> None:
        self.store = store
        self.blocks = blocks
        self.tools = tools

    async def apply_operation(
        self,
        application_id: str,
        operation: DraftOperation,
        *,
        formal_mutation_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        draft = await self.store.get_draft(application_id)
        snapshot: ApplicationSnapshot = draft["snapshot"].model_copy(deep=True)
        original_snapshot = snapshot.model_dump(mode="json")
        data = operation.data
        self._apply_to_snapshot(snapshot, operation.op, data)

        snapshot = ApplicationSnapshot.model_validate(snapshot.model_dump(mode="json"))
        if snapshot.model_dump(mode="json") == original_snapshot:
            raise ValueError("draft operation would not change the workflow")
        result = await self.store.save_draft(
            application_id,
            snapshot,
            expected_revision=operation.expected_revision,
            idempotency_key=operation.idempotency_key,
            change_context=self._change_context(operation.op, data),
            idempotency_digest=self._operation_idempotency_digest(
                application_id,
                operation,
            ),
            formal_mutation_context=formal_mutation_context,
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
        change_context_operation: str = "acceptance_repair",
    ) -> dict[str, Any]:
        if not operations:
            raise ValueError("atomic draft update has no operations")
        idempotency_digest = self._atomic_idempotency_digest(
            application_id=application_id,
            expected_revision=expected_revision,
            expected_content_hash=expected_content_hash,
            operations=operations,
            change_context_operation=change_context_operation,
        )
        replay = await self.store.get_draft_idempotency(
            application_id,
            idempotency_key,
        )
        if replay is not None:
            replay_digest = replay.pop("_idempotency_digest", None)
            if replay_digest is None:
                raise RevisionConflict(
                    "idempotency key belongs to an older unverifiable draft mutation"
                )
            if replay_digest != idempotency_digest:
                raise RevisionConflict(
                    "idempotency key was already used for a different atomic draft edit"
                )
            replay.update({
                "operations_applied": len(operations),
                "previous_content_hash": expected_content_hash,
            })
            return replay
        draft = await self.store.get_draft(application_id)
        if int(draft["revision"]) != expected_revision:
            raise RevisionConflict(
                f"repair revision conflict: expected {expected_revision}, current {draft['revision']}"
            )
        if draft["content_hash"] != expected_content_hash:
            raise RevisionConflict("repair content hash no longer matches the current draft")
        snapshot: ApplicationSnapshot = draft["snapshot"].model_copy(deep=True)
        original_snapshot = snapshot.model_dump(mode="json")
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
        if snapshot.model_dump(mode="json") == original_snapshot:
            raise ValueError("atomic draft update would not change the workflow")
        result = await self.store.save_draft(
            application_id,
            snapshot,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            change_context={
                "operation": change_context_operation,
                "operation_count": len(operation_names),
                "operation_types": operation_names[:20],
            },
            idempotency_digest=idempotency_digest,
        )
        result.update({
            "operations_applied": len(operation_names),
            "previous_content_hash": expected_content_hash,
        })
        return result

    @staticmethod
    def _operation_idempotency_digest(
        application_id: str,
        operation: DraftOperation,
    ) -> str:
        encoded = json.dumps(
            {
                "application_id": application_id,
                "expected_revision": operation.expected_revision,
                "op": operation.op,
                "data": operation.data,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _atomic_idempotency_digest(
        *,
        application_id: str,
        expected_revision: int,
        expected_content_hash: str,
        operations: list[dict[str, Any]],
        change_context_operation: str,
    ) -> str:
        payload = {
            "application_id": application_id,
            "expected_revision": expected_revision,
            "expected_content_hash": expected_content_hash,
            "operations": operations,
            "change_context_operation": change_context_operation,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def validate_preview_operations(
        self,
        snapshot: ApplicationSnapshot,
        operations: list[DraftOperation],
    ) -> ApplicationSnapshot:
        original_snapshot = snapshot.model_dump(mode="json")
        preview = snapshot.model_copy(deep=True)
        for operation in operations:
            self._apply_to_snapshot(preview, operation.op, operation.data)
        validated = ApplicationSnapshot.model_validate(
            preview.model_dump(mode="json")
        )
        if validated.model_dump(mode="json") == original_snapshot:
            raise ValueError("workflow edit preview would not change the workflow")
        return validated

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
            changes = dict(data.get("changes") or {})
            node_path = self._unique_node_path(
                snapshot.workflow,
                str(data["node_id"]),
            )
            self._update_node_at_path(
                snapshot.workflow,
                node_path,
                changes,
                merge_config=bool(data.get("merge_config", True)),
            )
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
            source = self._node(snapshot, edge.source)
            target = self._node(snapshot, edge.target)
            if source.id == target.id:
                raise ValueError("workflow edge cannot connect a node to itself")
            if any(
                (
                    item.source,
                    item.target,
                    item.source_port,
                    item.target_port,
                    item.branch,
                )
                == (
                    edge.source,
                    edge.target,
                    edge.source_port,
                    edge.target_port,
                    edge.branch,
                )
                for item in snapshot.workflow.edges
            ):
                raise ValueError("workflow edge already connects the same ports")
            edge_errors = self.blocks.validate_edge(source, target, edge)
            if edge_errors:
                raise ValueError("workflow edge is invalid: " + "; ".join(edge_errors))
            if self._edge_would_create_cycle(snapshot.workflow, edge):
                raise ValueError(
                    "workflow edge would create a cycle; use an explicit loop block"
                )
            snapshot.workflow.edges.append(edge)
        elif operation == "remove_edge":
            edge_id = str(data["edge_id"])
            if not any(edge.id == edge_id for edge in snapshot.workflow.edges):
                raise KeyError(f"edge not found: {edge_id}")
            snapshot.workflow.edges = [edge for edge in snapshot.workflow.edges if edge.id != edge_id]
        elif operation == "set_metadata":
            for field in ("name", "description", "mode", "requirement"):
                if field in data:
                    value = data[field]
                    if field == "mode":
                        value = ApplicationMode(value)
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
        elif operation == "replace_workflow":
            workflow = WorkflowSpec.model_validate(data["workflow"])
            errors = self.blocks.validate_workflow(workflow)
            if errors:
                raise ValueError("replacement workflow is invalid: " + "; ".join(errors))
            snapshot.workflow = workflow
        elif operation == "replace_tests":
            tests = [WorkflowTestCase.model_validate(item) for item in data.get("tests", [])]
            if not any(test.mandatory for test in tests):
                raise ValueError("replacement tests require at least one mandatory case")
            snapshot.tests = tests
        elif operation == "set_capability_build_contract":
            contract = CapabilityBuildContract.model_validate(data["contract"])
            closure = evaluate_capability_contract(contract)
            if not closure.valid:
                raise ValueError(
                    "capability build contract is invalid: "
                    + "; ".join(closure.blocking_errors)
                )
            snapshot.capability_build_contract = contract
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

    @staticmethod
    def _edge_would_create_cycle(workflow: WorkflowSpec, edge: EdgeSpec) -> bool:
        outgoing: dict[str, list[str]] = {}
        for item in workflow.edges:
            outgoing.setdefault(item.source, []).append(item.target)
        pending = [edge.target]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == edge.source:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(outgoing.get(current, []))
        return False

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
        errors.extend(self._validate_simulated_human_inputs(snapshot))
        warnings = self._input_warnings(snapshot)
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "revision": draft["revision"],
            "content_hash": draft["content_hash"],
            "test_count": len(snapshot.tests),
        }

    def _validate_simulated_human_inputs(
        self,
        snapshot: ApplicationSnapshot,
    ) -> list[str]:
        errors: list[str] = []
        node_map = self._workflow_nodes_by_id(snapshot.workflow)
        for test in snapshot.tests:
            for node_id, values in test.simulated_human_inputs.items():
                nodes = node_map.get(node_id, [])
                if not nodes:
                    errors.append(
                        f"test {test.id} simulated human input references unknown node: {node_id}"
                    )
                    continue
                if any(node.type != "human_input" for node in nodes):
                    errors.append(
                        f"test {test.id} simulated human input references non-human node: {node_id}"
                    )
                    continue
                for node in nodes:
                    fields = {
                        str(item.get("name")): item
                        for item in node.config.get("fields", [])
                        if isinstance(item, dict) and item.get("name")
                    }
                    unknown = sorted(set(values) - set(fields))
                    if unknown:
                        error = (
                            f"test {test.id} simulated human input for {node_id} "
                            f"contains unknown fields: {unknown}"
                        )
                        if error not in errors:
                            errors.append(error)
                    missing = sorted(
                        name
                        for name, field in fields.items()
                        if field.get("required", True) and values.get(name) is None
                    )
                    if missing:
                        error = (
                            f"test {test.id} simulated human input for {node_id} "
                            f"is missing required fields: {missing}"
                        )
                        if error not in errors:
                            errors.append(error)
        return errors

    @staticmethod
    def _workflow_nodes_by_id(
        workflow: WorkflowSpec,
    ) -> dict[str, list[NodeSpec]]:
        nodes_by_id: dict[str, list[NodeSpec]] = {}
        pending = [workflow]
        while pending:
            current = pending.pop()
            for node in current.nodes:
                nodes_by_id.setdefault(node.id, []).append(node)
                nested = node.config.get("workflow")
                if not isinstance(nested, dict):
                    continue
                try:
                    pending.append(WorkflowSpec.model_validate(nested))
                except ValueError:
                    # The normal nested-workflow validation reports the
                    # structural error. Do not make an invalid nested graph a
                    # source of accepted simulated-human node identities.
                    continue
        return nodes_by_id


    @staticmethod
    def _evidence_level_rank(value: EvidenceLevel) -> int:
        return list(EvidenceLevel).index(value)

    @staticmethod
    def _evidence_environment_rank(value: EvidenceEnvironment) -> int:
        return list(EvidenceEnvironment).index(value)

    @staticmethod
    def _verification_status_rank(value: VerificationStatus) -> int:
        ordered = [
            VerificationStatus.design_only,
            VerificationStatus.static_verified,
            VerificationStatus.component_verified,
            VerificationStatus.integration_verified,
            VerificationStatus.live_verified,
            VerificationStatus.production_observed,
        ]
        return ordered.index(value) if value in ordered else -1

    @classmethod
    def _test_meets_evidence_plan(cls, test: WorkflowTestCase, plan: Any) -> bool:
        target = test.evidence_target
        if target is None or test.structural_only:
            return False
        return (
            cls._evidence_level_rank(target.level)
            >= cls._evidence_level_rank(plan.target_level)
            and cls._evidence_environment_rank(target.environment)
            >= cls._evidence_environment_rank(plan.environment)
            and cls._verification_status_rank(target.expected_status)
            >= cls._verification_status_rank(plan.expected_status)
        )

    @staticmethod
    def _node(snapshot: ApplicationSnapshot, node_id: str) -> NodeSpec:
        try:
            return next(node for node in snapshot.workflow.nodes if node.id == node_id)
        except StopIteration as error:
            raise KeyError(f"node not found: {node_id}") from error

    @classmethod
    def _node_paths(
        cls,
        workflow: WorkflowSpec,
        node_id: str,
    ) -> list[tuple[int, ...]]:
        paths: list[tuple[int, ...]] = []
        for index, node in enumerate(workflow.nodes):
            if node.id == node_id:
                paths.append((index,))
            nested = node.config.get("workflow")
            if not isinstance(nested, dict):
                continue
            nested_workflow = WorkflowSpec.model_validate(nested)
            paths.extend(
                (index, *nested_path)
                for nested_path in cls._node_paths(nested_workflow, node_id)
            )
        return paths

    @classmethod
    def _unique_node_path(
        cls,
        workflow: WorkflowSpec,
        node_id: str,
    ) -> tuple[int, ...]:
        paths = cls._node_paths(workflow, node_id)
        if not paths:
            raise KeyError(f"node not found: {node_id}")
        if len(paths) != 1:
            raise ValueError(
                f"node id is ambiguous across nested workflows: {node_id}"
            )
        return paths[0]

    @classmethod
    def _node_at_path(
        cls,
        workflow: WorkflowSpec,
        path: tuple[int, ...],
    ) -> NodeSpec:
        node = workflow.nodes[path[0]]
        if len(path) == 1:
            return node
        nested = node.config.get("workflow")
        if not isinstance(nested, dict):
            raise ValueError("nested workflow node path is invalid")
        return cls._node_at_path(
            WorkflowSpec.model_validate(nested),
            path[1:],
        )

    @classmethod
    def find_draft_node(
        cls,
        snapshot: ApplicationSnapshot,
        node_id: str,
    ) -> NodeSpec:
        return cls._node_at_path(
            snapshot.workflow,
            cls._unique_node_path(snapshot.workflow, node_id),
        )

    def _update_node_at_path(
        self,
        workflow: WorkflowSpec,
        path: tuple[int, ...],
        changes: dict[str, Any],
        *,
        merge_config: bool,
    ) -> None:
        index = path[0]
        node = workflow.nodes[index]
        if len(path) == 1:
            effective_changes = dict(changes)
            if "config" in effective_changes and merge_config:
                effective_changes["config"] = self._deep_merge(
                    node.config,
                    effective_changes["config"],
                )
            updated = NodeSpec.model_validate(
                {
                    **node.model_dump(mode="json"),
                    **effective_changes,
                }
            )
            self.blocks.validate_node(updated)
            workflow.nodes[index] = updated
            return

        nested = node.config.get("workflow")
        if not isinstance(nested, dict):
            raise ValueError("nested workflow node path is invalid")
        nested_workflow = WorkflowSpec.model_validate(nested)
        self._update_node_at_path(
            nested_workflow,
            path[1:],
            changes,
            merge_config=merge_config,
        )
        parent_config = dict(node.config)
        parent_config["workflow"] = nested_workflow.model_dump(mode="json")
        updated_parent = NodeSpec.model_validate(
            {
                **node.model_dump(mode="json"),
                "config": parent_config,
            }
        )
        self.blocks.validate_node(updated_parent)
        workflow.nodes[index] = updated_parent

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
