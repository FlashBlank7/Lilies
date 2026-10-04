"""Imported examples explain each callable workflow without changing employee work."""
import asyncio
from copy import deepcopy

import pytest

from agent_platform.example_catalog import catalog
from agent_platform.example_projects import InstantiateExample, instantiate, refresh_example_defaults
from tests.test_example_projects import install
from tests.test_projects import configured, settled, start  # noqa: F401


def legacy_install(app, key):
    item = deepcopy(next(item for item in catalog() if item['id'] == key))
    for flow in item['workflows']:
        for field in ('description', 'inputs', 'outputs'):
            flow.pop(field, None)
        for node in flow['workflow']['nodes']:
            if node['type'] == 'code' and '\n\ndef csv_cell(value):' in node['config']['code']:
                before, after = node['config']['code'].split('\n\ndef csv_cell(value):', 1)
                after = after.split('\n\ndef export(', 1)[1]
                node['config']['code'] = (before+'\n\ndef export('+after).replace(
                    'k: csv_cell(v)', 'k: "\'"+v if isinstance(v, str) and v.startswith((\'=\', \'+\', \'-\', \'@\')) else v')
        if key == 'composition':
            for node in flow['workflow']['nodes']:
                if node['type'] == 'code':
                    node['config']['code'] = node['config']['code'].replace(
                        "'+group+' / '+value+'；可用字段：'+'、'.join(fields)", "'+group+' / '+value")
                for field in node['config'].get('inputs', []) if node['type'] == 'start' else []:
                    field.pop('column_source', None)
        if key == 'expenses':
            for node in flow['workflow']['nodes']:
                for field in node['config'].get('inputs', []) if node['type'] == 'start' else []:
                    if field['name'] in {'source_path', 'second_path'}:
                        field['type'] = 'string'
                        field.pop('accept', None)
    from agent_platform.example_projects import initialize
    initialize(app.state.services)
    return asyncio.run(instantiate(app.state.services, {'id':'root'}, item,
        InstantiateExample(request_key='legacy-'+key)))['project_id']


def edit(client, workflow_id, op, data):
    path = '/api/v1/applications/'+workflow_id+'/draft'
    draft = client.get(path).json()
    response = client.post(path, json={'expected_revision':draft['revision'],
        'idempotency_key':'edit-'+str(draft['revision']), 'op':op, 'data':data})
    assert response.status_code == 200, response.text


def test_cutting_catalog_import_and_overview_have_distinct_purpose_and_io(configured):
    client, _, _, settings = configured
    detail = client.get('/api/v1/example-projects/cutting-candidates').json()
    assert len({flow['description'] for flow in detail['workflows']}) == 3
    pid = install(client, 'cutting-candidates')
    base = '/api/v1/projects/'+pid
    guide = client.get(base+'/example').json()
    members = {member['id']:member for member in client.get(base).json()['members']}
    space = {flow['id']:flow for flow in client.get(base+'/space').json()['workflows']}
    notes = {note['workflow_id']:note for note in client.get(base+'/progress').json()['value']['workflows']}
    manual = (settings.workspace_root/pid/guide['manual_path']).read_text()
    for flow in guide['workflows']:
        wid = flow['id']
        draft = client.get('/api/v1/applications/'+wid+'/draft').json()
        assert flow['description'] == members[wid]['description'] == space[wid]['description']
        assert draft['snapshot']['description'] == notes[wid]['purpose'] == flow['description']
        assert notes[wid]['inputs'] == flow['inputs']
        assert notes[wid]['outputs'] == flow['outputs']
        assert all(flow[field] in manual for field in ('description', 'inputs', 'outputs'))
    main, compare, allocate = guide['workflows']
    assert '物料表' in notes[main['id']]['inputs'] and 'patterns.csv' in notes[main['id']]['outputs']
    assert 'stock_id' in notes[compare['id']]['inputs'] and '组内顺位' in notes[compare['id']]['outputs']
    assert '原需求表' in notes[allocate['id']]['inputs'] and '共同需求余额' in notes[allocate['id']]['outputs']
    assert client.get(base+'/tasks').json() == []


def test_composition_import_and_legacy_refresh_explain_each_member_without_changing_graphs(configured):
    client,app,_,_=configured
    new_pid=install(client,'composition')
    pid=legacy_install(app,'composition')
    base='/api/v1/projects/'+pid
    flows=client.get(base+'/example').json()['workflows']
    before={flow['id']:client.get('/api/v1/applications/'+flow['id']+'/draft').json() for flow in flows}
    asyncio.run(refresh_example_defaults(app.state.services))
    for project_id in (new_pid,pid):
        members=client.get('/api/v1/projects/'+project_id).json()['members']
        assert len({member['description'] for member in members})==3
        notes=client.get('/api/v1/projects/'+project_id+'/progress').json()['value']['workflows']
        assert len(notes)==3
        assert any('缺失值' in note['purpose'] and '无需指定分组' in note['inputs'] for note in notes)
        assert any('均值' in note['purpose'] and 'SVG' in note['outputs'] for note in notes)
    after={wid:client.get('/api/v1/applications/'+wid+'/draft').json() for wid in before}
    assert all(after[wid]['revision']==old['revision']+1 and after[wid]['snapshot']['workflow']==old['snapshot']['workflow'] for wid,old in before.items())
    asyncio.run(refresh_example_defaults(app.state.services))
    assert after=={wid:client.get('/api/v1/applications/'+wid+'/draft').json() for wid in before}


def test_legacy_cutting_refresh_updates_current_cards_once_and_preserves_history(configured):
    client, app, _, settings = configured
    pid = legacy_install(app, 'cutting-candidates')
    base = '/api/v1/projects/'+pid
    guide = client.get(base+'/example').json()
    flows = guide['workflows']
    task = settled(client, base, start(client, base, 'old-generation', workflow_id=pid))
    assert task['status'] == 'succeeded', task.get('error')
    history = asyncio.run(app.state.services.projects.store.get_task(pid, task['id'], snapshots=True))
    before = {flow['id']:client.get('/api/v1/applications/'+flow['id']+'/draft').json() for flow in flows}
    manual = (settings.workspace_root/pid/guide['manual_path']).read_bytes()
    skill = client.get(base+'/skills/example-guide').json()
    record = asyncio.run(app.state.services.projects.store.get_record(pid, 'example', 'guide'))
    asyncio.run(app.state.services.projects.initialize())
    first = {flow['id']:client.get('/api/v1/applications/'+flow['id']+'/draft').json() for flow in flows}
    assert first[pid] == before[pid]
    for flow in flows[1:]:
        wid = flow['id']
        assert first[wid]['revision'] == before[wid]['revision'] + 1
        assert first[wid]['snapshot']['description'] != before[wid]['snapshot']['description']
        assert first[wid]['snapshot']['workflow'] == before[wid]['snapshot']['workflow']
    descriptions = {flow['id']:flow['description'] for flow in client.get(base+'/space').json()['workflows']}
    assert len(set(descriptions.values())) == 3
    assert descriptions == {member['id']:member['description'] for member in client.get(base).json()['members']}
    assert len(client.get(base+'/progress').json()['value']['workflows']) == 3
    asyncio.run(refresh_example_defaults(app.state.services))
    assert first == {flow['id']:client.get('/api/v1/applications/'+flow['id']+'/draft').json() for flow in flows}
    assert client.get(base+'/tasks/'+task['id']).json() == task
    assert asyncio.run(app.state.services.projects.store.get_task(pid, task['id'], snapshots=True)) == history
    assert asyncio.run(app.state.services.projects.store.get_record(pid, 'example', 'guide')) == record
    assert client.get(base+'/skills/example-guide').json() == skill
    assert (settings.workspace_root/pid/guide['manual_path']).read_bytes() == manual


@pytest.mark.parametrize('key', ['cutting-candidates', 'composition'])
@pytest.mark.parametrize('custom', ['name', 'description', 'graph'])
def test_legacy_refresh_preserves_employee_workflow_and_progress_edits(configured, custom, key):
    client, app, _, _ = configured
    pid = legacy_install(app, key)
    base = '/api/v1/projects/'+pid
    flow = client.get(base+'/example').json()['workflows'][1]
    wid = flow['id']
    if custom == 'name':
        edit(client, wid, 'set_metadata', {'name':'员工自定义流程'})
    elif custom == 'description':
        edit(client, wid, 'set_metadata', {'description':'员工修改：仅检查部门批准条件'})
    else:
        workflow = client.get('/api/v1/applications/'+wid+'/draft').json()['snapshot']['workflow']
        workflow['nodes'][-1]['config']['outputs']['department_note'] = '部门自定义'
        edit(client, wid, 'replace_workflow', {'workflow':workflow})
    response = client.put(base+'/progress', json={'expected_revision':0,'value':{
        'goal':'部门安排', 'workflows':[{'workflow_id':wid, 'purpose':'员工的比较说明', 'inputs':'部门表', 'outputs':'部门报告'}]}})
    assert response.status_code == 200, response.text
    draft = client.get('/api/v1/applications/'+wid+'/draft').json()
    progress = client.get(base+'/progress').json()
    asyncio.run(refresh_example_defaults(app.state.services))
    assert client.get('/api/v1/applications/'+wid+'/draft').json() == draft
    assert client.get(base+'/progress').json() == progress


def test_expense_file_defaults_are_typed_on_import_and_legacy_refresh_keeps_selected_files(configured):
    client, app, _, _ = configured
    new_pid = install(client, 'expenses')
    pid = legacy_install(app, 'expenses')
    path = '/api/v1/applications/'+pid+'/draft'
    workflow = client.get(path).json()['snapshot']['workflow']
    fields = workflow['nodes'][0]['config']['inputs']
    fields[0]['default'] = 'requirement-package/员工费用.csv'
    fields[1]['default'] = 'requirement-package/员工退款.xlsx'
    edit(client, pid, 'replace_workflow', {'workflow':workflow})
    before = client.get(path).json()
    asyncio.run(refresh_example_defaults(app.state.services))
    after = client.get(path).json()
    assert after['revision'] == before['revision'] + 1
    for wid in (new_pid, pid):
        inputs = client.get('/api/v1/applications/'+wid+'/draft').json()['snapshot']['workflow']['nodes'][0]['config']['inputs']
        for field in inputs[:2]:
            assert field['type'] == 'file'
            assert field['accept'] == ['.csv','.tsv','.xlsx']
    for old, new in zip(fields[:2], after['snapshot']['workflow']['nodes'][0]['config']['inputs'][:2]):
        assert old['default'] == new['default']
    asyncio.run(refresh_example_defaults(app.state.services))
    assert client.get(path).json() == after


def test_expense_legacy_refresh_skips_custom_processing(configured):
    client, app, _, _ = configured
    pid = legacy_install(app, 'expenses')
    path = '/api/v1/applications/'+pid+'/draft'
    workflow = client.get(path).json()['snapshot']['workflow']
    workflow['nodes'][-1]['config']['outputs']['department_note'] = '部门自定义'
    edit(client, pid, 'replace_workflow', {'workflow':workflow})
    before = client.get(path).json()
    asyncio.run(refresh_example_defaults(app.state.services))
    assert client.get(path).json() == before
