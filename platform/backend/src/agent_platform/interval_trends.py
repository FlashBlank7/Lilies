"""Reusable mean-comparison labels from a documented forecasting method."""
from pathlib import Path

NAME = '区间均值趋势与样本制备'
DESCRIPTION = '将历史数值按未来区间构造上涨、平稳、下跌标签；保留当时可知特征、公布时刻和每个标签的原记录。'
GUIDE = '''适用于已经有日/月/季数值，但需要把“预测未来一段时间的趋势”定义为明确样本的任务。先查看工作流输入，再选择项目表格；不要求先训练。
每个对象每周期一条完整周期数值；原始高频记录先按业务含义汇总。可选实际可用时间列，同周期不同可用时刻代表修订，同周期同可用时刻重复报错。日/月/季均按UTC日历，季度从一月开始。起点是所选周期结束的下一边界，不是下一行；缺周期不压缩时间。
历史窗口的值只使用起点当时已公布的最新版本。未来各区间使用文件中最新实测版本构造标签：首段均值与历史窗口均值比较，后续段与前段比较。数值差严格大于平稳阈值算上涨，小于负阈值算下跌，恰等于阈值算平稳。阈值单位与原值相同，由员工明确配置，不自动调到最好成绩。
这是方案中的确定性样本方法，不是已经验证过的长期预测模型。samples.csv包含历史滞后特征、区间编号及target；label-origins.csv另存未来均值、变化和原记录号，禁止将这些未来字段作为特征。缺失、无效、起点时尚未公布的历史，或不完整的未来窗口，在excluded.csv解释，不填0或擅自缩短窗口。没有未来实测的最后起点输入单独保存，可用于之后的模型预测。
后续可用现有分类训练能力，显式选择输出features及mapping：target、sample_id、origin_time、label_available_time。按时间划分并排除验证起点尚未知标签，不能随机打散重叠窗口；interval是预测第几个未来区间，并非未来观测。series是对象标识，不默认当泛化特征。普通分类模板的随机划分默认值不适用于本数据，必须修改划分配置后才可用。
换原表、窗口或阈值会新建独立结果，不覆盖旧报告或调用任何模型。练习：把阈值从0.5改为1，查看平稳标签及排除数变化；从label-origins.csv抽一行，按原记录手工算两个均值。建议只准备下一步操作，不自动训练。'''


def workflow():
    from .official_workflows import graph, node, ref
    fields = [
        dict(name='source_path', label='历史周期数值表', type='file', required=True, default=''),
        dict(name='sheet', label='Excel工作表（单表可留空）', type='string', default=''),
        dict(name='period_column', label='周期列', type='string', default='period'),
        dict(name='value_column', label='数值列', type='string', default='value'),
        dict(name='series_column', label='对象列（可留空）', type='string', default=''),
        dict(name='available_column', label='实际可用时刻列（留空假定周期结束可用）', type='string', default=''),
        dict(name='frequency', label='原表周期', type='string', default='月', options=['日', '月', '季']),
        dict(name='history_periods', label='历史参考均值使用几个周期', type='number', default=3),
        dict(name='window_periods', label='每个未来区间包含几个周期', type='number', default=1),
        dict(name='windows', label='生成几个未来区间的标签', type='number', default=3),
        dict(name='threshold', label='平稳阈值（原数值单位；边界算平稳）', type='number', default=0),
        dict(name='origin_stride', label='相邻样本起点相隔几个周期', type='number', default=1),
        dict(name='first_origin', label='首个起点周期（可留空，例如2025-03）', type='string', default=''),
        dict(name='last_origin', label='最后起点周期（可留空）', type='string', default=''),
    ]
    for f in fields:
        f.setdefault('required', False)
    return graph([
        node('start', 'start', '选择历史资料与趋势含义', inputs=fields),
        node('prepare', 'code', '按当时可知历史构造区间标签',
             code=Path(__file__).with_name('interval_trends_code.py').read_text(), timeout=120,
             inputs={f['name']: ref('start', f['name']) for f in fields}),
        node('end', 'end', '样本、标签出处与使用说明',
             outputs={'result': ref('prepare', 'output'), 'markdown': ref('prepare', 'output', 'markdown')})])


def example_files():
    import math
    lines = ['series,period,value']
    for i in range(36):
        p = f'{2023 + i // 12}-{i % 12 + 1:02d}'
        lines.append(f'自编序列,{p},{10 + i * .08 + math.sin(i * math.pi / 4):.4f}')
    return {'history.csv': '\n'.join(lines) + '\n',
            'history-gap.csv': '\n'.join(line for line in lines if ',2024-06,' not in line) + '\n',
            '说明.txt': '自编月度数值，不是客户数据。三个月历史参考、每段两个月、三段趋势。先看标签出处，再将平稳阈值0.5改为1比较；换history-gap.csv观察缺少2024-06的影响，不能把下一行当下一月，末尾不完整未来区间不补0。后续训练必须使用时间划分及标签可用时刻。'}


def example_defaults():
    return dict(source_path='@file:history.csv', series_column='series', threshold=0.5, window_periods=2)


def training_workflow():
    """Configure the existing trainer for the sample contract, not a new engine."""
    from .official_workflows import training
    workflow = training('classification')
    for node in workflow['nodes']:
        if node['id'] == 'start':
            node['config']['inputs'] = [dict(name='source_path', label='趋势样本制备产生的 samples.csv',
                                             type='file', required=True, default='')]
        elif node['id'] == 'profile':
            node['config']['mapping'] = dict(target='target', id_column='sample_id',
                prediction_time_column='origin_time', label_available_time_column='label_available_time')
        elif node['id'] == 'features':
            node['config']['features']['exclude'] = ['series']
        elif node['id'] == 'train':
            node['config']['evaluation'].update(split='time', folds=2)
            node['config']['budget'].update(seconds=180, trials=2, trial_seconds=60)
            node['config']['candidate'].update(models=['linear', 'forest'], batch_size=2)
    return workflow


def prediction_workflow():
    from .official_workflows import prediction
    workflow = prediction()
    workflow['nodes'][0]['config']['inputs'] = [dict(name='source_path', type='file', required=True,
        label='趋势样本制备产生的 prediction-inputs.csv', default='')]
    workflow['nodes'][1]['config']['model_ref'] = 'interval-trend'
    return workflow
