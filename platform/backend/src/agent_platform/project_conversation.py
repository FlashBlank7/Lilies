"""Project progress and the small, project-bound conversation action protocol."""
from __future__ import annotations

import asyncio
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .local_agent_tools import Arguments, ProjectTools
from .project_store import ProjectConflict
from .requirement_discussion import load_discussion


class ProgressQuestion(Arguments):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    next_action: str = Field(min_length=1, max_length=2000)
    answer: str = Field(default='', max_length=8000)


class ProgressBlocker(Arguments):
    kind: Literal['data', 'decision', 'platform', 'runtime']
    owner: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=4000)
    next_action: str = Field(min_length=1, max_length=2000)


class ProgressResult(Arguments):
    label: str = Field(min_length=1, max_length=200)
    task_id: str = ''
    file_path: str = ''

    @model_validator(mode='after')
    def target(self):
        if not (self.task_id or self.file_path):
            raise ValueError('结果需要关联实际任务或项目文件')
        return self


class ProgressItem(Arguments):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=3000)
    availability: Literal['not_ready', 'trial', 'usable'] = 'not_ready'
    status: Literal['planned', 'working', 'waiting', 'paused', 'done'] = 'planned'
    summary: str = Field(default='', max_length=4000)
    next_action: str = Field(default='', max_length=2000)
    deliverable: str = Field(default='', max_length=2000, description='Concrete output of the current customer request, within the overall business goal.')
    completion_criteria: list[str] = Field(default_factory=list, max_length=12, description='Observable conditions for this delivery; not approvals or a new execution gate.')
    delivery_request_id: str = ''
    workflow_ids: list[str] = Field(default_factory=list, max_length=100)
    task_ids: list[str] = Field(default_factory=list, max_length=200)
    results: list[ProgressResult] = Field(default_factory=list, max_length=30)
    questions: list[ProgressQuestion] = Field(default_factory=list, max_length=20)
    blocker: ProgressBlocker | None = None
    requirements: list['RequirementComparison'] = Field(default_factory=list, max_length=40)

    @model_validator(mode='after')
    def actionable(self):
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError('同一事项的问题标识不能重复')
        if self.status == 'waiting' and not (self.blocker or self.questions):
            raise ValueError('等待事项必须说明具体问题或阻塞、责任方和恢复动作')
        if self.status in {'planned', 'working', 'paused'} and not self.next_action.strip():
            raise ValueError('未完成事项需要写明下一步动作')
        return self


class RequirementComparison(Arguments):
    requirement: str = Field(min_length=1, max_length=2000)
    source: str = Field(default='', max_length=1000, description='Original requirement document section or source; do not invent one.')
    current: str = Field(min_length=1, max_length=3000)
    status: Literal['met', 'partial', 'unmet', 'unverified'] = 'unverified'
    gap: str = Field(default='', max_length=2000)
    results: list[ProgressResult] = Field(default_factory=list, max_length=10)


class WorkflowNote(Arguments):
    workflow_id: str
    purpose: str = Field(min_length=1, max_length=2000)
    inputs: str = Field(default='', max_length=2000)
    outputs: str = Field(default='', max_length=2000)


class ProgressValue(Arguments):
    goal: str = Field(default='', max_length=8000)
    summary: str = Field(default='', max_length=4000)
    items: list[ProgressItem] = Field(default_factory=list, max_length=100)
    workflows: list[WorkflowNote] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def identities(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError('业务事项标识不能重复')
        if len({note.workflow_id for note in self.workflows}) != len(self.workflows):
            raise ValueError('工作流说明不能重复')
        return self


class ProgressUpdate(Arguments):
    expected_revision: int = Field(ge=0, strict=True)
    value: ProgressValue


class ProgressTool(Arguments):
    action: Literal['read', 'update', 'patch'] = 'read'
    view: Literal['summary', 'full'] = 'summary'
    item_id: str = ''
    expected_revision: int | None = Field(default=None, ge=0, strict=True,
        description='Required for patch/update: use the revision returned by the latest project_progress read. Omit only for read.')
    value: ProgressValue | None = None
    changes: dict = Field(default_factory=dict, description='patch with item_id updates/creates that item; without item_id only goal and summary may change. Other items and customer answers are preserved.')

    @model_validator(mode='after')
    def mutation_revision(self):
        if self.action in {'patch', 'update'} and self.expected_revision is None:
            raise ValueError('project_progress 修改必须提供 expected_revision；先读取当前事项，再使用返回的 revision 提交修改')
        return self


class ConversationMessage(Arguments):
    message: str = Field(default='', max_length=8000)
    item_id: str = ''
    question_id: str = ''
    task_id: str = ''
    dataset_id: str = ''
    study_id: str = ''
    candidate_id: str = ''


class ProjectAction(Arguments):
    action: Literal['inspect', 'build', 'trial', 'operate', 'resume', 'discuss', 'wait', 'finish']
    item_id: str = ''
    message: str = Field(default='', max_length=4000)
    workflow_id: str = ''
    task_id: str = ''
    run_id: str = ''
    request_key: str = Field(default='', max_length=240)
    inputs: dict = Field(default_factory=dict)
    feedback_task_id: str = ''
    deliverable: str = Field(default='', max_length=2000)
    completion_criteria: list[str] = Field(default_factory=list, max_length=12)


CONVERSATION_INSTRUCTIONS = """
统一项目对话模式：客户不需要选择 discuss/build/operate，也不需要懂JSON或工作流编号。
项目页没有“开始搭建”按钮。需求确认后，客户在同一对话说“开始搭建”或点击“继续推进”；不要引导不存在的按钮或内部模式。
先看当前上下文的进展摘要，缺少详情或修订号过期时再读project_progress；按业务能力维护目标、可用程度、建设状态、具体问题及实际结果关联。
project_progress默认返回摘要；item_id读取单项全文，view=full读取完整记录。
日常更新用action=patch、item_id、changes和expected_revision，只修改当前事项。创建事项也可用patch。
仅完整替换记录时才用update，并先view=full读取；不能把摘要当成全文覆盖。
可试用与整体目标完成分开：availability表示可用程度，status表示这一事项正在建设/等待/完成。
进展事项只表示客户业务能力，不把开发测试单列成可试用业务。首页summary控制在约200个汉字，
事项summary用两三句业务说明，next_action用一句话说明客户或统筹下一步；版本号、任务UUID、
profile、JSON参数和工具名留在关联运行/开发详情，不放进客户操作说明。
为各成员维护value.workflows中的purpose/inputs/outputs，用业务语言解释职责、所需输入和产出。
items[].requirements逐项对照原需求：requirement及source写原要求和出处，current写实际达到的范围，
status为met/partial/unmet/unverified，gap说明差距，results关联真实任务或文件。
这只是可修订的进展说明，不增加审批或运行门槛；缺结果用unverified，不从运行成功或测试数量推算达标率。
一条工作流关联多个业务事项时分别描述，不能把事项整体状态当作其中每个成员的独立验收结论。
不要从测试通过推断企业目标达成。不要为读懂进展要求客户翻交接文件；重要进展持续写入平台。
使用project_action选择inspect/build/trial/operate/resume/discuss/wait/finish。
inspect回答状态问题，只读，不改需求、不启动建设。build选中事项后才可编辑并测试；
trial/operate直接执行指定成员或主图，inputs必须真实，自动创建固定当前草稿的任务并关联事项。
resume继续已有业务任务，保持原快照；修复后试用用trial创建新任务并填写feedback_task_id。
build授权后还有可推进事项就继续，包括模型本轮结束后的自动接续；局部资料不足只等待该事项。
先把具体问题及影响/回答后的动作写入该事项questions，再wait。平台能力缺失用platform blocker，
写实际错误、维护方与恢复动作，不叫客户重新批准已授权修改，不自行绕过工具或缩减目标。
客户回复附有item_id/question_id/task_id时优先沿原事项处理；不要重新泛化访谈。
project_action(discuss)只用于实际需要修订需求；普通询问和试用不撤销已确认文档。
业务返回后用project_task_result提交易读的中文Markdown（可含业务表格）和本项目结果文件链接，
保留实际运行原始输出。必要解释必须来自真实运行，失败不能标成功。随后更新事项summary/results。
创建业务或测试成员时明确purpose；已有成员可用project_workflows(action=classify)标注用途。
finish结束当前请求；wait结束依赖外部输入的事项。仍有已授权待做动作时不要随意结束。
每次交办先在原事项写deliverable和completion_criteria，区分本次可交付结果与企业长期目标。
按完整事项连续搭建、运行、检查实际错误、修复及受影响回归；不要把代码错误、字段映射或工具参数问题交给客户指挥。
已通过且未受修改影响的检查不反复重跑。额外优化记入原需求差距，不自行扩大当前交付。
完成本次条件后呈现实际结果并结束本次事项；其他企业目标和真正待补事实保留。
本协议中流程模式由project_action内部切换，不能让客户自己切换内部模式。
"""


class ProjectConversation:
    def __init__(self, services, projects):
        self.services, self.projects = services, projects

    @property
    def manager(self):
        return self.services.local_agents

    async def update(self, project_id: str, value: ProgressValue, expected_revision: int) -> dict:
        project = await self.projects.store.get(project_id)
        members = {m['id'] for m in project['members']}
        files = ProjectTools(self.services, project_id, self.manager)
        if not {note.workflow_id for note in value.workflows} <= members:
            raise ValueError('工作流说明只能关联当前项目的成员')
        for item in value.items:
            if not set(item.workflow_ids) <= members:
                raise ValueError('进展只能关联当前项目的成员')
            results = item.results + [r for comparison in item.requirements for r in comparison.results]
            for task_id in set(item.task_ids + [r.task_id for r in results if r.task_id]):
                await self.projects.store.get_task(project_id, task_id)
            for result in results:
                if result.file_path and not files.path(result.file_path).is_file():
                    raise ValueError('结果文件不存在')
        previous = await self.projects.store.progress(project_id)
        # Replies are customer input, not replaceable agent summaries.
        answers = {(item['id'], q['id']): q['answer'] for item in previous['value']['items']
                   for q in item['questions'] if q['answer']}
        for item in value.items:
            for q in item.questions:
                if (item.id, q.id) in answers:
                    q.answer = answers[item.id, q.id]
        return await self.projects.store.put_progress(project_id, value.model_dump(), expected_revision)

    async def item(self, project_id: str, item_id: str) -> dict:
        progress = await self.projects.store.progress(project_id)
        item = next((i for i in progress['value']['items'] if i['id'] == item_id), None)
        if not item:
            raise ValueError('请先在项目进展中登记此业务事项')
        return item

    async def patch(self, project_id: str, args: ProgressTool) -> dict:
        progress = await self.projects.store.progress(project_id)
        if progress['revision'] != args.expected_revision:
            raise ProjectConflict('项目进展已更新，请读取当前事项及修订号后重试')
        value = progress['value']
        if args.item_id:
            if 'id' in args.changes and args.changes['id'] != args.item_id:
                raise ValueError('patch不能更改事项身份')
            item = next((i for i in value['items'] if i['id'] == args.item_id), None)
            if item is None:
                item = {'id': args.item_id}
                value['items'].append(item)
            item.update(args.changes)
        else:
            if set(args.changes) - {'goal', 'summary'}:
                raise ValueError('项目级patch仅支持goal和summary；事项修改请提供item_id')
            value.update(args.changes)
        return await self.update(project_id, ProgressValue.model_validate(value), args.expected_revision)

    async def patch_item(self, project_id: str, item_id: str, **changes) -> None:
        # One native session owns edits, but customer replies may arrive during tool calls.
        for attempt in range(3):
            progress = await self.projects.store.progress(project_id)
            item = next((i for i in progress['value']['items'] if i['id'] == item_id), None)
            if item is None:
                raise ProjectConflict('业务事项已调整，请读取最新项目进展')
            item.update(changes)
            try:
                await self.update(project_id, ProgressValue.model_validate(progress['value']), progress['revision'])
                return
            except ProjectConflict:
                if attempt == 2:
                    raise

    async def send(self, project_id: str, body: ConversationMessage) -> dict:
        await self.projects.store.get(project_id)
        for kind, ident in [('dataset', body.dataset_id), ('study', body.study_id), ('candidate', body.candidate_id)]:
            if ident:
                obj = await self.services.modeling.get(project_id, kind, ident)
                if kind == 'candidate' and body.study_id and obj['study_id'] != body.study_id:
                    raise ValueError('反馈候选不属于所选研究')
        item = await self.item(project_id, body.item_id) if body.item_id else None
        if body.task_id:
            await self.projects.store.get_task(project_id, body.task_id)
        if body.question_id:
            if not item or not body.message.strip():
                raise ValueError('回复问题需要业务事项及具体回答')
            question = next((q for q in item['questions'] if q['id'] == body.question_id), None)
            if not question:
                raise ValueError('问题不属于此业务事项')
            # Persist customer input without letting an older agent revision overwrite it.
            for attempt in range(3):
                progress = await self.projects.store.progress(project_id)
                latest = next((i for i in progress['value']['items'] if i['id'] == body.item_id), None)
                question = next((q for q in latest['questions'] if q['id'] == body.question_id), None) if latest else None
                if question is None:
                    raise ProjectConflict('问题已调整，请读取最新项目进展后回答')
                question['answer'] = body.message.strip()
                try:
                    await self.projects.store.put_progress(project_id, progress['value'], progress['revision'])
                    break
                except ProjectConflict:
                    if attempt == 2:
                        raise
        return await self.manager.message(project_id, body.message or '继续推进已授权的剩余事项。',
            intent='coordinate', conversation_context=body.model_dump(exclude={'message'}))

    def events(self, project_id: str, *, after: str = '', before: str = '', limit: int = 50,
               kind: str = 'messages', request_id: str = '') -> dict:
        from .project_activity import latest_operations, project_activity
        state = self.manager.load(project_id)
        all_events = state.pop('events')
        chosen = [(index, event) for index, event in enumerate(all_events)
                  if (bool(event.get('operation_id')) if kind == 'activity' else
                      (event['kind'] in {'user', 'assistant'} or (event['kind'] == 'result' and bool(event.get('request_id')) and event.get('purpose') != 'build_test')) == (kind == 'messages'))
                  and (not request_id or event.get('request_id') == request_id)]
        positions = {event['id']: index for index, event in enumerate(all_events)}
        if after and after not in positions or before and before not in positions:
            raise ValueError('会话分页位置不存在')
        candidates = [(index, event) for index, event in chosen
                      if (not after or index > positions[after]) and (not before or index < positions[before])]
        page = candidates[:limit] if after else candidates[-limit:]
        latest = latest_operations(all_events)
        operations = [op for op in latest if op['request_id'] == state.get('request_id')]
        visible_requests = {event.get('request_id') for _, event in page} | {state.get('request_id')}
        summaries = {}
        for op in latest:
            if op['request_id'] and op['request_id'] in visible_requests:
                summaries[op['request_id']] = {k: v for k, v in op.items() if k not in {'arguments', 'result'}}
        current = next((op for op in reversed(operations) if op['status'] == 'running'), operations[-1] if operations else None)
        if current:
            current = {k: v for k, v in current.items() if k not in {'arguments', 'result'}}
        return {**state, 'events': [project_activity(event) if kind == 'activity' else event for _, event in page],
                'current_activity': current, 'request_activity': summaries, 'total': len(chosen),
                'has_more': len(candidates) > len(page),
                'first_cursor': page[0][1]['id'] if page else before,
                'last_cursor': page[-1][1]['id'] if page else after,
                'requirements': load_discussion(self.projects.workspace(project_id))}

    async def action(self, project_id: str, args: ProjectAction) -> dict:
        state = self.manager.load(project_id)
        if not state.get('conversation_enabled'):
            raise ValueError('请从项目统一对话入口发起处理')
        item = await self.item(project_id, args.item_id) if args.item_id else None
        if args.action in {'build', 'wait'} and not item:
            raise ValueError('此动作需要关联业务事项')
        if args.task_id:
            await self.projects.store.get_task(project_id, args.task_id)
        if args.feedback_task_id:
            await self.projects.store.get_task(project_id, args.feedback_task_id)
        if args.workflow_id:
            await self.projects.member(project_id, args.workflow_id)
        if args.action in {'inspect', 'finish'}:
            ready = await self.ready(project_id)
            state = self.manager.load(project_id)
            state.update(phase='coordinate')
            if args.action == 'finish':
                state['continue_work'] = False
            self.manager.save(project_id, state)
            return {'progress': await self.projects.store.progress(project_id), 'ready_items': ready}
        if args.action == 'discuss':
            state = self.manager.load(project_id)
            state.update(phase='discuss', continue_work=False)
            self.manager.save(project_id, state)
            return {'phase': 'discuss', 'instruction': '仅确有需求变更时通过requirements_submit完整修订文档；本动作不撤销确认。'}
        if args.action == 'wait':
            if not (item['blocker'] or any(not q['answer'] for q in item['questions'])):
                raise ValueError('请先登记具体问题或实际阻塞及恢复动作')
            await self.patch_item(project_id, item['id'], status='waiting')
            state = self.manager.load(project_id)
            state.update(phase='coordinate')
            self.manager.save(project_id, state)
            return {'waiting_item': item['id'], 'ready_items': await self.ready(project_id)}
        if args.action == 'build':
            delivery = {'delivery_request_id': state.get('request_id', '')}
            if args.deliverable:
                delivery['deliverable'] = args.deliverable
            if args.completion_criteria:
                delivery['completion_criteria'] = args.completion_criteria
            await self.patch_item(project_id, item['id'], status='working', blocker=None,
                                  next_action=args.message or '继续搭建并验证此业务能力', **delivery)
            # Customer supplements and tool events can arrive during the DB await.
            state = self.manager.load(project_id)
            state.update(phase='build', active_item_id=item['id'], continue_work=True)
            state.pop('project_task_id', None)
            self.manager.save(project_id, state)
            return {'phase': 'build', 'item': await self.item(project_id, item['id'])}
        if args.action == 'resume':
            if not args.task_id:
                raise ValueError('继续业务处理需要原任务标识')
            task = await self.projects.store.get_task(project_id, args.task_id)
            if task['status'] not in {'waiting_input', 'interrupted', 'failed'}:
                raise ValueError('此业务任务不需要继续，请查看已有结果')
            if args.run_id:
                await self.projects.respond(project_id, task['id'], args.run_id, args.inputs)
            # A paused node's answer belongs to its checkpoint. Adding it to the
            # main input would change the step identity and restart completed work.
            if args.message or (args.inputs and not args.run_id):
                await self.projects.store.supplement(task['id'], args.message, {} if args.run_id else args.inputs)
            if task['mode'] == 'agent':
                await self.projects.store.update_task(task['id'], status='running')
            else:
                task = await self.projects.resume(project_id, task['id'], args.message)
        else:
            task = await self.projects.start(project_id, request_key=args.request_key or 'conversation-' + str(uuid4()),
                workflow_id=args.workflow_id or project_id, inputs=args.inputs, message=args.message,
                purpose='customer_trial' if args.action == 'trial' else 'business', item_id=item['id'] if item else '',
                feedback_task_id=args.feedback_task_id or state.get('conversation_context', {}).get('task_id', ''))
        state = self.manager.load(project_id)
        state.update(phase='operate', active_item_id=(item or {}).get('id', task.get('item_id', '')),
                     project_task_id=task['id'])
        self.manager.save(project_id, state)
        self.manager.track_project_task(project_id, task['id'])
        if item:
            latest = await self.item(project_id, item['id'])
            await self.patch_item(project_id, item['id'], task_ids=list(dict.fromkeys([*latest['task_ids'], task['id']]))[-200:])
        worker = self.projects.active.get(task['id'])
        if worker:
            await asyncio.shield(worker)
        return await self.projects.task(project_id, task['id'])

    async def ready(self, project_id: str) -> list[dict]:
        progress = await self.projects.store.progress(project_id)
        return [i for i in progress['value']['items'] if i['status'] in {'planned', 'working'}
                and not i['blocker'] and not any(not q['answer'] for q in i['questions'])]

    async def pause(self, project_id: str, reason: str, *, failed: bool = False) -> None:
        state = self.manager.load(project_id)
        item_id = state.get('active_item_id')
        if not item_id:
            return
        progress = await self.projects.store.progress(project_id)
        item = next((i for i in progress['value']['items'] if i['id'] == item_id), None)
        if item is None:
            return
        if item['status'] not in {'working', 'planned'}:
            return
        changes = {'status': 'paused', 'next_action': item['next_action'] or '继续处理并检查已有结果'}
        if failed:
            changes.update(status='waiting', blocker={'kind': 'runtime', 'owner': '平台维护者',
                'reason': reason, 'next_action': '检查实际错误并修复后，在此事项继续；原结果和业务任务保留'})
        await self.patch_item(project_id, item_id, **changes)

    async def topology(self, project_id: str) -> dict:
        frozen = await self.projects.freeze(project_id)
        project = await self.projects.store.get(project_id)
        calls, flows = [], {}
        for workflow_id, draft in frozen.items():
            graph = draft['snapshot']['workflow']
            nodes = []
            for node in graph['nodes']:
                target = node.get('config', {}).get('tool_name', '')
                member_id = target.removeprefix('workflow:') if isinstance(target, str) and target.startswith('workflow:') else ''
                nodes.append({'id': node['id'], 'type': node['type'], 'title': node.get('title') or node['id'],
                              'workflow_id': member_id,
                              'branches': node.get('config', {}).get('cases', []) if node['type'] == 'if_else' else [],
                              'default_branch': node.get('config', {}).get('default_branch', '')})
                if isinstance(target, str) and target.startswith('workflow:'):
                    calls.append({'source': workflow_id, 'target': target.removeprefix('workflow:'),
                                  'node_id': node['id'], 'label': node.get('title', node['id'])})
            flows[workflow_id] = {'revision': draft['revision'], 'nodes': nodes,
                                 'edges': [{'source': e['source'], 'target': e['target'], 'branch': e.get('branch')}
                                           for e in graph['edges']]}
        return {'members': project['members'], 'calls': calls, 'flows': flows}
