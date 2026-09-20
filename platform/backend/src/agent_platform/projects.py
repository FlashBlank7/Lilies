"""A project composes existing applications; the existing runtime executes its graph."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from .db import connect
from .project_store import ProjectConflict, ProjectStore, encode
from .project_conversation import ProjectConversation
from .requirement_discussion import DOCUMENT_FILE, load_discussion, save_discussion
from .workflow_models import ApplicationCreateRequest, WorkflowRunRequest


def waiting(outputs: dict) -> bool:
    return outputs.get('task_status') == 'waiting_input' or any(waiting(v) for v in outputs.values() if isinstance(v, dict))


def execution_result(record: dict) -> dict:
    return jsonable_encoder({key: value for key, value in record.items() if key != 'state'})


class Projects:
    def __init__(self, services):
        self.services = services
        self.store = ProjectStore(services.storage.db_path)
        self.active: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.conversation = ProjectConversation(services, self)
        from .project_knowledge import ProjectKnowledge
        self.knowledge = ProjectKnowledge(services)

    def workspace(self, project_id: str):
        # Project id is the id of its main application; legacy application paths remain valid.
        return self.services.settings.workspace_root.resolve() / project_id

    async def blocks_for(self, application_id: str = '', *, project_id: str | None = None):
        from .project_capabilities import ProjectBlocks
        owner = project_id or (await self.store.membership(application_id) if application_id else None)
        # Unscoped catalog browsing is conservative. Legacy standalone apps keep
        # their existing behavior; every project member inherits its owner setting.
        enabled = (await self.store.get(owner))['agent_modules_enabled'] if owner else bool(application_id)
        return ProjectBlocks(self.services.blocks, agent_modules_enabled=enabled)

    async def validate_capabilities(self, application_id: str, snapshot, *, project_id: str | None = None):
        blocks = await self.blocks_for(application_id, project_id=project_id)
        blocks.validate_workflow(snapshot.workflow)

    async def initialize(self):
        await self.store.initialize()
        await self.knowledge.initialize()
        for project in await self.store.list():
            self.services.sandboxes.protect_inputs(self.workspace(project['id']), ['requirement-package', 'requirements'])

    async def create(self, name: str, description: str = '', requirement: str = '') -> dict:
        app = await self.services.workflow_store.create_application(ApplicationCreateRequest(
            name=name, description=description, requirement=requirement))
        return await self.adopt_new_application(app['id'], name, description)

    async def adopt_new_application(self, application_id: str, name: str, description: str):
        project = await self.store.create(application_id, name, description)
        self.workspace(application_id).mkdir(parents=True, exist_ok=True)
        self.services.sandboxes.protect_inputs(self.workspace(application_id), ['requirement-package', 'requirements'])
        return project

    async def member(self, project_id: str, workflow_id: str) -> dict:
        project = await self.store.get(project_id)
        if workflow_id not in {m['id'] for m in project['members']}:
            raise ValueError('只能操作当前项目的成员工作流')
        return project

    async def add_member(self, project_id: str, name: str, description: str = '', purpose: str = 'business') -> dict:
        await self.store.get(project_id)
        app = await self.services.workflow_store.create_application(ApplicationCreateRequest(name=name, description=description))
        await self.store.add_member(project_id, app['id'], purpose)
        return {**app, 'purpose': purpose}

    async def remove_member(self, project_id: str, workflow_id: str):
        await self.member(project_id, workflow_id)
        if workflow_id == project_id:
            raise ProjectConflict('主工作流不能移除')
        frozen = await self.freeze(project_id)
        if any('workflow:' + workflow_id in encode(item['snapshot']['workflow']) for item in frozen.values()):
            raise ProjectConflict('其他工作流仍在调用这个成员，请先移除调用节点')
        if any(t['status'] in {'queued', 'running', 'waiting_input', 'interrupted'} for t in await self.store.tasks(project_id)):
            raise ProjectConflict('项目仍有未完成任务，请先结束任务再移除成员')
        await self.store.remove_member(project_id, workflow_id)

    async def freeze(self, project_id: str) -> dict:
        await self.store.get(project_id)
        def read():
            with connect(self.store.db_path) as c:
                c.execute('BEGIN')
                return {r['application_id']: {'revision': r['revision'], 'content_hash': r['content_hash'], 'snapshot': json.loads(r['snapshot_json'])}
                        for r in c.execute('SELECT d.application_id,d.revision,d.content_hash,d.snapshot_json FROM project_members m '
                                           'JOIN application_drafts d ON d.application_id=m.application_id WHERE m.project_id=?', (project_id,))}
        snapshots = await asyncio.to_thread(read)
        from .project_resources import model_resources
        resources = {m['model_ref']: m for m in await model_resources(self.services, project_id)}
        knowledge = {k['knowledge_ref']: k['active_version'] if k['status'] == 'ready' else ''
                     for k in await self.knowledge.list(project_id)}
        for snapshot in snapshots.values():
            snapshot['model_resources'] = resources
            snapshot['knowledge_resources'] = knowledge
        return snapshots

    async def confirm(self, project_id: str, revision: int) -> dict:
        await self.store.get(project_id)
        async with self.locks.setdefault(project_id, asyncio.Lock()):
            if self.services.local_agents.running(project_id):
                raise ProjectConflict('请等项目统筹本轮结束后确认')
            workspace = self.workspace(project_id)
            state = load_discussion(workspace)
            if state['revision'] != revision:
                raise ProjectConflict('需求文档已更新，请刷新')
            if state['status'] == 'confirmed':
                return state
            if state['status'] != 'review' or len(state['document'].strip()) < 10:
                raise ProjectConflict('请先完成需求沟通并查看文档')
            draft = await self.services.workflow_store.get_draft(project_id)
            snapshot = draft['snapshot'].model_copy(deep=True)
            snapshot.requirement = state['document']
            await self.services.workflow_store.save_draft(project_id, snapshot,
                expected_revision=draft['revision'], idempotency_key=f'project-confirm-{revision}')
            document = workspace / DOCUMENT_FILE
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_text(state['document'] + '\n', encoding='utf-8')
            state.update(status='confirmed', document_path=DOCUMENT_FILE)
            save_discussion(workspace, state)
            return state

    async def task(self, project_id: str, task_id: str) -> dict:
        task = await self.store.get_task(project_id, task_id)
        task['runs'] = await self.store.runs(task_id)
        task['supplements'] = await self.store.supplements(task_id)
        for run in task['runs']:
            run.pop('outputs_json', None)
            state = json.loads(run.pop('state_json'))
            run['waiting_node'] = next((n for n in state['snapshot']['workflow']['nodes']
                                        if n['id'] == state.get('waiting_node_id')), None)
        return task

    async def start(self, project_id: str, *, request_key: str, mode: str = 'workflow',
                    workflow_id: str = '', inputs: dict | None = None, message: str = '',
                    purpose: str = 'business', item_id: str = '', feedback_task_id: str = '',
                    validate_snapshots: Callable[[dict], None] | None = None) -> dict:
        workflow_id = workflow_id or project_id
        await self.member(project_id, workflow_id)
        if item_id:
            await self.conversation.item(project_id, item_id)
        if feedback_task_id:
            await self.store.get_task(project_id, feedback_task_id)
        async with self.locks.setdefault(project_id, asyncio.Lock()):
            # A repeated request returns its existing task even while an agent is working.
            existing = await self.store.task_for_request(project_id, request_key)
            if mode == 'agent' and not existing:
                if self.services.local_agents.running(project_id):
                    raise ProjectConflict('Lilies 正在处理另一轮任务')
                if self.services.local_agents.load(project_id).get('provider') != 'api':
                    raise ProjectConflict('请先连接项目模型')
            snapshots = await self.freeze(project_id)
            if not existing and validate_snapshots:
                validate_snapshots(snapshots)
            task, created = await self.store.create_task(str(uuid4()), project_id, request_key, mode,
                                                        workflow_id, inputs or {}, message, snapshots,
                                                        purpose=purpose, item_id=item_id, feedback_task_id=feedback_task_id)
            if created:
                await self._launch(task)
            return await self.task(project_id, task['id'])

    async def _launch(self, task: dict, message: str = ''):
        from .conversation_scope import conversation_scope
        with conversation_scope(task['project_id'], task.get('conversation_id', '')):
            return await self._launch_in_conversation(task, message)

    async def _launch_in_conversation(self, task: dict, message: str = ''):
        task_id, project_id = task['id'], task['project_id']
        await self.store.update_task(task_id, status='running', error='')
        if task['mode'] == 'agent':
            manager = self.services.local_agents
            state = manager.load(project_id)
            state['project_task_id'] = task_id
            manager.save(project_id, state)
            try:
                await manager.message(project_id, message or task['message'] or '处理此项目任务', intent='operate')
            except Exception as e:
                await self.store.update_task(task_id, status='failed', error=str(e))
                raise
        elif task['mode'] in {'training', 'prediction'}:
            from .project_resources import run_compute
            self.active[task_id] = asyncio.create_task(run_compute(self.services, task))
        else:
            self.active[task_id] = asyncio.create_task(self._run(task))

    async def context(self, project_id: str, task_id: str, step: str) -> dict:
        task = await self.store.get_task(project_id, task_id, snapshots=True)
        return {'project_id': project_id, 'task_id': task_id, 'snapshots': task['snapshots'], 'step': step,
                'model_resources': next(iter(task['snapshots'].values()), {}).get('model_resources', {}),
                'knowledge_resources': next(iter(task['snapshots'].values()), {}).get('knowledge_resources', {})}

    async def execute(self, project_id: str, task_id: str, workflow_id: str, inputs: dict,
                      *, step: str = 'main', parent_run_id: str | None = None, reuse: bool = False,
                      call_chain: list[str] | None = None, workspace_path: str | None = None,
                      record_scope: str = '') -> dict:
        context = await self.context(project_id, task_id, step)
        if record_scope:
            context['record_scope'] = record_scope
        if workflow_id not in context['snapshots']:
            raise ValueError('工作流不在本次项目任务固定的成员范围内')
        input_hash = hashlib.sha256(encode([workflow_id, inputs]).encode()).hexdigest()[:24]
        context['step'] = f'{step}:{input_hash}'
        runtime = self.services.workflow_runtime
        # A saved test's members share its isolated files, not the live project.
        workspace = runtime._resolve_scoped_workspace(
            workspace_path or str(self.workspace(project_id)), self.workspace(project_id))
        self.services.sandboxes.protect_inputs(workspace, ['requirement-package', 'requirements'])
        if reuse:
            runs = await self.store.runs(task_id)
            for run in reversed(runs):
                if run['step_key'] == context['step'] and run['status'] == 'succeeded' and not waiting(run['outputs']):
                    return {'id': run['id'], 'status': run['status'], 'outputs': run['outputs'], 'reused': True}
                if run['step_key'] == context['step'] and run['status'] == 'paused':
                    record = await self.services.workflow_store.get_run(run['id'])
                    state = record['state']
                    values = await self.store.response(task_id, run['id'], state.waiting_node_id or '')
                    node = next((n for n in state.snapshot.workflow.nodes if n.id == state.waiting_node_id), None)
                    if values is None and (node is None or node.type != 'tool'):
                        return execution_result(record)
                    await runtime.resume(run['id'], values or {})
                    await asyncio.shield(runtime.active_tasks[run['id']])
                    return execution_result(await self.services.workflow_store.get_run(run['id']))
                if run['step_key'] == context['step'] and run['status'] in {'failed', 'cancelled'}:
                    record = await self.services.workflow_store.get_run(run['id'])
                    # Resume the persisted node checkpoint; completed writes and calls are retained.
                    await self.services.harness.start_task(run['id'], kind='workflow_run',
                        owner_id=workflow_id, resource_id=run['id'], parent_task_id=parent_run_id,
                        metadata={'origin': 'project_resume'})
                    runtime._start(record['state'])
                    await asyncio.shield(runtime.active_tasks[run['id']])
                    return execution_result(await self.services.workflow_store.get_run(run['id']))
        scope = {'workspace_boundary': str(workspace),
                 'allowed_nested_application_ids': list(context['snapshots']),
                 'allowed_runtime_tools': ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash'],
                 'allowed_network_hosts': [], 'model_access': self.services.local_agents.connections.workflow_enabled(project_id), 'allowed_connector_operations': []}
        creation = asyncio.create_task(runtime.create_run(workflow_id,
            WorkflowRunRequest(inputs=inputs, use_draft=True, workspace_path=str(workspace)),
            project_context=context, parent_task_id=parent_run_id, application_call_chain=call_chain,
            origin='project', triggered_by='项目任务', **scope))
        try:
            run = await asyncio.shield(creation)
        except asyncio.CancelledError:
            run = await creation
            active = runtime.active_tasks.get(run['run_id'])
            if active and not active.done():
                runtime.cancel(run['run_id'])
                await asyncio.gather(active, return_exceptions=True)
            raise
        active = runtime.active_tasks.get(run['run_id'])
        if active:
            await asyncio.shield(active)
        result = await self.services.workflow_store.get_run(run['run_id'])
        return execution_result(result)

    async def _run(self, task: dict):
        try:
            inputs = dict(task['inputs'])
            for item in await self.store.supplements(task['id']):
                inputs.update(item['inputs'])
            result = await self.execute(task['project_id'], task['id'], task['workflow_id'], inputs, reuse=True)
            status = result['status']
            if status == 'paused' or (status == 'succeeded' and waiting(result['outputs'])):
                status = 'waiting_input'
            elif status not in {'succeeded', 'waiting_input'}:
                status = 'failed'
                await self.cancel_runs(task['id'])
            await self.store.update_task(task['id'], status=status, outputs=result['outputs'], error=result.get('error') or '')
        except asyncio.CancelledError:
            await self.cancel_runs(task['id'])
            await self.store.update_task(task['id'], status='interrupted')
        except Exception as e:
            await self.cancel_runs(task['id'])
            await self.store.update_task(task['id'], status='failed', error=str(e))

    async def cancel_runs(self, task_id: str):
        runtime = self.services.workflow_runtime
        while True:
            active = []
            runs = await self.store.runs(task_id)
            for run in runs:
                worker = runtime.active_tasks.get(run['id'])
                if worker and not worker.done():
                    runtime.cancel(run['id'])
                    active.append(worker)
            if active:
                # A cancelled parent may finish creating its shielded child before exiting.
                await asyncio.gather(*active, return_exceptions=True)
                continue
            for run in runs:
                if run['status'] in {'queued', 'running'}:
                    # Cancellation before the runtime coroutine's first instruction has no
                    # exception handler to persist its terminal status.
                    record = await self.services.workflow_store.get_run(run['id'])
                    await self.services.workflow_store.update_run(run['id'], status='cancelled', state=record['state'])
                    await self.services.harness.finish_task(run['id'], status='cancelled')
            break

    @staticmethod
    def require_task_scope(project_id: str, task: dict) -> None:
        from .conversation_scope import conversation_for
        current = conversation_for(project_id)
        if current and task['mode'] == 'agent' and task.get('conversation_id', '') != current:
            raise ProjectConflict('请在任务所属会话中停止或继续智能体任务')

    async def stop(self, project_id: str, task_id: str) -> dict:
        task = await self.store.get_task(project_id, task_id)
        self.require_task_scope(project_id, task)
        if task['mode'] == 'agent':
            from .conversation_scope import conversation_scope
            manager = self.services.local_agents
            with conversation_scope(project_id, task.get('conversation_id', '')):
                if (manager.load(project_id).get('project_task_id') == task_id and manager.running(project_id)
                        and manager.tasks.get(manager.key(project_id)) is not asyncio.current_task()):
                    await manager.stop(project_id)
        worker = self.active.get(task_id)
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await self.cancel_runs(task_id)
        if task['status'] not in {'succeeded', 'failed'}:
            await self.store.update_task(task_id, status='interrupted')
        return await self.task(project_id, task_id)

    async def resume(self, project_id: str, task_id: str, message: str = '') -> dict:
        async with self.locks.setdefault(project_id, asyncio.Lock()):
            task = await self.store.get_task(project_id, task_id)
            self.require_task_scope(project_id, task)
            if task['status'] not in {'waiting_input', 'interrupted', 'failed'}:
                raise ProjectConflict('仅等待补充、中断或失败的任务可以继续')
            from .conversation_scope import conversation_scope
            with conversation_scope(project_id, task.get('conversation_id', '')):
                if task['mode'] == 'agent' and self.services.local_agents.running(project_id):
                    raise ProjectConflict('此会话正在处理另一轮任务')
            await self._launch(task, message)
            return await self.task(project_id, task_id)

    async def supplement(self, project_id: str, task_id: str, message: str, inputs: dict):
        async with self.locks.setdefault(project_id, asyncio.Lock()):
            task = await self.store.get_task(project_id, task_id)
            self.require_task_scope(project_id, task)
            if task['status'] not in {'waiting_input', 'interrupted', 'failed'}:
                raise ProjectConflict('请在任务等待补充或停止后提交补充')
            await self.store.supplement(task_id, message, inputs)
            return await self.task(project_id, task_id)

    async def respond(self, project_id: str, task_id: str, run_id: str, values: dict):
        task = await self.task(project_id, task_id)
        run = next((r for r in task['runs'] if r['id'] == run_id), None)
        if not run or run['status'] != 'paused' or not run['waiting_node']:
            raise ProjectConflict('此任务的成员运行没有等待输入')
        await self.store.response(task_id, run_id, run['waiting_node']['id'], values)
        return {'saved': True, 'message': '输入已保存，点击继续后执行'}

    async def finish_agent(self, project_id: str, task_id: str, status: str, error: str):
        if status in {'interrupted', 'error'}:
            await self.cancel_runs(task_id)
            await self.store.update_task(task_id, status='interrupted' if status == 'interrupted' else 'failed', error=error)
        else:
            task = await self.store.get_task(project_id, task_id)
            if task['status'] == 'running':
                # A completed model turn alone is not a completed business task.
                await self.store.update_task(task_id, status='waiting_input', error='请查看统筹回复并补充或继续')

    async def test_workflow(self, project_id: str, workflow_id: str):
        await self.member(project_id, workflow_id)
        frozen = await self.freeze(project_id)
        if not frozen[workflow_id]['snapshot']['tests']:
            raise ValueError('请先为工作流添加测试用例')
        task, _ = await self.store.create_task(str(uuid4()), project_id, 'tests-' + str(uuid4()),
            'workflow', workflow_id, {}, '保存的工作流测试', frozen, purpose='build_test')
        await self.store.update_task(task['id'], status='running')
        if self.services.local_agents.current_operation.get():
            self.services.local_agents.track_project_task(project_id, task['id'])
        try:
            report = await self.services.workflow_runtime.run_test_suite(workflow_id,
                workspace_path=str(self.workspace(project_id)), workspace_boundary=str(self.workspace(project_id)),
                allowed_nested_application_ids=list(frozen),
                allowed_runtime_tools=['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash'],
                allowed_network_hosts=[], model_access=self.services.local_agents.connections.workflow_enabled(project_id), allowed_connector_operations=[],
                project_context=await self.context(project_id, task['id'], 'tests'))
            await self.store.update_task(task['id'], status='succeeded' if report['passed'] else 'failed', outputs=report)
            return {**report, 'project_task_id': task['id']}
        except BaseException as e:
            await self.cancel_runs(task['id'])
            await self.store.update_task(task['id'], status='interrupted' if isinstance(e, asyncio.CancelledError) else 'failed', error=str(e))
            raise

    async def close(self):
        for task in self.active.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.active.values(), return_exceptions=True)
