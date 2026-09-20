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
    return result


def task_summary(task: dict) -> dict:
    task = jsonable_encoder(task)
    result = {k: task[k] for k in ('id', 'project_id', 'workflow_id', 'status', 'error', 'purpose',
              'item_id', 'feedback_task_id', 'created_at', 'updated_at', 'message') if k in task}
    outputs = task.get('outputs', {})
    result['outputs_truncated'] = payload_measurement(outputs)['bytes'] > 8000
    result['outputs'] = preview(outputs) if result['outputs_truncated'] else outputs
    inputs = task.get('inputs', {})
    result['inputs_truncated'] = payload_measurement(inputs)['bytes'] > 8000
    result['inputs'] = preview(inputs) if result['inputs_truncated'] else inputs
    result['presentation'] = {k: task.get('presentation', {}).get(k) for k in ('message', 'artifacts')}
    if 'runs' in task:
        result['runs'] = [{k: r[k] for k in ('id', 'application_id', 'status', 'error', 'parent_run_id', 'draft_revision', 'waiting_node') if k in r}
                          for r in task['runs']]
    result['view'] = 'summary'
    result['detail'] = 'workflow_run(action="inspect", task_id="' + task.get('id', '') + '", view="full") returns all inputs, outputs and member runs.'
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
        if member['id'] in related or (not item and member.get('purpose') != 'test'):
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
                       'Keep existing customer answers and other items. Complete the requested change, run affected checks, '
                       'fix actual errors, then present a new trial linked to the feedback. Full data remains available via tools.'}
    task_id = state.get('project_task_id') or link.get('task_id')
    if task_id:
        context['business_task'] = task_summary(await services.projects.task(project_id, task_id))
    else:
        tasks = await services.projects.store.tasks(project_id, item_id=item_id, purpose='customer_trial', limit=3)
        context['recent_results'] = [task_summary(t) for t in tasks]
    if getattr(services, 'modeling', None):
        studies = await services.modeling.list(project_id, 'study', limit=5)
        context['modeling'] = [{k: s.get(k) for k in ('id', 'dataset_id', 'name', 'status', 'best', 'baseline', 'trials_used', 'budget', 'next_action', 'error', 'repair_candidate_id', 'failure_streak', 'search_strategy')} for s in studies]
        if link.get('dataset_id') and not studies:
            data = await services.modeling.get(project_id, 'dataset', link['dataset_id'])
            context['dataset'] = {k: data.get(k) for k in ('id', 'name', 'mapping', 'status')}
    return context
