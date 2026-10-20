"""Editable general-purpose candidate comparison, extracted from engineering practice."""
from pathlib import Path

NAME='候选方案约束检查与比较'
DESCRIPTION='检查已有方案是否满足明确条件，再按优先级或明确权重比较，保留失败原因与并列；没有可行候选时不强行推荐。'
GUIDE='''已有多套工艺、排产、资源分配或实验候选，员工想了解哪些满足条件、怎样取舍时调用本流程。
输入一张项目CSV/Excel，每行一个候选，含唯一方案标识、数值指标及条件相关字段。可按任务/订单列分组，各组独立比较；不能拿不同任务的数值混排名。表单中的条件和目标可添加、删除、上下移动，不需要JSON。
逐组比较只适合独立候选集合；若不同任务争用同一库存、设备或时间段，不能把各组首位直接拼成总体排程，可用“候选按共享数量分配”检查共同数量，并明确处理顺序；设备配对与时序仍需领域流程。
条件表的field是待检查列；operator支持数值不大于/小于/不小于/大于/等于/不等于及文字等于/不等于。value（固定值）与other_field（同一行对比列）恰好填一个。多个条件全部满足才通过，未知或缺失不放行。实际是否安全仍取决于条件完整性与测量真实性。
先区分不可违反的条件和偏好：条件不能被评分补偿，也不在全部失败时恢复原候选。所有候选都不满足时输出原因和缺项，不选最高分冒充可行解。
目标表field为数值指标，direction选择越大或越小越好。默认按从上到下的优先级逐项比较，只有前项相同才比较下一项。需要允许指标互相补偿时，明确选择权重评分，填写正权重weight及每多少变化算1分的scale；按方向乘正负后求和，不自动从数据归一化。不清楚业务偏好时可以只检查条件，留空目标，不编造权重。
没有条件时允许保存运行，但报告明确只是排序、未检查限制。比较字段缺失时保留已完成检查但不排名。相同目标值或分数保持并列，不能把现有候选中的第一名说成全局最优。
outputs.result.source_path为全部候选与检查/排名CSV；另有逐条件实际值与原因、目标分项、配置版本和中文说明。原文件不改，每次运行固定输入和配置，修改后新运行不会覆盖旧结果。
先前模型或领域代码产生的候选指标可以传给本流程；本流程不训练、不调用预测模型、不生成候选、不回写生产。复杂尺寸公式、候选生成及稳定操作区间保留在领域代码中，客户阈值不作为通用默认。'''


def workflow():
    from .official_workflows import graph,node,ref
    fields=[('source_path','已有候选方案表','','file',True),('id_column','方案标识列','candidate_id','string',True),
            ('group_column','分组列（不同任务分别比较；可留空）','','string',False),('sheet','Excel工作表（单表可留空）','','string',False)]
    inputs=[dict(name=k,label=label,default=default,type=kind,required=required) for k,label,default,kind,required in fields]
    inputs.extend([
        dict(name='constraints',label='必须满足的条件',type='array',required=False,default=[],columns=[
            dict(name='field',label='要检查的列'),dict(name='operator',label='比较方式',options=['不大于','小于','不小于','大于','数值等于','数值不等于','文字等于','文字不等于'],default='不大于'),
            dict(name='value',label='固定值（与对比列二选一）'),dict(name='other_field',label='同一行的对比列（可留空）')]),
        dict(name='mode',label='怎样比较满足条件的方案',type='string',required=True,default='按优先级逐项比较',options=['按优先级逐项比较','按明确权重评分']),
        dict(name='objectives',label='比较目标（从上到下为优先顺序）',type='array',required=False,default=[],columns=[
            dict(name='field',label='数值指标列'),dict(name='direction',label='比较方向',options=['越大越好','越小越好'],default='越大越好'),
            dict(name='weight',label='权重（仅评分方式使用）',type='number',default=1),dict(name='scale',label='每多少变化算1分（仅评分使用）',type='number',default=1)])])
    code=Path(__file__).with_name('candidate_comparison_code.py').read_text()
    return graph([node('start','start','选择方案与比较条件',inputs=inputs),
        node('prepare','code','检查字段与保存本次输入',code=code,timeout=120,inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in inputs}}),
        node('compare','code','检查硬条件并比较候选',code=code,timeout=120,inputs={'operation':'compare','prepared':ref('prepare','output')}),
        node('end','end','可选方案、原因与取舍说明',outputs={'result':ref('compare','output'),'markdown':ref('compare','output','markdown')})])


def example_defaults():
    return dict(group_column='case_id',constraints=[dict(field='used',operator='不大于',value='',other_field='available'),
        dict(field='pairable',operator='文字等于',value='可配',other_field='')],
        objectives=[dict(field='output',direction='越大越好',weight=1,scale=1),dict(field='waste',direction='越小越好',weight=1,scale=1)])


def example_files():
    return {
        'candidates.csv':'candidate_id,case_id,used,available,pairable,output,waste\nA,任务一,100,100,可配,4,10\nB,任务一,110,100,可配,9,0\nC,任务一,90,100,可配,4,5\nD,任务一,90,100,可配,4,5\nE,任务一,80,100,,8,0\nF,任务二,80,100,不可配,10,0\n',
        'candidates-2.csv':'candidate_id,case_id,used,available,pairable,output,waste\nA,任务一,100,100,可配,4,10\nB,任务一,99,100,可配,9,0\nC,任务一,90,100,可配,4,5\nD,任务一,90,100,可配,4,5\nE,任务一,80,100,可配,8,0\nF,任务二,80,100,可配,10,0\n',
        '字段与练习说明.txt':'全部为自编候选，不是客户设备数据。used表示资源使用，available为可用上限；output为产出，waste为损失，pairable为人工提供的配对检查结果。初次应排除资源超限的B、缺配对数据的E和不可配的F；任务一C/D并列优先于A，任务二没有满足条件的候选。换第二份后B在任务一首位，F在任务二可参与比较。可增加waste不大于6的条件，A应不再进入比较；删除条件可恢复原口径。评分练习务必先明确单位尺度及权重，候选第一不等于全局最优。'}
