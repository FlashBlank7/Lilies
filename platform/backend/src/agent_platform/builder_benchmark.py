from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .workflow_models import WorkflowSpec, WorkflowTestCase


DEFAULT_NODE_TYPE_EQUIVALENCE: dict[str, list[str]] = {
    "model_turn": ["llm"],
}


class BuilderBenchmarkCase(BaseModel):
    name: str
    requirement: str = ""
    reference: WorkflowSpec
    candidate: WorkflowSpec
    required_node_types: list[str] = Field(default_factory=list)
    required_tool_nodes: list[str] = Field(default_factory=list)
    required_harness_nodes: list[str] = Field(default_factory=list)
    equivalent_node_types: dict[str, list[str]] = Field(default_factory=dict)
    tests: list[WorkflowTestCase] = Field(default_factory=list)


class BuilderBenchmarkReport(BaseModel):
    name: str
    passed: bool
    score: float
    metrics: dict[str, Any]
    missing: dict[str, list[str]]


class BuilderBenchmarkCostRecord(BaseModel):
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    provider: str = ""
    model: str = ""
    notes: str = ""


class BuilderBenchmarkTrend(BaseModel):
    name: str
    score: float
    baseline_score: float | None = None
    delta: float | None = None
    direction: str = "new"


class BuilderBenchmarkSuiteCase(BaseModel):
    name: str
    description: str = ""
    cases: list[BuilderBenchmarkCase] = Field(default_factory=list, min_length=1)
    minimum_score: float = Field(default=0.8, ge=0, le=1)
    minimum_pass_rate: float = Field(default=1.0, ge=0, le=1)
    baseline_scores: dict[str, float] = Field(default_factory=dict)
    cost: BuilderBenchmarkCostRecord = Field(default_factory=BuilderBenchmarkCostRecord)


class BuilderBenchmarkSuiteReport(BaseModel):
    name: str
    passed: bool
    score: float
    pass_rate: float
    case_count: int
    failed_cases: list[str]
    reports: list[BuilderBenchmarkReport]
    metrics: dict[str, Any]
    trends: list[BuilderBenchmarkTrend]
    cost: BuilderBenchmarkCostRecord


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
        equivalent_node_types = self._equivalent_node_types(case)
        missing_node_types = [
            required for required in sorted(set(required_node_types))
            if not self._node_type_satisfied(required, candidate_types, equivalent_node_types)
        ]
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
                "node_type_equivalences": equivalent_node_types,
            },
            missing=missing,
        )

    def evaluate_suite(self, suite: BuilderBenchmarkSuiteCase) -> BuilderBenchmarkSuiteReport:
        reports = [self.evaluate(case) for case in suite.cases]
        case_count = len(reports)
        score = round(sum(report.score for report in reports) / max(case_count, 1), 3)
        passed_cases = [report for report in reports if report.passed]
        pass_rate = round(len(passed_cases) / max(case_count, 1), 3)
        failed_cases = [report.name for report in reports if not report.passed]
        trends = [
            self._trend_for(report, suite.baseline_scores.get(report.name))
            for report in reports
        ]
        metric_keys = sorted({key for report in reports for key in report.metrics})
        averages = {
            key: round(
                sum(float(report.metrics.get(key, 0)) for report in reports)
                / max(case_count, 1),
                3,
            )
            for key in metric_keys
            if all(isinstance(report.metrics.get(key, 0), int | float) for report in reports)
        }
        return BuilderBenchmarkSuiteReport(
            name=suite.name,
            passed=score >= suite.minimum_score and pass_rate >= suite.minimum_pass_rate,
            score=score,
            pass_rate=pass_rate,
            case_count=case_count,
            failed_cases=failed_cases,
            reports=reports,
            metrics={
                "average": averages,
                "minimum_score": suite.minimum_score,
                "minimum_pass_rate": suite.minimum_pass_rate,
            },
            trends=trends,
            cost=suite.cost,
        )

    def _trend_for(
        self,
        report: BuilderBenchmarkReport,
        baseline_score: float | None,
    ) -> BuilderBenchmarkTrend:
        if baseline_score is None:
            return BuilderBenchmarkTrend(name=report.name, score=report.score)
        delta = round(report.score - baseline_score, 3)
        if delta > 0:
            direction = "improved"
        elif delta < 0:
            direction = "regressed"
        else:
            direction = "unchanged"
        return BuilderBenchmarkTrend(
            name=report.name,
            score=report.score,
            baseline_score=baseline_score,
            delta=delta,
            direction=direction,
        )

    def _equivalent_node_types(self, case: BuilderBenchmarkCase) -> dict[str, list[str]]:
        merged = {key: list(value) for key, value in DEFAULT_NODE_TYPE_EQUIVALENCE.items()}
        for key, value in case.equivalent_node_types.items():
            existing = merged.setdefault(key, [])
            existing.extend(item for item in value if item not in existing)
        return {key: sorted(set(value)) for key, value in merged.items()}

    @staticmethod
    def _node_type_satisfied(
        required: str,
        candidate_types: list[str],
        equivalent_node_types: dict[str, list[str]],
    ) -> bool:
        candidate_set = set(candidate_types)
        if required in candidate_set:
            return True
        return any(alias in candidate_set for alias in equivalent_node_types.get(required, []))
