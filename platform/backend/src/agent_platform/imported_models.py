"""Bind existing preprocessing pipelines through the same offline compute service."""
import asyncio
import hashlib
from pathlib import Path
import re
import shutil
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from .modeling_models import DataMapping
from .workspace_copy import copy_workspace_file
from .project_store import ProjectConflict


class ImportModel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    source_path: str = Field(min_length=1)
    environment: str = Field(min_length=1, max_length=240)
    mapping: DataMapping = Field(default_factory=DataMapping)
    feature_columns: list[str] = Field(default_factory=list, max_length=500)
    expected_revision: int = Field(default=0, ge=0)


async def environments(services):
    process = await asyncio.create_subprocess_exec('docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, error = await asyncio.wait_for(process.communicate(), 10)
    if process.returncode:
        raise ValueError('无法读取本地计算环境：' + error.decode()[-300:])
    return sorted({name for name in output.decode().splitlines() if name.startswith('lilies-modeling:') and '<none>' not in name})


async def import_model(services, project_id, model_ref, body):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', model_ref):
        raise ValueError('模型引用仅使用字母、数字、横线或下划线')
    await services.projects.store.get(project_id)
    previous = await services.projects.store.get_record(project_id, 'model_resources', model_ref)
    if previous['revision'] != body.expected_revision:
        raise ProjectConflict('模型绑定已改变，请刷新后重试')
    modeling = services.modeling
    source = modeling.source(project_id, body.source_path)
    if source.suffix.lower() not in {'.joblib', '.pkl'}:
        raise ValueError('请选择包含预处理的 joblib／pkl Pipeline 模型包')
    if source.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('模型包上限为 256 MB')
    if body.environment not in await environments(services):
        raise ValueError('请选择平台已有的本地建模环境，不自动下载或更换环境')
    image = await modeling.image(body.environment)
    ident = str(uuid4())
    folder = modeling.path(project_id, ident)
    folder.mkdir(parents=True)
    try:
        copy_workspace_file(source, folder / 'model.joblib')
        digest = hashlib.sha256((folder / 'model.joblib').read_bytes()).hexdigest()
        dataset = {'id': ident, 'mapping': body.mapping.model_dump(), 'files': {}}
        result = await modeling.compute(project_id, dataset, {'action': 'inspect_model', 'model': '/data/model.joblib',
            'feature_columns': body.feature_columns}, folder / 'validation', image=image, timeout=120)
        environment = await modeling.snapshot_environment(image)
        doc = {'id': ident, 'status': 'validated', 'image': image, 'sha256': digest,
               'mapping': body.mapping.model_dump(), 'feature_columns': result['feature_columns'],
               'classes': result['classes'], 'source_path': body.source_path, 'environment': environment.name}
        await modeling.put(project_id, 'imported_model', doc)
        value = {'name': body.name, 'import_id': ident, 'image': image, 'sha256': digest, 'feature_columns': result['feature_columns'],
                 'status': 'ready', 'source_path': body.source_path}
        return await services.projects.store.put_record(project_id, 'model_resources', model_ref, value, body.expected_revision)
    except BaseException:
        # If a concurrent binding won, this immutable version is retained in the
        # modeling store; otherwise remove an incomplete file package.
        try:
            await modeling.get(project_id, 'imported_model', ident)
        except KeyError:
            shutil.rmtree(folder)
        raise


async def predict_import(modeling, project_id, args, binding, run_id, node_id):
    model = await modeling.get(project_id, 'imported_model', binding['import_id'])
    await modeling.image(model['image'])
    folder = modeling.path(project_id, model['id'])
    source = folder / 'model.joblib'
    if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != model['sha256']:
        raise ValueError('模型包已缺失或内容改变，请重新导入并绑定')
    dataset = await modeling.get(project_id, 'dataset', args['dataset_id'])
    dataset = {**dataset, 'mapping': model['mapping']}
    target = modeling.path(project_id, dataset['id']) / ('run-' + run_id + '-' + hashlib.sha256(node_id.encode()).hexdigest()[:10])
    result = await modeling.compute(project_id, dataset, {'action': 'predict', 'model': '/model', 'engine': 'sklearn',
        'features': {'columns': model['feature_columns']}, 'feature_columns': model['feature_columns'], 'classes': model['classes']},
        target, image=model['image'], extra_mounts=[(folder, '/model')])
    result['model_version'] = {'import_id': model['id'], 'image': model['image'], 'sha256': model['sha256']}
    return modeling.export_prediction(project_id, dataset, target, result)
