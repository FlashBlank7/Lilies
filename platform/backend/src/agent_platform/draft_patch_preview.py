from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    node_ids: list[str] = Field(default_factory=list, max_length=50)
    edge_ids: list[str] = Field(default_factory=list, max_length=100)


class NaturalLanguageDraftEditRequest(BaseModel):
    """Strict request shared by preview and atomic natural-language apply."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2000)
    node_ids: list[str] = Field(default_factory=list, max_length=50)
    edge_ids: list[str] = Field(default_factory=list, max_length=100)
    expected_revision: int = Field(ge=0)
    expected_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=16, max_length=200)
    preview_only: bool = True
    preview_task_id: str | None = Field(default=None, min_length=1, max_length=200)
    expected_preview_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )

    @field_validator("node_ids", "edge_ids")
    @classmethod
    def normalize_selection_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip()
            if not value:
                raise ValueError("selection ids must not be empty")
            if len(value) > 200:
                raise ValueError("selection ids must be at most 200 characters")
            if value not in seen:
                seen.add(value)
                normalized.append(value)
        return normalized


class DraftPatchPreviewResponse(BaseModel):
    supported: bool
    intent: PatchIntent
    message: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reference_node_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)


class NaturalLanguageDraftEditResponse(DraftPatchPreviewResponse):
    task_id: str
    applied: bool
    expected_revision: int = Field(ge=0)
    expected_content_hash: str
    preview_source: Literal["deterministic", "model", "stored_preview"]
    preview_digest: str
    draft: dict[str, Any]
    evidence: dict[str, Any]


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
        node_ids: list[str] | None = None,
        edge_ids: list[str] | None = None,
    ) -> DraftPatchPreviewResponse:
        text = instruction.strip()
        references = self._valid_reference_node_ids(snapshot, reference_node_ids or [])
        selected_nodes, selected_edges = self.validate_selection(
            snapshot,
            node_ids or [],
            edge_ids or [],
        )

        selected_removal = self._selected_removal_preview(
            revision,
            text,
            selected_nodes,
            selected_edges,
        )
        if selected_removal:
            return self._with_selection(
                selected_removal,
                references,
                selected_nodes,
                selected_edges,
            )

        selected_property = self._selected_node_property_preview(
            snapshot,
            revision,
            text,
            selected_nodes,
        )
        if selected_property:
            return self._with_selection(
                selected_property,
                references,
                selected_nodes,
                selected_edges,
            )

        multi_operation = self._multi_operation_preview(snapshot, revision, text)
        if multi_operation:
            return self._with_selection(
                multi_operation,
                references,
                selected_nodes,
                selected_edges,
            )

        metadata = self._workflow_metadata_preview(revision, text)
        if metadata:
            return self._with_selection(metadata, references, selected_nodes, selected_edges)

        start_input = self._start_input_preview(snapshot, revision, text)
        if start_input:
            return self._with_selection(start_input, references, selected_nodes, selected_edges)

        transform = self._template_transform_preview(snapshot, revision, text)
        if transform:
            return self._with_selection(transform, references, selected_nodes, selected_edges)

        rename = re.search(
            r"(?:rename|重命名)\s+(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)\s+(?:to|为|成)\s+[\"'“”‘’]?(?P<title>[^\"'“”‘’]+)[\"'“”‘’]?",
            text,
            flags=re.IGNORECASE,
        )
        if rename:
            node_id = rename.group("node")
            title = rename.group("title").strip()
            return self._with_selection(
                self._update_node(snapshot, revision, node_id, {"title": title}, "rename_node"),
                references,
                selected_nodes,
                selected_edges,
            )

        description = re.search(
            r"(?:describe|描述)\s+(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)\s+(?:as|为|成)\s+[\"'“”‘’]?(?P<description>[^\"'“”‘’]+)[\"'“”‘’]?",
            text,
            flags=re.IGNORECASE,
        )
        if description:
            node_id = description.group("node")
            value = description.group("description").strip()
            return self._with_selection(
                self._update_node(
                    snapshot, revision, node_id, {"description": value}, "update_node_description"
                ),
                references,
                selected_nodes,
                selected_edges,
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
                return self._with_selection(
                    self._missing_node(node_id),
                    references,
                    selected_nodes,
                    selected_edges,
                )
            return self._with_selection(
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
                selected_nodes,
                selected_edges,
            )

        if self._looks_like_workflow_scope(text):
            return self._with_selection(
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
                selected_nodes,
                selected_edges,
            )

        return self._with_selection(DraftPatchPreviewResponse(
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
        ), references, selected_nodes, selected_edges)

    @staticmethod
    def _selected_removal_preview(
        revision: int,
        text: str,
        selected_node_ids: list[str],
        selected_edge_ids: list[str],
    ) -> DraftPatchPreviewResponse | None:
        if not selected_node_ids and not selected_edge_ids:
            return None
        if not re.search(r"(?:delete|remove|删除|移除|去掉)", text, flags=re.IGNORECASE):
            return None
        if not re.search(
            r"(?:selected|selection|选中|所选|这些|这几个|它们|这个|该)",
            text,
            flags=re.IGNORECASE,
        ):
            return None
        mentions_nodes = bool(
            re.search(r"(?:node|brick|节点|积木)", text, flags=re.IGNORECASE)
        )
        mentions_edges = bool(
            re.search(r"(?:edge|connection|line|边|连线|连接)", text, flags=re.IGNORECASE)
        )
        if mentions_nodes and not mentions_edges:
            edge_targets: list[str] = []
            node_targets = selected_node_ids
        elif mentions_edges and not mentions_nodes:
            edge_targets = selected_edge_ids
            node_targets = []
        else:
            edge_targets = selected_edge_ids
            node_targets = selected_node_ids
        operations = [
            {
                "expected_revision": revision,
                "op": "remove_edge",
                "data": {"edge_id": edge_id},
            }
            for edge_id in edge_targets
        ]
        operations.extend(
            {
                "expected_revision": revision,
                "op": "remove_node",
                "data": {"node_id": node_id},
            }
            for node_id in node_targets
        )
        if not operations:
            return None
        return DraftPatchPreviewResponse(
            supported=True,
            intent="multi_operation_edit",
            message=(
                "Preview explicit removal of "
                f"{len(node_targets)} selected node(s) and "
                f"{len(edge_targets)} selected edge(s)."
            ),
            operations=operations,
            warnings=[
                "Removing a node also removes every edge incident to that node."
            ] if node_targets else [],
        )

    def _selected_node_property_preview(
        self,
        snapshot: ApplicationSnapshot,
        revision: int,
        text: str,
        selected_node_ids: list[str],
    ) -> DraftPatchPreviewResponse | None:
        if not selected_node_ids:
            return None
        selected_marker = re.search(
            r"(?:selected|selection|选中|所选|这些|这几个|它们|这个|该)",
            text,
            flags=re.IGNORECASE,
        )
        if not selected_marker:
            return None
        patterns: tuple[tuple[PatchIntent, str, str], ...] = (
            (
                "rename_node",
                "title",
                r"(?:标题|名称|title|name)\s*(?:改为|修改为|更新为|设置为|改成|to|as)\s*[：:]?\s*[\"'“‘]?(?P<value>[^\"'“”‘’，,；;。\n]+)",
            ),
            (
                "update_node_description",
                "description",
                r"(?:描述|说明|description)\s*(?:改为|修改为|更新为|设置为|改成|to|as)\s*[：:]?\s*[\"'“‘]?(?P<value>[^\"'“”‘’\n]+)",
            ),
        )
        for intent, field, pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group("value").strip().rstrip("。.;；")
            if not value:
                continue
            operations = [
                {
                    "expected_revision": revision,
                    "op": "update_node",
                    "data": {
                        "node_id": node_id,
                        "changes": {field: value},
                        "merge_config": True,
                    },
                }
                for node_id in selected_node_ids
            ]
            return DraftPatchPreviewResponse(
                supported=True,
                intent=intent if len(operations) == 1 else "multi_operation_edit",
                message=(
                    f"Preview {field} update for "
                    f"{len(selected_node_ids)} selected node(s)."
                ),
                operations=operations,
            )
        return None

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
    def validate_selection(
        snapshot: ApplicationSnapshot,
        node_ids: list[str],
        edge_ids: list[str],
    ) -> tuple[list[str], list[str]]:
        known_nodes = {node.id for node in snapshot.workflow.nodes}
        known_edges = {edge.id for edge in snapshot.workflow.edges}
        selected_nodes = DraftPatchPreviewer._deduplicate_ids(node_ids)
        selected_edges = DraftPatchPreviewer._deduplicate_ids(edge_ids)
        unknown_nodes = [node_id for node_id in selected_nodes if node_id not in known_nodes]
        unknown_edges = [edge_id for edge_id in selected_edges if edge_id not in known_edges]
        if unknown_nodes:
            raise ValueError("selected node not found: " + ", ".join(unknown_nodes))
        if unknown_edges:
            raise ValueError("selected edge not found: " + ", ".join(unknown_edges))
        return selected_nodes, selected_edges

    @staticmethod
    def _deduplicate_ids(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for raw in values:
            value = str(raw).strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @staticmethod
    def _with_selection(
        response: DraftPatchPreviewResponse,
        reference_node_ids: list[str],
        node_ids: list[str],
        edge_ids: list[str],
    ) -> DraftPatchPreviewResponse:
        response.reference_node_ids = reference_node_ids
        response.node_ids = node_ids
        response.edge_ids = edge_ids
        if node_ids or edge_ids:
            response.warnings.append(
                "Selected nodes and edges are the primary edit target; unselected structure must be preserved except for explicit connection-closure changes."
            )
        elif reference_node_ids:
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
        if re.search(
            r"(?:节点|积木|node).*(?:标题|名称|描述|说明|title|name|description)",
            lowered,
        ):
            return False
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


def validate_selection_operations(
    snapshot: ApplicationSnapshot,
    operations: list[dict[str, Any]],
    *,
    node_ids: list[str],
    edge_ids: list[str],
) -> list[str]:
    """Keep a boxed natural-language edit focused without breaking graph closure.

    Selected items are the primary target, not an isolated mini-workflow.  A
    planner may add nodes and reconnect the immediate boundary, but it may not
    use a selection as authority to rewrite unrelated nodes or workflow-wide
    metadata.
    """

    _validate_workflow_edit_reference_integrity(snapshot, operations)
    if not node_ids and not edge_ids:
        return []
    selected_nodes = set(node_ids)
    selected_edges = set(edge_ids)
    existing_nodes = {node.id for node in snapshot.workflow.nodes}
    existing_edges = {edge.id: edge for edge in snapshot.workflow.edges}
    added_nodes = {
        str(operation.get("data", {}).get("node", {}).get("id"))
        for operation in operations
        if operation.get("op") == "add_node"
        and isinstance(operation.get("data"), dict)
        and isinstance(operation["data"].get("node"), dict)
        and operation["data"]["node"].get("id")
    }
    selected_edge_endpoints = {
        endpoint
        for edge_id in selected_edges
        if (edge := existing_edges.get(edge_id)) is not None
        for endpoint in (edge.source, edge.target)
    }
    selection_scope_nodes = selected_nodes | selected_edge_endpoints
    component_by_scope_node = _selection_component_ids(
        snapshot,
        selected_nodes=selected_nodes,
        selected_edges=selected_edges,
        selection_scope_nodes=selection_scope_nodes,
    )
    upstream_components, consumer_components = _selection_boundary_components(
        snapshot,
        component_by_scope_node=component_by_scope_node,
    )
    allowed_boundary_upstreams = _disrupted_selected_boundary_upstreams(
        snapshot,
        operations,
        component_by_selected_node=component_by_scope_node,
    )
    upstream_disrupted, consumer_disrupted = _selection_boundary_disruptions(
        snapshot,
        operations,
        component_by_scope_node=component_by_scope_node,
        upstream_components=upstream_components,
        consumer_components=consumer_components,
    )
    planned_connections = [
        (
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
        )
        for operation in operations
        if operation.get("op") == "add_edge"
        and isinstance(operation.get("data"), dict)
        and isinstance((edge := operation["data"].get("edge")), dict)
    ]
    component_by_added_node = _selection_added_node_components(
        added_nodes=added_nodes,
        planned_connections=planned_connections,
        component_by_scope_node=component_by_scope_node,
        upstream_components=upstream_components,
        consumer_components=consumer_components,
    )
    component_by_edit_node = {
        **component_by_scope_node,
        **component_by_added_node,
    }
    removed_selected_edge_components = {
        component_by_scope_node[edge.source]
        for operation in operations
        if operation.get("op") == "remove_edge"
        and isinstance(operation.get("data"), dict)
        and (edge := existing_edges.get(str(operation["data"].get("edge_id") or "")))
        is not None
        and edge.id in selected_edges
        and edge.source in component_by_scope_node
    }

    warnings: list[str] = []
    for index, operation in enumerate(operations):
        op = str(operation.get("op") or "")
        data = operation.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"selection edit operation {index} data must be an object")
        if op == "update_node":
            node_id = str(data.get("node_id") or "")
            changes = data.get("changes")
            if node_id in selected_nodes or node_id in added_nodes:
                continue
            reference_targets = (
                _connection_reference_targets(changes.get("config"))
                if isinstance(changes, dict)
                and set(changes).issubset({"config"})
                else None
            )
            if (
                reference_targets
                and _selection_reference_repair_is_allowed(
                    consumer_id=node_id,
                    reference_targets=reference_targets,
                    component_by_edit_node=component_by_edit_node,
                    consumer_components=consumer_components,
                    consumer_disrupted=consumer_disrupted,
                    allowed_boundary_upstreams=allowed_boundary_upstreams,
                )
            ):
                warnings.append(
                    f"Selection closure updates adjacent node {node_id} configuration."
                )
                continue
            raise ValueError(
                f"selection edit attempted to update unselected node: {node_id}"
            )
        if op == "remove_node":
            node_id = str(data.get("node_id") or "")
            if node_id not in selected_nodes:
                raise ValueError(
                    f"selection edit attempted to remove unselected node: {node_id}"
                )
            continue
        if op == "remove_edge":
            edge_id = str(data.get("edge_id") or "")
            edge = existing_edges.get(edge_id)
            if edge_id in selected_edges:
                continue
            if edge and ({edge.source, edge.target} & selected_nodes):
                warnings.append(
                    f"Selection closure removes incident edge {edge_id}."
                )
                continue
            raise ValueError(
                f"selection edit attempted to remove unselected edge: {edge_id}"
            )
        if op == "add_node":
            continue
        if op == "add_edge":
            edge = data.get("edge")
            if not isinstance(edge, dict):
                raise ValueError(f"selection edit operation {index} edge must be an object")
            source = str(edge.get("source") or "")
            target = str(edge.get("target") or "")
            if source not in existing_nodes | added_nodes or target not in existing_nodes | added_nodes:
                raise ValueError("selection edit attempted to connect an unknown node")
            source_component = component_by_edit_node.get(source)
            target_component = component_by_edit_node.get(target)
            boundary_splice = (
                source_component is None
                and target_component is None
                and source in allowed_boundary_upstreams.get(target, set())
            )
            internal_connection = (
                source_component is not None
                and source_component == target_component
                and (
                    bool({source, target} & (selected_nodes | added_nodes))
                    or source_component in removed_selected_edge_components
                )
            )
            upstream_repair = (
                source_component is None
                and target_component is not None
                and target_component in upstream_components.get(source, set())
                and upstream_disrupted.get((source, target_component), False)
            )
            consumer_repair = (
                source_component is not None
                and target_component is None
                and source_component in consumer_components.get(target, set())
                and consumer_disrupted.get((source_component, target), False)
            )
            if not (
                boundary_splice
                or internal_connection
                or upstream_repair
                or consumer_repair
            ):
                raise ValueError(
                    "selection edit attempted to connect unrelated unselected nodes"
                )
            if boundary_splice:
                warnings.append(
                    f"Selection closure splices boundary edge {edge.get('id', '')}."
                )
            elif upstream_repair or consumer_repair:
                warnings.append(
                    f"Selection closure adds boundary edge {edge.get('id', '')}."
                )
            continue
        raise ValueError(
            f"selection edit cannot apply workflow-wide operation {op!r}; clear the selection for a whole-workflow edit"
        )
    return list(dict.fromkeys(warnings))


def _selection_component_ids(
    snapshot: ApplicationSnapshot,
    *,
    selected_nodes: set[str],
    selected_edges: set[str],
    selection_scope_nodes: set[str],
) -> dict[str, int]:
    """Bind every explicit node/edge endpoint to one original selection component."""

    adjacency = {node_id: set() for node_id in selection_scope_nodes}
    for edge in snapshot.workflow.edges:
        if (
            edge.source in selection_scope_nodes
            and edge.target in selection_scope_nodes
            and (
                edge.id in selected_edges
                or (
                    edge.source in selected_nodes
                    and edge.target in selected_nodes
                )
            )
        ):
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)

    component_by_node: dict[str, int] = {}
    component_id = 0
    for start in sorted(selection_scope_nodes):
        if start in component_by_node:
            continue
        pending = [start]
        component_by_node[start] = component_id
        while pending:
            source = pending.pop()
            for target in adjacency[source]:
                if target in component_by_node:
                    continue
                component_by_node[target] = component_id
                pending.append(target)
        component_id += 1
    return component_by_node


def _selection_boundary_components(
    snapshot: ApplicationSnapshot,
    *,
    component_by_scope_node: dict[str, int],
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Return original upstream and consumer membership per selection component."""

    upstream_components: dict[str, set[int]] = {}
    consumer_components: dict[str, set[int]] = {}
    for edge in snapshot.workflow.edges:
        source_component = component_by_scope_node.get(edge.source)
        target_component = component_by_scope_node.get(edge.target)
        if source_component is None and target_component is not None:
            upstream_components.setdefault(edge.source, set()).add(
                target_component
            )
        if source_component is not None and target_component is None:
            consumer_components.setdefault(edge.target, set()).add(
                source_component
            )
    return upstream_components, consumer_components


def _selection_boundary_disruptions(
    snapshot: ApplicationSnapshot,
    operations: list[dict[str, Any]],
    *,
    component_by_scope_node: dict[str, int],
    upstream_components: dict[str, set[int]],
    consumer_components: dict[str, set[int]],
) -> tuple[dict[tuple[str, int], bool], dict[tuple[int, str], bool]]:
    """Record boundaries whose original selected-component connection is gone."""

    removed_nodes = {
        str(operation.get("data", {}).get("node_id") or "")
        for operation in operations
        if operation.get("op") == "remove_node"
        and isinstance(operation.get("data"), dict)
    }
    removed_edge_ids = {
        str(operation.get("data", {}).get("edge_id") or "")
        for operation in operations
        if operation.get("op") == "remove_edge"
        and isinstance(operation.get("data"), dict)
    }

    def survives(edge: Any) -> bool:
        return (
            edge.id not in removed_edge_ids
            and edge.source not in removed_nodes
            and edge.target not in removed_nodes
        )

    upstream_disrupted: dict[tuple[str, int], bool] = {}
    for source, component_ids in upstream_components.items():
        for component_id in component_ids:
            boundary_edges = [
                edge
                for edge in snapshot.workflow.edges
                if edge.source == source
                and component_by_scope_node.get(edge.target) == component_id
            ]
            upstream_disrupted[(source, component_id)] = bool(boundary_edges) and not any(
                survives(edge) for edge in boundary_edges
            )

    consumer_disrupted: dict[tuple[int, str], bool] = {}
    for target, component_ids in consumer_components.items():
        for component_id in component_ids:
            boundary_edges = [
                edge
                for edge in snapshot.workflow.edges
                if edge.target == target
                and component_by_scope_node.get(edge.source) == component_id
            ]
            consumer_disrupted[(component_id, target)] = bool(boundary_edges) and not any(
                survives(edge) for edge in boundary_edges
            )
    return upstream_disrupted, consumer_disrupted


def _selection_added_node_components(
    *,
    added_nodes: set[str],
    planned_connections: list[tuple[str, str]],
    component_by_scope_node: dict[str, int],
    upstream_components: dict[str, set[int]],
    consumer_components: dict[str, set[int]],
) -> dict[str, int]:
    """Bind each new-node island to exactly one original selection component."""

    if not added_nodes:
        return {}
    editable_nodes = set(component_by_scope_node) | added_nodes
    adjacency = {node_id: set() for node_id in editable_nodes}
    boundary_constraints: dict[str, list[set[int]]] = {
        node_id: [] for node_id in editable_nodes
    }
    for source, target in planned_connections:
        if source in editable_nodes and target in editable_nodes:
            adjacency[source].add(target)
            adjacency[target].add(source)
            continue
        if source not in editable_nodes and target in editable_nodes:
            boundary_constraints[target].append(
                set(upstream_components.get(source, set()))
            )
        elif source in editable_nodes and target not in editable_nodes:
            boundary_constraints[source].append(
                set(consumer_components.get(target, set()))
            )

    result: dict[str, int] = {}
    visited: set[str] = set()
    for start in sorted(editable_nodes):
        if start in visited:
            continue
        members: set[str] = set()
        pending = [start]
        visited.add(start)
        while pending:
            source = pending.pop()
            members.add(source)
            for target in adjacency[source]:
                if target in visited:
                    continue
                visited.add(target)
                pending.append(target)
        group_added = members & added_nodes
        if not group_added:
            continue
        fixed_components = {
            component_by_scope_node[node_id]
            for node_id in members
            if node_id in component_by_scope_node
        }
        constraints = [
            allowed
            for node_id in members
            for allowed in boundary_constraints[node_id]
        ]
        if len(fixed_components) > 1 or any(not allowed for allowed in constraints):
            raise ValueError(
                "selection edit attempted to bridge unrelated selected components"
            )
        possible = set(fixed_components)
        if not possible:
            possible = set.intersection(*constraints) if constraints else set()
        else:
            for allowed in constraints:
                possible &= allowed
            if not possible:
                raise ValueError(
                    "selection edit attempted to bridge unrelated selected components"
                )
        if len(possible) != 1:
            raise ValueError(
                "selection edit added a node without one unambiguous selection component"
            )
        component_id = next(iter(possible))
        for node_id in group_added:
            result[node_id] = component_id
    return result


def _disrupted_selected_boundary_upstreams(
    snapshot: ApplicationSnapshot,
    operations: list[dict[str, Any]],
    *,
    component_by_selected_node: dict[str, int],
) -> dict[str, set[str]]:
    """Map each boundary consumer to upstreams whose selected route is removed.

    A boxed selection can contain disconnected branches.  Boundary closure must
    therefore preserve the original directed component relationship instead of
    treating every incoming source and outgoing consumer as interchangeable.
    A bypass is allowed only when the plan actually disrupts every original
    selected-node path for that source/consumer pair.
    """

    selected_nodes = set(component_by_selected_node)
    if not selected_nodes:
        return {}
    removed_nodes = {
        str(operation.get("data", {}).get("node_id") or "")
        for operation in operations
        if operation.get("op") == "remove_node"
        and isinstance(operation.get("data"), dict)
    }
    removed_edge_ids = {
        str(operation.get("data", {}).get("edge_id") or "")
        for operation in operations
        if operation.get("op") == "remove_edge"
        and isinstance(operation.get("data"), dict)
    }
    incoming_edges = [
        edge
        for edge in snapshot.workflow.edges
        if edge.target in selected_nodes
        and edge.source not in selected_nodes
    ]
    outgoing_edges = [
        edge
        for edge in snapshot.workflow.edges
        if edge.source in selected_nodes
        and edge.target not in selected_nodes
    ]
    original_adjacency = {node_id: set() for node_id in selected_nodes}
    surviving_adjacency = {
        node_id: set()
        for node_id in selected_nodes
        if node_id not in removed_nodes
    }
    for edge in snapshot.workflow.edges:
        if edge.source not in selected_nodes or edge.target not in selected_nodes:
            continue
        if (
            component_by_selected_node[edge.source]
            != component_by_selected_node[edge.target]
        ):
            continue
        original_adjacency[edge.source].add(edge.target)
        if (
            edge.id not in removed_edge_ids
            and edge.source in surviving_adjacency
            and edge.target in surviving_adjacency
        ):
            surviving_adjacency[edge.source].add(edge.target)

    def reachable(
        start: str,
        adjacency: dict[str, set[str]],
    ) -> set[str]:
        if start not in adjacency:
            return set()
        result = {start}
        pending = [start]
        while pending:
            source = pending.pop()
            for target in adjacency[source]:
                if target in result:
                    continue
                result.add(target)
                pending.append(target)
        return result

    incoming_by_source: dict[str, list[Any]] = {}
    for edge in incoming_edges:
        incoming_by_source.setdefault(edge.source, []).append(edge)
    outgoing_by_target: dict[str, list[Any]] = {}
    for edge in outgoing_edges:
        outgoing_by_target.setdefault(edge.target, []).append(edge)

    original_reachable = {
        edge.target: reachable(edge.target, original_adjacency)
        for edge in incoming_edges
    }
    surviving_reachable = {
        edge.target: reachable(edge.target, surviving_adjacency)
        for edge in incoming_edges
    }
    allowed: dict[str, set[str]] = {}
    for source, source_edges in incoming_by_source.items():
        for target, target_edges in outgoing_by_target.items():
            original_route_exists = any(
                component_by_selected_node[incoming.target]
                == component_by_selected_node[outgoing.source]
                and outgoing.source in original_reachable[incoming.target]
                for incoming in source_edges
                for outgoing in target_edges
            )
            if not original_route_exists:
                continue
            surviving_route_exists = any(
                incoming.id not in removed_edge_ids
                and incoming.source not in removed_nodes
                and incoming.target not in removed_nodes
                and outgoing.id not in removed_edge_ids
                and outgoing.source not in removed_nodes
                and outgoing.target not in removed_nodes
                and outgoing.source in surviving_reachable[incoming.target]
                for incoming in source_edges
                for outgoing in target_edges
            )
            if not surviving_route_exists:
                allowed.setdefault(target, set()).add(source)
    return allowed


def _connection_reference_targets(value: Any) -> set[str] | None:
    """Return reference sources only when every config leaf is a valid ref."""

    targets: set[str] = set()

    def visit(item: Any) -> bool:
        if isinstance(item, dict) and "$ref" in item:
            if set(item) != {"$ref"} or not isinstance(item["$ref"], dict):
                return False
            reference = item["$ref"]
            target = str(reference.get("node_id") or "")
            path = reference.get("path")
            if not target or not isinstance(path, list):
                return False
            targets.add(target)
            return all(isinstance(part, str) for part in path)
        if isinstance(item, dict):
            return bool(item) and all(visit(child) for child in item.values())
        if isinstance(item, list):
            return bool(item) and all(visit(child) for child in item)
        return False

    return targets if visit(value) and targets else None


def _selection_reference_repair_is_allowed(
    *,
    consumer_id: str,
    reference_targets: set[str],
    component_by_edit_node: dict[str, int],
    consumer_components: dict[str, set[int]],
    consumer_disrupted: dict[tuple[int, str], bool],
    allowed_boundary_upstreams: dict[str, set[str]],
) -> bool:
    """Require every adjacent reference repair to stay in one disrupted route."""

    valid_consumer_components = consumer_components.get(consumer_id, set())
    for target in reference_targets:
        component_id = component_by_edit_node.get(target)
        if component_id is None:
            if target not in allowed_boundary_upstreams.get(consumer_id, set()):
                return False
            continue
        if (
            component_id not in valid_consumer_components
            or not consumer_disrupted.get((component_id, consumer_id), False)
        ):
            return False
    return True


def _deep_merge_config(
    original: dict[str, Any],
    changes: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(original)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _workflow_reference_node_ids(value: Any) -> set[str]:
    result: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        if (
            isinstance(item.get("nodes"), list)
            and isinstance(item.get("edges"), list)
        ):
            # Nested iteration/loop workflows own a separate node-id namespace.
            return
        reference = item.get("$ref")
        if isinstance(reference, dict):
            node_id = reference.get("node_id")
            if isinstance(node_id, str) and node_id:
                result.add(node_id)
        for child in item.values():
            visit(child)

    visit(value)
    return result


def _validate_workflow_edit_reference_integrity(
    snapshot: ApplicationSnapshot,
    operations: list[dict[str, Any]],
) -> None:
    """Reject edits that leave data references outside the final graph.

    Existing legacy references without a direct edge remain tolerated until
    touched. New references must have a matching source-to-consumer edge, and
    removing such an edge requires removing or replacing the target config
    reference in the same atomic plan.
    """

    final_configs = {
        node.id: deepcopy(node.config)
        for node in snapshot.workflow.nodes
    }
    final_edges = {
        edge.id: (edge.source, edge.target)
        for edge in snapshot.workflow.edges
    }
    initial_node_ids = set(final_configs)
    initial_edge_pairs = set(final_edges.values())
    initial_reference_pairs = _workflow_reference_pairs(final_configs)

    for operation in operations:
        op = str(operation.get("op") or "")
        data = operation.get("data")
        if not isinstance(data, dict):
            continue
        if op == "add_node":
            node = data.get("node")
            if isinstance(node, dict):
                node_id = str(node.get("id") or "")
                config = node.get("config", {})
                if node_id and isinstance(config, dict):
                    final_configs[node_id] = deepcopy(config)
            continue
        if op == "update_node":
            node_id = str(data.get("node_id") or "")
            changes = data.get("changes")
            if not isinstance(changes, dict) or "config" not in changes:
                continue
            next_config = changes["config"]
            if not isinstance(next_config, dict):
                raise ValueError("workflow edit node config must be an object")
            if data.get("merge_config", True):
                final_configs[node_id] = _deep_merge_config(
                    final_configs.get(node_id, {}),
                    next_config,
                )
            else:
                final_configs[node_id] = deepcopy(next_config)
            continue
        if op == "remove_node":
            node_id = str(data.get("node_id") or "")
            final_configs.pop(node_id, None)
            final_edges = {
                edge_id: endpoints
                for edge_id, endpoints in final_edges.items()
                if node_id not in endpoints
            }
            continue
        if op == "add_edge":
            edge = data.get("edge")
            if isinstance(edge, dict):
                edge_id = str(edge.get("id") or "")
                source = str(edge.get("source") or "")
                target = str(edge.get("target") or "")
                if edge_id and source and target:
                    final_edges[edge_id] = (source, target)
            continue
        if op == "remove_edge":
            final_edges.pop(str(data.get("edge_id") or ""), None)
            continue
        if op == "replace_workflow":
            workflow = data.get("workflow")
            if not isinstance(workflow, dict):
                continue
            final_configs = {
                str(node.get("id")): deepcopy(node.get("config", {}))
                for node in workflow.get("nodes", [])
                if isinstance(node, dict)
                and node.get("id")
                and isinstance(node.get("config", {}), dict)
            }
            final_edges = {
                str(edge.get("id")): (
                    str(edge.get("source")),
                    str(edge.get("target")),
                )
                for edge in workflow.get("edges", [])
                if isinstance(edge, dict)
                and edge.get("id")
                and edge.get("source")
                and edge.get("target")
            }

    final_node_ids = set(final_configs)
    final_reference_pairs = _workflow_reference_pairs(final_configs)
    unknown_references = sorted(
        (consumer, source)
        for consumer, source in final_reference_pairs
        if source != "$inputs"
        and source not in final_node_ids
        and (
            (consumer, source) not in initial_reference_pairs
            or source in initial_node_ids
        )
    )
    if unknown_references:
        rendered = ", ".join(
            f"{consumer} -> {source}"
            for consumer, source in unknown_references
        )
        raise ValueError(
            "workflow edit would leave dangling workflow references: "
            + rendered
        )

    final_edge_pairs = set(final_edges.values())
    new_reference_pairs = final_reference_pairs - initial_reference_pairs
    previously_wired_references = (
        final_reference_pairs
        & initial_reference_pairs
        & {
            (target, source)
            for source, target in initial_edge_pairs
        }
    )
    required_reference_pairs = new_reference_pairs | previously_wired_references
    disconnected_references = sorted(
        (consumer, source)
        for consumer, source in required_reference_pairs
        if source != "$inputs"
        and (source, consumer) not in final_edge_pairs
    )
    if disconnected_references:
        rendered = ", ".join(
            f"{source} -> {consumer}"
            for consumer, source in disconnected_references
        )
        raise ValueError(
            "workflow edit would disconnect referenced workflow inputs: "
            + rendered
        )


def _workflow_reference_pairs(
    configs: dict[str, dict[str, Any]],
) -> set[tuple[str, str]]:
    return {
        (consumer_node_id, source_node_id)
        for consumer_node_id, config in configs.items()
        for source_node_id in _workflow_reference_node_ids(config)
    }
