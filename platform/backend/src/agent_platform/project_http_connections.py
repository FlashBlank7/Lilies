"""Project-owned HTTP destinations. Secrets stay in the existing encrypted store."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import connect
from .project_store import ProjectConflict, encode


METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'}


def checked_path(path: str) -> str:
    # Reject ambiguous encodings instead of allowing clients/proxies to normalize
    # an authorized path into another one. Query parameters have their own field.
    if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\\?#\r\n'):
        raise ValueError('请使用以 / 开头的相对路径，查询参数单独填写')
    decoded = unquote(path)
    if decoded != path or any(p in {'.', '..'} for p in path.split('/')):
        raise ValueError('服务路径不能包含编码路径或 . / .. 跳转')
    return path


def checked_base(value: str) -> str:
    url = urlsplit(value.strip())
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('基础地址必须是无凭据、查询参数和片段的 HTTP(S) 地址')
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ValueError('不支持这个网络地址')
    _ = url.port  # Validate the port even if no request has been made yet.
    checked_path(url.path or '/')
    return url.geturl().rstrip('/')


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    allowed_methods: list[str] = Field(default_factory=lambda: ['GET'])
    auth_header: str = ''
    # Full header value (e.g. Bearer ...). None retains an existing credential.
    credential: str | None = Field(default=None, max_length=16000)
    enabled: bool = True
    idempotency_header: str = ''
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid(self):
        self.base_url = checked_base(self.base_url)
        self.allowed_methods = sorted(set(m.upper() for m in self.allowed_methods))
        if not self.allowed_methods or not set(self.allowed_methods) <= METHODS:
            raise ValueError('请选择支持的 HTTP 方法')
        self.allowed_paths = sorted(set(self.allowed_paths))
        for path in self.allowed_paths:
            checked_path(path.removesuffix('/**') or '/')
            if '*' in path.removesuffix('/**'):
                raise ValueError('路径只支持精确匹配或以 /** 结尾的范围')
        for header in (self.auth_header, self.idempotency_header):
            if header and (not re.fullmatch(r'[A-Za-z0-9-]+', header) or header.lower() in {'host', 'content-length', 'cookie', 'connection', 'transfer-encoding'}):
                raise ValueError('不支持这个认证或幂等请求头')
        if self.auth_header and self.auth_header.lower() == self.idempotency_header.lower():
            raise ValueError('认证请求头与幂等请求头必须不同')
        return self


def scope_digest(item: dict) -> str:
    return hashlib.sha256(encode({k: item[k] for k in ('base_url', 'allowed_paths', 'allowed_methods', 'auth_header', 'idempotency_header')}).encode()).hexdigest()


def target_url(connection: dict, path: str, method: str) -> str:
    checked_path(path)
    if method not in connection['allowed_methods']:
        raise ValueError('服务连接不允许此 HTTP 方法：' + method)
    allowed = any(path == pattern or pattern.endswith('/**') and (
        path == pattern[:-3] or path.startswith(pattern[:-3] + '/')) for pattern in connection['allowed_paths'])
    if not allowed:
        raise ValueError('请求路径超出服务连接范围：' + path)
    return connection['base_url'] + path


class ProjectHTTPConnections:
    def __init__(self, services):
        self.services = services
        self.db_path = services.storage.db_path
        self.locks: dict[str, asyncio.Lock] = {}

    async def initialize(self):
        def create():
            with connect(self.db_path) as db:
                db.execute('''CREATE TABLE IF NOT EXISTS project_http_connections (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    revision INTEGER NOT NULL, config_json TEXT NOT NULL, secret_name TEXT NOT NULL
                )''')
        await asyncio.to_thread(create)

    async def rows(self, pid):
        def read():
            with connect(self.db_path) as db:
                return [dict(r) for r in db.execute('SELECT * FROM project_http_connections WHERE project_id=? ORDER BY id', (pid,))]
        return await asyncio.to_thread(read)

    @staticmethod
    def public(row):
        item = {**json.loads(row['config_json']), 'id': row['id'], 'revision': row['revision'], 'has_credential': bool(row['secret_name'])}
        item['scope_digest'] = scope_digest(item)
        return item

    async def list(self, pid):
        await self.services.projects.store.get(pid)
        return [self.public(r) for r in await self.rows(pid)]

    async def save(self, pid, cid, body: ConnectionInput):
        await self.services.projects.store.get(pid)
        async with self.locks.setdefault(pid, asyncio.Lock()):
            rows = await self.rows(pid)
            old = next((r for r in rows if r['id'] == cid), None)
            if cid and not old:
                raise KeyError('没有找到当前项目的服务连接')
            if (old['revision'] if old else 0) != body.expected_revision:
                raise ProjectConflict('服务连接已修改，请刷新后重试')
            cid = cid or str(uuid4())
            secret = old['secret_name'] if old else ''
            if not body.auth_header:
                secret = ''
            elif body.credential is not None:
                secret = 'http-' + str(uuid4()) if body.credential else ''
                if secret:
                    await self.services.harness.save_secret(owner_id=pid, name=secret, value=body.credential, description='项目服务连接凭据')
            config = body.model_dump(exclude={'credential', 'expected_revision'})
            def write():
                with connect(self.db_path) as db:
                    if old:
                        changed = db.execute('UPDATE project_http_connections SET revision=revision+1, config_json=?, secret_name=? WHERE id=? AND project_id=? AND revision=?', (encode(config), secret, cid, pid, body.expected_revision)).rowcount
                        if not changed:
                            raise ProjectConflict('服务连接已修改，请刷新后重试')
                    else:
                        db.execute('INSERT INTO project_http_connections VALUES (?,?,1,?,?)', (cid, pid, encode(config), secret))
            await asyncio.to_thread(write)
            return next(c for c in await self.list(pid) if c['id'] == cid)

    async def delete(self, pid, cid):
        def remove():
            with connect(self.db_path) as db:
                if not db.execute('DELETE FROM project_http_connections WHERE id=? AND project_id=?', (cid, pid)).rowcount:
                    raise KeyError('没有找到当前项目的服务连接')
        await asyncio.to_thread(remove)
        return {'deleted': True}

    async def authorize(self, context: dict, cid: str):
        pid = context['project_id']
        frozen = context.get('http_connections', {}).get(cid)
        if not frozen:
            raise ValueError('此连接不属于本次运行固定的项目服务，请配置连接后重新运行')
        row = next((r for r in await self.rows(pid) if r['id'] == cid), None)
        if not row or not self.public(row)['enabled']:
            raise ValueError('服务连接已禁用或移除，不能继续发送请求')
        current = self.public(row)
        if scope_digest(current) != scope_digest(frozen):
            raise ValueError('服务连接地址或权限范围已修改，请重新运行；继续原任务不会扩大范围')
        secret = ''
        if row['secret_name']:
            secret = await self.services.harness._resolve_secret_reference(owner_id=pid, reference={'$secret': row['secret_name']})
        return current, secret


def register_connection_routes(router, services, invoke):
    connections = services.projects.http_connections

    @router.get('/http-connections')
    async def list_connections(project_id: str):
        return await invoke(connections.list, project_id)

    @router.post('/http-connections', status_code=201)
    async def create_connection(project_id: str, body: ConnectionInput, request: Request):
        return await invoke(connections.save, project_id, '', body)

    @router.put('/http-connections/{connection_id}')
    async def update_connection(project_id: str, connection_id: str, body: ConnectionInput, request: Request):
        return await invoke(connections.save, project_id, connection_id, body)

    @router.delete('/http-connections/{connection_id}')
    async def delete_connection(project_id: str, connection_id: str, request: Request):
        return await invoke(connections.delete, project_id, connection_id)
