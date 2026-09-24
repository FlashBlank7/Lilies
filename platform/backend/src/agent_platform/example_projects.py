"""Install self-contained, privately owned example projects without executing them."""
import asyncio
from copy import deepcopy
import io
import json
import shutil

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from .db import connect
from .example_catalog import catalog, public_item
from .project_materials import add_material
from .project_skills import save_skill, SkillDocument
from .project_workflow_edit import save_workflow, SaveWorkflow
from .workflow_models import WorkflowSpec


class InstantiateExample(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: str = Field(min_length=1,max_length=120)
    name: str = Field(default='',max_length=100)


def initialize(services):
    with connect(services.storage.db_path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS example_project_installs (
            user_id TEXT NOT NULL, request_key TEXT NOT NULL, template_id TEXT NOT NULL,
            name TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(user_id,request_key))''')


def replace_refs(value, files, workflows):
    if isinstance(value,dict):
        # Start outputs include defaults; raw $inputs only contains submitted keys.
        if '$ref' in value and value['$ref'].get('node_id')=='$inputs':
            return {'$ref':{**value['$ref'],'node_id':'start'}}
        return {k:replace_refs(v,files,workflows) for k,v in value.items()}
    if isinstance(value,list):return [replace_refs(v,files,workflows) for v in value]
    if isinstance(value,str):
        if value.startswith('@file:'):return files[value[6:]]['path']
        if value.startswith('workflow:@workflow:'):return 'workflow:'+workflows[value[19:]]
    return value


async def discard_new_project(services, project_id):
    """Only called for an unpublished, never-run installation created by this request."""
    def remove():
        with connect(services.storage.db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            ids=[r['application_id'] for r in db.execute('SELECT application_id FROM project_members WHERE project_id=?',(project_id,))]
            for table in ('project_access_members','project_records','project_record_writes','project_progress','project_knowledge_versions','project_knowledge','project_members'):
                db.execute(f'DELETE FROM {table} WHERE project_id=?',(project_id,))
            db.execute('DELETE FROM projects WHERE id=?',(project_id,))
            for ident in ids:
                db.execute('DELETE FROM draft_idempotency WHERE application_id=?',(ident,))
                db.execute('DELETE FROM applications WHERE id=?',(ident,))
        for ident in ids:
            folder=services.projects.workspace(ident)
            if folder.exists():shutil.rmtree(folder)
            services.sandboxes.readonly_workspaces.pop(folder.resolve(),None)
    await asyncio.to_thread(remove)


async def instantiate(services,user,item,body):
    # SQLite claim works across concurrent requests and backend workers. Retrying
    # a completed request returns the same project, even after a server restart.
    with connect(services.storage.db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM example_project_installs WHERE user_id=? AND request_key=?',(user['id'],body.request_key)).fetchone()
        if row:
            if row['template_id']!=item['id'] or row['name']!=body.name:
                raise HTTPException(409,'同一创建请求不能用于不同示例或名称')
            if not row['result']:
                raise HTTPException(409,'示例正在创建，请稍后使用同一请求重试')
            result=json.loads(row['result'])
        else:
            db.execute('INSERT INTO example_project_installs(user_id,request_key,template_id,name) VALUES(?,?,?,?)',
                       (user['id'],body.request_key,item['id'],body.name))
            result=None
    if result:
        await services.accounts.require_project(user,result['project_id'])
        return result
    project=None
    try:
        project=await services.projects.create(body.name.strip() or item['name']+' · 示例',item['description'])
        pid=project['id']
        with connect(services.storage.db_path) as db:
            db.execute('UPDATE example_project_installs SET project_id=? WHERE user_id=? AND request_key=?',(pid,user['id'],body.request_key))
        files={}
        for name,content in item['files'].items():
            files[name]=await add_material(services,pid,UploadFile(file=io.BytesIO(content if isinstance(content,bytes) else content.encode('utf-8')),filename=name))
        workflows={'main':pid}
        for flow in item['workflows']:
            if flow['key']!='main':
                member=await services.projects.add_member(pid,flow['name'],item['description'])
                workflows[flow['key']]=member['id']
        # All IDs exist before any graph is saved, including nested calls.
        for flow in reversed(item['workflows']):
            wid=workflows[flow['key']]
            draft=await services.workflow_store.get_draft(wid)
            workflow=WorkflowSpec.model_validate(replace_refs(deepcopy(flow['workflow']),files,workflows))
            await save_workflow(services,pid,wid,SaveWorkflow(expected_revision=draft['revision'],workflow=workflow))
        if item['id'] in ('prediction','rules'):
            from .project_resources import save_model,ModelResource
            await save_model(services,pid,'example-model',ModelResource(name='示例质量分类模型'))
        if item['id']=='knowledge':
            from .project_knowledge import KnowledgeSettings,KnowledgeSource
            resource=await services.projects.knowledge.save(pid,'example-knowledge',KnowledgeSettings(name='示例借用制度'))
            await services.projects.knowledge.add(pid,'example-knowledge',KnowledgeSource(expected_revision=resource['revision'],source_path=files['handbook.txt']['path']))
        guide={**public_item(item,True),'files':list(files.values()),
               'workflows':[{'id':workflows[w['key']],'name':w['name']} for w in item['workflows']]}
        text='# '+item['name']+'\n\n所有资料为自编或合成，不代表客户现场效果。\n\n## 准备条件\n'+ '\n'.join('- '+v for v in item['requires'])
        text+='\n\n## 操作顺序\n'+'\n'.join(f'{i+1}. {s}' for i,s in enumerate(item['steps']))
        text+='\n\n## 可以这样问\n'+item['question']+'\n\n## 修改练习\n'+item['exercise']
        text+='\n\n## 资料与字段\n'+'\n'.join(f'- {n}：{f["path"]}' for n,f in files.items())
        if item['category']=='机器学习':
            text+='\n\n温度 temperature 与压力 pressure 为合成连续特征，material 为类别特征；target 是生成的标签，batch/furnace 是隔离分组，不能作为可泛化特征。过程 time 为观测时间，prediction_time 为预测时点，窗口仅取预测前记录。特征与预处理在训练折内拟合；测试集仅评价固定方案。没有真实工业效果结论。'
        text+='\n\n## 工作流\n'+'\n'.join(f'- {w["name"]}：{w["id"]}' for w in guide['workflows'])
        if item.get('guide'):
            text+='\n\n## 方法与调用\n'+item['guide']
        text+='\n\n智能体可用 project_workflows inspect 查看当前输入和默认值，再用 workflow_run 调用。一次性问题可直接回答。流程创建不运行任务，连接缺失时请配置，不自动换服务商。结果在运行记录查看；未实际运行不得报告已完成。新输入创建新运行，保留原结果。'
        manual=await add_material(services,pid,UploadFile(file=io.BytesIO(text.encode()),filename='使用说明.md'))
        guide['manual_path']=manual['path']
        await save_skill(services,pid,'example-guide',SkillDocument(name=item['name']+'使用说明',description=item['description'][:500],content=text))
        await services.projects.store.put_record(pid,'example','guide',guide,0)
        if user['id']!='root':await services.accounts.add_member(pid,user['id'],'owner')
        result={'project_id':pid,'template_id':item['id'],'template_version':item['version']}
        with connect(services.storage.db_path) as db:
            db.execute('UPDATE example_project_installs SET result=? WHERE user_id=? AND request_key=?',(json.dumps(result),user['id'],body.request_key))
        return result
    except BaseException:
        if project:await asyncio.shield(discard_new_project(services,project['id']))
        with connect(services.storage.db_path) as db:
            db.execute('DELETE FROM example_project_installs WHERE user_id=? AND request_key=?',(user['id'],body.request_key))
        raise


def register_example_routes(router,scoped,services,invoke):
    # Schema initialization happens after the existing database lifespan setup.
    items={item['id']:item for item in catalog()}
    def item_for(ident):
        if ident not in items:raise HTTPException(404,'示例不存在')
        return items[ident]

    @router.get('/example-projects')
    async def examples():return [public_item(item) for item in items.values()]

    @router.get('/example-projects/{template_id}')
    async def example(template_id:str):return public_item(item_for(template_id),True)

    @router.post('/example-projects/{template_id}/instantiate',status_code=201)
    async def create(template_id:str,body:InstantiateExample,request:Request):
        item=item_for(template_id)
        await asyncio.to_thread(initialize,services)
        return await invoke(instantiate,services,request.state.user,item,body)

    @scoped.get('/example')
    async def project_example(project_id:str):
        rows=await services.projects.store.records(project_id,'example')
        return next((r['value'] for r in rows if r['key']=='guide'),None)
