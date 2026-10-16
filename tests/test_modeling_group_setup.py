"""Known split mistakes are actionable before launching training candidates."""
import pytest

from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_projects import configured  # noqa: F401


def dataset(config):
    client, app, project, settings = config
    source = settings.workspace_root / project['id'] / 'requirement-package/data.csv'
    source.parent.mkdir(exist_ok=True)
    source.write_text('chip,x,y\n' + '\n'.join(f'{i // 2:03d},{i},{i * 2}' for i in range(24))
                      + '\n000,100,\n,101,\n')
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/datasets', json={'source_path': 'requirement-package/data.csv',
        'mapping': {'target': 'y', 'group_column': 'chip'}})
    assert response.status_code == 201, response.text
    return base, response.json()


def test_complete_profile_rejects_random_before_study_and_group_retry_trains(real_compute, monkeypatch):
    config, service = real_compute
    client, app, project, settings = config
    base, data = dataset(config)
    response = client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': {
        'action': 'profile', 'dataset_id': data['id']}})
    assert response.status_code == 200, response.text
    assert response.json()['sample_structure']['rows_in_repeated_groups'] == 24
    request = {'dataset_id': data['id'], 'request_key': 'group-check',
        'evaluation': {'split': 'random', 'folds': 2}, 'budget': {'seconds': 120, 'trials': 1, 'trial_seconds': 30}}
    async def no_runtime(*args, **kwargs):
        raise AssertionError('Known invalid settings must be rejected without starting a runtime')
    with monkeypatch.context() as patch:
        patch.setattr(service, 'image', no_runtime)
        response = client.post(base + '/modeling/studies', json=request)
    assert response.status_code == 422, response.text
    assert 'chip' in response.text and '12 个重复组' in response.text and '24 行' in response.text
    assert 'evaluation.split' in response.text
    assert client.get(base + '/modeling/studies').json() == []
    assert client.get(base + '/tasks').json() == []
    request['evaluation']['split'] = 'group'
    response = client.post(base + '/modeling/studies', json=request)
    assert response.status_code == 201, response.text
    study = response.json()
    assert client.post(base + '/modeling/studies', json=request).json()['id'] == study['id']
    response = client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': {
        'action': 'train', 'study_id': study['id'], 'candidate': {
            'request_key': 'linear', 'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}}})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'succeeded', response.json()
    saved = client.get(base + '/modeling/studies/' + study['id']).json()
    assert saved['trials_used'] == 1 and saved['best']['metrics']['mae'] < saved['baseline']['mae'] / 4
    assert saved['split']['missing_labels'] == 2
    for train, valid in saved['split']['folds']:
        assert not {i // 2 for i in train} & {i // 2 for i in valid}


@pytest.mark.parametrize('profile_mode', ['none', 'preview', 'legacy'])
def test_unscanned_preview_and_legacy_data_keep_worker_validation(real_compute, profile_mode):
    config, service = real_compute
    client, app, project, settings = config
    base, data = dataset(config)
    if profile_mode == 'preview':
        response = client.post(base + '/datasets/' + data['id'] + '/profile?sampled=true')
        assert response.status_code == 200, response.text
    elif profile_mode == 'legacy':
        # Historical full profiles did not record labeled group counts.
        import asyncio
        data['profile'] = {'sampled': False, 'rows': 26, 'group_column_count': 12}
        asyncio.run(service.put(project['id'], 'dataset', data))
    response = client.post(base + '/modeling/studies', json={
        'dataset_id': data['id'], 'request_key': 'legacy', 'evaluation': {'split': 'random'}})
    assert response.status_code == 201, response.text
    response = client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': {
        'action': 'train', 'study_id': response.json()['id'], 'wait': False, 'candidate': {
            'request_key': 'invalid-split', 'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}}})
    assert response.status_code == 200, response.text
    task = wait_task(client, base, response.json())
    assert task['status'] == 'failed' and '同组重复' in task['error']
