from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .workflow_models import ApplicationSnapshot

PatchIntent = Literal[
    "multi_operation_edit",
    "rename_node",
    "update_node_description",
    "remove_disconnected_node",
    "update_workflow_metadata",
    "update_workflow_requirement",
    "update_start_inputs",
    "upsert_template_transform",
    "unsupported",
]


class DraftPatchPreviewRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    reference_node_ids: list[str] = Field(default_factory=list, max_length=50)


class DraftPatchPreviewResponse(BaseModel):
    supported: bool
    intent: PatchIntent
    message: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reference_node_ids: list[str] = Field(default_factory=list)


class DraftPatchPreviewer:
    """Deterministic natural-language draft patch preview.

    This intentionally does not call a model and never mutates the draft.
    """

    def preview(
        self,
        snapshot: ApplicationSnapshot,
        revision: int,
        instruction: str,
        reference_node_ids: list[str] | None = None,
    ) -> DraftPatchPreviewResponse:
        text = instruction.strip()
        references = self._valid_reference_node_ids(snapshot, reference_node_ids or [])

        multi_operation = self._multi_operation_preview(snapshot, revision, text)
        if multi_operation:
            return self._with_references(multi_operation, references)

        metadata = self._workflow_metadata_preview(revision, text)
        if metadata:
            return self._with_references(metadata, references)

        start_input = self._start_input_preview(snapshot, revision, text)
        if start_input:
            return self._with_references(start_input, references)

        transform = self._template_transform_preview(snapshot, revision, text)
        if transform:
            return self._with_references(transform, references)

        rename = re.search(
            r"(?:rename|重命名)\s+(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)\s+(?:to|为|成)\s+[\"'“”‘’]?(?P<title>[^\"'“”‘’]+)[\"'“”‘’]?",
            text,
            flags=re.IGNORECASE,
        )
        if rename:
            node_id = rename.group("node")
            title = rename.group("title").strip()
            return self._with_references(
                self._update_node(snapshot, revision, node_id, {"title": title}, "rename_node"),
                references,
            )

        description = re.search(
            r"(?:describe|描述)\s+(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)\s+(?:as|为|成)\s+[\"'“”‘’]?(?P<description>[^\"'“”‘’]+)[\"'“”‘’]?",
            text,
            flags=re.IGNORECASE,
        )
        if description:
            node_id = description.group("node")
            value = description.group("description").strip()
            return self._with_references(
                self._update_node(
                    snapshot, revision, node_id, {"description": value}, "update_node_description"
                ),
                references,
            )

        remove = re.search(
            r"(?:remove|删除)\s+(?:disconnected\s+)?(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)",
            text,
            flags=re.IGNORECASE,
        )
        if remove:
            node_id = remove.group("node")
            connected = {
                endpoint
                for edge in snapshot.workflow.edges
                for endpoint in (edge.source, edge.target)
            }
            if node_id in connected:
                return DraftPatchPreviewResponse(
                    supported=False,
                    intent="remove_disconnected_node",
                    message=f"node {node_id} is connected; destructive removal requires explicit design.",
                    warnings=["Only disconnected node removal is previewed by this deterministic parser."],
                    reference_node_ids=references,
                )
            if not any(node.id == node_id for node in snapshot.workflow.nodes):
                return self._with_references(self._missing_node(node_id), references)
            return self._with_references(
                DraftPatchPreviewResponse(
                    supported=True,
                    intent="remove_disconnected_node",
                    message=f"Preview remove disconnected node {node_id}.",
                    operations=[{
                        "expected_revision": revision,
                        "op": "remove_node",
                        "data": {"node_id": node_id},
                    }],
                ),
                references,
            )

        if self._looks_like_workflow_scope(text):
            return self._with_references(
                DraftPatchPreviewResponse(
                    supported=True,
                    intent="update_workflow_requirement",
                    message="Preview whole-workflow requirement update from this natural-language edit.",
                    operations=[{
                        "expected_revision": revision,
                        "op": "set_metadata",
                        "data": {
                            "requirement": text,
                            "description": text[:180],
                        },
                    }],
                    warnings=[
                        "This deterministic preview records the workflow-level edit request; run the builder team for architecture-wide regeneration.",
                    ],
                ),
                references,
            )

        return DraftPatchPreviewResponse(
            supported=True,
            intent="update_workflow_requirement",
            message="Preview whole-workflow requirement update. This instruction is saved as the workflow edit request instead of being rejected.",
            operations=[{
                "expected_revision": revision,
                "op": "set_metadata",
                "data": {
                    "requirement": text,
                    "description": text[:180],
                },
            }],
            warnings=[
                "No deterministic structural transform matched this instruction; the workflow-level request remains applicable and can be used for a later builder-team expansion.",
            ],
            reference_node_ids=references,
        )

    def _multi_operation_preview(
        self,
        snapshot: ApplicationSnapshot,
        revision: int,
        text: str,
    ) -> DraftPatchPreviewResponse | None:
        operations: list[dict[str, Any]] = []
        messages: list[str] = []
        rename = re.search(
            r"(?:把|将)\s*[\"'“‘]?(?P<node>[^\"'“”‘’]+?)[\"'”’]?\s*(?:积木|节点|node)?\s*(?:的)?\s*(?:标题|名称|title|name)\s*(?:改为|修改为|更新为|设置为|改成|to|as)\s*[\"'“‘]?(?P<value>[^\"'“”‘’，,；;。\n]+)[\"'”’]?",
            text,
            flags=re.IGNORECASE,
        )
        if rename:
            node = self._node_by_reference(snapshot, rename.group("node"))
            if not node:
                return None
            title = rename.group("value").strip()
            operations.append({
                "expected_revision": revision,
                "op": "update_node",
                "data": {
                    "node_id": node.id,
                    "changes": {"title": title},
                    "merge_config": True,
                },
            })
            messages.append(f"rename node {node.id} to {title}")

        description = re.search(
            r"(?:把|将)?\s*(?:工作流|流程|workflow)(?:的)?\s*(?:描述|说明|description)\s*(?:更新|修改|改|设置)?(?:为|成|to|as)\s*[\"'“‘]?(?P<value>[^\"'“”‘’\n]+)[\"'”’]?",
            text,
            flags=re.IGNORECASE,
        )
        if description:
            value = description.group("value").strip().rstrip("。.;；")
            operations.append({
                "expected_revision": revision,
                "op": "set_metadata",
                "data": {"description": value},
            })
            messages.append("update workflow description")

        if not operations:
            return None
        return DraftPatchPreviewResponse(
            supported=True,
            intent="multi_operation_edit",
            message="Preview precise workflow edits: " + "; ".join(messages) + ".",
            operations=operations,
        )

    def _workflow_metadata_preview(
        self, revision: int, text: str
    ) -> DraftPatchPreviewResponse | None:
        patterns: list[tuple[PatchIntent, str, str]] = [
            (
                "update_workflow_metadata",
                "name",
                r"(?:rename|name|重命名|命名|名称)\s*(?:workflow|工作流|应用)?\s*(?:to|as|为|成|改成|设置为)\s+[\"'“”‘’]?(?P<value>[^\"'“”‘’]+)[\"'“”‘’]?",
            ),
            (
                "update_workflow_metadata",
                "description",
                r"(?:describe|description|说明|描述|介绍)\s*(?:workflow|工作流|应用)?\s*(?:as|to|为|成|改成|设置为)\s+[\"'“”‘’]?(?P<value>[^\"'“”‘’]+)[\"'“”‘’]?",
            ),
            (
                "update_workflow_requirement",
                "requirement",
                r"(?:requirement|goal|需求|目标)\s*(?:to|as|为|成|改成|设置为|更新为)?\s*[\"'“”‘’]?(?P<value>[^\"'“”‘’]+)[\"'“”‘’]?",
            ),
        ]
        for intent, field, pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group("value").strip()
            if not value:
                continue
            return DraftPatchPreviewResponse(
                supported=True,
                intent=intent,
                message=f"Preview workflow {field} update.",
                operations=[{
                    "expected_revision": revision,
                    "op": "set_metadata",
                    "data": {field: value},
                }],
            )
        return None

    def _start_input_preview(
        self, snapshot: ApplicationSnapshot, revision: int, text: str
    ) -> DraftPatchPreviewResponse | None:
        match = re.search(
            r"(?:add|添加|增加)\s+(?:start\s+)?(?:input|输入)\s+(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?:\s+(?:as|为|叫)\s+[\"'“”‘’]?(?P<label>[^\"'“”‘’]+)[\"'“”‘’]?)?",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        start = next((node for node in snapshot.workflow.nodes if node.type == "start"), None)
        if not start:
            return DraftPatchPreviewResponse(
                supported=False,
                intent="update_start_inputs",
                message="no start node found for input update",
            )
        name = match.group("name").strip()
        label = (match.group("label") or name).strip()
        current_inputs = [
            item for item in start.config.get("inputs", [])
            if isinstance(item, dict)
        ]
        if any(str(item.get("name")) == name for item in current_inputs):
            return DraftPatchPreviewResponse(
                supported=False,
                intent="update_start_inputs",
                message=f"input already exists: {name}",
            )
        next_inputs = [*current_inputs, {"name": name, "label": label, "type": "string", "required": False}]
        return DraftPatchPreviewResponse(
            supported=True,
            intent="update_start_inputs",
            message=f"Preview add workflow input {name}.",
            operations=[{
                "expected_revision": revision,
                "op": "update_node",
                "data": {"node_id": start.id, "changes": {"config": {"inputs": next_inputs}}, "merge_config": True},
            }],
        )

    def _template_transform_preview(
        self, snapshot: ApplicationSnapshot, revision: int, text: str
    ) -> DraftPatchPreviewResponse | None:
        if not self._looks_like_template_transform_request(text):
            return None
        terminal = self._terminal_node(snapshot)
        if not terminal:
            return DraftPatchPreviewResponse(
                supported=False,
                intent="upsert_template_transform",
                message="no end or answer node found for transform insertion",
            )
        existing = self._existing_terminal_transform(snapshot, terminal.id)
        if existing:
            existing_upstream = next((edge.source for edge in snapshot.workflow.edges if edge.target == existing.id), None)
            return DraftPatchPreviewResponse(
                supported=True,
                intent="upsert_template_transform",
                message=f"Preview replace existing result transform {existing.id}.",
                operations=[{
                    "expected_revision": revision,
                    "op": "update_node",
                    "data": {
                        "node_id": existing.id,
                        "changes": {
                            "title": self._transform_title(text),
                            "description": "Formats the workflow result from a natural-language workflow edit.",
                            "config": self._transform_config(text, existing_upstream, self._source_output_path(snapshot, existing_upstream)),
                        },
                        "merge_config": False,
                    },
                }],
            )

        upstream_edge = next((edge for edge in snapshot.workflow.edges if edge.target == terminal.id), None)
        upstream_id = upstream_edge.source if upstream_edge else self._last_non_terminal_node_id(snapshot, terminal.id)
        transform_id = self._unique_node_id(snapshot, "workflow_edit_transform")
        operations: list[dict[str, Any]] = [{
            "expected_revision": revision,
            "op": "add_node",
            "data": {"node": {
                "id": transform_id,
                "type": "template_transform",
                "block_version": 1,
                "title": self._transform_title(text),
                "description": "Formats the workflow result from a natural-language workflow edit.",
                "config": self._transform_config(text, upstream_id, self._source_output_path(snapshot, upstream_id)),
                "position": self._insert_position(snapshot, terminal.id),
                "retry": {"enabled": False, "max_attempts": 1, "delay_seconds": 0.5},
                "error_strategy": "fail",
            }},
        }]
        if upstream_edge:
            operations.append({
                "expected_revision": revision,
                "op": "remove_edge",
                "data": {"edge_id": upstream_edge.id},
            })
        if upstream_id:
            operations.append({
                "expected_revision": revision,
                "op": "add_edge",
                "data": {"edge": {
                    "id": self._unique_edge_id(snapshot, f"{upstream_id}-{transform_id}"),
                    "source": upstream_id,
                    "target": transform_id,
                    "source_port": "output",
                    "target_port": "input",
                }},
            })
        operations.append({
            "expected_revision": revision,
            "op": "add_edge",
            "data": {"edge": {
                "id": self._unique_edge_id(snapshot, f"{transform_id}-{terminal.id}"),
                "source": transform_id,
                "target": terminal.id,
                "source_port": "text",
                "target_port": "input",
            }},
        })
        terminal_config = self._terminal_config_after_transform(terminal, transform_id)
        if terminal_config is not None:
            operations.append({
                "expected_revision": revision,
                "op": "update_node",
                "data": {
                    "node_id": terminal.id,
                    "changes": {"config": terminal_config},
                    "merge_config": False,
                },
            })
        return DraftPatchPreviewResponse(
            supported=True,
            intent="upsert_template_transform",
            message=f"Preview insert result transform before {terminal.id}.",
            operations=operations,
        )

    def _update_node(
        self,
        snapshot: ApplicationSnapshot,
        revision: int,
        node_id: str,
        changes: dict[str, Any],
        intent: PatchIntent,
    ) -> DraftPatchPreviewResponse:
        if not any(node.id == node_id for node in snapshot.workflow.nodes):
            return self._missing_node(node_id)
        return DraftPatchPreviewResponse(
            supported=True,
            intent=intent,
            message=f"Preview {intent} for node {node_id}.",
            operations=[{
                "expected_revision": revision,
                "op": "update_node",
                "data": {"node_id": node_id, "changes": changes, "merge_config": True},
            }],
        )

    @staticmethod
    def _node_by_reference(snapshot: ApplicationSnapshot, reference: str) -> Any | None:
        value = reference.strip()
        exact_id = next((node for node in snapshot.workflow.nodes if node.id == value), None)
        if exact_id:
            return exact_id
        folded = value.casefold()
        return next(
            (node for node in snapshot.workflow.nodes if node.title.strip().casefold() == folded),
            None,
        )

    @staticmethod
    def _missing_node(node_id: str) -> DraftPatchPreviewResponse:
        return DraftPatchPreviewResponse(
            supported=False,
            intent="unsupported",
            message=f"node not found: {node_id}",
        )

    @staticmethod
    def _valid_reference_node_ids(
        snapshot: ApplicationSnapshot, reference_node_ids: list[str]
    ) -> list[str]:
        known = {node.id for node in snapshot.workflow.nodes}
        seen: set[str] = set()
        valid: list[str] = []
        for node_id in reference_node_ids:
            value = str(node_id)
            if value in known and value not in seen:
                seen.add(value)
                valid.append(value)
        return valid

    @staticmethod
    def _with_references(
        response: DraftPatchPreviewResponse, reference_node_ids: list[str]
    ) -> DraftPatchPreviewResponse:
        response.reference_node_ids = reference_node_ids
        if reference_node_ids:
            response.warnings.append(
                "Referenced bricks are context only; workflow edit scope remains whole-workflow."
            )
        return response

    @staticmethod
    def _looks_like_workflow_scope(text: str) -> bool:
        lowered = text.casefold()
        return len(text) >= 20 and any(marker in lowered for marker in ("workflow", "工作流", "流程"))

    @staticmethod
    def _looks_like_template_transform_request(text: str) -> bool:
        lowered = text.casefold()
        structural = any(marker in lowered for marker in (
            "template", "transform", "format", "summary", "summarize", "output",
            "模板", "转换", "格式", "总结", "摘要", "输出", "结果",
        ))
        action = any(marker in lowered for marker in (
            "add", "insert", "replace", "change", "update", "make", "return",
            "添加", "增加", "插入", "替换", "改", "修改", "生成", "返回", "整理",
        ))
        return structural and action

    @staticmethod
    def _terminal_node(snapshot: ApplicationSnapshot) -> Any | None:
        return next((node for node in snapshot.workflow.nodes if node.type in {"end", "answer"}), None)

    @staticmethod
    def _last_non_terminal_node_id(snapshot: ApplicationSnapshot, terminal_id: str) -> str | None:
        for node in reversed(snapshot.workflow.nodes):
            if node.id != terminal_id:
                return node.id
        return None

    @staticmethod
    def _existing_terminal_transform(snapshot: ApplicationSnapshot, terminal_id: str) -> Any | None:
        incoming_sources = [edge.source for edge in snapshot.workflow.edges if edge.target == terminal_id]
        transforms = [node for node in snapshot.workflow.nodes if node.type == "template_transform"]
        return next((node for node in transforms if node.id in incoming_sources), None)

    @staticmethod
    def _transform_title(text: str) -> str:
        lowered = text.casefold()
        if any(marker in lowered for marker in ("日语", "japanese")):
            return "Daily Japanese Summary"
        if any(marker in lowered for marker in ("总结", "summary", "summarize")):
            return "Result Summary"
        return "Result Formatter"

    @staticmethod
    def _transform_config(text: str, source_node_id: str | None, source_path: str = "output") -> dict[str, Any]:
        value_ref: Any = {"$ref": {"node_id": source_node_id, "path": [source_path]}} if source_node_id else ""
        return {
            "template": (
                "Workflow edit result\n\n"
                "Instruction: {{ instruction }}\n\n"
                "Upstream result:\n{{ value }}"
            ),
            "variables": {
                "instruction": text,
                "value": value_ref,
            },
        }

    @staticmethod
    def _terminal_config_after_transform(terminal: Any, transform_id: str) -> dict[str, Any] | None:
        ref = {"$ref": {"node_id": transform_id, "path": ["text"]}}
        if terminal.type == "answer":
            return {"answer": ref}
        if terminal.type == "end":
            return {"outputs": {"result": ref}}
        return None

    @staticmethod
    def _source_output_path(snapshot: ApplicationSnapshot, node_id: str | None) -> str:
        node = next((item for item in snapshot.workflow.nodes if item.id == node_id), None)
        if node and node.type in {"llm", "template_transform", "question_classifier"}:
            return "text"
        return "output"

    @staticmethod
    def _insert_position(snapshot: ApplicationSnapshot, terminal_id: str) -> dict[str, float]:
        terminal = next((node for node in snapshot.workflow.nodes if node.id == terminal_id), None)
        if not terminal:
            return {"x": 380, "y": 120}
        return {"x": max(90, terminal.position.x - 260), "y": terminal.position.y}

    @staticmethod
    def _unique_node_id(snapshot: ApplicationSnapshot, prefix: str) -> str:
        existing = {node.id for node in snapshot.workflow.nodes}
        if prefix not in existing:
            return prefix
        index = 2
        while f"{prefix}_{index}" in existing:
            index += 1
        return f"{prefix}_{index}"

    @staticmethod
    def _unique_edge_id(snapshot: ApplicationSnapshot, prefix: str) -> str:
        existing = {edge.id for edge in snapshot.workflow.edges}
        value = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix).strip("-") or "workflow-edit-edge"
        if value not in existing:
            return value
        index = 2
        while f"{value}-{index}" in existing:
            index += 1
        return f"{value}-{index}"
