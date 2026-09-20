"""Submit a candidate through an existing editable workflow and project task."""
from __future__ import annotations

from .project_store import ProjectConflict, encode
from .workflow_models import ApplicationSnapshot


def validate_training_workflow(services, snapshots, workflow_id, inputs):
    """Validate the exact frozen graph; never repair or replace a user's draft."""
    if workflow_id not in snapshots:
        raise ValueError('只能运行当前项目的成员工作流')
    graph = ApplicationSnapshot.model_validate(snapshots[workflow_id]['snapshot']).workflow
    errors = services.blocks.validate_workflow(graph)
    if errors:
        raise ValueError('训练流程无效：' + '; '.join(errors))
    starts = [n for n in graph.nodes if n.type == 'start']
    fields = {f.name: f for n in starts for f in services.blocks.validate_node(n).inputs}
    for key in ('study_id', 'candidate_id'):
        if key not in fields or fields[key].type.value != 'string':
            raise ValueError(f'训练流程需要声明字符串输入 {key}，请先通过 workflow_draft 配置')
    for field in fields.values():
        value = inputs.get(field.name, field.default)
        if field.required and value is None:
            raise ValueError(f'成员输入缺少必填字段：{field.name}')
        if value is not None and (not services.workflow_runtime._matches_type(value, field.type.value)
                or (field.type.value == 'number' and isinstance(value, bool))):
            raise ValueError(f'成员输入 {field.name} 类型不匹配，要求 {field.type.value}')
    trains = [n for n in graph.nodes if n.type == 'model_train']
    if len(trains) != 1:
        raise ValueError('submit_and_run 需要当前画布包含一处 model_train 节点；复杂流程请用原工作流运行入口')
    for key in ('study_id', 'candidate_id'):
        if trains[0].config.get(key) != {'$ref': {'node_id': '$inputs', 'path': [key]}}:
            raise ValueError(f'训练节点 {key} 必须引用 $inputs.{key}，请先修改该节点')

    # Include reachable members and embedded graphs so a convenience training
    # call cannot silently evaluate holdout or launch another candidate.
    visited, count = set(), 0
    def walk(value):
        nonlocal count
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get('type') == 'model_train':
                count += 1
                if value.get('config', {}).get('finalize'):
                    raise ValueError('submit_and_run 不执行最终留出集评价，请关闭 finalize')
            target = value.get('tool_name')
            if isinstance(target, str) and target.startswith('workflow:'):
                member = target[9:]
                if member not in snapshots:
                    raise ValueError('引用的成员不属于当前项目')
                visit(member)
            for item in value.values():
                walk(item)
    def visit(member):
        if member not in visited:
            visited.add(member)
            walk(snapshots[member]['snapshot']['workflow'])
    visit(workflow_id)
    if count != 1:
        raise ValueError('submit_and_run 每次只运行一个候选，不能包含其他训练节点')


async def submit_and_start(services, project_id, args, item_id=''):
    projects, modeling = services.projects, services.modeling
    await projects.member(project_id, args.workflow_id)
    study = await modeling.get(project_id, 'study', args.study_id)
    # A retry must find the old task even if the draft has since been edited.
    previous = await modeling.duplicate(project_id, 'candidate', args.study_id, args.candidate.model_dump())
    task = await projects.store.task_for_request(project_id, 'modeling/' + previous['id']) if previous else None
    if task:
        saved = previous.get('submission', {})
        if saved.get('workflow_id') != args.workflow_id or encode(saved.get('inputs')) != encode(args.inputs):
            raise ProjectConflict('此候选已用于不同工作流或输入；请使用新的候选请求标识')
        return await projects.task(project_id, task['id'])
    if not task:
        validate_training_workflow(services, await projects.freeze(project_id), args.workflow_id,
                                   {**args.inputs, 'study_id': args.study_id, 'candidate_id': 'pending'})
    candidate = await modeling.candidate(project_id, args.study_id, args.candidate,
        submission={'workflow_id': args.workflow_id, 'inputs': args.inputs, 'item_id': study.get('item_id') or item_id})
    inputs = {**args.inputs, 'study_id': args.study_id, 'candidate_id': candidate['id']}
    task = await projects.start(project_id, request_key='modeling/' + candidate['id'],
        workflow_id=args.workflow_id, inputs=inputs, purpose='build_test',
        item_id=candidate['submission']['item_id'], feedback_task_id=candidate.get('feedback_task_id', ''),
        validate_snapshots=lambda frozen: validate_training_workflow(services, frozen, args.workflow_id, inputs))
    return task
