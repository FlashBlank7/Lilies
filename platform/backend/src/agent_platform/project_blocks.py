"""Shared business records; fields and policies belong to the generated workflow."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectRecordConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['get', 'list', 'put']
    collection: Any
    key: Any = ''
    value: Any = Field(default_factory=dict)
    expected_revision: Any = 0


def register_project_blocks(registry):
    from .blocks import _definition
    from .workflow_models import ValueType

    definition = _definition('project_record', '项目业务记录',
        '读取或原子更新当前项目的共享业务记录。项目身份由运行时绑定。',
        'integration', ProjectRecordConfig, inputs=[('input', ValueType.any)],
        outputs=[('output', ValueType.object)], error_branch=True,
        manual={
            'summary': 'get 返回 found/revision/value；不存在时 found=false、revision=0、value=null。'
                       'list 返回 records（最多1000条）。put 必须提供读取到的 expected_revision；新建用0。'
                       'put 成功返回 written=true；冲突返回 written=false、conflict=true 和当前记录，不覆盖。'
                       '使用条件节点处理冲突，重新读取后判断，不能强行写入。所有字段支持平台 $ref。'
                       '数据写入宿主数据库，不通过 Bash 或文件读写访问数据库。'
                       '保存测试的每个用例使用独立空白记录空间；该用例的父子工作流共享它，'
                       '不会读取或修改正式业务记录，也不会继承其他用例或上次测试的记录。'
                       '测试需要的初始记录应由该用例的工作流准备。普通项目运行与客户试用仍共享项目业务记录。',
            'when_to_use': ['多个工作流共享申请、资源或处理状态时。'],
            'examples': [
                {'description': '读取共享记录', 'config': {'action': 'get', 'collection': 'resources', 'key': 'room-a'}},
                {'description': '按读取的版本更新', 'config': {'action': 'put', 'collection': 'resources', 'key': 'room-a',
                 'expected_revision': {'$ref': {'node_id': 'read', 'path': ['revision']}}, 'value': {'available': False}}},
            ],
            'anti_patterns': ['不要把项目id放进配置；不要忽略 written=false 的并发冲突。'],
            'common_errors': ['独立应用运行没有项目上下文；请从项目发起运行。'],
            'claude_architecture_mapping': 'Host project record service',
            'composability_constraints': ['需要可信项目运行上下文；更新必须检查冲突。'],
        })
    definition.editor['i18n']['zh']['title'] = '项目业务记录'
    definition.editor['i18n']['en'].update(
        title='Project Records', description='Read or update shared records in the current project.')
    definition.editor['fields'] = [
        {'path': 'action', 'label': '操作', 'control': 'enum', 'options': ['get', 'list', 'put']},
        {'path': 'collection', 'label': '集合', 'control': 'reference_or_text'},
        {'path': 'key', 'label': '记录键', 'control': 'reference_or_text'},
        {'path': 'value', 'label': '记录内容', 'control': 'json'},
        {'path': 'expected_revision', 'label': '期望修订号', 'control': 'json'},
    ]
    registry.register(definition, ProjectRecordConfig)
