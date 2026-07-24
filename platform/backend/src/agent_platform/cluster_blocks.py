"""Cluster coordination blocks — The Pair approach to multi-agent systems.

Each block is a Pair: deterministic Harness shell wrapping LLM strategy decisions.
The Harness guarantees message delivery, ordering, dedup, and conflict safety.
The LLM (via Builder or runtime) decides topics, recipients, and negotiation strategies.

Blocks:
  cluster_publish     — publish a message to a topic
  cluster_subscribe   — subscribe to a topic and wait for messages
  cluster_register    — register agent capabilities in the cluster
  cluster_discover    — discover agents by capability
  cluster_acquire     — acquire a lock on a shared resource
  cluster_release     — release a lock on a shared resource
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .workflow_models import ValueType


# ── Block configs ───────────────────────────────────────────────────

class ClusterPublishConfig(BaseModel):
    """Publish a message to a named topic.

    LLM strategy: choose topic name, compose message payload.
    Harness guarantees: persistence, ordering, idempotent (via _msg_id in payload).
    """
    topic: str = Field(min_length=1, max_length=200)
    publisher_id: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, ge=1, le=300)


class ClusterSubscribeConfig(BaseModel):
    """Subscribe to a topic and wait for new messages.

    LLM strategy: choose which topics to listen to.
    Harness guarantees: ordered delivery, no message loss, cursor tracking.
    """
    topic: str = Field(min_length=1, max_length=200)
    subscriber_id: str = Field(min_length=1, max_length=120)
    timeout_seconds: float = Field(default=30.0, ge=1, le=3600)
    poll_mode: bool = Field(default=False, description="If True, return all pending immediately instead of blocking")


class ClusterRegisterConfig(BaseModel):
    """Register this agent's capabilities in the cluster registry.

    LLM strategy: decide what capabilities to advertise.
    Harness guarantees: consistent registry, heartbeat-based liveness.
    """
    agent_id: str = Field(min_length=1, max_length=120)
    capabilities: list[str] = Field(min_length=1, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClusterDiscoverConfig(BaseModel):
    """Discover agents by capability in the cluster.

    LLM strategy: decide what capability to search for, how many results to use.
    Harness guarantees: up-to-date active agent list.
    """
    capability: str = Field(default="", max_length=200)
    agent_id: str = Field(min_length=1, max_length=120)


class ClusterAcquireConfig(BaseModel):
    """Acquire a lock on a shared resource for conflict prevention.

    LLM strategy: choose resource_id, decide whether to wait or pick alternative.
    Harness guarantees: deterministic conflict detection (read-read OK, others blocked).
    """
    resource_id: str = Field(min_length=1, max_length=500)
    agent_id: str = Field(min_length=1, max_length=120)
    mode: Literal["read", "write"] = "write"
    ttl_seconds: float = Field(default=300.0, ge=1, le=3600)


class ClusterReleaseConfig(BaseModel):
    """Release a previously acquired resource lock.

    Harness guarantees: only the lock owner can release.
    """
    resource_id: str = Field(min_length=1, max_length=500)
    agent_id: str = Field(min_length=1, max_length=120)


# ── Block definitions (for block registry) ─────────────────────────

_CLUSTER_BLOCKS: list[tuple[str, str, str, str]] = [
    ("cluster_publish", "Cluster Publish",
     "Publish a message to a named topic with persistence and ordering guarantees.",
     "Agent communication / pub-sub"),
    ("cluster_subscribe", "Cluster Subscribe",
     "Subscribe to a topic and wait for new messages with ordered delivery.",
     "Agent communication / pub-sub"),
    ("cluster_register", "Cluster Register",
     "Register agent capabilities in the cluster for discovery by other agents.",
     "Agent discovery"),
    ("cluster_discover", "Cluster Discover",
     "Discover active agents in the cluster by capability.",
     "Agent discovery"),
    ("cluster_acquire", "Cluster Acquire",
     "Acquire a lock on a shared resource for multi-agent conflict prevention.",
     "Resource coordination"),
    ("cluster_release", "Cluster Release",
     "Release a previously acquired resource lock.",
     "Resource coordination"),
]

_ZH_CLUSTER_BLOCKS: dict[str, tuple[str, str]] = {
    "cluster_publish": ("集群发布", "将消息发布到指定主题，保证持久化和有序。"),
    "cluster_subscribe": ("集群订阅", "订阅主题并等待新消息，保证有序投递。"),
    "cluster_register": ("集群注册", "在集群注册表中注册 Agent 能力以供发现。"),
    "cluster_discover": ("集群发现", "按能力发现集群中的活跃 Agent。"),
    "cluster_acquire": ("集群获取锁", "获取共享资源锁以防止多 Agent 冲突。"),
    "cluster_release": ("集群释放锁", "释放先前获取的资源锁。"),
}

_CLUSTER_EDITOR_FIELDS: dict[str, list[dict[str, Any]]] = {
    "cluster_publish": [
        {"path": "topic", "label": "Topic", "label_zh": "主题", "control": "text", "required": True},
        {"path": "publisher_id", "label": "Publisher ID", "label_zh": "发布者ID", "control": "reference_or_text", "required": True},
    ],
    "cluster_subscribe": [
        {"path": "topic", "label": "Topic", "label_zh": "主题", "control": "text", "required": True},
        {"path": "subscriber_id", "label": "Subscriber ID", "label_zh": "订阅者ID", "control": "reference_or_text", "required": True},
        {"path": "timeout_seconds", "label": "Timeout (s)", "label_zh": "超时秒数", "control": "number", "minimum": 1, "maximum": 3600, "step": 1},
        {"path": "poll_mode", "label": "Poll mode", "label_zh": "轮询模式", "control": "boolean"},
    ],
    "cluster_register": [
        {"path": "agent_id", "label": "Agent ID", "label_zh": "Agent标识", "control": "reference_or_text", "required": True},
        {"path": "capabilities", "label": "Capabilities", "label_zh": "能力列表", "control": "string_list", "required": True},
    ],
    "cluster_discover": [
        {"path": "capability", "label": "Capability", "label_zh": "能力关键词", "control": "text"},
        {"path": "agent_id", "label": "Agent ID", "label_zh": "查询者ID", "control": "reference_or_text", "required": True},
    ],
    "cluster_acquire": [
        {"path": "resource_id", "label": "Resource ID", "label_zh": "资源标识", "control": "reference_or_text", "required": True},
        {"path": "mode", "label": "Lock Mode", "label_zh": "锁模式", "control": "enum", "options": ["read", "write"], "required": True},
        {"path": "ttl_seconds", "label": "TTL (s)", "label_zh": "过期时间", "control": "number", "minimum": 1, "maximum": 3600, "step": 1},
    ],
    "cluster_release": [
        {"path": "resource_id", "label": "Resource ID", "label_zh": "资源标识", "control": "reference_or_text", "required": True},
    ],
}


# ── Builder manuals ──────────────────────────────────────────────

_CLUSTER_MANUALS: dict[str, dict[str, Any]] = {
    "cluster_publish": {
        "summary": "Publish a structured message to a named topic with SQLite-backed persistence, strict ordering (per-topic sequence), and idempotent delivery (duplicate msg_id returns the original).",
        "when_to_use": [
            "Use cluster_publish when multiple agents need to share results asynchronously.",
            "Use it to fan-out tasks: one agent publishes, many subscribe.",
            "Use it instead of direct $ref wiring when agents are dynamically discovered.",
            "Always include a unique _msg_id in the payload for idempotency.",
        ],
        "examples": [
            {"description": "Publish tasks for distributed execution", "connection": "... -> cluster_publish(topic='tasks') -> cluster_subscribe(topic='tasks')", "config": {"topic": "task.assignments", "publisher_id": "agent-planner"}},
        ],
        "anti_patterns": ["Do not use for synchronous request-response (use HTTP Request or $ref).", "Do not publish unstructured prose — always include typed payload with _msg_id."],
        "common_errors": ["Missing _msg_id causes every republish as new message.", "Publisher and subscriber use different topic names (case-sensitive)."],
        "claude_architecture_mapping": "Agent communication / pub-sub",
        "composability_constraints": ["Pair with cluster_subscribe for fan-out or fan-in patterns.", "Use with iteration to publish to multiple topics in parallel."],
    },
    "cluster_subscribe": {
        "summary": "Subscribe to a topic and receive new messages in order. Supports blocking wait (timeout) and non-blocking poll. Each subscriber tracks an independent cursor.",
        "when_to_use": [
            "Use when an agent needs to wait for work from another agent asynchronously.",
            "Set poll_mode=True for batch reads; poll_mode=False to block until a message arrives.",
        ],
        "examples": [
            {"description": "Wait 30s for a task assignment", "connection": "cluster_subscribe(topic='tasks') -> if_else -> llm", "config": {"topic": "task.assignments", "subscriber_id": "agent-executor", "timeout_seconds": 30}},
        ],
        "anti_patterns": ["Do not subscribe with the same subscriber_id from two branches (cursors diverge).", "Do not use as a database — messages are not indexed for queries."],
        "common_errors": ["Subscriber blocks forever if timeout is too high and no messages arrive.", "Forgetting to check output count before processing."],
        "claude_architecture_mapping": "Agent communication / pub-sub",
        "composability_constraints": ["Pair with cluster_publish.", "Use with iteration to subscribe to multiple topics."],
    },
    "cluster_register": {
        "summary": "Register an agent's capabilities in the cluster-wide registry for dynamic discovery by other agents.",
        "when_to_use": [
            "Use at the START of every agent workflow to announce its capabilities.",
            "Register granular capabilities: 'image_analysis', 'database_write', not 'ai'.",
        ],
        "examples": [
            {"description": "Register an image analysis agent", "connection": "start -> cluster_register -> ...", "config": {"agent_id": "agent-perception-01", "capabilities": ["image_analysis", "object_detection"]}},
        ],
        "anti_patterns": ["Do not register capabilities the agent cannot perform.", "Do not use overly generic capability names like 'ai' or 'agent'."],
        "common_errors": ["Capabilities list is empty.", "Agent ID collides with another registered agent."],
        "claude_architecture_mapping": "Agent discovery",
        "composability_constraints": ["Call before cluster_discover.", "Pair with cluster_discover for full discover-and-coordinate pattern."],
    },
    "cluster_discover": {
        "summary": "Query the cluster registry for active agents matching a capability keyword.",
        "when_to_use": [
            "Use BEFORE publishing tasks — find which agents can handle the work.",
            "Use with capability='' to list all active agents.",
        ],
        "examples": [
            {"description": "Discover database writers", "connection": "start -> cluster_discover -> if_else(found>0) -> cluster_publish", "config": {"capability": "database_write", "agent_id": "agent-planner"}},
        ],
        "anti_patterns": ["Do not call in a tight loop — cache the result.", "Do not call before agents have registered."],
        "common_errors": ["Capability string doesn't match what agents registered (case-sensitive).", "No agents registered yet."],
        "claude_architecture_mapping": "Agent discovery",
        "composability_constraints": ["Use output agents[] with iteration to contact each discovered agent.", "Register before discovering."],
    },
    "cluster_acquire": {
        "summary": "Acquire a distributed lock on a shared resource. Read locks are shared; write locks are exclusive. Auto-expires after TTL.",
        "when_to_use": [
            "Use BEFORE modifying a shared resource (database table, file, API).",
            "mode='read' for concurrent reads; mode='write' for exclusive modification.",
            "Always check acquired output — if False, wait, retry, or choose a different resource.",
        ],
        "examples": [
            {"description": "Protect a database write", "connection": "cluster_acquire -> if_else(acquired) -> llm(write) -> cluster_release", "config": {"resource_id": "db.reports", "mode": "write", "ttl_seconds": 300}},
        ],
        "anti_patterns": ["Do not acquire and forget to release.", "Do not acquire locks for read-only operations without consistency needs."],
        "common_errors": ["Another agent holds the lock — check acquired output.", "Lock expired during operation — refresh TTL for long tasks."],
        "claude_architecture_mapping": "Resource coordination",
        "composability_constraints": ["Always pair with cluster_release in the same workflow path.", "Use with if_else to handle denied locks gracefully."],
    },
    "cluster_release": {
        "summary": "Release a previously acquired resource lock. Only the original owner can release.",
        "when_to_use": [
            "Use IMMEDIATELY after completing the protected operation.",
            "Always pair cluster_acquire with a corresponding cluster_release.",
        ],
        "examples": [
            {"description": "Release after writing a report", "connection": "... -> cluster_acquire -> llm(write) -> cluster_release -> end", "config": {"resource_id": "db.reports", "agent_id": "{{agent_id}}"}},
        ],
        "anti_patterns": ["Do not release a lock acquired by another agent.", "Do not call without first checking acquire succeeded."],
        "common_errors": ["Agent ID doesn't match the lock owner.", "Resource ID doesn't match any active lock."],
        "claude_architecture_mapping": "Resource coordination",
        "composability_constraints": ["Always paired with cluster_acquire.", "Place in the same workflow branch as the acquire."],
    },
}
