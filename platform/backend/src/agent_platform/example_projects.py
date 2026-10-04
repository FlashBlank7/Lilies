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
from .workflow_readiness import readiness, project_readiness


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


def field_notes(template_id):
    """Describe the actual example inputs, not its broad catalog category."""
    if template_id == 'rolling-forecast':
        return ('history.csv 与 history-late.csv 的字段为：\n'
                '- `series`：序列标识，不同序列分别训练和评价。\n'
                '- `time`：观测时刻，对应表单“观测时刻列”。\n'
                '- `available`：这条数值实际可用的时刻，对应“实际可用时间列”；每个预测起点只使用当时已可用的记录。\n'
                '- `value`：要预测的历史数值，对应“要预测的数值列”；零和负数均有效。\n\n'
                '列名按原文件填写：观测时刻列填 time，数值列填 value，序列标识列填 series，实际可用时间列填 available。'
                '默认每个时段为一天（86400秒），预测步数为3，回测3个历史起点。'
                'history-late.csv 将最后一条观测的可用时间推迟两天，用于检查迟到数据不会提前参与预测。'
                '训练标签由流程从后续实际数值生成，原表无需另外提供标签列。全部为合成序列，不代表真实预测效果。')
    if template_id == 'prediction-feedback':
        return ('predictions.csv 含 `sample_id`（样本标识）、`batch`（分组）、`prediction`（已有预测）、`baseline`（已有基线）；'
                'measurements.csv 与 measurements-2.csv 含 `sample_id` 和 `actual`（实测）。'
                '两表按 sample_id 关联，不按行号拼接。review-labels.csv 在同表保存这些字段，用于类别复核练习。'
                '本流程只比较已有预测与实测，不训练或重新预测。')
    if template_id == 'interval-trends':
        return ('history.csv 与 history-gap.csv 含 `series`（对象标识）、`period`（观测周期）和 `value`（周期数值）。'
                '示例按月记录，history-gap.csv 缺少2024-06；缺周期不能压缩成下一行。'
                '流程按未来区间均值生成 samples.csv 的 target 标签，保留起点当时可知的历史特征；原表无需标签列。')
    if template_id == 'process':
        return ('process.csv 含 `furnace`（炉次标识）、`time`（观测时间）、`temperature`（温度）和 `pressure`（压力）；'
                'labels.csv 含 `furnace`、`prediction_time`（预测时点）及 `target`（合成数值标签）。'
                '过程表与标签表按炉次对应，窗口仅取预测时点之前的记录；furnace 用于隔离分组，不作为特征。'
                '带 -2 的过程表与标签表须成套更换。new-data.csv 另附 temperature、pressure、material 三列，不能代替过程表与标签表。'
                '预处理仅在训练折内拟合，测试集仅评价固定方案，不代表真实工业效果。')
    if template_id in ('classification', 'regression', 'group-training', 'prediction', 'rules', 'data-guidance'):
        label = '合成连续数值标签' if template_id == 'regression' else '合成分类标签（good / review）'
        return ('`temperature`（温度）与 `pressure`（压力）为合成连续特征，`material` 为类别特征；'
                f'`target` 是{label}，`batch` 是批次标识，不作为可泛化特征。'
                'new-data.csv 仅含 temperature、pressure、material，用于已有模型预测，不含训练标签。'
                '训练与预处理在训练折内拟合；测试集仅评价固定方案。没有真实工业效果结论。')
    return ''


def manual_text(guide):
    text='# '+guide['name']+'\n\n所有资料为自编或合成，不代表客户现场效果。\n\n## 准备条件\n'+ '\n'.join('- '+v for v in guide['requires'])
    text+='\n\n## 操作顺序\n'+'\n'.join(f'{i+1}. {s}' for i,s in enumerate(guide['steps']))
    text+='\n\n## 可以这样问\n'+guide['question']+'\n\n## 修改练习\n'+guide['exercise']
    text+='\n\n## 资料与字段\n'+'\n'.join(f'- {f["name"]}：{f["path"]}' for f in guide['files'])
    if notes := field_notes(guide['id']):
        text+='\n\n'+notes
    text+='\n\n## 工作流\n'+'\n'.join(
        f'- {w["name"]}：{w["id"]}'
        + (f'\n  用途：{w["description"]}' if w.get('description') else '')
        + (f'\n  输入：{w["inputs"]}' if w.get('inputs') else '')
        + (f'\n  输出：{w["outputs"]}' if w.get('outputs') else '')
        for w in guide['workflows'])
    if guide.get('guide'):
        text+='\n\n## 方法与调用\n'+guide['guide']
    return text+'\n\n在项目空间选择资料和已有工作流，点击“让智能体调用”准备消息，检查后发送；也可以填写运行参数直接运行。结果在运行记录中查看和下载。换资料后创建新运行，原结果保留。创建项目不会自动执行任务。'


async def seed_workflow_notes(services, project_id, flows):
    notes = [{'workflow_id':flow['id'], 'purpose':flow['description'],
              'inputs':flow['inputs'], 'outputs':flow['outputs']}
             for flow in flows if flow.get('inputs') and flow.get('outputs')]
    progress = await services.projects.store.progress(project_id)
    if notes and progress['revision'] == 0:
        from .project_store import ProjectConflict
        try:
            await services.projects.store.put_progress(project_id, {**progress['value'], 'workflows':notes}, 0)
        except ProjectConflict:
            # A concurrent owner edit takes precedence over example defaults.
            pass


async def refresh_example_defaults(services):
    """Repair recognized legacy defaults using normal draft revisions."""
    with connect(services.storage.db_path) as db:
        guides = [(row['project_id'], json.loads(row['value_json'])) for row in db.execute(
            "SELECT project_id,value_json FROM project_records WHERE collection='example' AND record_key='guide'")]
    guides = [(pid, guide) for pid, guide in guides if guide.get('id') in {'cutting-candidates', 'expenses'}]
    if not guides:
        return
    items = {item['id']:item for item in catalog() if item['id'] in {'cutting-candidates', 'expenses'}}
    from .workflow_storage import RevisionConflict
    for project_id, guide in guides:
        item = items[guide['id']]
        if guide['id'] == 'expenses':
            await refresh_expense_file_inputs(services, project_id, guide, item)
            continue
        project = await services.projects.store.get(project_id)
        members = {member['id']: member for member in project['members']}
        files = {file['name']:file for file in guide.get('files', [])}
        notes = []
        for flow in item['workflows']:
            installed = next((w for w in guide.get('workflows', []) if w.get('name') == flow['name']), None)
            if not installed or installed.get('id') not in members:
                continue
            workflow_id = installed['id']
            draft = await services.workflow_store.get_draft(workflow_id)
            snapshot = draft['snapshot']
            expected_name = project['name'] if flow['key'] == 'main' else flow['name']
            if snapshot.name != expected_name or snapshot.description != item['description']:
                continue
            try:
                expected_workflow = WorkflowSpec.model_validate(replace_refs(deepcopy(flow['workflow']), files, {}))
            except KeyError:
                continue
            if snapshot.workflow != expected_workflow:
                continue
            if snapshot.description != flow['description']:
                try:
                    await services.applications.apply_operations_atomically(workflow_id,
                        expected_revision=draft['revision'], expected_content_hash=draft['content_hash'],
                        idempotency_key=f'example-flow-description:{draft["revision"]}',
                        operations=[{'op':'set_metadata','data':{'description':flow['description']}}],
                        change_context_operation='example_description_correction')
                except RevisionConflict:
                    continue
            notes.append({**flow, 'id':workflow_id})
        await seed_workflow_notes(services, project_id, notes)


async def refresh_expense_file_inputs(services, project_id, guide, item):
    files = {file['name']:file for file in guide.get('files', [])}
    draft = await services.workflow_store.get_draft(project_id)
    snapshot = draft['snapshot']
    try:
        expected = WorkflowSpec.model_validate(replace_refs(deepcopy(item['workflows'][0]['workflow']), files, {}))
    except KeyError:
        return
    current = snapshot.workflow.model_copy(deep=True)
    fields = {field['name']:field for node in current.nodes if node.type == 'start'
              for field in node.config.get('inputs', [])}
    for node in expected.nodes:
        for field in node.config.get('inputs', []) if node.type == 'start' else []:
            if field['name'] not in {'source_path', 'second_path'}:
                continue
            saved = fields.get(field['name'])
            if not saved or saved.get('type') != 'string' or 'accept' in saved:
                return
            # A different selected file is still the same expense input; retain it.
            field['default'] = saved.get('default', '')
            saved.update(type=field['type'], accept=field['accept'])
    if current != expected:
        return
    from .workflow_storage import RevisionConflict
    try:
        await services.applications.apply_operations_atomically(project_id,
            expected_revision=draft['revision'], expected_content_hash=draft['content_hash'],
            idempotency_key=f'example-expense-file-inputs:{draft["revision"]}',
            operations=[{'op':'replace_workflow','data':{'workflow':current.model_dump(mode='json')}}],
            change_context_operation='example_file_input_correction')
    except RevisionConflict:
        pass


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
        main = next(flow for flow in item['workflows'] if flow['key'] == 'main')
        project=await services.projects.create(body.name.strip() or item['name']+' · 示例',item['description'],
            workflow_description=main.get('description',item['description']))
        pid=project['id']
        with connect(services.storage.db_path) as db:
            db.execute('UPDATE example_project_installs SET project_id=? WHERE user_id=? AND request_key=?',(pid,user['id'],body.request_key))
        files={}
        for name,content in item['files'].items():
            files[name]=await add_material(services,pid,UploadFile(file=io.BytesIO(content if isinstance(content,bytes) else content.encode('utf-8')),filename=name))
        workflows={'main':pid}
        for flow in item['workflows']:
            if flow['key']!='main':
                member=await services.projects.add_member(pid,flow['name'],flow.get('description',item['description']))
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
        guide=public_item(item,True)
        guide.update(files=list(files.values()), workflows=[{**w,'id':workflows[w['key']]} for w in guide['workflows']])
        text=manual_text(guide)
        manual=await add_material(services,pid,UploadFile(file=io.BytesIO(text.encode()),filename='使用说明.md'))
        guide['manual_path']=manual['path']
        await save_skill(services,pid,'example-guide',SkillDocument(name=item['name']+'使用说明',description=item['description'][:500],content=text+'\n\n智能体可用 project_workflows inspect 查看当前输入和默认值，再用 workflow_run 调用。一次性问题可直接回答。连接缺失时请配置，不自动换服务商；未实际运行不得报告已完成。'))
        await services.projects.store.put_record(pid,'example','guide',guide,0)
        await seed_workflow_notes(services,pid,guide['workflows'])
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
    async def examples():
        result = [await describe(item) for item in items.values()]
        return sorted(result, key=lambda item: item['readiness']['status'] != 'configured')

    async def describe(item, detail=False):
        main = next(w for w in item['workflows'] if w['key'] == 'main')
        return {**public_item(item, detail), 'readiness': await readiness(services, main['workflow'],
                related={w['key']: w['workflow'] for w in item['workflows']})}

    @router.get('/example-projects/{template_id}')
    async def example(template_id:str):return await describe(item_for(template_id),True)

    @router.post('/example-projects/{template_id}/instantiate',status_code=201)
    async def create(template_id:str,body:InstantiateExample,request:Request):
        item=item_for(template_id)
        await asyncio.to_thread(initialize,services)
        return await invoke(instantiate,services,request.state.user,item,body)

    @scoped.get('/example')
    async def project_example(project_id:str):
        rows=await services.projects.store.records(project_id,'example')
        guide = next((r['value'] for r in rows if r['key']=='guide'),None)
        if guide:
            # Show corrected guidance for existing projects without rewriting their
            # saved manual, editable skill, workflows, or historical runs.
            guide = {**guide, 'current_manual': manual_text(guide), 'field_notes': field_notes(guide['id']),
                     'readiness': await project_readiness(services, project_id, project_id)}
        return guide
