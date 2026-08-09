"""Builder Benchmark — repeatable quality measurement for Builder Team.

Usage:
  PYTHONPATH=platform/backend/src python -m agent_platform.benchmark

This measures:
  - Success rate: % of builds that publish
  - Failure classification: JSON / Block-hallucination / Graph-break / Test-loop / Timeout
  - Average turns & repair cycles per success/failure
  - Operation-level error rates by tool type

Design: runs against real LLM (DeepSeek). Not a unit test — an engineering metric.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .applications import ApplicationService
from .blocks import build_block_registry
from .builder import WorkflowBuilder
from .config import Settings, get_settings
from .permissions import PermissionBroker
from .providers.multi import MultiProvider
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .storage import Storage
from .tools import build_core_registry
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import WorkflowStorage


# ── Benchmark task definitions ────────────────────────────────────

BENCHMARK_TASKS = [
    # Level 1: trivial (should succeed 100%)
    ("L1-echo", "Build a workflow that takes a text message as input and returns it unchanged."),

    # Level 2: simple (2-3 nodes)
    ("L2-classify", "Build a workflow that classifies a customer message as complaint/question/feedback and returns the category."),
    ("L2-summarize", "Build a workflow that takes a long text and returns a one-paragraph summary."),

    # Level 3: moderate (4-6 nodes, branching)
    ("L3-route", "Build a workflow that takes a support ticket, classifies it as urgent/normal/low, and returns a different response template for each category."),
    ("L3-extract", "Build a workflow that takes meeting notes, extracts action items (who/what/when), and formats them as a task list."),

    # Level 4: complex (7-10 nodes, tools)
    ("L4-search-report", "Build a workflow that takes a research question, searches the web, and generates a structured report with citations."),
    ("L4-review-fix", "Build a workflow that reads source code, identifies potential bugs, and suggests fixes."),

    # Level 5: agentic (10+ nodes with agent architecture)
    ("L5-multi-step", "Build a workflow that takes a development task, creates a plan, writes code in steps, runs tests after each step, and reports results."),
]

# ── Failure classification ────────────────────────────────────────

@dataclass(slots=True)
class BuildResult:
    task_id: str
    success: bool
    status: str = ""
    turns: int = 0
    repair_cycles: int = 0
    draft_revision: int = 0
    node_count: int = 0
    edge_count: int = 0
    test_count: int = 0
    error: str = ""
    failure_class: str = ""   # json | block_hallucination | graph_break | test_loop | timeout | none
    elapsed_seconds: float = 0.0
    operations_total: int = 0
    operations_failed: int = 0


def classify_failure(error: str) -> str:
    """Classify a build error into one of the known failure modes."""
    err_lower = error.casefold()
    if "json" in err_lower and ("parse" in err_lower or "decode" in err_lower or "delimiter" in err_lower):
        return "json"
    if "unknown block type" in err_lower:
        return "block_hallucination"
    if "unreachable node" in err_lower or "disconnected" in err_lower:
        return "graph_break"
    if "mandatory" in err_lower and "test" in err_lower:
        return "test_loop"
    if "maximum turns" in err_lower or "repair cycles" in err_lower:
        return "timeout"
    if "invalid tool input" in err_lower and "json" in err_lower:
        return "json"
    return "other"


@dataclass(slots=True)
class BenchmarkReport:
    results: list[BuildResult] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    success_rate: float = 0.0
    avg_turns_success: float = 0.0
    avg_turns_failure: float = 0.0
    failure_distribution: dict[str, int] = field(default_factory=dict)
    avg_elapsed: float = 0.0

    def print(self) -> None:
        print("\n" + "=" * 70)
        print("BUILDER BENCHMARK REPORT")
        print("=" * 70)
        print(f"  Total:       {self.total}")
        print(f"  Succeeded:   {self.succeeded}")
        print(f"  Failed:      {self.failed}")
        print(f"  Success rate: {self.success_rate:.0%}")
        print(f"  Avg turns (success): {self.avg_turns_success:.1f}")
        print(f"  Avg turns (failure): {self.avg_turns_failure:.1f}")
        print(f"  Avg elapsed:  {self.avg_elapsed:.1f}s")
        print(f"\n  Failure distribution:")
        for cls, count in sorted(self.failure_distribution.items(), key=lambda x: -x[1]):
            pct = count / self.failed * 100 if self.failed else 0
            bar = "█" * int(pct / 5)
            print(f"    {cls:<20s} {count:>3d} ({pct:>5.1f}%) {bar}")
        print(f"\n  Per-task results:")
        for r in self.results:
            mark = "✅" if r.success else "❌"
            print(f"    {mark} {r.task_id:<20s} {r.turns:>3d}t {r.repair_cycles:>2d}r "
                  f"{r.node_count:>2d}n {r.edge_count:>2d}e "
                  f"{r.elapsed_seconds:>5.1f}s  {r.failure_class or ''}")


# ── Benchmark runner ──────────────────────────────────────────────

async def run_benchmark(
    tasks: list[tuple[str, str]] | None = None,
    max_turns: int = 40,
    max_repair_cycles: int = 4,
    auto_publish: bool = True,
) -> BenchmarkReport:
    """Run the Builder benchmark against a set of tasks."""
    if tasks is None:
        tasks = BENCHMARK_TASKS

    settings = get_settings()
    settings.prepare()

    storage = Storage(settings.data_dir)
    await storage.initialize()

    provider = MultiProvider(
        deepseek_api_key=settings.deepseek_api_key,
        deepseek_base_url=settings.deepseek_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
    )

    tools = build_core_registry()
    sandboxes = SandboxManager(settings)
    permissions = PermissionBroker()
    runtime = AgentRuntime(
        settings=settings, storage=storage, provider=provider,
        tools=tools, sandboxes=sandboxes, permissions=permissions,
    )
    blocks = build_block_registry()
    workflow_store = WorkflowStorage(storage)
    await workflow_store.initialize()

    applications = ApplicationService(workflow_store, blocks, tools)
    workflow_runtime = WorkflowRuntime(
        storage=storage, workflow_store=workflow_store, applications=applications,
        blocks=blocks, provider=provider, agent_runtime=runtime,
        tools=tools, sandboxes=sandboxes, runtime_model=settings.deepseek_runtime_model,
    )
    builder = WorkflowBuilder(
        storage=storage, workflow_store=workflow_store, applications=applications,
        blocks=blocks, runtime=workflow_runtime, provider=provider,
        agent_runtime=runtime, generator_model=settings.deepseek_generator_model,
        core_tools=tools,
    )

    results: list[BuildResult] = []

    for task_id, requirement in tasks:
        print(f"\n{'─'*60}")
        print(f"🏗️  {task_id}: {requirement[:80]}...")
        start_time = time.monotonic()

        try:
            # Create application
            app = await workflow_store.create_application(
                type("AppReq", (), {
                    "name": f"Bench-{task_id}",
                    "description": requirement[:200],
                    "requirement": requirement,
                    "mode": "workflow",
                })()  # type: ignore
            )
            app_id = app["id"]

            build_id = str(uuid4())
            await workflow_store.create_build(
                build_id, app_id, requirement,
                auto_publish=auto_publish,
                max_turns=max_turns,
                max_repair_cycles=max_repair_cycles,
            )

            # Run builder
            build_task = asyncio.create_task(builder._run(build_id))
            try:
                await asyncio.wait_for(build_task, timeout=600)
            except asyncio.TimeoutError:
                build_task.cancel()
                try: await build_task
                except: pass

            # Collect results
            build = await workflow_store.get_build(build_id)
            ts = build.get("team_state", type("TS", (), {"tasks": [], "repair_cycles": 0, "revision": 0, "coordinator_messages": []})())

            draft = await workflow_store.get_draft(app_id)
            wf = draft["snapshot"].workflow if draft else type("WF", (), {"nodes": [], "edges": []})()

            # Count operations
            events = await storage.list_events(build_id)
            ops_total = sum(1 for e in events if e.type == "build.operation")
            ops_failed = sum(1 for e in events if e.type == "build.operation" and not e.data.get("success", True))

            elapsed = time.monotonic() - start_time
            turns = len(ts.coordinator_messages) // 2 if hasattr(ts, "coordinator_messages") else 0

            result = BuildResult(
                task_id=task_id,
                success=build["status"] == "published",
                status=build["status"],
                turns=turns,
                repair_cycles=getattr(ts, "repair_cycles", 0),
                draft_revision=getattr(ts, "revision", 0),
                node_count=len(wf.nodes),
                edge_count=len(wf.edges),
                test_count=len(draft["snapshot"].tests) if draft else 0,
                error=build.get("error", ""),
                failure_class=classify_failure(build.get("error", "")) if build.get("error") else "none",
                elapsed_seconds=elapsed,
                operations_total=ops_total,
                operations_failed=ops_failed,
            )

        except Exception as e:
            elapsed = time.monotonic() - start_time
            result = BuildResult(
                task_id=task_id, success=False, status="error",
                error=str(e), failure_class=classify_failure(str(e)),
                elapsed_seconds=elapsed,
            )

        results.append(result)
        mark = "✅" if result.success else "❌"
        print(f"  {mark} {result.status} | {result.turns}t/{result.repair_cycles}r "
              f"| {result.node_count}n/{result.edge_count}e "
              f"| {result.elapsed_seconds:.1f}s | {result.failure_class}")

    await sandboxes.close()

    # Build report
    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    total = len(results)
    failure_dist: dict[str, int] = defaultdict(int)
    for r in failed:
        failure_dist[r.failure_class or "unknown"] += 1

    report = BenchmarkReport(
        results=results,
        total=total,
        succeeded=len(succeeded),
        failed=len(failed),
        success_rate=len(succeeded) / total if total else 0,
        avg_turns_success=sum(r.turns for r in succeeded) / len(succeeded) if succeeded else 0,
        avg_turns_failure=sum(r.turns for r in failed) / len(failed) if failed else 0,
        failure_distribution=dict(failure_dist),
        avg_elapsed=sum(r.elapsed_seconds for r in results) / total if total else 0,
    )
    return report


# ── CLI entry ──────────────────────────────────────────────────────

def main() -> None:
    """Run the benchmark from CLI."""
    import sys
    tasks = BENCHMARK_TASKS
    if len(sys.argv) > 1:
        # Filter by level prefix: python -m agent_platform.benchmark L1 L2
        levels = [a.upper() for a in sys.argv[1:]]
        tasks = [(tid, req) for tid, req in BENCHMARK_TASKS if any(tid.startswith(l) for l in levels)]

    print(f"Running benchmark with {len(tasks)} tasks...")
    report = asyncio.run(run_benchmark(tasks=tasks))
    report.print()

    # Exit code: 0 if >= 50% success, 1 otherwise
    sys.exit(0 if report.success_rate >= 0.5 else 1)


if __name__ == "__main__":
    main()
