from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .workflow_models import WorkflowSpec, WorkflowTestCase


class BuilderBenchmarkCase(BaseModel):
    name: str
    requirement: str = ""
    reference: WorkflowSpec
    candidate: WorkflowSpec
    required_node_types: list[str] = Field(default_factory=list)
    required_tool_nodes: list[str] = Field(default_factory=list)
    required_harness_nodes: list[str] = Field(default_factory=list)
    tests: list[WorkflowTestCase] = Field(default_factory=list)


class BuilderBenchmarkReport(BaseModel):
    name: str
    passed: bool
    score: float
    metrics: dict[str, Any]
    missing: dict[str, list[str]]


class BuilderBenchmark:
    def evaluate(self, case: BuilderBenchmarkCase) -> BuilderBenchmarkReport:
        candidate_types = [node.type for node in case.candidate.nodes]
        candidate_tool_nodes = [
            str(node.config.get("tool_name"))
            for node in case.candidate.nodes
            if node.type == "tool" and node.config.get("tool_name")
        ]
        candidate_tool_nodes.extend(
            str(node.config.get("settings", {}).get("tool_name"))
            for node in case.candidate.nodes
            if node.type == "tool_executor" and node.config.get("settings", {}).get("tool_name")
        )
        required_node_types = case.required_node_types or sorted({node.type for node in case.reference.nodes})
        required_tool_nodes = case.required_tool_nodes
        required_harness_nodes = case.required_harness_nodes
        missing_node_types = sorted(set(required_node_types) - set(candidate_types))
        missing_tool_nodes = sorted(set(required_tool_nodes) - set(candidate_tool_nodes))
        missing_harness_nodes = sorted(set(required_harness_nodes) - set(candidate_types))
        reference_edges = {(edge.source, edge.target, edge.branch) for edge in case.reference.edges}
        candidate_edges = {(edge.source, edge.target, edge.branch) for edge in case.candidate.edges}
        shared_edges = reference_edges & candidate_edges
        edge_similarity = len(shared_edges) / max(len(reference_edges), 1)
        node_type_coverage = (
            (len(required_node_types) - len(missing_node_types)) / max(len(required_node_types), 1)
        )
        tool_node_coverage = (
            (len(required_tool_nodes) - len(missing_tool_nodes)) / max(len(required_tool_nodes), 1)
            if required_tool_nodes else 1.0
        )
        harness_coverage = (
            (len(required_harness_nodes) - len(missing_harness_nodes)) / max(len(required_harness_nodes), 1)
            if required_harness_nodes else 1.0
        )
        readable_tests = sum(1 for test in case.tests if test.frame is not None)
        test_frame_coverage = readable_tests / max(len(case.tests), 1) if case.tests else 1.0
        score = round(
            0.35 * node_type_coverage
            + 0.20 * tool_node_coverage
            + 0.20 * harness_coverage
            + 0.15 * edge_similarity
            + 0.10 * test_frame_coverage,
            3,
        )
        missing = {
            "node_types": missing_node_types,
            "tool_nodes": missing_tool_nodes,
            "harness_nodes": missing_harness_nodes,
        }
        return BuilderBenchmarkReport(
            name=case.name,
            passed=score >= 0.8 and not any(missing.values()),
            score=score,
            metrics={
                "node_type_coverage": round(node_type_coverage, 3),
                "tool_node_coverage": round(tool_node_coverage, 3),
                "harness_coverage": round(harness_coverage, 3),
                "edge_similarity": round(edge_similarity, 3),
                "test_frame_coverage": round(test_frame_coverage, 3),
                "candidate_node_count": len(case.candidate.nodes),
                "reference_node_count": len(case.reference.nodes),
            },
            missing=missing,
        )

