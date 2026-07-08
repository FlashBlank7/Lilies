from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .workflow_models import ApplicationSnapshot


class DraftPatchPreviewRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)


class DraftPatchPreviewResponse(BaseModel):
    supported: bool
    intent: Literal["rename_node", "update_node_description", "remove_disconnected_node", "unsupported"]
    message: str
    operations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DraftPatchPreviewer:
    """Deterministic natural-language draft patch preview.

    This intentionally does not call a model and never mutates the draft.
    """

    def preview(
        self, snapshot: ApplicationSnapshot, revision: int, instruction: str
    ) -> DraftPatchPreviewResponse:
        text = instruction.strip()
        rename = re.search(
            r"(?:rename|重命名)\s+(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)\s+(?:to|为|成)\s+[\"'“”‘’]?(?P<title>[^\"'“”‘’]+)[\"'“”‘’]?",
            text,
            flags=re.IGNORECASE,
        )
        if rename:
            node_id = rename.group("node")
            title = rename.group("title").strip()
            return self._update_node(snapshot, revision, node_id, {"title": title}, "rename_node")

        description = re.search(
            r"(?:describe|描述)\s+(?:node\s+)?(?P<node>[A-Za-z0-9_-]+)\s+(?:as|为|成)\s+[\"'“”‘’]?(?P<description>[^\"'“”‘’]+)[\"'“”‘’]?",
            text,
            flags=re.IGNORECASE,
        )
        if description:
            node_id = description.group("node")
            value = description.group("description").strip()
            return self._update_node(
                snapshot, revision, node_id, {"description": value}, "update_node_description"
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
                )
            if not any(node.id == node_id for node in snapshot.workflow.nodes):
                return self._missing_node(node_id)
            return DraftPatchPreviewResponse(
                supported=True,
                intent="remove_disconnected_node",
                message=f"Preview remove disconnected node {node_id}.",
                operations=[{
                    "expected_revision": revision,
                    "op": "remove_node",
                    "data": {"node_id": node_id},
                }],
            )

        return DraftPatchPreviewResponse(
            supported=False,
            intent="unsupported",
            message="unsupported instruction; deterministic preview supports rename, description update, and disconnected node removal.",
        )

    def _update_node(
        self,
        snapshot: ApplicationSnapshot,
        revision: int,
        node_id: str,
        changes: dict[str, Any],
        intent: Literal["rename_node", "update_node_description"],
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

