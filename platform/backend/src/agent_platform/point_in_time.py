"""Editable workflow and public, synthetic example of availability-aware joins."""
from pathlib import Path

NAME = '按当时已知信息制备数据'
DESCRIPTION = '按预测时点匹配已发布的数据版本；处理迟到、修订和不同频率，保留缺失及每项来源，不自动训练。'
GUIDE = '''适用于把多源历史记录整理为可分析或训练的样本，不进行价格预测、策略建议或自动训练。
样本表每行有唯一sample_id与prediction_time；记录表为长表，每行entity、feature、observed_time、available_time、value。
普通用户选择两份资料即可使用同名默认字段；不同表头通过输入表单映射。entity两表同名，留空表示所有样本共用一套指标。
observed_time表示测量时点或统计期；available_time必须是这个版本实际可获得的时刻（含历史修订），不能以统计期代替。
匹配必须同时满足两个时间均不晚于样本预测时点，先取最新所属期，再取该期当时最新版本。较早期的迟来修订不会遮盖更新期。
可用时间和预测时点需要时分；仅知道日期时请先明确业务截止时刻，不能猜成零点。没有时区的时间按表单UTC偏移解释。
最长历史时长从所属时间到每个样本预测时点计算；0不限制。最新期缺失或过期保留为空，不自动回退到更早期，不填0。
输出result.source_path是新的样本CSV，可交给数据摸底或训练流程；无需重新复制源文件。匹配明细与数据哈希在单独产物，不混为训练特征。
调用训练前确认标签语义、未来不可用字段及按实体/时间划分；本流程不会证明原样本已有字段可用。不需要审批或先运行其他流程。
重复版本、重复样本ID、缺字段、时间错误有具体记录号。修正文件或参数后新建运行，原结果保留。'''


def workflow():
    from .official_workflows import graph, node, ref
    fields = [('source_path','预测样本表','',True), ('records_path','历史记录与版本表','',True),
              ('sample_id','样本唯一标识列','sample_id',True), ('prediction_time','预测时点列','prediction_time',True),
              ('entity','两表共同的实体列（留空则共用指标）','entity',False), ('feature','指标名称列','feature',True),
              ('observed_time','测量时间／统计期列','observed_time',True), ('available_time','本版本实际可用时间列','available_time',True),
              ('value','指标值列','value',True), ('features','使用的指标（逗号或换行分隔；留空全部）','',False),
              ('utc_offset','未带时区的时间按此UTC偏移解释','+08:00',True),
              ('sample_sheet','样本Excel工作表（单表可留空）','',False), ('records_sheet','记录Excel工作表（单表可留空）','',False)]
    inputs = [{'name':k,'label':label,'type':'string','default':default,'required':required} for k,label,default,required in fields]
    inputs.append({'name':'max_age_hours','label':'最长历史时长（小时；0不限制）','type':'number','default':0,'required':False})
    code = Path(__file__).with_name('point_in_time_code.py').read_text()
    return graph([node('start','start','选择两份资料与字段',inputs=inputs),
        node('prepare','code','检查资料并固定输入版本',code=code,timeout=120,inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in inputs}}),
        node('align','code','按当时可知的数据制备样本',code=code,timeout=120,inputs={'operation':'align','prepared':ref('prepare','output')}),
        node('end','end','样本、来源与方法说明',outputs={'result':ref('align','output'),'markdown':ref('align','output','markdown')})])


def example_files():
    return {
        'samples.csv':'sample_id,entity,prediction_time,target\nearly,A,2026-02-02T10:00:00+08:00,1\nrevised,A,2026-02-06T10:00:00+08:00,2\nlater,A,2026-03-05T10:00:00+08:00,3\nother,B,2026-02-06T10:00:00+08:00,4\n',
        'records.csv':'entity,feature,observed_time,available_time,value\nA,quality,2026-01-31,2026-02-03T09:00:00+08:00,10\nA,quality,2026-01-31,2026-02-05T09:00:00+08:00,11\nA,quality,2026-02-28,2026-03-01T09:00:00+08:00,12\nA,quality,2026-01-31,2026-03-04T09:00:00+08:00,99\nB,quality,2026-01-31,2026-02-03T09:00:00+08:00,20\n',
        'records-2.csv':'entity,feature,observed_time,available_time,value\nA,quality,2026-01-31,2026-02-03T09:00:00+08:00,100\nA,quality,2026-01-31,2026-02-05T09:00:00+08:00,110\nA,quality,2026-02-28,2026-03-01T09:00:00+08:00,120\nB,quality,2026-01-31,2026-02-03T09:00:00+08:00,200\n',
        '字段与练习说明.txt':'全部为自编数据。early样本应为空：当时数据尚未发布。revised取11；later取12，不能被旧月份的99覆盖；other取20，不能串到A。换records-2.csv应得到空、110、120、200，旧输出不变。把最长历史时长改为24小时，查看哪些值变为过期。目标列只用于演示后续分析，不是实际生产标签。'}
