"""Selected graph details and bounded, actual edit results for project tools."""
import logging
import platform

from .project_agent_context import result_preview
from .project_metrics import payload_measurement
from .workflow_models import ApplicationSnapshot, EdgeSpec


def structure_check(applications, snapshot, *, revision=0):
    """Return advisory diagnostics for this saved version, never a save gate."""
    try:
        report = applications.validate_structure(snapshot)
    except Exception:
        logging.getLogger(__name__).exception('Saved draft structure check could not finish')
        report = {'valid': None, 'errors': ['结构检查未能完成；草稿仍已保存，可稍后单独 validate。'], 'warnings': []}
    check = {'validation_scope': 'structure', 'revision': revision, 'content_hash': snapshot.content_hash(),
             'valid': report['valid'], 'runtime_checked': False,
             'python_version': platform.python_version(),
             'detail': 'Python 仅按平台版本静态检查语法，不执行代码，不验证依赖、业务或运行环境兼容性。'
                       '请根据 diagnostics 原地修正代码；无需重新读取未改动节点或运行自测。'}
    for key in ('errors', 'warnings'):
        messages = report[key]
        check[key] = [message if len(message) <= 500 else message[:500] + '…' for message in messages[:5]]
        if len(messages) > 5 or any(len(message) > 500 for message in messages[:5]):
            check[key + '_truncated'] = True
            check['detail'] += ' workflow_run(action="validate", workflow_id=id) reads the complete current check.'
    diagnostics = report.get('diagnostics', [])
    check['diagnostics'] = diagnostics[:5]
    if len(diagnostics) > 5:
        check['diagnostics_truncated'] = True
    return check


def indexed_nodes(graph, scope=()):
    for node in graph.get('nodes', []):
        yield node, graph, scope
        if node['type'] in {'loop', 'iteration'} and isinstance(node.get('config', {}).get('workflow'), dict):
            yield from indexed_nodes(node['config']['workflow'], (*scope, node['id']))


def focus_nodes(graph, node_ids):
    selected, connections = [], []
    all_nodes = list(indexed_nodes(graph))
    for node_id in dict.fromkeys(node_ids):
        matches = [item for item in all_nodes if item[0]['id'] == node_id]
        if not matches:
            raise ValueError(f'节点不存在：{node_id}；请读取当前草稿摘要或完整图')
        if len(matches) != 1:
            raise ValueError(f'节点 {node_id} 在多个子图中重名；请读取完整图区分后修改')
        node, parent, scope = matches[0]
        edges = [e for e in parent.get('edges', []) if node_id in (e['source'], e['target'])]
        neighbor_ids = {e[k] for e in edges for k in ('source', 'target')} - {node_id}
        selected.append(node)
        connections.append({'node_id': node_id, 'scope': list(scope), 'edges': edges,
            'neighbors': [{'id': n['id'], 'type': n['type'], 'title': n.get('title') or n['id']}
                          for n in parent['nodes'] if n['id'] in neighbor_ids]})
    return {'nodes': selected, 'connections': connections}


def node_changes(before: ApplicationSnapshot, after: ApplicationSnapshot):
    """Describe actual saved fields; large values stay available in node reads."""
    def flatten(snapshot):
        return {(*scope, n['id']): n for n, _, scope in indexed_nodes(snapshot.workflow.model_dump(mode='json'))}
    def edges(snapshot):
        graph = snapshot.workflow.model_dump(mode='json')
        by_scope = {(): graph['edges'], **{(*scope, n['id']): n['config']['workflow'].get('edges', [])
            for n, _, scope in indexed_nodes(graph)
            if n['type'] in {'loop', 'iteration'} and isinstance(n.get('config', {}).get('workflow'), dict)}}
        return {scope: [EdgeSpec.model_validate(edge) for edge in values] for scope, values in by_scope.items()}
    old, new = flatten(before), flatten(after)
    changes = []
    for path in sorted(old.keys() | new.keys()):
        if old.get(path) == new.get(path):
            continue
        previous, current = old.get(path, {}), new.get(path, {})
        fields = {}
        for key in previous.keys() | current.keys():
            if previous.get(key) != current.get(key) or (key in previous) != (key in current):
                value = current.get(key)
                if key == 'config' and key in current:
                    value = {k: v for k, v in current[key].items()
                             if k not in previous.get(key, {}) or v != previous[key][k]}
                    removed = sorted(previous.get(key, {}).keys() - current[key].keys())
                    if removed:
                        fields['removed_config_keys'] = removed
                fields[key] = result_preview(value)
        changes.append({'node_id': path[-1], 'scope': list(path[:-1]),
                        'change': 'added' if not previous else 'removed' if not current else 'updated',
                        'fields': fields})
    count = len(changes)
    changes = changes[:20]
    size = 0
    for change in changes:
        size += payload_measurement(change)['bytes']
        if size > 6000:
            change['field_names'] = list(change.pop('fields'))
            change['fields_omitted'] = True
    return {'nodes': changes, 'node_count': count, 'nodes_truncated': count > len(changes),
            'edges_changed': edges(before) != edges(after),
            'tests_changed': before.tests != after.tests,
            'detail': 'workflow_draft(view="nodes",node_ids=[...]) reads exact fields and connections.'}
