"""One model response creates an editable draft; execution is a separate action."""
from __future__ import annotations

import json
import time
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from fastapi.encoders import jsonable_encoder

from .agent_core import collect_model_stream
from .models import ChatMessage, ContentBlock
from .workflow_models import WorkflowSpec


class GenerateWorkflow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instruction: str = Field(min_length=1, max_length=12000)
    workflow_id: str = ''
    expected_revision: int | None = None
    name: str = Field(default='新工作流', min_length=1, max_length=100)
    advanced_blocks: bool = False


class SaveWorkflow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    workflow: WorkflowSpec
    request_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)


async def save_workflow(services, project_id, workflow_id, body):
    await services.projects.member(project_id, workflow_id)
    draft = await services.workflow_store.get_draft(workflow_id)
    previous = draft['snapshot'].workflow.model_dump(mode='json')
    if draft['revision'] != body.expected_revision:
        from .project_store import ProjectConflict
        raise ProjectConflict('画布已被修改，请刷新后合并；本次未覆盖已有改动')
    result = await services.applications.apply_operations_atomically(workflow_id,
        expected_revision=body.expected_revision, expected_content_hash=draft['content_hash'],
        idempotency_key=body.request_key, operations=[{'op': 'replace_workflow', 'data': {'workflow': body.workflow.model_dump(mode='json')}}],
        change_context_operation='workflow_edit')
    return {**jsonable_encoder(result), 'workflow_id': workflow_id, 'previous_workflow': previous,
            'draft': jsonable_encoder(await services.workflow_store.get_draft(workflow_id))}


async def generate_workflow(services, project_id, body):
    from .project_store import ProjectConflict
    began = time.perf_counter()
    await services.projects.store.get(project_id)
    existing = None
    if body.workflow_id:
        await services.projects.member(project_id, body.workflow_id)
        existing = await services.workflow_store.get_draft(body.workflow_id)
        if body.expected_revision != existing['revision']:
            raise ProjectConflict('画布已更新，请刷新后重试生成')
    provider = services.local_agents.connections.provider(project_id)
    blocks = await services.projects.blocks_for(project_id)
    from .blocks import DEFAULT_WORKFLOW_BLOCKS
    existing_types = {n.type for n in existing['snapshot'].workflow.nodes} if existing else set()
    catalog = [{'type': b.type, 'description': b.description, 'config_schema': b.config_schema,
                'input_ports': jsonable_encoder(b.input_ports), 'output_ports': jsonable_encoder(b.output_ports)}
               for b in blocks.list() if body.advanced_blocks or b.type in DEFAULT_WORKFLOW_BLOCKS | existing_types]
    context = {'instruction': body.instruction, 'catalog': catalog,
               'workflow': existing['snapshot'].workflow.model_dump(mode='json') if existing else None,
               'models': await services.projects.store.records(project_id, 'model_resources')}
    started = time.perf_counter()
    request_id = str(uuid4())
    services.local_agents.event(project_id, 'workflow_generation_started', '开始生成工作流', request_id=request_id)
    response = await collect_model_stream(provider.stream(model='project',
        system='生成一个可编辑工作流，直接返回 JSON 对象 {"workflow":{"nodes":[],"edges":[]}}。'
               '使用给定积木 schema；每个节点具有 id/type/title/config/position:{x,y}，边具有 id/source/target。'
               '无需运行或测试，资源可稍后绑定；model_predict 使用 model_ref 与 dataset_id，未配置用空字符串。'
               'LLM 密钥由项目提供，不写进图。保持已有图中未要求修改的配置及布局。'
               '变量引用为 {"$ref":{"node_id":"节点id或$inputs","path":["字段"]}}。'
               '模型及代码节点输出位于 output 字段。只生成图，不调用工具。',
        messages=[ChatMessage(role='user', content=[ContentBlock(type='text', text=json.dumps(context, ensure_ascii=False))])],
        tools=[], max_output_tokens=16384, thinking_enabled=True, effort='medium'), timeout_seconds=180)
    seconds = time.perf_counter() - started
    services.local_agents.event(project_id, 'model_usage', '生成工作流', request_id=request_id,
        usage=response.usage.model_dump(mode='json'), seconds=seconds)
    text = ''.join(b.text or '' for b in response.blocks if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    payload = json.loads(text)
    workflow = WorkflowSpec.model_validate(payload.get('workflow', payload))
    blocks.validate_workflow(workflow)
    errors = services.blocks.validate_draft(workflow)
    if errors:
        raise ValueError('生成的配置需要修正：' + '; '.join(errors))
    workflow_id = body.workflow_id
    if not existing:
        member = await services.projects.add_member(project_id, body.name)
        workflow_id = member['id']
        existing = await services.workflow_store.get_draft(workflow_id)
    result = await save_workflow(services, project_id, workflow_id, SaveWorkflow(
        workflow=workflow, expected_revision=existing['revision']))
    result['usage'] = jsonable_encoder(response.usage)
    result['model_calls'] = 1
    result['seconds'] = seconds
    result['elapsed_seconds'] = time.perf_counter() - began
    services.local_agents.event(project_id, 'workflow_generated', '工作流已保存', request_id=request_id,
        workflow_id=workflow_id, elapsed_seconds=result['elapsed_seconds'])
    return result


def register_workflow_edit_routes(router, services, invoke):
    @router.post('/workflow-generation')
    async def generate(project_id: str, body: GenerateWorkflow):
        return await invoke(generate_workflow, services, project_id, body)

    @router.put('/workflows/{workflow_id}/draft')
    async def save(project_id: str, workflow_id: str, body: SaveWorkflow):
        return await invoke(save_workflow, services, project_id, workflow_id, body)
