"""Exercise the production compute queue through project tasks without Docker."""
import asyncio
import time

import pytest

from tests.test_modeling import modeling as modeling, setup, wait_task  # noqa: F401
from tests.test_projects import configured as configured, graph, node, edge, ref, start  # noqa: F401


@pytest.fixture
def workers(modeling, monkeypatch):
    _, service = modeling
    processes = []

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.done = asyncio.Event()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def kill(self):
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.done.set()

    async def launch(*command, **kwargs):
        assert command[:2] == ('docker', 'run')
        process = Process()
        processes.append(process)
        return process

    async def remove(name):
        service.containers.discard(name)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(service, 'remove_container', remove)
    return processes


def prepare_split(service, project, study):
    output = service.path(project['id'], study['id']) / 'output'
    output.mkdir(exist_ok=True)
    (output / 'split.json').write_text('{}')


def remaining_second(client, service, project, study):
    async def save():
        study.update(used_seconds=9, active_since=None)
        study['budget']['seconds'] = 10
        await service.put(project['id'], 'study', study)
    client.portal.call(save)


def wait_candidate(client, path, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        candidate = client.get(path).json()
        if candidate['status'] == status:
            return candidate
        time.sleep(.01)
    raise AssertionError(candidate)


def assert_slot_occupied(client, service):
    async def probe():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(service.slots.acquire(), .02)
    client.portal.call(probe)


@pytest.mark.parametrize('stop_waiter', [False, True], ids=['start-preparation', 'stop-preparation'])
def test_initial_data_preparation_reports_queue_and_can_be_stopped(modeling, workers, stop_waiter):
    (client, _, project, settings), service = modeling
    base, dataset, first_study, _ = setup(client, project, settings)
    prepare_split(service, project, first_study)
    first = start(client, base, 'owns-slot-before-preparation')
    deadline = time.monotonic() + 5
    while not workers and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(workers) == 1

    study = client.post(base + '/modeling/studies', json={
        'dataset_id': dataset['id'], 'request_key': 'new-study-without-split',
        'budget': {'seconds': 180, 'trials': 5, 'trial_seconds': 45},
    }).json()
    candidate = client.post(base + f'/modeling/studies/{study["id"]}/candidates', json={
        'request_key': 'first-candidate-without-split', 'batch_size': 1, 'models': ['linear'],
    }).json()
    graph(client, project['id'], [node('start', 'start', inputs=[]),
          node('train', 'model_train', study_id=study['id'], candidate_id=candidate['id']),
          node('end', 'end', outputs={'result': ref('train', 'output')})],
          [edge('start', 'train'), edge('train', 'end')])
    second = start(client, base, 'wait-for-first-preparation')
    path = base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}'
    study_path = base + f'/modeling/studies/{study["id"]}'
    try:
        queued = wait_candidate(client, path, 'queued')
        research = client.get(study_path).json()
        assert research['status'] == 'queued'
        assert research['next_action'] == '等待计算资源，之后准备数据划分'
        assert queued['trials'] == [] and research['trials_used'] == 0
        assert len(workers) == 1
        assert client.get(base + f'/tasks/{first["id"]}').json()['status'] == 'running'
        assert not (service.path(project['id'], study['id']) / 'output/split.json').exists()

        if stop_waiter:
            assert client.post(base + f'/tasks/{second["id"]}/stop').status_code == 200
            assert wait_task(client, base, second, seconds=5)['status'] == 'interrupted'
            assert client.get(path).json()['status'] == 'interrupted'
            assert client.get(study_path).json()['active_since'] is None
            assert_slot_occupied(client, service)
            assert len(workers) == 1
            resumed = client.post(base + f'/tasks/{second["id"]}/resume', json={})
            assert resumed.status_code == 202
            assert resumed.json()['id'] == second['id']
            wait_candidate(client, path, 'queued')
            assert len(workers) == 1
        assert client.post(base + f'/tasks/{first["id"]}/stop').status_code == 200
        running = wait_candidate(client, path, 'running')
        research = client.get(study_path).json()
        assert running['task_id'] == second['id']
        assert research['status'] == 'running'
        assert research['next_action'] == '正在准备数据划分'
        deadline = time.monotonic() + 5
        while len(workers) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(workers) == 2
        assert research['trials_used'] == 0
    finally:
        assert client.post(base + f'/tasks/{second["id"]}/stop').status_code == 200
        assert client.post(base + f'/tasks/{first["id"]}/stop').status_code == 200


@pytest.mark.parametrize('stop_waiter', [False, True], ids=['queue-budget', 'queue-stop'])
def test_waiting_task_reports_budget_or_stop_and_keeps_owners_slot(modeling, workers, stop_waiter):
    (client, _, project, settings), service = modeling
    base, dataset, first_study, _ = setup(client, project, settings)
    prepare_split(service, project, first_study)
    first = start(client, base, 'owns-compute-slot')
    deadline = time.monotonic() + 5
    while not workers and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(workers) == 1

    try:
        study = client.post(base + '/modeling/studies', json={
            'dataset_id': dataset['id'], 'request_key': 'waiting-study',
            'budget': {'seconds': 10, 'trials': 5, 'trial_seconds': 5},
        }).json()
        candidate = client.post(base + f'/modeling/studies/{study["id"]}/candidates', json={
            'request_key': 'waiting-candidate', 'batch_size': 2, 'models': ['linear'],
        }).json()
        trial = {'slot': 0, 'status': 'completed', 'model': 'linear', 'metrics': {'mae': .5}}

        async def save_completed_trial():
            study.update(trials_used=1, best={'candidate_id': candidate['id'], 'slot': 0, 'score': .5})
            candidate.update(trials=[trial], status='interrupted')
            await service.put(project['id'], 'study', study)
            await service.put(project['id'], 'candidate', candidate)
        client.portal.call(save_completed_trial)
        prepare_split(service, project, study)
        graph(client, project['id'], [node('start', 'start', inputs=[]),
              node('train', 'model_train', study_id=study['id'], candidate_id=candidate['id']),
              node('end', 'end', outputs={'result': ref('train', 'output')})],
              [edge('start', 'train'), edge('train', 'end')])
        if not stop_waiter:
            remaining_second(client, service, project, study)
        second = start(client, base, 'waits-for-compute-slot')
        path = base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}'
        wait_candidate(client, path, 'queued')

        if stop_waiter:
            assert client.post(base + f'/tasks/{second["id"]}/stop').status_code == 200
        result = wait_task(client, base, second, seconds=5)
        saved = client.get(path).json()
        research = client.get(base + f'/modeling/studies/{study["id"]}').json()
        assert saved['status'] == 'interrupted'
        assert saved['trials'] == [trial]
        assert research['trials_used'] == 1
        assert research['active_since'] is None
        if stop_waiter:
            assert result['status'] == 'interrupted'
            assert saved['error'] == '用户停止或服务中断'
            assert '等待用户要求继续' in research['next_action']
        else:
            assert result['status'] == 'failed'
            assert saved['error'] == research['error'] == '等待计算资源时已用尽预算'
            assert '等待计算资源时已用尽预算' in result['error']
            assert research['used_seconds'] >= 10
        assert len(workers) == 1
        assert client.get(base + f'/tasks/{first["id"]}').json()['status'] == 'running'
        assert_slot_occupied(client, service)
    finally:
        assert client.post(base + f'/tasks/{first["id"]}/stop').status_code == 200

    async def check_one_available_slot():
        await asyncio.wait_for(service.slots.acquire(), .1)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(service.slots.acquire(), .02)
        finally:
            service.slots.release()
    client.portal.call(check_one_available_slot)
    assert workers[0].returncode == -9


def test_running_task_reports_execution_budget_and_releases_slot(modeling, workers):
    (client, _, project, settings), service = modeling
    base, _, study, candidate = setup(client, project, settings)
    prepare_split(service, project, study)
    remaining_second(client, service, project, study)
    result = wait_task(client, base, start(client, base, 'running-timeout'), seconds=5)
    path = base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}'
    assert result['status'] == 'failed'
    assert '计算运行时已用尽预算' in result['error']
    assert client.get(path).json()['error'] == '计算运行时已用尽预算'
    assert len(workers) == 1 and workers[0].returncode == -9
    assert not service.slots.locked()


def test_committed_trial_after_interrupt_finishes_without_compute_or_budget(modeling, workers):
    (client, _, project, settings), service = modeling
    base, _, study, candidate = setup(client, project, settings, batch_size=1)
    trial = {'slot': 0, 'status': 'completed', 'model': 'linear', 'metrics': {'mae': .5}}

    async def committed_state():
        study.update(status='interrupted', active_since=None, used_seconds=180, trials_used=1,
                     best={'candidate_id': candidate['id'], 'slot': 0, 'score': .5})
        candidate.update(status='interrupted', trials=[trial], scheduled_slots=1,
                         error='等待计算资源时已用尽预算')
        await service.put(project['id'], 'study', study)
        await service.put(project['id'], 'candidate', candidate)
    client.portal.call(committed_state)
    result = wait_task(client, base, start(client, base, 'resume-committed-trial'), seconds=5)
    assert result['status'] == 'succeeded', result
    saved = client.get(base + f'/modeling/studies/{study["id"]}/candidates/{candidate["id"]}').json()
    assert saved['status'] == 'completed' and saved['error'] == ''
    assert saved['trials'] == [trial]
    assert client.get(base + f'/modeling/studies/{study["id"]}').json()['trials_used'] == 1
    assert not workers
