from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import uuid4

from .models import utc_now
from .storage import Storage
from .workflow_models import ApplicationCreateRequest, ApplicationSnapshot, BuildTeamState, WorkflowRunState


class RevisionConflict(RuntimeError):
    pass


class PublishGateError(RuntimeError):
    pass


PLACEHOLDER_NAMES = {
    "untitled",
    "untitled agent",
    "untitled application",
    "未命名",
    "未命名智能体",
    "未命名应用",
}


def _is_placeholder_name(name: str) -> bool:
    return name.strip().lower() in PLACEHOLDER_NAMES


def _derive_application_name(requirement: str) -> str:
    text = re.sub(r"\s+", " ", requirement.strip())
    if not text:
        return "新智能体"
    first = re.split(r"[。.!?\n\r]", text, maxsplit=1)[0].strip(" ，,：:；;\"'“”‘’`")
    first = re.sub(
        r"^(请|请帮我|我需要|我想要|帮我|帮我做|搭建|创建|制作|生成|构建|设计)(一个|一款|一个可以|可以|能够|能)?",
        "",
        first,
    ).strip(" ，,：:；;")
    first = re.sub(
        r"^(please|build|create|make|generate|design)\s+(a|an|the)?\s*",
        "",
        first,
        flags=re.IGNORECASE,
    ).strip(" ,:;")
    if not first:
        first = text
    return first[:32].rstrip(" ，,：:；;") or "新智能体"


class WorkflowStorage:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self.storage._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  requirement TEXT NOT NULL,
                  active_version INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS application_drafts (
                  application_id TEXT PRIMARY KEY,
                  revision INTEGER NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  tested_hash TEXT,
                  validation_report_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS application_versions (
                  application_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  validation_report_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(application_id, version),
                  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS draft_idempotency (
                  application_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(application_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS builds (
                  id TEXT PRIMARY KEY,
                  application_id TEXT NOT NULL,
                  requirement TEXT NOT NULL,
                  status TEXT NOT NULL,
                  auto_publish INTEGER NOT NULL,
                  max_turns INTEGER NOT NULL,
                  max_repair_cycles INTEGER NOT NULL,
                  team_state_json TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                  id TEXT PRIMARY KEY,
                  application_id TEXT NOT NULL,
                  version INTEGER,
                  draft_revision INTEGER,
                  status TEXT NOT NULL,
                  state_json TEXT NOT NULL,
                  outputs_json TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS schedule_fires (
                  application_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  node_id TEXT NOT NULL,
                  local_date TEXT NOT NULL,
                  run_id TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(application_id, version, node_id, local_date),
                  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_application_versions_app
                  ON application_versions(application_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_builds_app ON builds(application_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_app ON workflow_runs(application_id, created_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(builds)").fetchall()
            }
            if "max_elapsed_seconds" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN max_elapsed_seconds REAL")

    async def create_application(self, request: ApplicationCreateRequest) -> dict[str, Any]:
        application_id = str(uuid4())
        name = request.name.strip()
        if _is_placeholder_name(name):
            name = _derive_application_name(request.requirement)
        snapshot = ApplicationSnapshot(
            name=name,
            description=request.description or request.requirement[:180],
            mode=request.mode,
            requirement=request.requirement,
        )
        async with self._lock:
            await asyncio.to_thread(self._create_application_sync, application_id, snapshot)
        return await self.get_application(application_id)

    def _create_application_sync(self, application_id: str, snapshot: ApplicationSnapshot) -> None:
        now = utc_now()
        encoded = snapshot.model_dump_json(exclude_none=True)
        content_hash = snapshot.content_hash()
        with self.storage._connect() as conn:
            conn.execute(
                "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
                (
                    application_id,
                    snapshot.name,
                    snapshot.description,
                    snapshot.mode.value,
                    snapshot.requirement,
                    None,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO application_drafts VALUES(?,?,?,?,?,?,?)",
                (application_id, 0, encoded, content_hash, None, "{}", now),
            )

    async def list_applications(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_applications_sync)

    def _list_applications_sync(self) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT a.*, d.revision AS draft_revision, d.tested_hash, d.content_hash
                   FROM applications a JOIN application_drafts d ON d.application_id=a.id
                   ORDER BY a.updated_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_application(self, application_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_application_sync, application_id)

    def _get_application_sync(self, application_id: str) -> dict[str, Any]:
        with self.storage._connect() as conn:
            row = conn.execute(
                """SELECT a.*, d.revision AS draft_revision, d.tested_hash, d.content_hash
                   FROM applications a JOIN application_drafts d ON d.application_id=a.id
                   WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"application not found: {application_id}")
            return dict(row)

    async def get_draft(self, application_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_draft_sync, application_id)

    def _get_draft_sync(self, application_id: str) -> dict[str, Any]:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT * FROM application_drafts WHERE application_id=?", (application_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"application draft not found: {application_id}")
            result = dict(row)
            result["snapshot"] = ApplicationSnapshot.model_validate_json(result.pop("snapshot_json"))
            result["validation_report"] = json.loads(result.pop("validation_report_json"))
            return result

    async def save_draft(
        self,
        application_id: str,
        snapshot: ApplicationSnapshot,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._save_draft_sync,
                application_id,
                snapshot,
                expected_revision,
                idempotency_key,
            )

    def _save_draft_sync(
        self,
        application_id: str,
        snapshot: ApplicationSnapshot,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.storage._connect() as conn:
            previous = conn.execute(
                "SELECT response_json FROM draft_idempotency WHERE application_id=? AND idempotency_key=?",
                (application_id, idempotency_key),
            ).fetchone()
            if previous:
                return json.loads(previous["response_json"])
            row = conn.execute(
                "SELECT revision FROM application_drafts WHERE application_id=?", (application_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"application draft not found: {application_id}")
            if int(row["revision"]) != expected_revision:
                raise RevisionConflict(
                    f"draft revision conflict: expected {expected_revision}, current {row['revision']}"
                )
            revision = expected_revision + 1
            content_hash = snapshot.content_hash()
            conn.execute(
                """UPDATE application_drafts SET revision=?,snapshot_json=?,content_hash=?,
                   tested_hash=NULL,validation_report_json='{}',updated_at=? WHERE application_id=?""",
                (revision, snapshot.model_dump_json(exclude_none=True), content_hash, now, application_id),
            )
            conn.execute(
                """UPDATE applications SET name=?,description=?,mode=?,requirement=?,updated_at=?
                   WHERE id=?""",
                (
                    snapshot.name,
                    snapshot.description,
                    snapshot.mode.value,
                    snapshot.requirement,
                    now,
                    application_id,
                ),
            )
            response = {"application_id": application_id, "revision": revision, "content_hash": content_hash}
            conn.execute(
                "INSERT INTO draft_idempotency VALUES(?,?,?,?)",
                (application_id, idempotency_key, json.dumps(response), now),
            )
            return response

    async def mark_tested(
        self, application_id: str, revision: int, content_hash: str, report: dict[str, Any]
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mark_tested_sync, application_id, revision, content_hash, report
            )

    def _mark_tested_sync(
        self, application_id: str, revision: int, content_hash: str, report: dict[str, Any]
    ) -> None:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT revision,content_hash FROM application_drafts WHERE application_id=?",
                (application_id,),
            ).fetchone()
            if not row or int(row["revision"]) != revision or row["content_hash"] != content_hash:
                raise RevisionConflict("draft changed while tests were running")
            conn.execute(
                """UPDATE application_drafts SET tested_hash=?,validation_report_json=?,updated_at=?
                   WHERE application_id=?""",
                (content_hash, json.dumps(report, ensure_ascii=False), utc_now(), application_id),
            )

    async def publish(self, application_id: str) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._publish_sync, application_id)

    def _publish_sync(self, application_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.storage._connect() as conn:
            draft = conn.execute(
                "SELECT * FROM application_drafts WHERE application_id=?", (application_id,)
            ).fetchone()
            if not draft:
                raise KeyError(f"application draft not found: {application_id}")
            if not draft["tested_hash"] or draft["tested_hash"] != draft["content_hash"]:
                raise PublishGateError("current draft has not passed its mandatory tests")
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS version FROM application_versions WHERE application_id=?",
                (application_id,),
            ).fetchone()
            version = int(row["version"])
            conn.execute(
                "INSERT INTO application_versions VALUES(?,?,?,?,?,?)",
                (
                    application_id,
                    version,
                    draft["snapshot_json"],
                    draft["content_hash"],
                    draft["validation_report_json"],
                    now,
                ),
            )
            conn.execute(
                "UPDATE applications SET active_version=?,updated_at=? WHERE id=?",
                (version, now, application_id),
            )
            return {"application_id": application_id, "version": version, "content_hash": draft["content_hash"]}

    async def list_versions(self, application_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_versions_sync, application_id)

    def _list_versions_sync(self, application_id: str) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT application_id,version,content_hash,validation_report_json,created_at
                   FROM application_versions WHERE application_id=? ORDER BY version DESC""",
                (application_id,),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["validation_report"] = json.loads(item.pop("validation_report_json"))
                results.append(item)
            return results

    async def get_version(self, application_id: str, version: int | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_version_sync, application_id, version)

    def _get_version_sync(self, application_id: str, version: int | None) -> dict[str, Any]:
        with self.storage._connect() as conn:
            if version is None:
                app = conn.execute(
                    "SELECT active_version FROM applications WHERE id=?", (application_id,)
                ).fetchone()
                if not app or app["active_version"] is None:
                    raise KeyError(f"application has no published version: {application_id}")
                version = int(app["active_version"])
            row = conn.execute(
                "SELECT * FROM application_versions WHERE application_id=? AND version=?",
                (application_id, version),
            ).fetchone()
            if not row:
                raise KeyError(f"application version not found: {application_id}@{version}")
            result = dict(row)
            result["snapshot"] = ApplicationSnapshot.model_validate_json(result.pop("snapshot_json"))
            result["validation_report"] = json.loads(result.pop("validation_report_json"))
            return result

    async def restore_version(self, application_id: str, version: int) -> dict[str, Any]:
        published = await self.get_version(application_id, version)
        draft = await self.get_draft(application_id)
        return await self.save_draft(
            application_id,
            published["snapshot"],
            expected_revision=draft["revision"],
            idempotency_key=f"restore:{version}:{uuid4()}",
        )

    async def create_build(
        self,
        build_id: str,
        application_id: str,
        requirement: str,
        auto_publish: bool,
        max_turns: int,
        max_repair_cycles: int,
        max_elapsed_seconds: float | None = None,
        planning_mode: str = "auto",
        complexity_router: dict[str, Any] | None = None,
        runtime_builder_policy: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        team_state = BuildTeamState(
            planning_mode=planning_mode,
            complexity_router=complexity_router,
            runtime_builder_policy=runtime_builder_policy,
        )
        async with self._lock:
            await asyncio.to_thread(
                self.storage._execute,
                """
                INSERT INTO builds (
                  id,
                  application_id,
                  requirement,
                  status,
                  auto_publish,
                  max_turns,
                  max_repair_cycles,
                  team_state_json,
                  error,
                  created_at,
                  updated_at,
                  max_elapsed_seconds
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    build_id,
                    application_id,
                    requirement,
                    "queued",
                    int(auto_publish),
                    max_turns,
                    max_repair_cycles,
                    team_state.model_dump_json(),
                    None,
                    now,
                    now,
                    max_elapsed_seconds,
                ),
            )

    async def update_build(
        self,
        build_id: str,
        *,
        status: str | None = None,
        team_state: BuildTeamState | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status
        if team_state is not None:
            values["team_state_json"] = team_state.model_dump_json()
        if error is not None:
            values["error"] = error
        columns = ",".join(f"{key}=?" for key in values)
        async with self._lock:
            await asyncio.to_thread(
                self.storage._execute,
                f"UPDATE builds SET {columns} WHERE id=?",
                tuple(values.values()) + (build_id,),
            )

    async def get_build(self, build_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(self.storage._get_one, "SELECT * FROM builds WHERE id=?", (build_id,))
        row["team_state"] = BuildTeamState.model_validate_json(row.pop("team_state_json"))
        row["auto_publish"] = bool(row["auto_publish"])
        return row

    async def list_builds(self, application_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._list_builds_sync, application_id)
        for row in rows:
            row["team_state"] = BuildTeamState.model_validate_json(row.pop("team_state_json"))
            row["auto_publish"] = bool(row["auto_publish"])
        return rows

    async def list_recent_builds(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._list_recent_builds_sync, max(1, min(limit, 500)))
        for row in rows:
            row["team_state"] = BuildTeamState.model_validate_json(row.pop("team_state_json"))
            row["auto_publish"] = bool(row["auto_publish"])
        return rows

    def _list_builds_sync(self, application_id: str) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM builds WHERE application_id=? ORDER BY created_at DESC",
                (application_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _list_recent_builds_sync(self, limit: int) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM builds ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def create_run(
        self,
        state: WorkflowRunState,
        *,
        version: int | None,
        draft_revision: int | None,
    ) -> None:
        now = utc_now()
        async with self._lock:
            await asyncio.to_thread(
                self.storage._execute,
                "INSERT INTO workflow_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    state.run_id,
                    state.application_id,
                    version,
                    draft_revision,
                    "queued",
                    state.model_dump_json(exclude_none=True),
                    "{}",
                    None,
                    now,
                    now,
                ),
            )

    async def fail_interrupted_runs(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._fail_interrupted_runs_sync)

    def _fail_interrupted_runs_sync(self) -> None:
        with self.storage._connect() as conn:
            conn.execute(
                """UPDATE workflow_runs
                   SET status='failed', error='interrupted by process restart', updated_at=?
                   WHERE status IN ('queued','running')""",
                (utc_now(),),
            )

    async def update_run(
        self,
        run_id: str,
        *,
        status: str,
        state: WorkflowRunState,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self.storage._execute,
                """UPDATE workflow_runs SET status=?,state_json=?,outputs_json=?,error=?,updated_at=?
                   WHERE id=?""",
                (
                    status,
                    state.model_dump_json(exclude_none=True),
                    json.dumps(outputs or {}, ensure_ascii=False),
                    error,
                    utc_now(),
                    run_id,
                ),
            )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self.storage._get_one, "SELECT * FROM workflow_runs WHERE id=?", (run_id,)
        )
        row["state"] = WorkflowRunState.model_validate_json(row.pop("state_json"))
        row["outputs"] = json.loads(row.pop("outputs_json"))
        return row

    async def claim_schedule_fire(
        self, application_id: str, version: int, node_id: str, local_date: str
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_schedule_fire_sync,
                application_id,
                version,
                node_id,
                local_date,
            )

    def _claim_schedule_fire_sync(
        self, application_id: str, version: int, node_id: str, local_date: str
    ) -> bool:
        with self.storage._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO schedule_fires
                   (application_id,version,node_id,local_date,run_id,created_at)
                   VALUES(?,?,?,?,NULL,?)""",
                (application_id, version, node_id, local_date, utc_now()),
            )
            if cursor.rowcount == 1:
                return True
            existing = conn.execute(
                """SELECT sf.run_id, wr.status
                   FROM schedule_fires sf
                   LEFT JOIN workflow_runs wr ON wr.id=sf.run_id
                   WHERE sf.application_id=? AND sf.version=? AND sf.node_id=? AND sf.local_date=?""",
                (application_id, version, node_id, local_date),
            ).fetchone()
            if existing and existing["run_id"] and existing["status"] in {"failed", "cancelled"}:
                conn.execute(
                    """DELETE FROM schedule_fires
                       WHERE application_id=? AND version=? AND node_id=? AND local_date=?""",
                    (application_id, version, node_id, local_date),
                )
                conn.execute(
                    """INSERT INTO schedule_fires
                       (application_id,version,node_id,local_date,run_id,created_at)
                       VALUES(?,?,?,?,NULL,?)""",
                    (application_id, version, node_id, local_date, utc_now()),
                )
                return True
            return False

    async def complete_schedule_fire(
        self,
        application_id: str,
        version: int,
        node_id: str,
        local_date: str,
        run_id: str,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self.storage._execute,
                """UPDATE schedule_fires SET run_id=? WHERE application_id=? AND version=?
                   AND node_id=? AND local_date=?""",
                (run_id, application_id, version, node_id, local_date),
            )

    async def release_schedule_fire(
        self, application_id: str, version: int, node_id: str, local_date: str
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self.storage._execute,
                """DELETE FROM schedule_fires WHERE application_id=? AND version=?
                   AND node_id=? AND local_date=? AND run_id IS NULL""",
                (application_id, version, node_id, local_date),
            )

    async def list_schedule_fires(self, application_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_schedule_fires_sync, application_id)

    def _list_schedule_fires_sync(self, application_id: str | None) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            if application_id:
                rows = conn.execute(
                    "SELECT * FROM schedule_fires WHERE application_id=? ORDER BY created_at DESC",
                    (application_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM schedule_fires ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
            return [dict(row) for row in rows]
