from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from .capability_contracts import CapabilityBuildContract
from .delivery_policy import resolve_delivery_policy
from .execution_policy import ExecutionPolicySnapshot
from .models import utc_now
from .storage import Storage
from .workflow_models import (
    ApplicationCreateRequest,
    ApplicationSnapshot,
    BuildTeamState,
    WorkflowRunState,
)


WORKFLOW_VALIDATION_CONTRACT_DIGEST = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [
                    "workflow_and_binding_validation",
                    "mandatory_acceptance_tests",
                    "simulated_human_input_validation",
                ],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
)


class RevisionConflict(RuntimeError):
    pass


class PublishGateError(RuntimeError):
    def __init__(self, message: str, decision: dict[str, Any]) -> None:
        super().__init__(message)
        self.decision = decision


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
    raw = requirement.strip()
    section_match = re.search(
        r"(?:^|\n)\s{0,3}#{0,4}\s*(?:业务目标|目标|Business goal|Goal)\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s{0,3}#{1,4}\s+|\n\s*(?:启动输入|工作流步骤|运行时界面|可编辑块|权限与边界|验收|下一步|Start input|Workflow steps|Runtime interface|Editable blocks|Permissions|Acceptance|Next)|$)",
        raw,
        flags=re.IGNORECASE,
    )
    source = section_match.group(1).strip() if section_match else raw
    lines = []
    for line in source.splitlines():
        cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        cleaned = re.sub(r"^\s*[-*+]\s*", "", cleaned)
        cleaned = re.sub(r"^\s*\d+[.)、]\s*", "", cleaned)
        cleaned = cleaned.replace("**", "").strip()
        if not cleaned:
            continue
        if re.match(
            r"^(工作流构建需求|工作流搭建方案|请按以下补全需求生成一个可编辑工作流|Workflow-building plan|Workflow requirement)$",
            cleaned,
            flags=re.IGNORECASE,
        ):
            continue
        lines.append(cleaned)
    text = re.sub(r"\s+", " ", " ".join(lines).strip())
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
    first = re.sub(r"[，,]\s*(支持|用于|并|以及|and|with).*$", "", first, flags=re.IGNORECASE)
    if not first:
        first = text
    return first[:32].rstrip(" ，,：:；;") or "新智能体"


class WorkflowStorage:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._lock = asyncio.Lock()
        self.on_draft_changed: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self.on_draft_changed_in_transaction: (
            Callable[[sqlite3.Connection, str, dict[str, Any]], None] | None
        ) = None

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
                  delivery_mode TEXT NOT NULL DEFAULT 'guided',
                  governed_hard_gate INTEGER NOT NULL DEFAULT 0,
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
                  validation_contract_digest TEXT NOT NULL DEFAULT '',
                  evidence_invalidated_at TEXT,
                  evidence_invalidated_revision INTEGER,
                  evidence_change_summary_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS application_versions (
                  application_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  validation_report_json TEXT NOT NULL,
                  validation_contract_digest TEXT NOT NULL DEFAULT '',
                  publication_decision_json TEXT NOT NULL DEFAULT '{}',
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
                CREATE TABLE IF NOT EXISTS formal_draft_baselines (
                  assignment_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  application_id TEXT NOT NULL,
                  baseline_revision INTEGER NOT NULL CHECK(baseline_revision >= 0),
                  baseline_content_hash TEXT NOT NULL,
                  started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS formal_draft_mutations (
                  mutation_id TEXT PRIMARY KEY,
                  assignment_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  application_id TEXT NOT NULL,
                  actor_kind TEXT NOT NULL CHECK(
                    actor_kind IN ('lilies_blackbox','unattributed')
                  ),
                  request_id TEXT,
                  tool_call_id TEXT,
                  operation TEXT NOT NULL,
                  operation_digest TEXT NOT NULL,
                  request_payload_digest TEXT,
                  idempotency_key TEXT NOT NULL,
                  before_revision INTEGER NOT NULL CHECK(before_revision >= 0),
                  before_content_hash TEXT NOT NULL,
                  after_revision INTEGER NOT NULL CHECK(after_revision > 0),
                  after_content_hash TEXT NOT NULL,
                  content_changed INTEGER NOT NULL CHECK(content_changed IN (0,1)),
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(assignment_id) REFERENCES formal_draft_baselines(assignment_id)
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
                CREATE INDEX IF NOT EXISTS idx_formal_draft_baselines_app
                  ON formal_draft_baselines(application_id,started_at,assignment_id);
                CREATE INDEX IF NOT EXISTS idx_formal_draft_mutations_assignment
                  ON formal_draft_mutations(assignment_id,after_revision,mutation_id);
                CREATE TRIGGER IF NOT EXISTS formal_draft_baselines_no_update
                  BEFORE UPDATE ON formal_draft_baselines
                  BEGIN SELECT RAISE(ABORT, 'formal draft baseline is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS formal_draft_baselines_no_delete
                  BEFORE DELETE ON formal_draft_baselines
                  BEGIN SELECT RAISE(ABORT, 'formal draft baseline is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS formal_draft_mutations_no_update
                  BEFORE UPDATE ON formal_draft_mutations
                  BEGIN SELECT RAISE(ABORT, 'formal draft mutation audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS formal_draft_mutations_no_delete
                  BEFORE DELETE ON formal_draft_mutations
                  BEGIN SELECT RAISE(ABORT, 'formal draft mutation audit is immutable'); END;
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(builds)").fetchall()
            }
            if "max_elapsed_seconds" not in columns:
                conn.execute("ALTER TABLE builds ADD COLUMN max_elapsed_seconds REAL")
            application_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(applications)").fetchall()
            }
            if "delivery_mode" not in application_columns:
                conn.execute(
                    "ALTER TABLE applications ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'guided'"
                )
            if "governed_hard_gate" not in application_columns:
                conn.execute(
                    "ALTER TABLE applications ADD COLUMN governed_hard_gate INTEGER NOT NULL DEFAULT 0"
                )
            draft_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(application_drafts)").fetchall()
            }
            if "evidence_invalidated_at" not in draft_columns:
                conn.execute("ALTER TABLE application_drafts ADD COLUMN evidence_invalidated_at TEXT")
            if "evidence_invalidated_revision" not in draft_columns:
                conn.execute(
                    "ALTER TABLE application_drafts ADD COLUMN evidence_invalidated_revision INTEGER"
                )
            if "evidence_change_summary_json" not in draft_columns:
                conn.execute(
                    "ALTER TABLE application_drafts ADD COLUMN evidence_change_summary_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "validation_contract_digest" not in draft_columns:
                conn.execute(
                    "ALTER TABLE application_drafts ADD COLUMN "
                    "validation_contract_digest TEXT NOT NULL DEFAULT ''"
                )
            version_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(application_versions)").fetchall()
            }
            if "publication_decision_json" not in version_columns:
                conn.execute(
                    "ALTER TABLE application_versions ADD COLUMN publication_decision_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "validation_contract_digest" not in version_columns:
                conn.execute(
                    "ALTER TABLE application_versions ADD COLUMN "
                    "validation_contract_digest TEXT NOT NULL DEFAULT ''"
                )

    async def create_application(self, request: ApplicationCreateRequest) -> dict[str, Any]:
        application_id = str(uuid4())
        name = request.name.strip()
        if _is_placeholder_name(name):
            name = _derive_application_name(request.requirement)
        snapshot = ApplicationSnapshot(
            name=name,
            description=request.description or request.requirement[:180],
            mode=request.mode,
            delivery_mode=request.delivery_mode,
            governed_hard_gate=request.governed_hard_gate,
            requirement=request.requirement,
            capability_build_contract=request.capability_build_contract,
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
                """INSERT INTO applications(
                     id,name,description,mode,delivery_mode,governed_hard_gate,requirement,
                     active_version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    application_id,
                    snapshot.name,
                    snapshot.description,
                    snapshot.mode.value,
                    snapshot.delivery_mode.value,
                    int(snapshot.governed_hard_gate),
                    snapshot.requirement,
                    None,
                    now,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO application_drafts(
                     application_id,revision,snapshot_json,content_hash,tested_hash,
                     validation_report_json,validation_contract_digest,evidence_invalidated_at,
                     evidence_invalidated_revision,evidence_change_summary_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    application_id,
                    0,
                    encoded,
                    content_hash,
                    None,
                    "{}",
                    "",
                    None,
                    None,
                    "[]",
                    now,
                ),
            )

    async def list_applications(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_applications_sync)

    def _list_applications_sync(self) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT a.*, d.revision AS draft_revision, d.tested_hash, d.content_hash,
                          d.evidence_invalidated_at,d.evidence_invalidated_revision,
                          d.evidence_change_summary_json,d.snapshot_json AS draft_snapshot_json
                   FROM applications a JOIN application_drafts d ON d.application_id=a.id
                   ORDER BY a.updated_at DESC"""
            ).fetchall()
            return [self._application_result(dict(row)) for row in rows]

    async def smoke_cleanup_application(
        self,
        application_id: str,
        *,
        smoke_marker: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._smoke_cleanup_application_sync,
                application_id,
                smoke_marker,
                dry_run,
            )

    def _smoke_cleanup_application_sync(
        self,
        application_id: str,
        smoke_marker: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"v\d+\.\d+\.\d+-smoke", smoke_marker):
            raise ValueError("smoke_marker must match v<major>.<minor>.<patch>-smoke")
        related_tables = [
            "application_drafts",
            "application_versions",
            "builds",
            "workflow_runs",
            "schedule_fires",
            "draft_idempotency",
        ]
        with self.storage._connect() as conn:
            row = conn.execute(
                """SELECT a.id,a.name,a.description,a.requirement,d.snapshot_json
                   FROM applications a
                   LEFT JOIN application_drafts d ON d.application_id=a.id
                   WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"application not found: {application_id}")
            haystack = "\n".join(
                str(row[key] or "")
                for key in ("name", "description", "requirement", "snapshot_json")
            )
            if smoke_marker not in haystack:
                raise ValueError(f"application does not contain smoke marker: {smoke_marker}")
            related_counts = {
                table: int(conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE application_id=?",
                    (application_id,),
                ).fetchone()["count"])
                for table in related_tables
            }
            if not dry_run:
                conn.execute("DELETE FROM draft_idempotency WHERE application_id=?", (application_id,))
                conn.execute("DELETE FROM applications WHERE id=?", (application_id,))
            return {
                "application_id": application_id,
                "smoke_marker": smoke_marker,
                "dry_run": dry_run,
                "deleted": not dry_run,
                "related_counts": related_counts,
            }

    async def get_application(self, application_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_application_sync, application_id)

    def _get_application_sync(self, application_id: str) -> dict[str, Any]:
        with self.storage._connect() as conn:
            row = conn.execute(
                """SELECT a.*, d.revision AS draft_revision, d.tested_hash, d.content_hash,
                          d.validation_report_json,d.evidence_invalidated_at,
                          d.evidence_invalidated_revision,d.evidence_change_summary_json,
                          d.snapshot_json AS draft_snapshot_json
                   FROM applications a JOIN application_drafts d ON d.application_id=a.id
                   WHERE a.id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"application not found: {application_id}")
            return self._application_result(dict(row))

    async def get_draft(self, application_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_draft_sync, application_id)

    async def begin_formal_draft_provenance(
        self,
        *,
        assignment_id: str,
        session_id: str,
        application_id: str,
    ) -> dict[str, Any]:
        """Freeze the exact draft state before a formal agent receives authority."""

        if not assignment_id or not session_id or not application_id:
            raise ValueError("formal draft baseline requires complete binding IDs")
        async with self._lock:
            return await asyncio.to_thread(
                self._begin_formal_draft_provenance_sync,
                assignment_id,
                session_id,
                application_id,
            )

    def _begin_formal_draft_provenance_sync(
        self,
        assignment_id: str,
        session_id: str,
        application_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM formal_draft_baselines WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if existing is not None:
                persisted = dict(existing)
                if (
                    persisted["session_id"] != session_id
                    or persisted["application_id"] != application_id
                ):
                    raise RevisionConflict(
                        "formal assignment baseline was reused with another binding"
                    )
                return persisted
            draft = conn.execute(
                """
                SELECT revision,content_hash FROM application_drafts
                WHERE application_id=?
                """,
                (application_id,),
            ).fetchone()
            if draft is None:
                raise KeyError(f"application draft not found: {application_id}")
            conn.execute(
                """
                INSERT INTO formal_draft_baselines(
                  assignment_id,session_id,application_id,baseline_revision,
                  baseline_content_hash,started_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    assignment_id,
                    session_id,
                    application_id,
                    int(draft["revision"]),
                    str(draft["content_hash"]),
                    now,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM formal_draft_baselines WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if stored is None:  # pragma: no cover - protected by insert
                raise RuntimeError("formal draft baseline did not persist")
            return dict(stored)

    def _get_draft_sync(self, application_id: str) -> dict[str, Any]:
        with self.storage._connect() as conn:
            row = conn.execute(
                """SELECT d.*,a.delivery_mode,a.governed_hard_gate
                   FROM application_drafts d JOIN applications a ON a.id=d.application_id
                   WHERE d.application_id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"application draft not found: {application_id}")
            result = dict(row)
            result["snapshot"] = ApplicationSnapshot.model_validate_json(result.pop("snapshot_json"))
            result["validation_report"] = json.loads(result.pop("validation_report_json"))
            result["governed_hard_gate"] = bool(result["governed_hard_gate"])
            result["delivery_policy"] = resolve_delivery_policy(
                result["delivery_mode"],
                governed_hard_gate=result["governed_hard_gate"],
            ).model_dump(mode="json")
            result["evidence"] = self._evidence_result(result, result["validation_report"])
            return result

    async def save_draft(
        self,
        application_id: str,
        snapshot: ApplicationSnapshot,
        *,
        expected_revision: int,
        idempotency_key: str,
        change_context: dict[str, Any] | None = None,
        idempotency_digest: str | None = None,
        formal_mutation_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            result = await asyncio.to_thread(
                self._save_draft_sync,
                application_id,
                snapshot,
                expected_revision,
                idempotency_key,
                change_context,
                idempotency_digest,
                formal_mutation_context,
            )
        idempotent_replay = bool(result.pop("_idempotent_replay", False))
        if self.on_draft_changed is not None and not idempotent_replay:
            await self.on_draft_changed(application_id, result)
        return result

    def _save_draft_sync(
        self,
        application_id: str,
        snapshot: ApplicationSnapshot,
        expected_revision: int,
        idempotency_key: str,
        change_context: dict[str, Any] | None,
        idempotency_digest: str | None,
        formal_mutation_context: dict[str, str] | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT response_json FROM draft_idempotency WHERE application_id=? AND idempotency_key=?",
                (application_id, idempotency_key),
            ).fetchone()
            if previous:
                replay = json.loads(previous["response_json"])
                stored_digest = replay.pop("_idempotency_digest", None)
                if stored_digest and not idempotency_digest:
                    raise RevisionConflict(
                        "idempotency key belongs to a different verified draft mutation"
                    )
                if idempotency_digest and not stored_digest:
                    raise RevisionConflict(
                        "idempotency key belongs to an older unverifiable draft mutation"
                    )
                if (
                    idempotency_digest
                    and stored_digest
                    and stored_digest != idempotency_digest
                ):
                    raise RevisionConflict(
                        "idempotency key was already used for a different draft mutation"
                    )
                replay["_idempotent_replay"] = True
                return replay
            row = conn.execute(
                """SELECT revision,content_hash,tested_hash,validation_report_json,
                          evidence_invalidated_at,evidence_invalidated_revision,
                          evidence_change_summary_json
                   FROM application_drafts WHERE application_id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"application draft not found: {application_id}")
            if int(row["revision"]) != expected_revision:
                raise RevisionConflict(
                    f"draft revision conflict: expected {expected_revision}, current {row['revision']}"
                )
            revision = expected_revision + 1
            content_hash = snapshot.content_hash()
            content_changed = content_hash != row["content_hash"]
            invalidated_at = row["evidence_invalidated_at"]
            invalidated_revision = row["evidence_invalidated_revision"]
            change_summary = self._json_list(row["evidence_change_summary_json"])
            if content_changed and row["tested_hash"]:
                invalidated_at = now
                invalidated_revision = revision
                summary = dict(change_context or {"operation": "draft_update"})
                summary.update({"revision": revision, "invalidated_at": now})
                change_summary = [*change_summary, summary][-20:]
            changed = conn.execute(
                """UPDATE application_drafts SET revision=?,snapshot_json=?,content_hash=?,
                   evidence_invalidated_at=?,evidence_invalidated_revision=?,
                   evidence_change_summary_json=?,updated_at=?
                   WHERE application_id=? AND revision=?""",
                (
                    revision,
                    snapshot.model_dump_json(exclude_none=True),
                    content_hash,
                    invalidated_at,
                    invalidated_revision,
                    json.dumps(change_summary, ensure_ascii=False),
                    now,
                    application_id,
                    expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise RevisionConflict(
                    "draft revision changed during compare-and-set"
                )
            conn.execute(
                """UPDATE applications SET name=?,description=?,mode=?,delivery_mode=?,
                   governed_hard_gate=?,requirement=?,updated_at=?
                   WHERE id=?""",
                (
                    snapshot.name,
                    snapshot.description,
                    snapshot.mode.value,
                    snapshot.delivery_mode.value,
                    int(snapshot.governed_hard_gate),
                    snapshot.requirement,
                    now,
                    application_id,
                ),
            )
            response = {
                "application_id": application_id,
                "revision": revision,
                "content_hash": content_hash,
                "evidence_state": self._evidence_state(row["tested_hash"], content_hash),
            }
            baselines = conn.execute(
                """
                SELECT * FROM formal_draft_baselines
                WHERE application_id=? AND baseline_revision<?
                ORDER BY started_at,assignment_id
                """,
                (application_id, revision),
            ).fetchall()
            context = formal_mutation_context or {}
            operation = str(
                context.get("operation")
                or (change_context or {}).get("operation")
                or "draft_update"
            )
            operation_digest = str(
                idempotency_digest
                or "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "application_id": application_id,
                            "expected_revision": expected_revision,
                            "operation": operation,
                            "idempotency_key": idempotency_key,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
            )
            for baseline in baselines:
                bound = (
                    str(context.get("assignment_id") or "")
                    == str(baseline["assignment_id"])
                    and str(context.get("session_id") or "")
                    == str(baseline["session_id"])
                    and str(context.get("application_id") or application_id)
                    == application_id
                    and bool(context.get("request_id"))
                    and bool(context.get("tool_call_id"))
                    and bool(context.get("request_payload_digest"))
                )
                actor_kind = "lilies_blackbox" if bound else "unattributed"
                mutation_id = hashlib.sha256(
                    (
                        f"{baseline['assignment_id']}:{application_id}:"
                        f"{expected_revision}:{revision}:{idempotency_key}"
                    ).encode()
                ).hexdigest()
                conn.execute(
                    """
                    INSERT INTO formal_draft_mutations(
                      mutation_id,assignment_id,session_id,application_id,
                      actor_kind,request_id,tool_call_id,operation,
                      operation_digest,request_payload_digest,idempotency_key,before_revision,
                      before_content_hash,after_revision,after_content_hash,
                      content_changed,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        mutation_id,
                        str(baseline["assignment_id"]),
                        str(baseline["session_id"]),
                        application_id,
                        actor_kind,
                        context.get("request_id") if bound else None,
                        context.get("tool_call_id") if bound else None,
                        operation,
                        operation_digest,
                        context.get("request_payload_digest") if bound else None,
                        idempotency_key,
                        expected_revision,
                        str(row["content_hash"]),
                        revision,
                        content_hash,
                        int(content_changed),
                        now,
                    ),
                )
            if self.on_draft_changed_in_transaction is not None:
                self.on_draft_changed_in_transaction(conn, application_id, response)
            stored_response = dict(response)
            if idempotency_digest:
                stored_response["_idempotency_digest"] = idempotency_digest
            conn.execute(
                "INSERT INTO draft_idempotency VALUES(?,?,?,?)",
                (application_id, idempotency_key, json.dumps(stored_response), now),
            )
            return response

    async def get_draft_idempotency(
        self,
        application_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_draft_idempotency_sync,
            application_id,
            idempotency_key,
        )

    def _get_draft_idempotency_sync(
        self,
        application_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM draft_idempotency WHERE application_id=? AND idempotency_key=?",
                (application_id, idempotency_key),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["response_json"])

    @staticmethod
    def _application_result(result: dict[str, Any]) -> dict[str, Any]:
        snapshot_json = result.pop("draft_snapshot_json", None)
        result["display_description"] = WorkflowStorage._display_description(
            snapshot_json,
            fallback=str(result.get("description") or result.get("requirement") or ""),
        )
        result["governed_hard_gate"] = bool(result["governed_hard_gate"])
        result["delivery_policy"] = resolve_delivery_policy(
            result["delivery_mode"],
            governed_hard_gate=result["governed_hard_gate"],
        ).model_dump(mode="json")
        validation_report_json = result.pop("validation_report_json", None)
        report = json.loads(validation_report_json) if validation_report_json else None
        result["evidence"] = WorkflowStorage._evidence_result(result, report)
        return result

    @staticmethod
    def _display_description(snapshot_json: str | None, *, fallback: str) -> str:
        try:
            snapshot = json.loads(snapshot_json or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        contract = snapshot.get("capability_build_contract")
        if isinstance(contract, dict) and str(contract.get("business_goal") or "").strip():
            source = str(contract["business_goal"])
        else:
            source = fallback
        return re.sub(r"\s+", " ", source).strip()[:500]

    @staticmethod
    def _json_list(value: str | None) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    @staticmethod
    def _evidence_state(tested_hash: str | None, content_hash: str) -> str:
        if not tested_hash:
            return "missing"
        return "current" if tested_hash == content_hash else "stale"

    @classmethod
    def _evidence_result(
        cls,
        row: dict[str, Any],
        validation_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if validation_report is None and isinstance(row.get("validation_report_json"), str):
            try:
                parsed_report = json.loads(row["validation_report_json"])
            except json.JSONDecodeError:
                parsed_report = None
            validation_report = parsed_report if isinstance(parsed_report, dict) else None
        latest_validation = (
            validation_report.get("validation", {})
            if isinstance(validation_report, dict)
            else {}
        )
        validation_contract_current = bool(
            validation_report
            and row.get("validation_contract_digest")
            == WORKFLOW_VALIDATION_CONTRACT_DIGEST
        )
        latest_validation_current = (
            isinstance(latest_validation, dict)
            and latest_validation.get("content_hash") == row["content_hash"]
        )
        latest_validation_failed = bool(
            latest_validation_current
            and validation_report
            and validation_report.get("passed") is False
        )
        state = cls._evidence_state(row.get("tested_hash"), row["content_hash"])
        if (
            (latest_validation_failed or not validation_contract_current)
            and state == "current"
        ):
            state = "stale"
        result = {
            "state": state,
            "current_hash": row["content_hash"],
            "last_tested_hash": row.get("tested_hash"),
            "latest_validation_failed": latest_validation_failed,
            "invalidated_at": row.get("evidence_invalidated_at"),
            "invalidated_revision": row.get("evidence_invalidated_revision"),
            "change_summary": cls._json_list(row.get("evidence_change_summary_json")),
            "revalidate_endpoint": f"/api/v1/applications/{row.get('application_id') or row.get('id')}/tests/run",
        }
        if validation_report is not None:
            result["last_validation_report"] = validation_report
        return result

    async def mark_tested(
        self, application_id: str, revision: int, content_hash: str, report: dict[str, Any]
    ) -> None:
        await self.record_test_report(
            application_id,
            revision,
            content_hash,
            report,
            passed=True,
        )

    async def record_test_report(
        self,
        application_id: str,
        revision: int,
        content_hash: str,
        report: dict[str, Any],
        *,
        passed: bool,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._record_test_report_sync,
                application_id,
                revision,
                content_hash,
                report,
                passed,
            )

    def _record_test_report_sync(
        self,
        application_id: str,
        revision: int,
        content_hash: str,
        report: dict[str, Any],
        passed: bool,
    ) -> None:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT revision,content_hash FROM application_drafts WHERE application_id=?",
                (application_id,),
            ).fetchone()
            if not row or int(row["revision"]) != revision or row["content_hash"] != content_hash:
                raise RevisionConflict("draft changed while tests were running")
            if passed:
                conn.execute(
                    """UPDATE application_drafts SET tested_hash=?,validation_report_json=?,
                       validation_contract_digest=?,
                       evidence_invalidated_at=NULL,evidence_invalidated_revision=NULL,
                       evidence_change_summary_json='[]',updated_at=?
                       WHERE application_id=?""",
                    (
                        content_hash,
                        json.dumps(report, ensure_ascii=False),
                        WORKFLOW_VALIDATION_CONTRACT_DIGEST,
                        utc_now(),
                        application_id,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE application_drafts SET validation_report_json=?,
                       validation_contract_digest=?,updated_at=?
                       WHERE application_id=?""",
                    (
                        json.dumps(report, ensure_ascii=False),
                        WORKFLOW_VALIDATION_CONTRACT_DIGEST,
                        utc_now(),
                        application_id,
                    ),
                )

    async def publication_decision(self, application_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._publication_decision_sync, application_id)

    def _publication_decision_sync(self, application_id: str) -> dict[str, Any]:
        with self.storage._connect() as conn:
            row = conn.execute(
                """SELECT d.*,a.delivery_mode,a.governed_hard_gate
                   FROM application_drafts d JOIN applications a ON a.id=d.application_id
                   WHERE d.application_id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"application draft not found: {application_id}")
            return self._publication_decision_from_row(dict(row))

    @classmethod
    def _publication_decision_from_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        policy = resolve_delivery_policy(
            row["delivery_mode"],
            governed_hard_gate=bool(row["governed_hard_gate"]),
        ).model_dump(mode="json")
        evidence = cls._evidence_result(row)
        warnings: list[dict[str, str]] = []
        if evidence["latest_validation_failed"]:
            warnings.append({
                "code": "failed_evidence",
                "message": "The latest acceptance run for the current draft failed.",
            })
        elif evidence["state"] == "missing":
            warnings.append({
                "code": "missing_evidence",
                "message": "The current draft has no successful acceptance evidence.",
            })
        elif evidence["state"] == "stale":
            warnings.append({
                "code": "stale_evidence",
                "message": "The draft changed after its latest successful acceptance run.",
            })
        blocked = bool(warnings and policy["hard_gate_enabled"])
        requires_confirmation = bool(
            warnings and policy["warning_ack_required"] and not blocked
        )
        return {
            "application_id": row["application_id"],
            "allowed": not blocked,
            "requires_confirmation": requires_confirmation,
            "blocked": blocked,
            "warning_codes": [item["code"] for item in warnings],
            "warnings": warnings,
            "evidence_state": evidence["state"],
            "evidence": evidence,
            "policy": policy,
            "policy_source": f"application.delivery_mode:{row['delivery_mode']}",
        }

    async def publish(
        self,
        application_id: str,
        *,
        acknowledge_warnings: bool = False,
        execution_policy_snapshot: ExecutionPolicySnapshot | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._publish_sync,
                application_id,
                acknowledge_warnings,
                execution_policy_snapshot,
            )

    def _publish_sync(
        self,
        application_id: str,
        acknowledge_warnings: bool,
        execution_policy_snapshot: ExecutionPolicySnapshot | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.storage._connect() as conn:
            draft = conn.execute(
                """SELECT d.*,a.delivery_mode,a.governed_hard_gate
                   FROM application_drafts d JOIN applications a ON a.id=d.application_id
                   WHERE d.application_id=?""",
                (application_id,),
            ).fetchone()
            if not draft:
                raise KeyError(f"application draft not found: {application_id}")
            decision = self._publication_decision_from_row(dict(draft))
            if decision["blocked"]:
                raise PublishGateError("publication blocked by governed evidence policy", decision)
            if decision["requires_confirmation"] and not acknowledge_warnings:
                raise PublishGateError("publication warnings require explicit acknowledgement", decision)
            decision["acknowledged_warnings"] = bool(
                acknowledge_warnings and decision["warnings"]
            )
            decision["decided_at"] = now
            if execution_policy_snapshot is not None:
                decision["execution_policy_snapshot"] = (
                    execution_policy_snapshot.model_dump(mode="json")
                )
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS version FROM application_versions WHERE application_id=?",
                (application_id,),
            ).fetchone()
            version = int(row["version"])
            conn.execute(
                """INSERT INTO application_versions(
                     application_id,version,snapshot_json,content_hash,
                     validation_report_json,validation_contract_digest,
                     publication_decision_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    application_id,
                    version,
                    draft["snapshot_json"],
                    draft["content_hash"],
                    draft["validation_report_json"],
                    draft["validation_contract_digest"],
                    json.dumps(decision, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                "UPDATE applications SET active_version=?,updated_at=? WHERE id=?",
                (version, now, application_id),
            )
            return {
                "application_id": application_id,
                "version": version,
                "content_hash": draft["content_hash"],
                "publication_decision": decision,
            }

    async def list_versions(self, application_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_versions_sync, application_id)

    def _list_versions_sync(self, application_id: str) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT application_id,version,content_hash,validation_report_json,
                          publication_decision_json,created_at
                   FROM application_versions WHERE application_id=? ORDER BY version DESC""",
                (application_id,),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["validation_report"] = json.loads(item.pop("validation_report_json"))
                item["publication_decision"] = json.loads(item.pop("publication_decision_json"))
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
            result["publication_decision"] = json.loads(result.pop("publication_decision_json"))
            return result

    async def restore_version(self, application_id: str, version: int) -> dict[str, Any]:
        published = await self.get_version(application_id, version)
        draft = await self.get_draft(application_id)
        restored = await self.save_draft(
            application_id,
            published["snapshot"],
            expected_revision=draft["revision"],
            idempotency_key=f"restore:{version}:{uuid4()}",
            change_context={"operation": "restore_version", "version": version},
        )
        async with self._lock:
            await asyncio.to_thread(
                self._restore_version_evidence_sync,
                application_id,
                restored["revision"],
                published,
            )
        refreshed = await self.get_draft(application_id)
        return {
            **restored,
            "evidence_state": refreshed["evidence"]["state"],
            "evidence": refreshed["evidence"],
        }

    def _restore_version_evidence_sync(
        self,
        application_id: str,
        expected_revision: int,
        published: dict[str, Any],
    ) -> None:
        decision = published.get("publication_decision") or {}
        evidence = decision.get("evidence") or {
            "state": "current",
            "last_tested_hash": published["content_hash"],
            "invalidated_at": None,
            "invalidated_revision": None,
            "change_summary": [],
        }
        tested_hash = evidence.get("last_tested_hash")
        with self.storage._connect() as conn:
            current = conn.execute(
                "SELECT revision,content_hash FROM application_drafts WHERE application_id=?",
                (application_id,),
            ).fetchone()
            if (
                not current
                or int(current["revision"]) != expected_revision
                or current["content_hash"] != published["content_hash"]
            ):
                raise RevisionConflict("draft changed while version evidence was being restored")
            conn.execute(
                """UPDATE application_drafts SET tested_hash=?,validation_report_json=?,
                   validation_contract_digest=?,
                   evidence_invalidated_at=?,evidence_invalidated_revision=?,
                   evidence_change_summary_json=?,updated_at=? WHERE application_id=?""",
                (
                    tested_hash,
                    json.dumps(published["validation_report"], ensure_ascii=False),
                    published.get("validation_contract_digest", ""),
                    evidence.get("invalidated_at"),
                    evidence.get("invalidated_revision"),
                    json.dumps(evidence.get("change_summary") or [], ensure_ascii=False),
                    utc_now(),
                    application_id,
                ),
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
        capability_build_contract: CapabilityBuildContract | None = None,
        capability_closure: dict[str, Any] | None = None,
        capability_routing: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        team_state = BuildTeamState(
            planning_mode=planning_mode,
            complexity_router=complexity_router,
            runtime_builder_policy=runtime_builder_policy,
            capability_build_contract=capability_build_contract,
            capability_closure=capability_closure,
            capability_routing=capability_routing,
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

    async def list_runs(self, application_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._list_runs_sync,
            application_id,
            max(1, min(limit, 100)),
        )
        for row in rows:
            row["state"] = WorkflowRunState.model_validate_json(row.pop("state_json"))
            row["outputs"] = json.loads(row.pop("outputs_json"))
        return rows

    async def export_formal_run_snapshot(
        self,
        application_id: str,
        *,
        assignment_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Export every run durably owned by one formal assignment/session.

        Run selection is server-owned: callers cannot hide a successful run by
        leaving its id out of an archive request.
        """

        if not assignment_id or not session_id:
            raise ValueError("formal run export requires assignment and session IDs")
        return await asyncio.to_thread(
            self._export_formal_run_snapshot_sync,
            application_id,
            assignment_id,
            session_id,
        )

    def _export_formal_run_snapshot_sync(
        self,
        application_id: str,
        assignment_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        with self.storage._connect() as conn:
            conn.execute("BEGIN")
            application = conn.execute(
                "SELECT id,active_version,created_at,updated_at FROM applications WHERE id=?",
                (application_id,),
            ).fetchone()
            draft = conn.execute(
                """
                SELECT revision,content_hash,snapshot_json,validation_report_json,
                       tested_hash,updated_at
                FROM application_drafts WHERE application_id=?
                """,
                (application_id,),
            ).fetchone()
            if application is None or draft is None:
                raise KeyError(f"application draft not found: {application_id}")
            active_version = application["active_version"]
            version = None
            if active_version is not None:
                version = conn.execute(
                    """
                    SELECT application_id,version,content_hash,snapshot_json,created_at
                    FROM application_versions WHERE application_id=? AND version=?
                    """,
                    (application_id, int(active_version)),
                ).fetchone()
                if version is None:
                    raise KeyError(
                        f"application version not found: {application_id}@{active_version}"
                    )
            run_rows = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE application_id=? ORDER BY created_at,id
                """,
                (application_id,),
            ).fetchall()
            baseline_rows = conn.execute(
                """
                SELECT * FROM formal_draft_baselines
                WHERE assignment_id=? AND session_id=? AND application_id=?
                ORDER BY started_at,assignment_id
                """,
                (assignment_id, session_id, application_id),
            ).fetchall()
            mutation_rows = conn.execute(
                """
                SELECT * FROM formal_draft_mutations
                WHERE assignment_id=? AND session_id=? AND application_id=?
                ORDER BY after_revision,created_at,mutation_id
                """,
                (assignment_id, session_id, application_id),
            ).fetchall()
            runs: list[dict[str, Any]] = []
            run_event_counts: dict[str, int] = {}
            run_event_complete = True
            for row in run_rows:
                raw_state = json.loads(str(row["state_json"]))
                if not isinstance(raw_state, dict) or (
                    str(raw_state.get("assignment_id") or "") != assignment_id
                    or str(raw_state.get("session_id") or "") != session_id
                ):
                    continue
                state = WorkflowRunState.model_validate(raw_state)
                run_id = str(row["id"])
                events = conn.execute(
                    """
                    SELECT seq,event_type,data_json,created_at
                    FROM events WHERE stream_id=? ORDER BY seq
                    """,
                    (run_id,),
                ).fetchall()
                event_seqs = [int(event["seq"]) for event in events]
                run_event_counts[run_id] = len(event_seqs)
                if event_seqs and event_seqs != list(
                    range(event_seqs[0], event_seqs[-1] + 1)
                ):
                    run_event_complete = False
                runs.append(
                    {
                        "id": str(row["id"]),
                        "application_id": str(row["application_id"]),
                        "version": row["version"],
                        "draft_revision": row["draft_revision"],
                        "status": str(row["status"]),
                        "state": state.model_dump(mode="json", exclude_none=True),
                        "outputs": json.loads(str(row["outputs_json"])),
                        "error": row["error"],
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                        "events": [
                            {
                                "seq": int(event["seq"]),
                                "type": str(event["event_type"]),
                                "data": json.loads(str(event["data_json"])),
                                "created_at": str(event["created_at"]),
                            }
                            for event in events
                        ],
                    }
                )
            if len(runs) > 1_000:
                raise ValueError("formal assignment has more than 1000 workflow runs")
            complete = len(baseline_rows) == 1 and run_event_complete
        return {
            "schema_version": "1.0",
            "complete": complete,
            "counts": {
                "runs": len(runs),
                "run_events": sum(run_event_counts.values()),
                "formal_draft_baselines": len(baseline_rows),
                "formal_draft_mutations": len(mutation_rows),
            },
            "run_event_counts": run_event_counts,
            "application": {
                "id": str(application["id"]),
                "active_version": (
                    int(active_version) if active_version is not None else None
                ),
                "created_at": str(application["created_at"]),
                "updated_at": str(application["updated_at"]),
            },
            "draft": {
                "revision": int(draft["revision"]),
                "content_hash": str(draft["content_hash"]),
                "snapshot": ApplicationSnapshot.model_validate_json(
                    draft["snapshot_json"]
                ).model_dump(mode="json", exclude_none=True),
                "validation_report": json.loads(
                    str(draft["validation_report_json"])
                ),
                "tested_hash": draft["tested_hash"],
                "updated_at": str(draft["updated_at"]),
            },
            "published_version": (
                {
                    "application_id": str(version["application_id"]),
                    "version": int(version["version"]),
                    "content_hash": str(version["content_hash"]),
                    "snapshot": ApplicationSnapshot.model_validate_json(
                        version["snapshot_json"]
                    ).model_dump(mode="json", exclude_none=True),
                    "created_at": str(version["created_at"]),
                }
                if version is not None
                else None
            ),
            "runs": runs,
            "formal_draft_provenance": {
                "baselines": [dict(row) for row in baseline_rows],
                "mutations": [dict(row) for row in mutation_rows],
            },
        }

    def _list_runs_sync(self, application_id: str, limit: int) -> list[dict[str, Any]]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM workflow_runs
                   WHERE application_id=?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (application_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

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
