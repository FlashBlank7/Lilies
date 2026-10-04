"""Shared offline Python execution for project conversations and code nodes."""
import asyncio
import hashlib
import json
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from typing import Any


class CodeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(default='def main(inputs):\n    return inputs', max_length=100000)
    inputs: Any = Field(default_factory=dict)
    timeout: int = Field(default=60, ge=1, le=300)
    reuse_completed: bool = Field(default=False, title='允许复用已完成结果',
        description='仅用于所有依赖文件通过输入声明、产物路径通过输出返回的代码。外部调用、隐藏文件依赖或需要每次执行的代码请勿开启。')


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
    code_hash = hashlib.sha256(config.code.encode('utf-8')).hexdigest()
    program = ('import contextlib, json, sys\n'
        f'namespace = {{"__workflow_code_sha256__": {code_hash!r}}}\n'
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
        'transform', CodeConfig, inputs=[('input', ValueType.any)], outputs=[('output', ValueType.any)], error_branch=True,
        manual={
            'summary': '执行显式 Python 处理逻辑。main(inputs) 接收配置的 inputs；返回任意可 JSON 序列化的值，运行结果为 {output: 返回值, logs: print 文本}。',
            'when_to_use': ['需要用确定的规则清洗表格、计算汇总、标记重复记录或生成文件时。',
                '输入来源和已暴露的参数可在表单中修改；计算方法、分组规则等写在代码里的逻辑需要编辑 Python，或请 Lilies 修改该节点。'],
            'examples': [{'description': '把上游费用记录传入 inputs，由 main(inputs) 计算合计，再将 output 交给后续节点。',
                'connection': 'start → code → end',
                'config': {'inputs': {'amounts': [10, 20]}, 'code': 'def main(inputs):\n    return {"total": sum(inputs["amounts"])}'}}],
            'anti_patterns': ['不要把尚未暴露为输入的业务规则当成可直接修改的表单参数。'],
            'common_errors': ['需定义 main(inputs)，返回值必须可 JSON 序列化；完整文件应保存后返回文件路径。',
                '下游读取返回值用 output；print 仅保存在 logs，不是返回结果。'],
            'claude_architecture_mapping': 'Explicit offline Python function execution',
            'composability_constraints': ['输入和返回值可以是文本、数字、对象或数组；端口未限定具体结构，需按当前代码确认字段。',
                '代码在禁网环境执行；需要的依赖须已安装。'],
        })
    definition.editor['i18n']['zh'].update(title='Python 代码', description=definition.description)
    definition.editor['fields'] = [
        {'path':'code','label':'Python 代码','label_zh':'Python 代码','control':'textarea',
         'description_zh':'这里决定实际处理规则。定义 main(inputs)，读取下方输入字段并 return 结果；只修改输入不会改变代码中的固定规则。'},
        {'path':'inputs','label':'输入字段','label_zh':'输入字段','control':'json',
         'description_zh':'传给 main(inputs) 的数据与参数。可选上游输出或填写固定值；字段名必须与代码读取的名称一致。'},
        {'path':'timeout','label':'超时（秒）','label_zh':'超时（秒）','control':'number','minimum':1,'maximum':300,
         'description_zh':'代码执行超过此时长会停止。处理较大文件时可适当增加，最多 300 秒。'},
        {'path':'reuse_completed','label':'允许复用已完成结果','label_zh':'允许复用已完成结果','control':'boolean',
         'description':'所有依赖文件须通过输入声明，产物文件须返回路径。存在隐藏依赖或需要每次执行时保持关闭。'},
    ]
    registry.register(definition, CodeConfig)
