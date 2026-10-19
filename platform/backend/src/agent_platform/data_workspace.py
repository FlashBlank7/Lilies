"""Data-first workspace. Objects are views of existing resources, operations are workflows."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import base64
import io
import threading
import json
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from .data_workspace_tables import Preview, Selection, TableViews, digest
from .modeling_models import Contract, DataMapping, DatasetRequest, DataAnalysisPlan
from .official_workflows import graph, node, ref
from .project_store import ProjectConflict
from .project_workflow_edit import SaveWorkflow, save_workflow
from .workflow_models import WorkflowSpec


PDF_LOCK = threading.Lock()


def pdf_page(path, index):
    import pypdfium2 as pdfium
    if path.stat().st_size > 20_000_000:
        raise ValueError('PDF 超过 20 MB，请下载查看完整内容')
    # PDFium is not thread-safe; only one document render enters the library.
    with PDF_LOCK:
        try:
            with pdfium.PdfDocument(path) as document:
                if index >= len(document):
                    raise ValueError('PDF 页码超出范围')
                page = document[index]
                try:
                    width, height = page.get_size()
                    if min(width, height) <= 0:
                        raise ValueError('PDF 页面尺寸无效')
                    bitmap = page.render(scale=min(2, 1500 / max(width, height)))
                    try:
                        output = io.BytesIO()
                        bitmap.to_pil().save(output, format='PNG')
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                return {'format':'pdf','page':index,'pages':len(document),
                        'image':'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()}
        except pdfium.PdfiumError as error:
            raise ValueError('无法预览这个 PDF，请检查密码或下载查看') from error


TABLES = {'.csv', '.tsv', '.xlsx'}
TOOLS = [
    {'id': 'select', 'name': '选取数据', 'description': '保存选中的字段和行，保留来源与字段含义。'},
    {'id': 'prepare', 'name': '整理与理解数据', 'description': '按字段规则解析原值，保留缺失、待确认和格式错误。'},
    {'id': 'profile', 'name': '数据概况', 'description': '查看数据结构、分布与状态。'},
    {'id': 'spearman', 'name': 'Spearman 相关分析', 'description': '分析至少两个数值字段，展示有效样本数与热力图。'},
    {'id': 'outliers', 'name': '异常值处理', 'description': '选择 IQR 或业务范围；默认只标记。'},
    {'id': 'train', 'name': '训练与评价', 'description': '选择特征、目标与分组方式，比较基线并保留独立测试。'},
    {'id': 'predict', 'name': '批量预测', 'description': '选择已训练模型，沿用模型规则，逐行保留预测或不可用原因。'},
]


class ObjectRef(Contract):
    id: str
    version: str


class Operation(Contract):
    request_key: str = Field(min_length=1, max_length=160)
    source: ObjectRef
    tool: str
    mapping: DataMapping | None = None
    selection: Selection = Field(default_factory=Selection)
    parameters: dict = Field(default_factory=dict)
    model: ObjectRef | None = None
    workflow_revision: int | None = None
    input_name: str = ''
    bindings: dict[str, ObjectRef] = Field(default_factory=dict)


class SaveChain(Contract):
    name: str = Field(min_length=1, max_length=100)
    request_key: str = Field(min_length=1, max_length=160)


def file_kind(path):
    suffix = Path(path).suffix.lower()
    return ('table' if suffix in TABLES else 'image' if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
            else 'document' if suffix in {'.pdf', '.docx', '.md', '.txt', '.html', '.htm', '.json', '.py'} else 'file')


class DataWorkspace:
    def __init__(self, services):
        self.services = services
        self.projects = services.projects
        self.modeling = services.modeling
        self.views = TableViews()
        self.locks = {}

    async def records(self, pid):
        return await self.projects.store.records(pid, 'workspace_operations')

    async def object(self, pid, ident):
        await self.projects.store.get(pid)
        kind, _, key = ident.partition(':')
        if kind == 'file':
            path = self.modeling.source(pid, key)
            version = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
            content_hash = version
            companions = {}
            inherited_mapping = None
            if path.name in {'processed.csv', 'predictions.csv'}:
                names = {'state':'data-state.json','origins':'source-rows.json'} if path.name == 'processed.csv' else {'origins':'source-rows.json'}
                for role, name in names.items():
                    relative = Path(key).with_name(name).as_posix()
                    if path.with_name(name).is_file():
                        companion = self.modeling.source(pid, relative)
                        companions[role] = relative
                        if role == 'state':
                            inherited_mapping = json.loads(companion.read_text()).get('mapping')
                if companions:
                    version = digest([content_hash, {k: hashlib.sha256(self.modeling.source(pid,v).read_bytes()).hexdigest() for k,v in companions.items()}])
            settings = await self.projects.store.get_record(pid, 'workspace_views', digest(ident))
            mapping = settings['value']['mapping'] if settings.get('found') and settings['value'].get('version') == version else inherited_mapping
            return {'id': ident, 'kind': file_kind(key), 'mapping': mapping, 'name': path.name, 'path': key,
                    'version': version, 'content_sha256': content_hash, 'companions': companions, 'bytes': path.stat().st_size, 'format': path.suffix.lower()}
        if kind == 'dataset':
            data = await self.modeling.get(pid, 'dataset', key)
            return {'id': ident, 'kind': 'table', 'name': data['name'], 'dataset_id': key,
                    'version': digest([data['files'], data['mapping']]), 'mapping': data['mapping'],
                    'format': Path(data['files']['source']['name']).suffix.lower(), 'source_id': 'dataset:' + data['derived_from'] if data.get('derived_from') else '',
                    'transformation': data.get('transformation')}
        if kind == 'model':
            candidate_id, _, raw_slot = key.partition(':')
            candidate = await self.modeling.get(pid, 'candidate', candidate_id)
            slot = int(raw_slot)
            trial = next((t for t in candidate['trials'] if t['slot'] == slot and t['status'] == 'completed'), None)
            if not trial:
                raise ValueError('模型尚未训练完成')
            study = await self.modeling.get(pid, 'study', candidate['study_id'])
            data = await self.modeling.get(pid, 'dataset', study['dataset_id'])
            return {'id': ident, 'kind': 'model', 'name': trial['model'] + ' · 试验 ' + str(slot + 1),
                    'version': digest([candidate_id, slot, candidate['image'], trial]),
                    'binding': {'study_id': study['id'], 'candidate_id': candidate_id, 'slot': slot},
                    'metrics': trial.get('metrics'), 'classification': trial.get('classification'),
                    'evaluation': study['evaluation'], 'test_result': study.get('test_result') if (study.get('best') or {}).get('candidate_id') == candidate_id and (study.get('best') or {}).get('slot') == slot else None,
                    'mapping': data['mapping'], 'features': candidate['features'],
                    'download': f'/api/v1/projects/{pid}/modeling/studies/{study["id"]}/candidates/{candidate_id}/download?slot={slot}'}
        if kind == 'result':
            opid, _, index = key.partition(':')
            operation = await self.operation(pid, opid)
            try:
                return operation['results'][int(index)]
            except (IndexError, ValueError) as error:
                raise ValueError('这份结果尚未产生，请刷新运行状态') from error
        raise ValueError('不支持的数据对象')

    async def checked(self, pid, reference):
        obj = await self.object(pid, reference.id)
        if obj['version'] != reference.version:
            raise ProjectConflict('数据或模型版本已经变化，请刷新后重新选择；本次没有启动运行')
        if obj.get('dataset_id'):
            data = await self.modeling.get(pid, 'dataset', obj['dataset_id'])
            for item in data['files'].values():
                path = self.modeling.path(pid, data['id']) / item['name']
                actual = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
                if actual != item['sha256']:
                    raise ProjectConflict('固定数据版本的文件已被改变，请重新导入；本次没有启动运行')
        return obj

    async def table_config(self, pid, obj, mapping=None):
        if obj['kind'] != 'table':
            raise ValueError('此工具需要表格数据，请选择 CSV、TSV、Excel 或中间数据表')
        if obj.get('dataset_id'):
            data = await self.modeling.get(pid, 'dataset', obj['dataset_id'])
            if mapping is not None and mapping.model_dump() != data['mapping']:
                if data['files'].get('state') and mapping.field_rules != DataMapping.model_validate(data['mapping']).field_rules:
                    raise ValueError('中间表的字段含义已经固定；请回到原始数据修改规则后重新整理')
            return {'mapping': mapping.model_dump() if mapping else data['mapping'],
                    **{k: str(self.modeling.path(pid, data['id']) / v['name']) for k, v in data['files'].items()}}
        return {'source': str(self.modeling.source(pid, obj['path'])), 'mapping': (mapping or DataMapping.model_validate(obj.get('mapping') or {})).model_dump(),
                **{k: str(self.modeling.source(pid, v)) for k,v in obj.get('companions', {}).items()}}

    async def preview(self, pid, body):
        obj = await self.checked(pid, ObjectRef(id=body.object_id, version=body.version)) if body.version else await self.object(pid, body.object_id)
        if obj['kind'] == 'table':
            config = await self.table_config(pid, obj, body.mapping)
            return {**await asyncio.to_thread(self.views.preview, config, body), 'object': obj}
        if obj['kind'] in {'model', 'analysis'}:
            return {'object': obj, 'result': obj.get('value', obj)}
        if obj.get('path'):
            path = self.modeling.source(pid, obj['path'])
            if path.suffix.lower() == '.pdf':
                return {'object':obj, **await asyncio.to_thread(pdf_page, path, body.offset)}
            if path.suffix.lower() == '.docx':
                from .project_knowledge import parse_document
                if path.stat().st_size > 20_000_000:
                    raise ValueError('文档过大，请下载查看完整内容')
                sections = await asyncio.to_thread(parse_document, path.read_bytes(), path.name)
                return {'object': obj, 'text': '\n\n'.join(s['text'] for s in sections), 'format': 'text'}
            if path.suffix.lower() in {'.md', '.txt', '.json', '.py', '.html', '.htm'}:
                with path.open('rb') as stream:
                    content = stream.read(2_000_001)
                return {'object': obj, 'text': content[:2_000_000].decode('utf-8-sig', errors='replace'),
                        'truncated': len(content) > 2_000_000, 'format': path.suffix.lower().lstrip('.')}
        return {'object': obj}

    async def all_modeling(self, pid, kind):
        values = []
        while True:
            page = await self.modeling.list(pid, kind, offset=len(values), limit=100, summary=True)
            values.extend(page)
            if len(page) < 100:
                return values

    async def catalog(self, pid):
        from .project_space import space
        value = await space(self.services, pid)
        workflows = []
        for item in value['workflows']:
            if not item['node_count']:
                continue
            draft = await self.services.workflow_store.get_draft(item['id'])
            outputs = [k for n in draft['snapshot'].workflow.nodes if n.type == 'end' for k in n.config.get('outputs', {})]
            workflows.append({**item, 'id': 'workflow:' + item['id'], 'outputs': outputs})
        candidates = await self.all_modeling(pid, 'candidate')
        models = [await self.object(pid, f'model:{c["id"]}:{t["slot"]}') for c in candidates for t in c['trials'] if t['status'] == 'completed']
        return {'tools': TOOLS, 'workflows': workflows, 'models': models}

    async def listing(self, pid, offset=0, limit=50):
        root = self.projects.workspace(pid)
        await self.projects.store.get(pid)
        def files():
            return sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                          if p.is_file() and not p.is_symlink() and not p.relative_to(root).as_posix().startswith('results/workspace-inputs/') and not any(s.startswith('.') for s in p.relative_to(root).parts))
        paths = await asyncio.to_thread(files)
        identifiers = ['file:' + p for p in paths]
        data = await self.all_modeling(pid, 'dataset')
        identifiers += ['dataset:' + d['id'] for d in data]
        candidates = await self.all_modeling(pid, 'candidate')
        identifiers += ['model:' + c['id'] + ':' + str(t['slot']) for c in candidates for t in c['trials'] if t['status'] == 'completed']
        objects = []
        for ident in identifiers[offset:offset + limit]:
            try:
                objects.append(await self.object(pid, ident))
            except (ValueError, OSError):
                continue
        records = sorted(await self.records(pid), key=lambda r: r['updated_at'])[-100:]
        operations = [await self.operation(pid, r['key'], r) for r in records]
        return {'objects': objects, 'operations': operations, 'total': len(identifiers),
                'offset': offset, 'has_more': offset + limit < len(identifiers)}

    async def operation(self, pid, opid, record=None):
        record = record or await self.projects.store.get_record(pid, 'workspace_operations', opid)
        if not record.get('found', True) or not record.get('value'):
            raise KeyError('没有找到本次处理')
        value = deepcopy(record['value'])
        task = await self.projects.task(pid, value['task_id']) if value.get('task_id') else None
        result = {**value, 'id': opid, 'created_at': record.get('updated_at'), 'status': task['status'] if task else 'configuring',
                  'error': task.get('error', '') if task else '', 'results': [],
                  'task': {k: task[k] for k in ('id', 'created_at', 'updated_at')} if task else None}
        seen = {value['source']['id'], 'dataset:' + str(value.get('dataset_id', ''))}
        async def add(obj):
            key = obj['id']
            if key not in seen:
                seen.add(key)
                result['results'].append({**obj, 'operation_id': opid, 'source_id': value['source']['id']})
        async def visit(output):
            if isinstance(output, list):
                for child in output:
                    await visit(child)
                return
            if isinstance(output, str) and output.startswith(('results/', 'solution/')):
                try:
                    await add(await self.object(pid, 'file:' + output))
                except (ValueError, OSError):
                    pass
            if not isinstance(output, dict):
                return
            if isinstance(output.get('dataset_id'), str):
                await add(await self.object(pid, 'dataset:' + output['dataset_id']))
            if isinstance(output.get('rows'), int) and (output.get('operation') in {'spearman', 'outliers', 'numeric', 'prepare', 'select'} or 'columns' in output):
                await add({'id': f'result:{opid}:{len(result["results"])}', 'kind': 'analysis', 'name': '分析结果',
                           'version': digest(output), 'value': output})
            if isinstance(output.get('trials'), list) and output.get('id'):
                for trial in output['trials']:
                    if trial['status'] == 'completed':
                        await add(await self.object(pid, f'model:{output["id"]}:{trial["slot"]}'))
            artifacts = list(output.get('artifacts', [])) if isinstance(output.get('artifacts'), list) else []
            if output.get('project_path'):
                artifacts.insert(0, {'file_path': output['project_path'], 'label': '逐行预测结果'})
            for artifact in artifacts:
                path = artifact.get('file_path') if isinstance(artifact, dict) else None
                if path:
                    try:
                        obj = await self.object(pid, 'file:' + path)
                        await add({**obj, 'name': artifact.get('label', obj['name'])})
                    except (ValueError, OSError):
                        pass
            for k, child in output.items():
                if k not in {'mapping', 'data_state', 'artifacts', 'trials'}:
                    await visit(child)
        if task:
            # Only completed step outputs from this task, including partial failure.
            for run in task.get('runs', []):
                if run['application_id'] != value['workflow_id']:
                    continue
                saved = await self.services.workflow_store.get_run(run['id'])
                state = saved['state']
                for n in state.snapshot.workflow.nodes:
                    if n.id in state.completed and n.type not in {'start', 'end'}:
                        await visit(state.outputs.get(n.id, {}))
                if not result['results'] and saved['status'] == 'succeeded':
                    output = saved.get('outputs', {})
                    await add({'id': f'result:{opid}:0', 'kind': 'analysis', 'name': '工作流结果',
                               'version': digest(output), 'value': output})
        return result

    async def dataset(self, pid, obj, mapping):
        if obj.get('dataset_id'):
            data = await self.modeling.get(pid, 'dataset', obj['dataset_id'])
            if mapping and mapping.model_dump() != data['mapping']:
                await self.table_config(pid, obj, mapping)
                return await self.modeling.revise_dataset(pid, data['id'], mapping)
            return data
        result = await self.modeling.dataset(pid, DatasetRequest(source_path=obj['path'], mapping=mapping or DataMapping.model_validate(obj.get('mapping') or {})))
        if result['files']['source']['sha256'] != obj.get('content_sha256', obj['version']):
            raise ProjectConflict('读取期间文件发生变化，请刷新后重新选择')
        return result

    async def bound_value(self, pid, obj, field, mapping=None):
        kind = field.get('resource_kind') or ('dataset' if field['name'] == 'dataset_id' else 'file' if field.get('type') == 'file' or field['name'] in {'source_path', 'file_path'} else '')
        if kind == 'dataset':
            if obj['kind'] != 'table':
                raise ValueError('输入“' + field['name'] + '”需要表格数据集')
            data = await self.dataset(pid, obj, mapping)
            if field.get('required_columns'):
                config = await self.table_config(pid, await self.object(pid, 'dataset:' + data['id']))
                frame, _ = await asyncio.to_thread(self.views.read, config)
                if not set(field['required_columns']) <= set(frame.columns):
                    raise ValueError('工作流需要字段：' + '、'.join(field['required_columns']))
            return data['id']
        if kind == 'model' and obj['kind'] == 'model':
            if field.get('type') == 'object':
                return obj['binding']
            from .project_resources import ModelResource, save_model
            binding_name = 'workspace_' + digest(obj['binding'])[:24]
            record = await self.projects.store.get_record(pid, 'model_resources', binding_name)
            if not record['found']:
                await save_model(self.services, pid, binding_name, ModelResource(name=obj['name'], **obj['binding']))
            return binding_name
        if kind == 'result' and obj['kind'] == 'analysis':
            return obj['value']
        if kind == 'file' and (obj.get('path') or obj.get('dataset_id')):
            if field.get('accepted_formats') and obj['format'].lstrip('.') not in [v.lstrip('.') for v in field['accepted_formats']]:
                raise ValueError('文件类型不匹配，支持：' + '、'.join(field['accepted_formats']))
            if field.get('required_columns'):
                config = await self.table_config(pid, obj, mapping)
                frame, _ = await asyncio.to_thread(self.views.read, config)
                missing = set(field['required_columns']) - set(frame.columns)
                if missing:
                    raise ValueError('工作流缺少必需字段：' + '、'.join(sorted(missing)))
            from .workspace_copy import copy_workspace_file
            companions = {}
            if obj.get('dataset_id'):
                data = await self.modeling.get(pid, 'dataset', obj['dataset_id'])
                source = self.modeling.path(pid, data['id']) / data['files']['source']['name']
                expected = data['files']['source']['sha256']
                companions = {k: self.modeling.path(pid, data['id']) / v['name'] for k,v in data['files'].items() if k in {'state','origins'}}
            else:
                source = self.modeling.source(pid, obj['path'])
                expected = obj.get('content_sha256', obj['version'])
                if source.name in {'processed.csv', 'predictions.csv'}:
                    companions = {k: self.modeling.source(pid, v) for k,v in obj.get('companions', {}).items()}
            filename = 'processed.csv' if companions else source.name
            relative = Path('results/workspace-inputs') / str(uuid4()) / filename
            destination = self.projects.workspace(pid) / relative
            destination.parent.mkdir(parents=True, exist_ok=False)
            await asyncio.to_thread(copy_workspace_file, source, destination)
            for key, companion in companions.items():
                await asyncio.to_thread(copy_workspace_file, companion, destination.with_name({'state':'data-state.json','origins':'source-rows.json'}[key]))
            if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
                raise ProjectConflict('读取期间文件发生变化，请刷新后重新选择')
            return relative.as_posix()
        raise ValueError('无法确定输入“' + field['name'] + '”如何使用此对象，请在流程输入中声明资源类型')

    async def run(self, pid, body):
        opid = digest(body.request_key)[:32]
        async with self.locks.setdefault(pid, asyncio.Lock()):
            record = await self.projects.store.get_record(pid, 'workspace_operations', opid)
            fingerprint = digest(body.model_dump())
            if record['found']:
                if record['value']['fingerprint'] != fingerprint:
                    raise ProjectConflict('同一次提交的配置发生变化，请使用新的运行请求')
                if record['value'].get('task_id'):
                    return await self.operation(pid, opid, record)
            obj = await self.checked(pid, body.source)
            name = next((t['name'] for t in TOOLS if t['id'] == body.tool), '')
            steps = [node('start', 'start', '本次输入', inputs=[])]
            selected = body.selection.model_dump()
            source_data = None
            input_objects = [{'role':'主要数据', **{k: obj[k] for k in ('id','version','name')}}]
            target_workflow = ''
            if body.tool.startswith('workflow:'):
                target_workflow = body.tool[9:]
                await self.projects.member(pid, target_workflow)
                draft = await self.services.workflow_store.get_draft(target_workflow)
                if draft['revision'] != body.workflow_revision:
                    raise ProjectConflict('工作流已修改，请重新打开配置窗口')
                name = draft['snapshot'].name
                fields = [f for n in draft['snapshot'].workflow.nodes if n.type == 'start' for f in n.config.get('inputs', [])]
                values = deepcopy(body.parameters)
                field = next((f for f in fields if f['name'] == body.input_name), None)
                if not field:
                    raise ValueError('请选择此数据对应的工作流输入')
                has_selection = any([selected['columns'], selected['filters'], selected['row_indices'] is not None])
                field_kind = field.get('resource_kind') or ('dataset' if field['name'] == 'dataset_id' else 'file')
                if has_selection and obj['kind'] == 'table':
                    if field_kind not in {'dataset', 'file'}:
                        raise ValueError('所选数据需要数据集或文件类型的工作流输入')
                    selection_view = await self.preview(pid, Preview(object_id=obj['id'], version=obj['version'], mapping=body.mapping, selection=body.selection, limit=1))
                    if not set(field.get('required_columns', [])) <= set(selection_view['selected_columns']):
                        raise ValueError('所选范围缺少工作流必需字段：' + '、'.join(field['required_columns']))
                    if field_kind == 'file' and field.get('accepted_formats') and 'csv' not in [v.lstrip('.') for v in field['accepted_formats']]:
                        raise ValueError('所选范围会生成 CSV 中间表；这个流程的输入未声明接受 CSV')
                    data = await self.dataset(pid, obj, body.mapping)
                    source_data = data['id']
                    values[field['name']] = ref('selection', 'output', 'dataset_id' if field_kind == 'dataset' else 'source_path')
                else:
                    values[field['name']] = await self.bound_value(pid, obj, field, body.mapping)
                    source_data = values[field['name']] if field_kind == 'dataset' else None
                for key, binding in body.bindings.items():
                    other = next((f for f in fields if f['name'] == key), None)
                    if not other:
                        raise ValueError('未知工作流输入：' + key)
                    if key == body.input_name:
                        raise ValueError('主要数据输入不能重复绑定')
                    other_obj = await self.checked(pid, binding)
                    values[key] = await self.bound_value(pid, other_obj, other)
                    input_objects.append({'role':other.get('label') or key, **{k:other_obj[k] for k in ('id','version','name')}})
                for field in fields:
                    if field['name'] not in values and field.get('default') is not None:
                        values[field['name']] = field['default']
                    # Default/parameter resource inputs need the same fixed-file
                    # binding as resources explicitly selected in the dialog.
                    resource_kind = field.get('resource_kind') or ('dataset' if field['name'] == 'dataset_id' else 'file' if field.get('type') == 'file' or field['name'] in {'source_path','file_path'} else '')
                    if field['name'] != body.input_name and field['name'] not in body.bindings and resource_kind in {'file','dataset'} and values.get(field['name']) not in (None, ''):
                        raw = values[field['name']]
                        if not isinstance(raw, str):
                            raise ValueError('输入“' + field['name'] + '”需要选择项目对象')
                        resource = await self.object(pid, resource_kind + ':' + raw)
                        resource = await self.checked(pid, ObjectRef(id=resource['id'], version=resource['version']))
                        values[field['name']] = await self.bound_value(pid, resource, field)
                        input_objects.append({'role':field.get('label') or field['name'], **{k:resource[k] for k in ('id','version','name')}})
                    if field.get('resource_kind') == 'model' and field['name'] not in body.bindings and isinstance(values.get(field['name']), dict):
                        binding = values[field['name']]
                        if 'candidate_id' in binding and 'slot' in binding:
                            default_model = await self.object(pid, f"model:{binding['candidate_id']}:{binding['slot']}")
                            input_objects.append({'role':field.get('label') or field['name'], **{k:default_model[k] for k in ('id','version','name')}})
                    if field.get('required', True) and values.get(field['name']) is None:
                        raise ValueError('请填写：' + (field.get('label') or field['name']))
                if source_data and has_selection:
                    steps.append(node('selection', 'data_analysis', '本次选取数据', dataset_id=source_data, analysis={'operation': 'select', **selected}))
                steps.append(node('step', 'tool', name, tool_name=body.tool, input=values))
            else:
                if not name:
                    raise ValueError('未知工具')
                await self.table_config(pid, obj, body.mapping)
                # Validate the full selection against the same parsed table shown in the UI.
                await self.preview(pid, Preview(object_id=obj['id'], version=obj['version'], mapping=body.mapping, selection=body.selection, limit=1))
                data = await self.dataset(pid, obj, body.mapping)
                source_data = data['id']
                dataset_value = source_data
                if body.tool != 'select' and (selected['filters'] or selected['row_indices'] is not None):
                    steps.append(node('selection', 'data_analysis', '本次选取数据', dataset_id=source_data,
                                      analysis={'operation': 'select', **selected, 'columns': []}))
                    dataset_value = ref('selection', 'output', 'dataset_id')
                parameters = deepcopy(body.parameters)
                if body.tool in {'select', 'profile', 'prepare', 'spearman', 'outliers'}:
                    plan = {**parameters, 'operation': body.tool, 'columns': selected['columns']}
                    if body.tool == 'select':
                        plan.update(selected)
                    plan = DataAnalysisPlan.model_validate(plan).model_dump()
                    steps.append(node('step', 'data_analysis', name, dataset_id=dataset_value, analysis=plan))
                elif body.tool == 'train':
                    from .modeling_models import Budget, CandidateRequest, Evaluation, FeaturePlan
                    evaluation = Evaluation.model_validate(parameters.get('evaluation', {}))
                    features = FeaturePlan.model_validate({**parameters.get('features', {}), 'columns': selected['columns']})
                    budget = Budget.model_validate(parameters.get('budget', {'seconds': 600, 'trials': 3, 'trial_seconds': 120}))
                    candidate = CandidateRequest.model_validate({'request_key': opid, 'engine': 'sklearn', 'models': ['linear', 'forest', 'hist_gradient'], 'batch_size': 3, **parameters.get('candidate', {})})
                    steps.append(node('step', 'model_train', name, dataset_id=dataset_value, features=features.model_dump(),
                                      evaluation=evaluation.model_dump(), budget=budget.model_dump(), candidate=candidate.model_dump(exclude={'request_key', 'features'})))
                    if evaluation.holdout_fraction:
                        steps.append(node('test', 'model_train', '固定模型的独立测试', finalize=True, study_id=ref('step', 'output', 'study_id')))
                elif body.tool == 'predict':
                    if not body.model:
                        raise ValueError('请选择已训练模型')
                    model = await self.checked(pid, body.model)
                    if model['kind'] != 'model':
                        raise ValueError('所选对象不是已训练模型')
                    input_objects.append({'role':'预测模型', **{k:model[k] for k in ('id','version','name')}})
                    steps.append(node('step', 'model_predict', name, dataset_id=dataset_value, **model['binding']))
            outputs = {'result': ref('step', 'output')}
            if any(n['id'] == 'selection' for n in steps):
                outputs['selection'] = ref('selection', 'output')
            if any(n['id'] == 'test' for n in steps):
                outputs['test'] = ref('test', 'output')
            steps.append(node('end', 'end', '本次结果', outputs=outputs))
            spec = WorkflowSpec.model_validate(graph(steps))
            if not record['found']:
                member = await self.projects.add_member(pid, name, '数据工作空间中的一次处理', purpose='workspace')
                draft = await self.services.workflow_store.get_draft(member['id'])
                await save_workflow(self.services, pid, member['id'], SaveWorkflow(expected_revision=draft['revision'], workflow=spec))
                value = {'name': name, 'fingerprint': fingerprint, 'request': body.model_dump(), 'source': body.source.model_dump(),
                         'workflow_id': member['id'], 'dataset_id': source_data, 'inputs':input_objects, 'graph': spec.model_dump(), 'task_id': ''}
                record = await self.projects.store.put_record(pid, 'workspace_operations', opid, value, 0)
            value = record['value']
            def validate(frozen):
                if target_workflow and frozen[target_workflow]['revision'] != body.workflow_revision:
                    raise ProjectConflict('工作流已修改，请重新打开配置窗口')
            task = await self.projects.start(pid, request_key='workspace/' + opid, workflow_id=value['workflow_id'],
                                             inputs={}, validate_snapshots=validate)
            await self.projects.store.put_record(pid, 'workspace_operations', opid, {**value, 'task_id': task['id']}, record['revision'])
            if obj.get('path') and obj['kind'] == 'table' and body.mapping:
                setting = await self.projects.store.get_record(pid, 'workspace_views', digest(obj['id']))
                await self.projects.store.put_record(pid, 'workspace_views', digest(obj['id']),
                    {'version': obj['version'], 'mapping': body.mapping.model_dump()}, setting['revision'])
            return await self.operation(pid, opid)

    async def save_chain(self, pid, opid, body):
        key = digest(body.request_key)[:32]
        async with self.locks.setdefault(pid, asyncio.Lock()):
            previous = await self.projects.store.get_record(pid, 'workspace_chains', key)
            if previous['found']:
                if previous['value']['request'] != [opid, body.name]:
                    raise ProjectConflict('此保存请求已经用于另一条处理链')
                return previous['value']
            record = await self.projects.store.get_record(pid, 'workspace_operations', opid)
            if not record['found']:
                raise KeyError('处理记录不存在')
            operations = {r['key']: r['value'] for r in await self.records(pid)}
            # Follow dataset-producing operations, never the lineage of a selected model.
            producers = {}
            for oid, op in operations.items():
                if op['request']['tool'] not in {'select', 'prepare', 'outliers'}:
                    continue
                detail = await self.operation(pid, oid)
                for item in detail['results']:
                    if item.get('dataset_id') and item['dataset_id'] != op.get('dataset_id'):
                        producers[item['id']] = oid
            chain, seen = [], set()
            current = opid
            while current and current not in seen:
                seen.add(current)
                chain.insert(0, operations[current])
                current = producers.get(operations[current]['source']['id'])
            if any(op['request']['selection']['row_indices'] is not None for op in chain):
                raise ValueError('手动选行绑定当前数据版本；保存可换表的流程前，请用字段筛选表达所需样本范围')
            root_kind = 'file' if chain[0]['source']['id'].startswith('file:') else 'dataset'
            root_name = 'dataset_id' if root_kind == 'dataset' else 'source_path'
            nodes = [node('start', 'start', '选择输入数据', inputs=[{'name': root_name, 'label': '输入数据', 'type': 'string', 'required': True, 'resource_kind': root_kind}])]
            incoming = ref('$inputs', root_name)
            if root_kind == 'file' and chain[0].get('dataset_id'):
                original_data = await self.modeling.get(pid, 'dataset', chain[0]['dataset_id'])
                mapping = deepcopy(original_data['mapping'])
                nodes[0]['config']['inputs'][0]['accepted_formats'] = ['csv','tsv','xlsx']
                required = set(mapping.get('field_rules', {}))
                for op in chain:
                    required.update(op['request']['selection']['columns'])
                    op_mapping = op['request'].get('mapping') or {}
                    required.update(op_mapping[k] for k in ('target','group_column','id_column') if op_mapping.get(k))
                nodes[0]['config']['inputs'][0]['required_columns'] = sorted(required)
                for field, purpose, label in [('sheet','worksheet','工作表'),('header_row','header_row','表头之前的行数'),('skip_after_header','skip_after_header','表头后跳过行数')]:
                    input_key = 'source_' + field
                    nodes[0]['config']['inputs'].append({'name':input_key,'label':label,'type':'any' if field=='sheet' else 'number',
                        'purpose':purpose,'default':mapping.get(field,0),'required':True})
                    mapping[field] = ref('$inputs', input_key)
                nodes.append(node('source', 'data_analysis', '读取新数据并沿用字段规则',
                                  source_path=incoming, mapping=mapping))
                incoming = ref('source', 'output', 'dataset_id')
            last = None
            for i, op in enumerate(chain):
                model_input = f'model_{i + 1}'
                if op['request'].get('model'):
                    model = await self.checked(pid, ObjectRef.model_validate(op['request']['model']))
                    nodes[0]['config']['inputs'].append({'name': model_input, 'label': '预测模型', 'type': 'object',
                        'resource_kind': 'model', 'required': True, 'default': model['binding']})
                prefix = f'op{i}_'
                def replace(value):
                    if isinstance(value, dict):
                        if '$ref' in value:
                            r = deepcopy(value)
                            r['$ref']['node_id'] = prefix + r['$ref']['node_id']
                            return r
                        return {k: incoming if k == 'dataset_id' and v == op.get('dataset_id') else replace(v) for k, v in value.items()}
                    if isinstance(value, list):
                        return [replace(v) for v in value]
                    return value
                for n in op['graph']['nodes']:
                    if n['type'] in {'start', 'end'}:
                        continue
                    nodes.append({**deepcopy(n), 'id': prefix + n['id'], 'config': replace(n['config'])})
                    if n['id'] == 'step' and n['type'] == 'tool' and not any(v['id'] == 'selection' for v in op['graph']['nodes']):
                        nodes[-1]['config']['input'][op['request']['input_name']] = incoming
                    if n['type'] == 'model_predict' and op['request'].get('model'):
                        for field in ('study_id', 'candidate_id', 'slot'):
                            nodes[-1]['config'][field] = ref('$inputs', model_input, field)
                last = prefix + 'step'
                incoming = ref(last, 'output', 'dataset_id')
            final_outputs = {'result': ref(last, 'output')}
            if any(n['id'] == prefix + 'test' for n in nodes):
                final_outputs['test'] = ref(prefix + 'test', 'output')
            nodes.append(node('end', 'end', '处理结果', outputs=final_outputs))
            from .project_space import AddWorkflow, add_workflow
            result = await add_workflow(self.services, pid, AddWorkflow(name=body.name, workflow=WorkflowSpec.model_validate(graph(nodes))))
            value = {'request': [opid, body.name], 'workflow_id': result['workflow_id'], 'editor_url': '/applications/' + result['workflow_id'] + '?tab=edit'}
            await self.projects.store.put_record(pid, 'workspace_chains', key, value, 0)
            return value


def register_data_workspace_routes(router, services, invoke):
    workspace = DataWorkspace(services)
    services.projects.data_workspace = workspace

    @router.get('/data-workspace')
    async def listing(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return await invoke(workspace.listing, project_id, offset, limit)

    @router.get('/data-workspace/catalog')
    async def catalog(project_id: str):
        return await invoke(workspace.catalog, project_id)

    @router.get('/data-workspace/object')
    async def detail(project_id: str, object_id: str):
        return await invoke(workspace.object, project_id, object_id)

    @router.post('/data-workspace/preview')
    async def preview(project_id: str, body: Preview):
        # FastAPI's default SQLAlchemy-safe encoder drops valid columns like _sample.
        return JSONResponse(await invoke(workspace.preview, project_id, body))

    @router.get('/data-workspace/file')
    async def file(project_id: str, object_id: str, version: str, download: bool = False):
        obj = await invoke(workspace.checked, project_id, ObjectRef(id=object_id, version=version))
        if not obj.get('path'):
            raise HTTPException(422, '此对象不是文件')
        path = workspace.modeling.source(project_id, obj['path'])
        return FileResponse(path, filename=path.name, content_disposition_type='attachment' if download else 'inline',
                            headers={'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'"})

    @router.post('/data-workspace/operations', status_code=202)
    async def run(project_id: str, body: Operation):
        return await invoke(workspace.run, project_id, body)

    @router.get('/data-workspace/operations/{operation_id}')
    async def operation(project_id: str, operation_id: str):
        return await invoke(workspace.operation, project_id, operation_id)

    @router.post('/data-workspace/operations/{operation_id}/save', status_code=201)
    async def save(project_id: str, operation_id: str, body: SaveChain):
        return await invoke(workspace.save_chain, project_id, operation_id, body)
