"""Workflow Quality Analyzer — graph-theoretic static analysis for BlockFlows.

Grounding:
  - Graph theory: DAG metrics, critical path, transitive closure
  - Petri nets: soundness (liveness + boundedness), siphon detection
  - Cyclomatic complexity: McCabe adapted for DAG node/edge counts
  - Information theory: entropy-based complexity threshold

All methods are *pure* (no I/O, no LLM) — they run in O(|V|+|E|) time.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .workflow_models import WorkflowSpec


@dataclass(slots=True)
class StructuralIssues:
    dead_code: list[str] = field(default_factory=list)
    """Nodes whose outputs are never consumed downstream."""
    orphan_inputs: list[str] = field(default_factory=list)
    """Start inputs that no downstream node references."""
    redundant_chain: list[str] = field(default_factory=list)
    """Consecutive template_transform nodes that could be merged."""


@dataclass(slots=True)
class RobustnessIssues:
    missing_error_handling: list[str] = field(default_factory=list)
    """LLM/tool/http nodes without retry or error_strategy."""
    unguarded_tool: list[str] = field(default_factory=list)
    """Tool nodes without preceding permission_gate."""


@dataclass(slots=True)
class ComplexityMetrics:
    node_count: int = 0
    edge_count: int = 0
    cyclomatic: int = 0
    """McCabe: E - N + 2P (P=1 for single-start DAG)."""
    max_depth: int = 0
    """Longest path from start to any terminal."""
    max_width: int = 0
    """Maximum nodes at any depth level."""
    dependency_density: float = 0.0
    """Edges / max_possible_edges. > 0.7 suggests over-constrained."""


@dataclass(slots=True)
class CoendIssues:
    """$ref coend consistency (category-theoretic data-flow validation)."""
    ambiguous_refs: list[str] = field(default_factory=list)
    """$ref to a node that may have different values across execution paths."""
    missing_optional: list[str] = field(default_factory=list)
    """$ref to a branch-dependent node without optional:true."""
    aggregator_mode_mismatch: list[str] = field(default_factory=list)
    """variable_aggregator mode may not match downstream consumer expectation."""


@dataclass(slots=True)
class PetrinetIssues:
    siphon_cycle: list[list[str]] = field(default_factory=list)
    """Dependency cycles detected among task_dispatcher tasks."""
    unbound_parallelism: list[str] = field(default_factory=list)
    """iteration nodes without explicit parallelism cap."""
    missing_break: list[str] = field(default_factory=list)
    """loop nodes whose break_condition may never be satisfied."""


@dataclass(slots=True)
class WorkflowQualityReport:
    score: int = 100
    """0-100 quality score. Deductions for each issue found."""
    grade: str = "A"
    """A (>=90), B (>=75), C (>=60), D (<60)."""
    structural: StructuralIssues = field(default_factory=StructuralIssues)
    robustness: RobustnessIssues = field(default_factory=RobustnessIssues)
    complexity: ComplexityMetrics = field(default_factory=ComplexityMetrics)
    petrinet: PetrinetIssues = field(default_factory=PetrinetIssues)
    coend: CoendIssues = field(default_factory=CoendIssues)
    suggestions: list[str] = field(default_factory=list)


def analyze_workflow(workflow: WorkflowSpec) -> WorkflowQualityReport:
    """Run all static analyses and return a quality report."""
    node_map = {n.id: n for n in workflow.nodes}
    graph = _build_graph(workflow)

    structural = _analyze_structural(workflow, node_map, graph)
    robustness = _analyze_robustness(workflow, node_map, graph)
    complexity = _analyze_complexity(workflow, node_map, graph)
    petrinet = _analyze_petrinet(workflow, node_map, graph)
    coend = _analyze_coend(workflow, node_map, graph)
    suggestions, score = _compute_score(structural, robustness, complexity, petrinet, coend, workflow)

    grade = "A"
    if score < 90: grade = "B"
    if score < 75: grade = "C"
    if score < 60: grade = "D"

    return WorkflowQualityReport(
        score=score, grade=grade,
        structural=structural, robustness=robustness,
        complexity=complexity, petrinet=petrinet, coend=coend,
        suggestions=suggestions,
    )


# ── Graph builder ──────────────────────────────────────────────────

def _build_graph(workflow: WorkflowSpec) -> dict[str, dict[str, list[str]]]:
    """Return {node_id: {'in': [sources], 'out': [targets]}}."""
    g: dict[str, dict[str, list[str]]] = {n.id: {"in": [], "out": []} for n in workflow.nodes}
    for e in workflow.edges:
        if e.source in g and e.target in g:
            g[e.source]["out"].append(e.target)
            g[e.target]["in"].append(e.source)
    return g


# ── Structural analysis ────────────────────────────────────────────

def _analyze_structural(
    workflow: WorkflowSpec,
    node_map: dict[str, Any],
    graph: dict[str, dict[str, list[str]]],
) -> StructuralIssues:
    s = StructuralIssues()

    # Dead code: nodes with output ports that nobody reads
    consumers: set[str] = set()
    for e in workflow.edges:
        consumers.add(e.source)
    for node in workflow.nodes:
        if node.type in {"end", "answer"}:
            continue
        if node.id not in consumers:
            # Only flag compute nodes (LLM, tool, template), not routing nodes
            if node.type in {"llm", "tool", "template_transform", "variable_assigner"}:
                s.dead_code.append(node.id)

    # Orphan inputs: start inputs not referenced by any downstream node
    for start_node in [n for n in workflow.nodes if n.type == "start"]:
        declared = [f.get("name", "") for f in start_node.config.get("inputs", []) if isinstance(f, dict)]
        if not declared:
            continue
        # Scan ALL config values (not just 'config' key) for $ref to $inputs
        used: set[str] = set()
        for node in workflow.nodes:
            if node.id == start_node.id:
                continue
            # Check node.config, plus any nested workflow (iteration/loop)
            refs = _extract_refs(node.model_dump(mode="json"))
            for ref in refs:
                if ref.get("node_id") == "$inputs" and ref.get("path"):
                    used.add(str(ref["path"][0]))
                # Also check if node references start node's output with specific field
                if ref.get("node_id") == start_node.id and ref.get("path"):
                    if ref["path"][0] != "output" or len(ref["path"]) > 1:
                        used.add(str(ref["path"][-1]))
        for name in declared:
            if name not in used and "*" not in used:
                s.orphan_inputs.append(f"{start_node.id}.{name}")

    # Redundant chain: consecutive template_transform nodes
    for edge in workflow.edges:
        src = node_map.get(edge.source)
        tgt = node_map.get(edge.target)
        if src and tgt and src.type == "template_transform" and tgt.type == "template_transform":
            if len(graph.get(edge.source, {}).get("out", [])) == 1:
                s.redundant_chain.append(f"{edge.source} → {edge.target}")

    return s


# ── Robustness analysis ────────────────────────────────────────────

def _analyze_robustness(
    workflow: WorkflowSpec,
    node_map: dict[str, Any],
    graph: dict[str, dict[str, list[str]]],
) -> RobustnessIssues:
    r = RobustnessIssues()

    fragile_types = {"llm", "tool", "tool_executor", "http_request", "claude_agent"}
    for node in workflow.nodes:
        # Check error handling
        if node.type in fragile_types:
            retry_enabled = bool(node.retry and node.retry.enabled)
            has_err = node.error_strategy and str(node.error_strategy.value) not in ("fail", "continue")
            if not retry_enabled and not has_err:
                r.missing_error_handling.append(node.id)

        # Check tool permission gate
        if node.type in {"tool", "tool_executor"}:
            has_gate = False
            for src_id in graph.get(node.id, {}).get("in", []):
                src = node_map.get(src_id)
                if src and src.type == "permission_gate":
                    has_gate = True
                    break
            if not has_gate:
                r.unguarded_tool.append(node.id)

    return r


# ── Complexity metrics ─────────────────────────────────────────────

def _analyze_complexity(
    workflow: WorkflowSpec,
    node_map: dict[str, Any],
    graph: dict[str, dict[str, list[str]]],
) -> ComplexityMetrics:
    N = len(workflow.nodes)
    E = len(workflow.edges)
    P = len([n for n in workflow.nodes if n.type in {"start", "schedule_trigger"}]) or 1

    c = ComplexityMetrics(node_count=N, edge_count=E)
    c.cyclomatic = E - N + 2 * P  # McCabe for DAGs

    # Max depth: longest path from any start to any terminal
    starts = [n.id for n in workflow.nodes if n.type in {"start", "schedule_trigger"}]
    if starts:
        depths = _topological_depths(graph, starts)
        c.max_depth = max(depths.values(), default=0)

    # Max width: count nodes at each depth
    depth_counts: dict[int, int] = defaultdict(int)
    for d in depths.values():
        depth_counts[d] += 1
    c.max_width = max(depth_counts.values(), default=0)

    # Dependency density
    max_edges = N * (N - 1) / 2
    c.dependency_density = round(E / max_edges, 3) if max_edges > 0 else 0.0

    return c


def _topological_depths(
    graph: dict[str, dict[str, list[str]]],
    starts: list[str],
) -> dict[str, int]:
    """Compute depth of each node from starts via Kahn traversal."""
    depths: dict[str, int] = {}
    indegree: dict[str, int] = defaultdict(int)
    for nid, g in graph.items():
        indegree[nid] = len(g["in"])
    queue = deque(starts)
    for s in starts:
        depths[s] = 0
    while queue:
        cur = queue.popleft()
        for tgt in graph[cur]["out"]:
            depths[tgt] = max(depths.get(tgt, 0), depths[cur] + 1)
            indegree[tgt] -= 1
            if indegree[tgt] == 0:
                queue.append(tgt)
    return depths


# ── Petri-net analysis ─────────────────────────────────────────────

def _analyze_petrinet(
    workflow: WorkflowSpec,
    node_map: dict[str, Any],
    graph: dict[str, dict[str, list[str]]],
) -> PetrinetIssues:
    p = PetrinetIssues()

    # Siphon (dependency cycle) detection in task_dispatcher blocks
    for node in workflow.nodes:
        if node.type != "task_dispatcher":
            continue
        tasks = node.config.get("settings", {}).get("tasks", [])
        if not isinstance(tasks, list):
            continue
        # Build dependency graph among tasks
        dep_graph: dict[str, list[str]] = {}
        for t in tasks:
            if not isinstance(t, dict):
                continue
            name = t.get("name", t.get("subject", t.get("title", str(id(t)))))
            deps = t.get("dependencies", t.get("blocked_by", []))
            if isinstance(deps, str):
                deps = [deps]
            dep_graph[name] = [str(d) for d in deps if str(d) in [tt.get("name", tt.get("subject", tt.get("title", str(id(tt))))) for tt in tasks]]

        # Detect cycles via DFS
        cycles = _find_cycles(dep_graph)
        if cycles:
            p.siphon_cycle = cycles

    # Unbounded parallelism
    for node in workflow.nodes:
        if node.type == "iteration":
            parallelism = node.config.get("parallelism", 1)
            if parallelism is None or int(parallelism) > 8:
                p.unbound_parallelism.append(node.id)

    # Loop without break condition
    for node in workflow.nodes:
        if node.type == "loop":
            brk = node.config.get("break_condition")
            if brk is None:
                p.missing_break.append(node.id)

    return p


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find all cycles in a directed graph via DFS with colors.
    Returns list of cycles (each cycle is a list of node names).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in graph}
    parent: dict[str, str | None] = {n: None for n in graph}
    cycles: list[list[str]] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in graph.get(u, []):
            if color.get(v) == GRAY:
                # Found cycle — backtrack
                cycle = [v, u]
                cur = parent.get(u)
                while cur is not None and cur != v:
                    cycle.append(cur)
                    cur = parent.get(cur)
                cycles.append(list(reversed(cycle)))
            elif color.get(v) == WHITE:
                parent[v] = u
                dfs(v)
        color[u] = BLACK

    for n in graph:
        if color.get(n) == WHITE:
            dfs(n)
    return cycles[:5]  # cap at 5 cycles


# ── Coend / data-flow consistency ──────────────────────────────────

def _analyze_coend(
    workflow: WorkflowSpec,
    node_map: dict[str, Any],
    graph: dict[str, dict[str, list[str]]],
) -> CoendIssues:
    """Coend analysis (category theory): validate $ref data-flow consistency.

    The coend ∫^v P(v, target) ⊗ Ev(v, path) must produce a unique value
    for the workflow to be data-flow consistent. This checks for violations.
    """
    c = CoendIssues()

    # Build branch information: which nodes are conditional (depend on if_else)?
    conditional_nodes: set[str] = set()
    for node in workflow.nodes:
        if node.type == "if_else":
            # All nodes reachable from any branch are conditional
            visited: set[str] = set()
            queue = [node.id]
            while queue:
                cur = queue.pop()
                if cur in visited: continue
                visited.add(cur)
                for tgt in graph.get(cur, {}).get("out", []):
                    queue.append(tgt)
            conditional_nodes |= visited

    # Check all $ref references
    for node in workflow.nodes:
        refs = _extract_refs(node.model_dump(mode="json"))
        for ref in refs:
            ref_node_id = ref.get("node_id", "")
            if ref_node_id in ("$inputs", ""):
                continue
            path = ref.get("path", [])
            is_optional = ref.get("optional", False)

            # Check 1: $ref to a conditional node without optional:true
            if ref_node_id in conditional_nodes and not is_optional:
                c.missing_optional.append(
                    f"{node.id}: $ref→{ref_node_id}.{'.'.join(map(str, path)) if path else 'output'} "
                    f"(conditional node, add optional:true)"
                )

            # Check 2: $ref to a node inside an iteration (ambiguous across items)
            ref_node = node_map.get(ref_node_id)
            if ref_node:
                # Walk back to find if this node is inside an iteration
                for src_id in graph.get(ref_node_id, {}).get("in", []):
                    src = node_map.get(src_id)
                    if src and src.type == "iteration":
                        c.ambiguous_refs.append(
                            f"{node.id}: $ref→{ref_node_id} is inside iteration "
                            f"'{src.title or src.id}', may be ambiguous across items"
                        )

    # Check 3: variable_aggregator mode vs downstream usage
    for node in workflow.nodes:
        if node.type == "variable_aggregator":
            mode = node.config.get("mode", "first")
            # Find downstream consumers
            consumers = graph.get(node.id, {}).get("out", [])
            for consumer_id in consumers:
                consumer = node_map.get(consumer_id)
                if consumer and consumer.type in {"template_transform", "llm"}:
                    config_str = str(consumer.config)
                    if mode == "first" and ("all" in config_str or "merge" in config_str):
                        c.aggregator_mode_mismatch.append(
                            f"{node.id}→{consumer_id}: aggregator mode='{mode}' "
                            f"but consumer '{consumer.title or consumer_id}' may expect all values"
                        )

    return c


# ── Scoring ────────────────────────────────────────────────────────

def _compute_score(
    structural: StructuralIssues,
    robustness: RobustnessIssues,
    complexity: ComplexityMetrics,
    petrinet: PetrinetIssues,
    coend: CoendIssues,
    workflow: WorkflowSpec,
) -> tuple[list[str], int]:
    score = 100
    suggestions: list[str] = []

    # Deductions
    dd = len(structural.dead_code)
    if dd:
        score -= dd * 4
        suggestions.append(f"🗑️ {dd} 个死代码节点（输出未被消费）: {structural.dead_code}")

    oi = len(structural.orphan_inputs)
    if oi:
        score -= oi * 3
        suggestions.append(f"📥 {oi} 个 Start 输入未被下游使用: {structural.orphan_inputs}")

    rc = len(structural.redundant_chain)
    if rc:
        score -= rc * 2
        suggestions.append(f"🔗 {rc} 个可合并的连续 template_transform 节点: {structural.redundant_chain}")

    me = len(robustness.missing_error_handling)
    if me:
        score -= me * 3
        suggestions.append(f"⚠️ {me} 个脆弱节点缺少错误处理: {robustness.missing_error_handling}")

    ug = len(robustness.unguarded_tool)
    if ug:
        score -= ug * 5
        suggestions.append(f"🔓 {ug} 个 tool 节点缺少前置 permission_gate")

    # Complexity penalties
    if complexity.node_count > 15:
        over = complexity.node_count - 15
        score -= over * 2
        suggestions.append(
            f"📐 工作流节点数 ({complexity.node_count}) 超过建议上限 (15)。"
            f"考虑拆分为 {_suggested_split_count(complexity.node_count)} 个子工作流。"
        )

    if complexity.max_depth > 8:
        score -= (complexity.max_depth - 8) * 2
        suggestions.append(f"📏 最大深度 ({complexity.max_depth}) 较深，检查是否有可并行的节点。")

    if complexity.dependency_density > 0.7:
        score -= 5
        suggestions.append(f"🔒 依赖密度 ({complexity.dependency_density}) 过高，工作流过度约束。")

    # Petri-net deductions
    sc = len(petrinet.siphon_cycle)
    if sc:
        score -= sc * 10
        suggestions.append(f"🔴 检测到 {sc} 个任务依赖循环（siphon）: {petrinet.siphon_cycle}")

    up = len(petrinet.unbound_parallelism)
    if up:
        score -= up * 3
        suggestions.append(f"⚡ {up} 个 iteration 节点未限制并发度")

    mb = len(petrinet.missing_break)
    if mb:
        score -= mb * 5
        suggestions.append(f"🔄 {mb} 个 loop 节点缺少显式 break_condition")

    # Coend/data-flow deductions
    ar = len(coend.ambiguous_refs)
    if ar:
        score -= ar * 4
        suggestions.append(f"🔀 {ar} 个 $ref 引用迭代内部节点，数据可能跨迭代项歧义")

    mo = len(coend.missing_optional)
    if mo:
        score -= mo * 3
        suggestions.append(f"⚠️ {mo} 个 $ref 引用条件分支节点但未设置 optional:true")

    am = len(coend.aggregator_mode_mismatch)
    if am:
        score -= am * 3
        suggestions.append(f"🔗 {am} 个 aggregator mode 可能与下游消费者不匹配")

    # Bonus
    has_tests = any(n.type in {"end", "answer"} for n in workflow.nodes)
    has_error_handling = all(
        n.id not in robustness.missing_error_handling
        for n in workflow.nodes
        if n.type in {"llm", "tool", "tool_executor", "http_request"}
    )
    if has_tests:
        score = min(100, score + 2)
    if has_error_handling:
        score = min(100, score + 3)

    return suggestions, max(0, score)


def _suggested_split_count(node_count: int) -> int:
    if node_count <= 15:
        return 1
    if node_count <= 25:
        return 2
    if node_count <= 40:
        return 3
    return 4


# ── Helpers ────────────────────────────────────────────────────────

def _extract_refs(value: Any) -> list[dict[str, Any]]:
    """Recursively extract all $ref objects from a nested dict/list."""
    refs: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, dict):
                refs.append(ref)
            for child in item.values():
                visit(child)

    visit(value)
    return refs


# ── Topology Balance ───────────────────────────────────────────────

@dataclass(slots=True)
class TopologyBalance:
    """Measure the Harness/LLM ratio and decision-point density in a workflow.

    A workflow with 5 Harness nodes + 1 LLM makes more decisions in
    deterministic topology than one with 2 Harness + 1 LLM, which
    delegates everything to the LLM prompt. Higher ratio = more
    workload-appropriate for complex agentic tasks.
    """

    ratio: float = 0.0
    """Harness leverage score. 0 = pure LLM pipe, 1 = pure Harness."""

    harness_nodes: int = 0
    llm_nodes: int = 0
    decision_points: int = 0
    path_count: int = 0

    grade: str = ""
    """high (>0.7), medium (>0.4), low (<=0.4)."""


HARNESS_BLOCKS = {
    "start", "end", "answer", "if_else", "loop", "iteration",
    "template_transform", "variable_assigner", "variable_aggregator",
    "task_dispatcher", "dependency_gate", "budget_gate", "round_limit",
    "permission_gate", "sandbox_boundary", "checkpoint_resume",
    "cancellation_point", "hook_point", "event_recorder",
    "mailbox_wait_wake", "context_compactor", "http_request",
    "schedule_trigger", "tool", "tool_executor",
}

LLM_BLOCKS = {
    "llm", "question_classifier", "parameter_extractor",
    "model_turn", "claude_agent", "subagent_spawn",
}

DECISION_BLOCKS = {
    "if_else", "loop", "question_classifier", "task_dispatcher",
    "dependency_gate", "budget_gate", "round_limit",
}


def compute_topology_balance(workflow: WorkflowSpec) -> TopologyBalance:
    """Measure the Harness/LLM balance and decision density of a workflow.

    Returns a TopologyBalance with ratio (0=pure LLM pipe, 1=pure Harness)
    and grade (high/medium/low).
    """
    graph = _build_graph(workflow)
    tb = TopologyBalance()

    for node in workflow.nodes:
        if node.type in HARNESS_BLOCKS:
            tb.harness_nodes += 1
        if node.type in LLM_BLOCKS:
            tb.llm_nodes += 1
        if node.type in DECISION_BLOCKS:
            tb.decision_points += 1

    tb.path_count = _count_paths(workflow, graph)

    if tb.llm_nodes == 0 and tb.decision_points == 0:
        tb.ratio = 0.3
    elif tb.llm_nodes == 0:
        tb.ratio = 1.0
    elif tb.harness_nodes == 0:
        tb.ratio = 0.0
    else:
        decision_leverage = min(tb.decision_points * (1 + tb.path_count / 10), 10)
        raw = decision_leverage / (decision_leverage + tb.llm_nodes)
        harness_frac = tb.harness_nodes / (tb.harness_nodes + tb.llm_nodes)
        tb.ratio = round(raw * harness_frac + 0.5 * harness_frac * (1 - raw), 3)
        tb.ratio = max(0.0, min(1.0, tb.ratio))

    if tb.ratio > 0.7:
        tb.grade = "high"
    elif tb.ratio > 0.4:
        tb.grade = "medium"
    else:
        tb.grade = "low"

    return tb


def _count_paths(workflow: WorkflowSpec, graph: dict[str, dict[str, list[str]]]) -> int:
    """Count distinct execution paths from start to any terminal.

    For DAGs: dynamic programming over topological order.
    Conditional branches (if_else) create path multiplication.
    """
    starts = [n.id for n in workflow.nodes if n.type in {"start", "schedule_trigger"}]
    terminals = {n.id for n in workflow.nodes if n.type in {"end", "answer"}}
    if not starts:
        return 1

    # Topological order
    indegree: dict[str, int] = {nid: len(graph.get(nid, {}).get("in", [])) for nid in graph}
    queue = deque(starts)
    order: list[str] = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for tgt in graph.get(cur, {}).get("out", []):
            indegree[tgt] -= 1
            if indegree[tgt] == 0:
                queue.append(tgt)

    # DP: path count from start to each node
    path_counts: dict[str, int] = {nid: 0 for nid in graph}
    for s in starts:
        path_counts[s] = 1

    for nid in order:
        node_obj = next((n for n in workflow.nodes if n.id == nid), None)
        multiplier = 1
        # if_else nodes create branching: each case branch counts separately
        if node_obj and node_obj.type == "if_else":
            cases = node_obj.config.get("cases", [])
            if cases:
                multiplier = len(cases)  # each branch is a distinct path

        for tgt in graph.get(nid, {}).get("out", []):
            path_counts[tgt] += path_counts[nid] * multiplier

    return sum(path_counts.get(t, 0) for t in terminals)


# ── Graph Edit Distance (Insight 3: Free Closure) ──────────────────

def graph_edit_distance(
    source: WorkflowSpec,
    target: WorkflowSpec,
) -> tuple[int, list[str]]:
    """Compute the minimal graph edit distance between two workflows.

    Operations (cost 1 each): add_node, remove_node, update_node_type,
    add_edge, remove_edge.

    This is the "geodesic distance" in the free monoidal closure F(B₀).
    Returns (distance, edit_operations).
    """
    src_nodes = {n.id: n for n in source.nodes}
    tgt_nodes = {n.id: n for n in target.nodes}
    ops: list[str] = []
    cost = 0

    # Node-level edits
    for nid, n in tgt_nodes.items():
        if nid not in src_nodes:
            cost += 1
            ops.append(f"+node {nid} ({n.type})")
        elif src_nodes[nid].type != n.type:
            cost += 1
            ops.append(f"~node {nid}: {src_nodes[nid].type}→{n.type}")

    for nid in src_nodes:
        if nid not in tgt_nodes:
            cost += 1
            ops.append(f"-node {nid}")

    # Edge-level edits
    src_edges = {(e.source, e.target) for e in source.edges}
    tgt_edges = {(e.source, e.target) for e in target.edges}

    for edge in tgt_edges:
        if edge not in src_edges:
            cost += 1
            ops.append(f"+edge {edge[0]}→{edge[1]}")

    for edge in src_edges:
        if edge not in tgt_edges:
            cost += 1
            ops.append(f"-edge {edge[0]}→{edge[1]}")

    return cost, ops


def suggest_minimal_repair(
    current: WorkflowSpec,
    target_template: WorkflowSpec,
) -> list[str]:
    """Given a failing workflow and a known-good template, suggest the
    minimal set of edits to transform the current into the template.

    Uses graph edit distance to compute the "repair direction".
    """
    cost, ops = graph_edit_distance(current, target_template)
    if cost == 0:
        return ["✅ Current workflow already matches template."]

    # Prioritize: add missing edges first (usually the fix), then type changes
    add_edges = [o for o in ops if o.startswith("+edge")]
    type_changes = [o for o in ops if o.startswith("~node")]
    add_nodes = [o for o in ops if o.startswith("+node")]
    remove = [o for o in ops if o.startswith("-")]

    suggestions = []
    if add_edges:
        suggestions.append(f"🔗 缺 {len(add_edges)} 条边: {', '.join(add_edges[:5])}")
    if type_changes:
        suggestions.append(f"🔧 {len(type_changes)} 个节点类型不匹配: {', '.join(type_changes[:5])}")
    if add_nodes:
        suggestions.append(f"📦 缺 {len(add_nodes)} 个节点: {', '.join(add_nodes[:5])}")
    if remove:
        suggestions.append(f"🗑️ {len(remove)} 个多余元素需移除")

    suggestions.append(f"📏 总编辑距离: {cost}")
    return suggestions
