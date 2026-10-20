"""Private employee/maintainer conversations, independent of model execution."""
import asyncio
import hashlib
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .conversation_scope import conversation_scope
from .db import connect
from .models import utc_now

Category = Literal['result', 'usability', 'runtime', 'idea', 'other']
Status = Literal['received', 'working', 'resolved', 'declined']


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    project_id: str = Field(default='', max_length=100)
    conversation_id: str = Field(default='', max_length=100)
    request_id: str = Field(default='', max_length=100)
    task_id: str = Field(default='', max_length=100)
    workflow_id: str = Field(default='', max_length=100)
    page: Literal['general', 'conversation', 'workflow', 'run'] = 'general'

    @model_validator(mode='after')
    def references(self):
        if any((self.conversation_id, self.request_id, self.task_id, self.workflow_id)) and not self.project_id:
            raise ValueError('关联操作需要项目')
        if self.request_id and not self.conversation_id:
            raise ValueError('关联回答需要会话')
        return self


class NewFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=8000)
    category: Category = 'other'
    source: Source = Field(default_factory=Source)
    excerpt: str = Field(default='', max_length=6000)


class Reply(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=8000)
    status: Status | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class ReadProgress(BaseModel):
    revision: int = Field(ge=1)


def initialize(path):
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS user_feedback (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
          request_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
          category TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
          source_json TEXT NOT NULL, project_name TEXT NOT NULL, excerpt TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL,
          UNIQUE(user_id,request_key));
        CREATE TABLE IF NOT EXISTS user_feedback_messages (
          id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES user_feedback(id),
          user_id TEXT NOT NULL, user_name TEXT NOT NULL, role TEXT NOT NULL,
          request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, text TEXT NOT NULL,
          status TEXT, created_at TEXT NOT NULL, image BLOB, media_type TEXT NOT NULL,
          UNIQUE(feedback_id,user_id,request_key));
        CREATE TABLE IF NOT EXISTS user_feedback_reads (
          feedback_id TEXT NOT NULL REFERENCES user_feedback(id), user_id TEXT NOT NULL,
          revision INTEGER NOT NULL, PRIMARY KEY(feedback_id,user_id));
        CREATE INDEX IF NOT EXISTS feedback_by_owner ON user_feedback(user_id,updated_at);
        CREATE INDEX IF NOT EXISTS feedback_messages_by_thread ON user_feedback_messages(feedback_id,created_at);
        ''')


def require_thread(db, ident, user):
    row = db.execute('SELECT * FROM user_feedback WHERE id=?', (ident,)).fetchone()
    if not row or (user['role'] != 'admin' and row['user_id'] != user['id']):
        raise HTTPException(404, '没有找到这条反馈')
    return dict(row)


def parse(model, payload):
    try:
        return model.model_validate_json(payload)
    except ValidationError:
        raise HTTPException(422, '请检查反馈内容、分类及关联信息；正文限8000字') from None


async def screenshot(file):
    if file is None:
        return b'', ''
    try:
        data = await file.read(3 * 1024 * 1024 + 1)
    finally:
        await file.close()
    if len(data) > 3 * 1024 * 1024:
        raise HTTPException(413, '截图不能超过3 MB')
    media = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
             'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
             'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else '')
    if not media:
        raise HTTPException(422, '请选择PNG、JPEG或WebP截图')
    return data, media


def fingerprint(body, image):
    return hashlib.sha256(body.model_dump_json().encode() + image).hexdigest()


def mark_read(db, ident, user_id, revision):
    db.execute('INSERT INTO user_feedback_reads VALUES(?,?,?) ON CONFLICT(feedback_id,user_id) '
               'DO UPDATE SET revision=MAX(revision,excluded.revision)', (ident, user_id, revision))


def add_message(db, ident, user, body, digest, image, media, now):
    db.execute('INSERT INTO user_feedback_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (str(uuid4()), ident, user['id'], user['name'], user['role'], body.request_key,
                digest, body.text, getattr(body, 'status', None), now, image or None, media))


def router(services):
    routes = APIRouter(prefix='/api/v1/feedback')
    path = services.projects.store.db_path

    async def source_info(source, user):
        if not source.project_id:
            return ''
        pid = source.project_id
        await services.accounts.require_project(user, pid)
        try:
            project = await services.projects.store.get(pid)
            if source.workflow_id and await services.projects.store.membership(source.workflow_id) != pid:
                raise KeyError()
            if source.task_id:
                await services.projects.store.get_task(pid, source.task_id)
            if source.conversation_id:
                await services.project_sessions.require(pid, source.conversation_id, user)
            if source.request_id:
                with conversation_scope(pid, '' if source.conversation_id == 'legacy' else source.conversation_id):
                    if not any(e.get('request_id') == source.request_id for e in services.local_agents.load(pid)['events']):
                        raise KeyError()
        except KeyError:
            raise HTTPException(404, '关联操作不存在或不属于这个项目') from None
        return project['name']

    @routes.post('', status_code=201)
    async def create(request: Request, payload: str = Form(...), image: UploadFile | None = File(None)):
        body = parse(NewFeedback, payload)
        data, media = await screenshot(image)
        user = request.state.user
        name = await source_info(body.source, user)
        digest = fingerprint(body, data)

        def save():
            with connect(path) as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT id,payload_hash FROM user_feedback WHERE user_id=? AND request_key=?',
                                 (user['id'], body.request_key)).fetchone()
                if old:
                    if old['payload_hash'] != digest:
                        raise HTTPException(409, '这次提交已保存，修改内容请作为补充发送')
                    return {'id': old['id']}
                ident, now = str(uuid4()), utc_now()
                db.execute('INSERT INTO user_feedback VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (ident, user['id'], user['name'], body.request_key, digest, body.category, 'received',
                            body.text[:160], body.source.model_dump_json(), name, body.excerpt, now, now, 1))
                add_message(db, ident, user, body, digest, data, media, now)
                mark_read(db, ident, user['id'], 1)
                return {'id': ident}
        return await asyncio.to_thread(save)

    @routes.get('/unread')
    async def unread(request: Request):
        user = request.state.user
        def read():
            with connect(path) as db:
                row = db.execute('SELECT COUNT(*) FROM user_feedback f LEFT JOIN user_feedback_reads r '
                                 'ON r.feedback_id=f.id AND r.user_id=? WHERE f.revision>COALESCE(r.revision,0) '
                                 'AND (? OR f.user_id=?)', (user['id'], user['role'] == 'admin', user['id'])).fetchone()
                return {'count': row[0]}
        return await asyncio.to_thread(read)

    @routes.get('')
    async def listing(request: Request, scope: Literal['mine', 'all'] = 'mine', status: Status | None = None,
                      category: Category | None = None, q: str = Query('', max_length=100),
                      page: Literal['general', 'conversation', 'workflow', 'run'] | None = None,
                      project_id: str = Query('', max_length=100), offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100)):
        user = request.state.user
        if scope == 'all' and user['role'] != 'admin':
            raise HTTPException(403, '只有管理员可以查看全部反馈')
        where, args = ['1=1'], []
        for sql, value in [('f.user_id=?', user['id'] if scope == 'mine' else None),
                           ('f.status=?', status), ('f.category=?', category),
                           ("json_extract(f.source_json,'$.page')=?", page),
                           ("json_extract(f.source_json,'$.project_id')=?", project_id or None)]:
            if value is not None:
                where.append(sql); args.append(value)
        if q:
            where.append('(f.summary LIKE ? OR f.project_name LIKE ? OR f.user_name LIKE ?)')
            args.extend(['%' + q + '%'] * 3)
        def read():
            with connect(path) as db:
                query = ' FROM user_feedback f WHERE ' + ' AND '.join(where)
                total = db.execute('SELECT COUNT(*)' + query, args).fetchone()[0]
                rows = db.execute('SELECT f.id,f.summary,f.user_name,f.category,f.status,f.project_name,f.updated_at,f.revision,'
                                  'COALESCE((SELECT revision FROM user_feedback_reads WHERE feedback_id=f.id AND user_id=?),0) AS read_revision'
                                  + query + ' ORDER BY f.updated_at DESC,f.id LIMIT ? OFFSET ?', [user['id'], *args, limit, offset])
                return {'items': [dict(r) for r in rows], 'total': total}
        return await asyncio.to_thread(read)

    @routes.get('/{ident}')
    async def detail(ident: str, request: Request):
        user = request.state.user
        def read():
            with connect(path) as db:
                row = require_thread(db, ident, user)
                for key in ('payload_hash', 'request_key'):
                    row.pop(key)
                row['source'] = json.loads(row.pop('source_json'))
                row['messages'] = [dict(r) for r in db.execute(
                    'SELECT id,user_id,user_name,role,text,status,created_at,media_type FROM user_feedback_messages '
                    'WHERE feedback_id=? ORDER BY rowid', (ident,))]
                return row
        row = await asyncio.to_thread(read)
        try:
            await source_info(Source.model_validate(row['source']), user)
            row['source_available'] = bool(row['source']['project_id'])
        except HTTPException:
            row['source_available'] = False
        return row

    @routes.post('/{ident}/read')
    async def seen(ident: str, body: ReadProgress, request: Request):
        def save():
            with connect(path) as db:
                row = require_thread(db, ident, request.state.user)
                mark_read(db, ident, request.state.user['id'], min(body.revision, row['revision']))
            return {'ok': True}
        return await asyncio.to_thread(save)

    @routes.post('/{ident}/messages')
    async def reply(ident: str, request: Request, payload: str = Form(...), image: UploadFile | None = File(None)):
        body = parse(Reply, payload)
        data, media = await screenshot(image)
        user, digest = request.state.user, fingerprint(body, data)
        def save():
            with connect(path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = require_thread(db, ident, user)
                old = db.execute('SELECT payload_hash FROM user_feedback_messages WHERE feedback_id=? AND user_id=? AND request_key=?',
                                 (ident, user['id'], body.request_key)).fetchone()
                if old:
                    if old['payload_hash'] != digest:
                        raise HTTPException(409, '这次回复已保存，修改内容请重新提交')
                    return {'id': ident}
                if body.status is not None:
                    if user['role'] != 'admin' and not (body.status == 'received' and row['status'] in {'resolved', 'declined'}):
                        raise HTTPException(403, '只能由管理员处理状态，或由提交者重新打开已结束的反馈')
                    if body.expected_revision != row['revision']:
                        raise HTTPException(409, '反馈已有新进展，请刷新后再修改状态；已填写内容可保留')
                now = utc_now()
                add_message(db, ident, user, body, digest, data, media, now)
                db.execute('UPDATE user_feedback SET status=?,updated_at=?,revision=revision+1 WHERE id=?',
                           (body.status or row['status'], now, ident))
                seen = db.execute('SELECT revision FROM user_feedback_reads WHERE feedback_id=? AND user_id=?',
                                  (ident, user['id'])).fetchone()
                # Sending a reply must not hide an incoming message the sender has not seen.
                if seen and seen['revision'] >= row['revision']:
                    mark_read(db, ident, user['id'], row['revision'] + 1)
            return {'id': ident}
        return await asyncio.to_thread(save)

    @routes.get('/{ident}/messages/{message_id}/image')
    async def attachment(ident: str, message_id: str, request: Request):
        def read():
            with connect(path) as db:
                require_thread(db, ident, request.state.user)
                row = db.execute('SELECT image,media_type FROM user_feedback_messages WHERE id=? AND feedback_id=?',
                                 (message_id, ident)).fetchone()
                if not row or not row['image']:
                    raise HTTPException(404, '截图不存在')
                return row['image'], row['media_type']
        data, media = await asyncio.to_thread(read)
        return Response(data, media_type=media, headers={'Cache-Control': 'private, no-store',
                        'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'"})

    return routes
