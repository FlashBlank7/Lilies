"""Copy one project draft without asking the model to regenerate its graph."""
from __future__ import annotations

import asyncio
import json
import re
from uuid import UUID, uuid5

from .db import connect
from .models import utc_now
from .project_agent_context import draft_summary
from .project_capabilities import ProjectBlocks
from .project_store import ProjectConflict, encode
from .project_draft_context import node_changes
from .workflow_models import ApplicationSnapshot


async def copy_member(services, project_id, args):
    await services.projects.store.get(project_id)
    # Stable identity and the existing idempotency store make concurrent retries
    # safe across processes. The transaction never exposes a blank half-copy.
    target = str(uuid5(UUID(project_id), 'workflow-copy:' + args.request_key))
    key = 'project_workflow_copy'
    request = {k: getattr(args, k) for k in (
        'workflow_id', 'expected_revision', 'name', 'description', 'purpose', 'request_key')}
    request['node_updates'] = args.node_updates

    def create():
        with connect(services.storage.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT response_json FROM draft_idempotency '
                'WHERE application_id=? AND idempotency_key=?', (target, key)).fetchone()
            if previous:
                stored = json.loads(previous['response_json'])
                original_request = stored.get('request', stored)
                original_request.setdefault('node_updates', {})
                if original_request != request:
                    raise ProjectConflict('同一复制请求不能用于不同来源、版本或配置')
                if not db.execute('SELECT 1 FROM project_members WHERE project_id=? AND application_id=?',
                                  (project_id, target)).fetchone():
                    raise ProjectConflict('该副本已移除；需要重新复制时请使用新 request_key')
                return stored.get('result', {'changes_available': False})
            source = db.execute('SELECT d.* FROM application_drafts d JOIN project_members m '
                'ON d.application_id=m.application_id WHERE m.project_id=? AND m.application_id=?',
                (project_id, args.workflow_id)).fetchone()
            if not source:
                raise ValueError('只能复制当前项目的成员工作流')
            if source['revision'] != args.expected_revision:
                raise ProjectConflict('源工作流已更新，请读取当前修订后再复制')
            snapshot = ApplicationSnapshot.model_validate_json(source['snapshot_json'])
            original = snapshot.model_copy(deep=True)
            for node_id, changes in args.node_updates.items():
                services.applications._apply_to_snapshot(snapshot, 'update_node',
                    {'node_id': node_id, 'changes': changes, 'merge_config': True})
            snapshot = ApplicationSnapshot.model_validate(snapshot.model_dump(mode='json'))
            # Match normal draft edits: a copy cannot introduce foreign members.
            members = {row[0] for row in db.execute('SELECT application_id FROM project_members WHERE project_id=?', (project_id,))}
            references = set(re.findall(r'workflow:([a-f0-9-]{36})', snapshot.model_dump_json()))
            if references - members:
                raise ValueError('只能引用当前项目的成员工作流')
            enabled = db.execute('SELECT agent_modules_enabled FROM projects WHERE id=?', (project_id,)).fetchone()[0]
            ProjectBlocks(services.blocks, agent_modules_enabled=bool(enabled)).validate_workflow(snapshot.workflow)
            snapshot.name = args.name.strip()
            if args.description:
                snapshot.description = args.description
            now = utc_now()
            db.execute('INSERT INTO applications(id,name,description,mode,delivery_mode,governed_hard_gate,'
                'requirement,active_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,NULL,?,?)',
                (target, snapshot.name, snapshot.description, snapshot.mode.value, snapshot.delivery_mode.value,
                 int(snapshot.governed_hard_gate), snapshot.requirement, now, now))
            db.execute('INSERT INTO application_drafts(application_id,revision,snapshot_json,content_hash,'
                'tested_hash,validation_report_json,updated_at) VALUES(?,0,?,?,NULL,\'{}\',?)',
                (target, snapshot.model_dump_json(exclude_none=True), snapshot.content_hash(), now))
            db.execute('INSERT INTO project_members(application_id,project_id,created_at,purpose) VALUES(?,?,?,?)',
                (target, project_id, now, args.purpose))
            receipt = {'applied_revision': 0, 'applied_content_hash': snapshot.content_hash(),
                       'changes': node_changes(original, snapshot), 'changes_available': True}
            db.execute('INSERT INTO draft_idempotency(application_id,idempotency_key,response_json,created_at) VALUES(?,?,?,?)',
                (target, key, encode({'request': request, 'result': receipt}), now))
            return receipt

    result = await asyncio.to_thread(create)
    draft = await services.workflow_store.get_draft(target)
    return {'id': target, 'workflow_id': target, 'source_workflow_id': args.workflow_id,
            'source_revision': args.expected_revision, 'purpose': args.purpose, **draft_summary(draft, nodes=False), **result}
