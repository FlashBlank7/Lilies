"""Observe request outcomes without reading personal conversations or invoking models."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_platform.connected_model import completion_events
from agent_platform.db import connect
from agent_platform.usage_learning import AutomaticSettings, patterns
from tests.test_users import platform, signup, project  # noqa: F401
from tests.test_project_sessions import configure, new, settled
from tests.test_official_agent import official, enable, FakeAgent, chat, wait  # noqa: F401

ADMIN = {'Authorization': 'Bearer admin-boot'}
BASE = '/api/v1/admin/improvements'


def test_duplicate_outcomes_keep_latest_observation_time():
    rows = [dict(id=key, root_id=root, project_id='p', project_name='P', resource_id='',
                 feature='chat_result', outcome=status, created=created)
            for key, root, status, created in [('b', 'b', 'error', 1), ('old-a', 'a', 'error', 2),
                                             ('c', 'c', 'completed', 3), ('new-a', 'a', 'error', 4)]]
    item = patterns([], [], list(reversed(rows)))[0]
    assert item['count'] == 2 and item['completed_after_failure'] == 0
    assert item['last_seen'] == '1970-01-01T00:00:04+00:00'
    assert [r['id'] for r in item['operations']] == ['b', 'new-a']


def test_api_outcomes_preserve_identity_ignore_stops_and_do_not_read_private_content(platform, monkeypatch):
    client, app = platform
    account, headers = signup(client, 'New employee')
    client.headers.update(headers)
    pid = project(client, headers); base = '/api/v1/projects/' + pid
    configure(client, base)
    path = base + '/conversations/' + new(client, base)
    mode = 'fail'

    async def stream(**kwargs):
        if mode == 'fail':
            raise RuntimeError('PRIVATE MODEL ERROR WITH CUSTOMER BODY')
        if mode == 'hold':
            await asyncio.Event().wait()
        for event in completion_events([{'type': 'text', 'text': 'PRIVATE ANSWER'}]):
            yield event

    monkeypatch.setattr(app.state.services.local_agents.connections, 'provider',
                        lambda *args, **kwargs: SimpleNamespace(stream=stream))
    for _ in range(2):
        assert client.post(path + '/messages', json={'message': 'PRIVATE EMPLOYEE MESSAGE'}).status_code == 202
        assert settled(client, path)['status'] == 'error'
    first = client.post(BASE + '/scan', headers=ADMIN)
    assert 'PRIVATE' not in first.text
    item = next(i for i in first.json()['items'] if i['kind'] == 'assistant_error')
    assert item['count'] == 2 and not item['automatic_eligible']
    assert item['completed_after_failure'] == 0 and item['tasks'] == []
    assert first.json()['sampled_interactions'] == 2
    assert client.get(BASE).status_code == 403
    mode = 'hold'
    assert client.post(path + '/messages', json={'message': '等待时停止'}).status_code == 202
    assert client.post(path + '/stop').status_code == 200
    assert settled(client, path)['status'] == 'interrupted'
    mode = 'complete'
    assert client.post(path + '/messages', json={'message': '另一个任务'}).status_code == 202
    assert settled(client, path)['status'] == 'idle'
    after = client.post(BASE + '/scan', headers=ADMIN).json()
    observed = next(i for i in after['items'] if i['id'] == item['id'])
    assert observed['completed_after_failure'] == 1 and observed['count'] == 2
    assert observed['status'] == 'new' and '不能据此判定已修复' in observed['limitation']
    assert after['sampled_interactions'] == 3
    with connect(app.state.services.product_usage.db) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM product_usage WHERE feature='chat_result'")]
    assert len(rows) == 4 and {r['user_id'] for r in rows} == {account['user']['id']}
    assert len({r['root_id'] for r in rows}) == 4
    assert all(r['tokens'] is None and r['seconds'] >= 0 for r in rows)
    assert {r['outcome'] for r in rows} == {'error', 'interrupted', 'completed'}
    assert 'PRIVATE' not in str(rows)
    brief = client.get(BASE + '/' + item['id'] + '/brief', headers=ADMIN)
    assert '不读取其他员工的会话' in brief.text and 'PRIVATE' not in brief.text
    # Storage failure in counters cannot prevent the real chat from finishing.
    def unavailable(*args, **kwargs):
        raise OSError('counter store unavailable')
    monkeypatch.setattr('agent_platform.product_usage.connect', unavailable)
    assert client.post(path + '/messages', json={'message': '正常完成'}).status_code == 202
    assert settled(client, path)['status'] == 'idle'


@pytest.mark.parametrize('kind', ['chat', 'generation'])
def test_official_failures_are_observed_without_automatic_retry(official, monkeypatch, kind):
    client, app, service = official
    _, headers = signup(client, 'Employee')
    pid = project(client, headers); enable(client, pid)
    path = chat(client, pid, headers)
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('PRIVATE PROVIDER FAILURE')

    monkeypatch.setattr(FakeAgent, 'turn', fail)
    for number in range(2):
        if kind == 'chat':
            r = client.post(path + '/messages', headers=headers, json={'message': 'PRIVATE TASK', 'request_key': str(number)})
            assert r.status_code == 202, r.text
            assert wait(client, path, headers)['status'] == 'error'
            assert client.post(path + '/messages', headers=headers,
                               json={'message': 'PRIVATE TASK', 'request_key': str(number)}).status_code == 202
        else:
            r = client.post(path + '/workflow-generation', headers=headers,
                            json={'instruction': 'PRIVATE REQUIREMENT', 'name': 'Test', 'request_key': str(number)})
            assert r.status_code == 202, r.text
            for _ in range(200):
                status = client.get(f'/api/v1/projects/{pid}/generation-jobs/' + r.json()['job_id'], headers=headers).json()
                if status['status'] == 'error':
                    break
                time.sleep(.02)
            assert status['status'] == 'error'
    scanned = client.post(BASE + '/scan', headers=ADMIN)
    assert 'PRIVATE' not in scanned.text
    items = [i for i in scanned.json()['items'] if i['kind'] == 'assistant_error']
    assert len(items) == 1 and items[0]['count'] == 2
    assert {r['feature'] for r in items[0]['operations']} == {kind + '_result'}
    assert len(calls) == 2
    learning = app.state.services.usage_learning
    client.portal.call(learning.save_settings, AutomaticSettings(enabled=True, project_ids=[pid]), {'id': 'root', 'role': 'admin'})
    client.portal.call(learning.automate)
    assert len(calls) == 2 and len(service.jobs()) == 2


def test_sampling_deduplicates_requests_and_excludes_maintenance_and_other_projects(platform):
    client, app = platform
    pid = project(client, ADMIN); other = project(client, ADMIN)
    usage = app.state.services.product_usage
    with connect(usage.db) as db:
        db.execute("INSERT INTO usage_findings(id,project_id,kind,payload,created,updated,active) VALUES(?,?,'operation_error',?,?,?,0)",
                   ('maintenance', pid, '{"id":"maintenance","kind":"operation_error"}', time.time(), time.time()))
        db.execute("INSERT INTO usage_handoffs(finding_id,user_id,conversation_id,status) VALUES('maintenance','root','private-maintenance','started')")
    for index in range(4):
        usage.record(key=str(index), user_id='root', project_id=pid, root_id='same-request', feature='chat_result', outcome='error')
    for feature in ('chat_result', 'generation_result', 'chat_error'):
        for index in range(2):
            usage.record(key=feature+str(index), user_id='root', project_id=pid,
                conversation_id='private-maintenance', root_id=feature+str(index), feature=feature,
                outcome='HTTP 503' if feature == 'chat_error' else 'error')
    usage.record(key='other', user_id='root', project_id=other, root_id='other', feature='chat_result', outcome='error')
    usage.record(key='blank', user_id='root', project_id=pid, feature='chat_result', outcome='error')
    first = client.post(BASE + '/scan', headers=ADMIN).json()
    assert not any(i['active'] for i in first['items'])
    # New outcome on this project makes two distinct failures, never six.
    usage.record(key='new-request', user_id='root', project_id=pid, root_id='new-request', feature='chat_result', outcome='failed')
    current = client.post(BASE + '/scan', headers=ADMIN).json()
    item = next(i for i in current['items'] if i['active'])
    assert item['kind'] == 'assistant_error' and item['project_id'] == pid and item['count'] == 2
    # Do not let old completed rows be counted as success after the new failure.
    for index in range(501):
        usage.record(key='completed'+str(index), user_id='root', project_id=other, root_id=str(index),
                     feature='generation_result', outcome='completed')
    report = client.post(BASE + '/scan', headers=ADMIN).json()
    assert report['sampled_interactions'] == 500 and report['interactions_truncated']
    assert not any(i['active'] for i in report['items'])
