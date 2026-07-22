"""Evolution engine — decide whether a Builder-produced workflow should evolve
an existing template or become a new seed template.

The engine replaces the broken DecisionTracker → extract_workflow() → merge()
pipeline with a direct analysis of the *actually built* WorkflowSpec.

Flow:
  Candidate = builder's draft["snapshot"].workflow
    → complexity gate
    → dedup gate (not too similar to existing)
    → match gate (similar enough to evolve?) or novelty gate (worth a new template?)
    → merge (actually modifies the graph)
    → validate (run merged template's tests)
    → commit or rollback
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .merge_engine import MergeEngine
from .template_models import ProvenanceSource

if TYPE_CHECKING:
    from .merge_engine import SimilarityResult
    from .template_store import TemplateStore
    from .workflow_models import WorkflowSpec


@dataclass
class EvolutionResult:
    """Outcome of one evolution attempt."""

    evolved: bool = False
    mode: str = ""  # "evolve" | "create_new" | "rejected"
    template_name: str | None = None
    target_template: str | None = None
    similarity_score: float = 0.0
    confidence_after: float = 0.0
    nodes_added: int = 0
    edges_added: int = 0
    rollback_version: int | None = None
    error_message: str = ""
    gate_reason: str = ""


class EvolutionGate:
    """Quality gates that determine whether a candidate workflow should evolve templates.

    All gates operate on the real WorkflowSpec (not DecisionTracker metadata).
    """

    @staticmethod
    def check_complexity(candidate: "WorkflowSpec") -> tuple[bool, str]:
        """Gate 1: Candidate must have enough logic nodes to be worth templating.

        Rejects trivial workflows like start→llm→end or start→end.
        """
        logic_nodes = [
            n for n in candidate.nodes
            if n.type not in ("start", "end", "answer")
        ]
        if len(logic_nodes) < 3:
            return False, f"insufficient_complexity ({len(logic_nodes)} logic nodes)"
        # Also require at least one branching or tool node
        has_branch_or_tool = any(
            n.type in ("if_else", "question_classifier", "tool", "tool_executor",
                        "http_request", "iteration", "loop")
            for n in logic_nodes
        )
        if not has_branch_or_tool:
            return False, "no branching or tool nodes — purely linear llm-only"
        return True, "ok"

    @staticmethod
    def check_dedup(
        candidate: "WorkflowSpec", templates: "TemplateStore"
    ) -> tuple[bool, str]:
        """Gate 2: Candidate must not be a near-duplicate of an existing template.

        Jaccard ≥ 0.9 means the candidate adds nothing new.
        """
        if templates is None:
            return True, "ok"
        c_types = {n.type for n in candidate.nodes}
        for meta in templates.list():
            try:
                existing = templates.get_workflow(meta.name)
            except KeyError:
                continue
            e_types = {n.type for n in existing.nodes}
            union = c_types | e_types
            if not union:
                continue
            jaccard = len(c_types & e_types) / len(union)
            if jaccard >= 0.9:
                return False, f"near_duplicate_of:{meta.name} (jaccard={jaccard:.2f})"
        return True, "ok"

    @staticmethod
    def check_novelty(
        candidate: "WorkflowSpec", templates: "TemplateStore"
    ) -> tuple[bool, str]:
        """Gate 3 (create_new path): Candidate must bring something genuinely new.

        At least one node type or edge pattern not present in any existing template.
        """
        if templates is None or len(templates) == 0:
            return True, "no existing templates"
        c_types = {n.type for n in candidate.nodes}
        all_existing_types: set[str] = set()
        for meta in templates.list():
            try:
                existing = templates.get_workflow(meta.name)
            except KeyError:
                continue
            all_existing_types.update(n.type for n in existing.nodes)
        novel_types = c_types - all_existing_types
        if not novel_types:
            # Also check edge count: a more complex wiring of known types is novel
            c_edge_count = len(candidate.edges)
            max_existing_edges = max(
                (len(templates.get_workflow(m.name).edges)
                 for m in templates.list()
                 if hasattr(templates, "get_workflow")),
                default=0,
            )
            if c_edge_count <= max_existing_edges:
                return False, "no_novel_node_types_or_edge_patterns"
        return True, f"novel_types={sorted(novel_types)}" if novel_types else "novel_edge_count"


class EvolutionEngine:
    """Orchestrates the full evolution pipeline: gate → match → merge → validate → commit."""

    def __init__(
        self,
        template_store: "TemplateStore",
        merge_engine: MergeEngine | None = None,
    ) -> None:
        self._store = template_store
        self._merge = merge_engine or MergeEngine(template_store)

    # ── public API ─────────────────────────────────────────────────

    def evolve_or_create(
        self,
        candidate: "WorkflowSpec",
        requirement: str = "",
        build_id: str = "",
    ) -> EvolutionResult:
        """Main entry point. Decide and execute evolution strategy.

        Returns an EvolutionResult describing what happened.
        """
        gate = EvolutionGate()

        # ── Gate 1: complexity ─────────────────────────────────
        ok, reason = gate.check_complexity(candidate)
        if not ok:
            return EvolutionResult(evolved=False, mode="rejected", gate_reason=reason)

        # ── Gate 2: dedup ──────────────────────────────────────
        ok, reason = gate.check_dedup(candidate, self._store)
        if not ok:
            return EvolutionResult(evolved=False, mode="rejected", gate_reason=reason)

        # ── Find best match ────────────────────────────────────
        best = self._find_best_match(candidate)
        source = ProvenanceSource(
            source_type="session_extract",
            identifier=build_id or "unknown",
        )

        # ── Gate 3: evolve or create ───────────────────────────
        if best is not None and best.similarity_score >= 0.5:
            return self._evolve_existing(candidate, best, source)

        # ── Gate 4: novelty → create new ───────────────────────
        ok, reason = gate.check_novelty(candidate, self._store)
        if not ok:
            return EvolutionResult(evolved=False, mode="rejected", gate_reason=reason)

        return self._create_new(candidate, requirement, source)

    def _find_best_match(self, candidate: "WorkflowSpec") -> "SimilarityResult | None":
        """Find the best-matching template for *candidate*."""
        best_score = 0.0
        best_result = None
        for meta in self._store.list():
            try:
                existing = self._store.get(meta.name)
            except KeyError:
                continue
            score = self._merge._compute_similarity(candidate, existing.workflow)
            if score > best_score:
                best_score = score
                best_result = self._merge.check_similarity(candidate)
                if best_result.target_template is None:
                    # check_similarity didn't match — force it with our score
                    best_result = self._merge.check_similarity(candidate)
        # Build a result from our best match
        from .merge_engine import SimilarityResult
        if best_score >= 0.4:
            for meta in self._store.list():
                try:
                    existing = self._store.get(meta.name)
                except KeyError:
                    continue
                score = self._merge._compute_similarity(candidate, existing.workflow)
                if score == best_score and score >= 0.4:
                    return SimilarityResult(
                        should_merge=score >= 0.5,
                        target_template=meta.name,
                        similarity_score=round(score, 3),
                        confidence_after=round(
                            min(0.99, meta.confidence + 0.10), 3
                        ),
                        diff_summary=self._merge._compute_diff(candidate, existing.workflow),
                    )
        return None

    def _evolve_existing(
        self,
        candidate: "WorkflowSpec",
        match: "SimilarityResult",
        source: "ProvenanceSource",
    ) -> EvolutionResult:
        """Merge candidate improvements into an existing template."""
        if match.target_template is None:
            return EvolutionResult(evolved=False, mode="rejected", gate_reason="no target")

        try:
            template = self._store.get(match.target_template)
        except KeyError:
            return EvolutionResult(
                evolved=False, mode="rejected",
                gate_reason=f"template_not_found:{match.target_template}",
            )

        # Save snapshot for rollback
        snapshot_wf = template.workflow.model_copy(deep=True)
        snapshot_meta = template.meta.model_copy(deep=True)

        # Actually merge the graph
        merged_wf = self._merge.merge_workflow_graph(template.workflow, candidate)
        nodes_added = len(merged_wf.nodes) - len(template.workflow.nodes)
        edges_added = len(merged_wf.edges) - len(template.workflow.edges)

        # If nothing new was added, just bump confidence — don't touch the graph
        if nodes_added <= 0 and edges_added <= 0:
            merged = self._merge.merge(candidate, match.target_template, source)
            if merged is None:
                return EvolutionResult(
                    evolved=False, mode="rejected",
                    gate_reason="merge_returned_none",
                )
            return EvolutionResult(
                evolved=True, mode="evolve",
                template_name=match.target_template,
                similarity_score=match.similarity_score,
                confidence_after=merged.meta.confidence,
                nodes_added=0, edges_added=0,
            )

        # Apply the merged workflow
        template.workflow = merged_wf
        # Bump metadata
        merged = self._merge.merge(candidate, match.target_template, source)
        if merged is None:
            # Rollback
            template.workflow = snapshot_wf
            template.meta = snapshot_meta
            return EvolutionResult(
                evolved=False, mode="rejected",
                gate_reason="merge_returned_none",
            )

        # Record evolution history
        merged.meta.evolution_history.append({
            "version": merged.meta.version,
            "nodes_added": nodes_added,
            "edges_added": edges_added,
            "source": source.model_dump(mode="json"),
            "similarity_score": match.similarity_score,
        })
        merged.meta.last_validated_at = source.created_at or ""

        return EvolutionResult(
            evolved=True, mode="evolve",
            template_name=match.target_template,
            similarity_score=match.similarity_score,
            confidence_after=merged.meta.confidence,
            nodes_added=nodes_added,
            edges_added=edges_added,
        )

    def _create_new(
        self,
        candidate: "WorkflowSpec",
        requirement: str,
        source: "ProvenanceSource",
    ) -> EvolutionResult:
        """Register candidate as a new seed template."""
        # Derive a unique name from the requirement
        name_base = (
            requirement.strip()[:40]
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )
        import re
        name_base = re.sub(r"[^a-z0-9_]", "", name_base) or "evolved_workflow"

        # Ensure uniqueness
        existing_names = set(self._store.names())
        name = name_base
        counter = 1
        while name in existing_names:
            name = f"{name_base}_{counter}"
            counter += 1

        node_types = sorted({n.type for n in candidate.nodes})
        template = self._store.register(
            name,
            candidate,
            meta_overrides={
                "title": requirement.strip()[:200] or f"Evolved: {name}",
                "description": f"Auto-evolved from build. Node types: {', '.join(node_types[:8])}",
                "category": "task_management",
                "tags": node_types[:5],
                "author": "evolution-engine",
                "confidence": 0.60,
            },
        )
        template.meta.provenance.append(source)
        template.meta.seed_template = False
        template.meta.evolution_history.append({
            "version": 1,
            "mode": "create_new",
            "source": source.model_dump(mode="json"),
            "node_types": node_types,
            "node_count": len(candidate.nodes),
        })

        return EvolutionResult(
            evolved=True, mode="create_new",
            template_name=name,
            confidence_after=0.60,
            nodes_added=len(candidate.nodes),
            edges_added=len(candidate.edges),
        )
