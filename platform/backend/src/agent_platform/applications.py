from __future__ import annotations

import hashlib
import json
from typing import Any

from .blocks import BlockRegistry
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


def _wiring_hint(snapshot: Any) -> str:
    """把"现在接了什么、还差什么"算出来附在连线类拒绝后面。

    只说"这条边已存在"等于让构建者去猜下一条该连哪。真机构建 310141fd 里
    4B 对同一条已存在的边连提 9 次被判停——它每轮拿到的信息里根本没有边的清单。
    这些都是从图上机械算得出来的，不给才是平台的问题。
    """
    edges = snapshot.workflow.edges
    nodes = snapshot.workflow.nodes
    wired = "；".join(f"{e.source}→{e.target}" for e in edges) or "（一条都没有）"
    has_out = {e.source for e in edges}
    has_in = {e.target for e in edges}
    terminal = {"end", "answer"}
    starting = {"start", "schedule_trigger"}
    missing_out = [n.id for n in nodes if n.type not in terminal and n.id not in has_out]
    missing_in = [n.id for n in nodes if n.type not in starting and n.id not in has_in]
    parts = [f"；当前已有的边：{wired}"]
    if missing_out:
        parts.append(f"；还没有出边的节点：{'、'.join(missing_out)}")
    if missing_in:
        parts.append(f"；还没有入边的节点：{'、'.join(missing_in)}")
    if not missing_out and not missing_in:
        parts.append("；每个节点都已接上——连线做完了，做下一件事。")
    return "".join(parts)


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
            raise ValueError(self._why_no_change(operation.op, data, snapshot))
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

    def hoist_stray_config_keys(
        self, snapshot: ApplicationSnapshot, node_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """把平铺在 changes 下的配置字段上提进 config（只给构建者路径用）。

        真机构建 7d5ffa06：4B 写 {"node_id":"end","changes":{"outputs":{...}}}，
        被 Pydantic 顶回 "Extra inputs are not permitted"，原样重试 3 次，end 的
        输出始终没绑上；图结构却是合法的，于是带着空输出走完全程。这一步是
        确定性的，机器代劳比反复教它更可靠（机械阶梯第 8 级）。

        **刻意只给构建者用**：自然语言编辑是业主在改自己的工作流，替人猜错了
        就是擅自改动，那条路径必须原样拒绝（回归测试守着这条边界）。
        """
        try:
            node = self._node(snapshot, str(node_id))
            model = self.blocks.config_model(node.type)
        except (KeyError, ValueError):
            return changes
        if model is None:
            return changes
        config_fields = set(model.model_fields)
        node_fields = set(NodeSpec.model_fields)
        updated = dict(changes)
        stray = {
            key: updated.pop(key)
            for key in list(updated)
            if key not in node_fields and key in config_fields
        }
        if not stray:
            return changes
        merged = dict(updated.get("config") or {})
        merged.update(stray)
        updated["config"] = merged
        return updated

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

    # NodeSpec 认得的顶层字段。其余的但凡出现在顶层，
    # 十有八九是本该写进 config 的东西。
    _NODE_FIELDS = frozenset(NodeSpec.model_fields)

    @staticmethod
    def _test_payload(data: dict[str, Any]) -> Any:
        """从 test_add 的参数里把测试本体取出来，容忍模型摊平写。

        真机测量（2026-08-29）：test_add 被拒 27 次，全是同一句裸报错
        `KeyError: 'test'`——模型把 id / name / requirement / assertions
        平铺在了顶层（有几次还自己发明了一个 frame 包着）。
        schema 写的是嵌在 test 下面，但摊平的意图无歧义，
        而 KeyError 什么也没教，模型只能重试。

        取值顺序：显式的 test → frame（模型爱用这个名字）→ 顶层摊平的字段。
        实在认不出就抛一句说得清的话，而不是 KeyError。
        """
        if isinstance(data.get("test"), dict):
            return data["test"]
        frame = data.get("frame")
        if isinstance(frame, dict) and {"name", "requirement"} & set(frame):
            return frame
        flat = {key: value for key, value in data.items()
                if key in WorkflowTestCase.model_fields}
        if {"name", "requirement"} <= set(flat):
            return flat
        raise ValueError(
            "test_add 要把测试放在 test 里："
            '{"test": {"id": "...", "name": "...", "requirement": "...", '
            '"inputs": {...}, "assertions": [...], "mandatory": true}}——'
            "至少要有 name 和 requirement。"
        )

    @classmethod
    def _sink_config_keys(cls, node: Any) -> Any:
        """把误放在节点顶层的配置字段收进 config。

        真机测量（2026-08-29，76 份搭建 transcript）：
        「Extra inputs are not permitted」被拒 53 次，绝大多数是模型把
        outputs / inputs 写在了节点顶层，而它们属于 config。
        模型从那句 pydantic 英文里看不出层级放错了，于是反复重提——
        而"反复提同一个被拒的方案"正是搭建失败的头号原因。

        意图无歧义就别judge：同一个文件里 update_node 早就这么干了
        （merge_config 被嵌进 changes 时直接抬出来，注释写着
        "the intent is unambiguous, so hoist it"）。这里是同一条道理，
        方向相反——该往里放的就往里放。

        只搬 NodeSpec 不认得的键；config 里已经有同名键的不动
        （那说明模型两处都写了，谁覆盖谁没有唯一答案，交给校验去报）。
        """
        if not isinstance(node, dict):
            return node
        stray = {key: value for key, value in node.items()
                 if key not in cls._NODE_FIELDS}
        if not stray:
            return node
        config = dict(node.get("config") or {})
        moved = {key: value for key, value in stray.items() if key not in config}
        if not moved:
            return node
        cleaned = {key: value for key, value in node.items() if key not in moved}
        cleaned["config"] = {**config, **moved}
        return cleaned

    @staticmethod
    def _why_no_change(operation: str, data: dict[str, Any],
                       snapshot: ApplicationSnapshot) -> str:
        """这次改动为什么什么都没变——说清楚，并给出下一步。

        真机测量（2026-08-29，76 份搭建 transcript）：搭建过程中被拒的工具调用
        1087 次，其中 **349 次（32%）** 就是这一条，原文是一句英文：

            draft operation would not change the workflow

        它说了"没变化"，没说**为什么**没变化，也没说**该怎么办**。
        模型能做的只有再试一遍——于是"反复提同一个被拒的方案"成了
        搭建失败的头号原因（57 次失败里 23 次，40%）。

        本项目在别处写过一条原则：**拒绝即教学**。这条一直没照做。
        """
        nodes = {node.id: node for node in snapshot.workflow.nodes}
        names = "、".join(list(nodes)[:8]) or "（图里还没有节点）"
        if operation == "add_node":
            node_id = str((data.get("node") or {}).get("id") or "")
            return (f"「{node_id}」已经在图里了，而且配置和你提交的一模一样，所以什么都没变。"
                    f"要改它用 draft_update_node；要加一个新的，换个 id。")
        if operation == "update_node":
            node_id = str(data.get("node_id") or (data.get("node") or {}).get("id") or "")
            return (f"「{node_id}」现在的配置和你提交的完全一样，改不动。"
                    f"先用 draft_inspect 看它当前长什么样，再决定改哪一处。")
        if operation == "remove_node":
            node_id = str(data.get("node_id") or "")
            return (f"图里没有「{node_id}」，删不掉。现有节点：{names}——"
                    f"你要删的是其中哪一个？名字拿不准就先 draft_inspect。")
        if operation == "add_edge":
            edge = data.get("edge") or data
            return (f"「{edge.get('source')} → {edge.get('target')}」这条连线已经存在了。"
                    f"要改走向先 draft_remove_edge 再连；要接别的节点换一对。")
        if operation == "remove_edge":
            edge = data.get("edge") or data
            return (f"「{edge.get('source')} → {edge.get('target')}」这条连线本来就没有。"
                    f"用 draft_inspect 看现有的连线。")
        if operation in ("add_test", "remove_test", "replace_tests"):
            return ("测试集合和你提交的一模一样，没有变化。"
                    "要加新用例就换个名字/输入；要改已有的先看 test_list。")
        return (f"这次 {operation} 提交的内容和现在完全一样，所以什么都没变。"
                f"先用 draft_inspect 看当前状态，再决定改哪里。")

    def _apply_to_snapshot(
        self,
        snapshot: ApplicationSnapshot,
        operation: str,
        data: dict[str, Any],
    ) -> None:

        if operation == "add_node":
            node = NodeSpec.model_validate(self._sink_config_keys(data["node"]))
            if any(item.id == node.id for item in snapshot.workflow.nodes):
                raise ValueError(f"node already exists: {node.id}")
            self.blocks.validate_node(node)
            snapshot.workflow.nodes.append(node)
        elif operation == "update_node":
            changes = dict(data.get("changes") or {})
            # Models sometimes nest merge_config inside changes; the intent is
            # unambiguous, so hoist it instead of failing NodeSpec validation.
            misplaced_merge = changes.pop("merge_config", None)
            if "merge_config" in data:
                merge_config = bool(data["merge_config"])
            elif misplaced_merge is not None:
                merge_config = bool(misplaced_merge)
            else:
                merge_config = True
            node_path = self._unique_node_path(
                snapshot.workflow,
                str(data["node_id"]),
            )
            self._update_node_at_path(
                snapshot.workflow,
                node_path,
                changes,
                merge_config=merge_config,
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
                raise ValueError(
                    f"edge already exists: {edge.id}" + _wiring_hint(snapshot)
                )
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
                raise ValueError(
                    "workflow edge already connects the same ports"
                    + _wiring_hint(snapshot)
                )
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
            test = WorkflowTestCase.model_validate(self._test_payload(data))
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
        # 自引用是结构性错误，必须在校验期拒绝而不是运行期崩。实测 32B 在
        # variable_assigner 里加了个引用自身产出的 output 赋值，draft_validate
        # 全绿、发布前才在 test_run 里炸——这类"能过结构校验的死图"正是
        # 静默失败的温床。
        def _self_references(payload: Any, owner: str) -> list[list[str]]:
            found: list[list[str]] = []
            if isinstance(payload, dict):
                reference = payload.get("$ref")
                if isinstance(reference, dict) and reference.get("node_id") == owner:
                    found.append([str(item) for item in (reference.get("path") or [])])
                for item in payload.values():
                    found.extend(_self_references(item, owner))
            elif isinstance(payload, list):
                for item in payload:
                    found.extend(_self_references(item, owner))
            return found

        for node in snapshot.workflow.nodes:
            self_refs = _self_references(node.config, node.id)
            if self_refs:
                errors.append(
                    f"{node.id}: 节点引用了它自己（path={self_refs[:3]}）——"
                    "节点不能读取自身的产出。要么改引用上游节点，要么把该值"
                    "直接算在本节点的表达式里。"
                )

        # 自引用是结构性错误，必须在校验期拒绝而不是运行期崩。实测 32B 在
        # variable_assigner 里加了个引用自身产出的 output 赋值，draft_validate
        # 全绿、发布前才在 test_run 里炸——这类"能过结构校验的死图"正是
        # 静默失败的温床。
        def _self_references(payload: Any, owner: str) -> list[list[str]]:
            found: list[list[str]] = []
            if isinstance(payload, dict):
                reference = payload.get("$ref")
                if isinstance(reference, dict) and reference.get("node_id") == owner:
                    found.append([str(item) for item in (reference.get("path") or [])])
                for item in payload.values():
                    found.extend(_self_references(item, owner))
            elif isinstance(payload, list):
                for item in payload:
                    found.extend(_self_references(item, owner))
            return found

        for node in snapshot.workflow.nodes:
            self_refs = _self_references(node.config, node.id)
            if self_refs:
                errors.append(
                    f"{node.id}: 节点引用了它自己（path={self_refs[:3]}）——"
                    "节点不能读取自身的产出。要么改引用上游节点，要么把该值"
                    "直接算在本节点的表达式里。"
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
    def _node_id_hint(node_ids: list[str], wanted: str) -> str:
        """拒绝即教学：node not found 必须说出现有 id。

        实测（2026-08-23 日报基准）：协调者反复对不存在的 'start' 做
        remove/connect，每次只被告知"node not found"，于是删了又加空转到
        200 次工具预算耗尽。列出现有 id 后它至少知道该用哪个名字。
        """

        import difflib

        hint = f"；现有节点 id：{node_ids[:12]}" if node_ids else "；当前草稿还没有任何节点"
        close = difflib.get_close_matches(wanted, node_ids, n=2, cutoff=0.5)
        if close:
            hint += f"；最接近的是 {close}"
        return hint

    @staticmethod
    def _node(snapshot: ApplicationSnapshot, node_id: str) -> NodeSpec:
        try:
            return next(node for node in snapshot.workflow.nodes if node.id == node_id)
        except StopIteration as error:
            raise KeyError(
                f"node not found: {node_id}"
                + ApplicationService._node_id_hint(
                    [node.id for node in snapshot.workflow.nodes], node_id
                )
            ) from error

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
            raise KeyError(
                f"node not found: {node_id}"
                + cls._node_id_hint([node.id for node in workflow.nodes], node_id)
            )
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
