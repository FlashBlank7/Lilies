"""Editable row preparation; learned transforms remain in the existing training pipeline."""
from pathlib import Path

NAME = '样本清洗与逐行特征准备'
DESCRIPTION = '按明确规则处理缺值、重复、标签和逐行公式，保留每条记录的去向与原因；保存处理方法，供新数据再次使用。'
GUIDE = '''需要把普通表格整理成可分析或训练的样本，并解释哪些记录被保留、排除以及特征怎么算时使用。先 inspect 当前工作流再调用，员工不必知道工作流编号。
每行一个样本，输入CSV/TSV/XLSX；多工作表明确指定。复合表头、工艺编号解析和复杂工艺公式仍由可编辑代码完成，本流程不猜测单位或标签语义。
仅声明实际需要的必填列和数值列；数字格式错误变为空值并记录原因，不默认为0。默认保留有可选数值/公式问题的记录，训练中的缺失值处理仍在折内完成；可选择排除转换有问题的记录。必填值缺失、已声明标签缺失/未映射、全空行单独排除。
标签映射是明确的原值到新值表，不把小数转整数，不猜测严重程度。填写标签列表示制备有标签样本；不映射时保留原值。新数据模式不要求标签存在，输出移除标签，不用标签生成特征。样本标识用于发现重复，不等于训练分组：设备/批次隔离仍由后续训练配置。
重复策略默认全部保留并说明；可只删除所有原单元格完全相同的额外记录。同标识但内容不同的记录不擅自合并或选第一条，报告具体记录号和冲突，需按实际样本单位处理。
特征行表支持两列的和、差、绝对差、乘积、比值，以及与固定数的缩放/偏移。顺序执行，后行可用前行结果；输出名必须新建，不能覆盖原列，分母为0返回空值和原因。不用标签或声明排除的字段生成特征。这里只提供常见逐行表达，特殊方法编辑代码，不扩建所有方法为平台积木。
排除列可用于移走预测后才知道的字段；必须由员工确认预测时点，平台不会从列名猜测。填补均值、标准化、编码、筛选等需要拟合的方法继续用现有训练折内预处理，本流程不在整表估计参数。
outputs.result.source_path是处理后的数据CSV，不含审查状态/行号等辅助特征；row-status.csv对应输入记录号与输出记录号，issues.csv列出问题，excluded.csv保留排除的原记录，method.json保存参数、输入/输出字段与版本。每次结果独立，原始资料不改。记录号按表头为1的数据记录编号，CSV含换行单元格时不是物理文本行号。
换数据但沿用原方法时选择前次method.json，并选择有标签样本或新数据模式；其余方法表单不生效。沿用时核对实际处理代码，修改处理节点后不能冒充相同方法。改方法要清空该文件选择并修改表单。方法文件保存参数和代码内容标识；领域字段和阈值保持所在项目的私有范围。
可把处理后的CSV交给现有分析、训练、预测或反馈流程；建议不自动启动后续任务。模型服务不会自动调用本流程：使用原模型前应先用训练时保存的方法处理新资料，特征语义保持一致。预测后字段、标签及重复批次仍需按任务判断，数据处理成功不证明模型有效。'''


def workflow():
    from .official_workflows import graph, node, ref
    fields = [
        dict(name='source_path', label='要处理的表格', type='file', required=True, default=''),
        dict(name='sheet', label='Excel 工作表（单表可留空）', type='string', default=''),
        dict(name='method_path', label='沿用前次处理方法（可选 method.json；选中后下方方法参数不生效）', type='file', default=''),
        dict(name='purpose', label='本次资料用途', type='string', default='有标签样本或一般分析', options=['有标签样本或一般分析','新数据预测前处理']),
        dict(name='required_columns', label='必填列（每行一列；其值缺失才排除记录）', type='string', default=''),
        dict(name='numeric_columns', label='转为数值的列（每行一列）', type='string', default=''),
        dict(name='id_columns', label='样本标识列（可组合；用于提示重复，每行一列）', type='string', default=''),
        dict(name='drop_columns', label='输出中排除的列（如预测后字段，每行一列）', type='string', default=''),
        dict(name='label_column', label='标签列（无需标签时留空）', type='string', default=''),
        dict(name='label_map', label='标签对应关系（留空保留原值；填写后未列出的值会排除）', type='array', default=[], columns=[
            dict(name='from',label='原值'), dict(name='to',label='新值')]),
        dict(name='duplicate_policy', label='完全相同的重复记录', type='string', default='保留并说明', options=['保留并说明','仅保留第一次出现']),
        dict(name='invalid_policy', label='可选数值或特征计算失败时', type='string', default='保留空值并说明', options=['保留空值并说明','排除有转换问题的记录']),
        dict(name='features', label='逐行特征（从上到下计算）', type='array', default=[], columns=[
            dict(name='output',label='新特征列名'), dict(name='left',label='第一列'),
            dict(name='operation',label='计算方式',options=['两列相加','第一列减第二列','两列差的绝对值','两列相乘','第一列除以第二列','乘固定数','加固定数'],default='第一列减第二列'),
            dict(name='right',label='第二列（固定数计算时留空）'), dict(name='constant',label='固定数（两列计算时留空）')]),
    ]
    code = Path(__file__).with_name('sample_preparation_code.py').read_text()
    return graph([node('start','start','选择资料和处理规则',inputs=fields),
        node('prepare','code','读取资料与固定处理方法',code=code,timeout=120,inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in fields}}),
        node('transform','code','处理样本与逐行特征',code=code,timeout=120,inputs={'operation':'transform','prepared':ref('prepare','output')}),
        node('report','code','解释保留、排除和下一步',code=code,inputs={'operation':'report','result':ref('transform','output')}),
        node('end','end','处理结果与可复用方法',outputs={'result':ref('report','output'),'markdown':ref('report','output','markdown')})])


def example_defaults():
    return dict(required_columns='sample_id',numeric_columns='length_a\nlength_b\nload',id_columns='sample_id',drop_columns='after_check',label_column='quality',
        label_map=[{'from':'通过','to':'pass'},{'from':'返修','to':'review'}],duplicate_policy='仅保留第一次出现',
        features=[dict(output='length_gap',left='length_a',operation='两列差的绝对值',right='length_b',constant=''),
                  dict(output='load_per_length',left='load',operation='第一列除以第二列',right='length_b',constant='')])


def example_files():
    return {
        'samples.csv':'sample_id,batch,length_a,length_b,load,quality,after_check\nA,b1,10,8,16,通过,合格\nA,b1,10,8,16,通过,合格\nB,b1,9,0,9,返修,复测\nC,b2,bad,7,14,通过,合格\nD,b2,8,8,16,,未检查\nE,b3,7,6,12,待定,未检查\nF,b3,11,10,20,通过,合格\nF,b3,11,10,20,返修,复测\n,b4,8,7,14,通过,合格\n,,,,,,\n',
        'new-samples.csv':'sample_id,batch,length_a,length_b,load\nN1,b5,12,10,20\nN2,b5,8,0,12\n',
        '字段与练习说明.txt':'自编示例，不是客户工艺。sample_id是样本标识，batch是批次。length_a/b与load为任意单位；除法特征的业务意义需另行确认。after_check代表事后检查，不可作为预测前特征。初次10条记录保留5条、排除5条：重复A、缺标签D、未映射E、缺标识、空行；B分母0和C非法数值保留空值并说明，F标签冲突保留双方提示核实。选择排除转换有问题的记录后保留3条。换new-samples.csv并沿用method.json，选择新数据模式，不要求标签也不恢复after_check列。'}
