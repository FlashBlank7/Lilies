"""One model response creates an editable draft; execution is a separate action."""
from __future__ import annotations

import json
import time
from copy import deepcopy
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
    workflow_path: list[str] = Field(default_factory=list, max_length=20)
    node_ids: list[str] = Field(default_factory=list, max_length=500)


def scoped_workflow(workflow, path):
    """Resolve an explicitly chosen loop/iteration body in the draft snapshot."""
    current = workflow
    for node_id in path:
        node = next((n for n in current['nodes'] if n['id'] == node_id), None)
        if not node or node['type'] not in {'loop', 'iteration'} or not isinstance(node['config'].get('workflow'), dict):
            raise ValueError('未找到所选循环的内部流程，请刷新画布')
        current = node['config']['workflow']
    return current


def editable_fragment(workflow, node_ids):
    ids = set(node_ids)
    if not ids <= {n['id'] for n in workflow['nodes']}:
        raise ValueError('所选节点已不存在，请重新选择')
    return {**workflow, 'nodes': [n for n in workflow['nodes'] if n['id'] in ids],
            'edges': [e for e in workflow['edges'] if e['source'] in ids and e['target'] in ids]}


def merge_generated_workflow(original, generated, path, node_ids):
    result = deepcopy(original)
    target = scoped_workflow(result, path)
    originals = {n['id']: n for n in target['nodes']}
    generated = {**generated, 'edges': generated.get('edges', []),
                 'nodes': [{**originals.get(n['id'], {}), **n} for n in generated.get('nodes', [])]}
    if node_ids:
        selected = set(node_ids)
        outside = {n['id'] for n in target['nodes']} - selected
        if outside & {n['id'] for n in generated['nodes']}:
            raise ValueError('生成结果包含选区外的节点；请扩大选区或只返回选中的节点')
        # Keep all boundary edges and everything outside the selection exactly.
        # Deleting a boundary endpoint needs an expanded selection, not a silent
        # rewrite of a neighbor's configuration or references.
        replacement = {**target,
            'nodes': [n for n in target['nodes'] if n['id'] not in selected] + generated['nodes'],
            'edges': [e for e in target['edges'] if not (e['source'] in selected and e['target'] in selected)] + generated['edges']}
    else:
        replacement = {**generated, 'viewport': target.get('viewport', {})}
    target.clear()
    target.update(replacement)
    return WorkflowSpec.model_validate(result)


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
    elif body.workflow_path or body.node_ids:
        raise ValueError('请先选择已有工作流，再修改选区或循环内部')
    original = existing['snapshot'].workflow.model_dump(mode='json') if existing else None
    target = scoped_workflow(original, body.workflow_path) if original is not None else None
    fragment = editable_fragment(target, body.node_ids) if body.node_ids else target
    provider = services.local_agents.connections.provider(project_id, role='generation')
    blocks = await services.projects.blocks_for(project_id)
    from .blocks import DEFAULT_WORKFLOW_BLOCKS
    existing_types = {n['type'] for n in target['nodes']} if target else set()
    catalog = [{'type': b.type, 'description': b.description, 'config_schema': b.config_schema,
                'input_ports': jsonable_encoder(b.input_ports), 'output_ports': jsonable_encoder(b.output_ports)}
               for b in blocks.list() if body.advanced_blocks or b.type in DEFAULT_WORKFLOW_BLOCKS | existing_types]
    context = {'instruction': body.instruction, 'catalog': catalog,
               'workflow': fragment,
               'scope': {'workflow_path': body.workflow_path, 'node_ids': body.node_ids},
               'read_only_context': target if body.node_ids else None,
               'models': await services.projects.store.records(project_id, 'model_resources')}
    started = time.perf_counter()
    request_id = str(uuid4())
    services.local_agents.event(project_id, 'workflow_generation_started', '开始生成工作流', request_id=request_id)
    response = await collect_model_stream(provider.stream(model='project',
        system='生成一个可编辑工作流，直接返回 JSON 对象 {"workflow":{"nodes":[],"edges":[]}}。'
               '使用给定积木 schema；每个节点具有 id/type/title/config/position:{x,y}，边具有 id/source/target。'
               '无需运行或测试，资源可稍后绑定；model_predict 使用 model_ref 与 dataset_id，未配置用空字符串。'
               'LLM 密钥由项目提供，不写进图。保持已有图中未要求修改的配置及布局。'
               'workflow 是需要返回的完整可编辑范围；read_only_context 仅供理解选区外节点。'
               '选区编辑只返回选中节点和新增节点及它们之间的边，保留连接选区外节点的端点 id。'
               '循环编辑只返回循环内部流程，平台会放回原位置，外层无需返回。'
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
    if not isinstance(payload, dict):
        raise ValueError('生成结果必须是工作流对象，请重试或调整描述')
    generated = payload.get('workflow', payload)
    workflow = WorkflowSpec.model_validate(generated)
    if original is not None:
        workflow = merge_generated_workflow(original, generated, body.workflow_path, body.node_ids)
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
