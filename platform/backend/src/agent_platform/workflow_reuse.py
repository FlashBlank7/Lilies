"""Conservative reuse of completed top-level steps in a new project run.

Unknown effects and missing checkpoints cause execution, never an assumed hit.
Code opts in only when all read dependencies and output files are declared.
"""
import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path


ML_TYPES = {'data_analysis', 'feature_extract', 'model_train'}


def eligible(node):
    return (node.type in {'start', 'template', 'end'} | ML_TYPES
            or node.type == 'code' and node.config.get('reuse_completed') is True)


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def digest(path, roots):
    path = Path(path)
    root = next((r for r in roots if path.is_relative_to(r) and path.resolve().is_relative_to(r.resolve())), None)
    if root is None or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root)):
        raise ValueError('复用文件不在当前项目内或使用了符号链接')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def workspace_files(value, workspace):
    paths = set()
    for item in walk(value):
        if not isinstance(item, str) or not item or len(item) > 1024 or '\n' in item:
            continue
        path = workspace / item
        # Existing directories are not a complete declaration of their contents.
        if path.is_dir():
            raise ValueError('目录依赖需要逐个声明文件')
        if path.is_file() or path.is_symlink():
            paths.add(path)
    return paths


async def checkpoint(runtime, state, node, output=None):
    """Capture before execution, then add outputs; hashes are never inferred later."""
    if not state.project_context or not eligible(node):
        return None
    if any(isinstance(v, dict) and v.get('node_id') == '$run' for v in walk(node.config)):
        return None
    try:
        service = runtime.projects.services.modeling
        pid = state.project_context['project_id']
        workspace = Path(state.workspace_path)
        roots = [workspace, service.root / pid]
        args = runtime._resolve(node.config, {'inputs': state.inputs, 'nodes': state.outputs})
        values = [args, state.inputs, output or {}]
        paths = await asyncio.to_thread(workspace_files, values, workspace)
        environment = ''
        if node.type == 'code':
            environment = await service.image(service.services.settings.sandbox_image)
        if node.type in ML_TYPES:
            environment = await service.image()
            # Scoped DB reads resolve IDs before any model storage path is used.
            ids = set()
            for value in walk(values):
                if isinstance(value, dict):
                    for kind in ('dataset', 'candidate', 'study'):
                        ident = value.get(kind + '_id')
                        if ident:
                            ids.add((kind, str(ident)))
                    if value.get('trials') and value.get('id'):
                        ids.add(('candidate', str(value['id'])))
            seen = set()
            while ids - seen:
                kind, ident = next(iter(ids - seen)); seen.add((kind, ident))
                doc = await service.get(pid, kind, ident)
                folder = service.path(pid, ident)
                if kind == 'dataset':
                    paths.update(folder / f['name'] for f in doc['files'].values())
                elif kind == 'candidate':
                    ids.add(('study', doc['study_id']))
                    for trial in doc['trials']:
                        if trial['status'] == 'completed':
                            model = folder / 'output' / f'trial-{trial["slot"]}'
                            files = [p for p in model.rglob('*') if p.is_file() and not p.name.startswith('training-note')]
                            if not files:
                                return None
                            paths.update(files)
                    if doc.get('code_sha256'):
                        paths.add(folder / 'transformer.py')
                else:
                    ids.add(('dataset', doc['dataset_id']))
                    paths.add(folder / 'output/split.json')
        files = await asyncio.to_thread(lambda: {str(p): digest(p, roots) for p in paths})
        return {'version': 1, 'files': files, 'environment': environment}
    except (OSError, ValueError, KeyError, RuntimeError):
        # Reuse is optional; an unavailable fingerprint must not break a valid run.
        return None


def definition(node):
    return node.model_dump(mode='json', exclude={'position', 'title', 'description'})


def dependencies(workflow, node):
    refs = {str(v['node_id']) for v in walk(node.config) if isinstance(v, dict) and 'node_id' in v}
    return {e.source for e in workflow.edges if e.target == node.id} | {r for r in refs if not r.startswith('$')}


def incoming(workflow, node):
    return sorted((e.source, e.source_port, e.target_port, e.branch or '') for e in workflow.edges if e.target == node.id)


async def seed(runtime, state, source_run_id):
    source = await runtime.workflow_store.get_run(source_run_id)
    old = source['state']
    if (source['status'] not in {'succeeded', 'failed', 'cancelled'}
            or not old.project_context or not state.project_context
            or old.project_context['project_id'] != state.project_context['project_id']
            or old.application_id != state.application_id):
        raise ValueError('只能复用当前项目同一工作流已结束的运行')
    state.reuse_source_run_id = source_run_id
    if (state.inputs != old.inputs or state.workspace_path != old.workspace_path
            or state.snapshot.model_dump(exclude={'workflow'}) != old.snapshot.model_dump(exclude={'workflow'})
            or any(state.project_context.get(k) != old.project_context.get(k)
                   for k in ('model_resources', 'knowledge_resources'))):
        return
    previous = {n.id: n for n in old.snapshot.workflow.nodes}
    pending = list(state.snapshot.workflow.nodes)
    while pending:
        progressed = False
        for node in pending[:]:
            deps = dependencies(state.snapshot.workflow, node)
            if not deps.issubset(set(state.reused_nodes)):
                continue
            pending.remove(node); progressed = True
            saved = old.reuse_checkpoints.get(node.id)
            output = old.outputs.get(node.id)
            if (not saved or saved.get('version') != 1 or not eligible(node)
                    or node.id not in old.completed or output is None
                    or any(output.get(k) for k in ('error', 'degraded', 'fallback_used'))
                    or node.id not in previous or definition(node) != definition(previous[node.id])
                    or incoming(state.snapshot.workflow, node) != incoming(old.snapshot.workflow, previous[node.id])):
                continue
            current = await checkpoint(runtime, state, node, output)
            if current != saved:
                continue
            state.outputs[node.id] = deepcopy(output)
            state.completed.append(node.id)
            state.reused_nodes.append(node.id)
            state.reuse_checkpoints[node.id] = deepcopy(saved)
        if not progressed:
            break
