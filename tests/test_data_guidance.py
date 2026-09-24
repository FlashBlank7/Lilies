"""Newcomer guidance uses real calculations and resumable forms; models are doubles."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from tests.test_projects import configured, graph, node, edge, ref, settled, start  # noqa: F401
from agent_platform import data_guidance_code as code
from agent_platform.project_agent_tools import MemberRun, WorkspaceProjectTools


@pytest.fixture
def table(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path=Path('requirement-package/table.csv');path.parent.mkdir()
    path.write_text('unit,batch,temperature,target,after_test\nA,b1,10,good,1\nA,b1,10,good,1\nB,b2,,rare,0\nC,b2,20,good,1\n',encoding='utf-8')
    return path


def test_computed_facts_preserve_rows_and_report_unknowns(table):
    old=table.read_bytes()
    profile=code.main({'operation':'profile','source_path':str(table),'question':'不知道怎么分析'})
    facts=profile['facts']
    assert facts['rows']==4 and facts['duplicate_rows']==1
    temperature=next(c for c in facts['columns'] if c['name']=='temperature')
    assert temperature['missing']==1 and temperature['mean']==pytest.approx(40/3)
    labels=next(c for c in facts['columns'] if c['name']=='target')
    assert labels['smallest_value_count']==1
    answer={'needs_input':False,'questions':[],'analysis':'标签含义待确认；不能直接推断预测时点。','next_steps':['明确预测对象后使用分类训练。']}
    result=code.main({'operation':'export','profile':profile,'advice':answer,'answer':{'understanding':'暂不清楚'}})
    assert '代码计算的事实' in result['markdown'] and '待确认' in result['markdown']
    assert table.read_bytes()==old
    assert all(Path(a['file_path']).is_file() for a in result['artifacts'])
    table.write_text('changed')
    with pytest.raises(ValueError,match='改变'):code.main({'operation':'export','profile':profile,'advice':answer})


def test_table_feedback_and_input_isolation(table):
    first=code.main({'operation':'profile','source_path':str(table)})
    table.write_text('x,y\n1,2\n3,4\n')
    second=code.main({'operation':'profile','source_path':str(table)})
    assert first['facts']['source_sha256']!=second['facts']['source_sha256']
    assert first['folder']!=second['folder'] and second['facts']['rows']==2
    assert json.loads(Path(first['folder'],'facts.json').read_text())['rows']==4
    for invalid,message in [('a,a\n1,2','表头'),('a,b\n1,2,3','列数'),('a,b\n','数据')]:
        table.write_text(invalid)
        with pytest.raises(ValueError,match=message):code.main({'operation':'profile','source_path':str(table)})
    with pytest.raises(ValueError,match='当前项目'):code.main({'operation':'profile','source_path':'../private.csv'})


def test_excel_requires_explicit_sheet_when_ambiguous(table):
    from openpyxl import Workbook
    book=Workbook();book.active.append(['x','target']);book.active.append([2,0]);book.create_sheet('第二表').append(['other'])
    path=table.with_suffix('.xlsx');book.save(path)
    with pytest.raises(ValueError,match='多张'):code.main({'operation':'profile','source_path':str(path)})
    result=code.main({'operation':'profile','source_path':str(path),'sheet':'Sheet'})
    assert result['facts']['rows']==1


def transport(monkeypatch, ask):
    calls=[]
    def respond(request):
        body=json.loads(request.content);calls.append(body)
        answer={'needs_input':ask and len(calls)==1,'questions':['target表示什么？预测发生在何时？'] if ask and len(calls)==1 else [],
                'analysis':'**已知事实**：重复记录需要检查。\n\n**待确认**：标签含义和预测时点。未知时先保留资料准备建议。',
                'next_steps':['明确目标后调用项目已有分类训练，按批次隔离。']}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer,ensure_ascii=False)},'finish_reason':'stop'}],
                                       'usage':{'prompt_tokens':80,'completion_tokens':60}})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**{**kwargs,'transport':httpx.MockTransport(respond)}))
    return calls


def setup(client, base):
    wid=client.post(base+'/space/official-workflows/data-guidance').json()['workflow_id']
    upload=client.post(base+'/materials',files={'file':('quality.csv',b'batch,x,target\na,1,good\na,1,good\nb,3,rare\n','text/csv')}).json()
    client.put(base+'/agent-session',json={'provider':'api','base_url':'http://127.0.0.1:9001/v1','model':'guidance-test-only','api_key':'test-only','runtime_enabled':True}).raise_for_status()
    return wid,upload['path']


@pytest.mark.parametrize('ask',[False,True])
def test_workflow_analyze_pause_unknown_continue_and_download(configured,monkeypatch,ask):
    client,app,project,settings=configured;pid=project['id'];base='/api/v1/projects/'+pid
    wid,path=setup(client,base);calls=transport(monkeypatch,ask)
    task=settled(client,base,start(client,base,'first',workflow_id=wid,inputs={'source_path':path,'question':'帮我看看这份数据'}))
    assert task['status']==('waiting_input' if ask else 'succeeded'),task
    if ask:
        again=client.get(base+'/tasks/'+task['id']).json();run=again['runs'][0]
        assert 'target表示什么' in run['waiting_input']['context']
        assert run['waiting_input']['node_id']=='ask'
        url=f'{base}/tasks/{task["id"]}/runs/{run["id"]}/input'
        assert client.post(url,json={'node_id':'old','values':{},'resume':True}).status_code==409
        assert client.post(url,json={'node_id':'ask','values':{'understanding':'伪造选项'},'resume':True}).status_code==422
        # The tool can submit the user's explicit answer to the same task.
        tools=WorkspaceProjectTools(app.state.services,pid,app.state.services.local_agents)
        async def answer():
            result=await tools.call('workflow_run',{'action':'respond','task_id':task['id'],'run_id':run['id'],
                'node_id':'ask','inputs':{'understanding':'暂不清楚，先给已有分析'}})
            await app.state.services.projects.active[task['id']]
            return result
        result=asyncio.run(answer())
        task=settled(client,base,task)
        assert task['status']=='succeeded',task
        assert client.post(url,json={'node_id':'ask','values':{},'resume':True}).status_code==409
        assert len(task['runs'])==1 and len(calls)==2
        assert '暂不清楚' in calls[-1]['messages'][-1]['content']
    else:
        assert len(calls)==1
    assert '数据摸底' in task['outputs']['markdown']
    for artifact in task['outputs']['result']['artifacts']:
        response=client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path'])
        assert response.status_code==200
    assert not client.get(base+'/modeling/studies').json()


def test_waiting_form_validates_types_and_stop_rejects_answers(configured):
    client,app,project,_=configured;pid=project['id'];base='/api/v1/projects/'+pid
    graph(client,pid,[node('start','start',inputs=[{'name':'context','type':'string','default':'这个数值的单位是什么？'}]),
        node('human','human_input',title='补充',context=ref('start','context'),fields=[{'name':'n','label':'数值','type':'number'}]),
        node('end','end',outputs={'answer':ref('human','output')})],[edge('start','human'),edge('human','end')])
    task=settled(client,base,start(client,base,'pause'));run=task['runs'][0]
    assert run['waiting_input']['context']=='这个数值的单位是什么？'
    url=f'{base}/tasks/{task["id"]}/runs/{run["id"]}/input'
    assert client.post(url,json={'values':{'n':'not-number'},'resume':True}).status_code==422
    client.post(base+'/tasks/'+task['id']+'/stop').raise_for_status()
    assert client.post(url,json={'values':{'n':2},'resume':True}).status_code==409
    assert client.get(base+'/tasks/'+task['id']).json()['status']=='interrupted'
