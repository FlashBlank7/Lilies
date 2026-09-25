"""Bounded, explicit previews for the project agent; authoritative data stays intact."""
from __future__ import annotations

from fastapi.encoders import jsonable_encoder

from .project_metrics import payload_measurement


def preview(value, *, depth=0):
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:1000] + '… [truncated]'
    if isinstance(value, (dict, list)) and depth >= 4:
        return {'preview_omitted': True, 'count': len(value)}
    if isinstance(value, list):
        result = [preview(v, depth=depth + 1) for v in value[:10]]
        if len(value) > 10:
            result.append({'omitted_items': len(value) - 10})
        return result
    if isinstance(value, dict):
        result = {k: preview(v, depth=depth + 1) for k, v in list(value.items())[:20]}
        if len(value) > 20:
            result['omitted_fields'] = list(value)[20:]
        return result
    return value


def result_preview(value):
    """Keep small results exact; describe bulky branches instead of copying rows.

    A depth-only preview still repeated thousands of bytes of distributions,
    feature rows and training configuration on every conversation turn. Keep
    compact values (including metrics and artifact paths) where possible and
    leave the original branch available through workflow_run.output_path.
    """
    size = payload_measurement(value)['bytes']
    if size <= 1000:
        return value

    # A native training candidate contains large fold indices and run metadata.
    # Its existing modeling summary retains metrics/baselines for each trial;
    # treating the entire trials list as a table hides the comparison itself.
    if (isinstance(value, dict) and isinstance(value.get('id'), str) and isinstance(value.get('study_id'), str)
            and isinstance(value.get('engine'), str) and value['engine'] in {'sklearn', 'autogluon'}
            and isinstance(value.get('trials'), list) and all(isinstance(t, dict) for t in value['trials'])):
        from .modeling_summary import candidate_summary
        try:
            candidate = candidate_summary(value)
        except (TypeError, KeyError):
            candidate = None  # Other workflows may use these same field names.
        if candidate is not None and payload_measurement(candidate)['bytes'] <= 8000:
            return candidate

    def omitted(item, size):
        description = {'preview_omitted': True, 'bytes': size}
        if isinstance(item, dict):
            description.update(type='object', fields=list(item)[:20], field_count=len(item))
        elif isinstance(item, list):
            description.update(type='array', count=len(item))
        elif isinstance(item, str):
            description.update(type='string', characters=len(item))
        else:
            description.update(type=type(item).__name__)
        return description

    if isinstance(value, dict):
        children = {}
        for key, child in value.items():
            child_size = payload_measurement(child)['bytes']
            children[key] = child if child_size <= 1000 else omitted(child, child_size)
        if payload_measurement(children)['bytes'] > 2000:
            # Collapse the largest remaining values first, so a sample table
            # does not force us to discard small metrics or download paths.
            sizes = {key: payload_measurement(child)['bytes'] for key, child in children.items()}
            for key in sorted(sizes, key=sizes.get, reverse=True):
                description = omitted(value[key], payload_measurement(value[key])['bytes'])
                if payload_measurement(description)['bytes'] < sizes[key]:
                    children[key] = description
                if payload_measurement(children)['bytes'] <= 2000:
                    break
        if payload_measurement(children)['bytes'] <= 2000:
            return children
    return omitted(value, size)


def draft_summary(draft: dict, *, nodes: bool = True) -> dict:
    draft = jsonable_encoder(draft)
    snapshot = draft['snapshot']
    graph = snapshot['workflow']
    result = {k: draft[k] for k in ('revision', 'content_hash', 'tested_hash') if k in draft}
    result.update(view='summary', name=snapshot['name'], description=snapshot.get('description', '')[:1000],
                  node_count=len(graph['nodes']), edge_count=len(graph['edges']), test_count=len(snapshot['tests']),
                  detail='workflow_draft(view="nodes", node_ids=[...]) for config; view="tests" for saved tests; view="full" for the complete draft.')
    result['member_calls'] = [n['config']['tool_name'][9:] for n in graph['nodes']
                              if isinstance(n.get('config', {}).get('tool_name'), str)
                              and n['config']['tool_name'].startswith('workflow:')]
    if nodes:
        result['nodes'] = [{k: n[k] for k in ('id', 'type', 'title')} for n in graph['nodes']]
        result['edges'] = graph['edges']
        result['tests'] = [{k: t.get(k) for k in ('id', 'name', 'requirement')} for t in snapshot['tests']]
    else:
        interface = {
            'inputs': [n['config'].get('inputs', []) for n in graph['nodes'] if n['type'] == 'start'],
            'outputs': [list(n['config'].get('outputs', {})) for n in graph['nodes'] if n['type'] == 'end'],
        }
        result['interface_truncated'] = payload_measurement(interface)['bytes'] > 4000
        result.update(preview(interface) if result['interface_truncated'] else interface)
        result['detail'] = ('Use workflow_run(action="start", workflow_id=id, inputs={...}) to run with these inputs. '
                            'project_workflows(action="inspect", workflow_id=id) reads the full interface; '
                            'workflow_draft reads implementation only when editing or diagnosing.')
    return result


def task_summary(task: dict) -> dict:
    task = jsonable_encoder(task)
    result = {k: task[k] for k in ('id', 'project_id', 'workflow_id', 'status', 'error', 'purpose',
              'item_id', 'feedback_task_id', 'created_at', 'updated_at', 'message') if k in task}
    outputs = task.get('outputs', {})
    result['outputs_truncated'] = payload_measurement(outputs)['bytes'] > 8000
    result['outputs'] = ({key: result_preview(value) for key, value in outputs.items()}
                         if result['outputs_truncated'] else outputs)
    inputs = task.get('inputs', {})
    result['inputs_truncated'] = payload_measurement(inputs)['bytes'] > 8000
    result['inputs'] = preview(inputs) if result['inputs_truncated'] else inputs
    result['presentation'] = {k: task.get('presentation', {}).get(k) for k in ('message', 'artifacts')}
    if 'runs' in task:
        result['runs'] = [{k: r[k] for k in ('id', 'application_id', 'status', 'error', 'parent_run_id', 'draft_revision', 'waiting_node', 'waiting_input') if k in r}
                          for r in task['runs']]
    result['view'] = 'summary'
    result['detail'] = ('Read summary first. workflow_run(action="inspect", task_id="' + task.get('id', '') +
                        '", output_path=["output_key"]) reads an exact output branch without traces; '
                        'view="full" includes all inputs, outputs and member runs for diagnosis.')
    return result


def progress_summary(progress: dict, item_id: str = '') -> dict:
    value = progress['value']
    result = {'view': 'summary', 'revision': progress['revision'],
              'goal': value.get('goal', ''), 'summary': value.get('summary', ''),
              'items': [{k: i.get(k) for k in ('id', 'title', 'goal', 'status', 'availability', 'summary',
                         'next_action', 'blocker', 'deliverable', 'completion_criteria')} for i in value['items']],
              'detail': 'project_progress(item_id="...") reads one full item; view="full" reads the complete record. Use action="patch" with expected_revision and item_id to update only that item.'}
    if item_id:
        item = next((i for i in value['items'] if i['id'] == item_id), None)
        if item:
            result['current_item'] = {k: item.get(k) for k in ('id', 'title', 'goal', 'status', 'availability',
                'summary', 'next_action', 'blocker', 'deliverable', 'completion_criteria', 'delivery_request_id', 'workflow_ids')}
            result['current_item']['questions'] = [{k: q.get(k) for k in ('id', 'text', 'impact', 'answer', 'next_action')}
                                                    for q in item.get('questions', []) if not q.get('answer')][-8:]
            result['current_item']['results'] = item.get('results', [])[:3]
    return result


async def conversation_context(services, project_id: str, state: dict, discussion: dict, message: str) -> dict:
    """Refresh revisions each turn; never resend full graphs or old task traces by default."""
    project = await services.projects.store.get(project_id)
    progress = await services.projects.store.progress(project_id)
    link = state.get('conversation_context', {})
    item_id = state.get('active_item_id') or link.get('item_id', '')
    item = next((i for i in progress['value']['items'] if i['id'] == item_id), {})
    related = set(item.get('workflow_ids', [])) | {project_id}
    workflows = []
    for member in project['members']:
        if member.get('purpose') != 'test' or member['id'] in related:
            draft = await services.workflow_store.get_draft(member['id'])
            workflows.append({'id': member['id'], 'purpose': member.get('purpose'), **draft_summary(draft, nodes=False)})
    context = {'phase': state['phase'], 'user_message': message,
        'project': {'id': project_id, 'name': project['name'],
                    'agent_modules_enabled': project['agent_modules_enabled']},
        'requirements': {'status': discussion['status'], 'revision': discussion['revision'],
            'document_available': bool(discussion['document']), 'read_with': 'requirements_submit(action="read")'},
        'progress': progress_summary(progress, item_id), 'workflows': workflows,
        'conversation_context': link, 'continue_work': state.get('continue_work', False),
        'instruction': 'Use the current item and revision summaries. Read relevant node/file details only when needed. '
                       'Keep existing customer answers and human edits. Solve the requested task using project tools. '
                       'Workflow generation only saves a draft; execute it when requested. Full data remains available via tools.'}
    from .local_agent_tools import ProjectTools, ProjectFile
    import asyncio
    files = await asyncio.to_thread(ProjectTools(services, project_id, services.local_agents).file, ProjectFile(action='list'))
    context['project_files'] = {'files': files['files'][:40],
        'truncated': files['truncated'] or len(files['files']) > 40,
        'detail': 'project_file(action="list") lists project files; read/profile loads only the selected material.'}
    task_id = state.get('project_task_id') or link.get('task_id')
    if task_id:
        context['business_task'] = task_summary(await services.projects.task(project_id, task_id))
    else:
        tasks = await services.projects.store.tasks(project_id, item_id=item_id, limit=3)
        from .conversation_scope import conversation_for
        current_conversation = conversation_for(project_id)
        context['recent_results'] = [task_summary(t) for t in tasks
            if t.get('mode') != 'agent' or t.get('conversation_id', '') == current_conversation]
    if getattr(services, 'modeling', None):
        studies = await services.modeling.list(project_id, 'study', limit=5)
        context['modeling'] = [{k: s.get(k) for k in ('id', 'dataset_id', 'name', 'status', 'best', 'baseline', 'trials_used', 'budget', 'next_action', 'error', 'repair_candidate_id', 'failure_streak', 'search_strategy')} for s in studies]
        if link.get('dataset_id') and not studies:
            data = await services.modeling.get(project_id, 'dataset', link['dataset_id'])
            context['dataset'] = {k: data.get(k) for k in ('id', 'name', 'mapping', 'status')}
    return context
