"""Small, editable workflow recipes built from the public block contract."""
from copy import deepcopy

from .workflow_models import WorkflowSpec
from .project_space import AddWorkflow, add_workflow
from .project_skills import SkillDocument, save_skill


def ref(node, *path):
    return {'$ref': {'node_id': node, 'path': list(path)}}


def node(ident, kind, title, **config):
    return {'id': ident, 'type': kind, 'title': title, 'config': config}


def graph(nodes):
    return {'nodes': nodes, 'edges': [{'id': a['id'] + '-' + b['id'], 'source': a['id'], 'target': b['id']}
                                     for a, b in zip(nodes, nodes[1:])]}


def training(problem, process=False):
    inputs = [{'name': 'source_path', 'type': 'string', 'required': True, 'description': '项目中已上传的 CSV、TSV 或 XLSX'},
              {'name': 'target', 'type': 'string', 'required': True, 'description': '需要预测的标签列名称'},
              {'name': 'group_column', 'type': 'string', 'default': '', 'description': '批次／炉次字段；使用分组划分时填写'}]
    mapping = {'target': ref('$inputs', 'target'), 'group_column': ref('$inputs', 'group_column')}
    if process:
        inputs += [{'name': name, 'type': 'string', 'required': True, 'description': description} for name, description in [
            ('labels_path', '每行一个预测时点和标签的样本表'), ('id_column', '设备／炉次标识列'),
            ('time_column', '过程测量时间列'), ('prediction_time_column', '标签表中的预测时点列')]]
        mapping.update(kind='timeseries', **{name: ref('$inputs', name) for name in ('id_column', 'time_column', 'prediction_time_column')})
    return graph([
        node('start', 'start', '选择数据和预测目标', inputs=inputs),
        node('profile', 'data_analysis', '登记数据与质量分析', source_path=ref('$inputs', 'source_path'),
             labels_path=ref('$inputs', 'labels_path') if process else '', mapping=mapping),
        node('features', 'feature_extract', '样本与特征', dataset_id=ref('profile', 'output', 'dataset_id'),
             features={'columns': [], 'exclude': [], 'timeseries': 'minimal', 'window_seconds': 3600 if process else None}),
        node('train', 'model_train', '比较模型与简单基线', dataset_id=ref('features', 'output', 'dataset_id'),
             features=ref('features', 'output', 'feature_plan'), evaluation={'problem': problem,
                 'metric': 'macro_f1' if problem == 'classification' else 'mae', 'split': 'group' if process else 'random',
                 'folds': 3, 'holdout_fraction': .2}, budget={'seconds': 600, 'trials': 3, 'trial_seconds': 120},
             candidate={'engine': 'sklearn', 'models': ['linear', 'forest', 'hist_gradient'], 'batch_size': 3}),
        node('test', 'model_train', '固定最佳方案的独立测试', finalize=True, study_id=ref('train', 'output', 'study_id')),
        node('end', 'end', '训练结果', outputs={'data': ref('profile', 'output'), 'features': ref('features', 'output'),
             'training': ref('train', 'output'), 'test': ref('test', 'output')}),
    ])


RULE_CODE = '''def main(inputs):
    import csv, json, math, hashlib
    from pathlib import Path
    from uuid import uuid4
    raw_threshold = inputs.get('threshold')
    result = inputs['prediction']
    policy = result.get('acceptance')
    if raw_threshold is None and not policy:
        raise ValueError('模型未保存自动采纳阈值，请填写经业务验证的阈值')
    threshold = float(raw_threshold) if raw_threshold is not None else policy['threshold']
    if threshold is not None and (not math.isfinite(threshold) or not 0 <= threshold <= 1):
        raise ValueError('放行阈值必须在 0 和 1 之间')
    result = inputs['prediction']
    if not result.get('model_version'):
        raise ValueError('缺少实际模型版本，不能以规则替代模型预测')
    source = Path(result['project_path'])
    if source.is_absolute() or '..' in source.parts or source.parts[0] != 'results':
        raise ValueError('预测文件必须来自当前项目结果目录')
    folder = Path('results/decisions') / str(uuid4())
    folder.mkdir(parents=True)
    count = accepted = 0
    preview = []
    with source.open(newline='', encoding='utf-8') as src, (folder/'decisions.csv').open('w', newline='', encoding='utf-8-sig') as dst:
        reader = csv.DictReader(src)
        columns = result.get('probability_columns', {})
        if not columns:
            raise ValueError('此模型没有分类概率；请绑定分类模型，或修改规则节点处理回归结果')
        fields = ['model_decision' if name=='decision' else name for name in reader.fieldnames]
        writer = csv.DictWriter(dst, fieldnames=fields + ['action', 'reason'])
        writer.writeheader()
        for row in reader:
            column = columns.get(row['prediction'])
            probability = float(row[column]) if column and row.get(column) else float('nan')
            allow = threshold is not None and math.isfinite(probability) and probability >= threshold
            if 'decision' in row: row['model_decision'] = row.pop('decision')
            row.update(action='accept_prediction' if allow else 'review', reason='达到采纳阈值' if allow else '无可用阈值或概率不足，交由复核')
            writer.writerow(row)
            count += 1
            accepted += int(allow)
            if len(preview) < 20: preview.append(row)
    (folder/'model-version.json').write_text(json.dumps(result['model_version'], ensure_ascii=False), encoding='utf-8')
    snapshot = folder/'prediction-input.json'
    snapshot.write_text(json.dumps({'prediction':result,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}, ensure_ascii=False), encoding='utf-8')
    markdown = '总计 '+str(count)+' 条；采纳分类建议 '+str(accepted)+' 条，交由复核 '+str(count-accepted)+' 条。使用阈值 '+str(threshold)+'（'+('手动指定' if raw_threshold is not None else '模型保存')+'）。自动采纳不等同产品放行，完整结果见下载。'
    return {'rows': count, 'inherit': accepted, 'measure': count-accepted, 'preview': preview,
            'markdown':markdown,
            'file': str(folder/'decisions.csv'), 'model_version': result['model_version'], 'prediction_input':str(snapshot),
            'applied_threshold':threshold, 'threshold_source':'manual' if raw_threshold is not None else 'model',
            'artifacts':[{'file_path':str(folder/'decisions.csv'),'label':'逐条规则结果 CSV'}, {'file_path':str(snapshot),'label':'规则重算输入 JSON'}]}
'''

REPLAY_INPUT_CODE = '''def main(inputs):
    import json, hashlib
    from pathlib import Path
    source=Path(inputs['source_path'])
    if source.is_absolute() or '..' in source.parts:raise ValueError('请选择当前项目的规则重算输入文件')
    snapshot=json.loads(source.read_text())
    prediction=snapshot['prediction']; data=Path(prediction['project_path'])
    if data.is_absolute() or '..' in data.parts or data.parts[0]!='results':raise ValueError('预测文件必须位于项目结果目录')
    if hashlib.sha256(data.read_bytes()).hexdigest()!=snapshot['sha256']:raise ValueError('原预测文件内容改变，不能复用；请重新预测')
    return prediction
'''


def replay_rules():
    return graph([node('start','start','选择已有预测与新规则',inputs=[
        {'name':'source_path','type':'string','required':True,'description':'前次模型与规则流程生成的规则重算输入 JSON'},
        {'name':'threshold','type':'number','required':False,'description':'新业务阈值；留空沿用模型保存的阈值'}]),
        node('load','code','读取固定预测结果',code=REPLAY_INPUT_CODE,inputs={'source_path':ref('start','source_path')}),
        node('rules','code','只重算规则与报告',code=RULE_CODE,inputs={'prediction':ref('load','output'),'threshold':ref('start','threshold')}),
        node('end','end','新规则结果',outputs={'result':ref('rules','output'),'markdown':ref('rules','output','markdown')})])


def prediction(rules=False):
    inputs = [{'name': 'source_path', 'type': 'string', 'required': True, 'description': '字段语义须与训练一致的无标签数据'}]
    if rules:
        inputs.append({'name': 'threshold', 'type': 'number', 'required': False, 'description': '手动指定业务阈值；留空使用模型保存的验证阈值，无可用阈值时全部复核'})
    nodes = [node('start', 'start', '选择新数据', inputs=inputs),
             node('predict', 'model_predict', '使用固定模型版本批量预测', source_path=ref('$inputs', 'source_path'), model_ref='')]
    if rules:
        nodes.append(node('rules', 'code', '采纳分类建议或交由复核', code=RULE_CODE,
                          inputs={'prediction': ref('predict', 'output'), 'threshold': ref('start', 'threshold')}))
    outputs = {'result': ref('rules' if rules else 'predict', 'output')}
    if rules: outputs['markdown'] = ref('rules','output','markdown')
    nodes.append(node('end', 'end', '预测结果', outputs=outputs))
    return graph(nodes)


CATALOG = {
    'tabular-classification': {'name': '表格分类训练', 'description': '质量类别、缺陷判别等已标注表格；分析、特征、三种基线候选与独立测试。', 'workflow': training('classification')},
    'model-rules-prediction': {'name': '模型与规则批量预测', 'description': '达到配置阈值才采纳分类建议，否则交由复核。产品放行、专有优先级和物理规则需另行编辑。', 'workflow': prediction(True)},
    'tabular-regression': {'name': '表格数值预测训练', 'description': '每行一个样本的连续质量目标；比较模型、简单基线及独立测试。', 'workflow': training('regression')},
    'process-regression': {'name': '工业过程窗口质量预测', 'description': '过程表加样本标签表，按预测时点截取窗口、按炉次隔离；需要真实标签与时间字段。', 'workflow': training('regression', True)},
    'batch-prediction': {'name': '已训练模型批量预测', 'description': '使用原模型、预处理和环境处理新数据，生成 CSV，不重新训练。', 'workflow': prediction()},
    'prediction-rules-replay': {'name': '已有预测的规则重算', 'description': '选择前次规则重算输入文件，只修改规则和报告；不调用模型、不重新训练。', 'workflow': replay_rules()},
}

from . import data_guidance
CATALOG['data-guidance'] = {'name': '数据摸底与分析建议', 'description': data_guidance.DESCRIPTION,
                            'workflow': data_guidance.workflow()}
from . import point_in_time
CATALOG['point-in-time'] = {'name': point_in_time.NAME, 'description': point_in_time.DESCRIPTION,
                           'workflow': point_in_time.workflow(), 'guide': point_in_time.GUIDE}
from . import prediction_feedback
CATALOG['prediction-feedback'] = {'name': prediction_feedback.NAME, 'description': prediction_feedback.DESCRIPTION,
                                 'workflow': prediction_feedback.workflow(), 'guide': prediction_feedback.GUIDE}
from . import candidate_comparison
CATALOG['candidate-comparison'] = {'name':candidate_comparison.NAME,'description':candidate_comparison.DESCRIPTION,
                                   'workflow':candidate_comparison.workflow(),'guide':candidate_comparison.GUIDE}
from . import sample_preparation
CATALOG['sample-preparation'] = {'name':sample_preparation.NAME,'description':sample_preparation.DESCRIPTION,
                                'workflow':sample_preparation.workflow(),'guide':sample_preparation.GUIDE}
from . import parameter_intervals
CATALOG['parameter-intervals'] = {'name':parameter_intervals.NAME,'description':parameter_intervals.DESCRIPTION,
                                'workflow':parameter_intervals.workflow(),'guide':parameter_intervals.GUIDE}
from . import rolling_forecast
CATALOG['rolling-forecast'] = {'name':rolling_forecast.NAME,'description':rolling_forecast.DESCRIPTION,
                             'workflow':rolling_forecast.workflow(),'guide':rolling_forecast.GUIDE}


def catalog():
    return [{'id': key, 'version': 2, 'name': value['name'], 'description': value['description']}
            for key, value in CATALOG.items()]


async def install(services, project_id, template_id):
    if template_id not in CATALOG:
        raise KeyError('官方工作流不存在')
    item = deepcopy(CATALOG[template_id])
    created = await add_workflow(services, project_id, AddWorkflow(name=item['name'], description=item['description'],
                                                              workflow=WorkflowSpec.model_validate(item['workflow'])))
    workflow_id = created['workflow_id']
    content = f'''使用 project_workflows inspect 查看工作流 {workflow_id}「{item['name']}」的当前输入与配置，workflow_run 调用同一个可编辑副本。
{item['description']}
运行前明确样本单位、预测时点、标签来源。训练流程从 source_path 和 target 登记数据，不需要研究或候选编号。
重复设备／炉次要用分组划分；面向未来使用要用时间划分并填写数据／标签可用时间，必要时设置窗口隔离间隔。不得以发布日期后的修订值冒充当时可用数据。
在特征节点排除预测时未知的字段、标签衍生字段及不适合泛化的标识。缺失值拟合、编码、特征筛选在训练折内进行。测试结果只评价已固定方案；可在训练评估表单填写自动采纳的最低验证准确率和最少样本数；只用折外预测选阈值，独立测试固定使用它。无满足条件的阈值时全部复核，不等同工艺放行规则。
特征步骤提供样本预览、完整特征CSV、标识与标签CSV和特征说明JSON，使用共同的原始样本序号对应。导出的是训练前确定性特征，不应在这份全量表上预先拟合标准化、填补或特征筛选。
训练结果在项目建模记录中，包含实际候选、逐折指标、基线和独立测试；文件可从建模记录下载。绑定项目模型后使用单独预测流程，不重训。模型与规则流程必须绑定真实分类模型，阈值由验证数据和业务代价确定，不能自行声称某个阈值可靠。
同一次运行继续时保留数据和模型快照。换文件、标签、字段或划分后创建新运行，旧结果保留。修改规则／报告时使用已有预测 CSV，不需要重训。
缺字段、空窗口、批次不足、模型未绑定时读具体错误，修改相关节点后重新运行。数据不足就列缺项，不生成训练成绩。'''
    if item.get('guide'):
        content = f'工作流 {workflow_id}「{item["name"]}」。\n' + item['guide']
    elif template_id == 'data-guidance':
        content = f'工作流 {workflow_id}「{item["name"]}」。\n' + data_guidance.GUIDE
    else:
        content += '\n根据员工的实际任务选择本流程，不需要先完成数据摸底流程或审批计划。解释为何采用当前划分、指标和方法，区分统计事实、模型预测与业务判断；员工可以纠正或调整。'
    await save_skill(services, project_id, 'official-' + workflow_id,
                     SkillDocument(name=item['name'] + '使用说明', description=item['description'], content=content))
    return {**created, 'template_id': template_id, 'template_version': 2}
