"""Share quantity limits across already ranked alternatives without scheduling runtime."""
from pathlib import Path

NAME='候选按共享数量分配'
DESCRIPTION='按明确顺序从各组已有候选中选取方案，逐次扣减共同数量，保留不足与未选原因；不修改实际库存。'
GUIDE='''已有分组候选及比较顺位，需要检查跨组共同数量时调用。现有“候选方案约束检查与比较”各组独立排序，不能直接把首位拼起来；此流程选择每组至多一个候选并共同扣减数量。员工仍决定是否采用结果。
需要三张CSV/TSV/Excel：候选表含candidate_id、group_id、comparison_rank；用量表含candidate_id、resource_id、quantity，每个方案/资源一行；上限表含resource_id、capacity，每个资源一行。字段可通过表单修改。同一资源必须同单位；数量可为库存，也可为剩余需求上限。缺少资源上限、重复标识/用量和无明确用量的方案会报错，不猜测为零。明确无用量也填写0。
分组处理默认按候选表第一次出现的顺序；可指定候选表中的数值列，并选择从小到大或从大到小。同一组该值必须相同，并列按首次出现顺序。每组按正整数顺位从小到大，取第一个满足当前剩余量的方案，再扣减共用数量；并列顺位按原候选行顺序。只比较总量，不能表达时间槽和先后工序。
comparison_status列如果存在，fail和unknown不选，即使有人填了顺位也不会放行；无顺位的方案也不选。unchecked或没有该状态列只表示按给定顺位分配，未检查其他限制。这里只重新核对数量；不能把输入排名当已经完成全部工艺验证。
与下料流程衔接：先生成候选，再在比较流程按stock_id分组并明确目标；把比较后的candidates.csv传为候选表，原生成的patterns.csv传为用量表，原需求表传为上限表。分组列stock_id，资源列demand_id，上限列quantity，用量列quantity；方案标识candidate_id和顺位comparison_rank保持。选stock_length从大到小可按长料先处理。三份文件须来自同一批候选和需求。复用原需求数量作为共同上限，不将各根独立上限累计。
这是顺序贪心，不回溯、重排或重新生成候选，顺序会影响满足的组数与资源剩余，不能称全局最优；全部失败也不等于业务无解。原工程的设备、锁定、短料配对及现场接口仍是领域实现，没有进入公共默认。
产物包括每组已选方案、所有候选原因、逐次用量及共同余额、输入快照与说明。更改上限或顺序另开运行，原候选与旧结果不变；不重训、不预测、不预留库存、不回写现场。'''


def workflow():
    from .official_workflows import graph,node,ref
    fields=[
        ('source_path','已比较的候选表','','file',True),('usage_path','每个候选的资源用量表','','file',True),('limits_path','共同数量上限表','','file',True),
        ('id_column','候选及用量表的方案标识列','candidate_id','string',True),('group_column','每组最多选一个的分组列','group_id','string',True),
        ('rank_column','组内顺位列（正整数，较小优先）','comparison_rank','string',True),
        ('resource_column','用量及上限表的共同资源标识列','resource_id','string',True),('quantity_column','用量表的数量列','quantity','string',True),
        ('capacity_column','上限表的数量列','capacity','string',True),('group_order_column','分组处理数值列（留空按首次出现）','','string',False),
        ('candidates_sheet','候选Excel工作表（单表留空）','','string',False),('usage_sheet','用量Excel工作表（单表留空）','','string',False),('limits_sheet','上限Excel工作表（单表留空）','','string',False)]
    inputs=[dict(name=n,label=l,default=d,type=t,required=r) for n,l,d,t,r in fields]
    inputs.append(dict(name='group_order_direction',label='分组数值的处理方向（仅指定数值列时）',type='string',required=False,default='从小到大',options=['从小到大','从大到小']))
    code=Path(__file__).with_name('candidate_allocation_code.py').read_text()
    return graph([node('start','start','选择候选、用量和共同数量',inputs=inputs),
        node('prepare','code','核对引用及保存本次输入',code=code,timeout=120,inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in inputs}}),
        node('allocate','code','按顺序选择并扣减共同数量',code=code,timeout=120,inputs={'operation':'allocate','prepared':ref('prepare','output')}),
        node('end','end','已选方案、未选原因和剩余数量',outputs={'result':ref('allocate','output'),'markdown':ref('allocate','output','markdown')})])


def example_files():
    return {
        '候选.csv':'candidate_id,group_id,comparison_rank,group_priority,comparison_status\nA优先,任务A,1,1,pass\nA节约,任务A,2,1,pass\nB唯一,任务B,1,2,pass\nC失败,任务C,1,3,fail\n',
        '用量.csv':'candidate_id,resource_id,quantity\nA优先,共同份额,2\nA节约,共同份额,1\nB唯一,共同份额,1\nC失败,共同份额,1\n',
        '上限.csv':'resource_id,capacity\n共同份额,2\n',
        '上限-增加.csv':'resource_id,capacity\n共同份额,3\n',
        '字段与练习说明.txt':'自编数量与顺位。默认任务A先选A优先，用尽2份，B无可选，C前序失败不会选择。改group_priority从大到小，C仍失败，B先用1份，A优先不足转用A节约，因此A/B都能选。说明默认贪心不是满足组数最多的全局解。换上限-增加.csv，默认顺序可同时选择A优先与B唯一。上限不能通过随意加数制造真实可行性，这些仅是操作练习。'}


def example_defaults():
    return {'source_path':'@file:候选.csv','usage_path':'@file:用量.csv','limits_path':'@file:上限.csv','group_order_column':'group_priority'}
