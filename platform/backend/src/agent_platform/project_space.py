"""A project's callable workflows and files form its shared working context."""
import asyncio

from pydantic import BaseModel, ConfigDict, Field

from .local_agent_tools import ProjectTools, ProjectFile
from .workflow_models import WorkflowSpec
from .project_workflow_edit import SaveWorkflow, save_workflow


async def space(services, project_id):
    project = await services.projects.store.get(project_id)
    blocks = await services.projects.blocks_for(project_id)
    workflows = []
    for member in project['members']:
        if member.get('purpose') == 'test':
            continue
        draft = await services.workflow_store.get_draft(member['id'])
        snapshot = draft['snapshot']
        workflows.append({'id': member['id'], 'name': snapshot.name,
            'description': snapshot.description, 'revision': draft['revision'],
            'node_count': len(snapshot.workflow.nodes), 'allowed': blocks.supports_workflow(snapshot.workflow),
            'inputs': [f for n in snapshot.workflow.nodes if n.type == 'start' for f in n.config.get('inputs', [])]})
    files = await asyncio.to_thread(ProjectTools(services, project_id, services.local_agents).file, ProjectFile(action='list'))
    return {'project_id': project_id, 'workflows': workflows, 'files': files['files'], 'files_truncated': files['truncated']}


class AddWorkflow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=1000)
    workflow: WorkflowSpec


async def add_workflow(services, project_id, body):
    project = await services.projects.store.get(project_id)
    blocks = await services.projects.blocks_for(project_id)
    blocks.validate_workflow(body.workflow)
    errors = services.blocks.validate_draft(body.workflow)
    if errors:
        raise ValueError('工作流配置需要修正：' + '; '.join(errors))
    members = {m['id'] for m in project['members']}
    def check_calls(value):
        if isinstance(value, dict):
            target = value.get('tool_name', '')
            if isinstance(target, str) and target.startswith('workflow:') and target[9:] not in members:
                raise ValueError('引用的子工作流尚未加入本项目，请先加入子流程并更新引用')
            for child in value.values():
                check_calls(child)
        elif isinstance(value, list):
            for child in value:
                check_calls(child)
    check_calls(body.workflow.model_dump())
    member = await services.projects.add_member(project_id, body.name, body.description)
    draft = await services.workflow_store.get_draft(member['id'])
    return await save_workflow(services, project_id, member['id'], SaveWorkflow(
        expected_revision=draft['revision'], workflow=body.workflow))


def register_space_routes(router, services, invoke):
    @router.get('/space/official-workflows')
    async def official(project_id: str):
        from .official_workflows import catalog
        await services.projects.store.get(project_id)
        return catalog()

    @router.post('/space/official-workflows/{template_id}', status_code=201)
    async def install_official(project_id: str, template_id: str):
        from .official_workflows import install
        return await invoke(install, services, project_id, template_id)

    @router.get('/space')
    async def listing(project_id: str):
        return await invoke(space, services, project_id)

    @router.post('/space/workflows', status_code=201)
    async def add(project_id: str, body: AddWorkflow):
        return await invoke(add_workflow, services, project_id, body)
