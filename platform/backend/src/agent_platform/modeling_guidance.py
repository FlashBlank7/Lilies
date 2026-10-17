"""On-demand modeling instructions and an ordinary editable workflow example."""
import json
from copy import deepcopy


GUIDE = """直接完成数据任务；工作流可随时保存，分析、训练和资源就绪都不是草稿的前置条件。

数据与业务含义
一次读取表头、单位、样本含义、目标和缺失/特殊值，复用已保存的概况或处理文件，避免反复完整读取同一资料。
先区分表格格式与业务语义：空单元格不自动代表上一行，合并显示不证明多行属于同一件产品；编号重复不自动允许去重。资料未说明行粒度、空白沿用、标签定义、预测时点与可用字段时，集中向用户提出会影响当前分析的问题，说明不同答案的影响，再执行依赖这些答案的转换或训练。不要自行假设后再补问，也不要把问题埋在已交付报告里。可以先做不改变语义的概况、原值统计和草稿；用户答未知时保留未知，或在明确获准的假设下另存试算。
标签是数字不代表质量有大小顺序。没有代码定义时保留原类别，不自行合并、删除稀有类别或命名为合格/不合格，也不把类别代码的 Spearman 当作质量关联。可以继续分析已明确的数值因素，说明标签含义缺项。
区间、斜杠等未解释值保留原文及待确认标记；不要先 errors='coerce' 再把待确认值当普通缺失填补。未确认字段可暂不进入正式模型，明确说明影响；用户允许的假设试算另存并标明规则。缺失类别保留未知，不能用 (类别=='A').astype(int) 把未知混成另一种已知类别。传入原类别列时设置 candidate.features.preprocessing.categorical_missing='preserve'，在训练折内独热编码且不填成已有类别；auto 的 sklearn/Optuna 默认会填训练折众数。新数据的未见类别（含训练未见的缺失）编码全零，不代表业务上已有定义；训练中出现过的缺失单独编码。

分析与流程
字段含义规则
有未知符号、区间或数字类别时，在 dataset.mapping.field_rules 按列保存规则，再使用平台分析、训练和预测；参考 field-rules.json。每列 dtype=number/category/text，description 和 unit 写已有定义；meaning=pending 表示整列含义未知。missing_tokens 是“原文→缺失原因”的字典（unknown/not_measured/not_applicable/unjudged），pending_tokens 是仍不知道含义的符号列表。空白默认为 unknown；allow_missing 只列出该字段明确允许交给模型缺失处理的原因，默认不允许。intervals 默认 pending；只有含义和取值方式明确时才选 midpoint/lower/upper。数字类别用 category；无明确等级顺序不得做秩相关，ordered_values 只用于已说明的顺序。
用户已说不知道时保持待确认，继续可做工作，不再反复问同一问题。模拟业务答复只写入演练项目规则，不能替代企业真实定义。普通格式解析可自动执行；含义未知不得用 to_numeric(errors='coerce')、空字符串或填零抹掉。自定义表头/合并单元格整理应保留所有测量原文、原标签和源行对应；仅按明确业务定义补齐分组标识。不要先在 project_code 里把未知值变成空白再登记，否则平台无法恢复丢失的含义。
有规则的数据集直接用于 profile、spearman、outliers 和训练，无需先把字段手工清洗成数字。分析只使用有效数值并报告样本数，data_state 给出原值、位置和原因；统计异常默认标记。训练遇到待确认/无效特征默认报错，mapping.invalid_rows='exclude' 可明确在固定划分前排除这些行，结果 split.excluded_data_rows 说明数量；它基于所有已声明规则的非元数据字段，若研究完全不使用某列，请在该研究输入中明确移除该列及其规则。目标缺失不填补；unjudged 标签不参加监督训练。预测保留每行，无法使用的行 prediction_status='unavailable'，prediction_reason 说明原因，不把它当作已预测。预测只检查模型实际需要的字段，缺少目标不影响预测。
numeric、异常删除/截断返回的新 dataset_id 已携带 data-state.json；优先继续使用这个 dataset_id。若重新登记 processed.csv，必须同时传 state_path，并保留完全相同的 mapping.field_rules；状态文件与表格绑定，换表必须重新解释原始数据，不能复制旧状态。模型包保存字段规则，预测继承训练定义，数据另带不一致规则时会明确报错。最终交付同时链接 data_state_path 或分析 artifacts 中的 data-state.json，说明待确认/排除/无法预测的数量。规则可在草稿高级配置中修改，保存草稿不要求数据问题已解决。

用 project_modeling(action='register_dataset', dataset={source_path,mapping}) 保存版本，mapping 设置实际 sheet/header_row/skip_after_header；profile 返回概况，sample_structure 描述有标签样本的分组，labels 是独立标签表的概况。sampled=true 只覆盖前 1000 行，不能把预览外未检查的记录当作无问题。
analyze 使用 analysis.operation='numeric'|'outliers'|'spearman'；这与画布 data_analysis 共用实现。numeric 只采用明确的占位符/区间规则，产生新 dataset_id 和转换明细。outliers 可 flag/drop/clip，默认先 flag；统计离群不等于错误工况。说明检测总体，IQR 为 0 时查看实际边界，不盲目删除或截断。spearman 明确选择可解释的数值字段，保留每对有效样本数、系数、p 值与未定义原因，不把很小的 p 值写成 0，也不宣称相关性是因果。
标准分析先用现有操作；特殊表头、多行样本整理可用 project_code，一次保存处理文件和必要脚本。训练中需要拟合的填补、编码、缩放、异常截断和筛选放在训练折内，通过 candidate.features 配置并随模型保存。
用户要求复用流程时，步骤确定后即可保存分析草稿；无需等模型搜索结束。reference='analysis-workflow.json' 是单行表头、两个已明确数值字段的普通流程示例：概况、IQR 标记、Spearman。用 project_skills(action='read',skill_id='modeling',reference=...) 读取；按实际列和表头调整，输入 source_path 指向新文件，输入 first_column/second_column 选择字段。没有标签解释或模型时，已明确的因素分析仍可独立运行。workflow_run 实际检查结果后，再换一份合法输入验证复用；不重新训练或覆盖旧结果来掩盖失败。
同时交付分析和预测时，读取 reference='analysis-prediction-workflow.json'：同一次输入登记的数据集连接概况、异常标记、Spearman 和模型预测，结束节点返回每项结果及文件。这是普通可编辑节点，可以在概况前插入原表整理节点并引用其本次输出。业务字段确定后把 analysis_columns、mapping、model_ref 固定到节点配置或入口默认值，让现场只需提供 source_path。不要把本次运行的 dataset_id 或静态相关文件写死到流程；模型引用可以固定，分析数据集必须随输入更新。

训练与交付
create_study 的 evaluation 固定评价方式。有标签样本的 group_column（未设时 id_column）重复时选择 split='group'，不能随机拆分同一对象；split='time' 需要明确预测时点和可用时间。无分组元数据不等于样本独立，应结合设备、批次和测量过程确定样本单位。正式比较使用 project_modeling 的固定划分；沙盒试算也需保持分组/时间隔离，不能用随机拆分分数替代正式评价。
train 只需 study_id 和 candidate，无需 workflow_id；wait=false 返回原任务，可同时编辑流程。读取原任务/候选结果；失败先按具体原因修正，再提交方案，不能反复启动相同错误。AIDE 是可选搜索策略，用 next_step 选择分支。对照基线、逐类召回和样本覆盖，不只报准确率；留出集不参与调参，finalize 仅在用户要求最终测试时执行。
用 project_models 绑定已完成试验，model_predict 引用 model_ref；预测使用保存的预处理与环境。失败或预算不足也交代已有文件、可运行流程、未完成项及下一步，不把已提交的任务写成已训练成功。交付可下载计算结果、模型引用/下载和流程入口；缺少标签语义、样本或有效评价时，具体说明哪些结论还不能成立。
交付模型用 project_models(action='export', model_ref='已绑定名称')，生成完整模型 ZIP、报告 Markdown 和结构化 JSON，返回可直接链接的项目文件路径。无需额外授权或手工拼研究/候选编号；导出不训练、不评价保留测试。报告使用导出的实际指标、split.development_samples/holdout_samples/holdout_status；not_evaluated 表示已留出但尚未评价，不能写成没有保留测试。
字段计数使用 profile 的 value_counts（最多12种值）或从保存表按明确条件计算；原始非空数、实际填充数、仍缺失数分别统计，不把“原始记录标记为真”当作填充数。自定义处理脚本同时保存统计 JSON，并从该结果生成报告表格，避免凭会话记忆手写数字。业务假设和解释另外标明，计算结果不能自动证明假设成立。
用户需要日常换表时，把整理步骤纳入流程，保留源行号和无法预测的原因。完成核心训练后优先导出、保存及运行流程、写报告和最终回复；已有结果按所需字段读取，避免反复全量列表。普通对话无需 project_task_result；用最终回复提供可点击文件与工作流入口。
同一份最终统计驱动报告和交付清单；区分重复组数、涉及行数与多余行数等口径。把用户要求的异常处理、相关分析、逐条结果、模型包和使用说明实际保存并链接，不只在正文说做过。工作流结束节点返回这些成果路径；最终清单指向本次成功运行的结果，不混用旧中间文件。若有未实现项直接说明，不把后续计划当作已交付。
用户要求复用整套分析时，只有整理和预测的流程还不完整：概况、异常与相关分析都应基于本次输入重新计算并返回结果。核对实际运行输出是否覆盖这些要求；已有静态相关表不能代表换表后也会重算。无法完成时明确说明缺少哪一步，不把“结构有效”或“预测成功”写成整套分析已经可复用。
复用验证要实际改变一份小样的字段值并另存为同格式输入，保留原文件；只传用户必填输入运行同一修订，检查行数、源行对应与预期输入变化，测试产物使用独立目录（可用 $run.run_id），避免覆盖交付数据。只换文件名或复制原表不算换数据验证。声明过的可选入口字段省略时，$inputs 使用默认值，无默认值为 null；未声明的字段仍需显式 optional 引用或先声明。运行失败先修正报错对应的引用/配置，不要重复改节点代码去补偿代码执行前的引用错误。
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

ANALYSIS_PREDICTION_WORKFLOW = deepcopy(ANALYSIS_WORKFLOW)
ANALYSIS_PREDICTION_WORKFLOW['nodes'][0]['config']['inputs'] = [
    {'name': 'source_path', 'type': 'string', 'required': True},
    {'name': 'analysis_columns', 'type': 'array', 'required': True},
    {'name': 'model_ref', 'type': 'string', 'required': True},
    {'name': 'mapping', 'type': 'object', 'default': {}},
]
ANALYSIS_PREDICTION_WORKFLOW['nodes'][1]['config']['mapping'] = ref('$inputs', 'mapping')
for node in ANALYSIS_PREDICTION_WORKFLOW['nodes'][2:4]:
    node['config']['analysis']['columns'] = ref('$inputs', 'analysis_columns')
ANALYSIS_PREDICTION_WORKFLOW['nodes'].insert(-1, {
    'id': 'predict', 'type': 'model_predict', 'title': '预测本次数据', 'config': {
        'dataset_id': ref('profile', 'output', 'dataset_id'), 'model_ref': ref('$inputs', 'model_ref')}})
ANALYSIS_PREDICTION_WORKFLOW['nodes'][-1]['title'] = '查看分析和预测结果'
ANALYSIS_PREDICTION_WORKFLOW['nodes'][-1]['config']['outputs']['逐条预测'] = ref('predict', 'output')
ANALYSIS_PREDICTION_WORKFLOW['edges'] = [
    {'id': f'{a}-{b}', 'source': a, 'target': b}
    for a, b in [('start', 'profile'), ('profile', 'outliers'), ('outliers', 'spearman'),
                 ('spearman', 'predict'), ('predict', 'end')]]

MODELING_SKILL = {'name': '项目数据与建模',
    'description': '数值整理、异常值、Spearman、分组训练及模型复用；含换表后重算分析并预测的可编辑流程示例。',
    'content': GUIDE, 'references': {
        'field-rules.json': json.dumps({'mapping': {'kind': 'tabular', 'target': 'quality', 'group_column': 'sample_id', 'invalid_rows': 'exclude', 'field_rules': {
            'height': {'dtype': 'number', 'description': '高度；区间和斜杠含义尚未明确', 'pending_tokens': ['/'], 'intervals': 'pending', 'allow_missing': ['unknown']},
            'pressure': {'dtype': 'number', 'allow_missing': ['unknown']},
            'quality': {'dtype': 'category', 'description': '仅为示例，必须替换为项目已说明的定义', 'categories': ['0', '1', '2'], 'missing_tokens': {'-1': 'unjudged'}}}}}, ensure_ascii=False, indent=2),
        'analysis-workflow.json': json.dumps(ANALYSIS_WORKFLOW, ensure_ascii=False, indent=2),
        'analysis-prediction-workflow.json': json.dumps(ANALYSIS_PREDICTION_WORKFLOW, ensure_ascii=False, indent=2)}}
