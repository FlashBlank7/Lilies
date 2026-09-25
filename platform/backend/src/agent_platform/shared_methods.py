"""Explicit internal sharing of editable definitions, never project data or connections."""
import asyncio
import json
from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .models import utc_now
from .project_skills import SkillDocument, skills, save_skill
from .project_store import connect
from .project_workflow_edit import SaveWorkflow, save_workflow
from .workflow_models import WorkflowSpec


class ShareMethod(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workflow_id: str = ''
    skill_id: str = ''
    reference_names: list[str] = Field(default_factory=list, max_length=100)
    skill_revision: int | None = Field(default=None, ge=0)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)
    limitations: str = Field(default='', max_length=2000)
    target_project_ids: list[str] = Field(min_length=1, max_length=50)


def initialize(db_path):
    with connect(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS shared_methods ('
                   'id TEXT PRIMARY KEY,source_project TEXT NOT NULL,user_id TEXT NOT NULL,created_at TEXT NOT NULL,'
                   'name TEXT NOT NULL,description TEXT NOT NULL,limitations TEXT NOT NULL,targets TEXT NOT NULL,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS shared_method_installs ('
                   'project_id TEXT NOT NULL,method_id TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(project_id,method_id))')


_RESOURCE_FIELDS = {
    'api_key', 'authorization', 'password', 'secret', 'access_token', 'refresh_token', 'bearer_token',
    'dataset_id', 'study_id', 'candidate_id', 'trial_id', 'source_path', 'file_path', 'labels_path',
    'transformer_path', 'model_ref', 'knowledge_ref', 'credential_id',
}


def _unbound_default(field, value):
    if isinstance(value, dict) and '$ref' in value:
        return deepcopy(value)
    return [] if field.get('type') == 'file_list' else ''


def _resource_input(field):
    return field.get('type') in ('file', 'file_list') or str(field.get('name', '')).lower() in _RESOURCE_FIELDS


def unbind(value):
    """Strip resource bindings and credential-bearing fields from the copy.

    Inline code and prompts are methods chosen by the sharer; the UI previews
    the exact definition. No project files or other records are included.
    """
    if isinstance(value, list):
        return [unbind(x) for x in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        lower = key.lower()
        if lower in _RESOURCE_FIELDS:
            result[key] = deepcopy(item) if isinstance(item, dict) and '$ref' in item else ''
        elif lower in {'headers', 'cookies'}:
            result[key] = {} if isinstance(item, dict) else []
        else:
            result[key] = unbind(item)
    # Form schemas store selected resources under `default`, rather than a
    # resource key. Keep the form and ordinary parameters, not the source binding.
    if 'default' in result and _resource_input(value):
        result['default'] = _unbound_default(value, result['default'])
    elif isinstance(value.get('columns'), list) and isinstance(result.get('default'), list):
        for row in result['default']:
            if isinstance(row, dict):
                for column in value['columns']:
                    if isinstance(column, dict) and _resource_input(column) and column.get('name') in row:
                        row[column['name']] = _unbound_default(column, row[column['name']])
    return result


async def make_share(services, project_id, body, user, *, preview=False):
    if bool(body.workflow_id) == bool(body.skill_id):
        raise ValueError('请选择一个工作流或一篇方法说明')
    if body.workflow_id and (body.reference_names or body.skill_revision is not None):
        raise ValueError('引用文件只能随对应的方法说明分享')
    for target in body.target_project_ids:
        await services.accounts.require_project(user, target)
    payload = {'workflows': [], 'skill': None}
    if body.workflow_id:
        pending, seen = [body.workflow_id], set()
        while pending:
            wid = pending.pop()
            if wid in seen:
                continue
            if len(seen) >= 30:
                raise ValueError('关联子流程超过 30 条，请拆分后分享')
            await services.projects.member(project_id, wid)
            seen.add(wid)
            draft = await services.workflow_store.get_draft(wid)
            snapshot = draft['snapshot']
            graph = snapshot.workflow.model_dump(mode='json')
            def calls(obj):
                if isinstance(obj, dict):
                    name = obj.get('tool_name', '')
                    if isinstance(name, str) and name.startswith('workflow:'):
                        pending.append(name[9:])
                    for child in obj.values():
                        calls(child)
                elif isinstance(obj, list):
                    for child in obj:
                        calls(child)
            calls(graph)
            payload['workflows'].append({'id':wid,'revision':draft['revision'],'name':snapshot.name,
                                        'description':snapshot.description,'workflow':unbind(graph)})
        payload['root'] = body.workflow_id
    else:
        original = await skills(services, project_id, body.skill_id)
        if body.skill_revision is not None and body.skill_revision != original['revision']:
            raise HTTPException(409, '方法已被修改，请重新选择并检查要分享的内容')
        references = original.get('references', {})
        names = list(dict.fromkeys(body.reference_names))
        if any(name not in references for name in names):
            raise ValueError('所选引用文件已不存在，请重新选择方法说明')
        # Only explicitly selected Skill references are frozen into the share.
        # Never follow project file paths, links, or references in the content.
        payload['skill'] = {k:original[k] for k in ('name','description','content','revision')}
        payload['skill']['references'] = {name: references[name] for name in names}
    if preview:
        return payload
    ident = str(uuid4())
    with connect(services.projects.store.db_path) as db:
        db.execute('INSERT INTO shared_methods VALUES(?,?,?,?,?,?,?,?,?)',
                   (ident,project_id,user['id'],utc_now(),body.name,body.description,body.limitations,
                    json.dumps(list(dict.fromkeys(body.target_project_ids))),json.dumps(payload,ensure_ascii=False)))
    return {'id':ident,'name':body.name}


def visible(services, project_id, method_id=''):
    with connect(services.projects.store.db_path) as db:
        rows = db.execute('SELECT * FROM shared_methods ORDER BY created_at DESC').fetchall()
    rows = [dict(row) for row in rows if project_id in json.loads(row['targets'])]
    if method_id:
        row = next((row for row in rows if row['id']==method_id), None)
        if row is None:
            raise HTTPException(404, '此项目无权查看共享内容')
        return row
    return [{k:r[k] for k in ('id','name','description','limitations','created_at')} for r in rows]


async def install(services, project_id, method_id):
    row = visible(services, project_id, method_id)
    with connect(services.projects.store.db_path) as db:
        previous = db.execute('SELECT result FROM shared_method_installs WHERE project_id=? AND method_id=?',(project_id,method_id)).fetchone()
    if previous:
        return json.loads(previous['result'])
    payload = json.loads(row['payload'])
    blocks = await services.projects.blocks_for(project_id)
    for item in payload['workflows']:
        graph = WorkflowSpec.model_validate(unbind(item['workflow']))
        blocks.validate_workflow(graph)
        if errors := services.blocks.validate_draft(graph):
            raise ValueError('共享定义需要修正：' + '; '.join(errors))
    remap = {}
    skill_id = 'shared-' + method_id
    with connect(services.projects.store.db_path) as db:
        existing_skill = db.execute("SELECT 1 FROM project_records WHERE project_id=? AND collection='skills' AND record_key=?", (project_id, skill_id)).fetchone()
    skill_document = None
    try:
        for item in payload['workflows']:
            member = await services.projects.add_member(project_id, item['name'], item['description'])
            remap[item['id']] = member['id']
        def rewrite(obj):
            if isinstance(obj, dict):
                name = obj.get('tool_name','')
                if isinstance(name,str) and name.startswith('workflow:'):
                    obj['tool_name'] = 'workflow:' + remap[name[9:]]
                for value in obj.values():rewrite(value)
            elif isinstance(obj,list):
                for value in obj:rewrite(value)
        for item in payload['workflows']:
            graph = unbind(item['workflow']); rewrite(graph)
            wid = remap[item['id']]
            draft = await services.workflow_store.get_draft(wid)
            await save_workflow(services,project_id,wid,SaveWorkflow(expected_revision=draft['revision'],workflow=WorkflowSpec.model_validate(graph)))
        if payload['skill']:
            item = payload['skill']
            skill_document = SkillDocument(
                **{k:item[k] for k in ('name','description','content')}, references=item.get('references', {}))
        else:
            skill_document = SkillDocument(name=row['name']+'使用说明',description=row['description'],
                content=f"用途：{row['description']}\n限制：{row['limitations']}\n工作流：{remap[payload['root']]}。使用 project_workflows inspect 查看当前输入，再通过 workflow_run 调用。模型、数据及连接需要在本项目配置。")
        await save_skill(services,project_id,skill_id,skill_document)
        result={'workflow_id':remap.get(payload.get('root')),'skill_id':skill_id,'source_id':method_id,'mapping':remap}
        with connect(services.projects.store.db_path) as db:
            db.execute('INSERT INTO shared_method_installs VALUES(?,?,?)',(project_id,method_id,json.dumps(result)))
        return result

    except BaseException:
        # Only definitions created by this attempt are removed. Existing project
        # members, inputs, runs and previously installed copies remain intact.
        def discard():
            with connect(services.projects.store.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                for wid in remap.values():
                    db.execute('DELETE FROM project_members WHERE project_id=? AND application_id=?', (project_id, wid))
                    db.execute('DELETE FROM draft_idempotency WHERE application_id=?', (wid,))
                    db.execute('DELETE FROM applications WHERE id=?', (wid,))
                if skill_document is not None and not existing_skill:
                    saved = db.execute("SELECT revision,value_json FROM project_records WHERE project_id=? AND collection='skills' AND record_key=?", (project_id, skill_id)).fetchone()
                    if saved and saved['revision'] == 1 and json.loads(saved['value_json']) == skill_document.model_dump(exclude={'expected_revision'}):
                        db.execute("DELETE FROM project_records WHERE project_id=? AND collection='skills' AND record_key=?", (project_id, skill_id))
        await asyncio.shield(asyncio.to_thread(discard))
        raise



def register_shared_routes(router, services, invoke):
    locks = {}

    @router.get('/space/shared-methods')
    async def listing(project_id: str):
        return visible(services,project_id)

    @router.post('/space/shared-methods/preview')
    async def preview(project_id: str, body: ShareMethod, request: Request):
        return await invoke(make_share,services,project_id,body,request.state.user,preview=True)

    @router.post('/space/shared-methods',status_code=201)
    async def share(project_id: str, body: ShareMethod, request: Request):
        return await invoke(make_share,services,project_id,body,request.state.user)

    @router.post('/space/shared-methods/{method_id}/install',status_code=201)
    async def add(project_id: str, method_id: str):
        async with locks.setdefault((project_id,method_id),asyncio.Lock()):
            return await invoke(install,services,project_id,method_id)
