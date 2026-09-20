"""Project tools and the page edit the same knowledge resources and revisions."""
import asyncio

import pytest

from agent_platform.project_metrics import is_read_call
from tests.test_project_knowledge import knowledge, setup_index  # noqa: F401
from tests.test_projects import configured  # noqa: F401


def call(client, base, **arguments):
    return client.post(base + '/agent-tools', json={'name': 'project_knowledge', 'arguments': arguments})


def test_tool_lifecycle_without_workflow_and_page_edits_conflict(knowledge):
    client, app, project, settings, base, service, calls = knowledge
    client.put(base + '/embedding-model', json={'provider':'api','model':'embed-v1',
        'base_url':'http://127.0.0.1:11434/v1','runtime_enabled':True}).raise_for_status()
    saved = call(client, base, action='configure', knowledge_ref='manual',
        settings={'name':'手册','expected_revision':0,'chunk_size':120,'chunk_overlap':20})
    assert saved.status_code == 200 and saved.json()['revision'] == 1
    root = app.state.services.projects.workspace(project['id']) / 'results'
    root.mkdir(exist_ok=True); (root/'manual.txt').write_text('The cat likes sleeping.')
    added = call(client, base, action='add', knowledge_ref='manual',
        source={'source_path':'results/manual.txt','expected_revision':1})
    assert added.status_code == 200 and added.json()['revision'] == 2
    doc_id = added.json()['documents'][0]['id']
    built = call(client, base, action='build', knowledge_ref='manual', expected_revision=2)
    assert built.status_code == 200 and built.json()['status'] == 'ready'
    version = built.json()['active_version']; count = len(calls)
    assert call(client, base, action='build', knowledge_ref='manual', expected_revision=2).json()['active_version'] == version
    assert len(calls) == count
    found = call(client, base, action='search', knowledge_ref='manual', query='feline')
    assert found.status_code == 200 and found.json()['results'][0]['source_path'] == 'results/manual.txt'
    page_edit = client.put(base + '/knowledge/manual', json={'name':'页面改名','expected_revision':2})
    assert page_edit.status_code == 200
    conflict = call(client, base, action='configure', knowledge_ref='manual', settings={'name':'旧配置','expected_revision':2})
    assert conflict.status_code == 409 and client.get(base+'/knowledge/manual').json()['name'] == '页面改名'
    removed = call(client, base, action='remove', knowledge_ref='manual', document_id=doc_id, expected_revision=3)
    assert removed.status_code == 200 and not removed.json()['documents']
    assert removed.json()['active_version'] == version and removed.json()['status'] == 'pending'
    assert client.get(base+'/tasks').json() == []  # Managing knowledge has no workflow/task prerequisite.


def test_tool_scope_and_required_fields(knowledge):
    client, app, project, settings, base, service, calls = knowledge
    setup_index(knowledge)
    for arguments in [
        {'action':'configure','knowledge_ref':'new'},
        {'action':'add','knowledge_ref':'manual'},
        {'action':'build','knowledge_ref':'manual'},
        {'action':'remove','knowledge_ref':'manual','expected_revision':2},
        {'action':'read'},
    ]:
        assert call(client,base,**arguments).status_code == 422
    assert call(client, base, action='add', knowledge_ref='manual',
        source={'source_path':'../other-project/private.txt','expected_revision':2}).status_code == 422
    other = client.post('/api/v1/projects',json={'name':'另一个项目'}).json()['id']
    assert call(client, '/api/v1/projects/'+other, action='read', knowledge_ref='manual').status_code == 404
    assert call(client,base,action='configure',knowledge_ref='manual',settings={'name':'手册','expected_revision':2},
        project_id=other).status_code == 422
    for action in ['list','read','search']:
        assert is_read_call('project_knowledge',{'action':action})
    for action in ['configure','add','remove','build']:
        assert not is_read_call('project_knowledge',{'action':action})


def test_cancelled_index_build_keeps_old_version_and_allows_retry(knowledge, monkeypatch):
    client, app, project, settings, base, service, calls = knowledge
    saved=setup_index(knowledge)
    client.post(base+'/knowledge/manual/documents',json={'expected_revision':2,'text':'new cat document'}).raise_for_status()
    entered=asyncio.Event()
    async def held(connection,texts):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service,'embeddings',held)
    async def cancel():
        task=asyncio.create_task(service.build(project['id'],'manual',3))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    client.portal.call(cancel)
    current=client.get(base+'/knowledge/manual').json()
    assert current['active_version']==saved['active_version'] and current['status']=='pending'
    assert not service.builds
    async def ready(connection,texts): return [[1.,0.] for _ in texts]
    monkeypatch.setattr(service,'embeddings',ready)
    assert call(client,base,action='build',knowledge_ref='manual',expected_revision=3).json()['status']=='ready'


def test_legacy_read_only_session_cannot_edit_knowledge(knowledge):
    client, app, project, settings, base, service, calls = knowledge
    setup_index(knowledge)
    manager=app.state.services.local_agents
    state=manager.load(project['id']);state.update(phase='operate',conversation_enabled=False);manager.save(project['id'],state)
    assert call(client,base,action='read',knowledge_ref='manual').status_code==200
    denied=call(client,base,action='remove',knowledge_ref='manual',expected_revision=2,document_id='unused')
    assert denied.status_code==422 and '只运行已有流程' in denied.text
    assert client.get(base+'/knowledge/manual').json()['revision']==2
