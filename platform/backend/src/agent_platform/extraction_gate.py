"""Extraction gate — decide whether a session's decision points are worth
extracting into a reusable workflow template.

Uses a three-layer filter:
  1. Minimum decision count (>= 2)
  2. Template coverage check (not already handled by existing templates)
  3. Novelty check (contains branches not seen in existing templates)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .meta_cognition import DecisionPoint
    from .template_store import TemplateStore


class ExtractionGate:
    """Quality gate for session-to-template extraction proposals."""

    def __init__(self, template_store: "TemplateStore | None" = None) -> None:
        self._store = template_store

    def should_propose(
        self,
        decision_points: list["DecisionPoint"],
    ) -> tuple[bool, str]:
        """Return (should_propose, reason).

        All three gates must pass for a proposal to be made.
        """
        # Gate 1 ────────────────────────────────────────────────
        if len(decision_points) < 2:
            return False, f"insufficient_decisions ({len(decision_points)})"

        # Gate 2 ────────────────────────────────────────────────
        if self._store is not None:
            for template in self._store.list():
                if self._is_covered(decision_points, template):
                    return False, f"covered_by:{template.name}"

        # Gate 3 ────────────────────────────────────────────────
        if self._store is not None:
            existing = [
                t for t in self._store.list()
                if hasattr(t, "tags") and t.tags
            ]
            if not self._is_novel(decision_points, existing):
                return False, "no_novel_branches"

        return True, "proposed"

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _is_covered(decision_points: list["DecisionPoint"], template) -> bool:
        """Check via tag overlap between session and template."""
        session_tags = ExtractionGate._extract_tags(decision_points)
        template_tags = set(getattr(template, "tags", []) or [])
        return len(session_tags & template_tags) >= 2

    @staticmethod
    def _extract_tags(decision_points: list["DecisionPoint"]) -> set[str]:
        """Extract keyword tags from decision question/answer text."""
        tags: set[str] = set()
        for dp in decision_points:
            text = (dp.question + " " + dp.context).casefold()
            if "api" in text:
                tags.add("api")
            if "automation" in text or "自动" in text:
                tags.add("automation")
            if "app" in text or "应用" in text:
                tags.add("app")
            if "schedule" in text or "定时" in text:
                tags.add("scheduled")
            if "web" in text or "http" in text:
                tags.add("web")
            if "test" in text or "测试" in text:
                tags.add("testing")
            if "deploy" in text or "部署" in text:
                tags.add("deployment")
            if "debug" in text or "调试" in text:
                tags.add("debugging")
            if "security" in text or "安全" in text:
                tags.add("security")
            # Also tag decision outcome text
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
        """At least some branch structure is novel vs existing templates."""
        if not templates:
            return True
        branch_count = sum(len(dp.branches) for dp in decision_points)
        max_existing_tags = max(
            (len(getattr(t, "tags", []) or []) for t in templates),
            default=0,
        )
        return branch_count > max_existing_tags or branch_count >= 3
