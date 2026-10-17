"""Agent exports use the exact bound trial and work through normal file downloads."""
import io
import json
import zipfile

from tests.test_modeling import modeling, real_compute, setup, wait_task  # noqa: F401
from tests.test_projects import configured, start  # noqa: F401


def export(client, base, model_ref):
    return client.post(base + '/agent-tools', json={'name': 'project_models', 'arguments': {
        'action': 'export', 'model_ref': model_ref}})


def test_agent_export_downloads_bound_model_and_generated_report_without_training(real_compute, monkeypatch):
    (client, app, project, settings), service = real_compute
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1)
    task = wait_task(client, base, start(client, base, 'train'))
    assert task['status'] == 'succeeded', task
    assert client.put(base + '/models/quality', json={'name': '质量', 'study_id': study['id'],
        'candidate_id': candidate['id'], 'slot': 0}).status_code == 200
    before = client.get(base + '/tasks').json()

    async def forbidden(*args, **kwargs):
        raise AssertionError('Export must not run training, prediction or holdout')
    monkeypatch.setattr(service, 'compute', forbidden)
    result = export(client, base, 'quality')
    assert result.status_code == 200, result.text
    result = result.json()
    assert result['model_ref'] == 'quality' and result['revision'] == 1
    assert (result['study_id'], result['candidate_id'], result['slot']) == (study['id'], candidate['id'], 0)
    assert result['split']['development_samples'] == 48
    assert result['split']['holdout_samples'] == 12 and result['split']['holdout_status'] == 'not_evaluated'
    assert result['test_result'] is None
    assert client.get(base + '/tasks').json() == before
    files = {}
    for artifact in result['artifacts']:
        path = artifact['file_path']
        downloaded = client.get(f'/api/v1/applications/{project["id"]}/workspace/files/{path}')
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == (settings.workspace_root / project['id'] / path).read_bytes()
        files[path.rsplit('/', 1)[-1]] = downloaded.content
    report = files['model-report.md'].decode()
    assert study['id'] in report and candidate['id'] in report
    assert '保留测试：12' in report and '没有已保存的最终测试结果' in report
    facts = json.loads(files['model-report.json'])
    assert facts['trial']['metrics'] == result['trial']['metrics']
    assert facts['split'] == result['split']
    with zipfile.ZipFile(io.BytesIO(files['prediction-model.zip'])) as archive:
        config = json.loads(archive.read('configuration.json'))
        assert config['selected_trial_slot'] == 0
        assert config['candidate']['id'] == candidate['id']
        assert 'model/model.joblib' in archive.namelist()
        assert 'predict.example.json' in archive.namelist()
    # Rebinding the name cannot rewrite an earlier exported model or report.
    assert client.put(base + '/models/quality', json={'name': '待绑定', 'expected_revision': 1}).status_code == 200
    assert export(client, base, 'quality').status_code == 422
    for artifact in result['artifacts']:
        path = artifact['file_path']
        assert (settings.workspace_root / project['id'] / path).read_bytes() == files[path.rsplit('/', 1)[-1]]
    other = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    assert export(client, '/api/v1/projects/' + other, 'quality').status_code == 422


def test_unbound_export_does_not_create_results(configured):
    client, app, project, settings = configured
    base = '/api/v1/projects/' + project['id']
    client.put(base + '/models/later', json={'name': '稍后训练'})
    assert export(client, base, 'later').status_code == 422
    assert not (settings.workspace_root / project['id'] / 'results/models').exists()
