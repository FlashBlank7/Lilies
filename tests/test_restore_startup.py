"""A restored instance can be inspected and exercised without replaying old work."""
import os
import sqlite3
import time
from functools import partial

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.event_automation import DurableEventTimerRequest, EventSubscriptionCreateRequest
from tests.test_projects import edge, graph, node, settled, start


def settings_for(tmp_path, **changes):
    return Settings(_env_file=None, api_token='restore-test', data_dir=tmp_path/'data',
                    workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                    official_agent_egress_enabled=False, **changes)


def seed(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer restore-test'
        pid = client.post('/api/v1/projects', json={'name': '恢复演练'}).json()['id']
        graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'result': '可手动运行'})],
              [edge('start', 'end')])
        automation = app.state.services.event_automation
        client.portal.call(partial(automation.apply_timer, application_id=pid,
            workspace_path=str(settings.workspace_root/pid), request=DurableEventTimerRequest(
                operation='schedule', timer_key='restore-due', subject_id='sample', event_id='saved-event',
                occurred_at='2020-01-01T00:00:00+00:00', hold_for_seconds=1, due_inputs={})))
        client.portal.call(automation.create_subscription, EventSubscriptionCreateRequest(
            name='restore-subscription', application_id=pid, websocket_url='ws://127.0.0.1:9',
            allowed_hosts=['127.0.0.1'], subscription_message={'subscribe': 'test'},
            event_identity_path='id', input_mapping={'id': 'id'}, workspace_path=str(settings.workspace_root/pid)))
    with sqlite3.connect(settings.data_dir/'agent_platform.db') as db:
        db.execute("INSERT INTO official_agent_jobs(id,project_id,conversation_id,user_id,kind,status,created) VALUES(?,?,?,?,?,?,?)",
                   ('00000000-0000-4000-8000-000000000001', pid, '', 'root', 'generation', 'queued', time.time()))
        db.execute("INSERT INTO official_agent_jobs(id,project_id,conversation_id,user_id,kind,status,created,ended) VALUES(?,?,?,?,?,?,?,?)",
                   ('old-result', pid, '', 'root', 'chat', 'completed', 1, 2))
    old = settings.workspace_root/'.workflow-run-artifacts'/'old-run'
    old.mkdir(parents=True)
    (old/'report.txt').write_text('历史报告')
    os.utime(old, (1, 1))
    return pid, old


def test_disabled_automatic_tasks_keep_queues_timers_subscriptions_and_history_but_allow_manual_runs(tmp_path):
    settings = settings_for(tmp_path, automatic_tasks_enabled=False)
    pid, old = seed(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        client.headers['Authorization'] = 'Bearer restore-test'
        health = client.get('/health').json()
        assert health['automatic_tasks_enabled'] is False
        assert health['model_egress_enabled'] is False and health['official_agent_egress_enabled'] is False
        services = app.state.services
        assert services.scheduler.health()['running'] is False
        assert services.event_automation._timer_task is None
        assert services.event_automation._subscriptions == {}
        base = f'/api/v1/projects/{pid}'
        assert client.get(base+'/tasks').json() == []
        done = settled(client, base, start(client, base, 'explicit-rehearsal', mode='workflow', workflow_id=pid, inputs={}))
        assert done['status'] == 'succeeded' and done['outputs']['result'] == '可手动运行'
        assert len(client.get(base+'/tasks').json()) == 1
        with sqlite3.connect(settings.data_dir/'agent_platform.db') as db:
            assert db.execute("SELECT status FROM official_agent_jobs WHERE id='00000000-0000-4000-8000-000000000001'").fetchone() == ('queued',)
            assert db.execute("SELECT status FROM official_agent_jobs WHERE id='old-result'").fetchone() == ('completed',)
            assert db.execute("SELECT status,run_id FROM event_timers WHERE timer_key='restore-due'").fetchone() == ('pending', None)
            assert db.execute("SELECT event_count FROM event_subscriptions WHERE name='restore-subscription'").fetchone() == (0,)
        assert (old/'report.txt').read_text() == '历史报告'


def test_default_startup_still_recovers_queues_and_starts_automation_and_maintenance(tmp_path):
    settings = settings_for(tmp_path, automatic_tasks_enabled=False)
    _, old = seed(settings)
    # No new setting supplied: regular deployments retain automatic startup.
    normal = settings_for(tmp_path)
    assert normal.automatic_tasks_enabled is True
    app = create_app(normal)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer restore-test'
        assert client.get('/health').json()['automatic_tasks_enabled'] is True
        assert app.state.services.scheduler.health()['running'] is True
        assert app.state.services.event_automation._timer_task is not None
        # The deliberately missing request file is rejected during normal recovery.
        with sqlite3.connect(normal.data_dir/'agent_platform.db') as db:
            assert db.execute("SELECT status FROM official_agent_jobs WHERE id='00000000-0000-4000-8000-000000000001'").fetchone() == ('interrupted',)
            assert db.execute("SELECT count(*) FROM official_agent_jobs WHERE id='old-result'").fetchone() == (0,)
        for _ in range(200):
            if not old.exists():
                break
            time.sleep(.01)
        assert not old.exists()
