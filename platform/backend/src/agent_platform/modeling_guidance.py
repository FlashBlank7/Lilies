"""On-demand modeling instructions and an ordinary editable workflow example."""
import json


GUIDE = """直接完成数据任务；工作流可随时保存，分析、训练和资源就绪都不是草稿的前置条件。

数据与业务含义
一次读取表头、单位、样本含义、目标和缺失/特殊值，复用已保存的概况或处理文件，避免反复完整读取同一资料。
标签是数字不代表质量有大小顺序。没有代码定义时保留原类别，不自行合并、删除稀有类别或命名为合格/不合格，也不把类别代码的 Spearman 当作质量关联。可以继续分析已明确的数值因素，说明标签含义缺项。
区间、斜杠等未解释值保留原文及待确认标记；不要先 errors='coerce' 再把待确认值当普通缺失填补。未确认字段可暂不进入正式模型，明确说明影响；用户允许的假设试算另存并标明规则。缺失类别保留未知，不能用 (类别=='A').astype(int) 把未知混成另一种已知类别。传入原类别列时设置 candidate.features.preprocessing.categorical_missing='preserve'，在训练折内独热编码且不填成已有类别；auto 的 sklearn/Optuna 默认会填训练折众数。新数据的未见类别（含训练未见的缺失）编码全零，不代表业务上已有定义；训练中出现过的缺失单独编码。

分析与流程
用 project_modeling(action='register_dataset', dataset={source_path,mapping}) 保存版本，mapping 设置实际 sheet/header_row/skip_after_header；profile 返回概况，sample_structure 描述有标签样本的分组，labels 是独立标签表的概况。sampled=true 只覆盖前 1000 行，不能把预览外未检查的记录当作无问题。
analyze 使用 analysis.operation='numeric'|'outliers'|'spearman'；这与画布 data_analysis 共用实现。numeric 只采用明确的占位符/区间规则，产生新 dataset_id 和转换明细。outliers 可 flag/drop/clip，默认先 flag；统计离群不等于错误工况。说明检测总体，IQR 为 0 时查看实际边界，不盲目删除或截断。spearman 明确选择可解释的数值字段，保留每对有效样本数、系数、p 值与未定义原因，不把很小的 p 值写成 0，也不宣称相关性是因果。
标准分析先用现有操作；特殊表头、多行样本整理可用 project_code，一次保存处理文件和必要脚本。训练中需要拟合的填补、编码、缩放、异常截断和筛选放在训练折内，通过 candidate.features 配置并随模型保存。
用户要求复用流程时，步骤确定后即可保存分析草稿；无需等模型搜索结束。reference='analysis-workflow.json' 是单行表头、两个已明确数值字段的普通流程示例：概况、IQR 标记、Spearman。用 project_skills(action='read',skill_id='modeling',reference=...) 读取；按实际列和表头调整，输入 source_path 指向新文件，输入 first_column/second_column 选择字段。没有标签解释或模型时，已明确的因素分析仍可独立运行。workflow_run 实际检查结果后，再换一份合法输入验证复用；不重新训练或覆盖旧结果来掩盖失败。

训练与交付
create_study 的 evaluation 固定评价方式。有标签样本的 group_column（未设时 id_column）重复时选择 split='group'，不能随机拆分同一对象；split='time' 需要明确预测时点和可用时间。无分组元数据不等于样本独立，应结合设备、批次和测量过程确定样本单位。正式比较使用 project_modeling 的固定划分；沙盒试算也需保持分组/时间隔离，不能用随机拆分分数替代正式评价。
train 只需 study_id 和 candidate，无需 workflow_id；wait=false 返回原任务，可同时编辑流程。读取原任务/候选结果；失败先按具体原因修正，再提交方案，不能反复启动相同错误。AIDE 是可选搜索策略，用 next_step 选择分支。对照基线、逐类召回和样本覆盖，不只报准确率；留出集不参与调参，finalize 仅在用户要求最终测试时执行。
用 project_models 绑定已完成试验，model_predict 引用 model_ref；预测使用保存的预处理与环境。失败或预算不足也交代已有文件、可运行流程、未完成项及下一步，不把已提交的任务写成已训练成功。交付可下载计算结果、模型引用/下载和流程入口；缺少标签语义、样本或有效评价时，具体说明哪些结论还不能成立。
"""


def ref(node, *path):
    return {'$ref': {'node_id': node, 'path': list(path)}}


ANALYSIS_WORKFLOW = {
    'nodes': [
        {'id': 'start', 'type': 'start', 'title': '选择数据和数值字段', 'config': {'inputs': [
            {'name': 'source_path', 'type': 'string', 'required': True},
            {'name': 'first_column', 'type': 'string', 'required': True},
            {'name': 'second_column', 'type': 'string', 'required': True}]}},
        {'id': 'profile', 'type': 'data_analysis', 'title': '查看数据概况', 'config': {
            'source_path': ref('$inputs', 'source_path'), 'analysis': {'operation': 'profile'}}},
        {'id': 'outliers', 'type': 'data_analysis', 'title': '标记统计异常', 'config': {
            'dataset_id': ref('profile', 'output', 'dataset_id'), 'analysis': {
                'operation': 'outliers', 'columns': [ref('$inputs', 'first_column'), ref('$inputs', 'second_column')],
                'method': 'iqr', 'handling': 'flag'}}},
        {'id': 'spearman', 'type': 'data_analysis', 'title': '计算因素相关性', 'config': {
            'dataset_id': ref('profile', 'output', 'dataset_id'), 'analysis': {
                'operation': 'spearman', 'columns': [ref('$inputs', 'first_column'), ref('$inputs', 'second_column')],
                'missing': 'pairwise'}}},
        {'id': 'end', 'type': 'end', 'title': '查看分析结果', 'config': {'outputs': {
            '数据概况': ref('profile', 'output'), '异常标记': ref('outliers', 'output'),
            '相关分析': ref('spearman', 'output')}}},
    ],
    'edges': [{'id': f'{a}-{b}', 'source': a, 'target': b}
              for a, b in [('start', 'profile'), ('profile', 'outliers'), ('outliers', 'spearman'), ('spearman', 'end')]],
}

MODELING_SKILL = {'name': '项目数据与建模',
    'description': '数值整理、异常值、Spearman、分组训练及模型复用；含可换数据的分析流程示例。',
    'content': GUIDE, 'references': {'analysis-workflow.json': json.dumps(ANALYSIS_WORKFLOW, ensure_ascii=False, indent=2)}}
