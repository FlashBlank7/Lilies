"""Cluster messaging infrastructure — The Pair approach to multi-agent coordination.

Design principle (from "The Primitive Is The Pair"):
  Every layer is a Pair: Harness (deterministic guarantees) + LLM (non-deterministic strategy).
  The Harness provides reliable message delivery, ordering, deduplication, and conflict
  detection. The LLM decides *who* to talk to, *what* to say, and *how* to resolve conflicts.

Architecture:
  ┌─────────────────────────────────────────────────────┐
  │  Cluster Harness (this module — deterministic)       │
  │  • Topic-based pub/sub via SQLite persisted queues   │
  │  • Agent capability registry                        │
  │  • Resource lock conflict detection                  │
  │  • Causal ordering per topic                         │
  │  • Idempotent publish                               │
  └─────────────────────────────────────────────────────┘
         ▲                      ▲
         │ subscribe/publish    │ register/acquire
         │                      │
  ┌──────┴──────┐       ┌──────┴──────┐
  │  Agent A    │       │  Agent B    │  ← LLM decides strategy
  │  (LLM core) │       │  (LLM core) │
  └─────────────┘       └─────────────┘
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


# ── Data models ────────────────────────────────────────────────────

@dataclass(slots=True)
class ClusterMessage:
    """A persisted message in a topic."""
    id: str
    topic: str
    publisher_id: str
    payload: dict[str, Any]
    sequence: int
    created_at: float


@dataclass(slots=True)
class AgentRegistration:
    """An agent's registration in the cluster."""
    agent_id: str
    capabilities: list[str]
    status: Literal["active", "draining", "offline"]
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass(slots=True)
class ResourceLock:
    """A lock on a shared resource for conflict detection."""
    resource_id: str
    owner_id: str
    mode: Literal["read", "write"]
    acquired_at: float
    expires_at: float | None


# ── Cluster Message Bus ─────────────────────────────────────────────

class ClusterMessageBus:
    """SQLite-backed pub/sub message bus with deterministic guarantees.

    Each topic is an ordered, persisted stream.  Subscribers track their
    position independently (cursor = last delivered sequence).  Messages
    are never lost — they persist until explicitly pruned.
    """

    def __init__(self, data_dir: Path) -> None:
        self._db_path = data_dir / "cluster_bus.db"
        self._lock = asyncio.Lock()
        # In-process wake-up for subscribers blocked on await_message
        self._wake_events: dict[str, asyncio.Event] = {}

    # ── Lifecycle ──────────────────────────────────────────────────

    async def initialize(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cluster_topics (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cluster_messages (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL REFERENCES cluster_topics(id),
                    publisher_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_msgs_topic_seq
                    ON cluster_messages(topic_id, sequence);
                CREATE TABLE IF NOT EXISTS cluster_subscriptions (
                    id TEXT PRIMARY KEY,
                    topic_id TEXT NOT NULL REFERENCES cluster_topics(id),
                    subscriber_id TEXT NOT NULL,
                    cursor_sequence INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    UNIQUE(topic_id, subscriber_id)
                );
                CREATE TABLE IF NOT EXISTS cluster_registry (
                    agent_id TEXT PRIMARY KEY,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cluster_locks (
                    resource_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('read','write')),
                    acquired_at REAL NOT NULL,
                    expires_at REAL
                );
            """)

    # ── Topic management ──────────────────────────────────────────

    async def ensure_topic(self, name: str) -> str:
        """Get or create a topic, returning its id."""
        async with self._lock:
            def _ensure() -> str:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT id FROM cluster_topics WHERE name = ?", (name,)
                    ).fetchone()
                    if row:
                        return row["id"]
                    tid = uuid4().hex[:12]
                    conn.execute(
                        "INSERT INTO cluster_topics (id, name, created_at) VALUES (?, ?, ?)",
                        (tid, name, time.time()),
                    )
                    return tid
            return await asyncio.to_thread(_ensure)

    # ── Publish ────────────────────────────────────────────────────

    async def publish(
        self, topic: str, publisher_id: str, payload: dict[str, Any],
    ) -> ClusterMessage:
        """Publish a message to a topic.  Idempotent via message id in payload."""
        topic_id = await self.ensure_topic(topic)
        msg_id = payload.get("_msg_id") or uuid4().hex[:16]
        now = time.time()

        async with self._lock:
            def _publish() -> ClusterMessage:
                with self._connect() as conn:
                    # Idempotency: same msg_id → return existing
                    existing = conn.execute(
                        "SELECT * FROM cluster_messages WHERE id = ?", (msg_id,)
                    ).fetchone()
                    if existing:
                        return ClusterMessage(
                            id=existing["id"], topic=topic,
                            publisher_id=existing["publisher_id"],
                            payload=json.loads(existing["payload"]),
                            sequence=existing["sequence"],
                            created_at=existing["created_at"],
                        )
                    # Get next sequence
                    last = conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) AS mx FROM cluster_messages WHERE topic_id = ?",
                        (topic_id,),
                    ).fetchone()
                    seq = last["mx"] + 1
                    conn.execute(
                        """INSERT INTO cluster_messages (id, topic_id, publisher_id, payload, sequence, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (msg_id, topic_id, publisher_id, json.dumps(payload), seq, now),
                    )
                    return ClusterMessage(
                        id=msg_id, topic=topic, publisher_id=publisher_id,
                        payload=payload, sequence=seq, created_at=now,
                    )

            msg = await asyncio.to_thread(_publish)

        # Wake any subscribers blocked on this topic
        wake = self._wake_events.get(topic)
        if wake:
            wake.set()
        return msg

    # ── Subscribe / Receive ────────────────────────────────────────

    async def subscribe(self, topic: str, subscriber_id: str) -> str:
        """Register a subscription. Returns subscription id."""
        topic_id = await self.ensure_topic(topic)
        sub_id = f"{subscriber_id}:{topic_id[:8]}"
        async with self._lock:
            def _sub() -> str:
                with self._connect() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO cluster_subscriptions
                           (id, topic_id, subscriber_id, cursor_sequence, created_at)
                           VALUES (?, ?, ?, 0, ?)""",
                        (sub_id, topic_id, subscriber_id, time.time()),
                    )
                    return sub_id
            return await asyncio.to_thread(_sub)

    async def await_message(
        self, topic: str, subscriber_id: str, timeout: float = 30.0,
    ) -> ClusterMessage | None:
        """Block until a new message arrives on the topic, or timeout."""
        topic_id = await self.ensure_topic(topic)
        sub_id = f"{subscriber_id}:{topic_id[:8]}"

        wake = self._wake_events.setdefault(topic, asyncio.Event())

        async def _poll() -> ClusterMessage | None:
            while True:
                async with self._lock:
                    msg = await self._fetch_next(topic, topic_id, sub_id)
                    if msg:
                        wake.clear()
                        return msg
                # Wait for publish to wake us
                try:
                    await asyncio.wait_for(wake.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    return None
                wake.clear()

        return await _poll()

    async def _fetch_next(
        self, topic: str, topic_id: str, sub_id: str,
    ) -> ClusterMessage | None:
        """Fetch the next undelivered message for a subscriber."""
        def _fetch() -> dict | None:
            with self._connect() as conn:
                sub = conn.execute(
                    "SELECT cursor_sequence FROM cluster_subscriptions WHERE id = ?",
                    (sub_id,),
                ).fetchone()
                if not sub:
                    return None
                cursor = sub["cursor_sequence"]
                msg = conn.execute(
                    """SELECT * FROM cluster_messages
                       WHERE topic_id = ? AND sequence > ?
                       ORDER BY sequence LIMIT 1""",
                    (topic_id, cursor),
                ).fetchone()
                if not msg:
                    return None
                # Advance cursor
                conn.execute(
                    "UPDATE cluster_subscriptions SET cursor_sequence = ? WHERE id = ?",
                    (msg["sequence"], sub_id),
                )
                return dict(msg)

        row = await asyncio.to_thread(_fetch)
        if row is None:
            return None
        return ClusterMessage(
            id=row["id"], topic=topic,
            publisher_id=row["publisher_id"],
            payload=json.loads(row["payload"]),
            sequence=row["sequence"],
            created_at=row["created_at"],
        )

    async def poll_messages(
        self, topic: str, subscriber_id: str,
    ) -> list[ClusterMessage]:
        """Non-blocking: return all pending messages for a subscriber."""
        topic_id = await self.ensure_topic(topic)
        sub_id = f"{subscriber_id}:{topic_id[:8]}"
        messages: list[ClusterMessage] = []
        while True:
            msg = await self._fetch_next(topic, topic_id, sub_id)
            if msg is None:
                break
            messages.append(msg)
        return messages

    # ── Conditional publish (L1 completeness) ──────────────────────

    async def conditional_publish(
        self,
        topic: str,
        publisher_id: str,
        payload: dict[str, Any],
        condition: dict[str, Any],
    ) -> tuple[bool, ClusterMessage | None]:
        """Atomically check a condition then publish. L1-completeness primitive.

        This is the Harness primitive proved equivalent to acquire (Thm 8.4).
        The condition is evaluated atomically with the write — no LLM in the loop.

        condition schema:
          {"type": "no_recent_from", "agent_id": "...", "within_seconds": N}
            → reject if agent_id published to this topic within the last N seconds
          {"type": "max_pending", "count": N}
            → reject if more than N messages are pending (undelivered) on this topic
          {"type": "exclusive_window", "agent_id": "..."}
            → reject if any other agent published to this topic in the last second

        Returns (accepted, message_or_None).
        """
        topic_id = await self.ensure_topic(topic)
        msg_id = payload.get("_msg_id") or uuid4().hex[:16]
        now = time.time()

        async with self._lock:
            def _conditional() -> tuple[bool, dict | None]:
                with self._connect() as conn:
                    # Evaluate condition atomically
                    cond_type = condition.get("type", "")
                    if cond_type == "no_recent_from":
                        agent = condition["agent_id"]
                        window = float(condition.get("within_seconds", 10))
                        recent = conn.execute(
                            """SELECT COUNT(*) as cnt FROM cluster_messages
                               WHERE topic_id = ? AND publisher_id = ?
                               AND created_at > ?""",
                            (topic_id, agent, now - window),
                        ).fetchone()
                        if recent and recent["cnt"] > 0:
                            return False, None

                    elif cond_type == "max_pending":
                        max_pending = int(condition.get("count", 100))
                        # Count messages not yet delivered to at least one subscriber
                        last_delivered = conn.execute(
                            """SELECT COALESCE(MIN(cursor_sequence), 0) as mn
                               FROM cluster_subscriptions WHERE topic_id = ?""",
                            (topic_id,),
                        ).fetchone()
                        cursor = last_delivered["mn"] if last_delivered else 0
                        pending = conn.execute(
                            "SELECT COUNT(*) as cnt FROM cluster_messages WHERE topic_id = ? AND sequence > ?",
                            (topic_id, cursor),
                        ).fetchone()
                        if pending and pending["cnt"] > max_pending:
                            return False, None

                    elif cond_type == "exclusive_window":
                        owner = condition.get("agent_id", "")
                        recent = conn.execute(
                            """SELECT COUNT(*) as cnt FROM cluster_messages
                               WHERE topic_id = ? AND publisher_id != ?
                               AND created_at > ?""",
                            (topic_id, owner, now - 1.0),
                        ).fetchone()
                        if recent and recent["cnt"] > 0:
                            return False, None

                    # Check idempotency
                    existing = conn.execute(
                        "SELECT * FROM cluster_messages WHERE id = ?", (msg_id,),
                    ).fetchone()
                    if existing:
                        return True, dict(existing)

                    # Get next sequence
                    last = conn.execute(
                        "SELECT COALESCE(MAX(sequence), 0) AS mx FROM cluster_messages WHERE topic_id = ?",
                        (topic_id,),
                    ).fetchone()
                    seq = last["mx"] + 1
                    conn.execute(
                        """INSERT INTO cluster_messages (id, topic_id, publisher_id, payload, sequence, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (msg_id, topic_id, publisher_id, json.dumps(payload), seq, now),
                    )
                    return True, {
                        "id": msg_id, "topic": topic, "publisher_id": publisher_id,
                        "payload": payload, "sequence": seq, "created_at": now,
                    }

            accepted, row = await asyncio.to_thread(_conditional)

        if accepted and row:
            msg = ClusterMessage(
                id=row["id"], topic=topic, publisher_id=row["publisher_id"],
                payload=json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"],
                sequence=row["sequence"], created_at=row["created_at"],
            )
            wake = self._wake_events.get(topic)
            if wake:
                wake.set()
            return True, msg
        return False, None

    # ── Topic history ──────────────────────────────────────────────

    async def peek_messages(
        self, topic: str, subscriber_id: str, limit: int = 100,
    ) -> list[ClusterMessage]:
        """Return pending messages without advancing the cursor.

        Unlike poll_messages, this does NOT consume messages — they remain
        available for subsequent polls. Used for building agent observations.
        """
        topic_id = await self.ensure_topic(topic)
        sub_id = f"{subscriber_id}:{topic_id[:8]}"

        def _peek() -> list[ClusterMessage]:
            with self._connect() as conn:
                sub = conn.execute(
                    "SELECT cursor_sequence FROM cluster_subscriptions WHERE id = ?",
                    (sub_id,),
                ).fetchone()
                if not sub:
                    return []
                cursor = sub["cursor_sequence"]
                rows = conn.execute(
                    """SELECT * FROM cluster_messages
                       WHERE topic_id = ? AND sequence > ?
                       ORDER BY sequence LIMIT ?""",
                    (topic_id, cursor, limit),
                ).fetchall()
                return [
                    ClusterMessage(
                        id=r["id"], topic=topic,
                        publisher_id=r["publisher_id"],
                        payload=json.loads(r["payload"]),
                        sequence=r["sequence"],
                        created_at=r["created_at"],
                    )
                    for r in rows
                ]
        return await asyncio.to_thread(_peek)

    async def topic_history(self, topic: str, limit: int = 50) -> list[ClusterMessage]:
        """Return recent messages on a topic (for debugging/monitoring)."""
        topic_id = await self.ensure_topic(topic)
        def _history() -> list[ClusterMessage]:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM cluster_messages
                       WHERE topic_id = ? ORDER BY sequence DESC LIMIT ?""",
                    (topic_id, limit),
                ).fetchall()
                return [
                    ClusterMessage(
                        id=r["id"], topic=topic,
                        publisher_id=r["publisher_id"],
                        payload=json.loads(r["payload"]),
                        sequence=r["sequence"], created_at=r["created_at"],
                    )
                    for r in reversed(rows)
                ]
        return await asyncio.to_thread(_history)


# ── Cluster Registry ────────────────────────────────────────────────

class ClusterRegistry:
    """Agent capability registry for dynamic discovery.

    Agents register their capabilities on startup. Other agents query
    by capability to find collaborators.  This is the Harness layer —
    the LLM decides WHICH capabilities to look for.
    """

    def __init__(self, bus: ClusterMessageBus) -> None:
        self._bus = bus

    async def register(
        self, agent_id: str, capabilities: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> AgentRegistration:
        """Register an agent's capabilities."""
        now = time.time()
        def _reg() -> None:
            with self._bus._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO cluster_registry
                       (agent_id, capabilities, status, metadata, updated_at)
                       VALUES (?, ?, 'active', ?, ?)""",
                    (agent_id, json.dumps(capabilities),
                     json.dumps(metadata or {}), now),
                )
        await asyncio.to_thread(_reg)
        return AgentRegistration(
            agent_id=agent_id, capabilities=capabilities,
            status="active", metadata=metadata or {}, updated_at=now,
        )

    async def unregister(self, agent_id: str) -> None:
        """Mark an agent as offline."""
        def _unreg() -> None:
            with self._bus._connect() as conn:
                conn.execute(
                    "UPDATE cluster_registry SET status='offline', updated_at=? WHERE agent_id=?",
                    (time.time(), agent_id),
                )
        await asyncio.to_thread(_unreg)

    async def heartbeat(self, agent_id: str) -> bool:
        """Update agent liveness timestamp. Returns True if agent exists."""
        now = time.time()
        def _beat() -> bool:
            with self._bus._connect() as conn:
                row = conn.execute(
                    "SELECT agent_id FROM cluster_registry WHERE agent_id = ?", (agent_id,),
                ).fetchone()
                if not row:
                    return False
                conn.execute(
                    "UPDATE cluster_registry SET updated_at = ?, status = 'active' WHERE agent_id = ?",
                    (now, agent_id),
                )
                return True
        return await asyncio.to_thread(_beat)

    async def expire_inactive_agents(self, heartbeat_ttl: float = 60.0) -> list[str]:
        """Mark agents offline if no heartbeat within TTL. Returns list of expired agent IDs."""
        now = time.time()
        def _expire() -> list[str]:
            with self._bus._connect() as conn:
                rows = conn.execute(
                    """SELECT agent_id FROM cluster_registry
                       WHERE status = 'active' AND updated_at < ?""",
                    (now - heartbeat_ttl,),
                ).fetchall()
                expired = [r["agent_id"] for r in rows]
                if expired:
                    conn.executemany(
                        "UPDATE cluster_registry SET status = 'offline', updated_at = ? WHERE agent_id = ?",
                        [(now, aid) for aid in expired],
                    )
                return expired
        return await asyncio.to_thread(_expire)

    async def discover(
        self, capability: str | None = None, status: str = "active",
    ) -> list[AgentRegistration]:
        """Find agents by capability or list all active agents."""
        def _disc() -> list[AgentRegistration]:
            with self._bus._connect() as conn:
                if capability:
                    # SQLite JSON search for capability in the array
                    rows = conn.execute(
                        """SELECT * FROM cluster_registry
                           WHERE status = ? AND capabilities LIKE ?
                           ORDER BY updated_at DESC""",
                        (status, f'%"{capability}"%'),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM cluster_registry WHERE status = ? ORDER BY updated_at DESC",
                        (status,),
                    ).fetchall()
                return [
                    AgentRegistration(
                        agent_id=r["agent_id"],
                        capabilities=json.loads(r["capabilities"]),
                        status=r["status"],
                        metadata=json.loads(r["metadata"]),
                        updated_at=r["updated_at"],
                    )
                    for r in rows
                ]
        return await asyncio.to_thread(_disc)


# ── Conflict Detector ───────────────────────────────────────────────

class ConflictDetector:
    """Resource lock manager for multi-agent conflict prevention.

    Before modifying a shared resource, an agent MUST acquire a lock.
    The Harness enforces deterministic rules:
      - Multiple readers OK (read-read compatible)
      - Writer conflicts with readers and other writers (read-write, write-write blocked)
      - Locks can expire (TTL) to prevent dead agents from holding resources forever

    The LLM decides: should I wait, negotiate, or choose a different resource?
    """

    LOCK_TTL = 300.0  # 5 minutes default

    def __init__(self, bus: ClusterMessageBus) -> None:
        self._bus = bus
        self._lock = asyncio.Lock()

    async def acquire(
        self, resource_id: str, owner_id: str,
        mode: Literal["read", "write"] = "write",
        ttl: float | None = None,
    ) -> bool:
        """Try to acquire a lock. Returns True if granted, False if conflict."""
        ttl = ttl or self.LOCK_TTL
        now = time.time()

        async with self._lock:
            def _acquire() -> bool:
                with self._bus._connect() as conn:
                    # Clean expired locks first
                    conn.execute(
                        "DELETE FROM cluster_locks WHERE expires_at IS NOT NULL AND expires_at < ?",
                        (now,),
                    )
                    existing = conn.execute(
                        "SELECT * FROM cluster_locks WHERE resource_id = ?", (resource_id,)
                    ).fetchone()
                    if existing:
                        # Read-read is OK
                        if mode == "read" and existing["mode"] == "read":
                            # Multiple readers allowed — just add another
                            pass
                        elif existing["owner_id"] != owner_id:
                            return False  # Conflict!
                    conn.execute(
                        """INSERT OR REPLACE INTO cluster_locks
                           (resource_id, owner_id, mode, acquired_at, expires_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (resource_id, owner_id, mode, now, now + ttl),
                    )
                    return True
            return await asyncio.to_thread(_acquire)

    async def release(self, resource_id: str, owner_id: str) -> bool:
        """Release a lock. Only the owner can release."""
        async with self._lock:
            def _release() -> bool:
                with self._bus._connect() as conn:
                    existing = conn.execute(
                        "SELECT owner_id FROM cluster_locks WHERE resource_id = ?",
                        (resource_id,),
                    ).fetchone()
                    if existing and existing["owner_id"] == owner_id:
                        conn.execute(
                            "DELETE FROM cluster_locks WHERE resource_id = ? AND owner_id = ?",
                            (resource_id, owner_id),
                        )
                        return True
                    return False
            return await asyncio.to_thread(_release)

    async def refresh(self, resource_id: str, owner_id: str, ttl: float | None = None) -> bool:
        """Extend a lock's TTL to prevent expiration during long operations."""
        ttl = ttl or self.LOCK_TTL
        now = time.time()
        async with self._lock:
            def _refresh() -> bool:
                with self._bus._connect() as conn:
                    existing = conn.execute(
                        "SELECT owner_id FROM cluster_locks WHERE resource_id = ?",
                        (resource_id,),
                    ).fetchone()
                    if existing and existing["owner_id"] == owner_id:
                        conn.execute(
                            "UPDATE cluster_locks SET expires_at = ? WHERE resource_id = ?",
                            (now + ttl, resource_id),
                        )
                        return True
                    return False
            return await asyncio.to_thread(_refresh)

    async def list_locks(self) -> list[ResourceLock]:
        """List all active locks (for monitoring/debugging)."""
        now = time.time()
        def _list() -> list[ResourceLock]:
            with self._bus._connect() as conn:
                # Clean expired first
                conn.execute(
                    "DELETE FROM cluster_locks WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
                rows = conn.execute("SELECT * FROM cluster_locks ORDER BY acquired_at").fetchall()
                return [
                    ResourceLock(
                        resource_id=r["resource_id"], owner_id=r["owner_id"],
                        mode=r["mode"], acquired_at=r["acquired_at"],
                        expires_at=r["expires_at"],
                    )
                    for r in rows
                ]
        return await asyncio.to_thread(_list)

    async def lock_holders(self, resource_id: str) -> list[ResourceLock]:
        """Get all current holders of a lock on a resource."""
        now = time.time()
        def _holders() -> list[ResourceLock]:
            with self._bus._connect() as conn:
                conn.execute(
                    "DELETE FROM cluster_locks WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                )
                rows = conn.execute(
                    "SELECT * FROM cluster_locks WHERE resource_id = ? ORDER BY acquired_at",
                    (resource_id,),
                ).fetchall()
                return [
                    ResourceLock(
                        resource_id=r["resource_id"], owner_id=r["owner_id"],
                        mode=r["mode"], acquired_at=r["acquired_at"],
                        expires_at=r["expires_at"],
                    )
                    for r in rows
                ]
        return await asyncio.to_thread(_holders)

    async def upgrade_lock(
        self, resource_id: str, owner_id: str,
        from_mode: Literal["read", "write"],
        to_mode: Literal["read", "write"],
    ) -> bool:
        """Upgrade a lock from read→write or downgrade write→read.

        Upgrade (read→write): only succeeds if no other holders exist.
        Downgrade (write→read): always succeeds for the lock owner.

        This is the L1-completeness lock transition primitive.
        """
        if from_mode == to_mode:
            return True
        now = time.time()
        async with self._lock:
            def _upgrade() -> bool:
                with self._bus._connect() as conn:
                    conn.execute(
                        "DELETE FROM cluster_locks WHERE expires_at IS NOT NULL AND expires_at < ?",
                        (now,),
                    )
                    existing = conn.execute(
                        "SELECT * FROM cluster_locks WHERE resource_id = ? AND owner_id = ?",
                        (resource_id, owner_id),
                    ).fetchone()
                    if not existing:
                        return False
                    if from_mode == "read" and to_mode == "write":
                        # Check no other holders exist
                        others = conn.execute(
                            "SELECT COUNT(*) as cnt FROM cluster_locks WHERE resource_id = ? AND owner_id != ?",
                            (resource_id, owner_id),
                        ).fetchone()
                        if others and others["cnt"] > 0:
                            return False
                    # Apply the mode change, preserving TTL
                    remaining = existing["expires_at"] - now if existing["expires_at"] else self.LOCK_TTL
                    conn.execute(
                        "UPDATE cluster_locks SET mode = ?, expires_at = ? WHERE resource_id = ? AND owner_id = ?",
                        (to_mode, now + remaining, resource_id, owner_id),
                    )
                    return True
            return await asyncio.to_thread(_upgrade)


# ── Convenience factory ────────────────────────────────────────────

async def create_cluster_infrastructure(data_dir: Path) -> tuple[
    ClusterMessageBus, ClusterRegistry, ConflictDetector,
]:
    """Create and initialize all cluster infrastructure."""
    bus = ClusterMessageBus(data_dir)
    await bus.initialize()
    registry = ClusterRegistry(bus)
    detector = ConflictDetector(bus)
    return bus, registry, detector
