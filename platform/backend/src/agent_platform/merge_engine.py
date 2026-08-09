"""Merge engine — decide whether a candidate workflow should be merged into
an existing template, and actually perform the graph merge.

Confidence model:
  expert_manual:     confidence = 0.70  (seed)
  1 session verify:  confidence += 0.15 → 0.85
  2 session verify:  confidence += 0.10 → 0.95
  3+ session verify: confidence += 0.03 → converge toward 0.99

v2: merge() now ACTUALLY modifies the workflow graph, adding novel nodes and
edges from the candidate that don't exist in the template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .template_models import ProvenanceSource, Template
    from .template_store import TemplateStore
    from .workflow_models import EdgeSpec, NodeSpec, WorkflowSpec


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

    # ── public API ─────────────────────────────────────────────────

    def check_similarity(
        self,
        candidate: "WorkflowSpec",
    ) -> SimilarityResult:
        """Test similarity of *candidate* against all registered templates."""
        if self._store is None:
            return SimilarityResult(should_merge=False, diff_summary="no store")

        best_score = 0.0
        best_template: "Template | None" = None

        for tpl_meta in self._store.list():
            t = self._store.get(tpl_meta.name) if hasattr(self._store, "get") else None
            if t is None:
                continue
            score = self._compute_similarity(candidate, t.workflow)
            if score > best_score:
                best_score = score
                best_template = t

        if best_score >= 0.5 and best_template is not None:
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

        v2: Actually merges the workflow graph by calling merge_workflow_graph(),
        adding novel nodes/edges from candidate. Confidence and provenance are
        also updated.
        """
        if self._store is None:
            return None
        try:
            template = self._store.get(target_name)
        except KeyError:
            return None

        # Capture original types BEFORE merge for novelty detection
        orig_t_types = {n.type for n in template.workflow.nodes}

        # Actually merge the graph
        merged_wf = self.merge_workflow_graph(template.workflow, candidate)
        template.workflow = merged_wf

        meta = template.meta
        meta.provenance.append(source)
        boost = 0.15 if meta.confidence < 0.80 else (
            0.10 if meta.confidence < 0.90 else 0.03
        )
        meta.confidence = round(min(0.99, meta.confidence + boost), 3)
        meta.version += 1
        meta.usage_count += 1

        # Track novel node types BEFORE merge (use original template types)
        c_types = {n.type for n in candidate.nodes} if hasattr(candidate, "nodes") else set()
        novel = c_types - orig_t_types
        if novel:
            meta.pending_branches_count += 1

        # Update success tracking
        meta.total_uses += 1
        meta.total_successes += 1  # reaching merge() means validation passed
        if meta.total_uses > 0:
            meta.success_rate = round(meta.total_successes / meta.total_uses, 3)

        return template

    # ── Graph merging ──────────────────────────────────────────────

    def merge_workflow_graph(
        self,
        template: "WorkflowSpec",
        candidate: "WorkflowSpec",
    ) -> "WorkflowSpec":
        """Actually merge two workflow graphs.

        Strategy:
        - Preserve all template nodes and edges as-is
        - Add candidate nodes that don't exist in template (by type+title similarity)
        - Add candidate edges between newly-added nodes
        - Handle ID conflicts by prefixing candidate node IDs

        Returns a NEW WorkflowSpec (doesn't mutate inputs).
        """
        from .workflow_models import EdgeSpec, WorkflowSpec

        t_nodes = list(template.nodes)
        t_edges = list(template.edges)
        t_ids = {n.id for n in t_nodes}

        # Track consumed type+title slots so duplicates are handled correctly
        type_title_counts: dict[tuple[str, str], int] = {}
        for n in t_nodes:
            key = (n.type, n.title.casefold())
            type_title_counts[key] = type_title_counts.get(key, 0) + 1
        # Track how many of each type+title we've already added as novel
        added_counts: dict[tuple[str, str], int] = {}

        # Track which candidate nodes are genuinely novel
        novel_nodes: list[NodeSpec] = []
        novel_ids: set[str] = set()
        id_map: dict[str, str] = {}  # candidate_id → new_id

        for c_node in candidate.nodes:
            # Skip start/end — templates already have their own terminals
            if c_node.type in ("start", "end", "answer"):
                continue

            # Check if this node type+title is already sufficiently covered.
            # We track how many of each (type, title) pair exist in the template
            # and how many we've already added from the candidate.
            key = (c_node.type, c_node.title.casefold())
            existing_count = type_title_counts.get(key, 0)
            already_added = added_counts.get(key, 0)

            # If template already has enough slots for this type+title, skip
            if already_added + 1 <= existing_count:
                added_counts[key] = already_added + 1  # consume one slot
                continue

            # Generate a unique ID
            new_id = self._unique_node_id(c_node, t_ids | novel_ids)
            id_map[c_node.id] = new_id
            novel_ids.add(new_id)
            added_counts[key] = added_counts.get(key, 0) + 1
            relocated = c_node.model_copy(update={"id": new_id})
            novel_nodes.append(relocated)

        # Add novel edges that connect to novel or existing nodes
        novel_edges: list[EdgeSpec] = []
        for c_edge in candidate.edges:
            new_source = id_map.get(c_edge.source, c_edge.source)
            new_target = id_map.get(c_edge.target, c_edge.target)

            # At least one endpoint must be novel for the edge to be meaningful
            if new_source == c_edge.source and new_target == c_edge.target:
                # Neither endpoint was novel — skip (template already has this wiring)
                continue

            # Both endpoints must exist in the merged graph
            all_ids = t_ids | novel_ids
            if new_source not in all_ids or new_target not in all_ids:
                continue

            novel_edges.append(
                EdgeSpec(
                    id=self._unique_edge_id(t_edges, novel_edges),
                    source=new_source,
                    target=new_target,
                    source_port=c_edge.source_port,
                    target_port=c_edge.target_port,
                    branch=c_edge.branch,
                )
            )

        merged = WorkflowSpec(
            nodes=t_nodes + novel_nodes,
            edges=t_edges + novel_edges,
        )
        return merged

    # ── similarity computation ─────────────────────────────────────

    @staticmethod
    def _compute_similarity(
        a: "WorkflowSpec", b: "WorkflowSpec"
    ) -> float:
        """Structural similarity of two workflow specs.

        Factors:
          - Node type + family Jaccard similarity     (weight 0.30)
            → raw types (0.6) blended with family grouping (0.4)
          - Decision-node count similarity            (weight 0.20)
          - Edge count similarity                     (weight 0.20)
          - Edge structural similarity                (weight 0.30)
            → compares adjacency patterns: does b wire the same types together?
        """
        from .block_families import get_family

        a_types = {n.type for n in a.nodes}
        b_types = {n.type for n in b.nodes}
        union = a_types | b_types
        raw_type_sim = len(a_types & b_types) / max(len(union), 1)

        # Family-level Jaccard: two blocks in the same family (e.g. context_assembler
        # and workspace_context_injector) should contribute partial similarity.
        family_a = {get_family(t) or t for t in a_types}
        family_b = {get_family(t) or t for t in b_types}
        family_union = family_a | family_b
        family_sim = len(family_a & family_b) / max(len(family_union), 1) if family_union else 0.0

        # Blend: raw types carry 60% weight, family structure 40%
        type_sim = 0.6 * raw_type_sim + 0.4 * family_sim

        a_decisions = sum(1 for n in a.nodes if n.type in ("llm", "if_else", "question_classifier", "model_turn"))
        b_decisions = sum(1 for n in b.nodes if n.type in ("llm", "if_else", "question_classifier", "model_turn"))
        depth_sim = 1.0 - abs(a_decisions - b_decisions) / max(a_decisions, b_decisions, 1)

        a_edges = len(a.edges)
        b_edges = len(b.edges)
        edge_sim = 1.0 - abs(a_edges - b_edges) / max(a_edges, b_edges, 1)

        # Edge structural similarity: compare source→target type pairs
        def edge_type_pairs(wf: "WorkflowSpec") -> set[tuple[str, str]]:
            node_types = {n.id: n.type for n in wf.nodes}
            pairs: set[tuple[str, str]] = set()
            for e in wf.edges:
                src_type = node_types.get(e.source, "?")
                tgt_type = node_types.get(e.target, "?")
                pairs.add((src_type, tgt_type))
            return pairs

        a_pairs = edge_type_pairs(a)
        b_pairs = edge_type_pairs(b)
        pair_union = a_pairs | b_pairs
        edge_struct_sim = len(a_pairs & b_pairs) / max(len(pair_union), 1) if pair_union else 0.0

        return (
            0.30 * type_sim
            + 0.20 * depth_sim
            + 0.20 * edge_sim
            + 0.30 * edge_struct_sim
        )

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

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _unique_node_id(node: "NodeSpec", existing_ids: set[str]) -> str:
        """Generate a unique node ID that doesn't collide with existing ones."""
        base = re.sub(r"[^a-zA-Z0-9_]", "_", node.id)[:32]
        if base not in existing_ids:
            return base
        for i in range(1, 100):
            candidate_id = f"{base}_{i}"
            if candidate_id not in existing_ids:
                return candidate_id
        return f"{base}_{uuid4().hex[:8]}"

    @staticmethod
    def _unique_edge_id(
        existing_edges: list, novel_edges: list,
    ) -> str:
        """Generate a unique edge ID."""
        all_edge_ids = {e.id for e in existing_edges} | {e.id for e in novel_edges}
        eid = f"e_{uuid4().hex[:8]}"
        while eid in all_edge_ids:
            eid = f"e_{uuid4().hex[:8]}"
        return eid
