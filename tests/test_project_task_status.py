from tests.test_projects import configured, graph, node, edge, start, settled  # noqa: F401


def test_compact_run_list_keeps_small_file_labels_without_large_inputs(configured):
    client, _, project, _ = configured
    pid=project['id']; base='/api/v1/projects/'+pid
    graph(client,pid,[node('start','start'),node('end','end',outputs={'ok':True})],[edge('start','end')])
    original={'source_path':'requirement-package/first/data.csv','second_path':'results/previous/review.csv',
              'files':['requirement-package/notes.txt'], 'request':'private full prompt','rows':[{'value':i} for i in range(300)]}
    first=settled(client,base,start(client,base,'first-file',workflow_id=pid,inputs=original))
    second=settled(client,base,start(client,base,'second-file',workflow_id=pid,inputs={'source_path':'requirement-package/data-2.csv'}))
    rows=client.get(base+'/tasks?compact=true').json()
    assert rows[0]['input_files']==['data-2.csv']
    assert set(rows[1]['input_files'])=={'data.csv','review.csv','notes.txt'}
    assert all('inputs' not in row and 'outputs' not in row for row in rows)
    assert client.get(base+'/tasks/'+first['id']).json()['inputs']==original
    assert first['id'] != second['id']


def test_waiting_tasks_filter_precedes_pagination_and_tracks_stopping(configured):
    client,app,project,_=configured;pid=project['id'];base='/api/v1/projects/'+pid
    graph(client,pid,[node('start','start'),node('ask','human_input',title='补充业务事实',fields=[{'name':'answer','type':'string','label':'答案'}]),node('end','end')],[edge('start','ask'),edge('ask','end')])
    first=settled(client,base,start(client,base,'old-wait',workflow_id=pid))
    second=settled(client,base,start(client,base,'new-wait',workflow_id=pid))
    assert first['status']==second['status']=='waiting_input'
    graph(client,pid,[node('start','start'),node('end','end',outputs={'message':'done'})],[edge('start','end')])
    latest=settled(client,base,start(client,base,'completed-after-waits',workflow_id=pid))
    assert client.get(base+'/tasks?limit=1').json()[0]['id']==latest['id']
    page=client.get(base+'/tasks?status=waiting_input&compact=true&limit=1').json()
    assert [t['id'] for t in page]==[second['id']] and 'inputs' not in page[0]
    older=client.get(base+'/tasks?status=waiting_input&limit=1&before='+second['id']).json()
    assert [t['id'] for t in older]==[first['id']]
    assert client.post(base+'/tasks/'+second['id']+'/stop').status_code==200
    assert [t['id'] for t in client.get(base+'/tasks?status=waiting_input').json()]==[first['id']]
    other=client.post('/api/v1/projects',json={'name':'another'}).json()['id']
    assert client.get('/api/v1/projects/'+other+'/tasks?status=waiting_input').json()==[]
    assert client.get(base+'/tasks?status=not-a-state').status_code==422


def test_run_history_parameters_keep_saved_labels_defaults_and_actual_choices(configured):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    fields = [
        {'name': 'source_path', 'type': 'string', 'default': 'requirement-package/expenses.csv'},
        {'name': 'group_by', 'label': '汇总维度', 'type': 'string', 'default': '按月、类别和币种'},
        {'name': 'mark_duplicates', 'label': '标记疑似重复', 'type': 'boolean', 'default': True},
        {'name': 'minimum_amount', 'label': '最低金额', 'type': 'number', 'default': 0},
    ]
    graph(client, pid, [node('start', 'start', inputs=fields), node('end', 'end')], [edge('start', 'end')])
    first = start(client, base, 'category', workflow_id=pid, inputs={'source_path': 'requirement-package/expenses.csv'})
    expected = [
        {'name': 'mark_duplicates', 'label': '标记疑似重复', 'value': '是'},
        {'name': 'minimum_amount', 'label': '最低金额', 'value': '0'},
        {'name': 'group_by', 'label': '汇总维度', 'value': '按月、类别和币种'},
    ]
    assert first['input_parameters'] == expected
    settled(client, base, first)
    second = settled(client, base, start(client, base, 'merchant', workflow_id=pid, inputs={
        'source_path': 'requirement-package/expenses.csv', 'group_by': '按月、商户和币种', 'mark_duplicates': False,
    }))
    fields[1].update(label='现在编辑的新名称', default='新的默认值')
    fields[2]['default'] = False
    graph(client, pid, [node('start', 'start', inputs=fields), node('end', 'end')], [edge('start', 'end')])
    rows = {task['id']: task for task in client.get(base + '/tasks?compact=true').json()}
    assert rows[first['id']]['input_parameters'] == expected
    expected_second = [{**expected[0], 'value': '否'}, expected[1], {**expected[2], 'value': '按月、商户和币种'}]
    assert rows[second['id']]['input_parameters'] == expected_second
    assert rows[first['id']]['input_files'] == rows[second['id']]['input_files'] == ['expenses.csv']
    assert all('inputs' not in row and 'snapshots' not in row for row in rows.values())
    assert client.get(base + '/tasks/' + first['id']).json()['input_parameters'] == expected


def test_history_parameters_follow_the_latest_actual_run_not_pending_supplements(configured):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    graph(client, pid, [node('start', 'start', inputs=[
        {'name': 'group_by', 'label': '汇总维度', 'type': 'string', 'default': '类别'},
    ]), node('ask', 'human_input', fields=[{'name': 'answer', 'label': '核对', 'type': 'string'}]),
        node('end', 'end')], [edge('start', 'ask'), edge('ask', 'end')])
    task = settled(client, base, start(client, base, 'supplemented', workflow_id=pid))
    assert task['status'] == 'waiting_input'
    pending = client.post(base + '/tasks/' + task['id'] + '/supplements', json={'inputs': {'group_by': '商户'}})
    assert pending.status_code == 200
    assert pending.json()['input_parameters'][0]['value'] == '类别'
    resumed = client.post(base + '/tasks/' + task['id'] + '/resume', json={})
    assert resumed.status_code == 202
    current = settled(client, base, resumed.json())
    assert current['inputs'] == {}
    assert current['input_parameters'] == [{'name': 'group_by', 'label': '汇总维度', 'value': '商户'}]
    assert client.get(base + '/tasks?compact=true').json()[0]['input_parameters'] == current['input_parameters']
