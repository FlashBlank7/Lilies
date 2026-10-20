"""File choices identify saved related runs without inventing file ownership."""
from tests.test_projects import configured, edge, graph, node, ref, settled, start  # noqa: F401


def workflow(client, project_id, name):
    workflow_id = client.post(f'/api/v1/projects/{project_id}/members', json={'name': name}).json()['id']
    graph(client, workflow_id, [node('start', 'start', inputs=[
        {'name': 'source_path', 'label': '输入表格', 'type': 'string'},
        {'name': 'method_path', 'label': '沿用方法', 'type': 'file'},
        {'name': 'output_path', 'type': 'string'},
        {'name': 'invalid_policy', 'label': '转换失败处理', 'type': 'string', 'options': ['保留', '排除']},
        {'name': 'api_token', 'type': 'string'},
    ]), node('end', 'end', outputs={
        'artifacts': [{'file_path': ref('start', 'output_path')}],
        'source_path': ref('start', 'source_path'),
    })], [edge('start', 'end')])
    return workflow_id


def run(client, project_id, workflow_id, key, output_path, policy='保留', **inputs):
    base = f'/api/v1/projects/{project_id}'
    return settled(client, base, start(client, base, key, workflow_id=workflow_id, inputs={
        'source_path': 'requirement-package/new-samples.csv',
        'method_path': 'results/previous/method.json',
        'output_path': output_path, 'invalid_policy': policy, 'api_token': 'do-not-show', **inputs,
    }))


def listings(client, project_id):
    files = client.get(f'/api/v1/applications/{project_id}/workspace/files').json()
    space = client.get(f'/api/v1/projects/{project_id}/space').json()['files']
    assert {f['path']: f.get('related_run') for f in files} == {f['path']: f.get('related_run') for f in space}
    return {f['path']: f for f in files}


def write_files(app, project_id, *paths):
    for path in paths:
        file = app.state.services.projects.workspace(project_id) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('unchanged bytes')


def test_file_lists_show_saved_workflow_and_inputs_for_explicit_output_paths(configured):
    client, app, project, _ = configured
    project_id = project['id']
    normal = workflow(client, project_id, '默认样本清洗')
    strict = workflow(client, project_id, '严格样本清洗')
    first, second = 'results/first/samples.csv', 'results/second/samples.csv'
    source, unrelated = 'requirement-package/new-samples.csv', 'results/old/samples.csv'
    write_files(app, project_id, first, second, source, unrelated)
    drafts = {wid: client.get(f'/api/v1/applications/{wid}/draft').json() for wid in (normal, strict)}
    tasks = [run(client, project_id, normal, 'normal', first), run(client, project_id, strict, 'strict', second, '排除')]
    assert all(task['status'] == 'succeeded' for task in tasks)

    files = listings(client, project_id)
    for path, task, wid, name, policy in zip((first, second), tasks, (normal, strict), ('默认样本清洗', '严格样本清洗'), ('保留', '排除')):
        related = files[path]['related_run']
        assert related['task_id'] == task['id']
        assert related['workflow_id'] == wid and related['workflow_name'] == name
        assert related['run_id'] and related['created_at']
        values = {field['name']: field['value'] for field in related['file_parameters'] + related['input_parameters']}
        assert values['source_path'] == 'new-samples.csv'
        assert values['method_path'] == 'method.json'
        assert values['invalid_policy'] == policy
        assert 'api_token' not in values
        assert (app.state.services.projects.workspace(project_id) / path).read_text() == 'unchanged bytes'
        assert client.get(f'/api/v1/applications/{wid}/draft').json() == drafts[wid]
    # An input/source_path value or a matching filename is not artifact evidence.
    assert 'related_run' not in files[source] and 'related_run' not in files[unrelated]


def test_reused_paths_use_latest_related_run_and_never_cross_projects(configured, monkeypatch):
    client, app, project, _ = configured
    project_id = project['id']
    local = workflow(client, project_id, '本项目清洗')
    reused, old, foreign = 'results/shared/method.json', 'results/old/method.json', 'results/foreign/method.json'
    write_files(app, project_id, reused, old, foreign)
    first = run(client, project_id, local, 'first', reused)
    run(client, project_id, local, 'old', old)
    latest = run(client, project_id, local, 'latest', reused, '排除', source_path='/private/hidden.csv', method_path='../hidden.json')
    other = client.post('/api/v1/projects', json={'name': '其他项目'}).json()['id']
    other_workflow = workflow(client, other, '其他项目清洗')
    run(client, other, other_workflow, 'foreign', foreign)

    files = listings(client, project_id)
    related = files[reused]['related_run']
    assert related['task_id'] == latest['id'] != first['id']
    assert not {'source_path', 'method_path'} & {field['name'] for field in related['file_parameters']}
    assert 'related_run' not in files[foreign]
    assert 'related_run' in files[old]
    monkeypatch.setattr('agent_platform.project_store._FILE_RUN_LIMIT', 1)
    bounded = listings(client, project_id)
    assert bounded[reused]['related_run']['task_id'] == latest['id']
    assert 'related_run' not in bounded[old]


def test_oversized_outputs_keep_original_file_listing(configured, monkeypatch):
    client, app, project, _ = configured
    project_id = project['id']
    local = workflow(client, project_id, '清洗')
    path = 'results/one/samples.csv'
    write_files(app, project_id, path)
    assert run(client, project_id, local, 'one', path)['status'] == 'succeeded'
    monkeypatch.setattr('agent_platform.project_store._FILE_OUTPUT_LIMIT', 2)
    assert 'related_run' not in listings(client, project_id)[path]
