from tests.test_projects import configured, graph, node, edge, start, settled  # noqa: F401


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
