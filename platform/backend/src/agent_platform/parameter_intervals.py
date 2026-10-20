"""Editable analysis of already computed or measured one-dimensional scans."""
from copy import deepcopy
from pathlib import Path

NAME = '参数扫描结果与连续区间'
DESCRIPTION = '从已有扫描表找出满足条件、分数接近最佳的连续采样区间，解释缺口、孤立高分和宽度不足；调整参数可重算，无需训练。'
GUIDE = '''已有一组数值参数扫描、实验或模型预测结果，员工想知道可选范围而不只看最高分点时调用。先 inspect 当前工作流确认输入；本流程不生成扫描、不调用预测模型、不训练或回写现场。
输入CSV/TSV/XLSX，每行一个参数点；指定数值参数列、分数列及越大/越小越好，可按不同样品或实验分组。同组相同数值参数只能有一条记录，重复测量应先按明确方法汇总，不能任取第一条。
条件行表复用候选比较：全部满足才进入区间分析，缺值和非法值待补充，不会当作0。没声明条件时只分析分数范围，报告明确未检查限制。分数含义、单位及模型/实际测量来源由员工提供，不把模型分数冒充真实安全性。
最大相邻间隔必须按扫描方案明确填写，单位同参数；不会从最小步长猜测允许跨过的缺口。中间任何不满足条件、条件未知、分数无效或未接近最佳的已知点都会断开区间，即使两侧距离很近。超过最大间隔也断开。
默认不平滑，可选择相邻3点或5点均值，仅在连续且条件可用的采样段内计算，边缘用现有点；不同参数间距仍按点数平均，不作物理加权。平滑后的分数只用于比较，原分数保留，不能由此认为单个点实际波动消失。
近最佳容差使用分数原单位，各组独立以可用点的最佳比较分为基准。设置最少点数（至少2）和最小宽度；孤立最高分不构成区间。可设置最大宽度，列出满足宽度上限的最大连续窗口，保留重叠和并列，不围绕最高分编造未采样端点。未满足最少点数/最小宽度的片段也保留原因。
先看各组是否有区间包含最佳点，再看稍低分但更宽的备选段；最佳点孤立时明确说明，不擅自替员工选一个备选。所有端点都是实际输入点，点之间未测位置不保证满足条件，不等于生产放行或全局最优。
outputs.result.source_path是逐点分析CSV；intervals_path为全部区间/片段，另有条件检查表、配置及报告。报告展示有限预览，完整表可下载。每次运行独立保存原文件内容标识和参数，修改容差、间隔或条件后重跑不会覆盖旧结果。结果可供后续实验或人工复核，建议不自动执行。
首版为单一数值参数分析，多参数相互作用需领域流程；不能把不同参数的一维最佳区间直接组合。客户工艺阈值及字段留在私有版本。'''


def workflow():
    from .official_workflows import graph, node, ref
    from . import candidate_comparison
    condition = deepcopy(next(f for f in candidate_comparison.workflow()['nodes'][0]['config']['inputs'] if f['name']=='constraints'))
    fields = [
        dict(name='source_path',label='已有参数扫描表',type='file',required=True,default=''),
        dict(name='sheet',label='Excel 工作表（单表可留空）',type='string',default=''),
        dict(name='parameter_column',label='数值参数列',type='string',required=True,default=''),
        dict(name='group_column',label='分组列（不同样品或实验分别分析；可留空）',type='string',default=''),
        dict(name='score_column',label='用于比较的分数列',type='string',required=True,default=''),
        dict(name='direction',label='分数的比较方向',type='string',default='越大越好',options=['越大越好','越小越好']),
        condition,
        dict(name='max_gap',label='允许的最大相邻间隔（参数单位；按扫描方案填写）',type='number',required=True,default=None),
        dict(name='tolerance',label='距离最佳分数的容差（分数单位；0表示只接受并列最佳）',type='number',default=0),
        dict(name='min_width',label='区间最小宽度（参数单位）',type='number',default=0),
        dict(name='min_points',label='区间最少采样点数（至少2）',type='number',default=2),
        dict(name='max_width',label='区间最大宽度（可留空；端点仍取已采样值）',type='number',default=None),
        dict(name='smoothing',label='怎样计算比较分数',type='string',default='不平滑',options=['不平滑','相邻3点均值','相邻5点均值']),
        dict(name='origin',label='扫描数值的来源',type='string',default='其他或不清楚',options=['模型计算结果','实际测量','自编示例','其他或不清楚']),
    ]
    for field in fields:field.setdefault('required',False)
    checks=Path(__file__).with_name('candidate_comparison_code.py').read_text()
    intervals=Path(__file__).with_name('parameter_intervals_code.py').read_text()
    return graph([node('start','start','选择扫描资料和区间要求',inputs=fields),
        node('prepare','code','保存扫描输入并核对字段',code=checks,timeout=120,inputs={'operation':'prepare',
            **{k:ref('start',k) for k in ['source_path','sheet','group_column','constraints']},'id_column':ref('start','parameter_column'),
            'mode':'按优先级逐项比较','objectives':[{'field':ref('start','score_column'),'direction':ref('start','direction')}]}),
        node('check','code','逐点检查明确条件',code=checks,timeout=120,inputs={'operation':'compare','prepared':ref('prepare','output')}),
        node('intervals','code','区分连续区间、缺口和孤立点',code=intervals,timeout=120,inputs={'checked':ref('check','output'),
            **{f['name']:ref('start',f['name']) for f in fields if f['name'] not in ['source_path','sheet','constraints']}}),
        node('end','end','区间依据与下一步说明',outputs={'result':ref('intervals','output'),'markdown':ref('intervals','output','markdown')})])


def example_defaults():
    return dict(parameter_column='setting',group_column='sample',score_column='score',max_gap=1,tolerance=0.02,min_width=1,
                origin='自编示例',constraints=[dict(field='check',operator='文字等于',value='通过',other_field='')])


def example_files():
    original='sample,setting,score,check\nA,1,0.98,通过\nA,2,1.00,通过\nA,3,0.99,通过\nA,4,1.10,不通过\nA,5,0.99,通过\nA,6,1.00,通过\nB,1,1.00,通过\nB,3,0.99,通过\nB,4,0.99,通过\nB,5,0.80,通过\nC,1,2.00,不通过\nC,2,1.00,\n'
    return {'scan.csv':original,'scan-2.csv':original.replace('A,4,1.10,不通过','A,4,1.00,通过').replace('B,3,0.99,通过','B,2,0.99,通过'),
        '字段与练习说明.txt':'自编参数与任意单位，不是客户工艺。sample代表独立实验，setting是数值参数，score越大越好，check是已知条件结果。默认A应有[1,3]和[5,6]两个区间，不能跨过不通过的4；B最佳点1孤立，但[3,4]可作为近最佳备选；C没有条件可用点。最小宽度改为2后只剩A的[1,3]符合宽度。换scan-2.csv后A可连为[1,6]，B的1和2可连接但2到4仍有缺口。最大宽度填写2应得到端点来自输入的多个窗口。没有训练或预测调用。'}
