"""An editable feedback recipe, independent of model training and prediction."""
from pathlib import Path

NAME = '预测与实测反馈对照'
DESCRIPTION = '关联已有预测和实测结果，查看未匹配记录、整体与分组误差，比较基线；不重训、不调用预测模型。'
GUIDE = '''当员工已经有预测结果及后来取得的实测值、复核标签时，调用本流程比较。只有原始数据但没有预测时，先按任务使用分析或预测流程；不猜测标签，也不自动训练。
选择预测表；现有预测流程的result.project_path可直接作为source_path，也可从项目结果文件选择。实测在同表时将实测表留空，否则选择第二份表。两表使用同名的一个或多个标识列（逗号或换行分隔）。重复或空标识报错，不按行号拼接、不自动去重。多模型或多版本对同一样本的预测，先选择一个版本，或把版本加入两表共同标识。
选择数值预测或类别判断；指定预测列、实测列；可按预测表中的设备、批次等列分组。数值字段不合法时保留该行并注明原因，不填0；类别按去除首尾空白的字符串精确比较，不猜测1与1.0是否相同。
未匹配、缺值和无效值不进入评价，但保留在逐条结果。每个指标显示评价样本数；实测表中没有对应预测的记录单独下载。
数值误差=预测-实测，正值表示预测偏高；展示平均绝对误差、均方根误差及偏差。只有明确业务容差时填写绝对误差容差（0表示必须完全相等）；留空不判合格。MAPE排除实测0，单列计算数量；常量实测的R²不定义。
类别评价展示准确率、宏平均F1、每类数量和混淆计数，少数类不能仅凭整体准确率判断可靠性。分组仅描述现有样本，不据此推断因果或生产稳定性。
可选基线来自预测表中的现成结果；模型与基线只在同一组双方有效的行上比较，类别集合取双方预测及实测并集，单独报告覆盖范围。各分组使用整体类别集合，缺类不改变宏平均口径。不根据实测数据事后选择阈值来冒充独立测试。
产物result.source_path是逐条对照CSV；还有分组CSV、未匹配实测CSV、类别明细（类别任务）、汇总JSON和中文报告。原始资料不变，每次运行保存独立结果。
员工可要求解释误差、换实测资料、改分组或容差重新运行。本流程不重训、不重新预测、不改模型或业务放行规则；下一步由用户任务决定。历史预测的训练/测试来源需另行核实，本次复盘不证明泛化表现。'''


def workflow():
    from .official_workflows import graph, node, ref
    fields = [
        ('source_path','预测结果表（CSV／Excel）','',True),
        ('actual_path','实测／复核结果表（与预测同表则留空）','',False),
        ('key_columns','两表同名的样本标识列（逗号或换行分隔）','sample_id',True),
        ('prediction_column','预测结果列','prediction',True),
        ('actual_column','实测值／复核标签列','actual',True),
        ('group_columns','分组列（来自预测表；可留空）','',False),
        ('baseline_column','已有基线结果列（来自预测表；可留空）','',False),
        ('absolute_tolerance','数值绝对误差容差（留空不判定，0表示完全相等）','',False),
        ('prediction_sheet','预测Excel工作表（单表可留空）','',False),
        ('actual_sheet','实测Excel工作表（单表可留空）','',False),
    ]
    inputs = [{'name':k,'label':label,'type':'string','default':default,'required':required} for k,label,default,required in fields]
    inputs.insert(2, {'name':'mode','label':'评价什么结果','type':'string','required':True,
                      'default':'数值预测','options':['数值预测','类别判断']})
    code = Path(__file__).with_name('prediction_feedback_code.py').read_text()
    return graph([
        node('start','start','选择预测、实测与对应字段',inputs=inputs),
        node('prepare','code','检查标识并保存本次输入',code=code,timeout=120,
             inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in inputs}}),
        node('evaluate','code','关联反馈、比较误差并解释结果',code=code,timeout=120,
             inputs={'operation':'evaluate','prepared':ref('prepare','output')}),
        node('end','end','对照结果与下一步建议',outputs={'result':ref('evaluate','output'),'markdown':ref('evaluate','output','markdown')})])


def example_files():
    return {
        'predictions.csv':'sample_id,batch,prediction,baseline\ns1,A,10,8\ns2,A,12,10\ns3,B,5,4\ns4,B,7,7\ns5,B,,1\n',
        'measurements.csv':'sample_id,actual\ns3,0\ns1,9\ns2,11\ns5,1\nextra,3\n',
        'measurements-2.csv':'sample_id,actual\ns1,10\ns2,12\ns3,5\ns4,7\ns5,1\n',
        'review-labels.csv':'sample_id,batch,prediction,actual,baseline\nc1,A,合格,合格,合格\nc2,A,合格,复核,合格\nc3,B,复核,复核,合格\nc4,B,未知,合格,合格\nc5,B,合格,,合格\n',
        '字段与练习说明.txt':'全部为自编数据。默认数值例有5条预测，其中3条可评价：误差为1、1、5；MAE=7/3。s4无实测，s5无预测，extra没有对应预测。batch来自预测表，实测乱序也应正确对应。换measurements-2.csv后4条可评价且误差为0，旧结果保留。可填容差2，比较容差内比例。类别练习选择review-labels.csv、实测表留空、模式选类别判断、容差留空：4条有效，2条正确，未知预测类别也进入评价。字段值是演示用，不是客户工艺阈值。'}
