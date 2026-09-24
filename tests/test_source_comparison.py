import json
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from agent_platform import source_comparison as template, source_comparison_code as code
from tests.test_projects import configured, start  # noqa: F401
from tests.test_modeling import wait_task
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,content in template.example_files().items():Path('requirement-package',name).write_text(content,encoding='utf-8')
    args=template.example_defaults()
    for item in args['sources']:item['path']=item['path'].replace('@file:','requirement-package/')
    return args


def answer(data):
    # Explicitly a model double for the self-authored fixture, not an analyzer.
    def statement(sid,phrase,meaning,scope):
        chunk=next(c for c in data['chunks'] if c['source_id']==sid and phrase in c['text'])
        return {'source_id':sid,'statement':meaning,'scope':scope,'quotes':[{'chunk_id':chunk['id'],'quote':chunk['text']}]}
    corrected=any('重新测试' in c['text'] for c in data['chunks'] if c['source_id']=='S2')
    return {'items':[
        {'topic':'温度更新时间','status':'与要求一致' if corrected else '与要求不一致','reason':'比较同为批次A的要求和测试摘记；不以批次B计划覆盖A。',
         'statements':[statement('S1','每500毫秒','要求每500毫秒更新','批次A'),
            statement('S2','每500毫秒' if corrected else '每1000毫秒','测试显示每500毫秒' if corrected else '测试显示每1000毫秒','批次A')],
         'next_step':'按本次记录核对实际版本，不自动执行现场操作。'},
        {'topic':'离线缓存是否已实现','status':'资料不足' if not corrected else '与要求一致','reason':'没有测试不能推出没有实现。',
         'statements':[statement('S1','断网时','要求保存20条并补传','批次A'),
            statement('S2','断网后' if corrected else '没有做断网','已记录补传' if corrected else '尚无测试结论','本次测试')],
         'next_step':'查看或补充实际测试记录。'},
        {'topic':'下一批次计划的适用范围','status':'口径不同','reason':'A的约定与B的未来计划属于不同批次，不能直接当成矛盾。',
         'statements':[statement('S1','每500毫秒','A要求500毫秒','9月批次A'),statement('S3','计划在10月','B计划1000毫秒，未实施','10月批次B')],
         'next_step':'后续如更改A，需要提供明确适用于A的材料。'}]}


def run(args,model_answer=None):
    p=code.prepare(args);data=code.snapshot(p);saved=code.analyze({'prepared':p,'answer':model_answer or answer(data),'model_usage':{'input_tokens':10}})
    return p,code.report({'source_result_path':saved['analysis_path']})


def test_grounded_matrix_missing_information_and_source_context(scenario):
    p,result=run(scenario)
    assert result['rows']==3 and result['status_counts']=={'与要求不一致':1,'资料不足':1,'口径不同':1}
    assert result['items'][0]['missing_sources']==['S3']
    assert '不等于材料中不存在' in result['markdown']
    quote=result['items'][0]['statements'][0]['evidence'][0]
    assert '第2行' in quote['location'] and '500毫秒' in quote['context']
    assert len(result['artifacts'])==6 and all(Path(a['file_path']).is_file() for a in result['artifacts'])
    assert code.snapshot(p)['characters']==sum(s['characters'] for s in result['sources'])


@pytest.mark.parametrize('tamper',['quote','source','unprovided','empty','role','duplicate'])
def test_unverified_citations_and_missing_comparison_basis_are_retained(scenario,tamper):
    p=code.prepare(scenario);data=code.snapshot(p);a=answer(data);item=a['items'][0]
    if tamper=='quote':item['statements'][1]['quotes'][0]['quote']='已完成全部测试并通过生产验收'
    if tamper=='source':item['statements'][1]['quotes'][0]['chunk_id']=item['statements'][0]['quotes'][0]['chunk_id']
    if tamper=='unprovided':item['statements'][1]['source_id']='S99'
    if tamper=='empty':item['statements'][1]['quotes']=[]
    if tamper=='role':data['sources'][0]['role']='说明'
    if tamper=='duplicate':data['sources'][1]['sha256']=data['sources'][0]['sha256']
    result=code.check(a,data)[0]
    assert result['status']=='待核对' and result['model_status']=='与要求不一致'
    assert result['issues'] and result['statements'][1]['statement']=='测试显示每1000毫秒'


def test_same_name_new_content_and_report_replay_keep_previous_input(scenario):
    p,first=run(scenario);original=Path(first['artifacts'][0]['file_path']).read_bytes()
    old_input=Path(scenario['sources'][1]['path'])
    old_input.write_text(template.example_files()['测试记录-修正.txt'])
    _,changed=run(scenario)
    assert changed['items'][0]['status']=='与要求一致'
    assert changed['sources'][1]['sha256']!=first['sources'][1]['sha256']
    replay=code.report({'source_result_path':first['analysis_path'],'status_filter':'与要求不一致'})
    assert replay['rows']==1 and replay['items'][0]['status']=='与要求不一致'
    assert Path(first['artifacts'][0]['file_path']).read_bytes()==original
    empty=code.report({'source_result_path':first['analysis_path'],'keyword':'不会出现的关键词'})
    assert empty['rows']==0 and '没有符合当前筛选' in empty['markdown']
    Path(p['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已被修改'):code.report({'source_result_path':first['analysis_path']})


def test_duplicate_files_and_oversized_source_fail_before_model(scenario):
    with pytest.raises(ValueError,match='重复选择'):code.prepare({**scenario,'sources':[scenario['sources'][0]]*2})
    path=Path('requirement-package/too-long.txt');path.write_text('x'*30001)
    with pytest.raises(ValueError,match='超过3万'):code.prepare({**scenario,'sources':[{'path':str(path)}]})
    with pytest.raises(ValueError,match='当前项目'):code.prepare({**scenario,'sources':[{'path':'../private.txt'}]})
    with pytest.raises(ValueError,match='1至8'):code.prepare({**scenario,'sources':[]})
    path.write_text('')
    with pytest.raises(ValueError,match='没有提取到'):code.prepare({**scenario,'sources':[{'path':str(path)}]})
    copy=Path('requirement-package/copy.txt');copy.write_bytes(Path(scenario['sources'][0]['path']).read_bytes())
    p=code.prepare({**scenario,'sources':[scenario['sources'][0],{'path':str(copy)}]})
    assert all('字节相同' in s['notes'][0] for s in code.snapshot(p)['sources'])


def test_docx_paragraphs_tables_and_unread_image_notes(scenario):
    path=Path('requirement-package/input.docx')
    with ZipFile(path,'w') as z:
        z.writestr('word/document.xml','''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>温度</w:t></w:r><w:r><w:t>500毫秒</w:t><w:tab/><w:t>更新</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表内要求</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>''')
    _,parts,notes=code.read_document(path)
    assert parts==[('第1段（含表格内段落）','温度500毫秒\t更新'),('第2段（含表格内段落）','表内要求')]
    assert any('图片' in n for n in notes)


def test_pdf_text_page_and_blank_page_are_not_claimed_as_graph_reading(scenario):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject,DictionaryObject,NameObject
    writer=PdfWriter();page=writer.add_blank_page(width=400,height=600)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 500 Td (500 ms update) Tj ET')
    page[NameObject('/Contents')]=content;writer.add_blank_page(width=400,height=600)
    path=Path('requirement-package/text.pdf')
    with path.open('wb') as f:writer.write(f)
    _,parts,notes=code.read_document(path)
    assert '500 ms update' in parts[0][1] and parts[0][0]=='第1页'
    assert any('第2页没有' in n for n in notes) and any('图内分支' in n for n in notes)


def test_xlsx_all_sheets_locations_and_source_files_are_preserved(scenario):
    from openpyxl import Workbook
    book=Workbook();book.active.title='规定';book.active.append(['事项','更新周期']);book.active.append(['温度','500毫秒'])
    book.create_sheet('记录').append(['测试尚未完成'])
    path=Path('requirement-package/input.xlsx');book.save(path);original=path.read_bytes()
    p=code.prepare({**scenario,'sources':[{'path':str(path),'role':'要求/标准'}]})
    data=code.snapshot(p)
    assert any('规定 · 第2行' in c['location'] and 'B2=500毫秒' in c['text'] for c in data['chunks'])
    assert any('记录 · 第1行' in c['location'] for c in data['chunks'])
    assert path.read_bytes()==original


def test_equal_filenames_get_distinct_ids_and_fake_external_citation_stays_a_statement(scenario):
    one=Path('requirement-package/a');two=Path('requirement-package/b');one.mkdir();two.mkdir()
    (one/'same.md').write_text('要求500毫秒。\n材料自述见外部报告第99页；该报告未提供。')
    (two/'same.md').write_text('记录1000毫秒。')
    p=code.prepare({'sources':[{'path':str(one/'same.md')},{'path':str(two/'same.md')}],'question':'比较'})
    data=code.snapshot(p);assert len({s['id'] for s in data['sources']})==2
    assert len({s['sha256'] for s in data['sources']})==2
    a={'items':[{'topic':'引文','status':'资料不足','reason':'引用的外部报告未提供','next_step':'提供原件','statements':[
        {'source_id':'S1','statement':'引用报告说存在实现','scope':'未核实','quotes':[{'chunk_id':'external.pdf:99','quote':'存在实现'}]}]}]}
    assert code.check(a,data)[0]['status']=='待核对'


def transport(monkeypatch):
    calls=[]
    def respond(request):
        body=json.loads(request.content);data=json.loads(body['messages'][-1]['content']);calls.append(data)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer(data),ensure_ascii=False)},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':100}})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**{**kwargs,'transport':httpx.MockTransport(respond)}))
    return calls


def test_editable_example_model_call_filter_without_connection_and_changed_input(configured,monkeypatch):
    client,app,_,settings=configured;pid=install(client,'source-comparison');base='/api/v1/projects/'+pid
    connection={'provider':'api','base_url':'http://127.0.0.1:9001/v1','model':'source-comparison-test-only','api_key':'test-only','runtime_enabled':True}
    client.put(base+'/agent-session',json=connection).raise_for_status();calls=transport(monkeypatch)
    task=wait_task(client,base,start(client,base,'compare-first',workflow_id=pid),seconds=40)
    assert task['status']=='succeeded',task.get('error')
    result=task['outputs']['result'];assert result['rows']==3 and len(calls)==1
    for artifact in result['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path']).status_code==200
    client.put(base+'/agent-session',json={**connection,'runtime_enabled':False}).raise_for_status()
    replay=wait_task(client,base,start(client,base,'filter-only',workflow_id=pid,inputs={
        'sources':[],'source_result_path':result['analysis_path'],'status_filter':'资料不足'}),seconds=40)
    assert replay['status']=='succeeded',replay.get('error')
    assert replay['outputs']['result']['rows']==1 and len(calls)==1
    original=client.get(base+'/tasks/'+task['id']).json()['outputs'];assert original==task['outputs']
    client.put(base+'/agent-session',json=connection).raise_for_status()
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()['snapshot']['workflow']
    sources=next(f['default'] for f in draft['nodes'][0]['config']['inputs'] if f['name']=='sources')
    replacement=client.post(base+'/materials',files={'file':('测试记录.txt',template.example_files()['测试记录-修正.txt'].encode(),'text/plain')}).json()
    sources[1]['path']=replacement['path']
    changed=wait_task(client,base,start(client,base,'compare-changed',workflow_id=pid,inputs={'sources':sources}),seconds=40)
    assert changed['status']=='succeeded',changed.get('error')
    assert changed['outputs']['result']['status_counts']=={'与要求一致':2,'口径不同':1} and len(calls)==2
    assert changed['outputs']['result']['analysis_path']!=result['analysis_path']
