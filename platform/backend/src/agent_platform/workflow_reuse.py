"""Conservative reuse of completed top-level steps in a new project run.

Unknown effects and missing checkpoints cause execution, never an assumed hit.
Code opts in only when all read dependencies and output files are declared.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .workflow_models import WorkflowSpec


ML_TYPES = {'data_analysis', 'feature_extract', 'model_train'}


def eligible(node):
    if node.type == 'iteration':
        return (node.config.get('reuse_completed') is True
                and not any(isinstance(v,dict) and v.get('node_id') in ('$run','$secret') for v in walk(node.config))
                and all(eligible(child) for child in WorkflowSpec.model_validate(node.config['workflow']).nodes))
    return (node.type in {'start', 'template_transform', 'end'} | ML_TYPES
            or node.type == 'model_predict' and node.config.get('study_id') and node.config.get('candidate_id') and not node.config.get('model_ref')
            or node.type == 'code' and node.config.get('reuse_completed') is True)


def direct_config(node):
    # References inside a nested graph resolve in that graph, not the parent.
    return {k:v for k,v in node.config.items() if k != 'workflow'} if node.type == 'iteration' else node.config


def execution_types(node):
    kinds={node.type}
    if node.type == 'iteration':
        for child in WorkflowSpec.model_validate(node.config['workflow']).nodes:kinds.update(execution_types(child))
    return kinds


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
        args = runtime._resolve(direct_config(node), {'inputs': state.inputs, 'nodes': state.outputs})
        if node.type == 'iteration':args={**args, 'inherited_inputs': state.inputs}
        # Start reads its declared inputs directly; all other eligible nodes
        # receive only resolved config. Unrelated project inputs are not reads.
        if node.type == 'start':
            args = {**args, 'values': {f['name']: state.inputs.get(f['name'], f.get('default'))
                                    for f in node.config.get('inputs', [])}}
        argument_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False,
                                                  allow_nan=False).encode()).hexdigest()
        values = [args, output or {}]
        file_values=values+[node.config['workflow']] if node.type=='iteration' else values
        paths = await asyncio.to_thread(workspace_files, file_values, workspace)
        kinds=execution_types(node);environments={}
        if 'code' in kinds:
            environments['code'] = await service.image(service.services.settings.sandbox_image)
        if kinds & (ML_TYPES | {'model_predict'}):
            environments['model'] = await service.image()
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
        # Keep existing single-step fingerprints compatible with saved runs.
        environment=(json.dumps(environments,sort_keys=True) if node.type=='iteration'
                     else next(iter(environments.values()),''))
        return {'version': 2, 'arguments': argument_hash, 'files': files, 'environment': environment}
    except (OSError, ValueError, KeyError, RuntimeError, TypeError):
        # Reuse is optional; an unavailable fingerprint must not break a valid run.
        return None


def definition(node):
    return node.model_dump(mode='json', exclude={'position', 'title', 'description'})


def dependencies(workflow, node):
    refs = {str(v['node_id']) for v in walk(direct_config(node)) if isinstance(v, dict) and 'node_id' in v}
    return {e.source for e in workflow.edges if e.target == node.id} | {r for r in refs if not r.startswith('$')}


def incoming(workflow, node):
    return sorted((e.source, e.source_port, e.target_port, e.branch or '') for e in workflow.edges if e.target == node.id)


async def source_state(runtime, state, source_run_id):
    source = await runtime.workflow_store.get_run(source_run_id)
    old = source['state']
    if (source['status'] not in {'succeeded', 'failed', 'cancelled'}
            or not old.project_context or not state.project_context
            or old.project_context['project_id'] != state.project_context['project_id']
            or old.application_id != state.application_id):
        raise ValueError('只能复用当前项目同一工作流已结束的运行')
    return old


async def seed(runtime, state, source_run_id):
    # Resolve eligibility when each step is reached: a changed start/parent
    # can still produce unchanged values for an independent downstream branch.
    await source_state(runtime, state, source_run_id)
    state.reuse_source_run_id = source_run_id


def blocked_nodes(workflow):
    blocked = {n.id for n in workflow.nodes if not eligible(n)
               or any(isinstance(v, dict) and v.get('node_id') == '$run' for v in walk(n.config))}
    while True:
        more = {n.id for n in workflow.nodes if dependencies(workflow, n) & blocked}
        if more <= blocked:
            return blocked
        blocked.update(more)


async def reuse_step(runtime, state, node, old):
    if (state.workspace_path != old.workspace_path
            or state.snapshot.model_dump(exclude={'workflow'}) != old.snapshot.model_dump(exclude={'workflow'})):
        return False
    previous = next((n for n in old.snapshot.workflow.nodes if n.id == node.id), None)
    saved = old.reuse_checkpoints.get(node.id)
    output = old.outputs.get(node.id)
    if (not saved or saved.get('version') != 2 or not eligible(node)
            or node.id not in old.completed or output is None
            or any(output.get(k) for k in ('error', 'degraded', 'fallback_used'))
            or previous is None or definition(node) != definition(previous)
            or incoming(state.snapshot.workflow, node) != incoming(old.snapshot.workflow, previous)):
        return False
    current = await checkpoint(runtime, state, node, output)
    if current != saved:
        return False
    state.outputs[node.id] = deepcopy(output)
    state.completed.append(node.id)
    state.reused_nodes.append(node.id)
    state.reuse_checkpoints[node.id] = deepcopy(saved)
    return True
