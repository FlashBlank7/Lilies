"""Administrator-owned subscription service; isolated clients and a small SQLite queue."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .codex_app_server import CodexAppServer, inspect_executable, validate_codex_version
from .conversation_scope import conversation_for, conversation_scope
from .project_store import connect

actor_id: ContextVar[str] = ContextVar('official_agent_actor', default='')
SHORT_COMPUTE_WAIT_SECONDS = 15


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    executable: str = 'codex'
    version: str = ''
    model: str = 'gpt-5.6-luna'
    thinking: str = 'max'
    reserve_percent: int = Field(default=50, ge=0, le=100)
    concurrency: int = Field(default=1, ge=1, le=8)
    max_tokens: int | None = Field(default=32000, ge=1000, le=200000)
    max_seconds: int = Field(default=600, ge=30, le=3600)


class Login(BaseModel):
    mode: Literal['existing', 'browser', 'device'] = 'existing'


class AssistantSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allowed: bool = False
    task: Literal['api', 'official'] = 'api'
    generation: Literal['inherit', 'api', 'official'] = 'inherit'


class OfficialAgent:
    def __init__(self, services):
        self.services = services
        self.root = services.settings.data_dir.resolve() / 'official-agent'
        self.db = services.projects.store.db_path
        self.control = None
        self.client_factory = CodexAppServer
        self.control_lock = asyncio.Lock()
        self.dispatch_lock = asyncio.Lock()
        self.last_user = ''
        self.rate_limits = {}
        self.last_error = ''
        self.login = None
        self.initialized = False
        self.shutting_down = False
        self.generation_tasks = {}

    @property
    def auth_file(self):
        return self.root / 'account' / 'codex-home' / 'auth.json'

    def config(self):
        path = self.root / 'config.json'
        return ServiceConfig.model_validate_json(path.read_text()) if path.exists() else ServiceConfig()

    def save_config(self, config):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / 'config.tmp'
        path.write_text(config.model_dump_json())
        path.chmod(0o600)
        path.replace(self.root / 'config.json')

    async def initialize(self):
        with connect(self.db) as db:
            db.execute('CREATE TABLE IF NOT EXISTS official_agent_projects(project_id TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS official_agent_jobs('
                       'id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, '
                       'kind TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, started REAL, ended REAL, '
                       'tokens INTEGER, error TEXT NOT NULL DEFAULT \'\')')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(official_agent_jobs)')}
            for name in ('queue_seconds', 'execution_seconds'):
                if name not in columns:
                    db.execute(f'ALTER TABLE official_agent_jobs ADD COLUMN {name} REAL NOT NULL DEFAULT 0')
            db.execute("DELETE FROM official_agent_jobs WHERE status NOT IN ('queued','running','waiting') AND ended<?", (time.time()-30*86400,))
            db.execute("UPDATE official_agent_jobs SET status='interrupted',ended=?,error='服务重启，进度保留，请手动继续' WHERE status IN ('running','waiting')", (time.time(),))
        self.initialized = True

    async def recover(self):
        # A queued chat has never called the model. Its input is already in its
        # private transcript; replay only these, never an interrupted training.
        for row in self.jobs('queued'):
            if row['kind'] == 'generation':
                from .official_generation_jobs import path, launch
                target = path(self, row['id'])
                if target.exists():
                    launch(self, row['id'], json.loads(target.read_text()))
                else:
                    self.update(row['id'], status='interrupted', error='生成请求未保存，请重新生成')
                continue
            with conversation_scope(row['project_id'], row['conversation_id']):
                manager = self.services.local_agents
                token = actor_id.set(row['user_id'])
                try:
                    if not manager.running(row['project_id']):
                        manager.tasks[manager.key(row['project_id'])] = asyncio.create_task(manager._run(
                            row['project_id'], manager.last_user_message(row['project_id'])))
                finally:
                    actor_id.reset(token)

    def source(self, project_id):
        if not self.initialized:
            return AssistantSource()
        with connect(self.db) as db:
            row = db.execute('SELECT value FROM official_agent_projects WHERE project_id=?', (project_id,)).fetchone()
        return AssistantSource.model_validate_json(row['value']) if row else AssistantSource()

    def selected(self, project_id, role='task'):
        source = self.source(project_id)
        value = source.task if role == 'task' or source.generation == 'inherit' else source.generation
        return value == 'official'

    def public_connection(self):
        config = self.config()
        return {'provider': 'official', 'model': config.model, 'thinking': config.thinking, 'name': '官方智能体'}

    async def authorize(self, project_id, user_id=None):
        source = self.source(project_id)
        project = await self.services.projects.store.get(project_id)
        if not source.allowed or not project['agent_modules_enabled']:
            raise ValueError('此项目未获准使用官方智能体，请联系管理员')
        identity = user_id or actor_id.get()
        if identity == 'root':
            user = {'id': 'root', 'role': 'admin'}
        else:
            with connect(self.db) as db:
                row = db.execute('SELECT id,role,status FROM users WHERE id=?', (identity,)).fetchone()
            if not row or row['status'] != 'active':
                raise ValueError('员工账号已失效，请重新登录')
            user = dict(row)
        await self.services.accounts.require_project(user, project_id)
        if conversation_for(project_id):
            await self.services.project_sessions.require(project_id, conversation_for(project_id), user)
        return user

    async def transport(self):
        async with self.control_lock:
            if self.control is None or (self.control.process is not None and self.control.process.returncode is not None):
                config = self.config()
                info = await inspect_executable(config.executable)
                validate_codex_version(info['version'])
                if config.version and config.version != info['version']:
                    raise ValueError('服务器 Codex 版本已变化，请管理员重新连接并验证')
                self.control = self.client_factory(info['path'], self.root / 'account',
                    auth_file=self.auth_file, subscription_only=True)
                try:
                    await self.control.connect()
                except BaseException:
                    await self.control.close()
                    self.control = None
                    raise
            return self.control

    async def inspect(self):
        client = await self.transport()
        account = (await client.request('account/read', {'refreshToken': False})).get('account')
        models = []
        cursor = None
        for _ in range(20):
            result = await client.request('model/list', {'limit': 100, 'includeHidden': True, **({'cursor': cursor} if cursor else {})})
            models.extend(result.get('data', []))
            cursor = result.get('nextCursor')
            if not cursor:
                break
        self.rate_limits = {}
        self.last_error = ''
        if account and account.get('type') == 'chatgpt':
            try:
                self.rate_limits = await client.request('account/rateLimits/read', {})
            except Exception:
                self.last_error = '暂时无法取得账号额度，新请求等待额度恢复'
        else:
            self.last_error = '请连接订阅账号；API Key 登录不能用于官方智能体'
        config = self.config()
        match = next((m for m in models if m.get('model') == config.model), None)
        options = [e['reasoningEffort'] for e in (match or {}).get('supportedReasoningEfforts', [])]
        selection_error = '' if match and config.thinking in options else f'账号目录不支持 {config.model} / {config.thinking}，请管理员检查设置'
        return {'config': config.model_dump(), 'account': account, 'models': models, 'rate_limits': self.rate_limits,
                'error': self.last_error or selection_error, 'selection_error': selection_error,
                'dispatch_reason': self.quota_reason(), 'login': self.login}

    def quota_reason(self):
        config = self.config()
        if not config.enabled:
            return '官方智能体尚未启用'
        buckets = list((self.rate_limits.get('rateLimitsByLimitId') or {}).values()) or [self.rate_limits.get('rateLimits') or {}]
        windows = [b.get(k) for b in buckets for k in ('primary', 'secondary') if b.get(k)]
        if self.rate_limits.get('ordinaryUsageAllowed') is False or any(b.get('spendControlReached') for b in buckets):
            return '订阅当前不允许继续使用；不会购买额度或切换计费来源'
        if not windows or any(not isinstance(w.get('usedPercent'), (int, float)) for w in windows):
            return '额度未知，等待刷新'
        if any(b.get('rateLimitReachedType') for b in buckets):
            return '账号额度已用尽，等待恢复'
        if any(w['usedPercent'] >= 100 - config.reserve_percent for w in windows):
            return '已达到管理员设置的额度保留线'
        return ''

    async def connect_account(self, mode):
        if self.active_jobs():
            raise ValueError('请先停止活动及排队任务，再更换账号')
        for key in list(self.services.local_agents.clients):
            if getattr(self.services.local_agents.clients[key], 'subscription_only', False):
                await self.services.local_agents.clients.pop(key).close()
        if self.control:
            await self.control.close()
            self.control = None
        info = await inspect_executable(self.config().executable)
        validate_codex_version(info['version'])
        config = self.config().model_copy(update={'executable': info['path'], 'version': info['version']})
        self.save_config(config)
        if mode == 'existing':
            source = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
            if not source.is_file():
                raise ValueError('服务器没有已登录账号，请使用登录入口')
            self.auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Copy only login material, never personal config, skills or history.
            temporary = self.auth_file.with_suffix('.tmp')
            with open(temporary, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as out:
                out.write(source.read_text())
            temporary.replace(self.auth_file)
            self.login = None
            return await self.inspect()
        client = await self.transport()
        self.login = await client.request('account/login/start', {'type': 'chatgptDeviceCode' if mode == 'device' else 'chatgpt'})
        return {'login': self.login}

    def jobs(self, status=None):
        with connect(self.db) as db:
            if status:
                return [dict(r) for r in db.execute('SELECT * FROM official_agent_jobs WHERE status=? ORDER BY created', (status,))]
            return [dict(r) for r in db.execute('SELECT * FROM official_agent_jobs ORDER BY created DESC LIMIT 100')]

    def job(self, job_id):
        with connect(self.db) as db:
            row = db.execute('SELECT * FROM official_agent_jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def active_jobs(self, project_id=None):
        with connect(self.db) as db:
            return bool(db.execute("SELECT 1 FROM official_agent_jobs WHERE status IN ('queued','running','waiting') "
                + ('AND project_id=? ' if project_id else '') + 'LIMIT 1', (project_id,) if project_id else ()).fetchone())

    def update(self, job_id, **values):
        allowed = {'status', 'started', 'ended', 'tokens', 'error', 'queue_seconds', 'execution_seconds'}
        if set(values) - allowed:
            raise ValueError('无效任务更新')
        with connect(self.db) as db:
            db.execute('UPDATE official_agent_jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), job_id))

    async def enqueue(self, project_id, job_id, kind='chat'):
        user = await self.authorize(project_id)
        with connect(self.db) as db:
            db.execute('INSERT OR IGNORE INTO official_agent_jobs(id,project_id,conversation_id,user_id,kind,status,created) VALUES(?,?,?,?,?,?,?)',
                       (job_id, project_id, conversation_for(project_id), user['id'], kind, 'queued', time.time()))

    async def acquire(self, project_id, job_id):
        await self.authorize(project_id)
        self.update(job_id, status='queued')
        next_probe = 0
        queued_at = time.time()
        while True:
            await self.authorize(project_id)
            async with self.dispatch_lock:
                config = self.config()
                running = self.jobs('running')
                queued = self.jobs('queued')
                # Oldest request of a different employee goes first; a single
                # employee still uses the service when nobody else is waiting.
                eligible = [r for r in queued if not any(x['project_id'] == r['project_id'] and x['conversation_id'] == r['conversation_id'] for x in running)]
                choices = [r for r in eligible if r['user_id'] != self.last_user] or eligible
                if choices and choices[0]['id'] == job_id and len(running) < config.concurrency and time.monotonic() >= next_probe:
                    try:
                        if not config.enabled:
                            raise ValueError('官方智能体尚未启用')
                        snapshot = await self.inspect()
                        reason = snapshot['error'] or snapshot['dispatch_reason']
                    except ValueError as error:
                        reason = str(error)
                    except Exception:
                        reason = '账号连接暂不可用，等待重新连接'
                    next_probe = time.monotonic() + 15
                    self.update(job_id, error=reason)
                    if not reason:
                        self.last_user = choices[0]['user_id']
                        row = choices[0]
                        self.update(job_id, status='running', started=row['started'] or time.time(), error='',
                                    queue_seconds=row['queue_seconds'] + time.time() - queued_at)
                        return
            await asyncio.sleep(.25)

    def client(self, project_id, runtime_dir):
        config = self.config()
        return self.client_factory(config.executable, runtime_dir, model=config.model, thinking=config.thinking,
                                   auth_file=self.auth_file, subscription_only=True,
                                   allow_model_calls=self.services.settings.model_egress_enabled)

    async def run_turn(self, project_id, job_id, client, message, on_event, on_tool):
        manager = self.services.local_agents
        state = manager.load(project_id)
        state.update(status='queued')
        manager.save(project_id, state)
        await self.acquire(project_id, job_id)
        state = manager.load(project_id)
        state.update(status='running')
        manager.save(project_id, state)
        config = self.config()
        began = time.monotonic()
        job = self.job(job_id) or {}
        remaining = config.max_seconds - job.get('execution_seconds', 0)
        if remaining <= 0:
            raise ValueError('已达到本任务模型执行时限，进度已保留')
        baseline = state.get('official_total_tokens', 0)
        previous_tokens = (self.job(job_id) or {}).get('tokens') or 0
        budget_exceeded = False

        async def event(method, params):
            nonlocal budget_exceeded
            if method == 'account/rateLimits/updated':
                self.rate_limits = params
                return
            if method == 'thread/tokenUsage/updated':
                usage = params.get('tokenUsage') or {}
                total = (usage.get('total') or {}).get('totalTokens')
                if isinstance(total, int):
                    used = previous_tokens + max(0, total - baseline)
                    self.update(job_id, tokens=used)
                    current = manager.load(project_id)
                    current['official_total_tokens'] = total
                    manager.save(project_id, current)
                    if config.max_tokens is not None and used >= config.max_tokens:
                        budget_exceeded = True
                        # The protocol reader cannot await its own response.
                        task = asyncio.create_task(client.interrupt())
                        self.services.background_tasks.add(task)
                        task.add_done_callback(self.services.background_tasks.discard)
            await on_event(method, params)

        async def tool(name, arguments):
            await self.authorize(project_id)
            if budget_exceeded:
                raise ValueError('已达到本任务用量上限')
            # Keep computation in the existing project task. Briefly observe
            # short jobs so their result can answer this tool call directly;
            # longer jobs retain the existing background continuation path.
            starts_compute = (name, arguments.get('action')) in {
                ('workflow_run', 'start'), ('project_modeling', 'train'),
                ('project_modeling', 'submit_and_run'), ('project_models', 'predict'),
            }
            if not starts_compute:
                return await on_tool(name, arguments)
            result = await on_tool(name, {**arguments, 'wait': False})
            wait_seconds = min(SHORT_COMPUTE_WAIT_SECONDS, max(0, int(remaining - (time.monotonic() - began)) - 1))
            if (arguments.get('wait', True) and wait_seconds and isinstance(result, dict)
                    and result.get('id') and result.get('status') in {'queued', 'running'}):
                await self.authorize(project_id)
                return await on_tool('workflow_run', {'action': 'inspect', 'task_id': result['id'],
                    'wait_seconds': wait_seconds, 'view': arguments.get('view', 'summary')})
            return result

        try:
            async with asyncio.timeout(remaining):
                result = await client.turn(message, event, tool, timeout=remaining)
            if budget_exceeded:
                raise ValueError('已达到本任务 token 上限；已保留会话和结果')
            return result
        except (asyncio.CancelledError, TimeoutError) as cause:
            try:
                await client.interrupt()
            except Exception:
                pass
            if isinstance(cause, TimeoutError):
                raise ValueError('已达到本任务模型执行时限，进度已保留') from cause
            raise
        finally:
            row = self.job(job_id) or {}
            self.update(job_id, status='waiting', execution_seconds=row.get('execution_seconds', 0) + time.monotonic() - began)

    async def close(self):
        pending = list(self.generation_tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.control:
            await self.control.close()
            self.control = None


def official_router(services):
    router = APIRouter(prefix='/api/v1')
    service = services.official_agent

    async def invoke(fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error

    @router.get('/admin/official-agent')
    async def status(refresh: bool = False):
        result = {'config': service.config().model_dump(), 'jobs': service.jobs()}
        if refresh:
            result.update(await invoke(service.inspect))
        return result

    @router.put('/admin/official-agent')
    async def configure(body: ServiceConfig):
        previous = service.config()
        changed = (body.model, body.thinking, body.executable) != (previous.model, previous.thinking, previous.executable)
        if service.active_jobs() and changed:
            raise HTTPException(409, '请先停止运行中的任务再更换模型')
        # Only a successful connection can pin/change the verified version.
        body.version = previous.version
        if changed:
            if service.control:
                await service.control.close()
                service.control = None
            for key in list(services.local_agents.clients):
                client = services.local_agents.clients[key]
                if getattr(client, 'subscription_only', False):
                    await services.local_agents.clients.pop(key).close()
        service.save_config(body)
        return {'config': body.model_dump()}

    @router.post('/admin/official-agent/login')
    async def login(body: Login):
        return await invoke(service.connect_account, body.mode)

    @router.post('/admin/official-agent/disconnect')
    async def disconnect():
        if service.active_jobs():
            raise HTTPException(409, '请先停止活动及排队任务')
        service.save_config(service.config().model_copy(update={'enabled': False}))
        client = await service.transport()
        await client.request('account/logout', {})
        service.login = None
        await service.close()
        return {'ok': True}

    @router.post('/admin/official-agent/jobs/{job_id}/stop')
    async def stop_job(job_id: str):
        row = service.job(job_id)
        if not row:
            raise HTTPException(404, '任务不存在')
        if row['kind'] == 'generation':
            task = service.generation_tasks.get(job_id)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return {'ok': True}
        with conversation_scope(row['project_id'], row['conversation_id']):
            manager = services.local_agents
            if manager.load(row['project_id']).get('request_id') != job_id:
                raise HTTPException(409, '该会话已开始其他任务，未停止新任务')
            await manager.stop(row['project_id'])
        return {'ok': True}

    @router.get('/projects/{project_id}/generation-jobs/{job_id}')
    async def generation_status(project_id: str, job_id: str, request: Request):
        from .official_generation_jobs import read
        return await read(service, project_id, job_id, request.state.user)

    @router.post('/projects/{project_id}/generation-jobs/{job_id}/stop')
    async def generation_stop(project_id: str, job_id: str, request: Request):
        from .official_generation_jobs import read
        await read(service, project_id, job_id, request.state.user)
        task = service.generation_tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await read(service, project_id, job_id, request.state.user)

    @router.get('/projects/{project_id}/assistant-source')
    async def source(project_id: str):
        return {**service.source(project_id).model_dump(), 'service_enabled': service.config().enabled,
                'model': service.config().model, 'thinking': service.config().thinking}

    @router.put('/projects/{project_id}/assistant-source')
    async def set_source(project_id: str, body: AssistantSource, request: Request):
        await services.accounts.require_project(request.state.user, project_id, owner=True)
        old = service.source(project_id)
        if body.allowed != old.allowed and request.state.user['role'] != 'admin':
            raise HTTPException(403, '只有管理员可以授权官方智能体')
        if body.task == 'official' or body.generation == 'official':
            project = await services.projects.store.get(project_id)
            if not body.allowed or not project['agent_modules_enabled']:
                raise HTTPException(422, '请先由管理员允许此项目使用完整智能体，并授权官方智能体')
        manager = services.local_agents
        if manager.project_running(project_id) or service.active_jobs(project_id):
            raise HTTPException(409, '请先停止项目中的活动会话')
        with connect(service.db) as db:
            db.execute('INSERT OR REPLACE INTO official_agent_projects VALUES(?,?)', (project_id, body.model_dump_json()))
        for key in list(manager.clients):
            if key == project_id or key.startswith(project_id + ':'):
                await manager.clients.pop(key).close()
        # Provider thread ids cannot cross API/official modes. Preserve the
        # visible conversation, handing off only its own recent messages.
        for path in [manager.root / project_id / 'session.json', * (manager.root / project_id / 'conversations').glob('*/session.json')]:
            if path.is_file():
                state = json.loads(path.read_text())
                state.update(thread_id=None, context_handoff=True, session_id=str(uuid4()), official_total_tokens=0)
                path.write_text(json.dumps(state, ensure_ascii=False))
        return await source(project_id)

    return router
