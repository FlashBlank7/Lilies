"""Owner-facing project API. Native agents receive only project-bound tools."""
import zipfile
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .local_agent_api import AgentToolCall, SelectAgent
from .project_agent_tools import WorkspaceProjectTools, project_tool_specs
from .project_store import ProjectConflict
from .project_conversation import ConversationMessage, ProgressUpdate
from .requirement_discussion import load_discussion
from .requirement_package import import_package
from .model_connections import ModelConnection


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NewProject(Body):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)
    requirement: str = Field(default='', max_length=28000)


class AccessMember(Body):
    name: str = Field(min_length=1, max_length=40)
    role: Literal['owner', 'collaborator'] = 'collaborator'


class NewMember(Body):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)
    purpose: Literal['business', 'test'] = 'business'


class CopyModelConnection(Body):
    source_project_id: str
    role: Literal['main', 'vision', 'generation'] = 'main'
    source_role: Literal['main', 'vision', 'generation'] | None = None


class CopyMaterial(Body):
    source_project_id: str
    source_path: str = Field(min_length=1)


class MemberPurpose(Body):
    purpose: Literal['business', 'test']


class ProjectCapabilities(Body):
    agent_modules_enabled: bool = Field(strict=True)


class Message(Body):
    message: str = Field(default='', max_length=8000)
    intent: Literal['discuss', 'build', 'operate'] = 'discuss'


class Confirmation(Body):
    revision: int = Field(ge=0)


class NewTask(Body):
    request_key: str = Field(min_length=1, max_length=240)
    mode: Literal['workflow', 'agent'] = 'workflow'
    workflow_id: str = ''
    inputs: dict[str, Any] = Field(default_factory=dict)
    message: str = Field(default='', max_length=8000)
    purpose: Literal['build_test', 'customer_trial', 'business'] = 'business'
    item_id: str = ''
    feedback_task_id: str = ''
    reuse_task_id: str = ''


class RecordUpdate(Body):
    value: dict[str, Any]
    expected_revision: int = Field(ge=0, strict=True)


class Supplement(Body):
    message: str = Field(default='', max_length=8000)
    inputs: dict[str, Any] = Field(default_factory=dict)


class HumanResponse(Body):
    values: dict[str, Any]
    node_id: str = ''
    resume: bool = False


async def invoke(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except ProjectConflict as e:
        raise HTTPException(409, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, RuntimeError, OSError) as e:
        raise HTTPException(422, str(e)) from e


def project_router(services, require_token):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(require_token)])
    projects, manager = services.projects, services.local_agents

    async def require_project(project_id: str):
        return await invoke(projects.store.get, project_id)

    scoped = APIRouter(prefix='/projects/{project_id}', dependencies=[Depends(require_project)])
    from .example_projects import register_example_routes
    register_example_routes(router, scoped, services, invoke)

    @router.get('/projects')
    async def list_projects(request: Request):
        items = []
        for project in await projects.store.list():
            role = await services.accounts.project_role(request.state.user, project['id'])
            if role:
                items.append({**project, 'access_role': role})
        return items

    @router.post('/projects', status_code=201)
    async def create_project(body: NewProject, request: Request):
        project = await invoke(projects.create, **body.model_dump())
        if request.state.user['id'] != 'root':
            await services.accounts.add_member(project['id'], request.state.user['id'], 'owner')
        return {**project, 'access_role': 'owner' if request.state.user['role'] != 'admin' else 'admin'}

    @router.post('/projects/requirement-packages/import', status_code=201)
    async def import_project(request: Request, file: UploadFile = File(...)):
        try:
            if not (file.filename or '').lower().endswith('.zip'):
                raise ValueError('请选择 ZIP 格式的需求包')
            result = await import_package(file.file, services.settings.workspace_root, services.workflow_store)
            app = result['application']
            project = await projects.adopt_new_application(app['id'], app['name'], app['description'])
            if request.state.user['id'] != 'root':
                await services.accounts.add_member(project['id'], request.state.user['id'], 'owner')
            return {**result, 'project': project}
        except (ValueError, zipfile.BadZipFile, NotImplementedError) as e:
            raise HTTPException(422, str(e)) from e
        finally:
            await file.close()

    @router.get('/applications/{application_id}/project')
    async def application_project(application_id: str):
        return {'project_id': await projects.store.membership(application_id)}

    @scoped.get('')
    async def get_project(project_id: str, request: Request):
        return {**await projects.store.get(project_id),
                'access_role': await services.accounts.project_role(request.state.user, project_id)}

    @scoped.get('/access-members')
    async def access_members(project_id: str):
        return await services.accounts.members(project_id)

    @scoped.post('/access-members')
    async def add_access_member(project_id: str, body: AccessMember, request: Request):
        await services.accounts.require_project(request.state.user, project_id, owner=True)
        user = await services.storage.user_by_name(body.name.strip())
        if not user or user['status'] != 'active':
            raise HTTPException(404, '未找到可加入项目的账号，请核对用户名')
        # Adding an existing owner as a collaborator must not remove ownership.
        if body.role == 'collaborator' and await services.accounts.project_role(user, project_id) == 'owner':
            raise HTTPException(409, '请通过转交负责人操作修改项目负责人')
        await services.accounts.add_member(project_id, user['id'], body.role)
        return await services.accounts.members(project_id)

    @scoped.delete('/access-members/{user_id}')
    async def remove_access_member(project_id: str, user_id: str, request: Request):
        await services.accounts.require_project(request.state.user, project_id, owner=True)
        await services.accounts.remove_member(project_id, user_id)
        return {'ok': True}

    @scoped.get('/members')
    async def list_members(project_id: str):
        return (await projects.store.get(project_id))['members']

    @scoped.put('/capabilities')
    async def set_capabilities(project_id: str, body: ProjectCapabilities):
        # Owner API only: project-bound Builder tools cannot enable capabilities.
        return await projects.store.set_agent_modules(project_id, body.agent_modules_enabled)

    @scoped.patch('/members/{workflow_id}')
    async def classify_member(project_id: str, workflow_id: str, body: MemberPurpose):
        await invoke(projects.member, project_id, workflow_id)
        await projects.store.classify_member(project_id, workflow_id, body.purpose)
        return {'workflow_id': workflow_id, 'purpose': body.purpose}

    @scoped.get('/progress')
    async def progress(project_id: str):
        return await projects.store.progress(project_id)

    @scoped.put('/progress')
    async def update_progress(project_id: str, body: ProgressUpdate):
        return await invoke(projects.conversation.update, project_id, body.value, body.expected_revision)

    @scoped.get('/topology')
    async def topology(project_id: str):
        return await projects.conversation.topology(project_id)

    @scoped.get('/conversation')
    async def conversation(project_id: str, request: Request, after: str = '', before: str = '',
                           limit: int = Query(default=50, ge=1, le=100),
                           kind: Literal['messages', 'tools', 'activity'] = 'messages', request_id: str = ''):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        try:
            return projects.conversation.events(project_id, after=after, before=before, limit=limit, kind=kind, request_id=request_id)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @scoped.post('/conversation/messages', status_code=202)
    async def conversation_message(project_id: str, request: Request, body: ConversationMessage):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        await invoke(projects.conversation.send, project_id, body)
        # Avoid returning the entire historical transcript on every message.
        return projects.conversation.events(project_id)

    @scoped.get('/conversation/metrics')
    async def conversation_metrics(project_id: str, request: Request, request_id: str = ''):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        from .project_metrics import session_metrics
        return session_metrics(manager.load(project_id)['events'], request_id)

    @scoped.post('/members', status_code=201)
    async def add_member(project_id: str, body: NewMember):
        return await invoke(projects.add_member, project_id, **body.model_dump())

    @scoped.delete('/members/{workflow_id}')
    async def remove_member(project_id: str, workflow_id: str):
        await invoke(projects.remove_member, project_id, workflow_id)
        return {'removed': workflow_id}

    @scoped.post('/members/{workflow_id}/tests/run')
    async def test_project_member(project_id: str, workflow_id: str):
        return await invoke(projects.test_workflow, project_id, workflow_id)

    @scoped.get('/requirement-package')
    async def package_files(project_id: str):
        return await WorkspaceProjectTools(services, project_id, manager).call('project_file', {'action': 'list', 'path': 'requirement-package'})

    @scoped.post('/materials', status_code=201)
    async def upload_material(project_id: str, file: UploadFile = File(...)):
        from .project_materials import add_material
        return await invoke(add_material, services, project_id, file)

    @scoped.post('/materials/copy', status_code=201)
    async def copy_material(project_id: str, body: CopyMaterial, request: Request):
        from .project_materials import copy_material as copy
        await services.accounts.require_project(request.state.user, body.source_project_id)
        return await invoke(copy, services, project_id, body.source_project_id, body.source_path)

    @scoped.get('/requirements')
    async def requirements(project_id: str):
        return load_discussion(projects.workspace(project_id))

    @scoped.post('/requirements/messages', status_code=202)
    async def discuss(project_id: str, request: Request, body: Message):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        return await invoke(manager.message, project_id, body.message or '请阅读需求包并与我核对理解', intent='discuss')

    @scoped.post('/requirements/confirm')
    async def confirm(project_id: str, body: Confirmation):
        return await invoke(projects.confirm, project_id, body.revision)

    @scoped.get('/agent-session')
    async def session(project_id: str, request: Request):
        if await services.project_sessions.legacy_allowed(project_id, request.state.user):
            return {**manager.load(project_id), 'requirements': load_discussion(projects.workspace(project_id)),
                    'raw_connection': manager.connections.public(manager.connections.load(project_id)) if manager.connections.load(project_id) else None}
        connection = manager.connections.load(project_id)
        return {**(manager.connections.public(connection) if connection else {'provider': None}),
                'status': 'idle', 'events': [], 'revision': 0, 'error': ''}

    @scoped.put('/agent-session')
    async def select(project_id: str, body: SelectAgent):
        return await invoke(manager.select, project_id, **body.model_dump())

    @scoped.get('/vision-model')
    async def vision_model(project_id: str):
        connection = manager.connections.load(project_id, 'vision')
        return manager.connections.public(connection) if connection else {'provider': None, 'has_api_key': False, 'runtime_enabled': False}

    @scoped.put('/vision-model')
    async def save_vision_model(project_id: str, body: ModelConnection):
        try:
            return manager.connections.save(project_id, body, role='vision')
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @scoped.get('/generation-model')
    async def generation_model(project_id: str):
        return manager.connections.generation_settings(project_id)

    @scoped.put('/generation-model')
    async def save_generation_model(project_id: str, body: ModelConnection):
        try:
            manager.connections.save(project_id, body, role='generation')
            return manager.connections.generation_settings(project_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @scoped.delete('/generation-model')
    async def inherit_generation_model(project_id: str):
        return manager.connections.inherit_generation(project_id)

    @scoped.post('/model-connection/copy')
    async def copy_model_connection(project_id: str, body: CopyModelConnection, request: Request):
        await require_project(body.source_project_id)
        await services.accounts.require_project(request.state.user, body.source_project_id, owner=True)
        connection = manager.connections.load(body.source_project_id, body.source_role or body.role)
        if not connection or connection.provider != 'api':
            raise HTTPException(422, '所选项目尚未配置此用途的 API 模型')
        if body.role == 'vision':
            return await save_vision_model(project_id, connection)
        if body.role == 'generation':
            return await save_generation_model(project_id, connection)
        return await invoke(manager.select, project_id, **connection.model_dump())

    @scoped.post('/agent-session/messages', status_code=202)
    async def message(project_id: str, request: Request, body: Message):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        if body.intent == 'operate' and not manager.running(project_id):
            raise HTTPException(409, '请通过项目任务发起或继续业务处理')
        return await invoke(manager.message, project_id, body.message or '继续', intent=body.intent)

    @scoped.post('/agent-session/stop')
    async def stop_agent(project_id: str, request: Request):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        return await manager.stop(project_id)

    @scoped.post('/agent-session/resume', status_code=202)
    async def resume_agent(project_id: str, request: Request, body: Message):
        await services.project_sessions.require(project_id, 'legacy', request.state.user)
        state = manager.load(project_id)
        if state.get('phase') == 'operate' and state.get('project_task_id'):
            return await invoke(projects.resume, project_id, state['project_task_id'], body.message)
        return await invoke(manager.message, project_id, body.message or '继续', intent=state['phase'])

    @scoped.get('/agent-tools')
    async def agent_tools(project_id: str):
        return {'contract_version': 1, 'tools': project_tool_specs()}

    @scoped.post('/agent-tools')
    async def call_tool(project_id: str, body: AgentToolCall):
        return await invoke(WorkspaceProjectTools(services, project_id, manager).call, body.name, body.arguments)

    @scoped.get('/records')
    async def records(project_id: str, collection: str | None = None):
        return await projects.store.records(project_id, collection)

    @scoped.get('/records/{collection}/{key:path}')
    async def record(project_id: str, collection: str, key: str):
        return await projects.store.get_record(project_id, collection, key)

    @scoped.put('/records/{collection}/{key:path}')
    async def update_record(project_id: str, collection: str, key: str, body: RecordUpdate):
        return await invoke(projects.store.put_record, project_id, collection, key, body.value, body.expected_revision)

    @scoped.get('/tasks')
    async def tasks(project_id: str, purpose: Literal['', 'business', 'customer_trial', 'build_test', 'unclassified'] = '',
                    item_id: str = '', before: str = '', limit: int = Query(default=100, ge=1, le=100), compact: bool = False):
        result = await invoke(projects.store.tasks, project_id, purpose=purpose, item_id=item_id, before=before, limit=limit)
        if compact:
            return [{k: v for k, v in task.items() if k not in {'inputs', 'outputs'}} for task in result]
        return result

    @scoped.post('/tasks', status_code=202)
    async def start_task(project_id: str, body: NewTask, request: Request):
        if body.mode == 'agent':
            await services.project_sessions.require(project_id, 'legacy', request.state.user)
        return await invoke(projects.start, project_id, **body.model_dump())

    @scoped.get('/tasks/{task_id}')
    async def task(project_id: str, task_id: str):
        return await invoke(projects.task, project_id, task_id)

    async def require_task_conversation(project_id, task_id, request):
        task = await invoke(projects.store.get_task, project_id, task_id)
        if task['mode'] == 'agent':
            await services.project_sessions.require(project_id, task.get('conversation_id') or 'legacy', request.state.user)

    @scoped.post('/tasks/{task_id}/stop')
    async def stop_task(project_id: str, task_id: str, request: Request):
        await require_task_conversation(project_id, task_id, request)
        return await invoke(projects.stop, project_id, task_id)

    @scoped.post('/tasks/{task_id}/resume', status_code=202)
    async def resume_task(project_id: str, task_id: str, request: Request, body: Message):
        await require_task_conversation(project_id, task_id, request)
        return await invoke(projects.resume, project_id, task_id, body.message)

    @scoped.post('/tasks/{task_id}/supplements')
    async def supplement_task(project_id: str, task_id: str, request: Request, body: Supplement):
        await require_task_conversation(project_id, task_id, request)
        return await invoke(projects.supplement, project_id, task_id, body.message, body.inputs)

    @scoped.post('/tasks/{task_id}/runs/{run_id}/input')
    async def project_task_run_input(project_id: str, task_id: str, request: Request, run_id: str, body: HumanResponse):
        await require_task_conversation(project_id, task_id, request)
        return await invoke(projects.respond, project_id, task_id, run_id, body.values, body.node_id, body.resume)

    from .modeling_api import register_modeling_routes
    register_modeling_routes(scoped, services, invoke)
    from .project_resources import register_resource_routes
    register_resource_routes(scoped, services, invoke)
    from .project_knowledge import register_knowledge_routes
    register_knowledge_routes(scoped, services, invoke)
    from .project_workflow_edit import register_workflow_edit_routes
    register_workflow_edit_routes(scoped, services, invoke)
    from .project_skills import register_skill_routes
    register_skill_routes(scoped, services, invoke)
    from .project_sessions import register_session_routes
    register_session_routes(scoped, services, invoke)
    from .project_space import register_space_routes
    register_space_routes(scoped, services, invoke)
    router.include_router(scoped)
    return router
