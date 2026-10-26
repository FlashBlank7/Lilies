"""Use ordinary project accounts and real TCP HTTP, never an admin run bypass."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_users import platform as platform, signup, project  # noqa: F401
from tests.test_projects import graph, node, edge, ref, start, settled
from tests.test_project_assistant_scope import call
from tests.fixtures.media_http_backend import Backend, TEST_KEY


@pytest.fixture
def media():
    backend = Backend()
    backend.add_case('trial')
    yield backend
    backend.close()


def setup(client, media, *, idempotent=True):
    _, owner = signup(client, 'owner')
    pid = project(client, owner)
    client.headers.update(owner)
    base = '/api/v1/projects/' + pid
    body = {'name': '测试影音服务', 'base_url': media.base + '/trial',
            'allowed_paths': ['/**'], 'allowed_methods': ['GET', 'POST'],
            'auth_header': 'X-Test-Key', 'credential': TEST_KEY,
            'idempotency_header': 'Idempotency-Key' if idempotent else ''}
    response = client.post(base + '/http-connections', json=body)
    assert response.status_code == 201, response.text
    connection = response.json()
    assert TEST_KEY not in response.text
    return pid, base, connection, body


def simple(client, pid, cid, **kwargs):
    graph(client, pid, [node('start', 'start'), node('http', 'http_request', connection_id=cid, path='/emby/records', **kwargs), node('end', 'end', outputs={'result': ref('http', 'output')})], [edge('start', 'http'), edge('http', 'end')])


def test_connection_ordinary_user_assistant_and_secret_isolation(platform, media):
    client, app = platform
    pid, base, connection, body = setup(client, media)
    _, member = signup(client, 'member')
    assert client.post(base+'/access-members', json={'name': 'member'}).status_code == 200
    cid = connection['id']
    simple(client, pid, cid)
    client.headers.update(member)
    assert client.put(base+'/http-connections/'+cid, json={**body, 'expected_revision':1}).status_code == 403
    assert client.delete(base+'/http-connections/'+cid).status_code == 403
    listing = call(client, pid, 'project_connections', action='list')
    assert listing.status_code == 200 and TEST_KEY not in listing.text
    assert call(client, pid, 'project_connections', action='save').status_code == 422
    result = call(client, pid, 'project_calls', action='start', workflow_id=pid, request_key='assistant')
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'succeeded', result.text
    assert len(result.json()['outputs']['result']['Items']) == 3
    task = client.get(base+'/tasks/'+result.json()['id'])
    assert TEST_KEY not in task.text
    run_id = task.json()['runs'][0]['id']
    assert TEST_KEY not in client.get('/api/v1/runs/'+run_id).text
    assert TEST_KEY not in client.get('/api/v1/runs/'+run_id+'/events/list').text
    assert client.post('/api/v1/applications/'+pid+'/runs', json={'inputs':{}}).status_code == 403
    # A normal records write cannot mutate the separate connection table.
    client.put(base+'/records/http_connections/'+cid, json={'value': {'base_url':'https://evil.invalid'}, 'expected_revision':0})
    assert client.get(base+'/http-connections').json()[0]['base_url'] == body['base_url']


def test_scope_and_cross_project_denial(platform, media):
    client, _ = platform
    pid, base, connection, body = setup(client, media)
    other = client.post('/api/v1/projects', json={'name':'other'}).json()['id']
    simple(client, other, connection['id'])
    result = settled(client, '/api/v1/projects/'+other, start(client, '/api/v1/projects/'+other, 'foreign'))
    assert result['status'] == 'failed' and not media.audit
    assert client.put(base+'/http-connections/'+connection['id'], json={**body, 'allowed_paths':['/emby/records'], 'allowed_methods':['GET'], 'expected_revision':1}).status_code == 200
    for index, path in enumerate(['/nas/delete', '/../nas/delete', '//evil.invalid', '/emby/records%2f..%2fdelete']):
        graph(client,pid,[node('s','start'),node('h','http_request',connection_id=connection['id'],path=path),node('e','end')],[edge('s','h'),edge('h','e')])
        result = settled(client,base,start(client,base,'scope-'+str(index)))
        assert result['status'] == 'failed' and not media.audit


def partial_graph(client, pid, cid, *, checks=True, omit=None, recovery=False):
    actions = [('delete','/nas/delete',{'path':'/media/10/video.mkv','etag':'file-10-v1'}),
               ('unmonitor','/radarr/unmonitor',{'id':710}),
               ('remove','/qbittorrent/remove',{'hash':'H-10','delete_files':False}),
               ('log','/yamtrack/log',{'media_id':'E-10','event':'cleaned'})]
    nodes = [node('s','start')]
    if recovery:
        nodes.append(node('original', 'variable_assigner', assignments={'input':ref('$recovery','inputs')}))
    nodes += [node(name,'http_request',connection_id=cid,path=path,method='POST',body=body) for name,path,body in actions if name != omit]
    nodes += [node('read','http_request',connection_id=cid,path='/radarr/records',query={'page':4}),
              node('end','end',outputs={'markdown':'本次运行结果'}, checks=[{'name':'取消监控','value':ref('read','output','records',0,'monitored'),'expected':False}] if checks else [])]
    graph(client,pid,nodes,[edge(a['id'],b['id']) for a,b in zip(nodes,nodes[1:])])


def test_partial_resume_retains_confirmed_delete_and_concurrent_resume(platform, media):
    client, _ = platform
    pid,base,connection,_ = setup(client,media)
    media.cases['trial']['fault']='partial'
    partial_graph(client,pid,connection['id'],checks=False)
    original=start(client,base,'once')
    failed=settled(client,base,original)
    assert failed['status']=='failed', failed
    assert len(media.cases['trial']['effects'])==1
    assert len([r for r in media.audit if r['path'].endswith('/unmonitor')])==3
    assert start(client,base,'once')['id']==failed['id']
    media.cases['trial']['fault']=''
    with ThreadPoolExecutor(2) as pool:
        responses=list(pool.map(lambda _: client.post(base+'/tasks/'+failed['id']+'/resume',json={'message':''}),range(2)))
    assert sorted(r.status_code for r in responses)==[202,409]
    result=settled(client,base,failed)
    assert result['status']=='succeeded',result
    assert len(media.cases['trial']['effects'])==4
    assert sum(r['path'].endswith('/nas/delete') for r in media.audit)==1
    assert result['runs'][0]['id']==failed['runs'][0]['id']


def test_lost_ack_retry_is_idempotent(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    media.cases['trial']['fault']='lost_ack'
    partial_graph(client,pid,c['id'],checks=False)
    done=settled(client,base,start(client,base,'lost'))
    assert done['status']=='succeeded',done
    assert len(media.cases['trial']['effects'])==4
    assert sum(r['path'].endswith('/nas/delete') for r in media.audit)==2


def test_result_checks_cannot_be_ignored_or_use_missing_values(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    for index, actual in enumerate([ref('read','output','Items',0,'Id'), ref('read','output','absent')]):
        nodes=[node('s','start'),node('read','http_request',connection_id=c['id'],path='/emby/records'),node('end','end',outputs={'markdown':'must not appear'},checks=[{'name':'已删除','value':actual,'expected':False,'failure_message':'仍未删除'}])]
        nodes[-1]['error_strategy']='continue'
        graph(client,pid,nodes,[edge('s','read'),edge('read','end')])
        result=settled(client,base,start(client,base,'check-'+str(index)))
        assert result['status']=='failed' and result['outputs']=={},result
        assert result['runs'][0]['result_checks']['end'][0]['passed'] is False


def test_rotation_allowed_scope_change_and_disable_block_resume(platform,media):
    client,_=platform
    pid,base,c,body=setup(client,media)
    media.cases['trial']['fault']='partial'
    partial_graph(client,pid,c['id'],checks=False)
    failed=settled(client,base,start(client,base,'failed'))
    cid=c['id']
    update={**body,'enabled':False,'expected_revision':1}
    assert client.put(base+'/http-connections/'+cid,json=update).status_code==200
    before=len(media.audit)
    client.post(base+'/tasks/'+failed['id']+'/resume',json={'message':''}).raise_for_status()
    assert settled(client,base,failed)['status']=='failed'
    assert len(media.audit)==before
    update.update(enabled=True,expected_revision=2,allowed_paths=['/radarr/**'])
    client.put(base+'/http-connections/'+cid,json=update).raise_for_status()
    client.post(base+'/tasks/'+failed['id']+'/resume',json={'message':''}).raise_for_status()
    assert '范围已修改' in settled(client,base,failed)['error']
    assert len(media.audit)==before
    update.update(expected_revision=3,allowed_paths=['/**'],credential=TEST_KEY)
    client.put(base+'/http-connections/'+cid,json=update).raise_for_status()
    media.cases['trial']['fault']=''
    client.post(base+'/tasks/'+failed['id']+'/resume',json={'message':''}).raise_for_status()
    assert settled(client,base,failed)['status']=='succeeded'
    assert len(media.cases['trial']['effects'])==4


def test_repair_run_keeps_original_input_and_write_identity(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    media.cases['trial']['fault']='partial'
    partial_graph(client,pid,c['id'],checks=False)
    failed=settled(client,base,start(client,base,'old',inputs={'original_target':'E-10'}))
    assert failed['status']=='failed'
    partial_graph(client,pid,c['id'],checks=False,recovery=True)
    media.cases['trial']['fault']=''
    done=settled(client,base,start(client,base,'repair',recovery_task_id=failed['id']))
    assert done['status']=='succeeded',done
    assert done['id']!=failed['id'] and done['runs'][0]['id']!=failed['runs'][0]['id']
    assert len(media.cases['trial']['effects'])==4
    assert sum(r['path'].endswith('/nas/delete') for r in media.audit)==1
    original=client.get(base+'/tasks/'+failed['id']).json()
    assert original['status']=='failed' and original['outputs']=={}
    run=client.get('/api/v1/runs/'+done['runs'][0]['id']).json()
    assert run['state']['outputs']['original']['output']['input']=={'original_target':'E-10'}
    other=client.post('/api/v1/projects',json={'name':'other'}).json()['id']
    assert client.post('/api/v1/projects/'+other+'/tasks',json={'request_key':'invalid','recovery_task_id':failed['id']}).status_code==404


def test_unknown_write_is_not_resent_and_readback_can_confirm(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media,idempotent=False)
    media.cases['trial']['fault']='lost_ack'
    def flow(query=None):
        nodes=[node('s','start'),node('delete','http_request',connection_id=c['id'],path='/nas/delete',method='POST',headers={'Idempotency-Key':'fixed-server-key'},body={'path':'/media/10/video.mkv','etag':'file-10-v1'},outcome_query=query),node('e','end',outputs={'result':ref('delete','output')})]
        graph(client,pid,nodes,[edge('s','delete'),edge('delete','e')])
    flow()
    failed=settled(client,base,start(client,base,'uncertain'))
    assert failed['status']=='failed' and '待确认操作' in failed['error'],failed
    assert len(media.cases['trial']['effects'])==1 and len(media.audit)==1
    client.post(base+'/tasks/'+failed['id']+'/resume',json={}).raise_for_status()
    assert settled(client,base,failed)['status']=='failed' and len(media.audit)==1
    flow({'path':'/nas/status','query':{'path':'/media/10/video.mkv'},'result_path':['exists'],'completed_value':False,'not_applied_value':True})
    done=settled(client,base,start(client,base,'confirmed',recovery_task_id=failed['id']))
    assert done['status']=='succeeded',done
    assert len(media.cases['trial']['effects'])==1
    assert sum(r['method']=='POST' for r in media.audit)==1


def test_redirect_scope_and_echoed_credentials(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    for i,path in enumerate(['/nas/echo','/nas/redirect']):
        nodes=[node('s','start'),node('h','http_request',connection_id=c['id'],path=path,query={'to':media.base+'/other/nas/echo'}),node('e','end',outputs={'result':ref('h','output'),'headers':ref('h','headers')})]
        graph(client,pid,nodes,[edge('s','h'),edge('h','e')])
        result=settled(client,base,start(client,base,'redirect-'+str(i)))
        if i==0:
            assert result['status']=='succeeded' and TEST_KEY not in str(result),result
        else:
            assert result['status']=='failed' and '重定向超出' in result['error'],result
    assert not any('/other/' in record['path'] for record in media.audit)


def test_503_reads_retry_401_does_not(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    simple(client,pid,c['id'],query={'page':2})
    for fault,count in [('transient_read',2),('auth',1)]:
        media.cases['trial']['fault']=fault
        before=len(media.audit)
        result=settled(client,base,start(client,base,fault))
        assert result['status']==('succeeded' if fault=='transient_read' else 'failed'),result
        assert len(media.audit)-before==count


def test_nested_connection_and_failed_checks_not_hidden(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    child=client.post(base+'/members',json={'name':'child'}).json()['id']
    simple(client,child,c['id'])
    graph(client,pid,[node('s','start'),node('child','tool',tool_name='workflow:'+child,input={}),node('e','end',outputs={'result':ref('child','output')})],[edge('s','child'),edge('child','e')])
    result=settled(client,base,start(client,base,'nested'))
    assert result['status']=='succeeded' and len(result['runs'])==2,result
    graph(client,child,[node('s','start'),node('e','end',checks=[{'name':'遗漏日志','value':False,'expected':True}])],[edge('s','e')])
    nodes=[node('s','start'),node('child','tool',tool_name='workflow:'+child,input={}),node('e','end',outputs={'markdown':'wrong'})]
    nodes[1]['error_strategy']='continue'
    graph(client,pid,nodes,[edge('s','child'),edge('child','e')])
    failed=settled(client,base,start(client,base,'failed-nested'))
    assert failed['status']=='failed' and failed['outputs']=={},failed


def test_restart_uses_persisted_nested_and_http_progress(tmp_path,media):
    import json
    import socket
    import subprocess
    import sys
    import time
    import httpx
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    settings={'api_token':'restart-admin','data_dir':str(tmp_path/'data'),'workspace_root':str(tmp_path/'ws'),
              'model_egress_enabled':False,'official_agent_egress_enabled':False,'scheduler_poll_seconds':3600}
    command=[sys.executable,'-c',"import json,sys,uvicorn; from agent_platform.api import create_app; from agent_platform.config import Settings; uvicorn.run(create_app(Settings(_env_file=None,**json.loads(sys.argv[1]))),host='127.0.0.1',port=int(sys.argv[2]),log_level='error')",json.dumps(settings),str(port)]
    def boot():
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(200):
            if process.poll() is not None:
                raise AssertionError('API process exited during startup')
            try:
                if httpx.get(f'http://127.0.0.1:{port}/api/v1/me').status_code==401:
                    return process
            except httpx.ConnectError:
                pass
            time.sleep(.05)
        process.kill();process.wait()
        raise AssertionError('API startup timeout')
    process=boot()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=15) as client:
            pid,base,c,_=setup(client,media)
            media.cases['trial']['fault']='partial'
            partial_graph(client,pid,c['id'],checks=False)
            failed=settled(client,base,start(client,base,'restart'))
            assert failed['status']=='failed' and len(media.cases['trial']['effects'])==1
            process.kill();process.wait(timeout=5)
            media.cases['trial']['fault']=''
            process=boot()
            response=client.post(base+'/tasks/'+failed['id']+'/resume',json={})
            assert response.status_code==202,response.text
            done=settled(client,base,failed)
            assert done['status']=='succeeded',done
            assert done['runs'][0]['id']==failed['runs'][0]['id']
            assert len(media.cases['trial']['effects'])==4
            assert sum(r['path'].endswith('/nas/delete') for r in media.audit)==1
    finally:
        process.terminate();process.wait(timeout=10)


def test_permission_revocation_blocks_resume(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    owner={'Authorization':client.headers['Authorization']}
    account,member=signup(client,'operator')
    client.post(base+'/access-members',json={'name':'operator'}).raise_for_status()
    media.cases['trial']['fault']='partial'
    partial_graph(client,pid,c['id'],checks=False)
    client.headers.update(member)
    failed=settled(client,base,start(client,base,'revoked'))
    assert failed['status']=='failed'
    client.delete(base+'/access-members/'+account['user']['id'],headers=owner).raise_for_status()
    before=len(media.audit)
    assert client.post(base+'/tasks/'+failed['id']+'/resume',json={}).status_code==404
    assert call(client,pid,'project_calls',action='resume',task_id=failed['id']).status_code==404
    assert len(media.audit)==before


def test_check_missing_value_with_optional_ref_is_not_success(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media)
    nodes=[node('s','start'),node('end','end',checks=[{'name':'必须有回读值','value':{'$ref':{'node_id':'$inputs','path':['missing'],'optional':True}},'operator':'not_equals','expected':False}])]
    graph(client,pid,nodes,[edge('s','end')])
    failed=settled(client,base,start(client,base,'missing'))
    assert failed['status']=='failed'


def test_uncertain_nested_write_cannot_be_hidden_by_multiple_callers(platform,media):
    client,_=platform
    pid,base,c,_=setup(client,media,idempotent=False)
    middle=client.post(base+'/members',json={'name':'middle'}).json()['id']
    child=client.post(base+'/members',json={'name':'child'}).json()['id']
    media.cases['trial']['fault']='lost_ack'
    graph(client,child,[node('s','start'),node('write','http_request',connection_id=c['id'],method='POST',path='/nas/delete',headers={'Idempotency-Key':'nested-server-key'},body={'path':'/media/10/video.mkv','etag':'file-10-v1'}),node('e','end')],[edge('s','write'),edge('write','e')])
    for parent,target in [(middle,child),(pid,middle)]:
        nodes=[node('s','start'),node('child','tool',tool_name='workflow:'+target,input={}),node('e','end',outputs={'markdown':'must not succeed'})]
        nodes[1]['error_strategy']='continue'
        graph(client,parent,nodes,[edge('s','child'),edge('child','e')])
    failed=settled(client,base,start(client,base,'uncertain-nested'))
    assert failed['status']=='failed' and failed['outputs']=={},failed
    assert len(media.cases['trial']['effects'])==1
    client.post(base+'/tasks/'+failed['id']+'/resume',json={}).raise_for_status()
    assert settled(client,base,failed)['status']=='failed'
    assert len(media.cases['trial']['effects'])==1
    assert sum(r['method']=='POST' for r in media.audit)==1


@pytest.mark.parametrize('fault',['permanent_read','lost_ack'])
def test_legacy_standalone_resume_preserves_receipts_and_unknown_writes(platform,media,fault):
    import time
    client,_=platform
    client.headers['Authorization']='Bearer admin-boot'
    wid=client.post('/api/v1/applications',json={'name':'legacy standalone'}).json()['id']
    media.cases['trial']['fault']=fault
    auth={'X-Test-Key':TEST_KEY,'Idempotency-Key':'legacy-fixed'}
    graph(client,wid,[node('s','start'),node('write','http_request',method='POST',url=media.base+'/trial/nas/delete',headers=auth,body={'path':'/media/10/video.mkv','etag':'file-10-v1'}),node('read','http_request',url=media.base+'/trial/emby/records',headers={'X-Test-Key':TEST_KEY},query={'page':2}),node('e','end')],[edge('s','write'),edge('write','read'),edge('read','e')])
    response=client.post('/api/v1/applications/'+wid+'/runs',json={'use_draft':True})
    assert response.status_code==202,response.text
    path='/api/v1/runs/'+response.json()['run_id']
    def done():
        for _ in range(300):
            result=client.get(path).json()
            if result['status'] not in {'queued','running'}:
                return result
            time.sleep(.01)
        raise AssertionError(result)
    failed=done()
    assert failed['status']=='failed',failed
    assert len(media.cases['trial']['effects'])==1
    media.cases['trial']['fault']=''
    response=client.post(path+'/resume',json={'values':{}})
    assert response.status_code==200,response.text
    result=done()
    assert result['status']==('succeeded' if fault=='permanent_read' else 'failed'),result
    assert sum(r['path'].endswith('/nas/delete') for r in media.audit)==1


@pytest.mark.parametrize('logical', [False, True])
def test_explicit_real_binding_targets_selected_connection(platform, media, logical):
    client, _ = platform
    pid, base, old, body = setup(client, media)
    media.add_case('selected')
    other=client.post(base+'/http-connections',json={**body,'name':'选定目标','base_url':media.base+'/selected'}).json()
    alias='media' if logical else old['id']
    config={'service_ref':alias} if logical else {'connection_id':alias}
    graph(client,pid,[node('s','start'),node('h','http_request',path='/emby/records',**config),
        node('e','end',outputs={'data':ref('h','output')})],[edge('s','h'),edge('h','e')])
    response=client.post(base+'/tasks',json={'request_key':'bound','inputs':{},'service_bindings':{alias:other['id']}})
    assert response.status_code==202,response.text
    task=settled(client,base,response.json())
    assert task['status']=='succeeded',task
    assert [r['path'] for r in media.audit]==['/selected/emby/records']
