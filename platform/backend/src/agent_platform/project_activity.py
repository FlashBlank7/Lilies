"""Readable activity projections over the existing persistent session events."""
from __future__ import annotations

from datetime import datetime


def activity_title(name: str, arguments: dict) -> str:
    action = arguments.get('action', '')
    if name == 'project_search':
        return '搜索公开资料'
    if name == 'project_web':
        return '读取公开来源'
    if name == 'project_modeling':
        return {'submit_and_run': '提交方案并运行训练', 'submit_candidate': '保存训练方案',
                'profile': '分析数据', 'export_dataset': '准备工作流数据文件',
                'create_study': '建立建模研究', 'training_note': '查看训练笔记',
                'candidates': '比较模型结果', 'finish': '整理本轮建模结果'}.get(action, '查看建模进展')
    if name == 'project_action':
        if action == 'wait' and arguments.get('task_id'):
            return '等待任务结果'
        return {'inspect': '查看项目进展', 'build': '准备搭建工作流', 'trial': '试用业务流程',
                'operate': '处理业务请求', 'resume': '继续原任务', 'wait': '整理待补条件',
                'finish': '整理本次交付', 'discuss': '修订需求理解'}.get(action, '推进项目')
    if name == 'workflow_draft':
        return '修改工作流草稿' if arguments.get('operation') or arguments.get('batch') else '读取工作流草稿'
    if name == 'workflow_run':
        return {'start': '运行工作流', 'tests': '测试工作流', 'inspect': '查看运行结果',
                'validate': '校验工作流'}.get(action, '检查工作流')
    if name == 'project_file':
        return '保存项目文件' if action == 'write' else '读取项目资料'
    return {'project_progress': '更新项目进展' if action in {'update', 'patch'} else '查看项目进展',
            'project_workflows': '管理项目工作流', 'project_records': '读取业务记录',
            'project_task_result': '整理业务结果', 'requirements_submit': '整理需求文档',
            'block_catalog': '查看积木说明', 'tool_catalog': '查看工具说明'}.get(name, '执行项目操作')


def project_activity(event: dict) -> dict:
    """Every update is self contained, including when the start is on an older page."""
    started, ended = event.get('started_at', event['time']), event.get('ended_at')
    duration = None
    if ended:
        try:
            duration = max(0, (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())
        except ValueError:
            pass
    return {key: event.get(key, '') for key in (
        'id', 'operation_id', 'request_id', 'item_id', 'task_id', 'workflow_id', 'workflow_name',
        'title', 'status', 'summary', 'arguments', 'result', 'time')} | {
        'started_at': started, 'ended_at': ended, 'duration_seconds': duration,
        'tool_name': event['text'],
    }


def latest_operations(events: list[dict]) -> list[dict]:
    operations = {}
    for event in events:
        if event.get('operation_id'):
            operations[event['operation_id']] = project_activity(event)
    return list(operations.values())
