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


def test_shared_input_defaults_require_target_project_files(platform, monkeypatch):
    client,app=platform
    _,auth=signup(client,'Author');source,target=project(client,auth,'Source'),project(client,auth,'Target')
    base='/api/v1/projects/'+source
    installed=client.post(base+'/space/official-workflows/cutting-candidates',headers=auth)
    assert installed.status_code==201,installed.text
    wid=installed.json()['workflow_id']
    draft=client.get('/api/v1/applications/'+wid+'/draft',headers=auth).json()
    graph=draft['snapshot']['workflow']
    inputs=graph['nodes'][0]['config']['inputs']
    for field in inputs:
        if field['type']=='file':field['default']='requirement-package/private/'+field['name']+'.csv'
        if field['name']=='kerf':field['default']=1
    inputs.extend([
        {'name':'other_files','type':'file_list','default':['requirement-package/private/extra.csv']},
        {'name':'model_ref','type':'string','default':'source-model'},
        {'name':'materials','type':'array','columns':[{'name':'document','type':'file','default':'private.md'},{'name':'label','type':'string'}],
         'default':[{'document':'requirement-package/private/design.pdf','label':'Reference A'}]},
    ])
    assert client.put(base+'/workflows/'+wid+'/draft',headers=auth,json={'expected_revision':draft['revision'],'workflow':graph}).status_code==200
    body={'workflow_id':wid,'name':'Portable method','target_project_ids':[target]}
    preview=client.post(base+'/space/shared-methods/preview',headers=auth,json=body)
    assert preview.status_code==200,preview.text
    def fields(value):return {f['name']:f for f in value['nodes'][0]['config']['inputs']}
    copied=fields(preview.json()['workflows'][0]['workflow'])
    assert copied['stock_path']['default']==copied['demand_path']['default']==''
    assert copied['other_files']['default']==[]
    assert copied['model_ref']['default']==''
    assert copied['materials']['default']==[{'document':'','label':'Reference A'}]
    assert copied['materials']['columns'][0]['default']==''
    assert copied['unit']['default']=='mm' and copied['kerf']['default']==1
    assert 'requirement-package/private/' not in preview.text
    # Existing shares may have been published before schema defaults were cleared.
    with monkeypatch.context() as old:
        old.setattr('agent_platform.shared_methods.unbind',lambda value:value)
        ident=client.post(base+'/space/shared-methods',headers=auth,json=body).json()['id']
    result=client.post('/api/v1/projects/'+target+'/space/shared-methods/'+ident+'/install',headers=auth)
    assert result.status_code==201,result.text
    saved=client.get('/api/v1/applications/'+result.json()['workflow_id']+'/draft',headers=auth).json()
    assert fields(saved['snapshot']['workflow'])==copied
    original=client.get('/api/v1/applications/'+wid+'/draft',headers=auth).json()
    assert fields(original['snapshot']['workflow'])['stock_path']['default'].startswith('requirement-package/private/')


def test_resource_defaults_keep_upstream_references_and_regular_parameters():
    from agent_platform.shared_methods import unbind
    ref={'$ref':{'node_id':'start','path':['file']}}
    value={'fields':[{'name':'file','type':'file','default':ref},{'name':'files','type':'file_list','default':ref}],
           'labels_path':ref,'file_path':'requirement-package/data.csv','transformer_path':'models/process.py',
           'notes':'Use the same units','code':'source_path = user_input'}
    result=unbind(value)
    assert result['fields']==value['fields'] and result['labels_path']==ref
    assert result['file_path']==result['transformer_path']==''
    assert result['notes']==value['notes'] and result['code']==value['code']
