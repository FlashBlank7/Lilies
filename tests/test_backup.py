import json
import os
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from agent_platform.api import create_app
from agent_platform.backup import create_backup, restore_backup
from agent_platform.config import Settings
from agent_platform.maintenance import data_access
from tests.test_projects import fixture_graphs, settled, start


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
    assert settings.modeling_image == 'sha256:fixed'
    with sqlite3.connect(restored / 'data/agent_platform.db') as c:
        assert c.execute('SELECT value FROM example').fetchone() == ('saved',)


def test_running_platform_blocks_backup_and_backup_blocks_startup(tmp_path):
    settings = Settings(api_token='test', data_dir=tmp_path / 'data',
                        workspace_root=tmp_path / 'workspaces', model_egress_enabled=False)
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


def test_restores_codex_index_paths_without_changing_thread_or_history(tmp_path, contents):
    data, workspaces, _ = contents
    home = data / 'local-agents/project/session/codex-home'
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
