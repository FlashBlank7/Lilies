"""Personal conversations share a project's tools and resources, never its chat transcript."""
from __future__ import annotations

import asyncio
from uuid import uuid4
from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field

from .conversation_scope import conversation_scope
from .models import utc_now
from .project_store import connect
from .project_conversation import ConversationMessage


class ConversationName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='新会话', min_length=1, max_length=100)


class ProjectSessions:
    def __init__(self, services):
        self.services = services
        self.db_path = services.projects.store.db_path

    async def initialize(self):
        def create():
            with connect(self.db_path) as c:
                c.execute('CREATE TABLE IF NOT EXISTS project_conversations ('
                          'id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), '
                          'user_id TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL)')
                c.execute('CREATE INDEX IF NOT EXISTS conversations_by_user ON project_conversations(project_id,user_id,created_at)')
        await asyncio.to_thread(create)

    async def legacy_allowed(self, project_id, user):
        return await self.services.accounts.project_role(user, project_id) in {'admin', 'owner'}

    async def require(self, project_id, conversation_id, user):
        await self.services.accounts.require_project(user, project_id)
        if conversation_id == 'legacy':
            if not await self.legacy_allowed(project_id, user):
                raise HTTPException(404, '未找到此会话')
            return {'id': 'legacy', 'title': '历史项目会话'}
        def read():
            with connect(self.db_path) as c:
                row = c.execute('SELECT * FROM project_conversations WHERE id=? AND project_id=? AND user_id=?',
                                (conversation_id, project_id, user['id'])).fetchone()
                return dict(row) if row else None
        row = await asyncio.to_thread(read)
        if not row:
            raise HTTPException(404, '未找到此会话')
        return row

    async def list(self, project_id, user):
        await self.services.accounts.require_project(user, project_id)
        def read():
            with connect(self.db_path) as c:
                return [dict(row) for row in c.execute('SELECT * FROM project_conversations '
                    'WHERE project_id=? AND user_id=? ORDER BY created_at DESC,id DESC', (project_id, user['id']))]
        rows = await asyncio.to_thread(read)
        manager = self.services.local_agents
        for row in rows:
            with conversation_scope(project_id, row['id']):
                row['status'] = manager.load(project_id)['status']
        with conversation_scope(project_id):
            legacy = manager.load(project_id)
        if legacy['events'] and await self.legacy_allowed(project_id, user):
            rows.append({'id': 'legacy', 'title': '历史项目会话', 'status': legacy['status'], 'legacy': True})
        return rows

    async def create(self, project_id, user, title):
        await self.services.accounts.require_project(user, project_id)
        row = {'id': str(uuid4()), 'project_id': project_id, 'user_id': user['id'], 'title': title, 'created_at': utc_now()}
        def insert():
            with connect(self.db_path) as c:
                c.execute('INSERT INTO project_conversations(id,project_id,user_id,title,created_at) VALUES (?,?,?,?,?)', tuple(row.values()))
        await asyncio.to_thread(insert)
        with conversation_scope(project_id, row['id']):
            manager = self.services.local_agents
            manager.save(project_id, manager.load(project_id))
        return {**row, 'status': 'idle'}

    async def rename(self, project_id, conversation_id, user, title):
        row = await self.require(project_id, conversation_id, user)
        if conversation_id == 'legacy':
            raise HTTPException(422, '历史项目会话保留原名称，可另建新会话')
        def update():
            with connect(self.db_path) as c:
                c.execute('UPDATE project_conversations SET title=? WHERE id=?', (title, conversation_id))
        await asyncio.to_thread(update)
        return {**row, 'title': title}


def register_session_routes(router, services, invoke):
    sessions = services.project_sessions
    conversation = services.projects.conversation

    @router.get('/conversations')
    async def listing(project_id: str, request: Request):
        return await sessions.list(project_id, request.state.user)

    @router.post('/conversations', status_code=201)
    async def create(project_id: str, body: ConversationName, request: Request):
        return await sessions.create(project_id, request.state.user, body.title)

    @router.patch('/conversations/{conversation_id}')
    async def rename(project_id: str, conversation_id: str, body: ConversationName, request: Request):
        return await sessions.rename(project_id, conversation_id, request.state.user, body.title)

    @router.get('/conversations/{conversation_id}')
    async def events(project_id: str, conversation_id: str, request: Request, after: str = '', before: str = '',
                     limit: int = Query(default=50, ge=1, le=100), kind: str = Query(default='messages', pattern='^(messages|tools|activity)$'), request_id: str = ''):
        await sessions.require(project_id, conversation_id, request.state.user)
        with conversation_scope(project_id, '' if conversation_id == 'legacy' else conversation_id):
            try:
                return conversation.events(project_id, after=after, before=before, limit=limit, kind=kind, request_id=request_id)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error

    @router.post('/conversations/{conversation_id}/messages', status_code=202)
    async def send(project_id: str, conversation_id: str, body: ConversationMessage, request: Request):
        await sessions.require(project_id, conversation_id, request.state.user)
        with conversation_scope(project_id, '' if conversation_id == 'legacy' else conversation_id):
            await invoke(conversation.send, project_id, body)
            return conversation.events(project_id)

    @router.post('/conversations/{conversation_id}/stop')
    async def stop(project_id: str, conversation_id: str, request: Request):
        await sessions.require(project_id, conversation_id, request.state.user)
        with conversation_scope(project_id, '' if conversation_id == 'legacy' else conversation_id):
            await services.local_agents.stop(project_id)
            return conversation.events(project_id)

    @router.get('/conversations/{conversation_id}/metrics')
    async def metrics(project_id: str, conversation_id: str, request: Request, request_id: str = ''):
        from .project_metrics import session_metrics
        await sessions.require(project_id, conversation_id, request.state.user)
        with conversation_scope(project_id, '' if conversation_id == 'legacy' else conversation_id):
            return session_metrics(services.local_agents.load(project_id)['events'], request_id)
