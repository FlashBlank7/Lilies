"""Fault injection uses real short-lived processes, without a Docker daemon."""
import asyncio
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from agent_platform import modeling as modeling_module
from agent_platform.modeling import Modeling
from tests.test_projects import configured as configured  # noqa: F401


@pytest.fixture
def service(tmp_path):
    return Modeling(SimpleNamespace(
        storage=SimpleNamespace(db_path=tmp_path / 'test.db'),
        settings=SimpleNamespace(data_dir=tmp_path, modeling_image='test-environment'),
    ))


@pytest.fixture
def docker_cli(tmp_path, monkeypatch):
    script = tmp_path / 'docker_cli.py'
    script.write_text('''import json,sys,time
mode, action = sys.argv[1:3]
if mode == 'hang':
    time.sleep(60)
if mode in ('failed', 'missing'):
    print('No such container: test' if mode == 'missing' else 'daemon unavailable', file=sys.stderr)
    sys.exit(1)
if mode == 'invalid':
    print('not JSON')
elif action == 'run':
    print(json.dumps({'worker': 'print("worker")', 'requirements': 'pandas==2.3.3', 'python': '3.13', 'machine': 'test'}))
elif action == 'image':
    print('sha256:test-image')
''')
    original = asyncio.create_subprocess_exec

    def install(**modes):
        calls, processes = [], []
        started = asyncio.Event()

        async def execute(*args, **kwargs):
            if args[0] != 'docker':
                return await original(*args, **kwargs)
            calls.append(args)
            mode = modes.get(args[1], 'ok')
            if mode == 'uninstalled':
                raise FileNotFoundError('docker')
            process = await original(sys.executable, str(script), mode, args[1], **kwargs)
            processes.append(process)
            if args[1] == 'run':
                started.set()
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', execute)
        monkeypatch.setattr(modeling_module, 'ENVIRONMENT_TIMEOUT_SECONDS', .3)
        monkeypatch.setattr(modeling_module, 'CONTAINER_CLEANUP_TIMEOUT_SECONDS', .3)
        return SimpleNamespace(calls=calls, processes=processes, started=started, modes=modes)

    return install


async def test_snapshot_records_environment_and_reuses_it(service, docker_cli):
    cli = docker_cli(rm='missing')  # --rm may already have removed the container.
    folder = await service.snapshot_environment('sha256:test-image')
    worker = (folder / 'worker.py').read_text()
    metadata = json.loads((folder / 'environment.json').read_text())
    assert metadata['image'] == 'sha256:test-image'
    assert metadata['worker_sha256'] == hashlib.sha256(worker.encode()).hexdigest()
    assert (folder / 'requirements.lock').read_text() == 'pandas==2.3.3'
    assert not service.containers
    calls = list(cli.calls)
    assert await service.snapshot_environment('sha256:test-image') == folder
    assert cli.calls == calls


@pytest.mark.parametrize('mode, message', [('hang', '超时'), ('failed', 'daemon unavailable'), ('invalid', '信息不完整'), ('uninstalled', '无法启动 Docker 命令')])
async def test_snapshot_failure_is_readable_and_cleans_only_its_container(service, docker_cli, mode, message):
    cli = docker_cli(run=mode)
    service.containers.add('another-running-model')
    with pytest.raises(RuntimeError, match=message) as caught:
        await service.snapshot_environment('sha256:test-image')
    assert 'sha256:test-image' in str(caught.value)
    run, cleanup = cli.calls
    name = run[run.index('--name') + 1]
    assert run[run.index('--label') + 1] == 'lilies.modeling.owner=' + service.owner_label
    assert cleanup == ('docker', 'rm', '-f', name)
    assert service.containers == {'another-running-model'}
    assert not list(service.root.glob('_environments/*/worker.py'))
    assert all(p.returncode is not None for p in cli.processes)


async def test_cancel_snapshot_stays_cancelled_and_cleans_up(service, docker_cli):
    cli = docker_cli(run='hang')
    task = asyncio.create_task(service.snapshot_environment('sha256:test-image'))
    await cli.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.containers
    assert all(p.returncode is not None for p in cli.processes)
    assert cli.calls[-1][:3] == ('docker', 'rm', '-f')


@pytest.mark.parametrize('mode', ['hang', 'failed', 'uninstalled'])
async def test_cleanup_failure_is_bounded_logged_and_can_be_retried(service, docker_cli, caplog, mode):
    cli = docker_cli(rm=mode)
    service.containers.add('owned-container')
    assert await asyncio.wait_for(service.remove_container('owned-container'), 2) is False
    assert service.containers == {'owned-container'}
    assert 'owned-container' in caplog.text
    assert all(p.returncode is not None for p in cli.processes)
    cli.modes['rm'] = 'ok'
    assert await service.remove_container('owned-container') is True
    assert not service.containers


async def test_failed_cleanup_preserves_original_snapshot_error(service, docker_cli, caplog):
    cli = docker_cli(run='failed', rm='hang')
    with pytest.raises(RuntimeError, match='读取建模环境.*daemon unavailable'):
        await asyncio.wait_for(service.snapshot_environment('sha256:test-image'), 2)
    assert len(service.containers) == 1
    assert next(iter(service.containers)) in caplog.text
    assert '清理超时' in caplog.text
    assert all(p.returncode is not None for p in cli.processes)


@pytest.mark.parametrize('mode, message', [('hang', '检查建模环境.*超时'), ('failed', 'daemon unavailable'), ('uninstalled', '无法启动 Docker 命令')])
async def test_image_check_failure_is_readable_and_reaps_cli(service, docker_cli, mode, message):
    cli = docker_cli(image=mode)
    with pytest.raises(RuntimeError, match=message):
        await service.image()
    assert all(p.returncode is not None for p in cli.processes)


def test_study_api_returns_environment_timeout_and_allows_retry(configured, docker_cli):
    client, app, project, settings = configured
    cli = docker_cli(run='hang')
    base = '/api/v1/projects/' + project['id']
    source = settings.workspace_root / project['id'] / 'requirement-package/data.csv'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('x,y\n1,2\n2,4\n')
    registered = client.post(base + '/datasets', json={'source_path': 'requirement-package/data.csv', 'mapping': {'target': 'y'}})
    assert registered.status_code == 201, registered.text
    body = {'dataset_id': registered.json()['id'], 'request_key': 'same-request'}
    failed = client.post(base + '/modeling/studies', json=body)
    assert failed.status_code == 422, failed.text
    assert '读取建模环境' in failed.json()['detail'] and '超时' in failed.json()['detail']
    assert client.get(base + '/modeling/studies').json() == []
    assert not app.state.services.modeling.containers
    cli.modes['run'] = 'ok'
    retried = client.post(base + '/modeling/studies', json=body)
    assert retried.status_code == 201, retried.text
    assert retried.json()['status'] == 'ready'
    assert len(client.get(base + '/modeling/studies').json()) == 1
