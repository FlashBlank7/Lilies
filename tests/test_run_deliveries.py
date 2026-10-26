"""Preserve exact run files without changing workflow outputs or retry behavior."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.run_deliveries import RunDeliveries
from tests.test_users import platform as platform, signup, project  # noqa: F401
from tests.test_projects import configured, graph, node, edge, ref, start, settled  # noqa: F401


def test_project_file_versions_survive_overwrite_and_enforce_scope(platform):
    client, app = platform
    _, owner = signup(client, 'delivery-owner')
    client.headers.update(owner)
    pid = project(client, owner)
    base = '/api/v1/projects/' + pid
    workspace = app.state.services.projects.workspace(pid)
    source = workspace / 'results' / 'report.md'
    source.parent.mkdir(parents=True, exist_ok=True)
    outputs = {'result': {'paths': {'report': 'results/report.md'}, 'markdown': '# 本次报告'}}
    graph(client, pid, [node('s', 'start'), node('e', 'end', outputs=outputs)], [edge('s', 'e')])
    source.write_text('# 第一版\n编号 001')
    first = settled(client, base, start(client, base, 'first'))
    assert first['status'] == 'succeeded'
    assert first['outputs'] == outputs
    saved = first['runs'][0]['delivery_files'][0]
    assert saved['status'] == 'saved'
    url = f"{base}/tasks/{first['id']}/deliveries/{saved['run_id']}/{saved['sha256']}?name=report.md"
    source.write_text('# 第二版')
    second = settled(client, base, start(client, base, 'second'))
    assert second['runs'][0]['delivery_files'][0]['sha256'] != saved['sha256']
    assert client.get(url).text == '# 第一版\n编号 001'
    source.unlink()
    assert client.get(url).status_code == 200
    other = project(client, owner)
    assert client.get(url.replace(pid, other)).status_code == 404
    _, stranger = signup(client, 'delivery-stranger')
    client.headers.update(stranger)
    assert client.get(url).status_code == 404


def record(root, outputs, rid='r'):
    return {'id': rid, 'outputs': outputs, 'state': SimpleNamespace(workspace_path=str(root))}


def test_historical_missing_link_and_nested_paths(tmp_path):
    store = RunDeliveries(tmp_path / 'data')
    root = tmp_path / 'workspace'
    (root / 'results').mkdir(parents=True)
    source = root / 'results' / 'r.html'
    source.write_text('<h1>原件</h1>')
    output = {'step': {'report': {'paths': {'report_html': 'results/r.html'}}}}
    old = record(root, output)
    assert store.files('p', 't', old)[0]['status'] == 'unverified'
    assert not store.root.exists()  # Reading history must not manufacture a snapshot.
    store.capture('p', 't', old)
    saved = store.files('p', 't', old)[0]
    assert store.download('p', 't', 'r', saved['sha256']).read_text() == source.read_text()
    (root / 'results' / 'link.md').symlink_to(source)
    changed = record(root, {'paths': {'report': 'results/missing.md'}, 'file': 'results/link.md'}, 'other')
    store.capture('p', 't', changed)
    assert all(entry['status'] == 'unavailable' for entry in store.files('p', 't', changed))
    assert json.loads((store.folder('p', 't', 'r') / 'manifest.json').read_text())['files'][0] == saved


def test_resume_keeps_old_download_and_isolates_simulation_workspace(tmp_path):
    store = RunDeliveries(tmp_path / 'data')
    root = tmp_path / 'simulation'
    (root / 'results').mkdir(parents=True)
    report = root / 'results' / 'r.md'
    report.write_text('阶段结果')
    run = record(root, {'paths': {'report': 'results/r.md'}})
    store.capture('p', 't', run)
    first = store.files('p', 't', run)[0]
    report.write_text('恢复后的结果')
    store.capture('p', 't', run)
    second = store.files('p', 't', run)[0]
    assert first['sha256'] != second['sha256']
    assert store.download('p', 't', 'r', first['sha256']).read_text() == '阶段结果'
    assert store.download('p', 't', 'r', second['sha256']).read_text() == '恢复后的结果'
    assert not (tmp_path / 'production').exists()


def test_stale_manifest_and_oversized_file_never_reuse_old_content(tmp_path, monkeypatch):
    import agent_platform.run_deliveries as deliveries
    store = RunDeliveries(tmp_path / 'data')
    root = tmp_path / 'workspace'
    (root / 'results').mkdir(parents=True)
    report = root / 'results' / 'r.md'
    report.write_text('old')
    run = {**record(root, {'report': 'results/r.md'}), 'updated_at': 'first'}
    store.capture('p', 't', run)
    first = store.files('p', 't', run)[0]
    run['updated_at'] = 'resumed'
    assert store.files('p', 't', run)[0]['status'] == 'unverified'
    monkeypatch.setattr(deliveries, 'MAX_FILE', 2)
    report.write_text('new content')
    store.capture('p', 't', run)
    current = store.files('p', 't', run)[0]
    assert current['status'] == 'unverified'
    assert current['reason'] == 'size_limit'
    assert 'sha256' not in current
    assert store.download('p', 't', 'r', first['sha256']).read_text() == 'old'


@pytest.mark.parametrize('declared', [True, False])
def test_failed_resume_after_another_task_overwrites_report(configured, monkeypatch, declared):
    """The completed write is not repeated; restart/recovery cannot relabel B as A."""
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, settings = configured
    pid, calls = project['id'], []
    base = '/api/v1/projects/' + pid
    original = _NODE_EXECUTORS['project_record']

    async def effect(runtime, run):
        if run.node.id == 'report':
            version = run.state.inputs['version']
            calls.append(version)
            path = Path(run.state.workspace_path) / 'results/report.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# report ' + version)
            if declared:
                return {'report': 'results/report.md'}
        if run.node.id == 'fail' and run.state.inputs['version'] == 'A' and calls == ['A']:
            raise RuntimeError('temporary downstream failure')
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'project_record', effect)
    graph(client, pid, [node('s', 'start', inputs=[{'name': 'version', 'type': 'string'}]),
        node('report', 'project_record', action='get', collection='test', key='report'),
        node('fail', 'project_record', action='get', collection='test', key='fail'),
        node('e', 'end', outputs={'version': ref('s', 'version'), 'report': 'results/report.md'})],
        [edge('s', 'report'), edge('report', 'fail'), edge('fail', 'e')])
    a = settled(client, base, start(client, base, 'A', inputs={'version': 'A'}))
    assert a['status'] == 'failed'
    if declared:
        assert a['runs'][0]['delivery_files'][0]['status'] == 'saved'
    b = settled(client, base, start(client, base, 'B', inputs={'version': 'B'}))
    assert b['status'] == 'succeeded'
    # Recreate services from disk before resuming, with no in-memory copy cache.
    with TestClient(create_app(settings)) as restarted:
        restarted.headers['Authorization'] = 'Bearer test'
        assert restarted.post(base + '/tasks/' + a['id'] + '/resume', json={}).status_code == 202
        done = settled(restarted, base, a)
        assert done['status'] == 'succeeded'
        assert calls == ['A', 'B']
        assert done['outputs']['version'] == 'A'
        saved = done['runs'][0]['delivery_files'][0]
        if declared:
            assert saved['status'] == 'saved'
            url = f"{base}/tasks/{a['id']}/deliveries/{saved['run_id']}/{saved['sha256']}"
            assert restarted.get(url).text == '# report A'
        else:
            assert saved['status'] == 'unverified'
            assert 'sha256' not in saved


def test_parent_forwards_child_copy_after_workspace_overwrite(configured, monkeypatch):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    wid = client.post(base + '/members', json={'name': '写报告'}).json()['id']
    path = app.state.services.projects.workspace(pid) / 'results/report.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('child original')
    graph(client, wid, [node('s', 'start'), node('e', 'end', outputs={'report': 'results/report.md'})], [edge('s', 'e')])
    graph(client, pid, [node('s', 'start'), node('call', 'tool', tool_name='workflow:' + wid),
        node('overwrite', 'project_record', action='get', collection='test', key='overwrite'),
        node('e', 'end', outputs={'result': ref('call', 'output')})],
        [edge('s', 'call'), edge('call', 'overwrite'), edge('overwrite', 'e')])
    original = _NODE_EXECUTORS['project_record']

    async def overwrite(runtime, run):
        path.write_text('later shared bytes')
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'project_record', overwrite)
    task = settled(client, base, start(client, base, 'parent'))
    assert task['status'] == 'succeeded', task
    assert path.read_text() == 'later shared bytes'
    assert len(task['runs']) == 2
    for run in task['runs']:
        saved = run['delivery_files'][0]
        assert saved['status'] == 'saved'
        url = f"{base}/tasks/{task['id']}/deliveries/{saved['run_id']}/{saved['sha256']}"
        assert client.get(url).text == 'child original'


def test_size_limits_allow_explicit_current_download_but_missing_files_do_not(tmp_path, monkeypatch):
    import agent_platform.run_deliveries as deliveries
    store = RunDeliveries(tmp_path / 'data')
    root = tmp_path / 'ws'
    (root / 'results').mkdir(parents=True)
    (root / 'results/a.zip').write_bytes(b'a' * 8)
    (root / 'results/b.csv').write_bytes(b'b' * 8)
    monkeypatch.setattr(deliveries, 'MAX_FILE', 10)
    monkeypatch.setattr(deliveries, 'MAX_TOTAL', 12)
    run = record(root, {'model_package': 'results/a.zip', 'results_csv': 'results/b.csv', 'report': 'results/missing.md'})
    store.capture('p', 't', run)
    entries = {f['path']: f for f in store.files('p', 't', run)}
    assert entries['results/a.zip']['status'] == 'saved'
    assert entries['results/b.csv']['status'] == 'unverified'
    assert entries['results/b.csv']['reason'] == 'size_limit'
    assert entries['results/missing.md']['status'] == 'unavailable'


@pytest.mark.parametrize('selected', ['first', 'second'])
def test_resumed_new_report_and_explicit_earlier_version_are_distinct(configured, monkeypatch, selected):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, _ = configured
    pid, calls = project['id'], []
    base = '/api/v1/projects/' + pid

    async def write(runtime, run):
        calls.append(run.node.id)
        path = Path(run.state.workspace_path) / 'results/report.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run.node.id)
        return {'output': {'report': 'results/report.md'}}

    monkeypatch.setitem(_NODE_EXECUTORS, 'code', write)
    graph(client, pid, [node('s', 'start'), node('first', 'code', code='def main(inputs): return {}'),
        node('human', 'human_input', fields=[{'name': 'ok', 'label': '确认', 'type': 'string', 'required': True}]),
        node('second', 'code', code='def main(inputs): return {}'),
        node('e', 'end', outputs={'report': ref(selected, 'output', 'report')})],
        [edge('s', 'first'), edge('first', 'human'), edge('human', 'second'), edge('second', 'e')])
    task = settled(client, base, start(client, base, 'pause'))
    assert task['status'] == 'waiting_input', task
    saved = task['runs'][0]['delivery_files'][0]
    url = f"{base}/tasks/{task['id']}/deliveries/{saved['run_id']}/{saved['sha256']}"
    assert client.get(url).text == 'first'
    (app.state.services.projects.workspace(pid) / 'results/report.md').write_text('unrelated overwrite')
    rid = task['runs'][0]['id']
    assert client.post(f"{base}/tasks/{task['id']}/runs/{rid}/input", json={'values': {'ok': 'yes'}}).status_code == 200
    assert client.post(f"{base}/tasks/{task['id']}/resume", json={}).status_code == 202
    done = settled(client, base, task)
    assert done['status'] == 'succeeded', done
    assert calls == ['first', 'second']
    current = done['runs'][0]['delivery_files'][0]
    assert current['status'] == 'saved'
    current_url = f"{base}/tasks/{task['id']}/deliveries/{current['run_id']}/{current['sha256']}"
    assert client.get(current_url).text == selected
    assert client.get(url).text == 'first'


def test_loop_final_file_keeps_the_last_iteration_copy(configured, monkeypatch):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    calls = []

    async def write(runtime, run):
        current = run.inputs['iteration']
        calls.append(current)
        path = Path(run.workspace_path) / 'results/report.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('iteration ' + str(current))
        return {'output': {'report': 'results/report.md', 'iteration': current}}

    monkeypatch.setitem(_NODE_EXECUTORS, 'code', write)
    nested = {'nodes': [node('ns', 'start', inputs=[{'name': 'iteration', 'type': 'number'}]),
        node('write', 'code', code='def main(inputs): return {}'),
        node('ne', 'end', outputs={'report': ref('write', 'output', 'report'), 'iteration': ref('write', 'output', 'iteration')})],
        'edges': [edge('ns', 'write'), edge('write', 'ne')]}
    graph(client, pid, [node('s', 'start'), node('loop', 'loop', workflow=nested, output_node_id='ne',
        break_value=ref('ne', 'iteration'), break_condition={'value': 0, 'operator': 'gte', 'expected': 1}, max_iterations=3),
        node('e', 'end', outputs={'report': ref('loop', 'output', 'report'), 'iteration': ref('loop', 'output', 'iteration')})],
        [edge('s', 'loop'), edge('loop', 'e')])
    task = settled(client, base, start(client, base, 'loop'))
    assert task['status'] == 'succeeded', task
    assert calls == [0, 1]
    saved = task['runs'][0]['delivery_files'][0]
    assert saved['status'] == 'saved'
    url = f"{base}/tasks/{task['id']}/deliveries/{saved['run_id']}/{saved['sha256']}"
    assert client.get(url).text == 'iteration 1'


def test_failed_producer_fallback_does_not_confirm_an_old_workspace_file(configured, monkeypatch):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    path = app.state.services.projects.workspace(pid) / 'results/report.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('old shared file')

    async def fail(runtime, run):
        raise ValueError('producer failed')

    monkeypatch.setitem(_NODE_EXECUTORS, 'code', fail)
    producer = {**node('produce', 'code', code='def main(inputs): return {}'),
                'error_strategy': 'degraded', 'degraded_value': {'report': 'results/report.md'}}
    graph(client, pid, [node('s', 'start'), producer, node('e', 'end', outputs={'report': ref('produce', 'output', 'report')})],
          [edge('s', 'produce'), edge('produce', 'e')])
    task = settled(client, base, start(client, base, 'fallback'))
    assert task['status'] == 'succeeded', task
    assert task['runs'][0]['delivery_files'][0]['status'] == 'unverified'
    assert 'sha256' not in task['runs'][0]['delivery_files'][0]


@pytest.mark.parametrize("rewrite", [False, True])
def test_resumed_path_forwarder_keeps_origin_unless_it_writes(configured, monkeypatch, rewrite):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    writes = []
    failed = False

    async def local_code(runtime, run):
        nonlocal failed
        version = run.state.inputs['version']
        if run.node.id == 'report':
            writes.append(version)
            path = Path(run.workspace_path) / 'results/report.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# report ' + version)
            return {'output': {'report': 'results/report.md'}}
        if run.node.id == 'gate':
            if version == 'A' and not failed:
                failed = True
                raise ValueError('temporary downstream failure')
            return {'output': {'ok': True}}
        assert run.node.id == 'forward'
        # Exactly the configured pure code: return {'report': inputs['report']}.
        inputs = runtime._resolve(run.config.inputs, run.context)
        if rewrite:
            (Path(run.workspace_path) / inputs['report']).write_text('# forwarded ' + version)
        return {'output': {'report': inputs['report']}}

    monkeypatch.setitem(_NODE_EXECUTORS, 'code', local_code)
    graph(client, pid, [
        node('s', 'start', inputs=[{'name': 'version', 'type': 'string'}]),
        node('report', 'code', code='def main(inputs): return {}'),
        node('gate', 'code', code='def main(inputs): return {}'),
        node('forward', 'code', inputs={'report': ref('report', 'output', 'report')},
             code="def main(inputs): return {'report': inputs['report']}"),
        node('e', 'end', outputs={'version': ref('s', 'version'), 'report': ref('forward', 'output', 'report')})
    ], [edge('s', 'report'), edge('report', 'gate'), edge('gate', 'forward'), edge('forward', 'e')])
    a = settled(client, base, start(client, base, 'A', inputs={'version': 'A'}))
    assert a['status'] == 'failed', a
    original = a['runs'][0]['delivery_files'][0]
    original_url = f"{base}/tasks/{a['id']}/deliveries/{original['run_id']}/{original['sha256']}"
    assert client.get(original_url).text == '# report A'
    b = settled(client, base, start(client, base, 'B', inputs={'version': 'B'}))
    assert b['status'] == 'succeeded', b
    assert client.post(base + '/tasks/' + a['id'] + '/resume', json={}).status_code == 202
    resumed = settled(client, base, a)
    assert resumed['status'] == 'succeeded', resumed
    assert writes == ['A', 'B']
    saved = resumed['runs'][0]['delivery_files'][0]
    assert saved['status'] == 'saved'
    url = f"{base}/tasks/{a['id']}/deliveries/{saved['run_id']}/{saved['sha256']}"
    content = client.get(url).text
    assert content == ('# forwarded A' if rewrite else '# report A')
    assert client.get(original_url).text == '# report A'


def test_concurrent_overwrite_during_forward_does_not_belong_to_resumed_run(configured, monkeypatch):
    import asyncio
    from threading import Event
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    writes = []
    failed = False
    entered = Event()
    release = Event()
    b_written = Event()

    async def local_code(runtime, run):
        nonlocal failed
        version = run.state.inputs['version']
        if run.node.id == 'report':
            writes.append(version)
            path = Path(run.workspace_path) / 'results/report.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# report ' + version)
            if version == 'B':
                b_written.set()
            return {'output': {'report': 'results/report.md'}}
        if run.node.id == 'gate':
            if version == 'A' and not failed:
                failed = True
                raise ValueError('temporary downstream failure')
            return {'output': {'ok': True}}
        inputs = runtime._resolve(run.config.inputs, run.context)
        if version == 'A':
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
        return {'output': {'report': inputs['report']}}

    monkeypatch.setitem(_NODE_EXECUTORS, 'code', local_code)
    graph(client, pid, [
        node('s', 'start', inputs=[{'name': 'version', 'type': 'string'}]),
        node('report', 'code', code='def main(inputs): return {}'),
        node('gate', 'code', code='def main(inputs): return {}'),
        node('forward', 'code', inputs={'report': ref('report', 'output', 'report')},
             code="def main(inputs): return {'report': inputs['report']}"),
        node('e', 'end', outputs={'version': ref('s', 'version'), 'report': ref('forward', 'output', 'report')})
    ], [edge('s', 'report'), edge('report', 'gate'), edge('gate', 'forward'), edge('forward', 'e')])
    a = settled(client, base, start(client, base, 'A', inputs={'version': 'A'}))
    assert a['status'] == 'failed', a
    original = a['runs'][0]['delivery_files'][0]
    original_url = f"{base}/tasks/{a['id']}/deliveries/{original['run_id']}/{original['sha256']}"
    assert client.get(original_url).text == '# report A'
    assert client.post(base + '/tasks/' + a['id'] + '/resume', json={}).status_code == 202
    assert entered.wait(5)
    try:
        b = start(client, base, 'B', inputs={'version': 'B'})
        # B cannot finish a write while A owns the file-producing step window.
        assert not b_written.wait(.2)
    finally:
        release.set()
    resumed = settled(client, base, a)
    b = settled(client, base, b)
    assert b['status'] == 'succeeded', b
    assert resumed['status'] == 'succeeded', resumed
    assert writes == ['A', 'B']
    saved = resumed['runs'][0]['delivery_files'][0]
    assert saved['status'] == 'saved'
    url = f"{base}/tasks/{a['id']}/deliveries/{saved['run_id']}/{saved['sha256']}"
    content = client.get(url).text
    assert content == '# report A'
