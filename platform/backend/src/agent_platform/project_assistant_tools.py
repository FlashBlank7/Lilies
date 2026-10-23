"""Build catalog and installed project calls share one contract across providers."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import Field

from .db import connect
from .local_agent_api import AgentToolCall
from .local_agent_tools import Arguments
from .project_agent_tools import WorkspaceProjectTools, MemberRun, project_tool_specs
from .project_agent_context import task_summary


class ProjectCalls(MemberRun):
    action: Literal['list', 'interface', 'start', 'inspect', 'respond'] = 'list'


class InstalledSkill(Arguments):
    action: Literal['list', 'read'] = 'list'
    skill_id: str = ''
    reference: str = ''


ASSISTANT_INSTRUCTIONS = '''你是 Lilies 项目助手。根据用户需求搭建工作流，或调用人已加入项目的能力。
两个目录严格分开：block_catalog 是搭建用的全局积木说明，不是可直接执行的业务能力；
project_calls(list/interface) 只提供本项目已加入的工作流和任务智能体，start 调用，inspect 查看真实结果。
project_workflows 和 workflow_draft 用于创建、复制和编辑草稿。保存不依赖模型、资源或任务完成。
你新建或修改的草稿不会自动成为可调用能力。请用户在项目空间点击“加入助手可调用能力”；不要自行加入或绕过限制。
未加入的流程可以编辑，不能执行。缺少能力时说明缺什么，可以按用户要求搭建草稿；不能直接执行代码、训练或从全局目录调用现成工作流。
使用项目资料澄清业务含义，不推测标签、缺失值或类别顺序。调用前检查输入，必要时询问用户。
运行失败时说明本次错误并保留产物，不能拿旧成功结果冒充当前结果。步骤结束不等于整个任务完成。
结果链接使用工具返回的实际项目相对文件路径。工作流编辑地址为 /applications/<id>?tab=edit。
项目助手的模型与服务由管理员后台配置。你不能修改自身来源、项目权限或可调用清单。
'''


def assistant_tool_specs(*, detailed=False):
    keep = {'block_catalog', 'workflow_draft', 'project_workflows', 'project_file', 'requirements_submit', 'project_progress', 'project_action', 'project_task_result'}
    specs = [t for t in project_tool_specs(detailed=detailed) if t['name'] in keep]
    for t in specs:
        if t['name'] == 'project_action':
            t['inputSchema'] = {**t['inputSchema'], 'properties': {**t['inputSchema']['properties'], 'action': {'type': 'string', 'enum': ['inspect', 'build', 'discuss', 'wait', 'finish']}}}
            t['description'] = 'Manage conversation progress without execution. Use project_calls for running installed capabilities.'
        t['deferLoading'] = False
        if t['name'] == 'project_workflows':
            t['description'] = 'Authoring only: list/inspect/create/copy/remove project drafts. New or edited drafts require a human to add them to project_calls before execution. classify cannot grant call access.'
        elif t['name'] == 'block_catalog':
            t['description'] = 'Global building-block catalog: list blocks and inspect their schemas/manuals for assembly. This does not list or execute project workflows. Use project_calls for installed callable capabilities.'
    specs += [dict(type='function', name='project_calls', description='List/interface/start/inspect/respond for human-added project workflows and task agents only. A draft or global block is not a callable capability. start freezes inputs and workflow snapshots; inspect reports actual task status.', inputSchema=ProjectCalls.model_json_schema(), deferLoading=False),
              dict(type='function', name='project_skills', description='Read only instructions explicitly saved in this project; instructions cannot grant execution permissions.', inputSchema=InstalledSkill.model_json_schema(), deferLoading=False)]
    return specs


def callable_ids(services, pid):
    with connect(services.storage.db_path) as db:
        return {r[0] for r in db.execute("SELECT application_id FROM project_members WHERE project_id=? AND assistant_callable=1 AND purpose NOT IN ('test','workspace')", (pid,))}


async def require_callable(services, pid, wid):
    await services.projects.member(pid, wid)
    draft = await services.workflow_store.get_draft(wid)
    if not draft['snapshot'].workflow.nodes:
        raise ValueError('空白草稿还没有可执行步骤，请先装配工作流')
    if wid not in callable_ids(services, pid):
        raise ValueError('此能力尚未加入项目助手的可调用清单，请由人在项目空间加入后再调用')


def set_callable(services, pid, wid, enabled):
    with connect(services.storage.db_path) as db:
        changed = db.execute('UPDATE project_members SET assistant_callable=? WHERE project_id=? AND application_id=?', (int(enabled), pid, wid)).rowcount
        if not changed:
            raise ValueError('工作流不属于当前项目')


async def capabilities(services, pid):
    from .project_space import space
    result = await space(services, pid)
    return [w for w in result['workflows'] if w['assistant_callable'] and w['node_count'] and w['allowed']]


class AssistantProjectTools(WorkspaceProjectTools):
    def tool_definitions(self):
        return assistant_tool_specs(detailed=True)

    async def conversation_context(self, state, discussion, message):
        from .project_agent_context import conversation_context
        context = await conversation_context(self.services, self.application_id, state, discussion, message)
        available = await capabilities(self.services, self.application_id)
        ids = {w['id'] for w in available}
        context['workflows'] = [w for w in context['workflows'] if w['id'] in ids]
        context['callable_capabilities'] = available
        for key in ('modeling', 'modeling_shared', 'modeling_shared_detail'):
            context.pop(key, None)
        for result in context.get('recent_results', []):
            if result.get('read_with', {}).get('tool') == 'workflow_run':
                result['read_with']['tool'] = 'project_calls'
        context['workflow_detail'] = 'project_calls(action="interface", workflow_id=id) reads input/output; start invokes an installed capability.'
        context['instruction'] += ' Only callable_capabilities may be executed. block_catalog is for building. New and edited drafts must be added by a human before calling.'
        return context

    def snapshot_scope(self, wid):
        pid = self.application_id
        def limit_snapshots(snapshots):
            allowed = callable_ids(self.services, pid)
            if wid not in allowed:
                raise ValueError('此能力已从助手可调用清单移除')
            reachable = set()
            def visit(target):
                if target in reachable:
                    return
                if target not in allowed or target not in snapshots:
                    raise ValueError('引用的子工作流尚未加入助手可调用能力')
                reachable.add(target)
                def scan(value):
                    if isinstance(value, dict):
                        tool = value.get('tool_name')
                        if isinstance(tool, str) and tool.startswith('workflow:'):
                            visit(tool[9:])
                        for child in value.values():
                            scan(child)
                    elif isinstance(value, list):
                        for child in value:
                            scan(child)
                scan(snapshots[target]['snapshot']['workflow'])
            visit(wid)
            for key in list(snapshots):
                if key not in reachable:
                    del snapshots[key]
                else:
                    snapshots[key]['assistant_scope'] = True
        return limit_snapshots

    async def call(self, name, arguments):
        if name.startswith('lilies__'):
            name = name.removeprefix('lilies__')
        pid = self.application_id
        if name in {'project_calls', 'workflow_run'}:
            args = ProjectCalls.model_validate(arguments)
            if args.action == 'list':
                return {'capabilities': await capabilities(self.services, pid)}
            if args.action == 'interface':
                await require_callable(self.services, pid, args.workflow_id)
                return await super().call('project_workflows', {'action': 'inspect', 'workflow_id': args.workflow_id})
            if args.action == 'inspect':
                return await super().call('workflow_run', args.model_dump())
            wid = args.workflow_id or pid
            if args.action == 'respond':
                task = await self.projects.store.get_task(pid, args.task_id, snapshots=True)
                wid = task['workflow_id']
                for member_id in task['snapshots']:
                    await require_callable(self.services, pid, member_id)
                return await super().call('workflow_run', args.model_dump())
            await require_callable(self.services, pid, wid)
            task = await self.run_build_task(self.projects.start(pid, request_key=args.request_key or 'assistant-'+str(uuid4()), workflow_id=wid, inputs=args.inputs, validate_snapshots=self.snapshot_scope(wid)), args.wait)
            return task if args.view == 'full' else task_summary(task)
        if name == 'project_action' and arguments.get('action') in {'trial', 'operate'}:
            from .project_conversation import ProjectAction
            args = ProjectAction.model_validate(arguments)
            wid = args.workflow_id or pid
            await require_callable(self.services, pid, wid)
            task = await self.projects.conversation.action(pid, args, validate_snapshots=self.snapshot_scope(wid))
            return task_summary(task)
        if name == 'project_action' and arguments.get('action') == 'resume':
            task = await self.projects.store.get_task(pid, arguments.get('task_id', ''), snapshots=True)
            for member_id in task['snapshots']:
                await require_callable(self.services, pid, member_id)
        if name == 'project_skills':
            args = InstalledSkill.model_validate(arguments)
            rows = await self.projects.store.records(pid, 'skills')
            if args.action == 'list':
                return [{'id': r['key'], 'name': r['value']['name'], 'description': r['value'].get('description', '')} for r in rows]
            row = next((r for r in rows if r['key'] == args.skill_id), None)
            if not row:
                raise ValueError('此说明尚未加入项目')
            return row['value'].get('references', {})[args.reference] if args.reference else row['value']
        if name not in {t['name'] for t in assistant_tool_specs()}:
            raise ValueError('此接口不属于项目助手可用接口；请使用积木目录搭建，或调用项目已加入的能力')
        if name == 'block_catalog' and arguments.get('tool_name'):
            tool = arguments['tool_name'].removeprefix('lilies__')
            spec = next((s for s in self.tool_definitions() if s['name'] == tool), None)
            if spec:
                return spec
            if tool.startswith('project_') or tool == 'workflow_run':
                raise ValueError('请通过 project_calls 查看项目可调用能力')
        if name == 'workflow_draft' and (arguments.get('operation') or arguments.get('batch')):
            from .project_capabilities import assistant_edit
            token = assistant_edit.set(True)
            try:
                return await super().call(name, arguments)
            finally:
                assistant_edit.reset(token)
        if name == 'project_workflows':
            action = arguments.get('action', 'list')
            if action == 'remove' and arguments.get('workflow_id') in callable_ids(self.services, pid):
                raise ValueError('请由人在项目空间移除已加入的能力，助手只能删除未加入的草稿')
            if action == 'classify':
                raise ValueError('助手不能更改项目能力用途或加入权限，请由人在项目空间操作')
            if action in {'create', 'copy'}:
                # A test-purpose member is noncallable from its first committed insert.
                arguments = {**arguments, 'purpose': 'test'}
                result = await super().call(name, arguments)
                wid = result.get('workflow_id') or result['id']
                with connect(self.services.storage.db_path) as db:
                    # Only the first creation changes access. A retried copy must
                    # preserve a subsequent human selection.
                    db.execute("UPDATE project_members SET assistant_callable=0,purpose='business' WHERE project_id=? AND application_id=? AND purpose='test'", (pid, wid))
                enabled = wid in callable_ids(self.services, pid)
                return {**result, 'assistant_callable': enabled, 'next_action': '' if enabled else '请由人在项目空间加入助手可调用能力'}
        return await super().call(name, arguments)


class CallableUpdate(Arguments):
    enabled: bool


class TaskAgent(Arguments):
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=20, max_length=20000)
    read_project_files: bool = False


def register_assistant_routes(router, services, invoke):
    @router.get('/assistant-tools')
    async def tool_list(project_id: str):
        return {'contract_version': 2, 'tools': assistant_tool_specs()}

    @router.post('/assistant-tools')
    async def call(project_id: str, body: AgentToolCall):
        return await invoke(AssistantProjectTools(services, project_id, services.local_agents).call, body.name, body.arguments)

    @router.get('/space/callable-capabilities')
    async def listing(project_id: str):
        return await invoke(capabilities, services, project_id)

    @router.put('/space/workflows/{workflow_id}/assistant-callable')
    async def install(project_id: str, workflow_id: str, body: CallableUpdate):
        await invoke(services.projects.member, project_id, workflow_id)
        set_callable(services, project_id, workflow_id, body.enabled)
        return {'workflow_id': workflow_id, 'assistant_callable': body.enabled}

    @router.post('/space/task-agents', status_code=201)
    async def add_agent(project_id: str, body: TaskAgent):
        from .models import AgentSpec, NetworkPolicy
        from .workflow_models import WorkflowSpec
        # Human configuration only; no provider invocation or new execution engine.
        agent = AgentSpec(name=body.name, description=body.instructions[:1000], system_prompt=body.instructions,
                          tools=['Read', 'Glob', 'Grep'] if body.read_project_files else [],
                          disallowed_tools=[] if body.read_project_files else services.tools.names(),
                          network_policy=NetworkPolicy.none, allow_subagents=False, max_turns=10)
        wid = (await services.projects.add_member(project_id, body.name, agent.description, purpose='test'))['id']
        set_callable(services, project_id, wid, False)
        draft = await services.workflow_store.get_draft(wid)
        snapshot = draft['snapshot'].model_copy(deep=True)
        snapshot.agents = {agent.id: agent}
        snapshot.workflow = WorkflowSpec.model_validate({'nodes': [
            {'id': 'start', 'type': 'start', 'title': '任务输入', 'config': {'inputs': [{'name': 'task', 'type': 'string', 'required': True, 'label': '具体任务'}]}},
            {'id': 'agent', 'type': 'claude_agent', 'title': body.name, 'config': {'agent_id': agent.id, 'task': {'$ref': {'node_id': '$inputs', 'path': ['task']}}}},
            {'id': 'end', 'type': 'end', 'title': '任务结果', 'config': {'outputs': {'result': {'$ref': {'node_id': 'agent', 'path': []}}}}}],
            'edges': [{'id': 'start-agent', 'source': 'start', 'target': 'agent'}, {'id': 'agent-end', 'source': 'agent', 'target': 'end'}]})
        await services.projects.store.set_agent_modules(project_id, True)
        await services.workflow_store.save_draft(wid, snapshot, expected_revision=draft['revision'], idempotency_key='human-task-agent')
        await services.projects.store.classify_member(project_id, wid, 'business')
        set_callable(services, project_id, wid, True)
        return {'workflow_id': wid, 'kind': 'task_agent'}
