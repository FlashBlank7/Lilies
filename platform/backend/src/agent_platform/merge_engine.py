"""Merge engine — decide whether a candidate workflow should be merged into
an existing template, based on structural similarity and tag overlap.

Confidence model:
  expert_manual:     confidence = 0.70  (seed)
  1 session verify:  confidence += 0.15 → 0.85
  2 session verify:  confidence += 0.10 → 0.95
  3+ session verify: confidence += 0.03 → converge toward 0.99
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .template_models import ProvenanceSource, Template
    from .template_store import TemplateStore
    from .workflow_models import WorkflowSpec


@dataclass
class SimilarityResult:
    should_merge: bool
    target_template: str | None = None
    similarity_score: float = 0.0
    confidence_after: float = 0.0
    diff_summary: str = ""


class MergeEngine:
    def __init__(self, template_store: "TemplateStore | None" = None) -> None:
        self._store = template_store

    # ── public API ─────────────────────────────────────────────

    def check_similarity(
        self,
        candidate: "WorkflowSpec",
    ) -> SimilarityResult:
        """Test similarity of *candidate* against all registered templates."""
        if self._store is None:
            return SimilarityResult(should_merge=False, diff_summary="no store")

        best_score = 0.0
        best_template: "Template | None" = None

        for template in self._store.list():
            t = self._store.get(template.name) if hasattr(self._store, "get") else None
            if t is None:
                continue
            score = self._compute_similarity(candidate, t.workflow)
            if score > best_score:
                best_score = score
                best_template = t

        if best_score >= 0.7 and best_template is not None:
            return SimilarityResult(
                should_merge=True,
                target_template=best_template.meta.name,
                similarity_score=round(best_score, 3),
                confidence_after=round(
                    min(0.99, best_template.meta.confidence + 0.15), 3
                ),
                diff_summary=self._compute_diff(candidate, best_template.workflow),
            )

        return SimilarityResult(
            should_merge=False,
            similarity_score=round(best_score, 3),
            diff_summary="No similar template found.",
        )

    def merge(
        self,
        candidate: "WorkflowSpec",
        target_name: str,
        source: "ProvenanceSource",
    ) -> "Template | None":
        """Merge *candidate* into template *target_name*.

        In v1, the existing workflow structure is preserved — the candidate
        adds confidence and provenance but does not alter the workflow graph.
        v2 will support merging novel branches.
        """
        if self._store is None:
            return None
        try:
            template = self._store.get(target_name)
        except KeyError:
            return None

        meta = template.meta
        meta.provenance.append(source)
        # Confidence bumps: hand-crafted sources also get the first bump
        boost = 0.15 if meta.confidence < 0.80 else (
            0.10 if meta.confidence < 0.90 else 0.03
        )
        meta.confidence = round(min(0.99, meta.confidence + boost), 3)
        meta.version += 1
        meta.usage_count += 1

        # Check for novel branches (structural diff)
        c_types = {n.type for n in candidate.nodes} if hasattr(candidate, "nodes") else set()
        t_types = {n.type for n in template.workflow.nodes}
        novel = c_types - t_types
        if novel:
            meta.pending_branches_count += 1

        return template

    # ── internal ───────────────────────────────────────────────

    @staticmethod
    def _compute_similarity(
        a: "WorkflowSpec", b: "WorkflowSpec"
    ) -> float:
        """Structural similarity of two workflow specs.

        Factors:
          - Node type Jaccard similarity    (weight 0.4)
          - Decision-node count similarity  (weight 0.3)
          - Edge count similarity           (weight 0.3)
        """
        a_types = {n.type for n in a.nodes}
        b_types = {n.type for n in b.nodes}
        union = a_types | b_types
        type_sim = len(a_types & b_types) / max(len(union), 1)

        a_decisions = sum(1 for n in a.nodes if n.type in ("llm", "if_else", "question_classifier"))
        b_decisions = sum(1 for n in b.nodes if n.type in ("llm", "if_else", "question_classifier"))
        depth_sim = 1.0 - abs(a_decisions - b_decisions) / max(a_decisions, b_decisions, 1)

        a_edges = len(a.edges)
        b_edges = len(b.edges)
        edge_sim = 1.0 - abs(a_edges - b_edges) / max(a_edges, b_edges, 1)

        return 0.4 * type_sim + 0.3 * depth_sim + 0.3 * edge_sim

    @staticmethod
    def _compute_diff(
        candidate: "WorkflowSpec", existing: "WorkflowSpec"
    ) -> str:
        c_types = {n.type for n in candidate.nodes}
        e_types = {n.type for n in existing.nodes}
        c_only = c_types - e_types
        e_only = e_types - c_types
        parts = []
        if c_only:
            parts.append(f"+{len(c_only)} new types: {sorted(c_only)}")
        if e_only:
            parts.append(f"-{len(e_only)} removed types: {sorted(e_only)}")
        return "; ".join(parts) if parts else "Identical structure"
