"""Single-stock cutting patterns, separate from shared-resource scheduling."""
from pathlib import Path

NAME = '按物料与需求生成下料候选'
DESCRIPTION = '按定长或长度范围需求、可用物料和切缝生成单料组合，列出各段实际长度与余量，供已有方案比较流程继续使用。'
GUIDE = '''员工有物料表与定长或范围需求，希望先得到可检查的切割组合时调用。本流程枚举每根物料的候选，不选择生产排程。
输入项目CSV/TSV/Excel：物料表stock_id、material、length；定长需求表demand_id、material、length、quantity。选择“长度范围”时需求表改用min_length、max_length替代length，固定尺寸可填写相同上下限。长度使用表单指定的同一单位，quantity为剩余需求整数。标识必须唯一，material必须明确且相同才能组合；有unit列时必须与表单单位完全一致，不自动转换单位。字段名称不同可先用已有表格整理流程映射。
范围长度提供两种明确策略：“按区间比例分配”在下限和上限间按相同比例扩展；“先中点再按需求表顺序分配”在不足中点时按比例，足够中点后优先满足表中靠前的需求，同一需求各段等长。先保证上下限和损耗，再按所选小数位向下取整；下限向上、上限向下收紧到该精度，无可用尺寸时明确报错。默认6位小数仅为计算设置，不代表设备精度。不得用上调尺寸精度掩盖真实设备限制。每个数量组合只给一种长度分配，不枚举所有连续尺寸；如果只允许离散尺寸，应另行建模。改变需求表顺序可能改变中点优先分配结果，选择方法前应说明偏好。
填写实际单次切缝宽度、总端部预留长度和切缝计数方式。每段各计一道表示n段需要n道切缝；仅相邻段计缝表示n段需要n-1道，只适合末段不再从余料分离的场景，不能默认套用。物料length是预留前长度，表单预留只扣一次。不了解损耗时先补资料，不把默认0当生产事实。
每根物料的组合不超过各项剩余需求、所设段数和需求种类上限；长度使用精确十进制，不向上四舍五入。枚举数量组合，不枚举同一组合的排列；切割顺序、缺陷分区、设备尺寸、短料配对不在本通用版本中。原项目这些专有处理可放在领域代码里。
输出每根物料的候选CSV、需求数量及实际长度明细、无候选原因、配置与来源快照和说明。余料长度和锯缝损耗分开；余料能否复用需要后续判断，不直接标为废料。搜索达到所设上限会明确失败并保留输入，缩小物料范围、段数或需求种类后再跑，不交付截断列表冒称完整。
outputs.result.comparison_inputs可以交给项目内“候选方案约束检查与比较”，按stock_id分组；默认不编造业务偏好，员工可再指定产出、余量等比较目标。生成候选不会自动运行下一流程。没有候选时先查看原因，不调用空表比较。
不同物料的候选都分别使用同一份剩余需求上限，不能把各根首选直接合并；需要额外分配逻辑核对全局需求、库存、设备和时间。这里不证明全局最优、不消耗库存、不回写现场。改变输入或参数新开运行，旧报告保留；改比较偏好只调用已有比较流程，不需重新生成。'''


def workflow():
    from .official_workflows import graph, node, ref
    inputs = [dict(name=n,label=l,default=d,type=t,required=r) for n,l,d,t,r in [
        ('stock_path','物料表（stock_id、material、length）','','file',True),
        ('demand_path','需求表（定长或长度范围，见字段说明）','','file',True),
        ('stock_sheet','物料Excel工作表（单表留空）','','string',False),
        ('demand_sheet','需求Excel工作表（单表留空）','','string',False),
        ('unit','两张表共同的长度单位','mm','string',True),
        ('kerf','实际单次切缝宽度（明确无损耗才填0）','','number',True),
        ('end_allowance','每根物料共预留长度（两端合计）',0,'number',True),
        ('max_pieces','每根最多产出段数（1至12）',6,'number',True),
        ('max_types','每根最多需求种类（1至6）',3,'number',True),
        ('search_limit','最多检查组合分支（100至200000）',20000,'number',True),
        ('length_precision','范围长度最多小数位（0至6，按实际设备填写）',6,'number',False),
    ]]
    inputs.append(dict(name='kerf_mode',label='实际切缝计数方式',type='string',required=True,
                       default='每段各计一道切缝',options=['每段各计一道切缝','仅相邻段间计切缝']))
    inputs.extend([
        dict(name='length_mode',label='需求长度方式',type='string',required=True,default='固定长度',options=['固定长度','长度范围']),
        dict(name='allocation_mode',label='范围内长度分配方式（仅范围需求生效）',type='string',required=False,default='按区间比例分配',
             options=['按区间比例分配','先中点再按需求表顺序分配']),
    ])
    code=Path(__file__).with_name('cutting_candidates_code.py').read_text()
    return graph([
        node('start','start','选择物料、需求和损耗口径',inputs=inputs),
        node('prepare','code','核对数据并保存本次输入',code=code,timeout=120,
             inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in inputs}}),
        node('generate','code','枚举满足声明条件的单料组合',code=code,timeout=120,
             inputs={'operation':'generate','prepared':ref('prepare','output')}),
        node('end','end','候选、余量与后续比较输入',outputs={'result':ref('generate','output'),'markdown':ref('generate','output','markdown')})])


def example_files():
    return {
        '物料.csv':'stock_id,material,length,unit\n料一,类型A,100,mm\n料二,类型A,61,mm\n料三,类型B,80,mm\n料四,类型C,100,mm\n',
        '需求.csv':'demand_id,material,length,quantity,unit\n需求一,类型A,30,2,mm\n需求二,类型A,45,1,mm\n需求三,类型B,90,1,mm\n',
        '需求-变更.csv':'demand_id,material,length,quantity,unit\n需求一,类型A,30,1,mm\n需求二,类型A,45,2,mm\n需求三,类型B,70,1,mm\n',
        '需求-范围.csv':'demand_id,material,min_length,max_length,quantity,unit\n需求一,类型A,30,45,2,mm\n需求二,类型A,40,55,1,mm\n需求三,类型B,70,80,1,mm\n',
        '范围长度练习.txt':'自编练习：选择需求-范围.csv，将需求长度方式改为长度范围，小数位填1。物料表与切缝保持原样。料一同时产出需求一和需求二各一段时，可分配产品长度98；比例分配为44和54，先中点再按表顺序为45和53。查看patterns.csv核对上下限及实际长度。两种方法均只给每个数量组合一种方案，不代表业务最优。把范围下限改得高于上限会具体报错；修正后重新运行，旧报告不变。真实可用尺寸与设备精度需要由任务资料提供。',
        '字段与练习说明.txt':'自编尺寸，不是客户物料或生产参数。每段计1 mm切缝、预留0、最多3段时：料一有4个组合，料二有2个，料三长度不足，料四没有同类型需求。将切缝改0，料二可增加两段30的组合；这只是损耗敏感性练习，不能把真实切缝改0来制造可行方案。换需求-变更.csv重新计算，料三得到候选，原结果保留。项目里附比较流程：使用刚生成的candidates.csv，按stock_id分组，自行明确目标后再比较。各根候选不能直接拼成排程，因为可能重复满足同一需求。'}
