"""Cluster telemetry — deterministic event log for multi-agent interaction analysis.

Design principle (from The Pair categorical formalization):
  The Harness records *what happened* with deterministic precision.
  The LLM (or analyst) interprets *why it happened* from the recorded data.

Every multi-agent interaction event is captured with:
  - Lamport clock (causal ordering across agents)
  - Wall clock (real-time ordering)
  - Event type and structured payload

This enables:
  1. Reproducible analysis — replay the event log deterministically
  2. Pattern discovery — find recurring interaction motifs
  3. Theorem verification — empirically validate L1 completeness, Det closure, etc.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal


class EventKind(str, Enum):
    # ── Lifecycle ──
    AGENT_START = "agent.start"
    AGENT_STOP = "agent.stop"
    AGENT_HEARTBEAT = "agent.heartbeat"
    AGENT_EXPIRED = "agent.expired"

    # ── Registry ──
    REGISTER = "register"
    UNREGISTER = "unregister"
    DISCOVER = "discover"

    # ── Messaging ──
    PUBLISH = "publish"
    DELIVER = "deliver"          # message delivered to a specific subscriber
    SUBSCRIBE = "subscribe"
    POLL = "poll"

    # ── Locking ──
    ACQUIRE = "acquire"
    ACQUIRE_DENIED = "acquire.denied"
    RELEASE = "release"
    LOCK_UPGRADE = "lock.upgrade"
    LOCK_EXPIRE = "lock.expire"

    # ── Conditional publish (L1 completeness primitive) ──
    CONDITIONAL_PUBLISH = "conditional_publish"
    CONDITIONAL_PUBLISH_REJECTED = "conditional_publish.rejected"


@dataclass(slots=True)
class TelemetryEvent:
    """A single interaction event in the cluster.

    Attributes:
        event_id: unique event identifier
        kind: event type
        agent_id: which agent emitted this event
        lamport_clock: Lamport logical clock value (causal ordering)
        wall_clock: real time (seconds since epoch)
        run_id: which scenario run this belongs to
        scenario_step: which deterministic step in the scenario
        payload: structured event data
    """
    event_id: str
    kind: EventKind
    agent_id: str
    lamport_clock: int
    wall_clock: float
    run_id: str
    scenario_step: int
    payload: dict[str, Any] = field(default_factory=dict)


# ── Interaction graph models (for pattern discovery) ──────────────

@dataclass(slots=True)
class MessageEdge:
    """A directed edge in the message flow graph: A →publish→ topic →deliver→ B"""
    from_agent: str
    to_agent: str
    topic: str
    msg_id: str
    sequence: int
    publish_time: float
    deliver_time: float
    latency_ms: float


@dataclass(slots=True)
class LockContention:
    """A lock conflict event: agent tried to acquire, was denied."""
    resource_id: str
    requester: str
    holder: str
    requested_mode: str
    holder_mode: str
    timestamp: float


@dataclass(slots=True)
class InteractionSummary:
    """Aggregated statistics for a scenario run."""
    run_id: str
    agent_count: int
    topic_count: int
    total_messages: int
    total_deliveries: int
    total_lock_attempts: int
    total_lock_conflicts: int
    total_lock_upgrades: int
    avg_message_latency_ms: float
    max_message_latency_ms: float
    lock_conflict_rate: float
    message_flow_graph: dict[str, list[str]]  # agent → [recipients]
    duration_seconds: float


# ── Telemetry Store ───────────────────────────────────────────────

class ClusterTelemetry:
    """SQLite-backed telemetry store for multi-agent interaction analysis.

    Shares the same DB file as ClusterMessageBus (cluster_bus.db),
    using separate tables to avoid interference with operational data.
    """

    def __init__(self, data_dir: Path) -> None:
        self._db_path = data_dir / "cluster_bus.db"
        self._lamport_clock: int = 0

    # ── Lifecycle ──────────────────────────────────────────────────

    def initialize_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS telemetry_events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    lamport_clock INTEGER NOT NULL,
                    wall_clock REAL NOT NULL,
                    run_id TEXT NOT NULL,
                    scenario_step INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_run
                    ON telemetry_events(run_id, scenario_step);
                CREATE INDEX IF NOT EXISTS idx_telemetry_agent
                    ON telemetry_events(agent_id, lamport_clock);
                CREATE INDEX IF NOT EXISTS idx_telemetry_kind
                    ON telemetry_events(kind, run_id);

                CREATE TABLE IF NOT EXISTS telemetry_runs (
                    run_id TEXT PRIMARY KEY,
                    scenario_name TEXT NOT NULL,
                    config TEXT NOT NULL DEFAULT '{}',
                    agent_count INTEGER NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL DEFAULT 'running'
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── Event recording ────────────────────────────────────────────

    def tick(self) -> int:
        """Advance Lamport clock and return new value."""
        self._lamport_clock += 1
        return self._lamport_clock

    def merge_clock(self, received_clock: int) -> int:
        """Merge a received Lamport clock value (on message delivery)."""
        self._lamport_clock = max(self._lamport_clock, received_clock) + 1
        return self._lamport_clock

    def record(
        self,
        kind: EventKind,
        agent_id: str,
        run_id: str,
        scenario_step: int,
        payload: dict[str, Any] | None = None,
        *,
        received_clock: int = 0,
    ) -> TelemetryEvent:
        """Record an event with current Lamport clock."""
        if received_clock:
            self.merge_clock(received_clock)
        else:
            self.tick()

        import uuid
        event_id = uuid.uuid4().hex[:16]
        now = time.time()
        evt = TelemetryEvent(
            event_id=event_id,
            kind=kind,
            agent_id=agent_id,
            lamport_clock=self._lamport_clock,
            wall_clock=now,
            run_id=run_id,
            scenario_step=scenario_step,
            payload=payload or {},
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO telemetry_events (event_id, kind, agent_id, lamport_clock,
                   wall_clock, run_id, scenario_step, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (evt.event_id, evt.kind.value, evt.agent_id, evt.lamport_clock,
                 evt.wall_clock, evt.run_id, evt.scenario_step,
                 json.dumps(evt.payload)),
            )
        return evt

    def start_run(self, run_id: str, scenario_name: str, config: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO telemetry_runs (run_id, scenario_name, config, agent_count, started_at, status)
                   VALUES (?, ?, ?, ?, ?, 'running')""",
                (run_id, scenario_name, json.dumps(config), config.get("agent_count", 0), time.time()),
            )

    def finish_run(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE telemetry_runs SET finished_at = ?, status = 'completed' WHERE run_id = ?",
                (time.time(), run_id),
            )

    # ── Query ──────────────────────────────────────────────────────

    def events(self, run_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        """Get all events for a run, ordered by Lamport clock."""
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    """SELECT * FROM telemetry_events
                       WHERE run_id = ? AND kind = ?
                       ORDER BY lamport_clock""",
                    (run_id, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM telemetry_events WHERE run_id = ? ORDER BY lamport_clock",
                    (run_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ── Interaction graph extraction ───────────────────────────────

    def extract_message_edges(self, run_id: str) -> list[MessageEdge]:
        """Extract all message flow edges from publish + deliver events."""
        publishes: dict[str, dict[str, Any]] = {}
        edges: list[MessageEdge] = []

        events = self.events(run_id)
        for evt in events:
            payload = json.loads(evt["payload"]) if isinstance(evt["payload"], str) else evt["payload"]
            if evt["kind"] == EventKind.PUBLISH.value:
                publishes[payload.get("msg_id", "")] = evt
            elif evt["kind"] == EventKind.DELIVER.value:
                pub = publishes.get(payload.get("msg_id", ""))
                if pub:
                    pub_payload = json.loads(pub["payload"]) if isinstance(pub["payload"], str) else pub["payload"]
                    edges.append(MessageEdge(
                        from_agent=pub["agent_id"],  # the publisher, not the subscriber
                        to_agent=payload.get("subscriber_id", evt["agent_id"]),
                        topic=pub_payload.get("topic", ""),
                        msg_id=payload.get("msg_id", ""),
                        sequence=payload.get("sequence", 0),
                        publish_time=pub["wall_clock"],
                        deliver_time=evt["wall_clock"],
                        latency_ms=(evt["wall_clock"] - pub["wall_clock"]) * 1000,
                    ))
        return edges

    def extract_lock_contentions(self, run_id: str) -> list[LockContention]:
        """Extract all lock conflict events."""
        contentions: list[LockContention] = []
        events = self.events(run_id, kind=EventKind.ACQUIRE_DENIED.value)
        for evt in events:
            payload = json.loads(evt["payload"]) if isinstance(evt["payload"], str) else evt["payload"]
            contentions.append(LockContention(
                resource_id=payload.get("resource_id", ""),
                requester=evt["agent_id"],
                holder=payload.get("holder", "unknown"),
                requested_mode=payload.get("mode", ""),
                holder_mode=payload.get("holder_mode", ""),
                timestamp=evt["wall_clock"],
            ))
        return contentions

    def summarize(self, run_id: str) -> InteractionSummary | None:
        """Compute aggregate statistics for a run."""
        events = self.events(run_id)
        if not events:
            return None

        agents: set[str] = set()
        topics: set[str] = set()
        total_publishes = 0
        total_deliveries = 0
        total_lock_attempts = 0
        total_lock_conflicts = 0
        total_lock_upgrades = 0
        latencies: list[float] = []
        flow_graph: dict[str, set[str]] = {}

        publishes_by_msg: dict[str, dict[str, Any]] = {}

        for evt in events:
            payload = json.loads(evt["payload"]) if isinstance(evt["payload"], str) else evt["payload"]
            agents.add(evt["agent_id"])

            if evt["kind"] in (EventKind.PUBLISH.value, EventKind.CONDITIONAL_PUBLISH.value):
                total_publishes += 1
                topics.add(payload.get("topic", ""))
                publishes_by_msg[payload.get("msg_id", "")] = evt

            elif evt["kind"] == EventKind.DELIVER.value:
                total_deliveries += 1
                pub = publishes_by_msg.get(payload.get("msg_id", ""))
                if pub:
                    pub_payload = json.loads(pub["payload"]) if isinstance(pub["payload"], str) else pub["payload"]
                    latencies.append((evt["wall_clock"] - pub["wall_clock"]) * 1000)
                    from_agent = pub_payload.get("publisher_id", pub["agent_id"])
                    to_agent = payload.get("subscriber_id", evt["agent_id"])
                    flow_graph.setdefault(from_agent, set()).add(to_agent)

            elif evt["kind"] in (EventKind.ACQUIRE.value, EventKind.ACQUIRE_DENIED.value):
                total_lock_attempts += 1
                if evt["kind"] == EventKind.ACQUIRE_DENIED.value:
                    total_lock_conflicts += 1

            elif evt["kind"] == EventKind.LOCK_UPGRADE.value:
                total_lock_upgrades += 1

        duration = events[-1]["wall_clock"] - events[0]["wall_clock"] if len(events) > 1 else 0

        return InteractionSummary(
            run_id=run_id,
            agent_count=len(agents),
            topic_count=len(topics),
            total_messages=total_publishes,
            total_deliveries=total_deliveries,
            total_lock_attempts=total_lock_attempts,
            total_lock_conflicts=total_lock_conflicts,
            total_lock_upgrades=total_lock_upgrades,
            avg_message_latency_ms=sum(latencies) / len(latencies) if latencies else 0,
            max_message_latency_ms=max(latencies) if latencies else 0,
            lock_conflict_rate=total_lock_conflicts / total_lock_attempts if total_lock_attempts else 0,
            message_flow_graph={k: sorted(v) for k, v in flow_graph.items()},
            duration_seconds=duration,
        )

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM telemetry_runs ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]
