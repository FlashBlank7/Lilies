"""Local accounts, revocable device sessions and project membership."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import time
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .db import connect
from .onboarding import OnboardingPatch, onboarding


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200_000)
    return f'pbkdf2${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algorithm, salt, expected = (stored or '').split('$')
        if algorithm != 'pbkdf2':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200_000)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(user: dict) -> dict:
    return {key: user[key] for key in ('id', 'name', 'role', 'status')}


@lru_cache(maxsize=1)
def dummy_password_hash():
    return password_hash(secrets.token_urlsafe(24))


class Accounts:
    def __init__(self, storage, settings):
        self.storage, self.settings = storage, settings
        self.db_path = storage.db_path
        self._dummy_hash = dummy_password_hash()

    async def initialize(self):
        await asyncio.to_thread(self.storage._ensure_user_columns)
        def initialize():
            with connect(self.db_path) as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS auth_sessions (
                        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                        created_at REAL NOT NULL, expires_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
                    CREATE TABLE IF NOT EXISTS auth_attempts (
                        kind TEXT NOT NULL, key TEXT NOT NULL, started_at REAL NOT NULL,
                        count INTEGER NOT NULL, PRIMARY KEY(kind,key)
                    );
                    CREATE TABLE IF NOT EXISTS project_access_members (
                        project_id TEXT NOT NULL REFERENCES projects(id),
                        user_id TEXT NOT NULL REFERENCES users(id),
                        role TEXT NOT NULL CHECK(role IN ('owner','collaborator')),
                        PRIMARY KEY(project_id,user_id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS project_access_owner
                        ON project_access_members(project_id) WHERE role='owner';
                ''')
                db.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (time.time(),))
        await asyncio.to_thread(initialize)

    async def issue(self, user: dict) -> dict:
        token = 'lil_' + secrets.token_urlsafe(32)
        now = time.time()
        expires = now + self.settings.auth_session_days * 86400
        def save():
            with connect(self.db_path) as db:
                db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)',
                           (token_hash(token), user['id'], now, expires))
        await asyncio.to_thread(save)
        return {'user': public_user(user), 'token': token, 'expires_at': expires}

    async def authenticate(self, token: str | None) -> dict:
        if not token:
            raise HTTPException(401, '请先登录')
        if self.settings.api_token and self.settings.api_token != 'change-me' and hmac.compare_digest(token, self.settings.api_token):
            return {'id': 'root', 'name': '管理员', 'role': 'admin', 'status': 'active'}
        def find():
            with connect(self.db_path) as db:
                row = db.execute('''SELECT u.id,u.name,u.role,u.status FROM users u
                    JOIN auth_sessions s ON s.user_id=u.id
                    WHERE s.token_hash=? AND s.expires_at>?''', (token_hash(token), time.time())).fetchone()
                return dict(row) if row else None
        user = await asyncio.to_thread(find)
        # Explicit legacy client tokens retain their identity, never extra access.
        if not user:
            user = await self.storage.user_by_token_hash(token_hash(token))
        if not user or user['status'] != 'active':
            raise HTTPException(401, '登录已失效，请重新登录')
        return user

    async def limit(self, kind: str, key: str, limit: int, seconds: int):
        def reserve():
            now = time.time()
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM auth_attempts WHERE kind=? AND key=?', (kind, key)).fetchone()
                if row and row['started_at'] + seconds > now and row['count'] >= limit:
                    raise HTTPException(429, '尝试过于频繁，请稍后再试',
                                        headers={'Retry-After': str(max(1, int(row['started_at'] + seconds - now)))})
                start, count = (row['started_at'], row['count'] + 1) if row and row['started_at'] + seconds > now else (now, 1)
                db.execute('INSERT OR REPLACE INTO auth_attempts VALUES(?,?,?,?)', (kind, key, start, count))
                db.execute('DELETE FROM auth_attempts WHERE started_at<?', (now - 86400,))
        await asyncio.to_thread(reserve)

    async def clear_limit(self, kind: str, key: str):
        await asyncio.to_thread(self.storage._execute, 'DELETE FROM auth_attempts WHERE kind=? AND key=?', (kind, key))

    async def create(self, name: str, password: str, *, role: str = 'member') -> dict:
        name = name.strip()
        if not 1 <= len(name) <= 40 or any(ord(c) < 32 for c in name):
            raise HTTPException(422, '用户名需为 1–40 个可见字符')
        if not 8 <= len(password) <= 1024:
            raise HTTPException(422, '密码需为 8–1024 位')
        digest = await asyncio.to_thread(password_hash, password)
        try:
            return await self.storage.create_user(name, token_hash(secrets.token_urlsafe(32)), role, password_hash=digest)
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, '用户名已存在') from error

    async def revoke(self, token: str):
        def revoke():
            with connect(self.db_path) as db:
                db.execute('DELETE FROM auth_sessions WHERE token_hash=?', (token_hash(token),))
                db.execute('UPDATE users SET token_hash=? WHERE token_hash=?',
                           (token_hash(secrets.token_urlsafe(32)), token_hash(token)))
        await asyncio.to_thread(revoke)

    async def change_password(self, user_id: str, password: str, *, old_password: str | None = None):
        if not 8 <= len(password) <= 1024:
            raise HTTPException(422, '密码需为 8–1024 位')
        digest = await asyncio.to_thread(password_hash, password)
        def change():
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                if not user:
                    raise HTTPException(404, '没有找到这个账号')
                if old_password is not None and not verify_password(old_password, user['password_hash']):
                    raise HTTPException(400, '当前密码不正确')
                db.execute('UPDATE users SET password_hash=?,token_hash=? WHERE id=?',
                           (digest, token_hash(secrets.token_urlsafe(32)), user_id))
                db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
        await asyncio.to_thread(change)

    async def set_status(self, user_id: str, status: str):
        def change():
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                if not user:
                    raise HTTPException(404, '没有找到这个账号')
                if status == 'disabled' and user['role'] == 'admin' and user['status'] == 'active':
                    count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]
                    if count <= 1:
                        raise HTTPException(409, '不能禁用最后一个管理员')
                db.execute('UPDATE users SET status=? WHERE id=?', (status, user_id))
                if status == 'disabled':
                    db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
                    db.execute('UPDATE users SET token_hash=? WHERE id=?', (token_hash(secrets.token_urlsafe(32)), user_id))
        await asyncio.to_thread(change)

    async def project_role(self, user: dict, project_id: str) -> str | None:
        if user['role'] == 'admin':
            return 'admin'
        def lookup():
            with connect(self.db_path) as db:
                row = db.execute('SELECT role FROM project_access_members WHERE project_id=? AND user_id=?',
                                 (project_id, user['id'])).fetchone()
                return row['role'] if row else None
        return await asyncio.to_thread(lookup)

    async def require_project(self, user: dict, project_id: str, *, owner: bool = False):
        role = await self.project_role(user, project_id)
        if not role:
            raise HTTPException(404, '没有找到这个项目或没有访问权限')
        if owner and role not in {'owner', 'admin'}:
            raise HTTPException(403, '只有项目负责人可以修改此设置')
        return role

    async def add_member(self, project_id: str, user_id: str, role: str = 'collaborator'):
        def add():
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                if role == 'owner':
                    db.execute("UPDATE project_access_members SET role='collaborator' WHERE project_id=? AND role='owner'", (project_id,))
                else:
                    existing = db.execute('SELECT role FROM project_access_members WHERE project_id=? AND user_id=?',
                                          (project_id, user_id)).fetchone()
                    if existing and existing['role'] == 'owner':
                        raise HTTPException(409, '请先转交项目负责人')
                db.execute('INSERT INTO project_access_members VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role',
                           (project_id, user_id, role))
        await asyncio.to_thread(add)

    async def members(self, project_id: str):
        def read():
            with connect(self.db_path) as db:
                return [dict(row) for row in db.execute('''SELECT u.id,u.name,u.status,m.role FROM project_access_members m
                    JOIN users u ON u.id=m.user_id WHERE m.project_id=? ORDER BY m.role DESC,u.name''', (project_id,))]
        return await asyncio.to_thread(read)

    async def remove_member(self, project_id: str, user_id: str):
        def remove():
            with connect(self.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT role FROM project_access_members WHERE project_id=? AND user_id=?',
                                 (project_id, user_id)).fetchone()
                if row and row['role'] == 'owner':
                    raise HTTPException(409, '请先转交项目负责人，再移除此成员')
                db.execute('DELETE FROM project_access_members WHERE project_id=? AND user_id=?', (project_id, user_id))
        await asyncio.to_thread(remove)


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=40)
    password: SecretStr = Field(min_length=1, max_length=1024)


class PasswordChange(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=8, max_length=1024)


class PasswordReset(BaseModel):
    password: SecretStr = Field(min_length=8, max_length=1024)


class UserStatus(BaseModel):
    status: str = Field(pattern='^(active|disabled)$')


def auth_client_address(request: Request, settings) -> str:
    """Only a configured ingress may supply an address for auth rate limits."""
    secret = settings.auth_proxy_secret
    supplied = request.headers.get('x-lilies-proxy-key', '')
    if secret and hmac.compare_digest(secret.encode(), supplied.encode()):
        raw = request.headers.get('x-lilies-client-ip', '')
        try:
            if '%' not in raw:
                address = ipaddress.ip_address(raw)
                return str(getattr(address, 'ipv4_mapped', None) or address)
        except ValueError:
            pass
    return request.client.host if request.client else 'unknown'


def account_router(accounts: Accounts, require_token):
    router = APIRouter(prefix='/api/v1')

    def admin(request):
        if request.state.user['role'] != 'admin':
            raise HTTPException(403, '只有管理员可以管理账号')

    @router.post('/auth/register', status_code=201)
    async def register(body: Credentials, request: Request):
        address = auth_client_address(request, accounts.settings)
        await accounts.limit('register', address, accounts.settings.auth_register_per_hour, 3600)
        return await accounts.issue(await accounts.create(body.name, body.password.get_secret_value()))

    @router.post('/auth/login')
    async def login(body: Credentials, request: Request):
        name = body.name.strip()
        address = auth_client_address(request, accounts.settings)
        key = token_hash(address + '\0' + name)
        await accounts.limit('login', key, accounts.settings.auth_login_failures_per_15m, 900)
        user = await accounts.storage.user_by_name(name)
        valid = await asyncio.to_thread(verify_password, body.password.get_secret_value(),
                                        user.get('password_hash') if user else accounts._dummy_hash)
        if not user or user['status'] != 'active' or not valid:
            raise HTTPException(401, '用户名或密码不正确')
        await accounts.clear_limit('login', key)
        return await accounts.issue(user)

    @router.get('/me', dependencies=[Depends(require_token)])
    async def me(request: Request):
        return {'user': public_user(request.state.user)}

    @router.get('/me/onboarding', dependencies=[Depends(require_token)])
    async def get_onboarding(request: Request):
        return await onboarding(accounts, request.state.user)

    @router.patch('/me/onboarding', dependencies=[Depends(require_token)])
    async def update_onboarding(body: OnboardingPatch, request: Request):
        return await onboarding(accounts, request.state.user, body)

    @router.post('/auth/logout', dependencies=[Depends(require_token)])
    async def logout(request: Request):
        await accounts.revoke(request.state.auth_token)
        return {'ok': True}

    @router.post('/auth/password', dependencies=[Depends(require_token)])
    async def change_password(body: PasswordChange, request: Request):
        await accounts.change_password(request.state.user['id'], body.new_password.get_secret_value(),
                                       old_password=body.current_password.get_secret_value())
        return {'ok': True}

    @router.get('/users', dependencies=[Depends(require_token)])
    async def users(request: Request):
        admin(request)
        return await accounts.storage.list_users()

    @router.post('/users', dependencies=[Depends(require_token)])
    async def create_user(body: Credentials, request: Request):
        admin(request)
        return {'user': public_user(await accounts.create(body.name, body.password.get_secret_value()))}

    @router.post('/users/{user_id}/password', dependencies=[Depends(require_token)])
    async def reset_password(user_id: str, body: PasswordReset, request: Request):
        admin(request)
        await accounts.change_password(user_id, body.password.get_secret_value())
        return {'ok': True}

    @router.post('/users/{user_id}/status', dependencies=[Depends(require_token)])
    async def set_status(user_id: str, body: UserStatus, request: Request):
        admin(request)
        await accounts.set_status(user_id, body.status)
        return {'ok': True, 'status': body.status}

    return router


def main():
    """Deployment-only administrator initialization; never promotes public signup."""
    import argparse
    import getpass
    from .config import get_settings
    from .storage import Storage
    parser = argparse.ArgumentParser(description='初始化 Lilies 管理员')
    parser.add_argument('name', help='新管理员用户名')
    args = parser.parse_args()
    password = getpass.getpass('管理员密码（至少八位）: ')
    if password != getpass.getpass('再次输入密码: '):
        parser.error('两次密码不一致')
    async def initialize():
        settings = get_settings()
        settings.prepare()
        storage = Storage(settings.data_dir)
        await storage.initialize()
        accounts = Accounts(storage, settings)
        await accounts.create(args.name, password, role='admin')
    try:
        asyncio.run(initialize())
    except HTTPException as error:
        parser.error(str(error.detail))
    print('管理员已创建，请在平台页面登录。')


if __name__ == '__main__':
    main()
