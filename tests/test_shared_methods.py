import pytest
from tests.test_users import platform, signup, project  # noqa:F401


def test_explicit_share_install_edit_independence_and_targets(platform):
    client,app=platform
    alice,a=signup(client,'Alice');bob,b=signup(client,'Bob')
    source,target,other=project(client,a,'Source'),project(client,a,'Target'),project(client,b,'Other')
    base='/api/v1/projects/'+source
    installed=client.post(base+'/space/official-workflows/tabular-classification',headers=a)
    # Use the catalogue's actual id, independent of its human label.
    if installed.status_code==404:
        ident=client.get(base+'/space/official-workflows',headers=a).json()[0]['id']
        installed=client.post(base+'/space/official-workflows/'+ident,headers=a)
    assert installed.status_code==201,installed.text
    wid=installed.json()['workflow_id']
    body={'workflow_id':wid,'name':'Shared classification','description':'Training and evaluation','target_project_ids':[target]}
    assert client.post(base+'/space/shared-methods',headers=b,json=body).status_code==404
    denied=client.post(base+'/space/shared-methods',headers=a,json={**body,'target_project_ids':[other]})
    assert denied.status_code==404
    published=client.post(base+'/space/shared-methods',headers=a,json=body)
    assert published.status_code==201,published.text
    ident=published.json()['id'];dest='/api/v1/projects/'+target
    assert client.get(dest+'/space/shared-methods',headers=a).json()[0]['id']==ident
    assert client.get('/api/v1/projects/'+other+'/space/shared-methods',headers=b).json()==[]
    assert client.post('/api/v1/projects/'+other+'/space/shared-methods/'+ident+'/install',headers=b).status_code==404
    first=client.post(dest+'/space/shared-methods/'+ident+'/install',headers=a)
    assert first.status_code==201,first.text
    second=client.post(dest+'/space/shared-methods/'+ident+'/install',headers=a)
    assert second.json()==first.json()
    copy=first.json()['workflow_id']
    assert copy!=wid
    graph=client.get('/api/v1/applications/'+copy+'/draft',headers=a).json()
    value=graph['snapshot']['workflow'];value['nodes'][0]['title']='Changed copy'
    assert client.put(dest+'/workflows/'+copy+'/draft',headers=a,json={'expected_revision':graph['revision'],'workflow':value}).status_code==200
    original=client.get('/api/v1/applications/'+wid+'/draft',headers=a).json()
    assert original['snapshot']['workflow']['nodes'][0]['title']!='Changed copy'
    skill=client.get(dest+'/skills/'+first.json()['skill_id'],headers=a).json()
    assert copy in skill['content']


def test_skill_share_does_not_include_referenced_customer_documents(platform):
    client,app=platform
    _,a=signup(client,'Alice');source,target=project(client,a),project(client,a)
    base='/api/v1/projects/'+source
    client.put(base+'/skills/method',headers=a,json={'name':'Method','content':'How to use a workflow','references':{'customer.txt':'private raw data'}})
    body={'skill_id':'method','name':'Method','target_project_ids':[target]}
    payload=client.post(base+'/space/shared-methods/preview',headers=a,json=body)
    assert payload.status_code==200,payload.text
    assert 'private raw data' not in payload.text
    assert payload.json()['skill']['revision']==1


def test_resource_unbinding_does_not_touch_variable_references():
    from agent_platform.shared_methods import unbind
    before={'api_key':'secret','dataset_id':'old','source_path':'customer.csv','model_ref':'trained',
            'headers':{'Authorization':'Bearer secret'},'input':{'$ref':{'node_id':'start','path':['input']}}}
    after=unbind(before)
    assert after['api_key']==after['dataset_id']==after['model_ref']==after['source_path']==''
    assert after['headers']=={}
    assert after['input']==before['input']
