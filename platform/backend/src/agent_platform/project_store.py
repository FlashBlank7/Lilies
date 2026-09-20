"""Project membership, shared records and on-demand task persistence."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .db import connect
from .models import utc_now


class ProjectConflict(ValueError):
    pass


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ProjectStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self):
        with connect(self.db_path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY REFERENCES applications(id),
                    name TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_members (
                    application_id TEXT PRIMARY KEY REFERENCES applications(id),
                    project_id TEXT NOT NULL REFERENCES projects(id), created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_members_project ON project_members(project_id);
                CREATE TABLE IF NOT EXISTS project_records (
                    project_id TEXT NOT NULL REFERENCES projects(id), collection TEXT NOT NULL,
                    record_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    value_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, collection, record_key)
                );
                CREATE TABLE IF NOT EXISTS project_tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    request_key TEXT NOT NULL, mode TEXT NOT NULL, workflow_id TEXT NOT NULL,
                    status TEXT NOT NULL, inputs_json TEXT NOT NULL, message TEXT NOT NULL,
                    snapshots_json TEXT NOT NULL, outputs_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS project_task_runs (
                    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id),
                    task_id TEXT NOT NULL REFERENCES project_tasks(id), step_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_task_runs_task ON project_task_runs(task_id);
                CREATE TABLE IF NOT EXISTS project_task_supplements (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES project_tasks(id),
                    message TEXT NOT NULL, inputs_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_task_responses (
                    task_id TEXT NOT NULL REFERENCES project_tasks(id), run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL, values_json TEXT NOT NULL,
                    PRIMARY KEY(task_id,run_id,node_id)
                );
                CREATE TABLE IF NOT EXISTS project_record_writes (
                    project_id TEXT NOT NULL REFERENCES projects(id), operation_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, operation_key)
                );
                CREATE TABLE IF NOT EXISTS project_test_records (
                    project_id TEXT NOT NULL REFERENCES projects(id), scope TEXT NOT NULL,
                    collection TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    value_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, scope, collection, record_key)
                );
                CREATE TABLE IF NOT EXISTS project_test_record_writes (
                    project_id TEXT NOT NULL REFERENCES projects(id), scope TEXT NOT NULL,
                    operation_key TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, scope, operation_key)
                );
                CREATE TABLE IF NOT EXISTS project_progress (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id), revision INTEGER NOT NULL,
                    value_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            # Existing projects remain unclassified until their owner/agent explicitly labels them.
            for table, columns in {
                'projects': {'agent_modules_enabled': "INTEGER NOT NULL DEFAULT 0"},
                'project_members': {'purpose': "TEXT NOT NULL DEFAULT 'unclassified'"},
                'project_tasks': {'purpose': "TEXT NOT NULL DEFAULT 'unclassified'",
                                  'item_id': "TEXT NOT NULL DEFAULT ''",
                                  'feedback_task_id': "TEXT NOT NULL DEFAULT ''",
                                  'presentation_json': "TEXT NOT NULL DEFAULT '{}'"},
            }.items():
                existing = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
                for name, definition in columns.items():
                    if name not in existing:
                        c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            c.execute("UPDATE project_tasks SET status='interrupted', updated_at=? "
                      "WHERE status IN ('queued','running')", (utc_now(),))

    def exists(self, project_id: str) -> bool:
        with connect(self.db_path) as c:
            return c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None

    async def create(self, project_id: str, name: str, description: str) -> dict:
        def create():
            with connect(self.db_path) as c:
                now = utc_now()
                c.execute("INSERT INTO projects(id,name,description,created_at) VALUES (?,?,?,?)", (project_id, name, description, now))
                c.execute("INSERT INTO project_members(application_id,project_id,created_at,purpose) VALUES (?,?,?,'business')", (project_id, project_id, now))
        await asyncio.to_thread(create)
        return await self.get(project_id)

    async def get(self, project_id: str) -> dict:
        def get():
            with connect(self.db_path) as c:
                row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                if row is None:
                    raise KeyError("没有找到这个项目")
                result = dict(row)
                result['agent_modules_enabled'] = bool(result['agent_modules_enabled'])
                result['main_workflow_id'] = project_id
                result['members'] = [dict(r) for r in c.execute(
                    "SELECT a.id,a.name,a.description,d.revision,m.purpose FROM project_members m "
                    "JOIN applications a ON a.id=m.application_id "
                    "JOIN application_drafts d ON d.application_id=a.id "
                    "WHERE m.project_id=? ORDER BY m.created_at,a.id", (project_id,))]
                return result
        return await asyncio.to_thread(get)

    async def list(self) -> list[dict]:
        def ids():
            with connect(self.db_path) as c:
                return [r[0] for r in c.execute("SELECT id FROM projects ORDER BY created_at DESC")]
        return [await self.get(i) for i in await asyncio.to_thread(ids)]

    async def set_agent_modules(self, project_id: str, enabled: bool) -> dict:
        def update():
            with connect(self.db_path) as c:
                c.execute('UPDATE projects SET agent_modules_enabled=? WHERE id=?', (int(enabled), project_id))
        await asyncio.to_thread(update)
        return await self.get(project_id)

    async def membership(self, application_id: str) -> str | None:
        def get():
            with connect(self.db_path) as c:
                row = c.execute("SELECT project_id FROM project_members WHERE application_id=?", (application_id,)).fetchone()
                return row[0] if row else None
        return await asyncio.to_thread(get)

    async def add_member(self, project_id: str, application_id: str, purpose: str = 'business') -> None:
        def add():
            with connect(self.db_path) as c:
                c.execute("INSERT INTO project_members(application_id,project_id,created_at,purpose) VALUES (?,?,?,?)", (application_id, project_id, utc_now(), purpose))
        await asyncio.to_thread(add)

    async def classify_member(self, project_id: str, application_id: str, purpose: str) -> None:
        def update():
            with connect(self.db_path) as c:
                c.execute('UPDATE project_members SET purpose=? WHERE project_id=? AND application_id=?',
                          (purpose, project_id, application_id))
        await asyncio.to_thread(update)

    async def progress(self, project_id: str) -> dict:
        def read():
            with connect(self.db_path) as c:
                row = c.execute('SELECT * FROM project_progress WHERE project_id=?', (project_id,)).fetchone()
                if not row:
                    return {'revision': 0, 'value': {'goal': '', 'summary': '', 'items': []}, 'updated_at': None}
                return {'revision': row['revision'], 'value': json.loads(row['value_json']), 'updated_at': row['updated_at']}
        return await asyncio.to_thread(read)

    async def put_progress(self, project_id: str, value: dict, expected_revision: int) -> dict:
        serialized = encode(value)
        if len(serialized.encode()) > 1_000_000:
            raise ValueError('项目进展超过 1 MB，请将详细结果保留在关联任务中')
        def write():
            with connect(self.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT revision FROM project_progress WHERE project_id=?', (project_id,)).fetchone()
                revision = row['revision'] if row else 0
                if revision != expected_revision:
                    raise ProjectConflict(f'项目进展已更新：当前修订号 {revision}，请重新读取')
                now = utc_now()
                c.execute('INSERT INTO project_progress VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET '
                          'revision=excluded.revision,value_json=excluded.value_json,updated_at=excluded.updated_at',
                          (project_id, revision + 1, serialized, now))
                return {'revision': revision + 1, 'value': value, 'updated_at': now}
        return await asyncio.to_thread(write)

    async def remove_member(self, project_id: str, application_id: str) -> None:
        def remove():
            with connect(self.db_path) as c:
                c.execute("DELETE FROM project_members WHERE project_id=? AND application_id=?", (project_id, application_id))
        await asyncio.to_thread(remove)

    @staticmethod
    def record_storage(project_id: str, scope: str):
        # Scope is supplied by the test runner, never by a workflow node or API input.
        if scope:
            return 'project_test_records', 'project_test_record_writes', 'project_id=? AND scope=?', [project_id, scope]
        return 'project_records', 'project_record_writes', 'project_id=?', [project_id]

    async def records(self, project_id: str, collection: str | None = None, *, scope: str = '') -> list[dict]:
        table, _, where, identity = self.record_storage(project_id, scope)
        def read():
            with connect(self.db_path) as c:
                query, params = f"SELECT * FROM {table} WHERE {where}", list(identity)
                if collection is not None:
                    query += " AND collection=?"
                    params.append(collection)
                return [self.record(r) for r in c.execute(query + " ORDER BY collection,record_key LIMIT 1000", params)]
        return await asyncio.to_thread(read)

    @staticmethod
    def record(row) -> dict:
        result = dict(row)
        result.pop('scope', None)
        result['key'] = result.pop('record_key')
        result['value'] = json.loads(result.pop('value_json'))
        return result

    async def get_record(self, project_id: str, collection: str, key: str, *, scope: str = '') -> dict:
        table, _, where, identity = self.record_storage(project_id, scope)
        def read():
            with connect(self.db_path) as c:
                row = c.execute(f"SELECT * FROM {table} WHERE {where} AND collection=? AND record_key=?",
                                [*identity, collection, key]).fetchone()
                return {**self.record(row), 'found': True} if row else {
                    'project_id': project_id, 'collection': collection, 'key': key,
                    'found': False, 'revision': 0, 'value': None}
        return await asyncio.to_thread(read)

    async def put_record(self, project_id: str, collection: str, key: str, value: dict,
                         expected_revision: int, operation_key: str = '', *, scope: str = '') -> dict:
        if not collection.strip() or len(collection) > 120 or not key.strip() or len(key) > 240:
            raise ValueError("集合和记录键不能为空或过长")
        value_json = encode(value)
        if len(value_json.encode()) > 1_000_000:
            raise ValueError("一条业务记录不能超过 1 MB")
        payload = encode([collection, key, value, expected_revision])
        table, writes, where, identity = self.record_storage(project_id, scope)
        identity_columns = 'project_id,scope' if scope else 'project_id'

        def write():
            with connect(self.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                if operation_key:
                    previous = c.execute(f"SELECT payload_json,result_json FROM {writes} "
                                         f"WHERE {where} AND operation_key=?", [*identity, operation_key]).fetchone()
                    if previous:
                        if previous['payload_json'] != payload:
                            raise ProjectConflict("重复写入标识对应不同内容，请重新读取记录")
                        return json.loads(previous['result_json'])
                row = c.execute(f"SELECT revision FROM {table} WHERE {where} AND collection=? AND record_key=?",
                                [*identity, collection, key]).fetchone()
                current = row[0] if row else 0
                if expected_revision != current:
                    raise ProjectConflict(f"记录已更新：期望修订号 {expected_revision}，当前 {current}，请重新读取")
                result = {'project_id': project_id, 'collection': collection, 'key': key,
                          'revision': current + 1, 'value': value, 'updated_at': utc_now(), 'found': True}
                values = [*identity, collection, key, result['revision'], value_json, result['updated_at']]
                c.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)}) "
                          f"ON CONFLICT({identity_columns},collection,record_key) "
                          "DO UPDATE SET revision=excluded.revision,value_json=excluded.value_json,updated_at=excluded.updated_at",
                          values)
                if operation_key:
                    values = [*identity, operation_key, payload, encode(result)]
                    c.execute(f"INSERT INTO {writes} VALUES ({','.join('?' for _ in values)})", values)
                return result
        return await asyncio.to_thread(write)

    @staticmethod
    def task(row, *, snapshots=False) -> dict:
        result = dict(row)
        for key in ('inputs', 'outputs', 'snapshots', 'presentation'):
            raw = result.pop(key + '_json')
            if key != 'snapshots' or snapshots:
                result[key] = json.loads(raw)
        return result

    async def create_task(self, task_id: str, project_id: str, request_key: str, mode: str,
                          workflow_id: str, inputs: dict, message: str, snapshots: dict,
                          purpose: str = 'business', item_id: str = '', feedback_task_id: str = '') -> tuple[dict, bool]:
        def create():
            with connect(self.db_path) as c:
                c.execute('BEGIN IMMEDIATE')
                existing = c.execute("SELECT * FROM project_tasks WHERE project_id=? AND request_key=?",
                                     (project_id, request_key)).fetchone()
                if existing:
                    if (existing['inputs_json'], existing['mode'], existing['workflow_id'], existing['message'],
                        purpose if existing['purpose'] == 'unclassified' else existing['purpose'], existing['item_id'], existing['feedback_task_id']) != (
                            encode(inputs), mode, workflow_id, message, purpose, item_id, feedback_task_id):
                        raise ProjectConflict("这个请求标识已经用于不同的业务请求")
                    return self.task(existing), False
                now = utc_now()
                c.execute("INSERT INTO project_tasks(id,project_id,request_key,mode,workflow_id,status,inputs_json,"
                          "message,snapshots_json,created_at,updated_at,purpose,item_id,feedback_task_id) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)",
                          (task_id, project_id, request_key, mode, workflow_id, encode(inputs), message, encode(snapshots), now, now,
                           purpose, item_id, feedback_task_id))
                return self.task(c.execute("SELECT * FROM project_tasks WHERE id=?", (task_id,)).fetchone()), True
        return await asyncio.to_thread(create)

    async def get_task(self, project_id: str, task_id: str, *, snapshots=False) -> dict:
        def read():
            with connect(self.db_path) as c:
                row = c.execute("SELECT * FROM project_tasks WHERE id=? AND project_id=?", (task_id, project_id)).fetchone()
                if row is None:
                    raise KeyError("没有找到这个项目任务")
                return self.task(row, snapshots=snapshots)
        return await asyncio.to_thread(read)

    async def task_for_request(self, project_id: str, request_key: str) -> dict | None:
        def read():
            with connect(self.db_path) as c:
                row = c.execute('SELECT * FROM project_tasks WHERE project_id=? AND request_key=?', (project_id, request_key)).fetchone()
                return self.task(row) if row else None
        return await asyncio.to_thread(read)

    async def supplement(self, task_id: str, message: str, inputs: dict):
        def write():
            with connect(self.db_path) as c:
                c.execute('INSERT INTO project_task_supplements(task_id,message,inputs_json,created_at) VALUES (?,?,?,?)',
                          (task_id, message, encode(inputs), utc_now()))
        await asyncio.to_thread(write)

    async def supplements(self, task_id: str) -> list[dict]:
        def read():
            with connect(self.db_path) as c:
                return [{**dict(r), 'inputs': json.loads(r['inputs_json'])} for r in c.execute(
                    'SELECT message,inputs_json,created_at FROM project_task_supplements WHERE task_id=? ORDER BY id', (task_id,))]
        return await asyncio.to_thread(read)

    async def response(self, task_id: str, run_id: str, node_id: str, values: dict | None = None) -> dict | None:
        def access():
            with connect(self.db_path) as c:
                if values is not None:
                    c.execute('INSERT INTO project_task_responses VALUES (?,?,?,?) ON CONFLICT(task_id,run_id,node_id) '
                              'DO UPDATE SET values_json=excluded.values_json', (task_id, run_id, node_id, encode(values)))
                    return values
                row = c.execute('SELECT values_json FROM project_task_responses WHERE task_id=? AND run_id=? AND node_id=?',
                                (task_id, run_id, node_id)).fetchone()
                return json.loads(row[0]) if row else None
        return await asyncio.to_thread(access)

    async def tasks(self, project_id: str, *, purpose: str = '', item_id: str = '',
                    before: str = '', limit: int = 100) -> list[dict]:
        def read():
            with connect(self.db_path) as c:
                query, args = 'SELECT * FROM project_tasks WHERE project_id=?', [project_id]
                if purpose:
                    query += ' AND purpose=?'
                    args.append(purpose)
                if item_id:
                    query += ' AND item_id=?'
                    args.append(item_id)
                if before:
                    cursor = c.execute('SELECT created_at,id FROM project_tasks WHERE project_id=? AND id=?', (project_id, before)).fetchone()
                    if not cursor:
                        raise ValueError('任务分页位置不属于当前项目')
                    query += ' AND (created_at,id)<(?,?)'
                    args.extend(cursor)
                return [self.task(r) for r in c.execute(query + ' ORDER BY created_at DESC,id DESC LIMIT ?', (*args, limit))]
        return await asyncio.to_thread(read)

    async def present_task(self, task_id: str, presentation: dict) -> None:
        def write():
            with connect(self.db_path) as c:
                c.execute('UPDATE project_tasks SET presentation_json=?,updated_at=? WHERE id=?',
                          (encode(presentation), utc_now(), task_id))
        await asyncio.to_thread(write)

    async def update_task(self, task_id: str, *, status: str, outputs: dict | None = None, error: str = ''):
        def update():
            with connect(self.db_path) as c:
                c.execute("UPDATE project_tasks SET status=?,outputs_json=COALESCE(?,outputs_json),error=?,updated_at=? WHERE id=?",
                          (status, encode(outputs) if outputs is not None else None, error, utc_now(), task_id))
        await asyncio.to_thread(update)

    async def track_run(self, task_id: str, step_key: str, run_id: str):
        def track():
            with connect(self.db_path) as c:
                c.execute("INSERT INTO project_task_runs VALUES (?,?,?,?)", (run_id, task_id, step_key, utc_now()))
        await asyncio.to_thread(track)

    async def runs(self, task_id: str) -> list[dict]:
        def read():
            with connect(self.db_path) as c:
                return [{**dict(r), 'outputs': json.loads(r['outputs_json']),
                         'inputs': json.loads(r['state_json']).get('inputs', {})} for r in c.execute(
                    "SELECT r.id,r.application_id,r.status,r.draft_revision,r.outputs_json,r.state_json,r.error,r.created_at,t.step_key "
                    "FROM project_task_runs t JOIN workflow_runs r ON r.id=t.run_id "
                    "WHERE t.task_id=? ORDER BY t.created_at", (task_id,))]
        return await asyncio.to_thread(read)
