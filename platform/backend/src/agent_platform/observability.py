"""Workflow observability — run metrics, cost attribution, failure patterns.

Provides answers to:
  - Which node took the longest? (flame-graph-like breakdown)
  - Which node cost the most tokens?
  - Why did this workflow fail? (failure pattern clustering)
  - How does this run compare to previous runs of the same version?
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .storage import Storage


@dataclass
class NodeMetrics:
    node_id: str
    node_type: str
    title: str
    elapsed_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    retry_count: int = 0
    failed: bool = False
    error_message: str = ""


@dataclass
class RunMetrics:
    run_id: str
    application_id: str
    status: str
    total_elapsed_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    node_count: int = 0
    tool_call_count: int = 0
    permission_requests: int = 0
    error_count: int = 0
    nodes: list[NodeMetrics] = field(default_factory=list)
    failure_pattern: str = ""
    compare_to_avg: dict[str, Any] = field(default_factory=dict)
    # 这次运行到底有没有留下用量数据。
    #
    # 不加这一格的话，"没记过用量"和"确实一个 token 都没花"在结果里
    # 长得一模一样，都是 0。真机上工作流运行**从来没有**记过用量
    # （node.completed 事件里 usage 是 {}），于是这个接口对每一次运行
    # 都报 0 token / $0——看起来像是算过了，其实什么都没有。
    # 0 和"没有"要分得开，这是这个仓里反复出现的一条。
    usage_recorded: bool = False


@dataclass
class FailurePattern:
    pattern_name: str
    error_keywords: list[str]
    count: int = 0
    example_run_ids: list[str] = field(default_factory=list)


class RunAnalyzer:
    """Analyze workflow run events to produce structured metrics."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def analyze(self, run_id: str) -> RunMetrics | None:
        """Build RunMetrics from a completed workflow run."""
        try:
            events = await self._storage.list_events(run_id)
        except Exception:
            return None

        if not events:
            return None

        metrics = RunMetrics(run_id=run_id, application_id="", status="unknown")
        node_start_times: dict[str, float] = {}
        node_metrics: dict[str, NodeMetrics] = {}
        workflow_started_at: float | None = None
        workflow_ended_at: float | None = None

        for event in events:
            etype = event.type
            data = event.data or {}
            ts = _parse_timestamp(event.created_at)

            if etype == "workflow.started":
                workflow_started_at = ts
                metrics.application_id = str(data.get("application_id", ""))
            elif etype == "workflow.completed":
                workflow_ended_at = ts
                metrics.status = "succeeded"
            elif etype == "workflow.failed":
                workflow_ended_at = ts
                metrics.status = "failed"
                metrics.error_count += 1
                metrics.failure_pattern = _classify_failure(data.get("error", ""))

            elif etype == "node.started":
                nid = str(data.get("node_id", ""))
                node_start_times[nid] = ts
                nm = node_metrics.setdefault(
                    nid,
                    NodeMetrics(
                        node_id=nid,
                        node_type=str(data.get("type", "")),
                        title=str(data.get("title", "")),
                    ),
                )
                nm.node_type = str(data.get("type", ""))
                nm.title = str(data.get("title", ""))
            elif etype == "node.completed":
                nid = str(data.get("node_id", ""))
                start = node_start_times.get(nid)
                if start:
                    nm = node_metrics.setdefault(
                        nid,
                        NodeMetrics(node_id=nid, node_type="", title=""),
                    )
                    nm.elapsed_ms = (ts - start) * 1000
            elif etype == "node.failed":
                nid = str(data.get("node_id", ""))
                nm = node_metrics.setdefault(
                    nid,
                    NodeMetrics(node_id=nid, node_type="", title=""),
                )
                nm.failed = True
                nm.error_message = str(data.get("error", ""))[:200]
                metrics.error_count += 1
            elif etype == "node.retry":
                nid = str(data.get("node_id", ""))
                nm = node_metrics.setdefault(
                    nid,
                    NodeMetrics(node_id=nid, node_type="", title=""),
                )
                nm.retry_count += 1

            elif etype in ("tool.started",) or ".tool.started" in etype:
                metrics.tool_call_count += 1
                # Try to attribute to the scoped node
                scoped = str(data.get("node_id", ""))
                if scoped:
                    nm = node_metrics.setdefault(
                        scoped, NodeMetrics(node_id=scoped, node_type="", title="")
                    )
                    nm.tool_calls += 1

            elif "permission.requested" in etype:
                metrics.permission_requests += 1

            # Token usage from model events
            usage = data.get("usage", {})
            if isinstance(usage, dict) and usage:
                metrics.usage_recorded = True
                it = int(usage.get("input_tokens", 0))
                ot = int(usage.get("output_tokens", 0))
                metrics.total_input_tokens += it
                metrics.total_output_tokens += ot
                cost = float(usage.get("cost_usd", 0))
                metrics.total_cost_usd += cost
                # Attribute to scoped node
                scoped = str(data.get("node_id", ""))
                if scoped:
                    nm = node_metrics.setdefault(
                        scoped, NodeMetrics(node_id=scoped, node_type="", title="")
                    )
                    nm.input_tokens += it
                    nm.output_tokens += ot
                    nm.cost_usd += cost

        if workflow_started_at and workflow_ended_at:
            metrics.total_elapsed_ms = (workflow_ended_at - workflow_started_at) * 1000

        metrics.node_count = len(node_metrics)
        metrics.nodes = sorted(node_metrics.values(), key=lambda n: n.elapsed_ms, reverse=True)

        # Compare to average of same application
        metrics.compare_to_avg = await self._compare_to_average(metrics)

        return metrics

    async def failure_patterns(
        self, application_id: str, limit: int = 20
    ) -> list[FailurePattern]:
        """Cluster failure patterns for an application's recent runs.

        原来是 `list_events(application_id)` ——**事件不挂在应用上，挂在运行上**
        （stream_id 就是 run_id，循环里 `run_id = event.stream_id` 那句
        正说明作者知道这一点）。于是这个查询永远返回空列表，
        这个接口对任何应用都答"没有失败模式"。
        真机验过：某个应用 70 次真实失败、66 次是同一句
        「node aggregator failed: collection expression requires an array」，
        它照样答 0 类。**找不到东西被当成了"没有东西"。**

        改成直接读运行表。理由和别处一样：计数进 SQL，比扫事件流准也便宜
        （真机上全库 29 万条事件，而失败的运行 227 条）。
        """
        patterns: dict[str, FailurePattern] = {}

        def _query() -> list[tuple[str, str]]:
            with self._storage._connect() as conn:
                return [
                    (str(row["id"]), str(row["error"] or ""))
                    for row in conn.execute(
                        """SELECT id,
                                  COALESCE(error, json_extract(state_json,'$.error'), '')
                                    AS error
                             FROM workflow_runs
                            WHERE application_id=? AND status='failed'
                            ORDER BY created_at DESC LIMIT 500""",
                        (application_id,),
                    ).fetchall()
                ]

        try:
            rows = await asyncio.to_thread(_query)
        except Exception:
            return []

        for run_id, error in rows:
            key = _classify_failure(error)
            if key not in patterns:
                patterns[key] = FailurePattern(
                    pattern_name=key,
                    error_keywords=error.split()[:5] if error else [],
                )
            patterns[key].count += 1
            if len(patterns[key].example_run_ids) < 3:
                patterns[key].example_run_ids.append(run_id)

        return sorted(patterns.values(), key=lambda p: p.count, reverse=True)[:limit]

    async def _compare_to_average(self, metrics: RunMetrics) -> dict[str, Any]:
        """Compare this run to the average of the same application."""
        # Simplified: return empty for now. Needs historical data.
        return {
            "message": "Comparative metrics require >= 2 runs of the same version.",
            "available": False,
        }


# ── helpers ────────────────────────────────────────────────────

def _parse_timestamp(ts_str: str | None) -> float:
    """Parse ISO timestamp to epoch seconds, best-effort."""
    if not ts_str:
        return 0.0
    import time as _time
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return _time.time()


def _classify_failure(error_text: str) -> str:
    """Classify a failure error into a pattern name."""
    if not error_text:
        return "unknown"
    text = error_text.casefold()
    if any(kw in text for kw in ("timeout", "timed out", "rate limit")):
        return "api_timeout_or_rate_limit"
    if any(kw in text for kw in ("permission denied", "unauthorized", "403")):
        return "permission_error"
    if any(kw in text for kw in ("syntax", "nameerror", "attributeerror", "keyerror")):
        return "code_execution_error"
    if any(kw in text for kw in ("out of memory", "disk full", "quota")):
        return "resource_exhausted"
    if any(kw in text for kw in ("json", "parse", "decode", "malformed")):
        return "json_parse_error"
    if any(kw in text for kw in ("not found", "missing", "no such")):
        return "missing_resource"
    if any(kw in text for kw in ("budget exceeded", "round limit", "max turns")):
        return "governance_limit_reached"
    return "unknown"


def render_metrics_summary(metrics: RunMetrics) -> str:
    """Render human-readable metrics summary."""
    lines = [
        f"# Run Metrics: {metrics.run_id[:8]}",
        "",
        f"Status: {metrics.status}",
        f"Duration: {metrics.total_elapsed_ms/1000:.1f}s",
        f"Tokens: {metrics.total_input_tokens} in / {metrics.total_output_tokens} out",
        f"Cost: ${metrics.total_cost_usd:.4f}",
        f"Nodes: {metrics.node_count} | Tools: {metrics.tool_call_count} | Errors: {metrics.error_count}",
        "",
        "## Node Breakdown (by duration)",
    ]
    for node in metrics.nodes[:10]:
        status = "❌" if node.failed else "✅"
        lines.append(
            f"- {status} `{node.node_type}` {node.title}: "
            f"{node.elapsed_ms/1000:.1f}s, "
            f"{node.input_tokens}+{node.output_tokens} tokens, "
            f"${node.cost_usd:.4f}"
        )
    if metrics.failure_pattern:
        lines.append(f"\nFailure pattern: {metrics.failure_pattern}")
    return "\n".join(lines)
