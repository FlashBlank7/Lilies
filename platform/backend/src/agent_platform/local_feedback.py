"""Generic editable local feedback recipe; no production constants or model calls."""
from pathlib import Path

NAME = '实测反馈修正与分段建议'
DESCRIPTION = '用顺序明确的实测观测调整附近风险分数，比较保留/排除分段与代价；不重训、不回写现场。'
GUIDE = '''已有同一对象的一维等间距风险曲线，以及后来获得的高/低风险观测，希望看看邻域修正如何影响分段时调用。只想比较预测和实测误差时，用“预测与实测反馈对照”；本流程不替代模型训练或效果评价。
原表需要坐标和0至1的风险分数（越大表示越有风险）；坐标严格递增、无重复，宽度由员工明确填写，不补网格缺口。坐标为等宽单元中心，覆盖到首尾中心外各半格。观测表需observation_id、坐标及0/1结论；1表示高风险，0表示低风险，未知结论不能编造。字段可在表单选择名称；不同坐标单位须先统一。一次处理一个对象，不能把多个批次混成一根曲线。
观测按文件行顺序应用，高斯权重=exp(-距离²/(2×影响宽度²))×修正力度；新分数=旧分数+权重×(观测-旧分数)。最后按明确上下限裁剪；没有观测就不改分数。相反观测顺序会影响结果，这是经验平滑修正，不是贝叶斯后验或校准概率。观测ID重复报错；同坐标不同观测保留顺序。再次运行从原始曲线及本次完整观测表计算，不应再把同一观测叠加到已修正曲线。
分段有保留、排除两个状态。每格保留代价=格宽×保留风险单位代价×风险分数；排除代价=格宽×排除单位代价，相邻状态变化加切换代价。代价需使用一致的量纲，来自任务资料；示例数值不能成为客户默认。最小化仅针对声明的离散目标，不含切缝、最短段长、设备、总需求、已切除区域和现场时序约束。输出内部边界是分析建议，不是可直接下发的生产指令。
输出逐点前后分数与动作、分段、观测应用顺序、原输入快照和中文报告。报告同时把旧方案放到新分数上计价，避免把分数变化误称为实际收益。影响宽度、力度与代价应据业务验证；反馈样本不能冒充独立测试。
修改观测、力度或代价后新开运行，旧结果保持；不会重训、调用预测模型、调整原模型或回写现场。每次最多10000个网格点和200条观测，资料超过范围请明确分批，不静默截断。'''


def workflow():
    from .official_workflows import graph, node, ref
    fields = [
        ('source_path','原始风险曲线（同一对象，CSV／Excel）','','file',True),
        ('observations_path','观测表（可留空，对照原分段）','','file',False),
        ('position_column','两表坐标列','position','string',True),
        ('score_column','原风险分数列（0至1）','score','string',True),
        ('observed_column','观测结论列（高风险1、低风险0）','high_risk','string',True),
        ('unit','两表共同的坐标单位','','string',True),
        ('grid_step','等间距网格宽度（坐标是每格中心）','','number',True),
        ('influence_width','观测影响宽度（与坐标同单位）','','number',True),
        ('strength','观测修正力度（0至1）','','number',True),
        ('keep_cost','保留风险单位代价（需有业务依据）','','number',True),
        ('discard_cost','排除单位代价（与保留同口径）','','number',True),
        ('switch_cost','相邻状态切换代价（明确无代价才填0）','','number',True),
        ('score_floor','修正分数下限（仅有观测时裁剪）',0,'number',True),
        ('score_ceiling','修正分数上限',1,'number',True),
        ('curve_sheet','曲线Excel工作表（单表留空）','','string',False),
        ('observations_sheet','观测Excel工作表（单表留空）','','string',False),
    ]
    inputs = [dict(name=n,label=l,default=d,type=t,required=r) for n,l,d,t,r in fields]
    code = Path(__file__).with_name('local_feedback_code.py').read_text()
    return graph([
        node('start','start','选择原曲线、观测和代价',inputs=inputs),
        node('prepare','code','检查网格和观测并保存本次输入',code=code,timeout=120,
             inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in inputs}}),
        node('evaluate','code','依序修正、计算分段并解释变化',code=code,timeout=120,
             inputs={'operation':'evaluate','prepared':ref('prepare','output')}),
        node('end','end','前后结果及使用限制',outputs={'result':ref('evaluate','output'),'markdown':ref('evaluate','output','markdown')})])


def example_defaults():
    return {'source_path':'@file:原风险.csv','observations_path':'@file:观测.csv','unit':'mm','grid_step':10,
            'influence_width':15,'strength':0.8,'keep_cost':3,'discard_cost':1,'switch_cost':5}


def example_files():
    return {
        '原风险.csv':'position,score,unit\n5,0.1,mm\n15,0.1,mm\n25,0.1,mm\n35,0.1,mm\n45,0.1,mm\n55,0.1,mm\n65,0.1,mm\n75,0.1,mm\n85,0.1,mm\n',
        '观测.csv':'observation_id,position,high_risk,unit\n反馈一,45,1,mm\n',
        '补充观测.csv':'observation_id,position,high_risk,unit\n反馈一,45,1,mm\n反馈二,45,0,mm\n',
        '反序观测.csv':'observation_id,position,high_risk,unit\n反馈二,45,0,mm\n反馈一,45,1,mm\n',
        '字段与练习说明.txt':'全部为自编教学值，不代表客户工艺参数。9格覆盖0至90 mm；原风险均0.1，中心45处高风险反馈力度0.8，中心更新为0.82。再追加同位置低风险反馈为0.164；反转顺序则为0.804，说明这不是顺序无关的贝叶斯后验。先核对单个数值，再看状态边界；保留风险代价3、排除1、切换5也仅为教学。改切换代价观察分段减少但不要据此推断生产效果。填宽度0应明确报错，修正后重跑，原结果保留。观测留空可以只计算原分段。'}
