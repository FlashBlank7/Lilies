from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .workflow_models import ApplicationSnapshot

PatchIntent = Literal[
    "rename_node",
    "update_node_description",
    "remove_disconnected_node",
    "update_workflow_metadata",
    "update_workflow_requirement",
    "update_start_inputs",
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

        metadata = self._workflow_metadata_preview(revision, text)
        if metadata:
            return self._with_references(metadata, references)

        start_input = self._start_input_preview(snapshot, revision, text)
        if start_input:
            return self._with_references(start_input, references)

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
            supported=False,
            intent="unsupported",
            message="unsupported instruction; workflow edit preview supports workflow name, description, requirement, start inputs, node title/description, and disconnected node removal.",
            reference_node_ids=references,
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
