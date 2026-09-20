"""Small measurements over existing session events, without a second event store."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime


def payload_measurement(value) -> dict:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return {'bytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()}


def is_read_call(name: str, arguments: dict) -> bool:
    if name == 'workflow_draft':
        return not (arguments.get('operation') or arguments.get('batch'))
    if name == 'project_modeling':
        return arguments.get('action') in {'datasets', 'studies', 'read_study', 'candidates', 'training_note', 'next_step'}
    if name == 'project_action' and arguments.get('action') == 'wait' and arguments.get('task_id'):
        return True
    return ((name == 'project_file' and arguments.get('action') in {'read', 'profile', 'list'})
            or (name == 'project_progress' and arguments.get('action', 'read') == 'read')
            or name in {'block_catalog', 'project_records', 'project_knowledge'}
            or (name == 'project_workflows' and arguments.get('action', 'list') == 'list')
            or (name == 'workflow_run' and arguments.get('action') == 'inspect'))


def _seconds(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _union_seconds(intervals):
    end, duration = float('-inf'), 0.0
    for start, stop in sorted(intervals):
        duration += max(0, stop - max(start, end))
        end = max(end, stop)
    return round(duration, 6)


def session_metrics(events: list[dict], request_id: str = '') -> dict:
    """Measurements, not a completion verdict. Legacy missing sizes stay explicit.

    A repeated read means an identical measured response from the same tool
    within one request, not proof that the read was unnecessary. Concurrent
    tool time is a union, so it cannot exceed observed request wall time.
    """
    groups: dict[str, list[dict]] = {}
    for event in events:
        key = event.get('request_id')
        if key and (not request_id or key == request_id):
            groups.setdefault(key, []).append(event)
    requests = []
    for key, rows in groups.items():
        tools, starts = {}, {}
        for e in rows:
            if e['kind'] == 'tool_started':
                starts[e.get('operation_id') or e['id']] = e
            if e['kind'] == 'tool':
                tools[e.get('operation_id') or e['id']] = e
        operations = {**starts, **tools}
        times = [t for e in rows if (t := _seconds(e.get('time'))) is not None]
        users = [e for e in rows if e['kind'] == 'user']
        began = min((_seconds(e.get('time')) for e in users), default=min(times) if times else None)
        first = next((e for e in rows if e['kind'] == 'result' and e.get('task_id')
                      and e.get('purpose') != 'build_test'), None)
        delivered = _seconds(first.get('time')) if first else None
        intervals = []
        for e in tools.values():
            start, stop = _seconds(e.get('started_at')), _seconds(e.get('ended_at'))
            if start is not None and stop is not None and stop >= start:
                intervals.append((start, stop))
        measured = [e for e in tools.values() if 'output_bytes' in e]
        turns = [e for e in rows if e['kind'] == 'agent_turn_started']
        model_calls = [e for e in rows if e['kind'] == 'model_usage']
        seen_reads, seen_parts = set(), set()
        duplicate_reads = duplicate_bytes = repeated_context_bytes = 0
        for e in measured:
            identity = (e['text'], e.get('output_sha256'))
            if e.get('read_call') and identity[1]:
                if identity in seen_reads:
                    duplicate_reads += 1
                    duplicate_bytes += e['output_bytes']
                seen_reads.add(identity)
        for e in turns:
            for name, part in e.get('context_parts', {}).items():
                identity = (name, part['sha256'])
                if identity in seen_parts:
                    repeated_context_bytes += part['bytes']
                seen_parts.add(identity)
        requests.append({'request_id': key, 'user_messages': len(users),
            'model_calls': len(model_calls) if model_calls else None,
            'model_seconds': sum(e.get('seconds', 0) for e in model_calls) if model_calls else None,
            'model_usage': [e['usage'] for e in model_calls],
            'observed_elapsed_seconds': round(max(times) - began, 6) if times and began is not None else None,
            'first_presented_result_seconds': round(delivered - began, 6) if delivered is not None and began is not None else None,
            'first_presented_task_id': first.get('task_id') if first else None,
            'tool_calls': len(operations), 'tool_failures': sum(e.get('success') is False for e in tools.values()),
            'tool_wall_seconds': _union_seconds(intervals),
            'measured_input_bytes': sum(e.get('input_bytes', 0) for e in operations.values()),
            'measured_output_bytes': sum(e['output_bytes'] for e in measured),
            'unmeasured_outputs': len(operations) - len(measured),
            'agent_turns': len(turns), 'measured_context_bytes': sum(e.get('context_bytes', 0) for e in turns),
            'repeated_context_part_bytes': repeated_context_bytes,
            'identical_read_responses': duplicate_reads, 'identical_read_bytes': duplicate_bytes})
    return {'requests': requests, 'notes': [
        '字节是工具参数/返回值及每轮新增上下文的 UTF-8 JSON 大小，不是模型 token 或计费量。',
        '相同读取按同一请求内的工具名和完整返回值哈希计数；相同内容未必都是多余读取。',
        '首个呈现结果不等于首次可用结果；可用性和工程介入次数需要按同一业务用例及对话核验。',
        '未计量的旧事件或失败返回不补估；工具耗时按并行区间合并，不重复累计。']}
