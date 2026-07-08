from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator

from .models import AgentSpec, ChatMessage, EventRecord, utc_now


class Storage:
    """SQLite metadata plus append-only JSONL event streams."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.db_path = data_dir / "agent_platform.db"
        self.events_dir = data_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord]]] = defaultdict(set)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL,
                  published_version INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_versions (
                  agent_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (agent_id, version),
                  FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS generations (
                  id TEXT PRIMARY KEY,
                  requirement TEXT NOT NULL,
                  workspace_path TEXT,
                  status TEXT NOT NULL,
                  agent_id TEXT,
                  agent_version INTEGER,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  agent_version INTEGER NOT NULL,
                  workspace_path TEXT NOT NULL,
                  status TEXT NOT NULL,
                  messages_json TEXT NOT NULL,
                  usage_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                  stream_id TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (stream_id, seq)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                  run_id TEXT NOT NULL,
                  checkpoint_id TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (run_id, checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS platform_harness_tasks (
                  id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  status TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  parent_task_id TEXT,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_generations_agent ON generations(agent_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
                CREATE INDEX IF NOT EXISTS idx_platform_harness_tasks_kind_status
                  ON platform_harness_tasks(kind, status);
                CREATE INDEX IF NOT EXISTS idx_platform_harness_tasks_owner
                  ON platform_harness_tasks(owner_id);
                """
            )

    async def save_agent_version(self, spec: AgentSpec, status: str = "draft") -> int:
        async with self._lock:
            return await asyncio.to_thread(self._save_agent_version_sync, spec, status)

    def _save_agent_version_sync(self, spec: AgentSpec, status: str) -> int:
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM agent_versions WHERE agent_id=?",
                (spec.id,),
            ).fetchone()
            version = int(row["version"])
            conn.execute(
                """INSERT INTO agents(id,name,description,created_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     description=excluded.description,updated_at=excluded.updated_at""",
                (spec.id, spec.name, spec.description, now, now),
            )
            conn.execute(
                "INSERT INTO agent_versions VALUES(?,?,?,?,?)",
                (spec.id, version, status, spec.model_dump_json(), now),
            )
            return version

    async def publish_agent(self, agent_id: str, version: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._publish_agent_sync, agent_id, version)

    def _publish_agent_sync(self, agent_id: str, version: int) -> None:
        now = utc_now()
        with self._connect() as conn:
            found = conn.execute(
                "SELECT 1 FROM agent_versions WHERE agent_id=? AND version=?",
                (agent_id, version),
            ).fetchone()
            if not found:
                raise KeyError(f"agent version not found: {agent_id}@{version}")
            conn.execute(
                "UPDATE agent_versions SET status='published' WHERE agent_id=? AND version=?",
                (agent_id, version),
            )
            conn.execute(
                "UPDATE agents SET published_version=?,updated_at=? WHERE id=?",
                (version, now, agent_id),
            )

    async def get_agent(self, agent_id: str, version: int | None = None) -> tuple[AgentSpec, int, str]:
        return await asyncio.to_thread(self._get_agent_sync, agent_id, version)

    def _get_agent_sync(self, agent_id: str, version: int | None) -> tuple[AgentSpec, int, str]:
        with self._connect() as conn:
            if version is None:
                agent = conn.execute("SELECT published_version FROM agents WHERE id=?", (agent_id,)).fetchone()
                if not agent or agent["published_version"] is None:
                    raise KeyError(f"published agent not found: {agent_id}")
                version = int(agent["published_version"])
            row = conn.execute(
                "SELECT spec_json,status FROM agent_versions WHERE agent_id=? AND version=?",
                (agent_id, version),
            ).fetchone()
            if not row:
                raise KeyError(f"agent version not found: {agent_id}@{version}")
            return AgentSpec.model_validate_json(row["spec_json"]), version, row["status"]

    async def list_agents(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_agents_sync)

    def _list_agents_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY updated_at DESC").fetchall()
            return [dict(row) for row in rows]

    async def create_generation(self, generation_id: str, requirement: str, workspace_path: str | None) -> None:
        now = utc_now()
        async with self._lock:
            await asyncio.to_thread(
                self._execute,
                "INSERT INTO generations VALUES(?,?,?,?,?,?,?,?,?)",
                (generation_id, requirement, workspace_path, "queued", None, None, None, now, now),
            )

    async def update_generation(self, generation_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        columns = ",".join(f"{key}=?" for key in values)
        params = tuple(values.values()) + (generation_id,)
        async with self._lock:
            await asyncio.to_thread(self._execute, f"UPDATE generations SET {columns} WHERE id=?", params)

    async def get_generation(self, generation_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_one, "SELECT * FROM generations WHERE id=?", (generation_id,))

    async def create_session(
        self, session_id: str, agent_id: str, version: int, workspace_path: str
    ) -> None:
        now = utc_now()
        async with self._lock:
            await asyncio.to_thread(
                self._execute,
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, agent_id, version, workspace_path, "ready", "[]", "{}", now, now),
            )

    async def update_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        messages: list[ChatMessage] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status
        if messages is not None:
            values["messages_json"] = json.dumps(
                [message.model_dump(mode="json", exclude_none=True) for message in messages],
                ensure_ascii=False,
            )
        if usage is not None:
            values["usage_json"] = json.dumps(usage)
        columns = ",".join(f"{key}=?" for key in values)
        async with self._lock:
            await asyncio.to_thread(
                self._execute,
                f"UPDATE sessions SET {columns} WHERE id=?",
                tuple(values.values()) + (session_id,),
            )

    async def get_session(self, session_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self._get_one, "SELECT * FROM sessions WHERE id=?", (session_id,))
        row["messages"] = [ChatMessage.model_validate(item) for item in json.loads(row.pop("messages_json"))]
        row["usage"] = json.loads(row.pop("usage_json"))
        return row

    async def save_checkpoint(
        self, run_id: str, checkpoint_id: str, data: dict[str, Any]
    ) -> None:
        """Persist a workflow checkpoint for crash recovery."""
        async with self._lock:
            await asyncio.to_thread(self._save_checkpoint_sync, run_id, checkpoint_id, data)

    def _save_checkpoint_sync(self, run_id: str, checkpoint_id: str, data: dict[str, Any]) -> None:
        import json as _json
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                (run_id, checkpoint_id, _json.dumps(data, ensure_ascii=False), now),
            )

    async def get_checkpoint(self, run_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        """Retrieve a persisted checkpoint."""
        import json as _json
        async with self._lock:
            row = await asyncio.to_thread(
                self._get_one,
                "SELECT * FROM checkpoints WHERE run_id=? AND checkpoint_id=?",
                (run_id, checkpoint_id),
            )
        if row:
            row["data"] = _json.loads(row.pop("data_json"))
            return row
        return None

    async def save_platform_task(self, record: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_platform_task_sync, record)

    def _save_platform_task_sync(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_harness_tasks(
                  id, kind, status, owner_id, resource_id, parent_task_id,
                  record_json, created_at, updated_at, finished_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  kind=excluded.kind,
                  status=excluded.status,
                  owner_id=excluded.owner_id,
                  resource_id=excluded.resource_id,
                  parent_task_id=excluded.parent_task_id,
                  record_json=excluded.record_json,
                  updated_at=excluded.updated_at,
                  finished_at=excluded.finished_at
                """,
                (
                    record["id"],
                    record["kind"],
                    record["status"],
                    record["owner_id"],
                    record["resource_id"],
                    record.get("parent_task_id"),
                    encoded,
                    record["created_at"],
                    record["updated_at"],
                    record.get("finished_at"),
                ),
            )

    async def get_platform_task(self, task_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._get_one,
            "SELECT record_json FROM platform_harness_tasks WHERE id=?",
            (task_id,),
        )
        return json.loads(row["record_json"])

    async def list_platform_tasks(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_platform_tasks_sync,
            kind,
            status,
            owner_id,
            max(1, min(limit, 500)),
        )

    def _list_platform_tasks_sync(
        self,
        kind: str | None,
        status: str | None,
        owner_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if status:
            where.append("status=?")
            params.append(status)
        if owner_id:
            where.append("owner_id=?")
            params.append(owner_id)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            "SELECT record_json FROM platform_harness_tasks "
            f"{where_sql} ORDER BY created_at DESC LIMIT ?"
        )
        rows = self._get_all(sql, tuple(params + [limit]))
        return [json.loads(row["record_json"]) for row in rows]

    async def count_platform_tasks(self, *, statuses: set[str]) -> int:
        if not statuses:
            return 0
        return await asyncio.to_thread(self._count_platform_tasks_sync, statuses)

    def _count_platform_tasks_sync(self, statuses: set[str]) -> int:
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM platform_harness_tasks WHERE status IN ({placeholders})",
                tuple(statuses),
            ).fetchone()
        return int(row["count"]) if row else 0

    async def sum_platform_usage_count(self, *, owner_id: str, usage_type: str) -> int:
        return await asyncio.to_thread(self._sum_platform_usage_count_sync, owner_id, usage_type)

    def _sum_platform_usage_count_sync(self, owner_id: str, usage_type: str) -> int:
        rows = self._get_all(
            "SELECT record_json FROM platform_harness_tasks WHERE owner_id=?",
            (owner_id,),
        )
        total = 0
        for row in rows:
            record = json.loads(row["record_json"])
            total += int(record.get("usage_counts", {}).get(usage_type, 0))
        return total

    async def append_event(self, stream_id: str, event_type: str, data: dict[str, Any]) -> EventRecord:
        async with self._lock:
            event = await asyncio.to_thread(self._append_event_sync, stream_id, event_type, data)
        for queue in list(self._subscribers[stream_id]):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers[stream_id].discard(queue)
        return event

    def _append_event_sync(self, stream_id: str, event_type: str, data: dict[str, Any]) -> EventRecord:
        now = utc_now()
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM events WHERE stream_id=?", (stream_id,)
            ).fetchone()
            seq = int(row["seq"])
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)", (stream_id, seq, event_type, encoded, now)
            )
        event = EventRecord(id=seq, stream_id=stream_id, type=event_type, data=data, created_at=now)
        path = self.events_dir / f"{stream_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
        return event

    async def list_events(self, stream_id: str, after: int = 0) -> list[EventRecord]:
        return await asyncio.to_thread(self._list_events_sync, stream_id, after)

    def _list_events_sync(self, stream_id: str, after: int) -> list[EventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE stream_id=? AND seq>? ORDER BY seq", (stream_id, after)
            ).fetchall()
        return [
            EventRecord(
                id=row["seq"],
                stream_id=stream_id,
                type=row["event_type"],
                data=json.loads(row["data_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def subscribe(self, stream_id: str, after: int = 0) -> AsyncIterator[EventRecord]:
        for event in await self.list_events(stream_id, after):
            yield event
            after = event.id
        queue: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=1000)
        self._subscribers[stream_id].add(queue)
        try:
            while True:
                event = await queue.get()
                if event.id > after:
                    after = event.id
                    yield event
        finally:
            self._subscribers[stream_id].discard(queue)

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._connect() as conn:
            conn.execute(sql, params)

    def _get_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if not row:
                raise KeyError("record not found")
            return dict(row)

    def _get_all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]
