"""Reusable fixture workflow; only HTTP blocks access the synthetic services."""
from pathlib import Path

from tests.test_projects import node, edge, ref
from tests.fixtures.media_http_backend import SERVICES

HERE = Path(__file__).parent


def workflow(connection_id, *, omit_log=False):
    def http(ident, method, path, **kwargs):
        return node(ident, 'http_request', connection_id=connection_id, method=method, path=path, **kwargs)
    fetch_nodes = [node('begin', 'start'), http('request', 'GET', ref('$inputs', 'item', 'url'), query={'page': ref('$inputs', 'item', 'page')}),
        node('done', 'end', outputs={'service': ref('$inputs', 'item', 'service'), 'url': ref('$inputs', 'item', 'url'), 'payload': ref('request', 'output'), 'headers': ref('request', 'headers')})]
    fetch = {'nodes': fetch_nodes, 'edges': [edge('begin', 'request'), edge('request', 'done')]}
    actions = [node('begin', 'start'),
        http('delete', 'POST', ref('$inputs', 'item', 'delete_path'), body={'path': ref('$inputs', 'item', 'path'), 'etag': ref('$inputs', 'item', 'etag')}),
        http('unmonitor', 'POST', ref('$inputs', 'item', 'unmonitor_path'), body={'id': ref('$inputs', 'item', 'manager_id')}),
        http('remove', 'POST', ref('$inputs', 'item', 'remove_path'), body={'hash': ref('$inputs', 'item', 'hash'), 'delete_files': False})]
    if not omit_log:
        actions.append(http('log', 'POST', ref('$inputs', 'item', 'log_path'), body={'media_id': ref('$inputs', 'item', 'media_id'), 'event': 'cleanup_completed'}))
    actions.append(node('done', 'end', outputs={'media_id': ref('$inputs', 'item', 'media_id')}))
    action = {'nodes': actions, 'edges': [edge(a['id'], b['id']) for a, b in zip(actions, actions[1:])]}
    fields = [{'name': name, 'type': typ, 'required': True} for name, typ in [('first_requests', 'array'), ('now', 'string'), ('users', 'array'), ('field_map', 'object')]]
    fields += [{'name': 'execute', 'type': 'boolean', 'default': True}, {'name': 'retention_hours', 'type': 'number', 'default': 24}]
    nodes = [node('start', 'start', inputs=fields),
        node('first', 'iteration', items=ref('start', 'first_requests'), workflow=fetch, output_node_id='done', parallelism=4),
        node('pages', 'code', code=(HERE/'media_pagination.py').read_text(), inputs={'first_pages': ref('first', 'items')}),
        node('remaining', 'iteration', items=ref('pages', 'output', 'requests'), workflow=fetch, output_node_id='done', parallelism=4),
        node('decide', 'code', code=(HERE/'media_decision.py').read_text(), inputs={'first_pages': ref('first', 'items'), 'other_pages': ref('remaining', 'items'), 'recovery': ref('$recovery'), **{k: ref('start', k) for k in ['now', 'users', 'execute', 'retention_hours', 'field_map']}}),
        node('actions', 'iteration', items=ref('decide', 'output', 'operations'), workflow=action, output_node_id='done', parallelism=1),
        node('readback', 'iteration', items=[{'service': s, 'url': '/'+s+'/records', 'page': 1} for s in ['nas', 'radarr', 'sonarr', 'qbittorrent', 'yamtrack']], workflow=fetch, output_node_id='done', parallelism=4),
        node('verify_pages', 'code', code=(HERE/'media_pagination.py').read_text(), inputs={'first_pages': ref('readback', 'items')}),
        node('verify_remaining', 'iteration', items=ref('verify_pages', 'output', 'requests'), workflow=fetch, output_node_id='done', parallelism=4),
        node('verify', 'code', code=(HERE/'media_verify.py').read_text(), inputs={'first_pages': ref('readback', 'items'), 'other_pages': ref('verify_remaining', 'items'), 'targets': ref('decide', 'output', 'operations')}),
        node('end', 'end', outputs={'analysis': ref('decide', 'output'), 'verification': ref('verify', 'output'), 'markdown': ref('verify', 'output', 'markdown')}, checks=[{'name': label, 'value': ref('verify', 'output', name), 'expected': ref('verify', 'output', 'target_count'), 'failure_message': '原目标仍有未完成动作'} for name, label in [('file', '文件删除'), ('monitor', '取消监控'), ('download', '下载任务移除'), ('log', '处理日志')]])]
    return {'nodes': nodes, 'edges': [edge(a['id'], b['id']) for a, b in zip(nodes, nodes[1:])]}


def inputs(*, variant=False):
    return {'first_requests': [{'service': s, 'url': '/'+s+'/records', 'page': 1} for s in SERVICES],
            'now': '2026-10-23T00:00:00Z', 'users': ['alice', 'bob'], 'execute': True,
            'field_map': {'id': 'MediaId' if variant else 'Id', 'title': 'DisplayName' if variant else 'Name', 'items': 'Items', 'total': 'TotalRecordCount'}}
