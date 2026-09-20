"""Project model names resolve to immutable trained versions at execution time."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from .modeling_models import CandidateRequest


class ModelResource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    study_id: str = ''
    candidate_id: str = ''
    slot: int = Field(default=0, ge=0)
    expected_revision: int = Field(default=0, ge=0)


async def save_model(services, project_id, model_ref, body):
    import re
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', model_ref):
        raise ValueError('模型引用仅使用字母、数字、横线或下划线')
    value = body.model_dump(exclude={'expected_revision'})
    if bool(body.study_id) != bool(body.candidate_id):
        raise ValueError('绑定模型需同时指定研究和候选版本')
    if body.candidate_id:
        modeling = services.modeling
        study = await modeling.get(project_id, 'study', body.study_id)
        candidate = await modeling.get(project_id, 'candidate', body.candidate_id)
        if candidate['study_id'] != study['id']:
            raise ValueError('候选不属于此研究')
        trial = next((t for t in candidate['trials'] if t['slot'] == body.slot and t['status'] == 'completed'), None)
        if trial is None:
            raise ValueError('所选版本尚未训练完成')
        value.update(image=candidate['image'], code_sha256=candidate.get('code_sha256', ''), metrics=trial['metrics'])
    result = await services.projects.store.put_record(project_id, 'model_resources', model_ref, value, body.expected_revision)
    return result


async def model_resources(services, project_id):
    records = await services.projects.store.records(project_id, 'model_resources')
    result = []
    for record in records:
        value = record['value']
        status = 'unbound'
        error = ''
        if value.get('candidate_id'):
            try:
                candidate = await services.modeling.get(project_id, 'candidate', value['candidate_id'])
                status = 'ready' if any(t['slot'] == value['slot'] and t['status'] == 'completed' for t in candidate['trials']) else candidate['status']
            except (KeyError, ValueError) as cause:
                status, error = 'failed', str(cause)
        result.append({'model_ref': record['key'], 'revision': record['revision'], **value, 'status': status, 'error': error})
    return result


def binding_from_context(project, model_ref):
    resources = project.get('model_resources', {})
    binding = resources.get(model_ref)
    if not binding or not binding.get('candidate_id'):
        raise ValueError(f'模型“{model_ref or "未选择"}”尚未绑定可用版本，请在项目模型页面绑定后重新运行')
    return binding


async def start_training(services, project_id, study_id, candidate):
    from uuid import uuid4
    import asyncio
    projects = services.projects
    await projects.store.get(project_id)
    async with projects.locks.setdefault(project_id, asyncio.Lock()):
        value = await services.modeling.candidate(project_id, study_id, candidate)
        task, created = await projects.store.create_task(str(uuid4()), project_id, 'training/' + value['id'],
            'training', '', {'study_id': study_id, 'candidate_id': value['id']}, '', {},
            purpose='business', feedback_task_id=value.get('feedback_task_id', ''))
        if created:
            await projects._launch(task)
        return await projects.task(project_id, task['id'])


async def run_compute(services, task):
    import asyncio
    from uuid import uuid4
    projects = services.projects
    try:
        if task['mode'] == 'training':
            inputs = task['inputs']
            value = await services.modeling.run_candidate(task['project_id'], inputs['study_id'], inputs['candidate_id'], task['id'], str(uuid4()), pause_when_done=True)
            status = 'succeeded' if value['status'] == 'completed' else 'failed'
        else:
            context = {'project_id': task['project_id'], 'task_id': task['id'], 'model_resources': task['inputs']['model_resources']}
            value = await services.modeling.run_block(context, 'model_predict', task['inputs'], str(uuid4()))
            status = 'succeeded'
        await projects.store.update_task(task['id'], status=status, outputs=value, error=value.get('error', ''))
    except asyncio.CancelledError:
        await projects.store.update_task(task['id'], status='interrupted')
        raise
    except Exception as error:
        await projects.store.update_task(task['id'], status='failed', error=str(error))


class PredictResource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1)
    request_key: str = Field(min_length=1, max_length=160)


async def start_prediction(services, project_id, model_ref, body):
    import asyncio
    from uuid import uuid4
    projects = services.projects
    await services.modeling.get(project_id, 'dataset', body.dataset_id)
    async with projects.locks.setdefault(project_id, asyncio.Lock()):
        key = 'prediction/' + body.request_key
        previous = await projects.store.task_for_request(project_id, key)
        if previous:
            if previous['mode'] != 'prediction' or previous['inputs']['dataset_id'] != body.dataset_id or previous['inputs']['model_ref'] != model_ref:
                from .project_store import ProjectConflict
                raise ProjectConflict('此请求标识已用于不同的预测')
            return await projects.task(project_id, previous['id'])
        resources = {m['model_ref']: m for m in await model_resources(services, project_id)}
        task, created = await projects.store.create_task(str(uuid4()), project_id, key, 'prediction', '',
            {'dataset_id': body.dataset_id, 'model_ref': model_ref, 'model_resources': resources}, '', {})
        if created:
            await projects._launch(task)
        return await projects.task(project_id, task['id'])


def register_resource_routes(router, services, invoke):

    @router.post('/models/{model_ref}/predict', status_code=202)
    async def predict(project_id: str, model_ref: str, body: PredictResource):
        return await invoke(start_prediction, services, project_id, model_ref, body)

    @router.get('/models')
    async def models(project_id: str):
        return await invoke(model_resources, services, project_id)

    @router.put('/models/{model_ref}')
    async def save(project_id: str, model_ref: str, body: ModelResource):
        return await invoke(save_model, services, project_id, model_ref, body)

    @router.post('/modeling/studies/{study_id}/train', status_code=202)
    async def train(project_id: str, study_id: str, body: CandidateRequest):
        return await invoke(start_training, services, project_id, study_id, body)
