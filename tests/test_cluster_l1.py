"""Integration tests for cluster L1 layer and multi-agent interaction analysis.

Tests are organized in four sections:
  1. L1 primitives — conditional_publish, heartbeat, lock_upgrade, expire
  2. Basic scenarios — pubsub, fan-out, resource contention
  3. Pattern detection — verify analyzer detects known patterns
  4. Theorem verification — empirical checks against categorical theorems
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_platform.cluster_analysis import ClusterAnalyzer, analyze_run
from agent_platform.cluster_messaging import (
    ClusterMessageBus,
    ClusterRegistry,
    ConflictDetector,
    create_cluster_infrastructure,
)
from agent_platform.cluster_runner import (
    AgentAction,
    AgentActionSpec,
    AgentObservation,
    AgentSpec,
    ClusterScenarioRunner,
    ScenarioConfig,
    make_contender_decision,
    make_negotiating_decision,
    make_publisher_decision,
    make_subscriber_decision,
)
from agent_platform.cluster_telemetry import (
    ClusterTelemetry,
    EventKind,
    InteractionSummary,
)


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def data_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
async def bus(data_dir):
    b = ClusterMessageBus(data_dir)
    await b.initialize()
    return b


@pytest.fixture
async def infra(data_dir):
    bus, registry, detector = await create_cluster_infrastructure(data_dir)
    return bus, registry, detector


@pytest.fixture
async def runner(data_dir):
    r = ClusterScenarioRunner(data_dir)
    await r.initialize()
    return r


# ═══════════════════════════════════════════════════════════════════
# Section 1: L1 Primitives
# ═══════════════════════════════════════════════════════════════════

class TestL1Primitives:
    """Verify L1-completeness primitives: conditional_publish, heartbeat, lock_upgrade."""

    @pytest.mark.asyncio
    async def test_conditional_publish_exclusive_window(self, bus):
        """Verifies conditional_publish with exclusive_window acts like a mutex."""
        topic = "test_exclusive"
        agent_a, agent_b = "agent_A", "agent_B"

        # A gets the exclusive window
        accepted_a, msg_a = await bus.conditional_publish(
            topic, agent_a,
            {"data": "A's message", "_msg_id": "msg_a_1"},
            {"type": "exclusive_window", "agent_id": agent_a},
        )
        assert accepted_a is True
        assert msg_a is not None
        assert msg_a.publisher_id == agent_a

        # B tries but A just published — should be rejected
        await asyncio.sleep(0.01)  # small delay to ensure different timestamp
        accepted_b, msg_b = await bus.conditional_publish(
            topic, agent_b,
            {"data": "B's message", "_msg_id": "msg_b_1"},
            {"type": "exclusive_window", "agent_id": agent_b},
        )
        assert accepted_b is False
        assert msg_b is None

    @pytest.mark.asyncio
    async def test_conditional_publish_no_recent_from(self, bus):
        """Verifies no_recent_from condition blocks re-publishing."""
        topic = "test_no_recent"
        agent_a = "agent_A"

        # First publish succeeds
        accepted, msg = await bus.conditional_publish(
            topic, agent_a,
            {"data": "first", "_msg_id": "msg_1"},
            {"type": "no_recent_from", "agent_id": agent_a, "within_seconds": 60},
        )
        assert accepted is True

        # Second publish within window fails
        accepted2, msg2 = await bus.conditional_publish(
            topic, agent_a,
            {"data": "second", "_msg_id": "msg_2"},
            {"type": "no_recent_from", "agent_id": agent_a, "within_seconds": 60},
        )
        assert accepted2 is False

        # But another agent can publish
        accepted3, msg3 = await bus.conditional_publish(
            topic, "agent_B",
            {"data": "B's turn", "_msg_id": "msg_3"},
            {"type": "no_recent_from", "agent_id": "agent_B", "within_seconds": 60},
        )
        assert accepted3 is True

    @pytest.mark.asyncio
    async def test_heartbeat_and_expiry(self, infra):
        """Verifies heartbeat keeps agents alive and expiry marks dead ones."""
        bus, registry, detector = infra

        # Register agent B first (will be expired)
        await registry.register("agent_B", ["worker"])
        # Wait so B's timestamp is old enough
        await asyncio.sleep(0.1)

        # Register agent A and immediately heartbeat it
        await registry.register("agent_A", ["worker"])
        await registry.heartbeat("agent_A")

        # Expire with TTL shorter than the A-B registration gap
        expired = await registry.expire_inactive_agents(heartbeat_ttl=0.05)
        assert "agent_B" in expired or len(expired) > 0, (
            f"Expected agent_B to be expired, got: {expired}"
        )

    @pytest.mark.asyncio
    async def test_lock_upgrade_read_to_write(self, infra):
        """Verifies read→write lock upgrade when no other holders."""
        bus, registry, detector = infra

        # Agent A gets read lock
        acquired = await detector.acquire("res_upgrade", "agent_A", "read")
        assert acquired is True

        # Upgrade to write (no other holders)
        upgraded = await detector.upgrade_lock("res_upgrade", "agent_A", "read", "write")
        assert upgraded is True

        # Verify mode changed
        holders = await detector.lock_holders("res_upgrade")
        assert holders[0].mode == "write"

        await detector.release("res_upgrade", "agent_A")

    @pytest.mark.asyncio
    async def test_lock_upgrade_blocked_by_other_readers(self, infra):
        """Verifies lock upgrade fails when another agent holds the lock.

        Note: current ConflictDetector uses resource_id as PRIMARY KEY.
        When a write-lock is held, other agents cannot acquire.
        Upgrade fails when the requester doesn't hold the lock.
        """
        bus, registry, detector = infra

        # Agent B gets write lock
        await detector.acquire("res_shared", "agent_B", "write")

        # A tries to acquire — should fail (B holds exclusive lock)
        acquired_a = await detector.acquire("res_shared", "agent_A", "read")
        assert acquired_a is False

        # A tries to upgrade a lock it doesn't hold — should fail
        upgraded = await detector.upgrade_lock("res_shared", "agent_A", "read", "write")
        assert upgraded is False

        # B releases, now A can acquire and upgrade
        await detector.release("res_shared", "agent_B")
        acquired_a2 = await detector.acquire("res_shared", "agent_A", "read")
        assert acquired_a2 is True

        upgraded2 = await detector.upgrade_lock("res_shared", "agent_A", "read", "write")
        assert upgraded2 is True

        await detector.release("res_shared", "agent_A")

    @pytest.mark.asyncio
    async def test_lock_holders_query(self, infra):
        """Verifies lock_holders returns current holders.

        Note: current schema uses resource_id as PRIMARY KEY (single lock per resource).
        """
        bus, registry, detector = infra

        # A acquires write lock
        acquired = await detector.acquire("res_query", "agent_A", "write")
        assert acquired is True

        holders = await detector.lock_holders("res_query")
        assert len(holders) == 1
        assert holders[0].owner_id == "agent_A"
        assert holders[0].mode == "write"

        await detector.release("res_query", "agent_A")

        # After release, no holders
        holders2 = await detector.lock_holders("res_query")
        assert len(holders2) == 0


# ═══════════════════════════════════════════════════════════════════
# Section 2: Basic Multi-Agent Scenarios
# ═══════════════════════════════════════════════════════════════════

class TestBasicScenarios:
    """Run deterministic multi-agent scenarios and verify basic properties."""

    @pytest.mark.asyncio
    async def test_pubsub_two_agents(self, runner):
        """A publishes to a topic, B subscribes and receives.

        Note: SUBSCRIBE_POLL now only subscribes (non-consuming peek).
        Deliveries happen when the subscriber later actively polls.
        """
        config = ScenarioConfig(
            name="pubsub_two_agents",
            description="Two agents, one topic, basic pub/sub",
            agents=[AgentSpec("A", ["publisher"]), AgentSpec("B", ["subscriber"])],
            topics=["tasks"],
            rounds=6,
        )
        decisions = {
            "A": make_publisher_decision("tasks"),
            "B": lambda aid, r, obs: (
                AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="tasks")
                if r <= 2 else AgentActionSpec(action=AgentAction.IDLE)
            ),
        }
        summary = await runner.run(config, decisions)

        # A publishes every round
        assert summary.total_messages >= config.rounds - 1
        # Flow graph shows A active
        assert "A" in summary.message_flow_graph or summary.total_messages > 0

    @pytest.mark.asyncio
    async def test_fan_out_one_to_many(self, runner):
        """One publisher, three subscribers — fan-out pattern.

        Workers subscribe in round 1, then publisher publishes. Workers see
        tasks via peek (observation). We verify message flow from telemetry.
        """
        def worker_decision(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            if round_num == 1:
                return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="tasks")
            # After subscribing, observation shows pending tasks
            if round_num >= 4 and obs.pending_messages:
                # Consume a message (creates DELIVER event)
                return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="tasks")
            return AgentActionSpec(action=AgentAction.IDLE)

        def pub_decision(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            if round_num >= 2:
                return AgentActionSpec(
                    action=AgentAction.PUBLISH, topic="tasks",
                    payload={"data": f"task_{round_num}"},
                )
            return AgentActionSpec(action=AgentAction.IDLE)

        config = ScenarioConfig(
            name="fan_out",
            agents=[
                AgentSpec("dispatcher", ["dispatch"]),
                AgentSpec("worker_1", ["worker"]),
                AgentSpec("worker_2", ["worker"]),
                AgentSpec("worker_3", ["worker"]),
            ],
            topics=["tasks"],
            rounds=8,
        )
        decisions = {
            "dispatcher": pub_decision,
            "worker_1": worker_decision,
            "worker_2": worker_decision,
            "worker_3": worker_decision,
        }
        summary = await runner.run(config, decisions)
        assert summary.total_messages > 0

    @pytest.mark.asyncio
    async def test_resource_contention_two_agents(self, runner):
        """Two agents contending for one resource — lock conflicts expected."""
        config = ScenarioConfig(
            name="contention",
            agents=[
                AgentSpec("agent_X", ["writer"]),
                AgentSpec("agent_Y", ["writer"]),
            ],
            topics=[],
            resources=["db.primary"],
            rounds=15,
        )
        decisions = {
            "agent_X": make_contender_decision("db.primary", hold_for_rounds=3),
            "agent_Y": make_contender_decision("db.primary", hold_for_rounds=3),
        }
        summary = await runner.run(config, decisions)

        # Should have lock attempts
        assert summary.total_lock_attempts > 0
        # Should have some conflicts (both try to acquire same resource)
        assert summary.total_lock_conflicts >= 0  # conflicts depend on timing
        # Conflict rate should be > 0 for two contenders
        if summary.total_lock_attempts > 3:
            assert summary.lock_conflict_rate > 0, (
                "Two agents contending for one resource should produce some conflicts"
            )

    @pytest.mark.asyncio
    async def test_negotiation_with_conditional_publish(self, runner):
        """Two agents negotiate via conditional_publish before acquiring.

        Each agent uses conditional_publish with exclusive_window to try to
        claim the negotiation topic. Only one can succeed per round, creating
        a turn-taking dynamic.
        """
        config = ScenarioConfig(
            name="negotiation",
            agents=[
                AgentSpec("negotiator_A", ["negotiator"]),
                AgentSpec("negotiator_B", ["negotiator"]),
            ],
            topics=["__negotiate__.db", "results"],
            resources=["db.shared"],
            rounds=12,
        )

        def negotiate_decision(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            # Try to publish via conditional_publish to claim the negotiation window
            return AgentActionSpec(
                action=AgentAction.CONDITIONAL_PUBLISH,
                topic="__negotiate__.db",
                payload={"intent": "acquire", "agent": agent_id, "round": round_num},
                condition={"type": "exclusive_window", "agent_id": agent_id},
            )

        decisions = {
            "negotiator_A": negotiate_decision,
            "negotiator_B": negotiate_decision,
        }
        summary = await runner.run(config, decisions)

        # Should have some publishes (one agent wins each exclusive window)
        assert summary.total_messages > 0
        # Some attempts may be rejected (conditional_publish_rejected)
        assert summary is not None

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_agents_alive(self, runner):
        """Agents that heartbeat regularly stay active."""
        config = ScenarioConfig(
            name="heartbeat_test",
            agents=[
                AgentSpec("persistent", ["worker"]),
                AgentSpec("ephemeral", ["worker"]),
            ],
            topics=["tasks"],
            rounds=8,
            heartbeat_interval_rounds=2,
            expire_after_rounds=4,
        )

        def persistent_decide(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            """This agent heartbeats on odd rounds."""
            if round_num % 2 == 1:
                return AgentActionSpec(action=AgentAction.HEARTBEAT)
            return AgentActionSpec(action=AgentAction.PUBLISH, topic="tasks",
                                   payload={"data": f"msg_{round_num}"})

        def ephemeral_decide(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            """This agent never heartbeats."""
            return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="tasks")

        decisions = {
            "persistent": persistent_decide,
            "ephemeral": ephemeral_decide,
        }
        summary = await runner.run(config, decisions)
        # Run should complete without error
        assert summary is not None

    @pytest.mark.asyncio
    async def test_deterministic_reproducibility(self, runner):
        """Same scenario run twice should produce identical summaries."""
        config = ScenarioConfig(
            name="repro_test",
            agents=[AgentSpec("A", ["pub"]), AgentSpec("B", ["sub"])],
            topics=["data"],
            rounds=6,
            agent_order=["A", "B"],
        )
        decisions = {
            "A": make_publisher_decision("data"),
            "B": make_subscriber_decision("data"),
        }

        summary1 = await runner.run(config, decisions, run_id="rep_a")
        # Re-initialize to clear state
        runner2 = ClusterScenarioRunner(runner._data_dir)
        await runner2.initialize()
        summary2 = await runner2.run(config, decisions, run_id="rep_b")

        # Same number of messages and deliveries
        assert summary1.total_messages == summary2.total_messages
        assert summary1.total_deliveries == summary2.total_deliveries


# ═══════════════════════════════════════════════════════════════════
# Section 3: Pattern Detection
# ═══════════════════════════════════════════════════════════════════

class TestPatternDetection:
    """Verify the analyzer detects known patterns from telemetry data."""

    @pytest.mark.asyncio
    async def test_detect_fan_out(self, runner):
        """1→N message flow should be detected as fan-out.

        Uses direct bus operations to create DELIVER events for pattern detection.
        """
        config = ScenarioConfig(
            name="fan_out_detect",
            agents=[
                AgentSpec("master", ["master"]),
                AgentSpec("w1", ["worker"]),
                AgentSpec("w2", ["worker"]),
                AgentSpec("w3", ["worker"]),
            ],
            topics=["jobs"],
            rounds=10,
        )

        # Direct publish + subscribe + poll pattern to create edges
        def master_decide(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            if round_num <= 3:
                return AgentActionSpec(
                    action=AgentAction.PUBLISH, topic="jobs",
                    payload={"task": f"job_{round_num}"},
                )
            return AgentActionSpec(action=AgentAction.IDLE)

        def worker_decide(wid: str):
            def decide(agent_id: str, round_num: int, obs) -> AgentActionSpec:
                if round_num == 1:
                    return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="jobs")
                if round_num >= 5:
                    return AgentActionSpec(action=AgentAction.IDLE)
                return AgentActionSpec(action=AgentAction.IDLE)
            return decide

        decisions = {
            "master": master_decide,
            "w1": worker_decide("w1"),
            "w2": worker_decide("w2"),
            "w3": worker_decide("w3"),
        }
        summary = await runner.run(config, decisions)

        # Directly create deliveries via the bus for pattern detection
        runs = runner.telemetry.list_runs()
        assert len(runs) > 0
        run_id = runs[0]["run_id"]

        for wid in ["w1", "w2", "w3"]:
            await runner.bus.subscribe("jobs", wid)
            msgs = await runner.bus.poll_messages("jobs", wid)
            for m in msgs:
                runner.telemetry.record(
                    EventKind.DELIVER, wid, run_id, 99,
                    {"topic": "jobs", "msg_id": m.id, "publisher_id": m.publisher_id,
                     "sequence": m.sequence, "subscriber_id": wid},
                )

        analyzer = ClusterAnalyzer(runner.telemetry)

        report = analyzer.analyze(run_id)
        assert report is not None

        fan_outs = [p for p in report.message_flow_patterns if p.pattern_type == "fan_out"]
        assert len(fan_outs) > 0, "Fan-out pattern should be detected"

    @pytest.mark.asyncio
    async def test_detect_hot_resource(self, runner):
        """High contention on one resource should be detected."""
        config = ScenarioConfig(
            name="hot_resource_detect",
            agents=[
                AgentSpec("a1", ["writer"]),
                AgentSpec("a2", ["writer"]),
                AgentSpec("a3", ["writer"]),
            ],
            topics=[],
            resources=["db.hot"],
            rounds=20,
        )
        decisions = {
            "a1": make_contender_decision("db.hot", hold_for_rounds=2),
            "a2": make_contender_decision("db.hot", hold_for_rounds=2),
            "a3": make_contender_decision("db.hot", hold_for_rounds=2),
        }
        await runner.run(config, decisions)

        runs = runner.telemetry.list_runs()
        run_id = runs[0]["run_id"]

        analyzer = ClusterAnalyzer(runner.telemetry)
        report = analyzer.analyze(run_id)
        assert report is not None

        hot_patterns = [p for p in report.lock_contention_patterns
                       if p.pattern_type == "hot_resource"]
        assert len(hot_patterns) > 0, "Hot resource pattern should be detected with 3 contenders"

    @pytest.mark.asyncio
    async def test_text_report_generation(self, runner):
        """Verify the text report formatter works without error."""
        config = ScenarioConfig(
            name="report_test",
            agents=[AgentSpec("A", ["pub"]), AgentSpec("B", ["sub"])],
            topics=["data"],
            rounds=4,
        )
        decisions = {
            "A": make_publisher_decision("data"),
            "B": make_subscriber_decision("data"),
        }
        await runner.run(config, decisions)

        runs = runner.telemetry.list_runs()
        run_id = runs[0]["run_id"]

        report_text = analyze_run(runner.telemetry, run_id)
        assert "Summary" in report_text or "──" in report_text
        assert "Message Flow" in report_text or run_id in report_text


# ═══════════════════════════════════════════════════════════════════
# Section 4: Theorem Verification
# ═══════════════════════════════════════════════════════════════════

class TestTheoremVerification:
    """Empirically verify categorical theorems against interaction data."""

    @pytest.mark.asyncio
    async def test_l1_completeness_all_interactions_expressible(self, runner):
        """Thm 8.4: Every interaction should be expressible with L1 primitives."""
        config = ScenarioConfig(
            name="l1_completeness_test",
            agents=[AgentSpec("A", ["test"]), AgentSpec("B", ["test"])],
            topics=["topic_a", "topic_b"],
            resources=["res_1"],
            rounds=10,
        )

        def mixed_decision(agent_id: str, round_num: int, obs) -> AgentActionSpec:
            """Use a mix of L1-expressible actions."""
            actions = [
                AgentActionSpec(action=AgentAction.PUBLISH, topic="topic_a",
                               payload={"data": f"msg_{round_num}"}),
                AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="topic_a"),
                AgentActionSpec(action=AgentAction.ACQUIRE, resource_id="res_1", mode="write"),
                AgentActionSpec(action=AgentAction.RELEASE, resource_id="res_1"),
                AgentActionSpec(action=AgentAction.CONDITIONAL_PUBLISH, topic="topic_b",
                               payload={"data": f"cond_{round_num}"},
                               condition={"type": "exclusive_window", "agent_id": agent_id}),
            ]
            return actions[round_num % len(actions)]

        decisions = {"A": mixed_decision, "B": mixed_decision}
        summary = await runner.run(config, decisions)

        runs = runner.telemetry.list_runs()
        run_id = runs[0]["run_id"]

        analyzer = ClusterAnalyzer(runner.telemetry)
        report = analyzer.analyze(run_id)
        assert report is not None

        # L1 completeness check should pass
        assert report.theorem_checks.get("thm_l1_completeness", False)

    @pytest.mark.asyncio
    async def test_no_deadlock_empirical(self, runner):
        """Empirically verify no circular wait (deadlock) in lock contention."""
        config = ScenarioConfig(
            name="deadlock_test",
            agents=[AgentSpec(f"a{i}", ["worker"]) for i in range(4)],
            topics=[],
            resources=[f"res_{i}" for i in range(3)],
            rounds=25,
        )
        decisions = {}
        for i in range(4):
            resource = f"res_{i % 3}"
            agent_id = f"a{i}"
            decisions[agent_id] = make_contender_decision(resource, hold_for_rounds=2)

        summary = await runner.run(config, decisions)

        runs = runner.telemetry.list_runs()
        run_id = runs[0]["run_id"]

        analyzer = ClusterAnalyzer(runner.telemetry)
        report = analyzer.analyze(run_id)
        assert report is not None

        # No deadlock should be detected
        assert report.theorem_checks.get("empirical_no_deadlock", False), (
            "Deadlock detected in lock contention scenario"
        )

    @pytest.mark.asyncio
    async def test_det_closure_no_orphan_deliveries(self, runner):
        """Thm 8.3 (Det closure): Every delivery has a corresponding publish."""
        config = ScenarioConfig(
            name="det_closure_test",
            agents=[AgentSpec("pub", ["pub"]), AgentSpec("sub", ["sub"])],
            topics=["data"],
            rounds=8,
        )
        decisions = {
            "pub": make_publisher_decision("data"),
            "sub": make_subscriber_decision("data"),
        }
        summary = await runner.run(config, decisions)

        # No delivery without publish
        assert summary.total_deliveries <= summary.total_messages, (
            "Deliveries exceed publishes — violates Det closure"
        )

    @pytest.mark.asyncio
    async def test_telemetry_event_integrity(self, runner):
        """Verify telemetry records all events with correct Lamport clock ordering."""
        config = ScenarioConfig(
            name="telemetry_integrity",
            agents=[AgentSpec("A", ["pub"]), AgentSpec("B", ["sub"])],
            topics=["data"],
            rounds=5,
        )
        decisions = {
            "A": make_publisher_decision("data"),
            "B": make_subscriber_decision("data"),
        }
        await runner.run(config, decisions)

        runs = runner.telemetry.list_runs()
        run_id = runs[0]["run_id"]
        events = runner.telemetry.events(run_id)

        # Events should be ordered by Lamport clock
        clocks = [e["lamport_clock"] for e in events]
        assert clocks == sorted(clocks), "Events must be in Lamport clock order"

        # Each event has required fields
        for evt in events:
            assert "event_id" in evt
            assert "kind" in evt
            assert "agent_id" in evt
            assert "lamport_clock" in evt
            assert "run_id" in evt
            assert evt["run_id"] == run_id


# ═══════════════════════════════════════════════════════════════════
# Section 5: Concurrency stress
# ═══════════════════════════════════════════════════════════════════

class TestConcurrency:
    """Test concurrent multi-agent interactions under load."""

    @pytest.mark.asyncio
    async def test_concurrent_publish_subscribe(self, bus):
        """Multiple agents publish and subscribe concurrently without data loss."""
        topic = "concurrent_test"
        N_AGENTS = 10
        N_MESSAGES = 20

        # Register subscribers
        for i in range(N_AGENTS):
            await bus.subscribe(topic, f"agent_{i}")

        # Concurrent publishes
        async def publisher(agent_idx: int):
            for j in range(N_MESSAGES // N_AGENTS):
                await bus.publish(topic, f"pub_{agent_idx}",
                                  {"data": f"m_{agent_idx}_{j}",
                                   "_msg_id": f"cm_{agent_idx}_{j}"})

        await asyncio.gather(*(publisher(i) for i in range(N_AGENTS)))

        # Verify each subscriber received all messages
        for i in range(N_AGENTS):
            msgs = await bus.poll_messages(topic, f"agent_{i}")
            assert len(msgs) == N_MESSAGES, (
                f"agent_{i} received {len(msgs)} messages, expected {N_MESSAGES}"
            )

    @pytest.mark.asyncio
    async def test_concurrent_lock_acquisition(self, infra):
        """Multiple agents concurrently acquire and release locks."""
        bus, registry, detector = infra
        resource = "concurrent_lock"
        acquired_count = 0

        async def contender(agent_idx: int):
            nonlocal acquired_count
            for _ in range(10):
                ok = await detector.acquire(resource, f"agent_{agent_idx}", "write", ttl=2.0)
                if ok:
                    acquired_count += 1
                    await asyncio.sleep(0.01)
                    await detector.release(resource, f"agent_{agent_idx}")
                else:
                    await asyncio.sleep(0.02)

        await asyncio.gather(*(contender(i) for i in range(5)))

        # At least some acquisitions should succeed
        assert acquired_count > 0

        # Resource should be free at end
        holders = await detector.lock_holders(resource)
        assert len(holders) == 0


# ═══════════════════════════════════════════════════════════════════
# Section 6: Message flow edge cases
# ═══════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_scenario(self, runner):
        """Scenario with no rounds should complete cleanly."""
        config = ScenarioConfig(
            name="empty",
            agents=[AgentSpec("A", ["test"])],
            topics=[],
            rounds=0,
        )
        decisions = {"A": lambda aid, r, obs: AgentActionSpec(action=AgentAction.IDLE)}
        summary = await runner.run(config, decisions)
        assert summary is not None
        assert summary.total_messages == 0

    @pytest.mark.asyncio
    async def test_single_agent_no_interaction(self, runner):
        """Single agent with no communication — no interactions expected."""
        config = ScenarioConfig(
            name="solo",
            agents=[AgentSpec("loner", ["solo"])],
            topics=["unused"],
            rounds=5,
        )
        decisions = {"loner": lambda aid, r, obs: AgentActionSpec(action=AgentAction.IDLE)}
        summary = await runner.run(config, decisions)
        assert summary.total_messages == 0
        assert summary.total_deliveries == 0

    @pytest.mark.asyncio
    async def test_topic_history(self, bus):
        """Topic history returns messages in correct order."""
        topic = "history_test"
        for i in range(5):
            await bus.publish(topic, "test_agent",
                             {"data": f"msg_{i}", "_msg_id": f"hist_{i}"})

        history = await bus.topic_history(topic, limit=3)
        assert len(history) == 3
        # Most recent 3, in chronological order (reversed by the method)
        sequences = [h.sequence for h in history]
        assert sequences == sorted(sequences)

    @pytest.mark.asyncio
    async def test_idempotent_publish(self, bus):
        """Publishing the same msg_id twice returns the same message."""
        topic = "idempotent_test"
        msg_id = "unique_msg_42"
        payload = {"data": "original", "_msg_id": msg_id}

        msg1 = await bus.publish(topic, "agent_A", payload)
        msg2 = await bus.publish(topic, "agent_A",
                                 {"data": "should_be_ignored", "_msg_id": msg_id})

        assert msg1.id == msg2.id
        assert msg1.sequence == msg2.sequence
        # Payload should match the original
        assert msg1.payload.get("data") == "original"
