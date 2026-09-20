"""Shared offline Python execution for project conversations and code nodes."""
import asyncio
import json
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from typing import Any


class CodeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(default='def main(inputs):\n    return inputs', max_length=100000)
    inputs: Any = Field(default_factory=dict)
    timeout: int = Field(default=60, ge=1, le=300)


async def execute_python(sandboxes, workspace, code, timeout):
    workspace = sandboxes.resolve_workspace(str(workspace))
    sandboxes.protect_inputs(workspace, ['requirement-package', 'requirements'])
    session = 'project-code-' + str(uuid4())
    creation = asyncio.create_task(sandboxes.get_or_create(session, str(workspace), 'none', []))
    try:
        sandbox = await asyncio.shield(creation)
        result = await sandbox.run(['python', '-'], stdin=code, timeout=timeout)
        return {'stdout': result.stdout, 'stderr': result.stderr, 'exit_code': result.exit_code,
                'output_truncated': result.output_truncated}
    finally:
        await asyncio.gather(creation, return_exceptions=True)
        await sandboxes.remove(session)


async def execute_function(sandboxes, workspace, config, inputs):
    # Only JSON literals enter the wrapper; user source runs inside Docker.
    program = ('import contextlib, json, sys\nnamespace = {}\n'
        'with contextlib.redirect_stdout(sys.stderr):\n'
        f'    exec(compile({config.code!r}, "workflow.py", "exec"), namespace)\n'
        f'    result = namespace["main"](json.loads({json.dumps(inputs, ensure_ascii=False)!r}))\n'
        'print(json.dumps(result, ensure_ascii=False))\n')
    result = await execute_python(sandboxes, workspace, program, config.timeout)
    if result['exit_code']:
        raise ValueError('Python 执行失败：' + result['stderr'][-8000:])
    if result['output_truncated']:
        raise ValueError('代码输出过大，请将完整结果保存为文件并返回路径')
    return {'output': json.loads(result['stdout']), 'logs': result['stderr']}


def register_code_block(registry):
    from .blocks import _definition
    from .workflow_models import ValueType
    definition = _definition('code', 'Python 代码',
        '在项目 Docker 环境执行 Python。定义 main(inputs)，返回可序列化结果；输出位于 output，print 内容保存在 logs。环境禁用网络。',
        'transform', CodeConfig, inputs=[('input', ValueType.any)], outputs=[('output', ValueType.any)], error_branch=True)
    definition.editor['i18n']['zh'].update(title='Python 代码', description=definition.description)
    definition.editor['fields'] = [
        {'path':'code','label':'Python 代码','label_zh':'Python 代码','control':'textarea'},
        {'path':'inputs','label':'输入字段','label_zh':'输入字段','control':'json'},
        {'path':'timeout','label':'超时（秒）','label_zh':'超时（秒）','control':'number','minimum':1,'maximum':300},
    ]
    registry.register(definition, CodeConfig)
