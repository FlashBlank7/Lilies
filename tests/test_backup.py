import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from agent_platform.api import create_app
from agent_platform.backup import create_backup, restore_backup
from agent_platform.config import Settings
from agent_platform.maintenance import data_access
from tests.test_projects import fixture_graphs, settled, start


@pytest.fixture(autouse=True)
def isolate_backup_settings(monkeypatch):
    # Process environment wins over dotenv; clear it when testing restored files.
    names = {'DATA_DIR', 'WORKSPACE_ROOT', 'WORKSPACE_HOST_ROOT', 'MODELING_IMAGE',
             'MODEL_EGRESS_ENABLED', 'OFFICIAL_AGENT_EGRESS_ENABLED', 'AUTOMATIC_TASKS_ENABLED'}
    for name in tuple(os.environ):
        if name.upper() in names:
            monkeypatch.delenv(name)


@pytest.fixture
def backup_cli(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'platform/backend/src'
    environment = {**os.environ, 'PYTHONPATH': str(source), 'MODEL_EGRESS_ENABLED': 'false',
                   'OFFICIAL_AGENT_EGRESS_ENABLED': 'false', 'AUTOMATIC_TASKS_ENABLED': 'false'}

    def run(*arguments):
        return subprocess.run([sys.executable, '-m', 'agent_platform.backup',
                               *(str(arg) for arg in arguments)],
                              cwd=tmp_path, env=environment, capture_output=True, text=True,
                              timeout=30)
    return run


@pytest.fixture
def contents(tmp_path):
    data, workspaces = tmp_path / 'source-data', tmp_path / 'source-workspaces'
    data.mkdir()
    workspaces.mkdir()
    with sqlite3.connect(data / 'agent_platform.db') as c:
        c.execute('CREATE TABLE example(value TEXT)')
        c.execute("INSERT INTO example VALUES ('saved')")
    for name in ('modeling/project/model.joblib', 'local-agents/project/state.json',
                 'events/saved.jsonl', 'secrets/connector.json'):
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('original')
    (workspaces / 'project').mkdir()
    (workspaces / 'project/source.csv').write_text('original data')
    config = tmp_path / 'config.env'
    config.write_text('API_TOKEN=test\nMODELING_IMAGE=sha256:fixed\nMODEL_EGRESS_ENABLED=false\n')
    return data, workspaces, config


def test_full_backup_restore_keeps_files_private_and_independent(tmp_path, contents):
    data, workspaces, config = contents
    external = tmp_path / 'external'
    external.write_text('outside contents must not be copied')
    (data / 'local-agents/project/auth.json').symlink_to(external)
    (workspaces / 'project').chmod(0o555)
    backup = create_backup(data, workspaces, tmp_path / 'backups', config)
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / '.env').stat().st_mode & 0o777 == 0o600
    (data / 'modeling/project/model.joblib').write_text('changed after backup')
    restored = restore_backup(backup, tmp_path / '恢复目录')
    assert (restored / 'data/modeling/project/model.joblib').read_text() == 'original'
    assert (restored / 'data/secrets/connector.json').read_text() == 'original'
    assert (restored / 'data/events/saved.jsonl').read_text() == 'original'
    assert (restored / 'data/local-agents/project/state.json').read_text() == 'original'
    assert (restored / 'data/local-agents/project/auth.json').is_symlink()
    assert os.readlink(restored / 'data/local-agents/project/auth.json') == str(external)
    assert (restored / 'workspaces/project').stat().st_mode & 0o777 == 0o555
    assert (restored / 'workspaces/project/source.csv').read_text() == 'original data'
    assert (restored / 'data/modeling/project/model.joblib').stat().st_ino != (
        backup / 'data/modeling/project/model.joblib').stat().st_ino
    settings = Settings(_env_file=restored / '.env')
    assert settings.data_dir == restored / 'data'
    assert settings.workspace_root == restored / 'workspaces'
    assert settings.model_egress_enabled is False
    assert settings.official_agent_egress_enabled is False
    assert settings.automatic_tasks_enabled is False
    assert settings.modeling_image == 'sha256:fixed'
    with sqlite3.connect(restored / 'data/agent_platform.db') as c:
        assert c.execute('SELECT value FROM example').fetchone() == ('saved',)


def test_running_platform_blocks_backup_and_backup_blocks_startup(tmp_path):
    settings = Settings(api_token='test', data_dir=tmp_path / 'data',
                        workspace_root=tmp_path / 'workspaces', model_egress_enabled=False,
                        official_agent_egress_enabled=False, _env_file=None)
    with TestClient(create_app(settings)):
        with pytest.raises(RuntimeError, match='停止平台'):
            create_backup(settings.data_dir, settings.workspace_root, tmp_path / 'backups')
    with data_access(settings.data_dir, settings.workspace_root, exclusive=True):
        with pytest.raises(RuntimeError, match='停止平台'):
            with TestClient(create_app(settings)):
                pass
    assert create_backup(settings.data_dir, settings.workspace_root, tmp_path / 'backups').is_dir()


def test_restored_project_resumes_original_waiting_task_without_reallocation(tmp_path):
    settings = Settings(api_token='test', data_dir=tmp_path / 'data',
                        workspace_root=tmp_path / 'workspaces', model_egress_enabled=False,
                        official_agent_egress_enabled=False, _env_file=None,
                        scheduler_poll_seconds=3600, run_artifacts_keep_days=0)
    with TestClient(create_app(settings)) as client:
        client.headers['Authorization'] = 'Bearer test'
        project = client.post('/api/v1/projects', json={'name': '备份恢复测试'}).json()
        base, validate, _ = fixture_graphs(client, project)
        client.put(base + '/records/resources/only', json={'expected_revision': 0, 'value': {'owner': ''}})
        first = settled(client, base, start(client, base, 'first', inputs={'request_id': 'first'}))
        pending = settled(client, base, start(client, base, 'second', inputs={'request_id': 'second'}))
        assert first['status'] == 'succeeded'
        assert pending['status'] == 'waiting_input'
    backup = create_backup(settings.data_dir, settings.workspace_root, tmp_path / 'backups')
    restored = restore_backup(backup, tmp_path / 'restore')
    new_settings = settings.model_copy(update={'data_dir': restored / 'data',
                                               'workspace_root': restored / 'workspaces'})
    with TestClient(create_app(new_settings)) as client:
        client.headers['Authorization'] = 'Bearer test'
        assert client.get(base + '/tasks/' + pending['id']).json()['status'] == 'waiting_input'
        assert client.get(base + '/records/resources/only').json()['value']['owner'] == 'first'
        client.put(base + '/records/resources/only', json={'expected_revision': 2, 'value': {'owner': ''}})
        assert client.post(base + '/tasks/' + pending['id'] + '/resume', json={}).status_code == 202
        done = settled(client, base, pending)
        assert done['status'] == 'succeeded', done
        assert len([r for r in done['runs'] if r['application_id'] == validate]) == 1
        assert client.get(base + '/records/resources/only').json()['value']['owner'] == 'second'
    with sqlite3.connect(restored / 'data/agent_platform.db') as c:
        for raw, in c.execute('SELECT state_json FROM workflow_runs'):
            state = json.loads(raw)
            assert Path(state['workspace_path']).is_relative_to(restored / 'workspaces')
    # Nothing wrote back to the original service's state.
    with TestClient(create_app(settings)) as client:
        client.headers['Authorization'] = 'Bearer test'
        assert client.get(base + '/records/resources/only').json()['value']['owner'] == 'first'


def test_partial_copy_is_removed_and_retry_can_succeed(tmp_path, contents, monkeypatch):
    from agent_platform import backup as module
    data, workspaces, _ = contents
    original = module.copy_workspace_file
    def failing(src, dst):
        if Path(src).name == 'source.csv':
            raise OSError('disk full')
        return original(src, dst)
    monkeypatch.setattr(module, 'copy_workspace_file', failing)
    with pytest.raises(OSError, match='disk full'):
        create_backup(data, workspaces, tmp_path / 'backups')
    assert list((tmp_path / 'backups').iterdir()) == []
    assert (workspaces / 'project/source.csv').read_text() == 'original data'
    monkeypatch.setattr(module, 'copy_workspace_file', original)
    assert create_backup(data, workspaces, tmp_path / 'backups').is_dir()


@pytest.mark.parametrize('corruption', ['missing_model', 'invalid_database'])
def test_broken_backups_do_not_produce_partial_restore(tmp_path, contents, corruption):
    data, workspaces, _ = contents
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    if corruption == 'missing_model':
        (backup / 'data/modeling/project/model.joblib').unlink()
    else:
        db = backup / 'data/agent_platform.db'
        db.write_bytes(b'0' * db.stat().st_size)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_backup(backup, tmp_path / 'restore')
    assert not (tmp_path / 'restore').exists()
    assert not list(tmp_path.glob('.restore-*'))


def test_existing_and_nested_directories_are_not_overwritten(tmp_path, contents):
    data, workspaces, _ = contents
    with pytest.raises(ValueError, match='嵌套'):
        create_backup(data, workspaces, workspaces / 'backup')
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    with pytest.raises(ValueError, match='不会覆盖'):
        restore_backup(backup, data)
    with pytest.raises(ValueError, match='嵌套'):
        restore_backup(backup, backup / 'restored')


def test_restore_relocates_timer_and_subscription_workspaces_without_rewriting_business_inputs(
        tmp_path, contents):
    data, workspaces, _ = contents
    paths = {'project': str(workspaces / 'project'), 'root': str(workspaces),
             'external': str(tmp_path / 'source-workspaces-external'), 'relative': 'project'}
    inputs = {'source_path': str(workspaces / 'project/source.csv'),
              'workspace_path': str(workspaces / 'project'),
              'code': f'open({str(workspaces / "project/source.csv")!r})'}
    configurations = {
        key: {'workspace_path': path, 'static_inputs': inputs,
              'subscription_message': {'workspace_path': str(workspaces)},
              'input_mapping': {'source': 'event.file'}, 'enabled': True}
        for key, path in paths.items()}
    with sqlite3.connect(data / 'agent_platform.db') as c:
        c.execute('CREATE TABLE event_timers(timer_key TEXT PRIMARY KEY, workspace_path TEXT, '
                  'due_inputs_json TEXT, status TEXT)')
        c.execute('CREATE TABLE event_subscriptions(id TEXT PRIMARY KEY, config_json TEXT, event_count INTEGER)')
        for key, path in paths.items():
            c.execute('INSERT INTO event_timers VALUES(?,?,?,?)',
                      (key, path, json.dumps(inputs), 'pending'))
            c.execute('INSERT INTO event_subscriptions VALUES(?,?,?)',
                      (key, json.dumps(configurations[key]), 7))
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    restored = restore_backup(backup, tmp_path / 'restore')
    expected_paths = {**paths, 'project': str(restored / 'workspaces/project'),
                      'root': str(restored / 'workspaces')}
    with sqlite3.connect(restored / 'data/agent_platform.db') as c:
        for key, path, raw, status in c.execute('SELECT * FROM event_timers'):
            assert path == expected_paths[key]
            assert json.loads(raw) == inputs
            assert status == 'pending'
        for key, raw, event_count in c.execute('SELECT * FROM event_subscriptions'):
            assert json.loads(raw) == {**configurations[key], 'workspace_path': expected_paths[key]}
            assert event_count == 7
    for original in (data, backup / 'data'):
        with sqlite3.connect(original / 'agent_platform.db') as c:
            assert dict(c.execute('SELECT timer_key,workspace_path FROM event_timers')) == paths
            assert {key: json.loads(raw) for key, raw in c.execute(
                'SELECT id,config_json FROM event_subscriptions')} == configurations


@pytest.mark.parametrize('relative_home', [
    'local-agents/project/session/codex-home',
    'local-agents/project/conversations/conversation/session/codex-home',
    'official-agent/generation/job/codex-home',
    'official-agent/account/codex-home',
])
def test_restores_codex_index_paths_without_changing_thread_or_history(tmp_path, contents, relative_home):
    data, workspaces, _ = contents
    home = data / relative_home
    (home / 'sessions').mkdir(parents=True)
    rollout = home / 'sessions/rollout-original-thread.jsonl'
    rollout.write_text('original conversation')
    with sqlite3.connect(home / 'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT)')
        c.execute('INSERT INTO threads VALUES (?,?,?,?)',
                  ('original-thread', str(rollout), str(home.parent / 'empty-workspace'), 'keep title'))
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    restored = restore_backup(backup, tmp_path / 'restore')
    relative = home.relative_to(data)
    with sqlite3.connect(restored / 'data' / relative / 'state_5.sqlite') as c:
        ident, path, cwd, title = c.execute('SELECT * FROM threads').fetchone()
    assert ident == 'original-thread' and title == 'keep title'
    assert path == str(restored / 'data' / rollout.relative_to(data))
    assert Path(path).read_text() == 'original conversation'
    assert Path(cwd).is_relative_to(restored / 'data')
    with sqlite3.connect(home / 'state_5.sqlite') as c:
        assert c.execute('SELECT rollout_path FROM threads').fetchone()[0] == str(rollout)


@pytest.mark.parametrize('relative_link', [
    'local-agents/linked-project',
    'local-agents/project/conversations/conversation/session/codex-home',
    'official-agent',
])
def test_restore_does_not_relocate_codex_indexes_through_directory_symlinks(
        tmp_path, contents, relative_link):
    data, workspaces, _ = contents
    external_home = tmp_path / 'external' / 'codex-home'
    external_home.mkdir(parents=True)
    old_rollout = str(data / 'private-rollout.jsonl')
    with sqlite3.connect(external_home / 'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT)')
        c.execute('INSERT INTO threads VALUES (?,?,?)', ('outside', old_rollout, str(data)))
    link = data / relative_link
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(external_home if link.name == 'codex-home' else external_home.parent)
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    restored = restore_backup(backup, tmp_path / 'restore')
    assert (restored / 'data' / relative_link).is_symlink()
    with sqlite3.connect(external_home / 'state_5.sqlite') as c:
        assert c.execute('SELECT rollout_path,cwd FROM threads').fetchone() == (old_rollout, str(data))


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_restore_rejects_codex_index_file_symlinks(tmp_path, contents, suffix):
    data, workspaces, _ = contents
    home = data / 'official-agent/generation/job/codex-home'
    home.mkdir(parents=True)
    if suffix:
        with sqlite3.connect(home / 'state_5.sqlite') as c:
            c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT)')
    external = tmp_path / 'external-index'
    external.write_bytes(b'untouched external data')
    (home / ('state_5.sqlite' + suffix)).symlink_to(external)
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    with pytest.raises(ValueError, match='Codex 会话索引.*不能是符号链接'):
        restore_backup(backup, tmp_path / 'restore')
    assert external.read_bytes() == b'untouched external data'
    assert not (tmp_path / 'restore').exists()
    assert not list(tmp_path.glob('.restore-*'))


@pytest.mark.parametrize('with_config', [True, False], ids=['enabled-config', 'without-config'])
def test_backup_cli_round_trip_disables_external_and_automatic_work(
        tmp_path, contents, backup_cli, with_config):
    data, workspaces, config = contents
    original = ('API_TOKEN=test\nMODELING_IMAGE=sha256:fixed\nCUSTOM_SETTING=retained\n'
                'DATA_DIR=/previous/data\nWORKSPACE_ROOT=/previous/workspaces\n'
                'WORKSPACE_HOST_ROOT=/previous/workspaces\nMODEL_EGRESS_ENABLED=true\n'
                'OFFICIAL_AGENT_EGRESS_ENABLED=true\nAUTOMATIC_TASKS_ENABLED=true\n')
    config.write_text(original)
    output = tmp_path / 'backups'
    args = ['create', '--data', data, '--workspaces', workspaces, '--output', output]
    if with_config:
        args.extend(['--config', config])
    created = backup_cli(*args)
    assert created.returncode == 0, created.stderr
    assert '备份完成' in created.stdout
    backup, = output.iterdir()
    destination = tmp_path / '恢复 CLI'
    restored = backup_cli('restore', backup, destination)
    assert restored.returncode == 0, restored.stderr
    assert '恢复完成' in restored.stdout
    settings = Settings(_env_file=destination / '.env')
    assert settings.data_dir == destination / 'data'
    assert settings.workspace_root == destination / 'workspaces'
    assert settings.workspace_host_root == destination / 'workspaces'
    assert settings.model_egress_enabled is False
    assert settings.official_agent_egress_enabled is False
    assert settings.automatic_tasks_enabled is False
    assert (destination / '.env').stat().st_mode & 0o777 == 0o600
    if with_config:
        assert (destination / '.env').read_text().startswith(original)
        assert (backup / '.env').read_text() == original
        assert settings.modeling_image == 'sha256:fixed'
    else:
        assert not (backup / '.env').exists()
    assert (destination / 'workspaces/project/source.csv').read_text() == 'original data'
    with sqlite3.connect(destination / 'data/agent_platform.db') as connection:
        assert connection.execute('SELECT value FROM example').fetchone() == ('saved',)
    assert not list(output.glob('.incomplete-*'))
    assert not list(tmp_path.glob('.restore-*'))


@pytest.mark.parametrize('failure', ['missing_directory', 'corrupt_database'])
def test_backup_cli_create_failure_is_reported_and_cleaned(tmp_path, contents, backup_cli, failure):
    data, workspaces, _ = contents
    if failure == 'missing_directory':
        data = tmp_path / 'missing-data'
        message = '目录不存在'
    else:
        (data / 'agent_platform.db').write_bytes(b'not a sqlite database')
        message = 'not a database'
    output = tmp_path / 'backups'
    result = backup_cli('create', '--data', data, '--workspaces', workspaces, '--output', output)
    assert result.returncode == 1
    assert message in result.stderr
    assert 'NameError' not in result.stderr
    assert 'Traceback' not in result.stderr
    assert '备份完成' not in result.stdout
    assert not output.exists() or not list(output.iterdir())
    assert (workspaces / 'project/source.csv').read_text() == 'original data'


@pytest.mark.parametrize('failure', ['corrupt_database', 'existing_destination'])
def test_backup_cli_restore_failure_preserves_existing_files(tmp_path, contents, backup_cli, failure):
    data, workspaces, _ = contents
    backup = create_backup(data, workspaces, tmp_path / 'backups')
    destination = tmp_path / 'restore'
    if failure == 'corrupt_database':
        database = backup / 'data/agent_platform.db'
        database.write_bytes(b'0' * database.stat().st_size)
        message = 'not a database'
    else:
        destination.mkdir()
        (destination / 'keep.txt').write_text('do not overwrite')
        message = '不会覆盖现有项目'
    result = backup_cli('restore', backup, destination)
    assert result.returncode == 1
    assert message in result.stderr
    assert 'NameError' not in result.stderr
    assert 'Traceback' not in result.stderr
    assert '恢复完成' not in result.stdout
    assert backup.is_dir()
    assert not list(tmp_path.glob('.restore-*'))
    if failure == 'existing_destination':
        assert list(destination.iterdir()) == [destination / 'keep.txt']
        assert (destination / 'keep.txt').read_text() == 'do not overwrite'
    else:
        assert not destination.exists()
