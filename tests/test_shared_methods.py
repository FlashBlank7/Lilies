import pytest
from tests.test_users import platform, signup, project  # noqa:F401
from tests.test_projects import configured, start, settled  # noqa:F401


def composition_share(client):
    response=client.post('/api/v1/example-projects/composition/instantiate',json={'request_key':'share-composition'})
    assert response.status_code==201,response.text
    source=response.json()['project_id']
    target=client.post('/api/v1/projects',json={'name':'Receiving project'}).json()['id']
    base='/api/v1/projects/'+source
    share=client.post(base+'/space/shared-methods',json={'workflow_id':source,'name':'Reusable data checks','target_project_ids':[target]})
    assert share.status_code==201,share.text
    return source,target,share.json()['id']


def test_composed_workflow_share_rewrites_children_and_runs_in_target_project(configured):
    client,_,_,settings=configured
    source,target,ident=composition_share(client)
    dest='/api/v1/projects/'+target
    response=client.post(dest+'/space/shared-methods/'+ident+'/install')
    assert response.status_code==201,response.text
    result=response.json()
    assert len(result['mapping'])==3
    original=set(result['mapping']);copied=set(result['mapping'].values())
    assert not original&copied
    assert len(client.get(dest).json()['members'])==4
    root=client.get('/api/v1/applications/'+result['workflow_id']+'/draft').json()['snapshot']['workflow']
    calls=[n['config']['tool_name'][9:] for n in root['nodes'] if n['type']=='tool']
    assert set(calls)==copied-{result['workflow_id']}
    guide=client.get('/api/v1/projects/'+source+'/example').json()
    source_file=next(f['path'] for f in guide['files'] if f['name']=='data.csv')
    material=client.post(dest+'/materials/copy',json={'source_project_id':source,'source_path':source_file})
    assert material.status_code==201,material.text
    task=settled(client,dest,start(client,dest,'shared-composition',mode='workflow',
        workflow_id=result['workflow_id'],inputs={'source_path':material.json()['path']}))
    assert task['status']=='succeeded',task
    assert task['outputs']['profile'] and task['outputs']['summary']
    assert list((settings.workspace_root/target/'results/examples').rglob('report.md'))
    assert not (settings.workspace_root/source/'results/examples').exists()
    assert client.post(dest+'/space/shared-methods/'+ident+'/install').json()==result


@pytest.mark.parametrize('failure',['workflow','skill','skill_after_write','member'])
def test_failed_workflow_install_leaves_no_partial_members_and_retries(configured,monkeypatch,failure):
    from agent_platform import shared_methods
    client,app,_,_=configured
    _,target,ident=composition_share(client)
    dest='/api/v1/projects/'+target
    members=client.get(dest).json()['members']
    skills=client.get(dest+'/skills').json()
    applications=client.get('/api/v1/applications').json()
    owner=app.state.services.projects if failure=='member' else shared_methods
    name='add_member' if failure=='member' else 'save_workflow' if failure=='workflow' else 'save_skill'
    actual=getattr(owner,name)
    calls=0
    async def fail(*args,**kwargs):
        nonlocal calls
        calls+=1
        # Some definitions are already saved when the second child fails.
        if failure=='skill' or calls==2:raise ValueError('模拟安装期间保存失败')
        result=await actual(*args,**kwargs)
        if failure=='skill_after_write':raise ValueError('模拟安装期间保存失败')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(owner,name,fail)
        failed=client.post(dest+'/space/shared-methods/'+ident+'/install')
    assert failed.status_code==422,failed.text
    assert '模拟安装期间保存失败' in failed.text
    assert client.get(dest).json()['members']==members
    assert client.get(dest+'/skills').json()==skills
    assert client.get('/api/v1/applications').json()==applications
    repaired=client.post(dest+'/space/shared-methods/'+ident+'/install')
    assert repaired.status_code==201,repaired.text
    assert len(client.get(dest).json()['members'])==len(members)+3
    assert client.post(dest+'/space/shared-methods/'+ident+'/install').json()==repaired.json()


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
    ident=client.post(base+'/space/shared-methods',headers=a,json=body).json()['id']
    result=client.post('/api/v1/projects/'+target+'/space/shared-methods/'+ident+'/install',headers=a).json()
    copied=client.get('/api/v1/projects/'+target+'/skills/'+result['skill_id'],headers=a).json()
    assert copied['references']=={}


def test_selected_skill_references_are_frozen_and_independently_editable(platform):
    client,_=platform
    _,a=signup(client,'Author');source,target=project(client,a),project(client,a)
    base='/api/v1/projects/'+source;dest='/api/v1/projects/'+target
    value={'name':'Check measurements','content':'Read check.py and use explicit units.',
           'references':{'check.py':'print(2 + 3)','private.txt':'customer content'}}
    assert client.put(base+'/skills/method',headers=a,json=value).status_code==200
    body={'skill_id':'method','skill_revision':1,'reference_names':['check.py','check.py'],
          'name':'Portable check','target_project_ids':[target]}
    preview=client.post(base+'/space/shared-methods/preview',headers=a,json=body)
    assert preview.status_code==200,preview.text
    assert preview.json()['skill']['references']=={'check.py':'print(2 + 3)'}
    assert 'customer content' not in preview.text
    ident=client.post(base+'/space/shared-methods',headers=a,json=body).json()['id']
    updated={**value,'expected_revision':1,'references':{'check.py':'print(9)','private.txt':'new customer content'}}
    assert client.put(base+'/skills/method',headers=a,json=updated).status_code==200
    copied=client.post(dest+'/space/shared-methods/'+ident+'/install',headers=a)
    assert copied.status_code==201,copied.text
    assert client.post(dest+'/space/shared-methods/'+ident+'/install',headers=a).json()==copied.json()
    sid=copied.json()['skill_id']
    skill=client.get(dest+'/skills/'+sid,headers=a).json()
    assert skill['references']=={'check.py':'print(2 + 3)'}
    read=client.post(dest+'/agent-tools',headers=a,json={'name':'project_skills',
        'arguments':{'action':'read','skill_id':sid,'reference':'check.py'}})
    assert read.status_code==200,read.text
    assert read.json()['content']=='print(2 + 3)'
    assert client.put(dest+'/skills/'+sid,headers=a,json={'name':skill['name'],
        'content':skill['content'],'expected_revision':skill['revision'],'references':{'check.py':'print(6)'}}).status_code==200
    assert client.get(base+'/skills/method',headers=a).json()['references']==updated['references']


def test_stale_or_missing_skill_reference_cannot_be_shared(platform):
    client,_=platform
    _,a=signup(client,'Author');source,target=project(client,a),project(client,a)
    base='/api/v1/projects/'+source
    value={'name':'Method','content':'Use code.py','references':{'code.py':'print(1)'}}
    client.put(base+'/skills/method',headers=a,json=value)
    body={'skill_id':'method','skill_revision':1,'reference_names':['missing.py'],
          'name':'Method','target_project_ids':[target]}
    missing=client.post(base+'/space/shared-methods',headers=a,json=body)
    assert missing.status_code==422 and '引用文件已不存在' in missing.text
    client.put(base+'/skills/method',headers=a,json={**value,'expected_revision':1})
    stale=client.post(base+'/space/shared-methods',headers=a,json={**body,'reference_names':['code.py']})
    assert stale.status_code==409 and '方法已被修改' in stale.text
    assert client.get('/api/v1/projects/'+target+'/space/shared-methods',headers=a).json()==[]


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
