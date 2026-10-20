"""Read a failed run's relevant nodes without sending its entire frozen graph."""
import json
import re

from .project_agent_context import result_preview
from .project_draft_context import focus_nodes, indexed_nodes


async def diagnose_task(services, project_id, args):
    task = await services.projects.store.get_task(project_id, args.task_id, snapshots=True)
    runs = await services.projects.store.runs(task['id'])
    if args.run_id:
        run = next((r for r in runs if r['id'] == args.run_id), None)
        if not run:
            raise ValueError('run_id 不属于当前项目任务')
    else:
        run = next((r for r in reversed(runs) if r['status'] == 'failed'), runs[-1] if runs else None)
    workflow_id = run['application_id'] if run else task['workflow_id']
    if args.workflow_id and args.workflow_id != workflow_id:
        raise ValueError('所选 workflow_id 与诊断运行不一致；可用 run_id 选择该任务的具体运行')
    fixed = task['snapshots'].get(workflow_id)
    if not fixed:
        raise ValueError('此任务未保存工作流快照；请读取完整任务详情')
    state = json.loads(run['state_json']) if run else None
    graph = (state['snapshot'] if state else fixed['snapshot'])['workflow']
    errors = []
    if run:
        events = await services.storage.list_events(run['id'], limit=100, tail=True)
        errors = [{'node_id': e.data.get('node_id', ''), 'error': e.data.get('error', '')}
                  for e in events if e.type == 'node.failed'][-10:]
    selected = [args.node_id] if args.node_id else []
    if not selected:
        available = {n['id'] for n, _, _ in indexed_nodes(graph)}
        # Loop traces use container[index].node. Only resolve a unique saved id;
        # otherwise preserve the scoped error and let the caller select details.
        # A nested failure is also emitted for its enclosing loop. Prefer the
        # deepest actual failure, instead of loading the whole loop config.
        for failure in sorted(reversed(errors), key=lambda e: e['node_id'].count('].'), reverse=True):
            ident = re.sub(r'^.*\[\d+\]\.', '', failure['node_id'])
            if ident in available:
                try:
                    focus_nodes(graph, [ident])
                except ValueError:
                    continue
                selected = [ident]
                break
    stage = 'node_execution' if errors else 'unknown'
    if not run and (task.get('error') or '').startswith('成员输入'):
        stage = 'input_validation'
        if not selected:
            selected = [n['id'] for n in graph['nodes'] if n['type'] == 'start']
    focused = focus_nodes(graph, selected)
    upstream = {e['source'] for connection in focused['connections'] for e in connection['edges']
                if e['target'] == connection['node_id']}
    context = state or {'inputs': task['inputs']}
    input_scope, execution_scope = 'run', ''
    if focused['connections'] and focused['connections'][0]['scope']:
        # Never look up an inner node's predecessors in the outer graph: ids
        # can be reused. Select the persisted iteration that actually failed.
        scope = focused['connections'][0]['scope']
        pattern = ''.join(re.escape(container) + r'\[\d+\]\.' for container in scope)
        candidates = {prefix: progress for prefix, progress in (state or {}).get('nested_progress', {}).items()
                      if re.fullmatch(pattern, prefix)}
        node_id = selected[0]
        failure_prefixes = [e['node_id'][:-len(node_id)] for e in reversed(errors)
                            if e['node_id'].endswith('.' + node_id)]
        execution_scope = next((p for p in failure_prefixes if p in candidates),
                               next(iter(candidates)) if len(candidates) == 1 else '')
        context = candidates.get(execution_scope, {})
        input_scope = 'nested_graph' if context else 'unavailable'
    result = {'id': task['id'], 'status': task['status'], 'error': task.get('error', ''),
              'view': 'diagnostic', 'workflow_id': workflow_id, 'run_id': run['id'] if run else None,
              'revision': fixed['revision'], 'content_hash': fixed['content_hash'],
              'snapshot_source': 'original_run', 'failure_stage': stage, 'node_errors': errors,
              'inputs': result_preview(context.get('inputs')), 'inputs_scope': input_scope,
              'execution_scope': execution_scope, **focused,
              'upstream_outputs': result_preview({key: value for key, value in context.get('outputs', {}).items()
                                                  if key in upstream}),
              'detail': 'Full original inputs/outputs: workflow_run(inspect,task_id,view="full"). '
                        'Select node_id/run_id here for other details; workflow_draft reads current editable config.'}
    try:
        current = await services.workflow_store.get_draft(workflow_id)
        result['current_draft'] = {k: current[k] for k in ('revision', 'content_hash')}
        result['current_draft']['changed_since_run'] = current['content_hash'] != fixed['content_hash']
        if not result['current_draft']['changed_since_run']:
            result['edit_base'] = {'workflow_id': workflow_id, 'expected_revision': current['revision'],
                                   'expected_content_hash': current['content_hash']}
            result['detail'] = ('These node configs also match the current draft; edit_base can be used for '
                'copy/node_updates or a draft edit without rereading the full graph. Copy preserves untouched nodes/layout. '
                'Original full details remain available via workflow_run(inspect,task_id,view="full").')
    except KeyError:
        result['current_draft'] = None
    return result
