"""Deterministic multi-agent scenario runner.

Runs configurable multi-agent interaction scenarios on the cluster infrastructure
with full telemetry recording. Uses round-based deterministic scheduling to make
interaction traces reproducible for pattern analysis.

Architecture (from The Pair categorical formalization):
  Harness: round scheduler, deterministic agent ordering, telemetry recording
  LLM: agent decision function (mock or real) — what each agent chooses to do

Each scenario defines:
  - N agents with capabilities
  - M topics for communication
  - K resources for lock contention
  - Agent decision functions (mock or LLM-backed)
  - Number of rounds to run
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from .cluster_messaging import (
    AgentRegistration,
    ClusterMessage,
    ClusterMessageBus,
    ClusterRegistry,
    ConflictDetector,
    ResourceLock,
    create_cluster_infrastructure,
)
from .cluster_telemetry import ClusterTelemetry, EventKind, InteractionSummary


# ── Agent action model ─────────────────────────────────────────────

class AgentAction(str, Enum):
    """Actions an agent can take in one round."""
    PUBLISH = "publish"
    SUBSCRIBE_AWAIT = "subscribe_await"
    SUBSCRIBE_POLL = "subscribe_poll"
    REGISTER = "register"
    DISCOVER = "discover"
    ACQUIRE = "acquire"
    RELEASE = "release"
    UPGRADE_LOCK = "upgrade_lock"
    CONDITIONAL_PUBLISH = "conditional_publish"
    HEARTBEAT = "heartbeat"
    IDLE = "idle"


@dataclass
class AgentActionSpec:
    """A concrete action specification for one round."""
    action: AgentAction
    topic: str = ""
    resource_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    mode: Literal["read", "write"] = "write"
    ttl: float = 300.0
    capability: str = ""
    condition: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentSpec:
    """Definition of an agent in a scenario."""
    agent_id: str
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioConfig:
    """Configuration for a multi-agent scenario run."""
    name: str
    description: str = ""
    agents: list[AgentSpec] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    rounds: int = 20
    agent_order: list[str] | None = None  # deterministic ordering; None = alphabetical
    heartbeat_interval_rounds: int = 5
    expire_after_rounds: int = 10  # rounds without heartbeat before agent considered dead


# ── Decision function types ────────────────────────────────────────

# A decision function takes (agent_id, round_number, observation) → AgentActionSpec
DecisionFn = Callable[[str, int, dict[str, Any]], AgentActionSpec]


# ── Observation model (what an agent can see at decision time) ─────

@dataclass
class AgentObservation:
    """What an agent can observe at the start of a round."""
    agent_id: str
    round_number: int
    pending_messages: list[dict[str, Any]]  # messages waiting on subscribed topics
    held_locks: list[dict[str, Any]]
    known_agents: list[dict[str, Any]]  # from last discovery
    recent_events: list[dict[str, Any]]  # last N events relevant to this agent


# ── Scenario runner ────────────────────────────────────────────────

class ClusterScenarioRunner:
    """Deterministic multi-agent scenario runner.

    Usage:
        runner = ClusterScenarioRunner(data_dir)
        await runner.initialize()

        config = ScenarioConfig(
            name="basic_pubsub",
            agents=[AgentSpec("A"), AgentSpec("B")],
            topics=["tasks"],
            rounds=10,
        )

        # Mock decision: A publishes, B subscribes
        def decide_a(agent_id, round_num, obs):
            return AgentActionSpec(action=AgentAction.PUBLISH, topic="tasks",
                                   payload={"msg": f"hello_{round_num}"})
        def decide_b(agent_id, round_num, obs):
            return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic="tasks")

        decisions = {"A": decide_a, "B": decide_b}
        summary = await runner.run(config, decisions)
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._bus: ClusterMessageBus | None = None
        self._registry: ClusterRegistry | None = None
        self._detector: ConflictDetector | None = None
        self._telemetry: ClusterTelemetry | None = None

    async def initialize(self) -> None:
        bus, registry, detector = await create_cluster_infrastructure(self._data_dir)
        self._bus = bus
        self._registry = registry
        self._detector = detector
        self._telemetry = ClusterTelemetry(self._data_dir)
        self._telemetry.initialize_sync()

    @property
    def bus(self) -> ClusterMessageBus:
        if self._bus is None:
            raise RuntimeError("ClusterScenarioRunner not initialized")
        return self._bus

    @property
    def registry(self) -> ClusterRegistry:
        if self._registry is None:
            raise RuntimeError("ClusterScenarioRunner not initialized")
        return self._registry

    @property
    def detector(self) -> ConflictDetector:
        if self._detector is None:
            raise RuntimeError("ClusterScenarioRunner not initialized")
        return self._detector

    @property
    def telemetry(self) -> ClusterTelemetry:
        if self._telemetry is None:
            raise RuntimeError("ClusterScenarioRunner not initialized")
        return self._telemetry

    # ── Main run loop ──────────────────────────────────────────────

    async def run(
        self,
        config: ScenarioConfig,
        decisions: dict[str, DecisionFn],
        *,
        run_id: str | None = None,
    ) -> InteractionSummary:
        """Run a complete multi-agent scenario.

        Args:
            config: scenario definition
            decisions: per-agent decision functions agent_id → DecisionFn
            run_id: optional run identifier (auto-generated if None)

        Returns:
            InteractionSummary with aggregate statistics
        """
        run_id = run_id or uuid4().hex[:12]
        agent_order = config.agent_order or sorted(a.agent_id for a in config.agents)
        agent_map = {a.agent_id: a for a in config.agents}

        # Start telemetry run
        self.telemetry.start_run(run_id, config.name, {
            "agent_count": len(config.agents),
            "topic_count": len(config.topics),
            "resource_count": len(config.resources),
            "rounds": config.rounds,
            "agent_order": agent_order,
            "description": config.description,
        })

        # Per-agent state
        subscriptions: dict[str, set[str]] = defaultdict(set)  # agent_id → subscribed topics
        active_agents: set[str] = set()
        last_heartbeat: dict[str, int] = {}
        agent_observations: dict[str, AgentObservation] = {}

        # Register all agents
        for agent_spec in config.agents:
            await self._execute_register(config, agent_spec, run_id)
            active_agents.add(agent_spec.agent_id)
            last_heartbeat[agent_spec.agent_id] = 0

        # Ensure topics
        for topic in config.topics:
            await self.bus.ensure_topic(topic)

        # ── Round loop ─────────────────────────────────────────────
        for round_num in range(1, config.rounds + 1):
            # Heartbeat and expiry
            if round_num % config.heartbeat_interval_rounds == 0:
                for aid in list(active_agents):
                    await self._execute_heartbeat(aid, config, run_id, round_num)
                    last_heartbeat[aid] = round_num

            expired = await self.registry.expire_inactive_agents(
                heartbeat_ttl=config.expire_after_rounds * 0.1  # approximate
            )
            for aid in expired:
                if aid in active_agents:
                    active_agents.discard(aid)
                    self.telemetry.record(
                        EventKind.AGENT_EXPIRED, aid, run_id, round_num,
                        {"last_heartbeat_round": last_heartbeat.get(aid, 0)},
                    )

            # Build observations for each active agent
            for aid in agent_order:
                if aid not in active_agents:
                    continue
                agent_observations[aid] = await self._build_observation(
                    aid, round_num, subscriptions.get(aid, set())
                )

            # Each active agent makes one decision and executes it
            for aid in agent_order:
                if aid not in active_agents or aid not in decisions:
                    continue
                obs = agent_observations.get(aid)
                if obs is None:
                    obs = await self._build_observation(aid, round_num, subscriptions.get(aid, set()))

                try:
                    action_spec = decisions[aid](aid, round_num, obs)
                except Exception as exc:
                    self.telemetry.record(
                        EventKind.AGENT_STOP, aid, run_id, round_num,
                        {"error": str(exc)},
                    )
                    active_agents.discard(aid)
                    continue

                # Track new subscriptions
                if action_spec.action == AgentAction.SUBSCRIBE_AWAIT:
                    subscriptions[aid].add(action_spec.topic)
                elif action_spec.action == AgentAction.SUBSCRIBE_POLL:
                    subscriptions[aid].add(action_spec.topic)

                # Execute the action
                await self._execute_action(aid, action_spec, config, run_id, round_num)

        # ── Cleanup ────────────────────────────────────────────────
        for agent_spec in config.agents:
            self.telemetry.record(
                EventKind.AGENT_STOP, agent_spec.agent_id, run_id, config.rounds + 1,
                {"status": "scenario_complete"},
            )

        self.telemetry.finish_run(run_id)
        summary = self.telemetry.summarize(run_id)
        if summary is None:
            raise RuntimeError(f"No telemetry data for run {run_id}")
        return summary

    # ── Action execution ───────────────────────────────────────────

    async def _execute_action(
        self, agent_id: str, spec: AgentActionSpec,
        config: ScenarioConfig, run_id: str, step: int,
    ) -> None:
        """Execute a single agent action and record telemetry."""
        telemetry = self.telemetry

        if spec.action == AgentAction.IDLE:
            return

        elif spec.action == AgentAction.PUBLISH:
            topic = spec.topic or config.topics[0]
            publish_payload = spec.payload or {"data": f"msg_from_{agent_id}_step_{step}"}
            publish_payload.setdefault("_msg_id", f"{run_id}_{agent_id}_{step}")
            msg = await self.bus.publish(topic, agent_id, publish_payload)
            # Include key payload fields in telemetry for analysis
            telemetry_payload: dict[str, Any] = {
                "topic": topic, "msg_id": msg.id,
                "sequence": msg.sequence, "payload_size": len(json.dumps(publish_payload)),
            }
            # Carry domain-specific fields into telemetry for pattern detection
            for key in ("task_type", "task_id", "priority", "producer_id",
                       "required_capability", "worker_id", "claim_round",
                       "quality", "result", "observer"):
                if key in publish_payload:
                    telemetry_payload[key] = publish_payload[key]
            telemetry.record(EventKind.PUBLISH, agent_id, run_id, step, telemetry_payload)

        elif spec.action == AgentAction.SUBSCRIBE_AWAIT:
            topic = spec.topic or config.topics[0]
            await self.bus.subscribe(topic, agent_id)
            msg = await self.bus.await_message(topic, agent_id, timeout=2.0)
            telemetry.record(EventKind.SUBSCRIBE, agent_id, run_id, step, {
                "topic": topic,
            })
            if msg:
                telemetry.record(EventKind.DELIVER, agent_id, run_id, step, {
                    "topic": topic, "msg_id": msg.id,
                    "publisher_id": msg.publisher_id, "sequence": msg.sequence,
                    "subscriber_id": agent_id,
                })

        elif spec.action == AgentAction.SUBSCRIBE_POLL:
            topic = spec.topic or config.topics[0]
            await self.bus.subscribe(topic, agent_id)
            # Don't consume messages here — observation uses peek.
            # Agents see all pending messages via _build_observation.
            telemetry.record(EventKind.POLL, agent_id, run_id, step, {
                "topic": topic, "message_count": 0,
            })

        elif spec.action == AgentAction.REGISTER:
            await self._execute_register(
                config,
                AgentSpec(agent_id=agent_id, capabilities=spec.capability.split(",") if spec.capability else ["generic"]),
                run_id,
            )

        elif spec.action == AgentAction.DISCOVER:
            capability = spec.capability or ""
            agents = await self.registry.discover(capability if capability else None)
            telemetry.record(EventKind.DISCOVER, agent_id, run_id, step, {
                "capability": capability, "found": len(agents),
                "agent_ids": [a.agent_id for a in agents],
            })

        elif spec.action == AgentAction.ACQUIRE:
            resource_id = spec.resource_id or (config.resources[0] if config.resources else "default")
            acquired = await self.detector.acquire(resource_id, agent_id, spec.mode, spec.ttl)
            if acquired:
                telemetry.record(EventKind.ACQUIRE, agent_id, run_id, step, {
                    "resource_id": resource_id, "mode": spec.mode,
                })
            else:
                # Record who holds the lock
                holders = await self.detector.lock_holders(resource_id)
                telemetry.record(EventKind.ACQUIRE_DENIED, agent_id, run_id, step, {
                    "resource_id": resource_id, "mode": spec.mode,
                    "holder": holders[0].owner_id if holders else "unknown",
                    "holder_mode": holders[0].mode if holders else "unknown",
                })

        elif spec.action == AgentAction.RELEASE:
            resource_id = spec.resource_id or (config.resources[0] if config.resources else "default")
            await self.detector.release(resource_id, agent_id)
            telemetry.record(EventKind.RELEASE, agent_id, run_id, step, {
                "resource_id": resource_id,
            })

        elif spec.action == AgentAction.UPGRADE_LOCK:
            resource_id = spec.resource_id or (config.resources[0] if config.resources else "default")
            upgraded = await self.detector.upgrade_lock(
                resource_id, agent_id, "read", "write",
            )
            telemetry.record(EventKind.LOCK_UPGRADE if upgraded else EventKind.ACQUIRE_DENIED,
                           agent_id, run_id, step, {
                "resource_id": resource_id, "from": "read", "to": "write",
                "success": upgraded,
            })

        elif spec.action == AgentAction.CONDITIONAL_PUBLISH:
            topic = spec.topic or config.topics[0]
            cp_payload = spec.payload or {"data": f"cond_msg_{agent_id}_{step}"}
            cp_payload.setdefault("_msg_id", f"{run_id}_{agent_id}_cond_{step}")
            condition = spec.condition or {"type": "exclusive_window", "agent_id": agent_id}
            accepted, msg = await self.bus.conditional_publish(topic, agent_id, cp_payload, condition)
            telemetry_payload_cp: dict[str, Any] = {
                "topic": topic, "condition_type": condition.get("type", ""),
            }
            if accepted and msg:
                telemetry_payload_cp["msg_id"] = msg.id
                for key in ("task_type", "task_id", "worker_id", "claim_round", "score"):
                    if key in cp_payload:
                        telemetry_payload_cp[key] = cp_payload[key]
                telemetry.record(EventKind.CONDITIONAL_PUBLISH, agent_id, run_id, step, telemetry_payload_cp)
            else:
                telemetry.record(EventKind.CONDITIONAL_PUBLISH_REJECTED, agent_id, run_id, step, telemetry_payload_cp)

        elif spec.action == AgentAction.HEARTBEAT:
            await self._execute_heartbeat(agent_id, config, run_id, step)

    # ── Helpers ────────────────────────────────────────────────────

    async def _execute_register(
        self, config: ScenarioConfig, agent: AgentSpec, run_id: str,
    ) -> None:
        await self.registry.register(agent.agent_id, agent.capabilities, agent.metadata)
        self.telemetry.record(EventKind.REGISTER, agent.agent_id, run_id, 0, {
            "capabilities": agent.capabilities,
        })

    async def _execute_heartbeat(
        self, agent_id: str, config: ScenarioConfig, run_id: str, step: int,
    ) -> None:
        ok = await self.registry.heartbeat(agent_id)
        self.telemetry.record(EventKind.AGENT_HEARTBEAT, agent_id, run_id, step, {
            "alive": ok,
        })

    async def _build_observation(
        self, agent_id: str, round_num: int, topics: set[str],
    ) -> AgentObservation:
        """Build what an agent can see at the start of a round."""
        pending = []
        for topic in topics:
            # Use peek (non-consuming) to build observation
            try:
                msgs = await self.bus.peek_messages(topic, agent_id)
                for m in msgs:
                    pending.append({
                        "topic": topic, "msg_id": m.id,
                        "publisher_id": m.publisher_id,
                        "sequence": m.sequence,
                        "payload": m.payload,
                    })
            except Exception:
                pass

        # Held locks
        held_locks = []
        all_locks = await self.detector.list_locks()
        for lock in all_locks:
            if lock.owner_id == agent_id:
                held_locks.append({
                    "resource_id": lock.resource_id,
                    "mode": lock.mode,
                    "acquired_at": lock.acquired_at,
                })

        # Known agents
        agents = await self.registry.discover()
        known = [{"agent_id": a.agent_id, "capabilities": a.capabilities, "status": a.status}
                 for a in agents if a.agent_id != agent_id]

        return AgentObservation(
            agent_id=agent_id,
            round_number=round_num,
            pending_messages=pending,
            held_locks=held_locks,
            known_agents=known,
            recent_events=[],
        )


# ── Built-in decision functions for common agent roles ─────────────

def make_publisher_decision(
    topic: str,
    messages_per_round: int = 1,
) -> DecisionFn:
    """Create a decision function for an agent that publishes to a topic every round."""
    def decide(agent_id: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        return AgentActionSpec(
            action=AgentAction.PUBLISH,
            topic=topic,
            payload={"from": agent_id, "round": round_num, "content": f"task_{round_num}"},
        )
    return decide


def make_subscriber_decision(
    topic: str,
    poll_mode: bool = True,
) -> DecisionFn:
    """Create a decision function for an agent that subscribes and polls a topic."""
    def decide(agent_id: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        if poll_mode:
            return AgentActionSpec(action=AgentAction.SUBSCRIBE_POLL, topic=topic)
        return AgentActionSpec(action=AgentAction.SUBSCRIBE_AWAIT, topic=topic)
    return decide


def make_contender_decision(
    resource_id: str,
    hold_for_rounds: int = 3,
) -> DecisionFn:
    """Create a decision function for an agent that contends for a resource lock.

    Strategy: try to acquire, hold for N rounds, release.
    On conflict: wait one round, retry.
    """
    acquired_round: dict[str, int] = {}

    def decide(agent_id: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        # Check if we already hold the lock
        for lock in obs.held_locks:
            if lock["resource_id"] == resource_id:
                if agent_id not in acquired_round:
                    acquired_round[agent_id] = round_num
                # Hold for N rounds then release
                if round_num - acquired_round.get(agent_id, round_num) >= hold_for_rounds:
                    acquired_round.pop(agent_id, None)
                    return AgentActionSpec(action=AgentAction.RELEASE, resource_id=resource_id)
                return AgentActionSpec(action=AgentAction.IDLE)

        # Try to acquire
        return AgentActionSpec(action=AgentAction.ACQUIRE, resource_id=resource_id, mode="write")

    return decide


def make_negotiating_decision(
    negotiate_topic: str,
    resource_id: str,
) -> DecisionFn:
    """Create a decision function for a negotiating agent.

    Uses conditional_publish for negotiation (L1 primitive):
      - Publish intent to negotiate topic
      - If exclusive window granted, acquire lock
      - Otherwise wait
    """
    has_lock: dict[str, bool] = {}

    def decide(agent_id: str, round_num: int, obs: AgentObservation) -> AgentActionSpec:
        # Check if we have the lock
        if has_lock.get(agent_id):
            return AgentActionSpec(action=AgentAction.RELEASE, resource_id=resource_id)

        # Check if we received intent from others
        for msg in obs.pending_messages:
            if msg.get("topic") == negotiate_topic:
                # Someone else wants the resource — negotiate
                return AgentActionSpec(
                    action=AgentAction.CONDITIONAL_PUBLISH,
                    topic=negotiate_topic,
                    payload={"intent": "acquire", "agent": agent_id, "round": round_num},
                    condition={"type": "exclusive_window", "agent_id": agent_id},
                )

        # First round: publish intent
        return AgentActionSpec(
            action=AgentAction.CONDITIONAL_PUBLISH,
            topic=negotiate_topic,
            payload={"intent": "acquire", "agent": agent_id, "round": round_num},
            condition={"type": "exclusive_window", "agent_id": agent_id},
        )

    return decide
