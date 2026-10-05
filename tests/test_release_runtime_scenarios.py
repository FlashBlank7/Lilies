"""Release scenarios through project APIs, without model calls.

The long CPU scenario uses the production Docker sandbox when explicitly selected
with RELEASE_RUNTIME_DOCKER=1; ordinary test runs cover the graph/accounts cases.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_projects import edge, node, ref
from tests.test_users import platform, project, signup  # noqa: F401


def save_graph(client, headers, pid, nodes, edges, *, revision=None):
    path = f'/api/v1/projects/{pid}/workflows/{pid}/draft'
    if revision is None:
        revision = client.get(f'/api/v1/applications/{pid}/draft', headers=headers).json()['revision']
    return client.put(path, headers=headers, json={
        'expected_revision': revision, 'workflow': {'nodes': nodes, 'edges': edges}})


def launch(client, headers, pid, key, **kwargs):
    response = client.post(f'/api/v1/projects/{pid}/tasks', headers=headers,
                           json={'request_key': key, 'workflow_id': pid, **kwargs})
    assert response.status_code == 202, response.text
    return response.json()


def wait_task(client, headers, pid, tid, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f'/api/v1/projects/{pid}/tasks/{tid}', headers=headers)
        assert response.status_code == 200, response.text
        task = response.json()
        if task['status'] not in {'running', 'queued'}:
            return task
        time.sleep(.03)
    raise AssertionError(task)


def test_large_branch_graph_executes_only_selected_paths(platform):
    client, _ = platform
    _, headers = signup(client, 'branch-owner')
    pid = project(client, headers, '大型分支计算')
    nodes = [node('start', 'start')]
    edges = []
    branches = 64
    for index in range(branches):
        choose, yes, no, join = [f'{kind}_{index}' for kind in ('choose', 'yes', 'no', 'join')]
        nodes.extend([
            node(choose, 'if_else', cases=[{'id': 'yes', 'conditions': [
                {'value': index % 2, 'operator': 'equals', 'expected': 0}]}]),
            node(yes, 'project_record', action='put', collection='branches', key=str(index),
                 value={'selected': 'yes'}, expected_revision=0),
            node(no, 'project_record', action='put', collection='branches', key=str(index),
                 value={'selected': 'no'}, expected_revision=0),
                node(join, 'template_transform', template=str(index)),
        ])
        edges.extend([edge('start', choose), edge(choose, yes, 'yes'), edge(choose, no, 'else'),
                      edge(yes, join), edge(no, join), edge(join, 'end')])
    nodes.append(node('end', 'end', outputs={'joins': [ref(f'join_{i}', 'text') for i in range(branches)]}))
    saved = save_graph(client, headers, pid, nodes, edges)
    assert saved.status_code == 200, saved.text
    began = time.monotonic()
    task = launch(client, headers, pid, 'large-branches')
    result = wait_task(client, headers, pid, task['id'])
    assert result['status'] == 'succeeded', result
    assert result['outputs']['joins'] == [str(i) for i in range(branches)]
    for index in range(branches):
        row = client.get(f'/api/v1/projects/{pid}/records/branches/{index}', headers=headers).json()
        assert row['revision'] == 1
        assert row['value'] == {'selected': 'yes' if index % 2 == 0 else 'no'}
    runtime = client.get('/api/v1/runs/' + result['runs'][0]['id'], headers=headers)
    assert runtime.status_code == 200, runtime.text
    state = runtime.json()['state']
    assert len(state['completed']) == 3 * branches + 2
    assert len(state['skipped']) == branches
    print(json.dumps({'scenario': 'large-branches', 'nodes': len(nodes), 'edges': len(edges),
                      'completed': len(state['completed']), 'skipped': len(state['skipped']),
                      'seconds': round(time.monotonic() - began, 3)}))


def test_two_accounts_run_and_edit_without_crossing_task_snapshots(platform):
    client, _ = platform
    alice, a = signup(client, 'concurrent-alice')
    bob, b = signup(client, 'concurrent-bob')
    pa, pb = project(client, a, '甲的任务'), project(client, b, '乙的任务')
    owners = [(pa, a, 'alice'), (pb, b, 'bob')]
    for pid, headers, label in owners:
        response = save_graph(client, headers, pid, [
            node('start', 'start'),
            node('write', 'project_record', action='put', collection='progress', key='once', value={'owner': label}),
            node('ask', 'human_input', fields=[{'name': 'answer', 'label': '回答', 'type': 'string', 'required': True}]),
            node('end', 'end', outputs={'owner': label, 'answer': ref('ask', 'answer')}),
        ], [edge('start', 'write'), edge('write', 'ask'), edge('ask', 'end')])
        assert response.status_code == 200, response.text
    began = time.monotonic()
    with ThreadPoolExecutor(2) as pool:
        tasks = list(pool.map(lambda owner: launch(client, owner[1], owner[0], 'same-request-key'), owners))
    paused = [wait_task(client, headers, pid, task['id']) for (pid, headers, _), task in zip(owners, tasks)]
    assert all(task['status'] == 'waiting_input' for task in paused)
    assert paused[0]['id'] != paused[1]['id']

    for (pid, headers, _), foreign, task in zip(owners, [b, a], paused):
        base = f'/api/v1/projects/{pid}/tasks/{task["id"]}'
        assert client.get(base, headers=foreign).status_code == 404
        assert client.post(base + '/stop', headers=foreign).status_code == 404
        assert client.post(base + '/resume', headers=foreign, json={}).status_code == 404
        denied = save_graph(client, foreign, pid, [node('start', 'start'), node('end', 'end')],
                            [edge('start', 'end')], revision=1)
        assert denied.status_code == 404
        assert [p['id'] for p in client.get('/api/v1/projects', headers=headers).json()] == [pid]

    def edit(owner):
        pid, headers, label = owner
        return save_graph(client, headers, pid, [node('start', 'start'),
            node('end', 'end', outputs={'owner': label + '-edited'})], [edge('start', 'end')])
    with ThreadPoolExecutor(2) as pool:
        edits = list(pool.map(edit, owners))
    assert all(response.status_code == 200 for response in edits), [r.text for r in edits]
    for (pid, headers, label), task in zip(owners, paused):
        base = f'/api/v1/projects/{pid}/tasks/{task["id"]}'
        saved = client.post(base + '/runs/' + task['runs'][0]['id'] + '/input', headers=headers,
                            json={'values': {'answer': label + '-answer'}})
        assert saved.status_code == 200, saved.text
    with ThreadPoolExecutor(2) as pool:
        resumed = list(pool.map(lambda pair: client.post(
            f'/api/v1/projects/{pair[0][0]}/tasks/{pair[1]["id"]}/resume',
            headers=pair[0][1], json={}), zip(owners, paused)))
    assert all(response.status_code == 202 for response in resumed)
    for (pid, headers, label), task in zip(owners, paused):
        done = wait_task(client, headers, pid, task['id'])
        assert done['status'] == 'succeeded', done
        assert done['outputs'] == {'owner': label, 'answer': label + '-answer'}
        assert [run['id'] for run in done['runs']] == [run['id'] for run in task['runs']]
        assert client.get(f'/api/v1/projects/{pid}/records/progress/once', headers=headers).json()['revision'] == 1
        latest = wait_task(client, headers, pid, launch(client, headers, pid, 'after-edit')['id'])
        assert latest['outputs'] == {'owner': label + '-edited'}

    # Once explicitly shared, two editors still cannot silently overwrite one revision.
    response = client.post(f'/api/v1/projects/{pa}/access-members', headers=a, json={'name': bob['user']['name']})
    assert response.status_code == 200, response.text
    revision = client.get(f'/api/v1/applications/{pa}/draft', headers=a).json()['revision']
    def shared_edit(editor):
        headers, label = editor
        return save_graph(client, headers, pa, [node('start', 'start'),
            node('end', 'end', outputs={'editor': label})], [edge('start', 'end')], revision=revision)
    with ThreadPoolExecutor(2) as pool:
        saved = list(pool.map(shared_edit, [(a, 'alice'), (b, 'bob')]))
    assert sum(response.status_code == 200 for response in saved) == 1, [r.text for r in saved]
    conflict = next(response for response in saved if response.status_code != 200)
    assert conflict.status_code in {409, 422}, conflict.text
    assert 'conflict' in conflict.text or '已被修改' in conflict.text, conflict.text
    assert client.get(f'/api/v1/applications/{pa}/draft', headers=a).json()['revision'] == revision + 1
    print(json.dumps({'scenario': 'two-accounts', 'accounts': [alice['user']['id'], bob['user']['id']],
                      'concurrent_tasks': 2, 'edit_statuses': sorted(r.status_code for r in saved),
                      'seconds': round(time.monotonic() - began, 3)}))


@pytest.mark.skipif(os.environ.get('RELEASE_RUNTIME_DOCKER') != '1', reason='Explicit real Docker CPU rehearsal')
def test_docker_cpu_stop_restart_and_resume_keeps_completed_nodes(tmp_path):
    seconds = 30
    settings = Settings(api_token='isolated-release-admin', data_dir=tmp_path/'data',
        workspace_root=tmp_path/'workspaces', workspace_host_root=None,
        model_egress_enabled=False, official_agent_egress_enabled=False,
        automatic_tasks_enabled=False, scheduler_poll_seconds=3600)
    app = create_app(settings)
    code = '''import hashlib, json, time
from pathlib import Path
def main(inputs):
    attempt = Path('cpu-attempts.txt')
    count = int(attempt.read_text()) + 1 if attempt.exists() else 1
    attempt.write_text(str(count))
    started = time.monotonic()
    rounds = 0
    digest = b'release-runtime'
    while time.monotonic() - started < inputs['seconds']:
        for _ in range(20000):
            digest = hashlib.sha256(digest).digest()
        rounds += 20000
        Path('cpu-heartbeat.json').write_text(json.dumps({'rounds': rounds, 'attempt': count}))
    Path('cpu-result.json').write_text(json.dumps({'rounds': rounds, 'attempt': count}))
    return {'rounds': rounds, 'attempt': count, 'file': 'cpu-result.json'}
'''
    began = time.monotonic()
    with TestClient(app) as client:
        _, headers = signup(client, 'cpu-owner')
        _, peer_headers = signup(client, 'cpu-peer')
        pid = project(client, headers, '长计算停止继续')
        peer_pid = project(client, peer_headers, '另一个账号的并行计算')
        response = save_graph(client, headers, pid, [node('start', 'start'),
            node('write_once', 'project_record', action='put', collection='progress', key='once', value={'started': True}),
            node('cpu', 'code', code=code, inputs={'seconds': seconds}, timeout=90),
            node('end', 'end', outputs={'result': ref('cpu', 'output')})],
            [edge('start', 'write_once'), edge('write_once', 'cpu'), edge('cpu', 'end')])
        assert response.status_code == 200, response.text
        peer_graph = save_graph(client, peer_headers, peer_pid, [node('start', 'start'),
            node('cpu', 'code', code=code, inputs={'seconds': 20}, timeout=90),
            node('end', 'end', outputs={'result': ref('cpu', 'output')})],
            [edge('start', 'cpu'), edge('cpu', 'end')])
        assert peer_graph.status_code == 200, peer_graph.text
        with ThreadPoolExecutor(2) as pool:
            task, peer_task = list(pool.map(lambda owner: launch(client, owner[1], owner[0], 'long-cpu'),
                                           [(pid, headers), (peer_pid, peer_headers)]))
        workspace = settings.workspace_root / pid
        heartbeat = workspace / 'cpu-heartbeat.json'
        peer_workspace = settings.workspace_root / peer_pid
        peer_heartbeat = peer_workspace / 'cpu-heartbeat.json'
        deadline = time.monotonic() + 30
        while not (heartbeat.exists() and peer_heartbeat.exists()) and time.monotonic() < deadline:
            time.sleep(.05)
        assert heartbeat.exists(), client.get(f'/api/v1/projects/{pid}/tasks/{task["id"]}', headers=headers).json()
        assert peer_heartbeat.exists(), client.get(
            f'/api/v1/projects/{peer_pid}/tasks/{peer_task["id"]}', headers=peer_headers).json()
        time.sleep(10)
        current = client.get(f'/api/v1/projects/{pid}/tasks/{task["id"]}', headers=headers).json()
        assert current['status'] == 'running'
        run_id = current['runs'][0]['id']
        containers = [sandbox.container_name for sandbox in app.state.services.sandboxes.sessions.values()
                      if sandbox.workspace == workspace]
        assert len(containers) == 1
        edited = save_graph(client, headers, pid, [node('start', 'start'),
            node('end', 'end', outputs={'wrong_draft': True})], [edge('start', 'end')])
        assert edited.status_code == 200, edited.text
        stopping = time.monotonic()
        response = client.post(f'/api/v1/projects/{pid}/tasks/{task["id"]}/stop', headers=headers)
        stop_seconds = time.monotonic() - stopping
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'interrupted'
        assert all(run['status'] == 'cancelled' for run in response.json()['runs'])
        assert all(sandbox.workspace != workspace for sandbox in app.state.services.sandboxes.sessions.values())
        for container in containers:
            inspection = subprocess.run(['docker', 'inspect', container], capture_output=True, text=True)
            assert inspection.returncode != 0, inspection.stdout
        stopped_heartbeat = heartbeat.read_bytes()
        running_peer_heartbeat = peer_heartbeat.read_bytes()
        time.sleep(.3)
        assert heartbeat.read_bytes() == stopped_heartbeat
        assert peer_heartbeat.read_bytes() != running_peer_heartbeat
        assert not (workspace / 'cpu-result.json').exists()
        peer_done = wait_task(client, peer_headers, peer_pid, peer_task['id'], timeout=30)
        assert peer_done['status'] == 'succeeded', peer_done
        assert peer_done['outputs']['result']['attempt'] == 1
        assert json.loads((peer_workspace / 'cpu-result.json').read_text())['attempt'] == 1
        assert not (workspace / 'cpu-result.json').exists()

    # A fresh API lifespan must remain dormant until this same account resumes.
    restarted_app = create_app(settings)
    with TestClient(restarted_app) as client:
        base = f'/api/v1/projects/{pid}/tasks/{task["id"]}'
        assert client.get(base, headers=headers).json()['status'] == 'interrupted'
        assert not restarted_app.state.services.projects.active
        resumed_at = time.monotonic()
        response = client.post(base + '/resume', headers=headers, json={})
        assert response.status_code == 202, response.text
        done = wait_task(client, headers, pid, task['id'], timeout=90)
        assert done['status'] == 'succeeded', done
        assert done['outputs']['result']['rounds'] > 0
        assert done['outputs']['result']['attempt'] == 2
        assert done['runs'][0]['id'] == run_id and len(done['runs']) == 1
        assert done['runs'][0]['draft_revision'] == 1
        assert client.get(f'/api/v1/projects/{pid}/records/progress/once', headers=headers).json()['revision'] == 1
        assert json.loads((workspace / 'cpu-result.json').read_text())['attempt'] == 2
        assert not restarted_app.state.services.sandboxes.sessions
        print(json.dumps({'scenario': 'docker-cpu', 'seconds_before_stop': 10,
            'stop_seconds': round(stop_seconds, 3), 'resume_seconds': round(time.monotonic() - resumed_at, 3),
            'total_seconds': round(time.monotonic() - began, 3), 'cpu_seconds_per_attempt': seconds,
            'rounds': done['outputs']['result']['rounds'], 'attempts': 2,
            'concurrent_accounts': 2, 'peer_cpu_seconds': 20, 'peer_attempts': 1,
            'completed_write_revision': 1, 'task_id': task['id'], 'run_id': run_id}))
