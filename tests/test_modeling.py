"""Project APIs and actual workflow execution; the local adapter runs real ML.

MODEL_TEST_PYTHON=/path/to/locked/env/bin/python selects a real-library worker.
MODEL_TEST_DOCKER=1 validates the production container transport instead.
"""
import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import time
import zipfile

import pytest
from agent_platform import modeling_worker
from tests.test_projects import configured as configured  # noqa: F401
from tests.test_projects import graph, node, edge, ref, start


@pytest.fixture
def modeling(configured, monkeypatch):
    client, app, project, settings = configured
    service = app.state.services.modeling
    async def image():
        return 'test-real-worker-environment'
    if not os.environ.get('MODEL_TEST_DOCKER'):
        monkeypatch.setattr(service, 'image', image)
        async def snapshot_environment(image):
            folder = settings.data_dir / 'test-environment'; folder.mkdir(exist_ok=True)
            (folder / 'worker.py').write_text(Path(modeling_worker.__file__).read_text())
            (folder / 'requirements.lock').write_text('Real worker dependencies installed in MODEL_TEST_PYTHON')
            (folder / 'environment.json').write_text(json.dumps({'image': image}))
            return folder
        monkeypatch.setattr(service, 'snapshot_environment', snapshot_environment)
    yield configured, service


@pytest.fixture
def real_compute(modeling, monkeypatch):
    configured, service = modeling
    if os.environ.get('MODEL_TEST_DOCKER'):
        return configured, service
    executable = os.environ.get('MODEL_TEST_PYTHON')
    if not executable and importlib.util.find_spec('optuna'):
        executable = sys.executable
    if not executable:
        pytest.skip('Set MODEL_TEST_PYTHON or MODEL_TEST_DOCKER for real training checks')

    async def compute(project_id, dataset, config, folder, *, image='', on_event=None, timeout=1800, extra_mounts=None, writable_mounts=None):
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / 'output'; output.mkdir(exist_ok=True)
        replacements = {target: str(host) for host, target in (extra_mounts or []) + (writable_mounts or [])}
        replacements.update({target + '/optuna.db': str(host / 'optuna.db') for host, target in writable_mounts or []})
        config = {k: replacements.get(v, v) if isinstance(v, str) else v for k, v in config.items()}
        config.update(mapping=dataset['mapping'], output=str(output), **{k: str(service.path(project_id, dataset['id']) / v['name']) for k, v in dataset['files'].items()})
        path = folder / 'job.json'; path.write_text(json.dumps(config))
        env = {**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
        async with service.slots:
            process = await asyncio.create_subprocess_exec(executable, modeling_worker.__file__, str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
            service.test_pid = process.pid
            logs = asyncio.create_task(process.stderr.read())
            result = None; failure = ''
            try:
                async with asyncio.timeout(timeout):
                    while line := await process.stdout.readline():
                        event = json.loads(line)
                        if event['kind'] == 'result':
                            result = event['result']; result['runtime'] = {k: event[k] for k in ('seconds', 'peak_memory_bytes')}
                        if event['kind'] == 'error':
                            failure = event['error']
                        if on_event:
                            await on_event(event)
                    await process.wait()
                text = (await logs).decode()
                if process.returncode or result is None:
                    raise RuntimeError(failure or text)
                return result
            finally:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL); await process.wait()
                if not logs.done():
                    logs.cancel()
    monkeypatch.setattr(service, 'compute', compute)
    return configured, service


def setup(client, project, settings, batch_size=2, transformer=''):
    pid = project['id']; base = '/api/v1/projects/' + pid
    package = settings.workspace_root / pid / 'requirement-package'; package.mkdir(exist_ok=True)
    (package / 'data.csv').write_text('id,x,category,y\n' + '\n'.join(f'{i:04d},{i/10},{"a" if i%2 else "b"},{i/5}' for i in range(60)))
    response = client.post(base + '/datasets', json={'source_path': 'requirement-package/data.csv', 'mapping': {'target': 'y', 'id_column': 'id'}})
    assert response.status_code == 201, response.text
    dataset = response.json()
    study_request = {'dataset_id': dataset['id'], 'request_key': 'research', 'budget': {'seconds': 180, 'trials': 5, 'trial_seconds': 45}}
    response = client.post(base + '/modeling/studies', json=study_request)
    assert response.status_code == 201, response.text
    study = response.json()
    response = client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json={'request_key': 'first', 'batch_size': batch_size, 'models': ['linear'], 'features': {'transformer_path': transformer}})
    assert response.status_code == 201, response.text
    candidate = response.json()
    graph(client, pid, [node('start', 'start', inputs=[]), node('train', 'model_train', study_id=study['id'], candidate_id=candidate['id']),
                       node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])
    return base, dataset, study, candidate


def wait_task(client, base, task, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = client.get(base + '/tasks/' + task['id']).json()
        if value['status'] not in {'queued', 'running'}:
            return value
        time.sleep(.05)
    raise AssertionError(value)


def test_dataset_readonly_versions_scope_and_idempotency(modeling):
    (client, app, project, settings), service = modeling
    base, dataset, study, candidate = setup(client, project, settings)
    original = settings.workspace_root / project['id'] / 'requirement-package/data.csv'
    original.write_text('changed')
    frozen = service.path(project['id'], dataset['id']) / dataset['files']['source']['name']
    assert frozen.read_text().startswith('id,x,')
    assert client.post(base + '/datasets', json={'source_path': '../secrets.csv'}).status_code == 422
    other = client.post('/api/v1/projects', json={'name': '另一个项目'}).json()['id']
    assert client.get(f'/api/v1/projects/{other}/datasets/{dataset["id"]}').status_code == 404
    assert client.post(f'/api/v1/projects/{other}/modeling/studies', json={'dataset_id': dataset['id'], 'request_key': 'wrong'}).status_code == 404
    repeated = client.post(base + '/modeling/studies', json=study['request'])
    assert repeated.json()['id'] == study['id']
    assert client.post(base + '/modeling/studies', json={**study['request'], 'name': 'changed'}).status_code == 409
    assert client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json={**candidate['request'], 'metrics': {'mae': 0}}).status_code == 422
    assert client.get(base + '/modeling/studies/' + study['id'] + '/candidates?offset=0&limit=1').json()[0]['id'] == candidate['id']
    assert 'request' not in client.get(base + '/modeling/studies?summary=true').json()[0]
    assert client.get(base + '/modeling/studies?after=9999').json() == []


def test_actual_workflow_training_prediction_download_and_repeat(real_compute):
    (client, app, project, settings), service = real_compute
    base, dataset, study, candidate = setup(client, project, settings)
    graph(client, project['id'], [node('start', 'start', inputs=[]), node('analysis', 'data_analysis', dataset_id=dataset['id']),
        node('features', 'feature_extract', dataset_id=ref('analysis', 'output', 'dataset_id'), features={}),
        node('train', 'model_train', dataset_id=ref('features', 'output', 'dataset_id'), features=ref('features', 'output', 'feature_plan'), study_id=study['id'], candidate_id=candidate['id']),
        node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'analysis'), edge('analysis', 'features'), edge('features', 'train'), edge('train', 'end')])
    task = start(client, base, 'train', purpose='build_test')
    result = wait_task(client, base, task)
    assert result['status'] == 'succeeded', result
    assert result['runs']
    research = client.get(base + '/modeling/studies/' + study['id']).json()
    assert research['best']['score'] < research['baseline']['mae']
    assert research['trials_used'] == 2
    repeated = start(client, base, 'train', purpose='build_test')
    assert repeated['id'] == task['id']
    download = client.get(base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}/download')
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert {'worker.py', 'environment.json', 'requirements.lock', 'predict.example.json', 'model/model.joblib', 'training-note.md', 'training-note.json', 'comparison.csv', 'curve.svg', 'split.json'} <= set(archive.namelist())
    path = base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}'
    original_trials = client.get(path).json()['trials']
    for trial in original_trials:
        slot = trial['slot']
        saved = service.path(project['id'], candidate['id']) / f'output/trial-{slot}/training-note.md'
        assert saved.is_file()
        response = client.get(path + f'/trials/{slot}/note')
        assert response.status_code == 200, response.text
        note = response.json()['note']
        assert note['trial']['metrics'] == trial['metrics']
        assert note['trial']['effective_model']['parameters']['alpha'] == trial['parameters']['regularization']
        assert len(note['comparison']) == 2
        assert note['task_id'] == task['id']
        assert note['gaps'] == []
        with zipfile.ZipFile(io.BytesIO(client.get(path + f'/trials/{slot}/note/download').content)) as archive:
            assert {'validation-predictions.csv', 'training-note.md', 'split.json', 'curve.svg'} <= set(archive.namelist())
        with zipfile.ZipFile(io.BytesIO(client.get(path + f'/download?slot={slot}').content)) as archive:
            assert json.loads(archive.read('configuration.json'))['selected_trial_slot'] == slot
            assert json.loads(archive.read('training-note.json'))['trial']['metrics'] == trial['metrics']
    other = client.post('/api/v1/projects', json={'name': '其他项目'}).json()['id']
    assert client.get(path.replace(project['id'], other) + '/trials/0/note').status_code == 404
    assert client.get(path + '/trials/999/note').status_code == 404
    assert client.get(path + '/download?slot=999').status_code == 404
    # Unlabeled input, same trained mapping, same actual project runtime.
    new = client.post(base + '/datasets/upload', files={'file': ('predict.csv', b'id,x,category\n0101,2,a\n0102,4,b\n', 'text/csv')}).json()
    graph(client, project['id'], [node('start', 'start', inputs=[]), node('predict', 'model_predict', dataset_id=new['id'], study_id=study['id'], candidate_id=candidate['id']), node('again', 'model_predict', dataset_id=new['id'], study_id=study['id'], candidate_id=candidate['id']), node('end', 'end', outputs={'result': ref('predict', 'output'), 'again': ref('again', 'output')})], [edge('start', 'predict'), edge('predict', 'again'), edge('again', 'end')])
    prediction = wait_task(client, base, start(client, base, 'prediction', purpose='customer_trial'))
    assert prediction['status'] == 'succeeded', prediction
    output = prediction['outputs']['result']
    assert output['rows'] == 2
    assert output['preview'][0]['id'] == '0101'
    assert client.get(base + '/' + output['artifact']).status_code == 200
    assert output['artifact'] != prediction['outputs']['again']['artifact']
    draft_path = f'/api/v1/applications/{project["id"]}/draft'
    draft = client.get(draft_path).json()
    added = client.post(draft_path, json={'expected_revision': draft['revision'], 'idempotency_key': 'predict-case', 'op': 'add_test', 'data': {'test': {
        'id': 'predict-case', 'name': 'Use the delivered model', 'requirement': 'Predict without changing research',
        'assertions': [{'path': ['result', 'rows'], 'operator': 'equals', 'expected': 2}]}}})
    assert added.status_code == 200, added.text
    tested = client.post(base + '/members/' + project['id'] + '/tests/run')
    assert tested.status_code == 200, tested.text
    assert tested.json()['passed'], tested.json()
    assert client.get(base + '/modeling/studies/' + study['id']).json()['trials_used'] == 2
    graph(client, project['id'], [node('start', 'start', inputs=[]), node('final', 'model_train', study_id=study['id'], finalize=True), node('end', 'end', outputs={'result': ref('final', 'output')})], [edge('start', 'final'), edge('final', 'end')])
    final = wait_task(client, base, start(client, base, 'final-evaluation'))
    assert final['status'] == 'succeeded', final
    assert final['outputs']['result']['rows'] == 12
    assert client.get(base + '/modeling/studies/' + study['id']).json()['status'] == 'sealed'
    assert client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json={'request_key': 'test-peeking'}).status_code == 409
    for trial in original_trials:
        note = client.get(path + f'/trials/{trial["slot"]}/note').json()['note']
        assert bool(note['test_result']) == (trial['slot'] == research['best']['slot'])
        assert note['trial'] == trial  # Later final evaluation does not rewrite the trial.


def test_failed_candidate_never_replaces_best(real_compute):
    (client, app, project, settings), service = real_compute
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1)
    assert wait_task(client, base, start(client, base, 'first'))['status'] == 'succeeded'
    best = client.get(base + '/modeling/studies/' + study['id']).json()['best']
    broken = client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json={'request_key': 'bad', 'parent_id': candidate['id'], 'batch_size': 1, 'models': ['linear'], 'parameters': {'nonexistent_parameter': True}}).json()
    graph(client, project['id'], [node('start', 'start', inputs=[]), node('train', 'model_train', study_id=study['id'], candidate_id=broken['id']), node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])
    wait_task(client, base, start(client, base, 'bad'))
    result = client.get(base + '/modeling/studies/' + study['id']).json()
    assert result['trials_used'] == 2
    assert result['best'] == best
    path = base + f'/modeling/studies/{study["id"]}/candidates/{broken["id"]}'
    response = client.get(path + '/trials/0/note')
    assert response.status_code == 200, response.text
    assert 'nonexistent_parameter' in response.json()['note']['trial']['error']
    assert response.json()['note']['comparison'][-1]['score'] is None
    assert client.get(path + '/trials/0/note/download').status_code == 200
    assert result['no_improvement_batches'] == 0
    assert result['repair_candidate_id'] == broken['id']
    # Failed training is repairable; repeated identical errors pause this study,
    # without declaring the search ineffective or erasing completed models.
    for attempt in range(2):
        broken = client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json={
            'request_key': f'repair-still-bad-{attempt}', 'parent_id': broken['id'],
            'batch_size': 1, 'models': ['linear'], 'parameters': {'nonexistent_parameter': True}}).json()
        graph(client, project['id'], [node('start', 'start', inputs=[]), node('train', 'model_train', study_id=study['id'], candidate_id=broken['id']), node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])
        wait_task(client, base, start(client, base, f'still-bad-{attempt}'))
    paused = client.get(base + '/modeling/studies/' + study['id']).json()
    assert paused['status'] == 'interrupted'
    assert paused['no_improvement_batches'] == 0
    assert paused['trials_used'] == 4
    assert paused['best'] == best
    fixed = client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json={
        'request_key': 'fixed', 'parent_id': broken['id'], 'engine': 'sklearn', 'batch_size': 1,
        'models': ['linear'], 'parameters': {'alpha': .5}}).json()
    graph(client, project['id'], [node('start', 'start', inputs=[]), node('train', 'model_train', study_id=study['id'], candidate_id=fixed['id']), node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])
    assert wait_task(client, base, start(client, base, 'fixed'))['status'] == 'succeeded'
    resumed = client.get(base + '/modeling/studies/' + study['id']).json()
    assert resumed['trials_used'] == 5
    assert resumed['failure_streak']['count'] == 0
    assert not resumed.get('repair_candidate_id')
    assert client.get(path + '/trials/0/note').json()['note']['trial']['status'] == 'failed'


def test_search_contract_validates_scopes_and_ranges():
    from agent_platform.modeling_models import CandidateRequest
    from pydantic import ValidationError
    good = {'request_key': 'r', 'models': ['svm'], 'search_space': {'svm': {'C': {'type': 'float', 'low': .1, 'high': 10, 'log': True}}}}
    assert CandidateRequest(**good).search_space['svm']['C'].log
    for invalid in [
        {**good, 'engine': 'sklearn'}, {**good, 'parameters': {'C': 1}},
        {**good, 'search_space': {'svm': {'C': {'type': 'float', 'low': 0, 'high': 10, 'log': True}}}},
        {**good, 'search_space': {'svm': {'degree': {'type': 'int', 'low': 1.5, 'high': 3}}}},
        {**good, 'autogluon_hyperparameters': {'GBM': {}}},
    ]:
        with pytest.raises(ValidationError):
            CandidateRequest(**invalid)


def test_old_candidate_request_remains_idempotent_after_optional_search_fields(modeling):
    (client, app, project, settings), service = modeling
    base, dataset, study, candidate = setup(client, project, settings)
    async def legacy_request():
        candidate['request'].pop('search_space')
        candidate['request'].pop('autogluon_hyperparameters')
        await service.put(project['id'], 'candidate', candidate)
    asyncio.run(legacy_request())
    reply = client.post(base + '/modeling/studies/' + study['id'] + '/candidates', json=candidate['request'])
    assert reply.status_code == 201, reply.text
    assert reply.json()['id'] == candidate['id']


def test_finish_stops_clock_without_evaluating_holdout_and_can_resume(real_compute):
    (client, app, project, settings), service = real_compute
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1)
    assert wait_task(client, base, start(client, base, 'first'))['status'] == 'succeeded'
    path = base + '/modeling/studies/' + study['id']
    original = client.get(path).json()
    response = client.post(path + '/finish', json={'reason': '本轮比较完成，后续取得新资料再研究'})
    assert response.status_code == 200, response.text
    done = response.json()
    assert done['status'] == 'finished' and done['active_since'] is None
    assert done['best'] == original['best'] and done['trials_used'] == 1
    assert not done.get('test_result')
    assert not (service.path(project['id'], study['id']) / 'holdout').exists()
    assert client.post(path + '/finish', json={}).json()['used_seconds'] == done['used_seconds']
    assert client.post(path + '/candidates', json={'request_key': 'after-finish'}).status_code == 409
    resumed = client.patch(path + '/budget', json=original['budget']).json()
    assert resumed['status'] == 'ready' and resumed['best'] == done['best']
    assert resumed['used_seconds'] == pytest.approx(done['used_seconds'], abs=.05)
    assert client.get(path + '/candidates/' + candidate['id']).json()['trials'][0]['status'] == 'completed'


def test_stop_kills_compute_and_resume_keeps_snapshot(real_compute):
    (client, app, project, settings), service = real_compute
    solution = settings.workspace_root / project['id'] / 'solution'; solution.mkdir()
    code = solution / 'features.py'
    code.write_text('from sklearn.preprocessing import FunctionTransformer\nimport time\ndef slow(frame):\n    time.sleep(1)\n    return frame\ndef build_transformer():\n    return FunctionTransformer(slow, validate=False)\n')
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1, transformer='solution/features.py')
    task = start(client, base, 'interrupt')
    path = base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}'
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        current = client.get(path).json()
        if current.get('current'):
            break
        time.sleep(.1)
    assert current.get('current'), current
    code.write_text('raise RuntimeError("This later edit must never execute")')
    stopped = client.post(base + '/tasks/' + task['id'] + '/stop')
    assert stopped.status_code == 200, stopped.text
    assert client.get(path).json()['status'] == 'interrupted'
    if hasattr(service, 'test_pid'):
        with pytest.raises(ProcessLookupError):
            os.kill(service.test_pid, 0)
    response = client.post(base + '/tasks/' + task['id'] + '/resume', json={'message': '继续'})
    assert response.status_code == 202, response.text
    result = wait_task(client, base, task)
    assert result['status'] == 'succeeded', result
    saved = client.get(path).json()
    assert saved['trials'][0]['status'] == 'completed', saved
    assert client.get(base + '/modeling/studies/' + study['id']).json()['trials_used'] == 1


def test_restart_preserves_results_and_budget_without_running(modeling):
    (client, app, project, settings), service = modeling
    base, dataset, study, candidate = setup(client, project, settings)
    async def interrupted_state():
        study.update(status='running', active_since=time.time() - 5, trials_used=1, best={'score': 1.5})
        candidate.update(status='running', trials=[{'slot': 0, 'status': 'completed'}])
        await service.put(project['id'], 'study', study)
        await service.put(project['id'], 'candidate', candidate)
        await service.initialize()
    asyncio.run(interrupted_state())
    saved = client.get(base + '/modeling/studies/' + study['id']).json()
    assert saved['status'] == 'interrupted'
    assert saved['active_since'] is None
    assert 5 <= saved['used_seconds'] < 10
    assert saved['trials_used'] == 1
    assert saved['best']['score'] == 1.5
    assert client.get(base + '/tasks').json() == []


def test_final_trial_committed_before_interrupt_does_not_need_more_budget(real_compute):
    (client, app, project, settings), service = real_compute
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1)
    task = start(client, base, 'last-commit')
    assert wait_task(client, base, task)['status'] == 'succeeded'
    folder = service.path(project['id'], candidate['id']) / 'output/trial-0'
    mtime = (folder / 'model.joblib').stat().st_mtime_ns
    async def crash_after_commit():
        saved_study = await service.get(project['id'], 'study', study['id'])
        saved_candidate = await service.get(project['id'], 'candidate', candidate['id'])
        saved_study.update(status='interrupted', active_since=None)
        saved_study['budget']['trials'] = 1
        saved_candidate.update(status='interrupted', batch_accounted=False)
        await service.put(project['id'], 'study', saved_study)
        await service.put(project['id'], 'candidate', saved_candidate)
        return await service.run_candidate(project['id'], study['id'], candidate['id'], task['id'], 'resumed-run')
    restored = asyncio.run(crash_after_commit())
    assert restored['status'] == 'completed'
    assert (folder / 'model.joblib').stat().st_mtime_ns == mtime
    assert client.get(base + '/modeling/studies/' + study['id']).json()['trials_used'] == 1
