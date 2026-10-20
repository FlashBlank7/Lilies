"""A long training computation must not monopolize project task submission."""
import asyncio

import httpx

from tests.test_modeling import modeling, setup  # noqa: F401
from tests.test_projects import configured, graph, node, edge, start, settled  # noqa: F401


def prepare_held_training(fixture, monkeypatch):
    (client, app, project, settings), service = fixture
    base, _, study, _ = setup(client, project, settings)
    entered = asyncio.Event()
    computations = []

    async def held(*args, **kwargs):
        computations.append(args)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, 'compute', held)
    body = {'request_key': 'long-training', 'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}
    url = base + f'/modeling/studies/{study["id"]}/train'
    return client, app, project, service, base, url, body, entered, computations


def test_retrying_active_training_returns_existing_task_and_rejects_changed_request(modeling, monkeypatch):
    client, app, project, service, base, url, body, entered, computations = prepare_held_training(modeling, monkeypatch)
    first = client.post(url, json=body)
    assert first.status_code == 202, first.text
    task = first.json()
    client.portal.call(asyncio.wait_for, entered.wait(), 2)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver',
                                    headers={'Authorization': 'Bearer test'}) as http:
            try:
                repeated = await asyncio.wait_for(http.post(url, json=body), 2)
                assert repeated.status_code == 202, repeated.text
                assert repeated.json()['id'] == task['id']
                assert repeated.json()['status'] == 'running'
                changed = await asyncio.wait_for(http.post(url, json={**body, 'models': ['forest']}), 2)
                assert changed.status_code == 409, changed.text
                assert len(computations) == 1
                assert [value['id'] for value in (await http.get(base+'/tasks')).json()] == [task['id']]
            finally:
                stopped = await http.post(base+'/tasks/'+task['id']+'/stop')
                assert stopped.json()['status'] == 'interrupted'

    client.portal.call(scenario)


def test_another_training_submission_does_not_block_short_workflow_or_resume(modeling, monkeypatch):
    client, app, project, service, base, url, body, entered, computations = prepare_held_training(modeling, monkeypatch)
    pid = project['id']
    graph(client, pid, [node('start', 'start'), node('ask', 'human_input', fields=[
        {'name': 'answer', 'label': '回答', 'type': 'string', 'required': True}]),
        node('end', 'end', outputs={'resumed': True})], [edge('start', 'ask'), edge('ask', 'end')])
    paused = settled(client, base, start(client, base, 'paused', workflow_id=pid))
    assert paused['status'] == 'waiting_input'
    response = client.post(base+'/tasks/'+paused['id']+'/runs/'+paused['runs'][0]['id']+'/input',
                           json={'values': {'answer': '继续'}})
    assert response.status_code == 200, response.text
    graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'short': True})], [edge('start', 'end')])
    training = client.post(url, json=body).json()
    client.portal.call(asyncio.wait_for, entered.wait(), 2)
    original = service.candidate
    submitting = asyncio.Event()

    async def observe_submission(project_id, study_id, request, **kwargs):
        if request.request_key == 'next-training':
            submitting.set()
        return await original(project_id, study_id, request, **kwargs)

    monkeypatch.setattr(service, 'candidate', observe_submission)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver',
                                    headers={'Authorization': 'Bearer test'}) as http:
            pending = asyncio.create_task(http.post(url, json={**body, 'request_key': 'next-training'}))
            try:
                await asyncio.wait_for(submitting.wait(), 2)
                assert not pending.done()  # The same study still serializes candidate changes.
                short, resumed = await asyncio.wait_for(asyncio.gather(
                    http.post(base+'/tasks', json={'request_key': 'short', 'workflow_id': pid}),
                    http.post(base+'/tasks/'+paused['id']+'/resume', json={}),
                ), 2)
                assert short.status_code == resumed.status_code == 202
                assert resumed.json()['id'] == paused['id']
                assert not pending.done()
                assert len(computations) == 1
                return short.json()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await http.post(base+'/tasks/'+training['id']+'/stop')

    short = client.portal.call(scenario)
    assert settled(client, base, short)['outputs'] == {'short': True}
    assert settled(client, base, paused)['outputs'] == {'resumed': True}
