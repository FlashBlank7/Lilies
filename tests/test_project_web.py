"""Public sources use the project boundary without a workflow or live provider."""
import hashlib

import httpx
import pytest

from agent_platform import project_web as web
from tests.test_projects import configured  # noqa: F401
from tests.test_users import platform, signup, project  # noqa: F401


@pytest.fixture
def sources(monkeypatch):
    requests=[]
    state={'body':b'<html><title>Example</title><script>privateScript()</script><p>First fact.</p><a href="/more">More</a></html>',
           'type':'text/html; charset=utf-8','status':200,'location':'','addresses':['93.184.215.14']}
    async def resolve(host, port): return state['addresses']
    def respond(request):
        requests.append(request)
        return httpx.Response(state['status'],content=state['body'],
            headers={'content-type':state['type'],**({'location':state['location']} if state['location'] else {})})
    monkeypatch.setattr(web,'addresses',resolve)
    monkeypatch.setattr(web,'Client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(respond),**kw))
    return state, requests


def read(client,pid,url='https://public.test/page',headers=None):
    return client.post('/api/v1/projects/'+pid+'/agent-tools',headers=headers,
        json={'name':'project_web','arguments':{'url':url}})


def test_read_freezes_source_pins_host_and_never_creates_task(configured,sources):
    client,_,p,settings=configured;state,requests=sources
    result=read(client,p['id']);assert result.status_code==200,result.text
    first=result.json();root=settings.workspace_root/p['id']
    assert first['title']=='Example' and 'First fact.' in first['preview']
    assert 'privateScript' not in first['preview'] and first['links']==['https://public.test/more']
    assert first['sha256']==hashlib.sha256(state['body']).hexdigest()
    assert (root/first['original_path']).read_bytes()==state['body']
    request=requests[0]
    assert request.url.host=='93.184.215.14'
    assert request.headers['host']=='public.test' and request.extensions['sni_hostname']=='public.test'
    before=(root/first['source_path']).read_bytes()
    state['body']='<p>新的结论，保留0.77。</p>'.encode()
    second=read(client,p['id']).json()
    assert second['source_path']!=first['source_path'] and second['sha256']!=first['sha256']
    assert '新的结论' in second['preview'] and (root/first['source_path']).read_bytes()==before
    for artifact in first['artifacts']:
        assert client.get('/api/v1/applications/'+p['id']+'/workspace/files/'+artifact['file_path']).status_code==200
    assert client.get('/api/v1/projects/'+p['id']+'/tasks').json()==[]


def test_large_preview_keeps_full_text_for_later_read(configured,sources):
    client,_,p,settings=configured;state,_=sources
    state['type']='text/plain';state['body']=('资料很长。'*2000).encode()
    out=read(client,p['id']).json()
    assert out['preview_truncated'] and len(out['preview'])==4000
    assert len((settings.workspace_root/p['id']/out['source_path']).read_text())==10000


@pytest.mark.parametrize(('change','url','message'),[
    ({},'file:///etc/passwd','公开HTTP'),
    ({},'https://name:password@public.test/','嵌入账号密码'),
    ({'addresses':['127.0.0.1']},'http://public.test/','内网或保留'),
    ({'addresses':['169.254.169.254']},'http://public.test/','内网或保留'),
    ({'addresses':['::ffff:192.168.1.1']},'http://public.test/','内网或保留'),
    ({'status':404},'https://public.test/','HTTP 404'),
    ({'body':b'x'*(web.MAX_BYTES+1),'type':'text/plain'},'https://public.test/','超过5 MB'),
    ({'type':'image/jpeg'},'https://public.test/','文字网页或PDF'),
    ({'body':b'<script>hidden()</script>'},'https://public.test/','没有可读取的文字'),
])
def test_failed_read_gives_reason_and_no_partial_source(configured,sources,change,url,message):
    client,_,p,settings=configured;state,_=sources;state.update(change)
    result=read(client,p['id'],url)
    assert result.status_code==422,result.text
    assert message in result.text
    assert not list((settings.workspace_root/p['id']/'results').glob('web-source-*'))


def test_each_redirect_rechecks_network_permission_and_private_address(configured,sources,monkeypatch):
    client,app,p,_=configured;state,requests=sources
    state.update(status=302,location='https://other.test/page')
    harness=app.state.services.harness
    harness.network_egress_policy='allowlist';harness.network_egress_allowlist=['public.test']
    response=read(client,p['id'])
    assert response.status_code==422 and '网络配置不允许' in response.text
    assert len(requests)==1
    harness.network_egress_policy='full'
    async def resolve(host,port):return ['127.0.0.1'] if host=='other.test' else ['93.184.215.14']
    monkeypatch.setattr(web,'addresses',resolve)
    assert '内网或保留' in read(client,p['id']).text and len(requests)==2
    harness.network_egress_policy='none'
    assert '网络配置不允许' in read(client,p['id']).text and len(requests)==2


def test_public_redirect_preserves_original_and_final_source(configured,monkeypatch):
    client,_,p,_=configured
    visited=[]
    async def resolve(host,port):
        return ['93.184.215.14' if host=='public.test' else '93.184.215.15']
    def respond(request):
        visited.append(request)
        if request.headers['host']=='public.test':
            return httpx.Response(302,headers={'location':'https://docs.test/guide/page'})
        return httpx.Response(200,headers={'content-type':'text/html'},
            content=b'<title>Guide</title><p>Read this.</p><a href="../next">Next</a>')
    monkeypatch.setattr(web,'addresses',resolve)
    monkeypatch.setattr(web,'Client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(respond),**kw))
    response=read(client,p['id']);assert response.status_code==200,response.text
    out=response.json()
    assert out['requested_url']=='https://public.test/page'
    assert out['url']=='https://docs.test/guide/page' and out['links']==['https://docs.test/next']
    assert [request.url.host for request in visited]==['93.184.215.14','93.184.215.15']
    assert visited[-1].extensions['sni_hostname']=='docs.test'


def test_pdf_is_saved_without_claiming_to_have_read_pages(configured,sources):
    client,_,p,settings=configured;state,_=sources
    state.update(type='application/pdf',body=b'%PDF-1.7\ntransport fixture, not a parsed PDF')
    out=read(client,p['id']).json()
    assert out['source_path'].endswith('.pdf') and not out['preview']
    assert '尚未提取正文或核对页码' in out['note']
    assert (settings.workspace_root/p['id']/out['source_path']).read_bytes()==state['body']


def test_source_and_download_stay_inside_employee_project(platform,sources):
    client,_=platform
    _,a=signup(client,'网页甲');_,b=signup(client,'网页乙');pid=project(client,a)
    result=read(client,pid,headers=a);assert result.status_code==200,result.text
    path='/api/v1/applications/'+pid+'/workspace/files/'+result.json()['source_path']
    assert client.get(path,headers=a).status_code==200
    assert client.get(path,headers=b).status_code==404
    assert read(client,pid,headers=b).status_code==404
