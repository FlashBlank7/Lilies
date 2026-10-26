"""Real project/assistant entry + Docker offline adapters + real fake HTTP backend."""
import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.fixtures.media_http_backend import Backend, TEST_KEY
from tests.fixtures.media_workflow import workflow, inputs
from tests.test_projects import graph
from tests.test_users import signup, project
from tests.test_project_assistant_scope import call

pytestmark = pytest.mark.skipif(os.getenv('MEDIA_HTTP_TEST_DOCKER') != '1', reason='requires real Docker offline code executor')


def wait(client, base, task):
    for _ in range(1800):
        result = client.get(base+'/tasks/'+task['id']).json()
        if result['status'] not in {'running','queued'}:
            return result
        time.sleep(.1)
    raise AssertionError(result)


def setup_client(client, backend, case, *, omit_log=False):
    _, headers = signup(client, 'business-'+str(uuid4())[:8])
    pid = project(client, headers, '影音 HTTP 验收')
    client.headers.update(headers)
    base = '/api/v1/projects/'+pid
    body = {'name':'影音服务', 'base_url':backend.base+'/'+case, 'allowed_paths':['/**'], 'allowed_methods':['GET','POST'], 'auth_header':'X-Test-Key', 'credential':TEST_KEY, 'idempotency_header':'Idempotency-Key'}
    response = client.post(base+'/http-connections',json=body)
    assert response.status_code==201,response.text
    cid = response.json()['id']
    graph(client,pid,**workflow(cid,omit_log=omit_log))
    client.put(base+'/space/workflows/'+pid+'/assistant-callable',json={'enabled':True}).raise_for_status()
    return pid,base,cid,headers


def run(client,pid,**kwargs):
    response=call(client,pid,'project_calls',action='start',workflow_id=pid,view='full',wait=False,request_key=str(uuid4()),**kwargs)
    assert response.status_code==200,response.text
    return wait(client,'/api/v1/projects/'+pid,response.json())


@pytest.fixture
def env(tmp_path):
    backend=Backend()
    settings=Settings(_env_file=None,api_token='acceptance-admin',data_dir=tmp_path/'data',workspace_root=tmp_path/'workspaces',
        model_egress_enabled=False,official_agent_egress_enabled=False,automatic_tasks_enabled=False,
        sandbox_image=os.getenv('SANDBOX_IMAGE','agent-platform-sandbox:pr3-ci-20261018'),
        platform_harness_network_egress_policy='allowlist',platform_harness_network_egress_allowlist=['127.0.0.1'])
    with TestClient(create_app(settings)) as client:
        yield client,backend,settings
    backend.close()


def assert_complete(result,backend,case,*,variant=False):
    assert result['status']=='succeeded',result
    analysis=result['outputs']['analysis']
    assert len(analysis['decisions'])==13
    prefix='V-' if variant else 'E-'
    assert {d['media_id'] for d in analysis['decisions'] if d['decision']=='candidate'} <= {prefix+str(n) for n in (1,10,11,13)}
    assert len(analysis['operations'])==4
    assert all(result['outputs']['verification'][k]==4 for k in ('file','monitor','download','log'))
    state=backend.snapshot(case)
    assert len(state['effects'])==16
    assert sum(not r['exists'] for r in state['rows']['nas'])==4
    assert all(check['passed'] for run in result['runs'] for checks in run['result_checks'].values() for check in checks)


@pytest.mark.parametrize('fault',['','transient_read','lost_ack','auth','schema','stale'])
def test_media_ordinary_project_assistant_contract(env,fault):
    client,backend,_=env
    backend.add_case('case',fault)
    pid,base,cid,_=setup_client(client,backend,'case')
    result=run(client,pid,inputs=inputs())
    if fault in {'auth','schema','stale'}:
        assert result['status']=='failed' and result['outputs']=={},result
        assert backend.snapshot('case')['effects']==[]
    else:
        assert_complete(result,backend,'case')
        expected=['candidate','keep_tag','not_all_watched','seeding','retention_period','cloud_pointer','unknown_user_state','ambiguous_or_missing_match','unknown_completion_time','candidate','candidate','retention_period','candidate']
        assert [d['decision'] for d in result['outputs']['analysis']['decisions']]==expected


@pytest.mark.parametrize('repair',[False,True])
def test_original_partial_failure_finishes_remaining_without_redelete(env,repair):
    client,backend,_=env
    backend.add_case('case','partial')
    pid,base,cid,_=setup_client(client,backend,'case')
    failed=run(client,pid,inputs=inputs())
    assert failed['status']=='failed',failed
    assert len(backend.snapshot('case')['effects'])==5
    backend.cases['case']['fault']=''
    if repair:
        revised = workflow(cid)
        revised['nodes'][-1]['config']['outputs']['repair'] = True
        graph(client,pid,**revised)
        client.put(base+'/space/workflows/'+pid+'/assistant-callable',json={'enabled':True}).raise_for_status()
        result=run(client,pid,inputs=inputs(),recovery_task_id=failed['id'])
    else:
        response=call(client,pid,'project_calls',action='resume',task_id=failed['id'])
        assert response.status_code==200,response.text
        result=wait(client,base,response.json())
    assert_complete(result,backend,'case')
    assert sum(row['path'].endswith('/nas/delete') for row in backend.audit)==4
    if repair:
        assert client.get(base+'/tasks/'+failed['id']).json()['status']=='failed'
    else:
        assert result['id']==failed['id']


def test_omitted_action_fails_even_when_executed_nodes_succeed(env):
    client,backend,_=env
    backend.add_case('case')
    pid,base,cid,_=setup_client(client,backend,'case',omit_log=True)
    failed=run(client,pid,inputs=inputs())
    assert failed['status']=='failed' and failed['outputs']=={},failed
    assert len(backend.snapshot('case')['effects'])==12
    checks=failed['runs'][0]['result_checks']['end']
    assert [c['name'] for c in checks if not c['passed']]==['处理日志']
    graph(client,pid,**workflow(cid))
    client.put(base+'/space/workflows/'+pid+'/assistant-callable',json={'enabled':True}).raise_for_status()
    result=run(client,pid,inputs=inputs(),recovery_task_id=failed['id'])
    assert_complete(result,backend,'case')
    assert sum(row['path'].endswith('/nas/delete') for row in backend.audit)==4


def test_different_service_ids_pages_and_fields(env):
    client,backend,_=env
    backend.add_case('variant',page_size=5,variant=True)
    pid,_,_,_=setup_client(client,backend,'variant')
    result=run(client,pid,inputs=inputs(variant=True))
    assert_complete(result,backend,'variant',variant=True)
    assert {op['manager_id'] for op in result['outputs']['analysis']['operations']}=={5701,5710,5711,5713}
