from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from .models import AgentSpec, ChatMessage, EventRecord, utc_now

logger = logging.getLogger(__name__)


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
        await asyncio.to_thread(self._lock_down_files)

    def _lock_down_files(self) -> None:
        """库文件只给自己读写。

        2026-08-29 实测：data/agent_platform.db 是 0644——
        同机其他用户能直接读走整个库：业主码、客户使用码、
        用户令牌哈希、全部业务数据。
        sqlite 按 umask 建文件，默认 umask 022 就是 644；
        不显式收就是松的（和 guanjia 的配置文件同一个病）。

        WAL 和 SHM 是同一份数据的另外两块，一起收。
        收不动就算了（不支持权限的文件系统）——起不来比权限松更糟。
        """
        base = Path(self.db_path)
        try:
            base.parent.chmod(0o700)
        except OSError:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(base) + suffix)
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        from .db import connect

        return connect(self.db_path)

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
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL UNIQUE,
                  token_hash TEXT NOT NULL UNIQUE,
                  role TEXT NOT NULL DEFAULT 'member',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS platform_secrets (
                  id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL,
                  value TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(owner_id, name)
                );
                CREATE TABLE IF NOT EXISTS platform_worker_heartbeats (
                  worker_id TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  active_task_id TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS governed_memory_items (
                  id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL,
                  scope_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  retention_class TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                  id TEXT PRIMARY KEY,
                  application_id TEXT NOT NULL,
                  profile_id TEXT NOT NULL,
                  environment_id TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  achieved_status TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_generations_agent ON generations(agent_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
                CREATE INDEX IF NOT EXISTS idx_platform_harness_tasks_kind_status
                  ON platform_harness_tasks(kind, status);
                CREATE INDEX IF NOT EXISTS idx_platform_harness_tasks_owner
                  ON platform_harness_tasks(owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_secrets_owner
                  ON platform_secrets(owner_id);
                CREATE INDEX IF NOT EXISTS idx_platform_worker_heartbeats_status_seen
                  ON platform_worker_heartbeats(status, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_governed_memory_owner_scope_status
                  ON governed_memory_items(owner_id, scope_id, status);
                CREATE INDEX IF NOT EXISTS idx_governed_memory_expiry
                  ON governed_memory_items(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_evaluation_runs_application_created
                  ON evaluation_runs(application_id, created_at DESC);
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

    async def save_evaluation_run(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await asyncio.to_thread(
                self._execute,
                """
                INSERT INTO evaluation_runs(
                  id,application_id,profile_id,environment_id,outcome,
                  achieved_status,record_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  outcome=excluded.outcome,
                  achieved_status=excluded.achieved_status,
                  record_json=excluded.record_json,
                  updated_at=excluded.updated_at
                """,
                (
                    record["id"],
                    record["application_id"],
                    record["profile_id"],
                    record["environment_id"],
                    record["outcome"],
                    record["achieved_status"],
                    encoded,
                    record["created_at"],
                    record["updated_at"],
                ),
            )

    async def get_evaluation_run(self, run_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._get_one,
            "SELECT record_json FROM evaluation_runs WHERE id=?",
            (run_id,),
        )
        return json.loads(row["record_json"])

    async def list_evaluation_runs(
        self,
        application_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._get_all,
            """
            SELECT record_json FROM evaluation_runs
            WHERE application_id=? ORDER BY created_at DESC LIMIT ?
            """,
            (application_id, max(1, min(limit, 500))),
        )
        return [json.loads(row["record_json"]) for row in rows]

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

    async def scan_platform_tasks(self) -> list[dict[str, Any]]:
        """Return the durable task corpus for governance filtering and aggregation."""
        return await asyncio.to_thread(self._scan_platform_tasks_sync)

    def _scan_platform_tasks_sync(self) -> list[dict[str, Any]]:
        rows = self._get_all(
            "SELECT record_json FROM platform_harness_tasks ORDER BY created_at DESC, id DESC",
            (),
        )
        return [json.loads(row["record_json"]) for row in rows]

    async def claim_next_platform_task(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        kind: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_next_platform_task_sync,
                worker_id,
                lease_seconds,
                kind,
                owner_id,
            )

    def _claim_next_platform_task_sync(
        self,
        worker_id: str,
        lease_seconds: float,
        kind: str | None,
        owner_id: str | None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        expires_at = (
            self._parse_iso_datetime(now) + timedelta(seconds=lease_seconds)
            if self._parse_iso_datetime(now)
            else None
        )
        where = ["status='queued'"]
        params: list[Any] = []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if owner_id:
            where.append("owner_id=?")
            params.append(owner_id)
        where_sql = " AND ".join(where)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT id, record_json FROM platform_harness_tasks
                WHERE {where_sql}
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            record = json.loads(row["record_json"])
            record["status"] = "running"
            record["worker_id"] = worker_id
            record["lease_expires_at"] = expires_at.isoformat() if expires_at else None
            record["lease_version"] = int(record.get("lease_version", 0)) + 1
            record["updated_at"] = now
            record["finished_at"] = None
            metadata = record.setdefault("metadata", {})
            lease_metadata = metadata.setdefault("worker_lease", {})
            lease_metadata.update({
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "last_action": "queue_claimed",
                "updated_at": now,
                "queue_claimed_at": lease_metadata.get("queue_claimed_at") or now,
                "lease_version": record["lease_version"],
                "queue_claimed": True,
            })
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            updated = conn.execute(
                """
                UPDATE platform_harness_tasks
                SET status=?, record_json=?, updated_at=?, finished_at=?
                WHERE id=? AND status='queued'
                """,
                ("running", encoded, now, None, record["id"]),
            ).rowcount
            conn.execute("COMMIT")
        return record if updated == 1 else None

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

    async def fail_stale_platform_tasks(self, *, cutoff: str, error: str) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._fail_stale_platform_tasks_sync, cutoff, error)

    def _fail_stale_platform_tasks_sync(self, cutoff: str, error: str) -> list[dict[str, Any]]:
        now = utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_json FROM platform_harness_tasks
                WHERE status IN ('queued', 'running') AND updated_at < ?
                ORDER BY updated_at
                """,
                (cutoff,),
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                record = json.loads(row["record_json"])
                record["status"] = "failed"
                record["error"] = error
                record["updated_at"] = now
                record["finished_at"] = now
                metadata = record.setdefault("metadata", {})
                metadata["stale_reconciled"] = True
                metadata["stale_cutoff"] = cutoff
                encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """
                    UPDATE platform_harness_tasks
                    SET status=?, record_json=?, updated_at=?, finished_at=?
                    WHERE id=?
                    """,
                    ("failed", encoded, now, now, record["id"]),
                )
                records.append(record)
        return records

    async def fail_expired_platform_task_leases(
        self, *, cutoff: str, error: str
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(
                self._fail_expired_platform_task_leases_sync,
                cutoff,
                error,
            )

    def _fail_expired_platform_task_leases_sync(self, cutoff: str, error: str) -> list[dict[str, Any]]:
        now = utc_now()
        cutoff_dt = self._parse_iso_datetime(cutoff)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_json FROM platform_harness_tasks
                WHERE status IN ('queued', 'running')
                ORDER BY updated_at
                """
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                record = json.loads(row["record_json"])
                lease_expires_at = record.get("lease_expires_at")
                if not lease_expires_at:
                    continue
                expires_dt = self._parse_iso_datetime(str(lease_expires_at))
                if not expires_dt or expires_dt > cutoff_dt:
                    continue
                detailed_error = (
                    f"{error}: task={record.get('id')} worker={record.get('worker_id') or 'unknown'} "
                    f"lease_expires_at={lease_expires_at}"
                )
                record["status"] = "failed"
                record["error"] = detailed_error
                record["updated_at"] = now
                record["finished_at"] = now
                metadata = record.setdefault("metadata", {})
                lease_metadata = metadata.setdefault("worker_lease", {})
                lease_metadata["expired"] = True
                lease_metadata["expired_worker_id"] = record.get("worker_id")
                lease_metadata["lease_expires_at"] = lease_expires_at
                lease_metadata["expired_at"] = now
                lease_metadata["cutoff"] = cutoff
                encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """
                    UPDATE platform_harness_tasks
                    SET status=?, record_json=?, updated_at=?, finished_at=?
                    WHERE id=?
                    """,
                    ("failed", encoded, now, now, record["id"]),
                )
                records.append(record)
        return records

    async def requeue_expired_platform_task_leases(self, *, cutoff: str) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._requeue_expired_platform_task_leases_sync, cutoff)

    def _requeue_expired_platform_task_leases_sync(self, cutoff: str) -> list[dict[str, Any]]:
        now = utc_now()
        cutoff_dt = self._parse_iso_datetime(cutoff)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_json FROM platform_harness_tasks
                WHERE status='running'
                ORDER BY updated_at
                """
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                record = json.loads(row["record_json"])
                lease_expires_at = record.get("lease_expires_at")
                if not lease_expires_at:
                    continue
                expires_dt = self._parse_iso_datetime(str(lease_expires_at))
                if not expires_dt or expires_dt > cutoff_dt:
                    continue
                previous_worker_id = record.get("worker_id")
                record["status"] = "queued"
                record["worker_id"] = None
                record["lease_expires_at"] = None
                record["updated_at"] = now
                record["finished_at"] = None
                record["error"] = ""
                record["lease_version"] = int(record.get("lease_version", 0)) + 1
                metadata = record.setdefault("metadata", {})
                lease_metadata = metadata.setdefault("worker_lease", {})
                lease_metadata.update({
                    "requeued": True,
                    "requeued_at": now,
                    "requeued_from_worker_id": previous_worker_id,
                    "expired_lease_expires_at": lease_expires_at,
                    "cutoff": cutoff,
                    "last_action": "queue_requeued",
                    "lease_version": record["lease_version"],
                })
                encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """
                    UPDATE platform_harness_tasks
                    SET status=?, record_json=?, updated_at=?, finished_at=?
                    WHERE id=?
                    """,
                    ("queued", encoded, now, None, record["id"]),
                )
                records.append(record)
        return records

    async def save_platform_worker_heartbeat(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._save_platform_worker_heartbeat_sync, record)

    def _save_platform_worker_heartbeat_sync(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        persisted = dict(record)
        persisted["updated_at"] = now
        encoded = json.dumps(persisted, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_worker_heartbeats(
                  worker_id, status, active_task_id, record_json, last_seen_at, updated_at
                )
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(worker_id) DO UPDATE SET
                  status=excluded.status,
                  active_task_id=excluded.active_task_id,
                  record_json=excluded.record_json,
                  last_seen_at=excluded.last_seen_at,
                  updated_at=excluded.updated_at
                """,
                (
                    persisted["worker_id"],
                    persisted["status"],
                    persisted.get("active_task_id", ""),
                    encoded,
                    persisted["last_seen_at"],
                    persisted["updated_at"],
                ),
            )
        return persisted

    async def list_platform_worker_heartbeats(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_platform_worker_heartbeats_sync, max(1, min(limit, 500)))

    def _list_platform_worker_heartbeats_sync(self, limit: int) -> list[dict[str, Any]]:
        rows = self._get_all(
            "SELECT record_json FROM platform_worker_heartbeats ORDER BY last_seen_at DESC LIMIT ?",
            (limit,),
        )
        return [json.loads(row["record_json"]) for row in rows]

    def _parse_iso_datetime(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def save_platform_secret(
        self,
        *,
        owner_id: str,
        name: str,
        value: str,
        description: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._save_platform_secret_sync,
                owner_id,
                name,
                value,
                description,
            )

    def _save_platform_secret_sync(
        self,
        owner_id: str,
        name: str,
        value: str,
        description: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id,created_at FROM platform_secrets WHERE owner_id=? AND name=?",
                (owner_id, name),
            ).fetchone()
            secret_id = str(existing["id"]) if existing else f"{owner_id}:{name}"
            created_at = str(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT INTO platform_secrets(id, owner_id, name, description, value, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(owner_id, name) DO UPDATE SET
                  description=excluded.description,
                  value=excluded.value,
                  updated_at=excluded.updated_at
                """,
                (secret_id, owner_id, name, description, value, created_at, now),
            )
            row = conn.execute(
                "SELECT * FROM platform_secrets WHERE owner_id=? AND name=?",
                (owner_id, name),
            ).fetchone()
        return dict(row)

    async def get_platform_secret(self, *, owner_id: str, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_platform_secret_sync, owner_id, name)

    def _get_platform_secret_sync(self, owner_id: str, name: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_secrets WHERE owner_id=? AND name=?",
                (owner_id, name),
            ).fetchone()
        if not row:
            raise KeyError(f"platform secret not found: {owner_id}/{name}")
        return dict(row)

    async def list_platform_secrets(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_platform_secrets_sync, owner_id)

    def _list_platform_secrets_sync(self, owner_id: str | None) -> list[dict[str, Any]]:
        if owner_id:
            rows = self._get_all(
                "SELECT * FROM platform_secrets WHERE owner_id=? ORDER BY updated_at DESC",
                (owner_id,),
            )
        else:
            rows = self._get_all("SELECT * FROM platform_secrets ORDER BY updated_at DESC", ())
        return rows

    async def delete_platform_secret(self, *, owner_id: str, name: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_platform_secret_sync, owner_id, name)

    def _delete_platform_secret_sync(self, owner_id: str, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM platform_secrets WHERE owner_id=? AND name=?",
                (owner_id, name),
            )
        return cursor.rowcount > 0

    async def save_governed_memory_item(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._save_governed_memory_item_sync, record)

    def _save_governed_memory_item_sync(self, record: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO governed_memory_items(
                  id, owner_id, scope_id, status, source_type, source_id,
                  retention_class, expires_at, record_json, created_at, updated_at, revoked_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  owner_id=excluded.owner_id,
                  scope_id=excluded.scope_id,
                  status=excluded.status,
                  source_type=excluded.source_type,
                  source_id=excluded.source_id,
                  retention_class=excluded.retention_class,
                  expires_at=excluded.expires_at,
                  record_json=excluded.record_json,
                  updated_at=excluded.updated_at,
                  revoked_at=excluded.revoked_at
                """,
                (
                    record["id"],
                    record["owner_id"],
                    record["scope_id"],
                    record["status"],
                    record["source"]["source_type"],
                    record["source"]["source_id"],
                    record["retention_class"],
                    record["expires_at"],
                    encoded,
                    record["created_at"],
                    record["updated_at"],
                    record.get("revoked_at"),
                ),
            )
            row = conn.execute("SELECT record_json FROM governed_memory_items WHERE id=?", (record["id"],)).fetchone()
        if not row:
            raise KeyError(f"governed memory item not found after save: {record['id']}")
        return json.loads(row["record_json"])

    async def get_governed_memory_item(self, memory_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_governed_memory_item_sync, memory_id)

    def _get_governed_memory_item_sync(self, memory_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT record_json FROM governed_memory_items WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise KeyError(f"governed memory item not found: {memory_id}")
        return json.loads(row["record_json"])

    async def list_governed_memory_items(
        self,
        *,
        owner_id: str,
        scope_id: str | None = None,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_governed_memory_items_sync,
            owner_id,
            scope_id,
            statuses,
            max(1, min(limit, 500)),
        )

    def _list_governed_memory_items_sync(
        self,
        owner_id: str,
        scope_id: str | None,
        statuses: set[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = ["owner_id=?"]
        params: list[Any] = [owner_id]
        if scope_id:
            clauses.append("scope_id=?")
            params.append(scope_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(statuses))
        params.append(limit)
        where = " AND ".join(clauses)
        rows = self._get_all(
            f"SELECT record_json FROM governed_memory_items WHERE {where} ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        )
        return [json.loads(row["record_json"]) for row in rows]

    # ── 用户体系（每人独立令牌；旧 API_TOKEN 为管理员引导令牌） ──

    def _ensure_user_columns(self) -> None:
        with self._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "password_hash" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")

    async def create_user(self, name: str, token_hash: str, role: str = "member", password_hash: str | None = None) -> dict[str, Any]:
        from uuid import uuid4
        user = {
            "id": str(uuid4()), "name": name.strip(), "token_hash": token_hash,
            "role": role, "status": "active", "created_at": utc_now(),
        }
        await asyncio.to_thread(self._ensure_user_columns)
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO users(id,name,token_hash,role,status,created_at,password_hash) VALUES(?,?,?,?,?,?,?)",
            (user["id"], user["name"], user["token_hash"], user["role"], user["status"], user["created_at"], password_hash),
        )
        return user


    async def user_by_name(self, name: str) -> dict[str, Any] | None:
        await asyncio.to_thread(self._ensure_user_columns)
        def query():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id,name,role,status,created_at,password_hash FROM users WHERE name=?",
                    (name,),
                ).fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(query)

    async def rotate_user_token(self, user_id: str, token_hash: str) -> None:
        await asyncio.to_thread(
            self._execute, "UPDATE users SET token_hash=? WHERE id=?", (token_hash, user_id)
        )

    async def count_users(self) -> int:
        def query():
            with self._connect() as conn:
                return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
        return await asyncio.to_thread(query)

    async def user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        def query():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id,name,role,status,created_at FROM users WHERE token_hash=?",
                    (token_hash,),
                ).fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(query)

    async def list_users(self) -> list[dict[str, Any]]:
        def query():
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id,name,role,status,created_at FROM users ORDER BY created_at"
                ).fetchall()
                return [dict(r) for r in rows]
        return await asyncio.to_thread(query)

    async def set_user_status(self, user_id: str, status_value: str) -> None:
        await asyncio.to_thread(
            self._execute, "UPDATE users SET status=? WHERE id=?", (status_value, user_id)
        )

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
            # seq 在单条语句内原子分配（读 MAX 与写入同一语句，SQLite 写锁覆盖全程）。
            # 此前"先 SELECT MAX 再 INSERT"在取消/超时留下的 to_thread 孤儿线程与
            # 下一次写入并发时撞出 UNIQUE 冲突——bagpipe 首次异构构建的死因。
            cursor = conn.execute(
                "INSERT INTO events(stream_id, seq, event_type, data_json, created_at) "
                "SELECT ?, COALESCE(MAX(seq),0)+1, ?, ?, ? FROM events WHERE stream_id=?",
                (stream_id, event_type, encoded, now, stream_id),
            )
            row = conn.execute(
                "SELECT seq FROM events WHERE rowid=?", (cursor.lastrowid,)
            ).fetchone()
            seq = int(row["seq"])
        event = EventRecord(id=seq, stream_id=stream_id, type=event_type, data=data, created_at=now)
        path = self.events_dir / f"{stream_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
        return event

    async def list_events(self, stream_id: str, after: int = 0, *,
                          limit: int | None = None,
                          tail: bool = False) -> list[EventRecord]:
        """读一个流的事件。

        limit 一定要下推到 SQL，别「全读进来再切片」：真机上单个构建流
        有 18 万条事件，platform_harness 流 435 MB——读全量会把
        整个 API 冻住十几秒、内存冲到几 GB。
        tail=True 取最后 limit 条（要看结局时用，开头那几条没意义）。
        """
        return await asyncio.to_thread(
            self._list_events_sync, stream_id, after, limit, tail)

    def _list_events_sync(self, stream_id: str, after: int,
                          limit: int | None = None,
                          tail: bool = False) -> list[EventRecord]:
        with self._connect() as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT * FROM events WHERE stream_id=? AND seq>? ORDER BY seq",
                    (stream_id, after)).fetchall()
            elif tail:
                rows = list(reversed(conn.execute(
                    "SELECT * FROM events WHERE stream_id=? AND seq>? "
                    "ORDER BY seq DESC LIMIT ?",
                    (stream_id, after, int(limit))).fetchall()))
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE stream_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (stream_id, after, int(limit))).fetchall()
        records = [
            EventRecord(
                id=row["seq"],
                stream_id=stream_id,
                type=row["event_type"],
                data=json.loads(row["data_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
        # 已经按 limit 取够了就别补冷文件：tail 的「前段缺口」是**故意**留的，
        # 不是归档造成的。不挡这一下的话，冷读会把全量补回来，限量当场失效
        # （tail=10 实测返回 50 条）——而限量正是为了不把 18 万条读进内存。
        if limit is not None and len(records) >= limit:
            return records

        # 冷读回退：老事件被归档出 DB（追加式 JSONL 是权威全量副本）。
        # DB 结果前段有缺口时从冷文件补齐——诊断能力不因归档丢失。
        first_seq = records[0].id if records else None
        if first_seq is None or first_seq > after + 1:
            upper = first_seq if first_seq is not None else None
            cold = self._read_cold_events(stream_id, after=after, before=upper)
            if cold:
                records = cold + records
        # 补过冷文件也要守住上限
        if limit is not None and len(records) > limit:
            records = records[-limit:] if tail else records[:limit]
        return records

    def _read_cold_events(
        self, stream_id: str, *, after: int, before: int | None
    ) -> list[EventRecord]:
        import gzip

        seen: dict[int, EventRecord] = {}
        # 压缩归档（.jsonl.gz）与活跃追加（.jsonl）可能并存：压缩后 stream
        # 复活（返修）会新开 .jsonl——两者都读，seen 按序号去重。
        candidates = [
            (self.events_dir / f"{stream_id}.jsonl.gz", gzip.open),
            (self.events_dir / f"{stream_id}.jsonl", open),
        ]
        for path, opener in candidates:
            if not path.is_file():
                continue
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = EventRecord.model_validate_json(line)
                    except Exception:
                        continue
                    if record.id <= after:
                        continue
                    if before is not None and record.id >= before:
                        continue
                    seen[record.id] = record
        return [seen[key] for key in sorted(seen)]

    async def compress_cold_event_files(self, *, older_than_days: int) -> dict[str, int]:
        """把长期不活跃的事件冷文件 gzip 压缩（JSONL 压缩率约 10:1）。

        权威全量不删除；读取端自动兼容 .jsonl.gz。
        """

        return await asyncio.to_thread(self._compress_cold_event_files_sync, older_than_days)

    def _compress_cold_event_files_sync(self, older_than_days: int) -> dict[str, int]:
        import gzip
        import time as _time

        cutoff = _time.time() - max(0, older_than_days) * 86_400
        compressed = 0
        saved = 0
        for path in self.events_dir.glob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                target = path.with_suffix(".jsonl.gz")
                original = path.stat().st_size
                with path.open("rb") as src, gzip.open(target, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                path.unlink()
                compressed += 1
                saved += original - target.stat().st_size
            except Exception as error:  # noqa: BLE001 - 压不动一个文件不该停下整轮
                # 静默 continue 的问题是：磁盘一直涨，日志里什么都没有，
                # 而"压缩了 0 个"和"一个都没压成"长得一模一样。
                logger.warning("冷事件文件压不动 %s：%s", path.name, error)
                continue
        return {"compressed": compressed, "bytes_saved": saved}

    async def ensure_event_indexes(self) -> None:
        """事件表的维护用索引。

        故意不放进建表脚本：这台真机上 events 有 1 GB，建索引要扫全表，
        放启动路径上照样把服务堵住。它属于后台维护的一部分。
        """
        await asyncio.to_thread(self._ensure_event_indexes_sync)

    def _ensure_event_indexes_sync(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_created_at "
                         "ON events(created_at)")

    async def archive_events_before(self, *, keep_days: int) -> dict[str, int]:
        """把 DB 里的老事件删掉（JSONL 冷文件是权威全量，读取端自动回退）。

        每个 stream 保留其最大 seq 行作哨兵：seq 由 MAX(seq)+1 生成，
        全删会让新事件序号回退、与冷文件撞号。
        """

        return await asyncio.to_thread(self._archive_events_before_sync, keep_days)

    def _archive_events_before_sync(self, keep_days: int) -> dict[str, int]:
        with self._connect() as conn:
            if keep_days < 1:
                # 配成 0 或负数时 cutoff 就是"现在"，除了每个 stream 的哨兵行，
                # 业务事件（审计线索）会被**全部**删掉，而且悄无声息——
                # 日志上只会写一句"归档 N 行"。
                # 产物清理那一侧早就定了规矩：看不懂的配置一律当成"别删"，
                # 这是删数据的地方该有的默认方向。这里跟上，两处一个脾气。
                # （原先是 max(0, keep_days)，把负数夹成 0，
                #   而 0 恰恰是最destructive的那个值。）
                remaining = conn.execute(
                    "SELECT COUNT(*) AS c FROM events").fetchone()["c"]
                logger.warning(
                    "event_archive_keep_days=%s 会把事件删光，这次什么都不删。"
                    "真要清空请显式删库，别靠这个配置。", keep_days)
                return {"removed": 0, "remaining": remaining}
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=keep_days)
        ).isoformat()
        with self._connect() as conn:
            before = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            conn.execute(
                """DELETE FROM events WHERE created_at < ?
                   AND seq < (SELECT MAX(seq) FROM events e2
                              WHERE e2.stream_id = events.stream_id)""",
                (cutoff,),
            )
            after_count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        return {"removed": before - after_count, "remaining": after_count}

    async def database_space(self) -> dict[str, int]:
        """库文件多大、其中有多少是删完之后空出来、但**没还给磁盘**的。

        SQLite 的 DELETE 只把页标成空闲，文件不会变小（本仓 auto_vacuum=0，
        代码里也没有任何 VACUUM）。归档任务一次清掉几十万行之后，
        日志上写着"移除 N 行"，而 `ls -lh` 看到的还是原来那么大——
        运维会以为归档没起作用。

        真机 2026-08-29 的数字：库 978 MB，其中 845 MB 是
        platform_harness.usage.recorded；那批是**一个已经修好的 bug**
        留下的（当时每条事件都把任务至今的全部用量明细抄一份，平方级增长）。
        修完不再长，但存量还在，而且要等它过了保留期才轮得到清。

        只报数，不动手：VACUUM 要重写整个库、拿排他锁、临时占两倍磁盘，
        那是运维该挑时间做的事，不是后台任务该偷偷做的。
        见 scripts/compact_db.py。
        """
        def _read() -> dict[str, int]:
            with self._connect() as conn:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            return {
                "bytes": int(page_size) * int(page_count),
                "reclaimable_bytes": int(page_size) * int(free),
            }

        return await asyncio.to_thread(_read)

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
