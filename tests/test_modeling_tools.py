"""The composed agent operation uses editable graphs and real project tasks."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import time

import pytest

from agent_platform.modeling_models import ModelingTool
from agent_platform.modeling_summary import study_summary, candidate_summary, note_summary, profile_summary
from agent_platform.project_agent_tools import WorkspaceProjectTools, project_tool_specs
from tests.test_modeling import modeling, real_compute, setup, wait_task  # noqa: F401
from tests.test_projects import configured, graph, node, edge, ref  # noqa: F401
from tests.test_project_tool_help import prepare


def call(client, base, **args):
    return client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': args})


def input_graph(client, workflow):
    fields = [{'name': k, 'type': 'string', 'required': True} for k in ('study_id', 'candidate_id')]
    graph(client, workflow, [node('start', 'start', inputs=fields),
        node('train', 'model_train', study_id=ref('$inputs', 'study_id'), candidate_id=ref('$inputs', 'candidate_id')),
        node('end', 'end', outputs={'model': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])


def prepared(fixture):
    config, service = fixture
    client, app, project, settings = config
    prepare(config)
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1)
    input_graph(client, project['id'])
    args = {'action': 'submit_and_run', 'study_id': study['id'], 'workflow_id': project['id'],
            'candidate': {'request_key': 'composed', 'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}}
    return client, app, project, base, study, args, service


def test_summary_keeps_diagnostics_and_full_records_are_unmodified():
    trial = {'slot': 0, 'model': 'svm', 'status': 'completed', 'metrics': {'mae': .3}, 'baseline': {'mae': .5},
        'fold_metrics': [{'mae': .4}], 'diagnostics': [{'constant_prediction': True}], 'warnings': ['常数预测'],
        'group_errors': [{'group': str(i), 'error': 10-i} for i in range(10)],
        'effective_model': {'many': list(range(1000))}, 'prediction_preview': [1]*100, 'seconds': 1}
    candidate = {'id': 'c', 'study_id': 's', 'trials': [trial], 'features': {'columns': ['x']}, 'hypothesis': '试验窗口',
        'error': '失败原因', 'task_id': 't', 'parent_id': 'old', 'parameters': {'large': 'x'*4000}}
    before = deepcopy(candidate); result = candidate_summary(candidate)
    assert result['trials'][0]['diagnostics'] == trial['diagnostics']
    assert result['trials'][0]['warnings'] == ['常数预测']
    assert result['trials'][0]['group_errors_omitted'] == 5
    assert result['error'] == '失败原因' and result['task_id'] == 't'
    assert len(json.dumps(result)) < len(json.dumps(candidate)) / 2
    assert candidate == before
    study = {'id': 's', 'evaluation': {'metric': 'macro_f1'}, 'budget': {'seconds': 60, 'trials': 10},
        'used_seconds': 15, 'trials_used': 2, 'best': {'score': .8}, 'baseline': {'macro_f1': .4},
        'split': {'samples': list(range(1000)), 'evaluation_label': 'validation'}}
    result = study_summary(study)
    assert result['metric_direction'] == 'maximize'
    assert result['remaining_budget'] == {'seconds': 45, 'trials': 8}
    assert 'samples' not in result['split'] and len(study['split']['samples']) == 1000
    profile = {'sampled': True, 'rows': 10, 'columns': [{'name': 'x', 'missing': 2, 'distribution': [1]*50}], 'preview': [1]*100}
    assert profile_summary(profile)['columns'] == [{'name': 'x', 'missing': 2}]


def test_tool_help_and_default_summary_with_full_and_http_compatibility(modeling):
    client, app, project, base, study, args, service = prepared(modeling)
    short = call(client, base, action='read_study', study_id=study['id']).json()
    full = call(client, base, action='read_study', study_id=study['id'], view='full').json()
    assert short['view'] == 'summary' and 'request' not in short
    assert full == client.get(base+'/modeling/studies/'+study['id']).json()
    assert short['best'] == full['best'] and 'remaining_budget' in short
    spec = next(s for s in project_tool_specs() if s['name'] == 'project_modeling')
    help = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'project_modeling'}}).json()
    assert len(spec['description']) < len(help['description']) / 3
    example = help['workflow_example']
    graph(client, project['id'], example['nodes'], example['edges'])
    example_args = next(e for e in help['examples'] if e['action']=='submit_and_run')
    assert ModelingTool.model_validate(example_args).wait is True


@pytest.mark.parametrize('change,match', [
    ('literal', '必须引用'), ('finalize', '留出集'), ('missing', '声明字符串'),
    ('extra_input', '必填字段'), ('type', '类型不匹配'), ('reserved', '由平台绑定'),
    ('foreign', '当前项目'), ('phase', '绑定建设事项'), ('nested_finalize', '留出集')])
def test_preflight_rejects_invalid_inputs_and_graphs_without_creating_candidate(modeling, change, match):
    client, app, project, base, study, args, service = prepared(modeling)
    draft = client.get('/api/v1/applications/'+project['id']+'/draft').json()['snapshot']['workflow']
    if change == 'literal':
        draft['nodes'][1]['config']['candidate_id'] = 'old'
    elif change == 'finalize':
        draft['nodes'][1]['config']['finalize'] = True
    elif change == 'missing':
        draft['nodes'][0]['config']['inputs'] = []
    elif change in {'extra_input', 'type'}:
        draft['nodes'][0]['config']['inputs'].append({'name': 'amount', 'type': 'number', 'required': True})
        if change == 'type': args['inputs'] = {'amount': True}
    elif change == 'reserved': args['inputs'] = {'candidate_id': 'injected'}
    elif change == 'foreign':
        args['workflow_id'] = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    elif change == 'phase':
        manager = app.state.services.local_agents; state = manager.load(project['id'])
        state['phase'] = 'operate'; manager.save(project['id'], state)
    elif change == 'nested_finalize':
        member = client.post(base+'/members', json={'name': 'nested', 'purpose': 'test'}).json()['id']
        graph(client, member, [node('start','start'), node('final','model_train',study_id=study['id'],finalize=True),
              node('end','end')], [edge('start','final'),edge('final','end')])
        draft['nodes'].insert(2, node('nested','tool',tool_name='workflow:'+member,input={}))
        draft['edges'] = [edge('start','train'),edge('train','nested'),edge('nested','end')]
    if change not in {'reserved', 'foreign', 'phase'}:
        graph(client, project['id'], draft['nodes'], draft['edges'])
    response = call(client, base, **args)
    assert response.status_code == 422, response.text
    assert match in response.text
    assert len(client.get(base+'/modeling/studies/'+study['id']+'/candidates').json()) == 1
    assert client.get(base+'/tasks').json() == []


def test_real_composed_training_feedback_and_idempotent_results(real_compute):
    client, app, project, base, study, args, service = prepared(real_compute)
    before = client.get('/api/v1/applications/'+project['id']+'/draft').json()
    result = call(client, base, **args)
    assert result.status_code == 200, result.text
    value = result.json(); assert value['status'] == 'succeeded', value
    trial = value['candidate']['trials'][0]
    assert trial['status'] == 'completed' and 'mae' in trial['metrics']
    assert value['task']['purpose'] == 'build_test' and value['task']['item_id'] == 'quantity'
    assert value['task']['runs'][0]['draft_revision'] == before['revision']
    assert value['candidate']['task_id'] == value['project_task_id']
    assert client.get('/api/v1/applications/'+project['id']+'/draft').json() == before
    note_args = {'action': 'training_note', 'study_id': study['id'], 'candidate_id': value['candidate_id'], 'slot': 0}
    note = call(client, base, **note_args).json()
    full = call(client, base, **note_args, view='full').json()
    assert note['trial']['metrics'] == full['trial']['metrics']
    assert note['split']['folds'] == len(full['split']['folds'])
    assert 'effective_model' in full['trial'] and 'effective_model' not in note['trial']
    assert 'test_result' not in value['study']
    # A repeat after the user replaces the graph returns the old frozen task.
    graph(client, project['id'], [node('start','start'),node('end','end',outputs={'edited': True})], [edge('start','end')])
    duplicate = call(client, base, **args).json()
    assert duplicate['project_task_id'] == value['project_task_id']
    assert duplicate['candidate']['trials'] == value['candidate']['trials']
    assert call(client, base, **{**args, 'inputs': {'changed': 1}}).status_code == 409
    assert call(client, base, **{**args, 'candidate': {**args['candidate'], 'hypothesis': 'different'}}).status_code == 409
    # New feedback creates a new task but uses the same input-driven graph.
    input_graph(client, project['id'])
    new = call(client, base, **{**args, 'candidate': {**args['candidate'], 'request_key': 'feedback',
        'parent_id': value['candidate_id'], 'feedback_task_id': value['project_task_id']}}).json()
    assert new['status'] == 'succeeded' and new['project_task_id'] != value['project_task_id']
    assert new['task']['feedback_task_id'] == value['project_task_id']
    assert new['candidate']['trials'][0]['metrics'] == trial['metrics']


def test_async_duplicate_stop_resume_and_frozen_draft(real_compute, monkeypatch):
    client, app, project, base, study, args, service = prepared(real_compute)
    original = service.compute
    entered = False
    async def slow(*a, **kw):
        nonlocal entered
        entered = True
        await asyncio.sleep(30)
        return await original(*a, **kw)
    monkeypatch.setattr(service, 'compute', slow)
    result = call(client, base, **args, wait=False).json()
    deadline = time.monotonic()+5
    while not entered and time.monotonic()<deadline: time.sleep(.01)
    assert entered
    started = time.monotonic()
    repeated = call(client, base, **args, wait=False).json()
    assert time.monotonic()-started < 5
    assert repeated['project_task_id'] == result['project_task_id']
    tid = result['project_task_id']
    graph(client, project['id'], [node('start','start'),node('end','end',outputs={'wrong_version': True})], [edge('start','end')])
    assert client.post(base+'/tasks/'+tid+'/stop', json={}).status_code == 200
    assert call(client, base, **args, wait=False).json()['status'] == 'interrupted'
    # Actual modeling service restart leaves the task dormant.
    asyncio.run(service.close()); asyncio.run(service.initialize())
    assert client.get(base+'/tasks/'+tid).json()['status'] == 'interrupted'
    monkeypatch.setattr(service, 'compute', original)
    assert client.post(base+'/tasks/'+tid+'/resume', json={}).status_code == 202
    finished = wait_task(client, base, {'id': tid})
    assert finished['status'] == 'succeeded', finished
    assert 'wrong_version' not in finished['outputs']
    candidate = client.get(base+'/modeling/studies/'+study['id']+'/candidates/'+result['candidate_id']).json()
    assert len(candidate['trials']) == 1


def test_cancel_during_task_creation_still_stops_the_created_task(modeling, monkeypatch):
    client, app, project, base, study, args, service = prepared(modeling)
    async def run():
        manager = app.state.services.local_agents
        tools = WorkspaceProjectTools(app.state.services, project['id'], manager)
        started, release = asyncio.Event(), asyncio.Event()
        async def creation():
            started.set(); await release.wait()
            # No computation needed to verify cancellation of task creation.
            return {'id': 'created'}
        stopped = []
        async def stop(pid, tid): stopped.append((pid, tid))
        monkeypatch.setattr(app.state.services.projects, 'stop', stop)
        job = asyncio.create_task(tools.run_build_task(creation()))
        await started.wait(); job.cancel(); release.set()
        with pytest.raises(asyncio.CancelledError): await job
        assert stopped == [(project['id'], 'created')]
        assert 'created' in manager.project_test_tasks[project['id']]
    asyncio.run(run())


def test_saved_candidate_retries_after_launch_connection_failure(real_compute, monkeypatch):
    client, app, project, base, study, args, service = prepared(real_compute)
    original = app.state.services.projects.start
    async def unavailable(*a, **kw):
        raise ValueError('连接暂时中断')
    monkeypatch.setattr(app.state.services.projects, 'start', unavailable)
    assert call(client, base, **args).status_code == 422
    pending = call(client, base, action='candidates', study_id=study['id']).json()
    candidate = next(c for c in pending if c['hypothesis'] == '建立初步模型并比较效果' and c['engine'] == 'sklearn')
    assert candidate['status'] == 'ready'
    monkeypatch.setattr(app.state.services.projects, 'start', original)
    done = call(client, base, **args).json()
    assert done['candidate_id'] == candidate['id'] and done['status'] == 'succeeded'
    assert len(done['candidate']['trials']) == 1
    assert len(client.get(base+'/tasks').json()) == 1


def test_rechecks_actual_snapshot_if_draft_changes_after_preflight(modeling, monkeypatch):
    client, app, project, base, study, args, service = prepared(modeling)
    original = service.candidate
    async def change_after_preflight(*a, **kw):
        candidate = await original(*a, **kw)
        # Simulate a concurrent editor commit between preflight and task freeze.
        draft = await app.state.services.workflow_store.get_draft(project['id'])
        snapshot = draft['snapshot'].model_copy(deep=True)
        next(n for n in snapshot.workflow.nodes if n.type=='model_train').config['finalize'] = True
        await app.state.services.workflow_store.save_draft(project['id'], snapshot, expected_revision=draft['revision'],
                                                         idempotency_key='concurrent-finalize')
        return candidate
    monkeypatch.setattr(service, 'candidate', change_after_preflight)
    response = call(client, base, **args)
    assert response.status_code == 422 and '留出集' in response.text
    assert client.get(base+'/tasks').json() == []


def test_real_legacy_and_composed_benchmark(real_compute):
    client, app, project, base, study, args, service = prepared(real_compute)
    def size(value): return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
    measured = {}
    def tool(name, arguments, mode):
        began = time.monotonic()
        r = client.post(base+'/agent-tools', json={'name': name, 'arguments': arguments})
        assert r.status_code == 200, r.text
        v = r.json()
        measured.setdefault(mode, []).append({'tool': name, 'action': arguments.get('action'),
            'seconds': round(time.monotonic()-began, 6), 'input_bytes': size(arguments), 'output_bytes': size(v)})
        return v
    began = time.monotonic()
    warm = tool('project_modeling', {**args, 'candidate': {**args['candidate'], 'request_key':'warmup'}}, 'warmup')
    cold = time.monotonic()-began
    legacy_member = client.post(base+'/members', json={'name':'legacy benchmark', 'purpose':'test'}).json()['id']
    input_graph(client, legacy_member)
    draft = client.get('/api/v1/applications/'+legacy_member+'/draft').json()
    # The former path returns full candidates/notes and edits two literal IDs.
    began = time.monotonic()
    old = tool('project_modeling', {'action':'submit_candidate', 'view':'full', 'study_id':study['id'],
        'candidate':{**args['candidate'], 'request_key':'legacy'}}, 'legacy')
    tool('workflow_draft', {'workflow_id':legacy_member, 'operation': {'op':'update_node',
        'expected_revision':draft['revision'], 'idempotency_key':'bind-legacy', 'data': {'node_id':'train',
        'changes':{'config':{'study_id':study['id'], 'candidate_id':old['id']}}, 'merge_config':True}}}, 'legacy')
    old_task = tool('workflow_run', {'action':'start', 'workflow_id':legacy_member,
        'inputs':{'study_id':study['id'], 'candidate_id':old['id']}, 'request_key':'legacy'}, 'legacy')
    old_note = tool('project_modeling', {'action':'training_note', 'study_id':study['id'], 'candidate_id':old['id'],
                                        'slot':0, 'view':'full'}, 'legacy')
    legacy_elapsed = time.monotonic()-began
    began = time.monotonic()
    new = tool('project_modeling', {**args, 'candidate':{**args['candidate'], 'request_key':'new'}}, 'composed')
    new_note = tool('project_modeling', {'action':'training_note', 'study_id':study['id'], 'candidate_id':new['candidate_id'], 'slot':0}, 'composed')
    composed_elapsed = time.monotonic()-began
    assert old_task['status'] == new['status'] == 'succeeded'
    assert old_note['trial']['metrics'] == new_note['trial']['metrics']
    assert len(measured['legacy']) == 4 and len(measured['composed']) == 2
    assert sum(x['output_bytes'] for x in measured['composed']) < sum(x['output_bytes'] for x in measured['legacy'])
    if path := os.environ.get('MODEL_TOOL_BENCHMARK_OUTPUT'):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({'scope':'Fixed constructed data; actual workflow runner and real ML; no agent latency. Same study, fixed split, same sklearn model/config, caches warmed for both paths.',
            'study_id':study['id'], 'image':study['image'], 'cold_first_candidate_seconds':cold,
            'legacy_seconds':legacy_elapsed, 'composed_seconds':composed_elapsed, 'calls':measured,
            'legacy_metrics':old_note['trial']['metrics'], 'composed_metrics':new_note['trial']['metrics'],
            'legacy_compute_seconds':old_note['trial']['seconds'], 'composed_compute_seconds':new_note['trial']['seconds'],
            'cold_definition':'Existing local image; first split/feature/worker startup. Image download/build excluded.'},ensure_ascii=False,indent=2))
