"""Rolling forecasting origins assembled from the existing training and prediction blocks."""
from pathlib import Path

NAME='多步预测与滚动回测'
DESCRIPTION='逐个历史起点只用当时可知数据训练，预测后续多个时段，与最近值和季节基线比较；保留每步误差、版本及缺项。'
GUIDE='''已有按时间记录的数值，想知道未来几个时段能否预测、效果是否超过简单方法时调用。CSV/TSV/XLSX含时间、数值，可有序列标识和实际可用时间。负值和零有效，不把价格或功率强制截成非负。
按表单指定实际时间间隔（秒），步长是这个间隔的倍数，不是下一行；缺时段不压缩。无时区的时间按指定UTC偏移解释。有实际发布日期/修订时填写可用时间；留空明确假定记录时刻即可使用，不能据此验证现实发布时间。相同观测时刻可提供多个不同可用时刻的版本；同观测、同可用时刻重复则需先澄清。多频率外部指标另用时点制备流程，首版只预测各数值序列自身。
每个起点独立调用现有训练和预测积木。训练输入是过去连续几个时段的滞后值与预测步长；同一个回归模型共享多个步长，不递归使用未来真实值。缺少部分滞后值交给训练折内填补，所有滞后都缺失的样本排除；预测起点全部缺失时报错。历史特征固定为该样本起点当时可知版本，训练标签只使用当前回测起点已可知的版本。内部模型比较也按时间划分并排除尚未可知标签。
默认少量起点、线性模型；可比较线性与随机森林，依据每个起点内部验证选择，后续实际值不参与挑选。历史窗口0表示逐步扩大，正数表示仅用最近指定数量时段的样本起点。首个回测起点留空时用最后完整预测窗口往前取指定数量起点；报告明确覆盖范围，不把这些窗口说成整个历史。
最后时点预测是可选操作，单独训练并预测输入序列最后记录时点之后的数据；这不是保证最新生产时点，也不自动部署模型。该部分没有实际值时不计入评价。
先看按序列、步长分别计算的MAE/RMSE及方向正确率；同一目标时刻可能在多个起点被预测，按预测决策分别保留，不去重当成独立样本。最近值基线沿用起点最新可知观测，季节基线只使用起点前相同周期位置的观测，找不到就标缺项。不同方法在共同可评价记录上比较，缺项数量另列，不能靠少算困难记录获胜。
方向相对起点最新可知值，容差为原数值单位。可选上下边界由员工填写，输出触及边界的精确率/召回率；没有真实事件时召回率不可计算。每个起点还给未来各步的秩相关（平均秩、常量不可计算），与绝对误差分别理解，不把它们等同业务收益。
输出完整预测/实际/基线CSV、逐组逐步指标、各起点指标和特征/标签来源记录。训练模型、折内预处理及计算环境保留在现有项目建模记录，每个预测固定study/candidate/slot版本。原运行与文件不改，修改输入/参数后建立新运行。停止和恢复沿用项目任务与迭代积木，不新增阶段审批。
来源项目只有需求和算法方案，没有原价格训练集，公共示例是构造序列。流程验证不证明客户生产效果，也不实现报价回写。VMD/GRU融合、复杂外部特征、概率区间及长期区间趋势分类仍须另行验证，不用算法名称冒充已实现能力。'''


def workflow():
    from .official_workflows import graph,node,ref
    fields=[
        dict(name='source_path',label='历史数值表',type='file',required=True,default=''),
        dict(name='sheet',label='Excel工作表（单表可留空）',type='string',default=''),
        dict(name='time_column',label='观测时刻列',type='string',required=True,default='time'),
        dict(name='value_column',label='要预测的数值列',type='string',required=True,default='value'),
        dict(name='series_column',label='序列标识列（不同对象分别训练；可留空）',type='string',default=''),
        dict(name='available_column',label='实际可用时间列（留空假定观测时刻即可使用）',type='string',default=''),
        dict(name='timezone',label='无时区时间的UTC偏移，例如 +08:00',type='string',default='+00:00'),
        dict(name='step_seconds',label='一个时段的秒数（日86400，小时3600，15分钟900）',type='number',default=86400),
        dict(name='lags',label='使用此前多少个时段（包含起点时段）',type='number',default=7),
        dict(name='horizon',label='每次预测后续几个时段',type='number',default=3),
        dict(name='origins',label='历史回测起点数量',type='number',default=3),
        dict(name='origin_stride',label='回测起点之间相隔几个时段',type='number',default=3),
        dict(name='first_origin',label='首个回测起点（ISO时间；留空从最后完整窗口往前取）',type='string',default=''),
        dict(name='training_window',label='训练历史窗口（时段数；0为逐步扩大）',type='number',default=0),
        dict(name='seasonal_period',label='季节基线周期（时段数；按业务周期填写）',type='number',default=7),
        dict(name='model_choice',label='比较哪些模型',type='string',default='仅线性回归',options=['仅线性回归','线性与随机森林比较']),
        dict(name='forecast_last',label='另行预测文件最后时点之后的数据',type='boolean',default=False),
        dict(name='direction_tolerance',label='方向判断的平稳容差（数值单位）',type='number',default=0),
        dict(name='lower_boundary',label='低边界（可留空，不套用其他业务阈值）',type='number',default=None),
        dict(name='upper_boundary',label='高边界（可留空）',type='number',default=None),
    ]
    for f in fields:f.setdefault('required',False)
    code=Path(__file__).with_name('rolling_forecast_code.py').read_text()
    inner=graph([
        node('begin','start','本起点输入',inputs=[dict(name='job',type='object',required=True),dict(name='prepared',type='object',required=True)]),
        node('window','code','仅制备起点当时可知的样本',code=code,timeout=120,reuse_completed=True,inputs={'operation':'window','prepared':ref('begin','prepared'),'job':ref('begin','job')}),
        node('data','data_analysis','登记本起点训练数据',source_path=ref('window','output','train_path'),mapping={
            'target':'target','id_column':'sample_id','prediction_time_column':'origin_time','label_available_time_column':'label_available_time','error_group_columns':['horizon']}),
        node('train','model_train','在历史数据内训练和比较',dataset_id=ref('data','output','dataset_id'),
            features={'columns':ref('window','output','features')},evaluation={'problem':'regression','metric':'mae','split':'time','folds':2,'holdout_fraction':0},
            candidate=ref('window','output','candidate'),budget={'seconds':240,'trials':2,'trial_seconds':60}),
        node('predict','model_predict','用本起点模型预测后续时段',source_path=ref('window','output','predict_path'),
            study_id=ref('train','output','study_id'),candidate_id=ref('train','output','id')),
        node('collect','code','关联实际值与简单基线',code=code,reuse_completed=True,inputs={'operation':'collect','window':ref('window','output'),'prediction':ref('predict','output'),'training':ref('train','output')}),
        node('done','end','本起点预测结果',outputs={'result':ref('collect','output')})])
    return graph([node('start','start','选择数据与预测口径',inputs=fields),
        node('prepare','code','检查时间轴与固定输入版本',code=code,timeout=120,reuse_completed=True,inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in fields}}),
        node('origins','iteration','逐起点训练与预测',items=ref('prepare','output','jobs'),variables={'prepared':ref('prepare','output')},item_name='job',
             workflow=inner,output_node_id='done',output_path=['result'],parallelism=1,reuse_completed=True),
        node('report','code','按步长比较误差与下一步',code=code,timeout=120,inputs={'operation':'report','prepared':ref('prepare','output'),'results':ref('origins','items')}),
        node('end','end','预测与回测报告',outputs={'result':ref('report','output'),'markdown':ref('report','output','markdown')})])


def example_defaults():return {'source_path':'@file:history.csv','series_column':'series','available_column':'available','lower_boundary':0,'upper_boundary':18}


def example_files():
    import csv,io,math
    from datetime import datetime,timedelta,timezone
    buffer=io.StringIO();writer=csv.writer(buffer);writer.writerow(['series','time','available','value'])
    start=datetime(2025,1,1,tzinfo=timezone.utc)
    for i in range(90):
        t=start+timedelta(days=i);value=8+i*.08+4*math.sin(2*math.pi*i/7)+(i%3-1)*.15
        writer.writerow(['示例序列',t.isoformat(),t.isoformat(),round(value,4)])
    original=buffer.getvalue()
    altered=original.replace('2025-03-31T00:00:00+00:00,2025-03-31T00:00:00+00:00,','2025-03-31T00:00:00+00:00,2025-04-02T00:00:00+00:00,')
    return {'history.csv':original,'history-late.csv':altered,
        '字段与练习说明.txt':'这是自编日序列，不是生产数据。value含趋势、每7日变化及小扰动；available为实际可用时间。默认回测3个起点、每次3步，比较模型与最近值/季节基线；阈值0和18仅演示。把horizon改为5、或仅用最近30时段训练，比较各步误差。换history-late.csv后最后观测迟到两天，启用最后时点预测时不得把它提前作为特征。尚无未来真实值的预测不计入评价。'}
