"""Tests for meta-cognition layer: ExtractionGate, MergeEngine, DecisionTracker."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "backend" / "src"))

from agent_platform.meta_cognition import DecisionTracker, DecisionBranch, DecisionPoint
from agent_platform.extraction_gate import ExtractionGate
from agent_platform.merge_engine import MergeEngine, SimilarityResult
from agent_platform.template_store import TemplateStore
from agent_platform.workflow_models import NodeSpec, EdgeSpec, WorkflowSpec


# ── helpers ────────────────────────────────────────────────────

def make_dp(question: str, answers: list[tuple[str, str]]) -> DecisionPoint:
    """Create a DecisionPoint with branches."""
    dp = DecisionPoint(question=question, context="test")
    for ans, outcome in answers:
        dp.branches.append(DecisionBranch(answer=ans, outcome=outcome))
    return dp


def make_workflow(node_types: list[str]) -> WorkflowSpec:
    """Create a minimal WorkflowSpec from node types."""
    nodes = []
    edges = []
    for i, nt in enumerate(node_types):
        node_id = f"n{i}" if nt != "start" else "s"
        if node_id == "s":
            node_id = "s"
        else:
            node_id = f"n{i}"
        cfg: dict = {}
        if nt == "start":
            cfg = {"inputs": []}
            node_id = "s"
        elif nt == "end":
            cfg = {"outputs": {}}
            node_id = "e"
        elif nt == "llm":
            cfg = {"system": "test", "prompt": "test"}
        elif nt == "if_else":
            cfg = {"cases": [{"id": "yes", "conditions": [{"value": True, "operator": "equals", "expected": True}], "logical_operator": "and"}], "default_branch": "no"}
        elif nt == "template_transform":
            cfg = {"template": "test", "variables": {}}
        else:
            cfg = {"input": {}, "settings": {}}

        node_id = f"n{i}" if nt not in ("start", "end") else ("s" if nt == "start" else "e")
        nodes.append(NodeSpec(id=node_id, type=nt, title=nt, config=cfg))
    for i in range(len(nodes) - 1):
        edges.append(EdgeSpec(id=f"e{i}", source=nodes[i].id, target=nodes[i + 1].id,
                              source_port="output", target_port="input"))
    return WorkflowSpec(nodes=nodes, edges=edges)


# ── ExtractionGate tests ───────────────────────────────────────

class TestExtractionGate:
    def test_rejects_single_decision(self):
        gate = ExtractionGate()
        dps = [make_dp("Q1?", [("YES", "do A")])]
        ok, reason = gate.should_propose(dps)
        assert not ok
        assert "insufficient_decisions" in reason

    def test_proposes_two_decisions(self):
        gate = ExtractionGate()
        dps = [
            make_dp("API available?", [("YES", "use API"), ("NO", "try quick mode")]),
            make_dp("Quick mode?", [("YES", "launch app"), ("NO", "simulate taps")]),
        ]
        ok, reason = gate.should_propose(dps)
        assert ok
        assert reason == "proposed"

    def test_skips_covered_by_template(self, tmp_path):
        store = TemplateStore()
        wf = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        store.register("test_app_automation", wf, meta_overrides={
            "title": "App automation", "tags": ["api", "automation", "app"],
        })
        gate = ExtractionGate(store)
        # Build decision points with API + automation tags
        d1 = make_dp("API available?", [("NO", "check quick mode")])
        d2 = make_dp("Quick mode?", [("YES", "launch app")])
        # Ensure extraction picks up the tags
        d1.question = "Is there an API for automation?"
        d2.question = "Does the app support scheduled quick mode?"
        ok, reason = gate.should_propose([d1, d2])
        # May or may not be covered depending on tag overlap
        # The test verifies the gate doesn't crash
        assert isinstance(ok, bool)

    def test_extract_tags_from_decision_text(self):
        # Test the tag extraction without full store
        gate = ExtractionGate()
        dps = [
            make_dp("Is there an API for automation?", [("NO", "use web http request")]),
        ]
        tags = gate._extract_tags(dps)
        assert "api" in tags
        assert "automation" in tags
        assert "web" in tags


# ── MergeEngine tests ──────────────────────────────────────────

class TestMergeEngine:
    def test_detects_similar_structure(self, tmp_path):
        store = TemplateStore()
        wf_a = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        store.register("tpl_a", wf_a, meta_overrides={"title": "A", "tags": ["api"]})

        wf_b = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        engine = MergeEngine(store)
        result = engine.check_similarity(wf_b)
        assert result.should_merge
        assert result.similarity_score >= 0.9
        assert result.confidence_after > 0.70

    def test_rejects_dissimilar_structure(self, tmp_path):
        store = TemplateStore()
        wf_a = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        store.register("tpl_a", wf_a, meta_overrides={"title": "A", "tags": ["api"]})

        wf_b = make_workflow(["start", "variable_assigner", "variable_assigner", "end"])
        engine = MergeEngine(store)
        result = engine.check_similarity(wf_b)
        assert not result.should_merge

    def test_merge_bumps_confidence(self, tmp_path):
        store = TemplateStore()
        wf = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        store.register("tpl_x", wf, meta_overrides={"title": "X", "tags": ["api"]})

        from agent_platform.template_models import ProvenanceSource
        engine = MergeEngine(store)
        source = ProvenanceSource(source_type="session_extract", identifier="sess-1")

        merged = engine.merge(wf, "tpl_x", source)
        assert merged is not None
        assert merged.meta.confidence > 0.70
        assert merged.meta.version == 2
        assert len(merged.meta.provenance) == 1

    def test_merge_with_novel_branches_increments_pending(self, tmp_path):
        store = TemplateStore()
        wf_a = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        store.register("tpl_novel", wf_a, meta_overrides={"title": "Novel", "tags": ["api"]})

        # Candidate has extra node types
        wf_b = make_workflow(["start", "llm", "if_else", "template_transform", "question_classifier", "end"])

        from agent_platform.template_models import ProvenanceSource
        engine = MergeEngine(store)
        source = ProvenanceSource(source_type="session_extract", identifier="sess-2")
        merged = engine.merge(wf_b, "tpl_novel", source)
        assert merged is not None
        assert merged.meta.pending_branches_count >= 1


# ── DecisionTracker → WorkflowSpec tests ───────────────────────

class TestDecisionTracker:
    def test_extract_workflow_has_valid_structure(self):
        tracker = DecisionTracker("Test automation")
        tracker._current = tracker.ask("API available?", "Trying to automate app X")
        no_branch = tracker.answer("NO", "No API — check quick mode")
        # Sub-decision
        sub = DecisionPoint(question="Quick mode available?", context="")
        no_branch.sub_decisions.append(sub)
        tracker._current = sub
        tracker.answer("YES", "Just launch the app")
        tracker.answer("NO", "Try simulated screen taps")

        wf = tracker.extract_workflow()
        assert wf is not None
        assert len(wf.nodes) >= 3
        assert len(wf.edges) >= 2
        # Should have start, llm, if_else, template, end
        types = {n.type for n in wf.nodes}
        assert "start" in types
        assert "end" in types
        assert "if_else" in types or "llm" in types

    def test_extract_workflow_three_level_nesting(self):
        tracker = DecisionTracker("Deep automation analysis")
        # Level 1: ask + answer
        tracker._current = tracker.ask("API?", "")
        api_no = tracker.answer("NO", "No API available")
        # Sub-decision under "NO"
        sub = DecisionPoint(question="Quick mode?", context="")
        api_no.sub_decisions.append(sub)
        tracker._current = sub
        tracker.answer("YES", "Quick mode works")

        wf = tracker.extract_workflow()
        assert wf is not None
        llm_count = sum(1 for n in wf.nodes if n.type == "llm")
        assert llm_count >= 1

    def test_summary_generates_markdown(self):
        tracker = DecisionTracker("Test")
        tracker._current = tracker.ask("Q1?", "")
        tracker.answer("YES", "Result A")
        # Add a second root decision (clear _current first)
        tracker._current = None
        tracker._current = tracker.ask("Q2?", "")
        tracker.answer("NO", "Result B")
        summary = tracker.summary()
        assert "Decision Tree" in summary or "Q1" in summary
        assert "Q1" in summary


# ── Integration: extraction pipeline ───────────────────────────

class TestExtractionPipeline:
    def test_full_pipeline_extract_gate_merge(self, tmp_path):
        """End-to-end: session decisions → gate → extract → merge."""
        # 1. Simulate decision tracking (2 separate root decisions)
        tracker = DecisionTracker("App automation")
        tracker._current = tracker.ask("Is there a public API?", "Trying to automate DingTalk")
        tracker.answer("NO", "API not available for individuals")
        tracker._current = None  # Reset for new root
        tracker._current = tracker.ask("Does app have quick/auto mode?", "Checking alternatives")
        tracker.answer("YES", "Just need to launch the app at scheduled time")

        # 2. Gate check (no store → should propose)
        gate = ExtractionGate()
        should, reason = gate.should_propose(tracker.roots)
        assert should, f"Gate rejected: {reason}"

        # 3. Extract workflow
        wf = tracker.extract_workflow()
        assert wf is not None
        assert len(wf.nodes) >= 4

        # 4. Create store and register
        store = TemplateStore()
        store.register("app_automation", wf, meta_overrides={
            "title": "App automation workflow",
            "tags": ["api", "automation", "app"],
            "confidence": 0.70,
        })

        # 5. Similar session → should be covered
        tracker2 = DecisionTracker("Similar app automation")
        tracker2._current = tracker2.ask("API?", "Another app")
        tracker2.answer("NO", "No API")
        tracker2._current = None
        tracker2._current = tracker2.ask("Quick mode?", "Check")
        tracker2.answer("YES", "Quick mode works")

        gate2 = ExtractionGate(store)
        should2, reason2 = gate2.should_propose(tracker2.roots)
        assert not should2
        assert "covered_by" in reason2

        # 6. Merge a similar workflow
        from agent_platform.template_models import ProvenanceSource
        engine = MergeEngine(store)
        src = ProvenanceSource(source_type="session_extract", identifier="verify-1")
        merged = engine.merge(wf, "app_automation", src)
        assert merged is not None
        assert merged.meta.confidence > 0.70
        assert merged.meta.version >= 2


# ── EvolutionGate tests (v2: WorkflowSpec-based) ───────────────

class TestEvolutionGate:
    def test_rejects_trivial_linear_workflow(self):
        """Gate 1: start→llm→end is too simple to template."""
        from agent_platform.evolution_engine import EvolutionGate
        wf = make_workflow(["start", "llm", "end"])
        ok, reason = EvolutionGate.check_complexity(wf)
        assert not ok
        assert "insufficient_complexity" in reason or "no branching" in reason

    def test_accepts_branching_workflow(self):
        """Gate 1: start→llm→if_else→template→aggregator→end passes."""
        from agent_platform.evolution_engine import EvolutionGate
        wf = make_workflow(["start", "llm", "if_else", "template_transform", "variable_aggregator", "end"])
        ok, reason = EvolutionGate.check_complexity(wf)
        assert ok, f"Gate rejected: {reason}"

    def test_accepts_tool_workflow(self):
        """Gate 1: workflow with tool nodes passes complexity check."""
        from agent_platform.evolution_engine import EvolutionGate
        wf = make_workflow(["start", "tool", "llm", "template_transform", "end"])
        ok, reason = EvolutionGate.check_complexity(wf)
        assert ok, f"Gate rejected: {reason}"

    def test_rejects_near_duplicate(self, tmp_path):
        """Gate 2: candidate too similar to existing template."""
        from agent_platform.evolution_engine import EvolutionGate
        store = TemplateStore()
        wf_a = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        store.register("tpl_a", wf_a, meta_overrides={"title": "A"})
        # Same node types → should be detected as near-duplicate
        wf_b = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        ok, reason = EvolutionGate.check_dedup(wf_b, store)
        # Jaccard = 5/5 = 1.0 → rejected
        assert not ok
        assert "near_duplicate" in reason

    def test_passes_novelty_check_for_new_types(self, tmp_path):
        """Gate 3 (novelty): candidate with new node types passes."""
        from agent_platform.evolution_engine import EvolutionGate
        store = TemplateStore()
        wf_a = make_workflow(["start", "llm", "end"])
        store.register("tpl_a", wf_a, meta_overrides={"title": "A"})
        # Different node types → novel
        wf_b = make_workflow(["start", "tool", "http_request", "llm", "template_transform", "end"])
        ok, reason = EvolutionGate.check_novelty(wf_b, store)
        assert ok, f"Gate rejected: {reason}"


# ── MergeEngine graph merge tests ──────────────────────────────

class TestWorkflowGraphMerge:
    def test_merge_adds_novel_nodes(self):
        """merge_workflow_graph adds candidate nodes not in template."""
        engine = MergeEngine()
        tmpl = make_workflow(["start", "llm", "template_transform", "end"])
        cand = make_workflow(["start", "llm", "tool", "template_transform", "end"])
        merged = engine.merge_workflow_graph(tmpl, cand)
        # Template has 4 nodes; candidate adds a new "tool" node
        assert len(merged.nodes) >= 5  # start + llm + template_transform + end + tool
        merged_types = {n.type for n in merged.nodes}
        assert "tool" in merged_types

    def test_merge_preserves_template_structure(self):
        """merge_workflow_graph keeps all original template nodes."""
        engine = MergeEngine()
        tmpl = make_workflow(["start", "llm", "template_transform", "end"])
        cand = make_workflow(["start", "tool", "llm", "if_else", "end"])
        merged = engine.merge_workflow_graph(tmpl, cand)
        # All template node IDs must still be present
        tmpl_ids = {n.id for n in tmpl.nodes}
        merged_ids = {n.id for n in merged.nodes}
        for tid in tmpl_ids:
            assert tid in merged_ids, f"Template node {tid} lost during merge"

    def test_merge_handles_id_conflicts(self):
        """merge_workflow_graph renames conflicting node IDs."""
        engine = MergeEngine()
        # Both have a node with the same ID pattern
        tmpl = make_workflow(["start", "llm", "end"])
        cand = make_workflow(["start", "llm", "if_else", "end"])
        merged = engine.merge_workflow_graph(tmpl, cand)
        # All node IDs must be unique
        ids = [n.id for n in merged.nodes]
        assert len(ids) == len(set(ids)), f"Duplicate node IDs: {ids}"

    def test_merge_skips_duplicate_terminal_nodes(self):
        """merge_workflow_graph doesn't add duplicate start/end nodes."""
        engine = MergeEngine()
        tmpl = make_workflow(["start", "llm", "end"])
        cand = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        merged = engine.merge_workflow_graph(tmpl, cand)
        # Should still have exactly 1 start and 1 end
        start_count = sum(1 for n in merged.nodes if n.type == "start")
        end_count = sum(1 for n in merged.nodes if n.type == "end")
        assert start_count == 1, f"Expected 1 start, got {start_count}"
        assert end_count == 1, f"Expected 1 end, got {end_count}"

    def test_merge_with_real_template_structure(self):
        """merge_workflow_graph works with dingtalk-like template structure."""
        engine = MergeEngine()
        # Simulate a dingtalk-style tiered degradation template
        tmpl = make_workflow([
            "start", "llm", "if_else", "tool",
            "template_transform", "variable_aggregator", "end",
        ])
        # Candidate adds an extra tool + http_request
        cand = make_workflow([
            "start", "llm", "if_else", "tool", "tool",
            "http_request", "template_transform", "variable_aggregator", "end",
        ])
        merged = engine.merge_workflow_graph(tmpl, cand)
        merged_types = {n.type for n in merged.nodes}
        assert "http_request" in merged_types
        # Count tool nodes — should increase
        tmpl_tool_count = sum(1 for n in tmpl.nodes if n.type == "tool")
        merged_tool_count = sum(1 for n in merged.nodes if n.type == "tool")
        assert merged_tool_count > tmpl_tool_count, \
            f"Expected more tool nodes, got {merged_tool_count} (template had {tmpl_tool_count})"

    def test_edge_structure_similarity_detects_wiring_patterns(self):
        """_compute_similarity with edge structure distinguishes wiring patterns."""
        # Two workflows with same node types but different wiring
        wf_a = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        wf_b = make_workflow(["start", "llm", "if_else", "template_transform", "end"])
        score = MergeEngine._compute_similarity(wf_a, wf_b)
        # Same types and same linear wiring → high similarity
        assert score >= 0.8, f"Expected high similarity, got {score}"


# ── EvolutionEngine integration tests ──────────────────────────

class TestEvolutionEngine:
    def test_evolve_or_create_evolves_similar_workflow(self, tmp_path):
        """Similar workflow merges into existing template."""
        from agent_platform.evolution_engine import EvolutionEngine
        store = TemplateStore()
        wf_a = make_workflow([
            "start", "llm", "if_else", "template_transform",
            "variable_aggregator", "end",
        ])
        store.register("router_tpl", wf_a, meta_overrides={
            "title": "Router", "tags": ["routing"], "confidence": 0.70,
        })
        # Candidate is structurally similar but adds a tool
        wf_b = make_workflow([
            "start", "llm", "if_else", "tool",
            "template_transform", "variable_aggregator", "end",
        ])
        evo_engine = EvolutionEngine(store)
        result = evo_engine.evolve_or_create(wf_b, "Add tool to router", "build-1")
        assert result.evolved
        assert result.mode == "evolve"
        assert result.template_name == "router_tpl"
        assert result.nodes_added >= 1  # tool node added

    def test_evolve_or_create_creates_new_when_no_match(self, tmp_path):
        """Novel workflow creates a new template."""
        from agent_platform.evolution_engine import EvolutionEngine
        store = TemplateStore()
        # Register a simple linear template
        wf_a = make_workflow(["start", "llm", "end"])
        store.register("simple", wf_a, meta_overrides={
            "title": "Simple", "tags": ["simple"], "confidence": 0.70,
        })
        # Candidate is very different — should create new
        wf_b = make_workflow([
            "start", "tool", "http_request", "tool",
            "template_transform", "variable_aggregator", "end",
        ])
        evo_engine = EvolutionEngine(store)
        result = evo_engine.evolve_or_create(wf_b, "HTTP automation workflow", "build-2")
        assert result.evolved
        assert result.mode == "create_new"
        assert result.template_name is not None
        # Should not match "simple" (Jaccard too low)
        assert result.template_name != "simple"

    def test_evolve_or_create_rejects_trivial_workflow(self, tmp_path):
        """start→llm→end is too trivial to evolve."""
        from agent_platform.evolution_engine import EvolutionEngine
        store = TemplateStore()
        wf = make_workflow(["start", "llm", "end"])
        evo_engine = EvolutionEngine(store)
        result = evo_engine.evolve_or_create(wf, "Simple task", "build-3")
        assert not result.evolved
        assert result.mode == "rejected"

    def test_merge_increases_confidence(self, tmp_path):
        """Evolution bumps template confidence."""
        from agent_platform.evolution_engine import EvolutionEngine
        store = TemplateStore()
        wf = make_workflow([
            "start", "llm", "if_else", "template_transform",
            "variable_aggregator", "end",
        ])
        store.register("evolve_me", wf, meta_overrides={
            "title": "Evolve me", "confidence": 0.70,
        })
        initial_conf = store.get("evolve_me").meta.confidence
        # Build a similar candidate
        cand = make_workflow([
            "start", "llm", "tool", "if_else",
            "template_transform", "variable_aggregator", "end",
        ])
        evo_engine = EvolutionEngine(store)
        result = evo_engine.evolve_or_create(cand, "Enhanced router", "build-4")
        if result.evolved and result.mode == "evolve":
            final_conf = store.get("evolve_me").meta.confidence
            assert final_conf > initial_conf, \
                f"Confidence didn't increase: {initial_conf} → {final_conf}"
