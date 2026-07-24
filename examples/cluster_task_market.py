"""Task Market — A self-organizing multi-agent allocation system.

A concrete instantiation of the cluster L1 infrastructure that demonstrates
emergent coordination patterns from simple local agent rules.

Scenario:
  N Producers publish tasks to a shared market topic.
  M Workers with heterogeneous capabilities claim and execute tasks.
  An Observer monitors the market and records statistics.

Communication topology (all L1-expressible):
  market.tasks    ← Producers publish tasks
  market.claims   ← Workers use conditional_publish to atomically claim tasks
  market.results  ← Workers publish execution results
  market.feedback ← Producers publish quality assessment

Expected emergent patterns:
  1. Capability specialization — workers gravitate to matching task types
  2. Priority ordering — high-priority tasks claimed faster
  3. Load balancing — homogeneous workers distribute tasks evenly
  4. Quality convergence — claim accuracy improves over time
  5. Market clearing — task backlog reaches equilibrium

Usage:
  python examples/cluster_task_market.py [--producers N] [--workers M] [--rounds R]
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_platform.cluster_analysis import ClusterAnalyzer, analyze_run
from agent_platform.cluster_messaging import create_cluster_infrastructure
from agent_platform.cluster_runner import (
    AgentAction,
    AgentActionSpec,
    AgentObservation,
    AgentSpec,
    ClusterScenarioRunner,
    ScenarioConfig,
)
from agent_platform.cluster_telemetry import ClusterTelemetry


# ═══════════════════════════════════════════════════════════════════
# Domain model
# ═══════════════════════════════════════════════════════════════════

TASK_TYPES = ["analysis", "translation", "summarization", "classification", "generation"]
CAPABILITIES = ["nlp", "vision", "data", "code", "reasoning"]

# Task type → required capability mapping
TASK_CAPABILITY_MAP = {
    "analysis": ["data", "reasoning"],
    "translation": ["nlp"],
    "summarization": ["nlp", "reasoning"],
    "classification": ["nlp", "data"],
    "generation": ["code", "reasoning", "nlp"],
}

PRIORITIES = ["low", "medium", "high", "critical"]
PRIORITY_WEIGHT = {"low": 1, "medium": 2, "high": 4, "critical": 8}


@dataclass
class TaskRecord:
    task_id: str
    task_type: str
    priority: str
    producer_id: str
    required_capability: str
    created_round: int
    claimed_by: str = ""
    claimed_round: int = 0
    completed: bool = False


# ═══════════════════════════════════════════════════════════════════
# Agent decision functions
# ═══════════════════════════════════════════════════════════════════

def make_producer_decision(
    agent_id: str,
    task_types: list[str],
    priority_distribution: dict[str, float] | None = None,
    tasks_per_round: float = 0.6,  # probability of producing a task each round
) -> Any:
    """Create a producer that generates tasks of its assigned types.

    Producers publish tasks to market.tasks with varying types and priorities.
    """
    if priority_distribution is None:
        priority_distribution = {"low": 0.3, "medium": 0.4, "high": 0.2, "critical": 0.1}

    task_counter = 0

    def decide(agent_id_inner: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        nonlocal task_counter

        # Check feedback from previous tasks
        feedback_msgs = [m for m in obs.pending_messages
                        if m.get("topic") == "market.feedback"]
        for fb in feedback_msgs:
            payload = fb.get("payload", {})
            if payload.get("producer_id") == agent_id_inner:
                # Could adapt priority distribution based on feedback
                pass

        # Decide whether to produce a task this round
        if random.random() > tasks_per_round:
            return AgentActionSpec(action=AgentAction.IDLE)

        task_type = random.choice(task_types)
        # Find a capability that matches this task type
        caps = TASK_CAPABILITY_MAP.get(task_type, ["reasoning"])
        required_cap = random.choice(caps)

        # Weighted random priority
        r = random.random()
        cumulative = 0
        priority = "medium"
        for p, weight in priority_distribution.items():
            cumulative += weight
            if r <= cumulative:
                priority = p
                break

        task_counter += 1
        task_id = f"{agent_id_inner}_task_{task_counter}"

        return AgentActionSpec(
            action=AgentAction.PUBLISH,
            topic="market.tasks",
            payload={
                "task_id": task_id,
                "task_type": task_type,
                "priority": priority,
                "producer_id": agent_id_inner,
                "required_capability": required_cap,
                "created_round": round_num,
                "reward": PRIORITY_WEIGHT[priority],
            },
        )

    return decide


def make_worker_decision(
    agent_id: str,
    capabilities: list[str],
    strategy: str = "capability_match",  # "capability_match", "greedy", "cautious"
) -> Any:
    """Create a worker that claims and executes tasks matching its capabilities.

    Strategy variants:
      - capability_match: score tasks by capability alignment * priority
      - greedy: claim any task, prefer high reward
      - cautious: only claim tasks with perfect capability match
    """
    completed_count = 0
    claim_history: dict[str, list[bool]] = defaultdict(list)  # task_type → [success]
    subscribed = False
    # State machine for claim→acquire→publish→release cycle
    pending_result: dict[str, str] = {}  # task_id→task_type, set after claim
    holding_lock: bool = False

    def decide(agent_id_inner: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        nonlocal completed_count, subscribed, pending_result, holding_lock

        # State 0: Subscribe to the market
        if not subscribed:
            subscribed = True
            return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="market.tasks")

        # State 1: We claimed a task last round, now acquire lock to write result
        if pending_result:
            task_id = list(pending_result.keys())[0]
            task_type = pending_result[task_id]
            # Check if we already have the lock
            has_lock = any(
                l["resource_id"] == "db.results" and l["mode"] == "write"
                for l in obs.held_locks
            )
            if has_lock:
                # Publish result
                completed_count += 1
                claim_history[task_type].append(True)
                pending_result.clear()
                holding_lock = True  # will release next round
                return AgentActionSpec(
                    action=AgentAction.PUBLISH,
                    topic="market.results",
                    payload={
                        "task_id": task_id,
                        "worker_id": agent_id_inner,
                        "task_type": task_type,
                        "quality": random.uniform(0.6, 1.0),
                    },
                )
            else:
                # Try to acquire lock (may fail if another worker holds it)
                return AgentActionSpec(
                    action=AgentAction.ACQUIRE,
                    resource_id="db.results",
                    mode="write",
                )

        # State 2: We have the lock but no pending result — release it
        if holding_lock:
            holding_lock = False
            return AgentActionSpec(action=AgentAction.RELEASE, resource_id="db.results")

        # Check for new tasks
        task_msgs = [m for m in obs.pending_messages
                     if m.get("topic") == "market.tasks"]

        if not task_msgs:
            # No tasks available — poll again to get latest
            return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="market.tasks")

        # Score each available task
        scored = []
        for msg in task_msgs:
            payload = msg.get("payload", {})
            task_type = payload.get("task_type", "")
            required_cap = payload.get("required_capability", "")
            priority = payload.get("priority", "medium")
            reward = payload.get("reward", 1)

            # Capability match score
            cap_match = 1.0 if required_cap in capabilities else 0.2

            # Historical success rate for this task type
            hist = claim_history.get(task_type, [True])
            success_rate = sum(hist) / len(hist) if hist else 0.5

            # Priority weight
            priority_bonus = PRIORITY_WEIGHT.get(priority, 1) / 8.0

            if strategy == "cautious":
                if required_cap not in capabilities:
                    continue
                score = success_rate * priority_bonus
            elif strategy == "greedy":
                score = reward * (0.3 + 0.7 * cap_match)
            else:  # capability_match
                score = (cap_match * 0.5 + success_rate * 0.3 + priority_bonus * 0.2)

            scored.append((score, msg))

        if not scored:
            return AgentActionSpec(action=AgentAction.IDLE)

        # Pick the best task
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_msg = scored[0]
        payload = best_msg["payload"]

        # Only claim if score is high enough — higher threshold creates specialization
        threshold = {"cautious": 0.7, "greedy": 0.25, "capability_match": 0.5}[strategy]
        if best_score < threshold:
            return AgentActionSpec(action=AgentAction.IDLE)

        # Claim the task by publishing a claim message.
        # In a real system, this would be conditional_publish with per-task
        # exclusivity. Here we use regular publish — multiple workers CAN claim
        # the same task (racing), and the producer decides who wins.
        # This demonstrates Fan-Out (tasks → workers) + Fan-In (workers → claims).
        task_id = payload.get("task_id", "")
        task_type = payload.get("task_type", "")

        # Remember to publish result next round (via lock)
        pending_result[task_id] = task_type

        return AgentActionSpec(
            action=AgentAction.PUBLISH,
            topic="market.claims",
            payload={
                "task_id": task_id,
                "worker_id": agent_id_inner,
                "claim_round": round_num,
                "task_type": task_type,
                "score": best_score,
            },
        )

    return decide


def make_observer_decision() -> Any:
    """Create a passive observer that monitors the market and publishes statistics."""
    stats_round = 0

    def decide(agent_id: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        nonlocal stats_round

        # Every 5 rounds, publish market statistics
        if round_num - stats_round < 5:
            return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="market.tasks")

        stats_round = round_num
        tasks_seen = len([m for m in obs.pending_messages if m.get("topic") == "market.tasks"])
        claims_seen = len([m for m in obs.pending_messages if m.get("topic") == "market.claims"])

        return AgentActionSpec(
            action=AgentAction.PUBLISH,
            topic="market.stats",
            payload={
                "round": round_num,
                "pending_tasks": tasks_seen,
                "claims_this_window": claims_seen,
                "observer": agent_id,
            },
        )

    return decide


# ═══════════════════════════════════════════════════════════════════
# Scenario builder
# ═══════════════════════════════════════════════════════════════════

def build_task_market_scenario(
    n_producers: int = 3,
    n_workers: int = 5,
    n_rounds: int = 50,
    seed: int = 42,
) -> tuple[ScenarioConfig, dict[str, Any]]:
    """Build a complete Task Market scenario.

    Returns (config, decisions).
    """
    random.seed(seed)

    agents: list[AgentSpec] = []
    decisions: dict[str, Any] = {}

    # ── Producers ──────────────────────────────────────────────────
    # Each producer specializes in 1-3 task types
    producer_task_types = {}
    for i in range(n_producers):
        pid = f"producer_{i}"
        n_types = random.randint(1, 3)
        types = random.sample(TASK_TYPES, n_types)
        producer_task_types[pid] = types
        agents.append(AgentSpec(pid, types))

        # Vary priority distribution per producer
        if i == 0:
            # Emergency producer — more critical tasks
            prio_dist = {"low": 0.1, "medium": 0.2, "high": 0.3, "critical": 0.4}
        elif i == n_producers - 1:
            # Background producer — mostly low priority
            prio_dist = {"low": 0.5, "medium": 0.3, "high": 0.15, "critical": 0.05}
        else:
            prio_dist = {"low": 0.3, "medium": 0.4, "high": 0.2, "critical": 0.1}

        decisions[pid] = make_producer_decision(
            pid, types, prio_dist,
            tasks_per_round=0.5 + random.random() * 0.3,
        )

    # ── Workers ────────────────────────────────────────────────────
    # Each worker has 1-3 random capabilities
    strategies = ["capability_match", "capability_match", "greedy", "cautious"]
    worker_capabilities = {}
    for i in range(n_workers):
        wid = f"worker_{i}"
        n_caps = random.randint(1, 3)
        caps = random.sample(CAPABILITIES, n_caps)
        worker_capabilities[wid] = caps
        strategy = strategies[i % len(strategies)]
        agents.append(AgentSpec(wid, caps, {"strategy": strategy}))
        decisions[wid] = make_worker_decision(wid, caps, strategy)

    # ── Observer ───────────────────────────────────────────────────
    agents.append(AgentSpec("observer", ["monitor"]))
    decisions["observer"] = make_observer_decision()

    # Shuffle agent order to prevent deterministic ordering bias
    agent_ids = [a.agent_id for a in agents]
    random.shuffle(agent_ids)

    config = ScenarioConfig(
        name="task_market",
        description=(
            f"Self-organizing task market: {n_producers} producers, "
            f"{n_workers} workers ({len(CAPABILITIES)} capability types), "
            f"{len(TASK_TYPES)} task types, {n_rounds} rounds. "
            f"Seed={seed}"
        ),
        agents=agents,
        topics=["market.tasks", "market.claims", "market.results", "market.feedback", "market.stats"],
        resources=["db.results"],
        rounds=n_rounds,
        agent_order=agent_ids,
        heartbeat_interval_rounds=10,
        expire_after_rounds=20,
    )

    return config, decisions


# ═══════════════════════════════════════════════════════════════════
# Analysis
# ═══════════════════════════════════════════════════════════════════

def analyze_market(telemetry: ClusterTelemetry, run_id: str, config: ScenarioConfig) -> str:
    """Produce a human-readable analysis of a Task Market run."""
    analyzer = ClusterAnalyzer(telemetry)
    report = analyzer.analyze(run_id)
    if report is None:
        return f"No data for run {run_id}"

    events = telemetry.events(run_id)

    # ── Compute market-specific metrics ────────────────────────────
    tasks_published = 0
    tasks_claimed = 0
    workers_active: set[str] = set()
    producers_active: set[str] = set()
    claim_scores: list[float] = []
    tasks_by_type: dict[str, int] = defaultdict(int)
    claims_by_worker: dict[str, int] = defaultdict(int)
    priority_claims: dict[str, int] = defaultdict(int)

    for evt in events:
        payload = json.loads(evt["payload"]) if isinstance(evt["payload"], str) else evt["payload"]
        kind = evt["kind"]

        if kind == "publish" and payload.get("topic") == "market.tasks":
            tasks_published += 1
            # task_type is nested in the payload under topic-specific fields
            task_type = payload.get("task_type", "?")
            tasks_by_type[task_type] += 1
            producers_active.add(evt["agent_id"])

        elif kind in ("publish", "conditional_publish") and payload.get("topic") == "market.claims":
            tasks_claimed += 1
            claims_by_worker[evt["agent_id"]] += 1
            workers_active.add(evt["agent_id"])
            claim_scores.append(payload.get("score", 0))

        elif kind == "publish" and payload.get("topic") == "market.results":
            # Result published — worker completed a task
            workers_active.add(evt["agent_id"])

    # ── Build report ───────────────────────────────────────────────

    # Detect priority handling: are high-priority tasks claimed faster?
    # (crude approximation: comparing claim rates)
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║         Task Market — Emergence Analysis Report              ║",
        "╚══════════════════════════════════════════════════════════════╝",
        "",
        f"Configuration:",
        f"  Producers: {len([a for a in config.agents if a.agent_id.startswith('producer')])}",
        f"  Workers:   {len([a for a in config.agents if a.agent_id.startswith('worker')])}",
        f"  Rounds:    {config.rounds}",
        f"  Topics:    {config.topics}",
        "",
        "── Market Activity ──",
        f"  Tasks published: {tasks_published}",
        f"  Tasks claimed:   {tasks_claimed}",
        f"  Claim rate:      {tasks_claimed/max(tasks_published,1):.1%}",
        f"  Active producers: {len(producers_active)}",
        f"  Active workers:   {len(workers_active)}",
        f"  Avg claim score:  {sum(claim_scores)/max(len(claim_scores),1):.3f}",
        "",
        "── Task Distribution ──",
    ]

    for ttype in sorted(tasks_by_type.keys()):
        bar = "█" * min(tasks_by_type[ttype], 40)
        lines.append(f"  {ttype:20s} {tasks_by_type[ttype]:3d} {bar}")

    lines.append("")
    lines.append("── Worker Activity ──")
    for wid in sorted(claims_by_worker.keys(), key=lambda w: claims_by_worker[w], reverse=True):
        bar = "█" * min(claims_by_worker[wid], 40)
        # Find worker capabilities
        caps = "?"
        for a in config.agents:
            if a.agent_id == wid:
                caps = ",".join(a.capabilities)
                break
        lines.append(f"  {wid:20s} [{caps:25s}] {claims_by_worker[wid]:3d} claims {bar}")

    # ── Emergence analysis ─────────────────────────────────────────
    lines.append("")
    lines.append("── Structural Patterns ──")
    for p in report.message_flow_patterns:
        lines.append(f"  [{p.pattern_type}] {p.description}")

    lines.append("")
    lines.append("── Emergence Signals ──")
    if report.emergence_signals:
        for s in report.emergence_signals:
            lines.append(f"  [{s.signal_type}] (confidence: {s.confidence:.2f})")
            lines.append(f"    {s.description}")
    else:
        lines.append("  (none detected — may need more rounds or agents)")

    lines.append("")
    lines.append("── Theorem Checks ──")
    for check, passed in report.theorem_checks.items():
        status = "✅" if passed else "❌"
        lines.append(f"  {status} {check}")

    # ── Interpretation ─────────────────────────────────────────────
    lines.append("")
    lines.append("── Lock Contention ──")
    if report.lock_contention_patterns:
        for p in report.lock_contention_patterns:
            lines.append(f"  [{p.pattern_type}] {p.description}")
        lines.append(f"  Total: {len(report.lock_contention_patterns)} patterns, "
                     f"{len(report.raw_contentions)} raw contention events")
    else:
        lines.append("  (no contention — lock-free execution)")

    lines.append("")
    lines.append("── Interpretation ──")

    # Detect specialization
    if len(claims_by_worker) >= 3:
        max_claims = max(claims_by_worker.values()) if claims_by_worker else 1
        min_claims = min(claims_by_worker.values()) if claims_by_worker else 0
        if max_claims > min_claims * 2:
            top_worker = max(claims_by_worker, key=claims_by_worker.get)
            lines.append(f"  📌 Specialization detected: {top_worker} dominates claiming")
            lines.append(f"     ({max_claims} vs {min_claims} claims) — workers self-select tasks")
        else:
            lines.append(f"  ✅ Load balanced: claims evenly distributed among workers")

    # Detect market clearing
    if tasks_published > 0:
        clearance = tasks_claimed / tasks_published
        if clearance > 0.8:
            lines.append(f"  ✅ Market clearing: {clearance:.0%} of tasks claimed (efficient)")
        elif clearance > 0.4:
            lines.append(f"  ⚠️  Partial clearing: {clearance:.0%} of tasks claimed (some friction)")
        else:
            lines.append(f"  ❌ Market failure: only {clearance:.0%} tasks claimed (mismatch?)")

    # Detect feedback loop
    has_feedback = any(
        evt["kind"] == "publish" and
        (json.loads(evt["payload"]) if isinstance(evt["payload"], str) else evt["payload"]).get("topic") == "market.feedback"
        for evt in events
    )
    if has_feedback:
        lines.append(f"  🔄 Feedback loop active: producers adapt to worker quality")

    # Structural insight
    if report.message_flow_patterns:
        lines.append(f"  🏗️  {len(report.message_flow_patterns)} message flow patterns identified")
    if report.lock_contention_patterns:
        lines.append(f"  🔒 {len(report.lock_contention_patterns)} lock contention patterns identified")

    lines.append("")
    lines.append("── Raw Telemetry ──")
    lines.append(f"  Total events: {len(events)}")
    lines.append(f"  Run ID: {run_id}")
    lines.append(f"  Telemetry DB: cluster_bus.db (telemetry_events table)")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

async def main(
    n_producers: int = 3,
    n_workers: int = 5,
    n_rounds: int = 50,
    seed: int = 42,
) -> None:
    """Run the Task Market scenario and produce analysis."""
    print(f"🏗️  Task Market — {n_producers} producers, {n_workers} workers, {n_rounds} rounds")
    print(f"   Topics: market.tasks, market.claims, market.results, market.feedback, market.stats")
    print()

    # Build scenario
    config, decisions = build_task_market_scenario(
        n_producers=n_producers,
        n_workers=n_workers,
        n_rounds=n_rounds,
        seed=seed,
    )

    print("   Producers:")
    for a in config.agents:
        if a.agent_id.startswith("producer"):
            print(f"     {a.agent_id}: tasks={a.capabilities}")

    print("   Workers:")
    for a in config.agents:
        if a.agent_id.startswith("worker"):
            strat = a.metadata.get("strategy", "?")
            print(f"     {a.agent_id}: caps={a.capabilities} ({strat})")

    print()
    print(f"⏳ Running {n_rounds} rounds...")

    # Initialize runner
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        runner = ClusterScenarioRunner(data_dir)
        await runner.initialize()

        t0 = time.time()
        summary = await runner.run(config, decisions)
        elapsed = time.time() - t0

        print(f"✅ Completed in {elapsed:.2f}s")
        print(f"   Messages: {summary.total_messages} published, {summary.total_deliveries} delivered")
        print(f"   Lock ops: {summary.total_lock_attempts} attempts, "
              f"{summary.total_lock_conflicts} conflicts")
        print()

        # Analyze
        runs = runner.telemetry.list_runs()
        if runs:
            run_id = runs[0]["run_id"]
            report_text = analyze_market(runner.telemetry, run_id, config)
            print(report_text)
            print()
            print(f"📁 Telemetry saved to: {data_dir}/cluster_bus.db")
            print(f"   Run ID: {run_id}")
            print(f"   Query: SELECT * FROM telemetry_events WHERE run_id = '{run_id}';")
        else:
            print("❌ No telemetry data recorded")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Task Market — Multi-Agent Emergence Demo")
    parser.add_argument("--producers", type=int, default=3)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    asyncio.run(main(
        n_producers=args.producers,
        n_workers=args.workers,
        n_rounds=args.rounds,
        seed=args.seed,
    ))
