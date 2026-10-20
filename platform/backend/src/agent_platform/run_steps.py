"""Small, read-only step projections of the graph saved with a workflow run."""
from collections import defaultdict, deque
from datetime import datetime
from itertools import islice
import re


_PRIVATE = re.compile(r'(password|passwd|secret|credential|authorization|cookie|private.?key|api.?key|(^|[_-])token$)', re.I)
_CODE = {'code', 'source_code', 'script', 'workflow', 'system_prompt'}
_INPUT_FIELDS = {'input', 'inputs', 'variables', 'items', 'value', 'values', 'context',
                 'query', 'text', 'url', 'method', 'params', 'body', 'json_body',
                 'headers', 'assignments', 'outputs', 'answer', 'initial_state',
                 'key', 'collection', 'file', 'files', 'path', 'paths', 'payload',
                 'series', 'unit', 'horizon', 'forecasts', 'capacity', 'budget'}
_UNAVAILABLE = '本次记录中没有这个值'


def _preview(value, *, inputs=None, outputs=None, run_id=None, string_limit=600):
    """Bound work and output while resolving data references, never expressions/code."""
    remaining = 2400

    def visit(item, depth=0, resolve=True):
        nonlocal remaining
        if remaining <= 0:
            return '…'
        remaining -= 8
        if depth >= 5 and isinstance(item, (dict, list)):
            return '…'
        if isinstance(item, dict):
            if '$secret' in item:
                return '••••••'
            reference = item.get('$ref')
            if resolve and inputs is not None and isinstance(reference, dict) and set(item) <= {'$ref', 'optional'}:
                ident, path = reference.get('node_id'), reference.get('path', [])
                if any(_PRIVATE.search(str(key)) or key in _CODE for key in path):
                    return '••••••'
                source = (inputs if ident == '$inputs' else {'run_id': run_id} if ident == '$run'
                          else (outputs or {}).get(ident))
                expanded = [part for key in path for part in (str(key).split('.') if isinstance(key, str) else [key])]
                for walk in (expanded, path):
                    current = source
                    try:
                        for key in walk:
                            current = current[int(key)] if isinstance(current, list) else current[key]
                        return visit(current, depth + 1, resolve=False)
                    except (KeyError, IndexError, TypeError, ValueError):
                        pass
                return None if item.get('optional') or reference.get('optional') else _UNAVAILABLE
            if resolve and inputs is not None and any(str(key).startswith('$') for key in item):
                return '表达式未计算'
            result = {}
            for key, val in islice(item.items(), 12):
                if remaining <= 0:
                    result['…'] = '其余内容已省略'
                    break
                shown_key = str(key)[:100]
                remaining -= len(shown_key)
                result[shown_key] = ('••••••' if _PRIVATE.search(str(key)) else '内容已省略'
                                     if key in _CODE else visit(val, depth + 1, resolve))
            if len(item) > 12:
                result['…'] = f'另有 {len(item) - 12} 项'
            return result
        if isinstance(item, list):
            result = []
            for val in item[:8]:
                if remaining <= 0:
                    break
                result.append(visit(val, depth + 1, resolve))
            if len(result) < len(item):
                result.append(f'…另有 {len(item) - len(result)} 项')
            return result
        if isinstance(item, str):
            # Credentials can also appear in HTTP errors and URLs.
            truncated = len(item) > min(string_limit, remaining)
            item = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer ••••••', item[:max(0, min(string_limit, remaining))])
            item = re.sub(r'(?i)((?:api[_-]?key|access_token|password|secret)\s*[=:]\s*)[^\s&\"\',}]+', r'\1••••••', item)
            remaining -= len(item)
            return item + ('…' if truncated else '')
        return item

    return visit(value)


def _error_preview(error):
    if not isinstance(error, str) or 'Traceback (most recent call last):' not in error:
        return _preview(error)
    # A Python stack often fills the entire ordinary preview before its cause.
    # Keep the final exception and its following lines (e.g. missing columns),
    # leaving the full stack available through the existing event endpoint.
    traceback = error.rsplit('Traceback (most recent call last):', 1)[1]
    exception = re.search(r'(?m)^(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?::|$)', traceback)
    reason = traceback[exception.start():] if exception else traceback[-1000:]
    return _preview(reason.strip(), string_limit=1000)


def _business_preview(value):
    """Prefer saved explanations over transport wrappers; never infer outcomes."""
    technical = {'sha256', 'logs', 'operation', 'comparison_inputs', 'search_states',
                 'complete_within_declared_limits', *_CODE}
    remaining = 160

    def select(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            return '其余内容见结果文件'
        if depth >= 5:
            return item
        if isinstance(item, list):
            result = [select(v, depth + 1) for v in item[:8]]
            if len(item) > 8:
                result.append(f'另有 {len(item) - 8} 项，见结果文件')
            return result
        if not isinstance(item, dict) or '$secret' in item:
            return item
        # Code and end nodes wrap their saved result. A business report may be
        # beyond the first twelve keys of that result; select it before bounding.
        if isinstance(item.get('markdown'), str) and item['markdown'].strip():
            return {'markdown': item['markdown']}
        keys = list(islice((key for key in item if key not in technical), 13))
        if len(keys) == 1 and keys[0] in {'output', 'result', 'inputs', 'outputs'}:
            return select(item[keys[0]], depth + 1)
        result = {key: select(item[key], depth + 1) for key in keys[:12]}
        if len(keys) > 12:
            result['更多内容'] = '其余字段见技术详情或结果文件'
        return result

    return _preview(select(value), string_limit=2000)


def _ordered_nodes(graph):
    """Match the runtime's stable topological order, independent of canvas position."""
    nodes = {node['id']: node for node in graph.get('nodes', [])}
    indegree = dict.fromkeys(nodes, 0)
    outgoing = defaultdict(list)
    for edge in graph.get('edges', []):
        if edge['source'] in nodes and edge['target'] in nodes:
            indegree[edge['target']] += 1
            outgoing[edge['source']].append(edge['target'])
    queue = deque(ident for ident, degree in indegree.items() if degree == 0)
    seen = set()
    while queue:
        ident = queue.popleft()
        seen.add(ident)
        yield nodes[ident]
        for target in outgoing[ident]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    # Old invalid graphs should still be readable.
    yield from (node for ident, node in nodes.items() if ident not in seen)


def _event_details(events):
    details = defaultdict(dict)
    for event in events:
        ident = event.data.get('node_id')
        if not ident or event.type not in {'node.started', 'node.completed', 'node.failed',
                                          'node.retry', 'node.degraded', 'node.skipped', 'node.reused'}:
            continue
        detail = details[ident]
        detail['last'] = event.type
        if event.type == 'node.started':
            detail.clear()
            detail.update(last=event.type, started=event.created_at)
        elif event.type == 'node.failed':
            detail['error'] = event.data.get('error')
        if event.type in {'node.completed', 'node.failed'} and detail.get('started'):
            try:
                elapsed = datetime.fromisoformat(event.created_at) - datetime.fromisoformat(detail['started'])
                detail['duration_ms'] = max(0, round(elapsed.total_seconds() * 1000))
            except (TypeError, ValueError):
                pass
    return details


async def run_steps(services, run_id, *, offset=0, limit=100):
    record = await services.workflow_store.get_run(run_id)
    state = record['state']
    # Only status/time/error hints come from this bounded window. Persisted state
    # is authoritative even when thousands of later token events hide a start.
    details = _event_details(await services.storage.list_events(run_id, limit=1000, tail=True))
    graph = state.snapshot.workflow.model_dump(mode='json')
    rows = []

    def walk(workflow, progress, prefix='', node_path=(), scope=()):
        for node in _ordered_nodes(workflow):
            ident = prefix + node['id']
            rows.append((node, progress, ident, [*node_path, node['id']], ' / '.join(scope)))
            nested = node.get('config', {}).get('workflow')
            if node['type'] not in {'loop', 'iteration'} or not isinstance(nested, dict):
                continue
            pattern = re.compile(re.escape(ident) + r'\[(\d+)\]\.')
            occurrences = [(int(match[1]), key, value) for key, value in state.nested_progress.items()
                           if (match := pattern.fullmatch(key))]
            for index, key, value in sorted(occurrences):
                walk(nested, value, key, (*node_path, node['id']),
                     (*scope, f'{_preview(node["title"])} · 第 {index + 1} 轮'))

    walk(graph, {'inputs': state.inputs, 'outputs': state.outputs,
                 'completed': state.completed, 'skipped': state.skipped})
    steps = []
    failed = re.match(r'node (.+?) failed:', record.get('error') or '')
    for node, progress, ident, path, scope in rows[offset:offset + limit]:
        outputs = progress.get('outputs', {})
        output = outputs.get(node['id'])
        detail = details.get(ident, {})
        complete = node['id'] in progress.get('completed', []) or node['id'] in outputs
        error = output.get('error') if isinstance(output, dict) else None
        if complete:
            status = 'warning' if error else 'reused' if not scope and node['id'] in state.reused_nodes else 'completed'
        elif node['id'] in progress.get('skipped', []):
            status = 'skipped'
        elif state.waiting_node_id == ident or (state.waiting_node_id or '').startswith(ident + '['):
            status = 'waiting'
        elif detail.get('last') in {'node.failed', 'node.degraded'} or failed and failed[1] == ident:
            status, error = 'failed', detail.get('error') or record.get('error')
        elif detail.get('last') in {'node.started', 'node.retry'}:
            status = 'running' if record['status'] in {'running', 'queued'} else 'interrupted'
        else:
            status = 'pending'
        inputs = progress.get('inputs', {})
        if node['type'] in {'start', 'schedule_trigger'}:
            input_preview, input_source = _preview(inputs), '本次输入'
        else:
            config = node.get('config', {})
            arguments = {key: value for key, value in config.items() if key in _INPUT_FIELDS}
            input_preview = _preview(arguments, inputs=inputs, outputs=outputs, run_id=run_id)
            input_source = '按本次记录还原'
        steps.append({'id': ident, 'node_path': path, 'title': _preview(node['title']),
                      'description': _preview(node.get('description', '')), 'type': node['type'],
                      'status': status, 'input_preview': input_preview, 'input_source': input_source,
                      'output_preview': _preview(output), 'error': _error_preview(error),
                      'input_summary': _business_preview(input_preview),
                      'output_summary': _business_preview(output),
                      'duration_ms': detail.get('duration_ms'), 'scope': scope})
    return {'run_id': run_id, 'application_id': record['application_id'],
            'name': _preview(state.snapshot.name), 'status': record['status'],
            'draft_revision': record.get('draft_revision'), 'error': _error_preview(record.get('error')),
            'steps': steps, 'total': len(rows),
            'next_offset': offset + limit if offset + limit < len(rows) else None}
