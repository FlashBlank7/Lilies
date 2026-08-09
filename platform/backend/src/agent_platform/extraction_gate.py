"""Extraction gate — decide whether a workflow is worth evolving into or
registering as a template.

.. deprecated::
    This module operates on DecisionPoint lists from DecisionTracker and is
    only used by the legacy POST /sessions/{id}/extract-template endpoint.
    New code should use EvolutionGate (evolution_engine.py) which operates on
    real WorkflowSpec objects produced by Builder Team.

v2: Gates now operate on the real WorkflowSpec produced by Builder Team,
not on DecisionTracker metadata. The old DecisionPoint-based gates are
preserved for backward compatibility with the session extraction API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .meta_cognition import DecisionPoint
    from .template_store import TemplateStore


class ExtractionGate:
    """Quality gate for session-to-template extraction proposals.

    Preserved for backward compatibility with POST /sessions/{id}/extract-template.
    For the Builder auto-extract flow, use EvolutionGate instead.
    """

    def __init__(self, template_store: "TemplateStore | None" = None) -> None:
        self._store = template_store

    def should_propose(
        self,
        decision_points: list["DecisionPoint"],
    ) -> tuple[bool, str]:
        """Return (should_propose, reason)."""
        if len(decision_points) < 2:
            return False, f"insufficient_decisions ({len(decision_points)})"

        if self._store is not None:
            for template in self._store.list():
                if self._is_covered(decision_points, template):
                    return False, f"covered_by:{template.name}"

        if self._store is not None:
            existing = [
                t for t in self._store.list()
                if hasattr(t, "tags") and t.tags
            ]
            if not self._is_novel(decision_points, existing):
                return False, "no_novel_branches"

        return True, "proposed"

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_covered(decision_points: list["DecisionPoint"], template) -> bool:
        session_tags = ExtractionGate._extract_tags(decision_points)
        template_tags = set(getattr(template, "tags", []) or [])
        return len(session_tags & template_tags) >= 2

    @staticmethod
    def _extract_tags(decision_points: list["DecisionPoint"]) -> set[str]:
        tags: set[str] = set()
        for dp in decision_points:
            text = (dp.question + " " + dp.context).casefold()
            for keyword in (
                "api", "automation", "自动", "app", "应用", "schedule", "定时",
                "web", "http", "test", "测试", "deploy", "部署",
                "debug", "调试", "security", "安全",
            ):
                if keyword in text:
                    tags.add(keyword.rstrip("自动应用定时测试部署调试安全"))
            for branch in dp.branches:
                outcome_text = (branch.answer + " " + branch.outcome).casefold()
                for keyword in ("api", "web", "app", "automation", "schedule",
                                "test", "deploy", "debug", "security"):
                    if keyword in outcome_text:
                        tags.add(keyword)
        return tags

    @staticmethod
    def _is_novel(
        decision_points: list["DecisionPoint"],
        templates: list,
    ) -> bool:
        if not templates:
            return True
        branch_count = sum(len(dp.branches) for dp in decision_points)
        max_existing_tags = max(
            (len(getattr(t, "tags", []) or []) for t in templates),
            default=0,
        )
        return branch_count > max_existing_tags or branch_count >= 3
