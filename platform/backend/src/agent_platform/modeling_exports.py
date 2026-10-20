"""Model downloads shared by the HTTP endpoint and project agent exports."""
import json
import zipfile
from .training_notes import write_bundle


async def model_bundle(modeling, project_id, study_id, candidate_id, slot=None):
    candidate = await modeling.get(project_id, 'candidate', candidate_id)
    if candidate['study_id'] != study_id:
        raise KeyError('候选不属于当前研究')
    study = await modeling.get(project_id, 'study', study_id)
    dataset = await modeling.get(project_id, 'dataset', study['dataset_id'])
    good = [t for t in candidate['trials'] if t['status'] == 'completed']
    if not good:
        raise ValueError('尚无成功模型可以下载')
    metric = study['evaluation']['metric']
    best = sorted(good, key=lambda t: t['metrics'][metric], reverse=metric not in {'mae', 'rmse'})[0] if slot is None else next((t for t in good if t['slot'] == slot), None)
    if best is None:
        raise KeyError('该试验没有成功模型')
    note = await modeling.training_note(project_id, study_id, candidate_id, best['slot'])
    folder = modeling.path(project_id, candidate_id)
    model = folder / 'output' / f'trial-{best["slot"]}'
    archive = folder / f'model-{best["slot"]}.zip'
    environment = await modeling.snapshot_environment(candidate['image'])
    # Rebuild a complete bundle atomically; concurrent readers see the old or new zip.
    from uuid import uuid4
    temp = folder / ('model-' + str(uuid4()) + '.zip.tmp')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
        write_bundle(z, note)
        for path in model.rglob('*'):
            if path.is_file() and not path.is_symlink() and not any(part.startswith('ag-fold-') for part in path.parts):
                z.write(path, 'model/' + str(path.relative_to(model)))
        if (folder / 'transformer.py').exists():
            z.write(folder / 'transformer.py', 'transformer.py')
        z.writestr('configuration.json', json.dumps({'study': study, 'candidate': candidate, 'selected_trial_slot': best['slot']}, ensure_ascii=False, indent=2))
        for name in ('worker.py', 'requirements.lock', 'environment.json'):
            z.write(environment / name, name)
        if (environment / 'modeling_preprocessing.py').is_file():
            z.write(environment / 'modeling_preprocessing.py', 'modeling_preprocessing.py')
        # The supplied input/input.csv example has one header row, even
        # when training used an XLSX sheet, title rows or extra headings.
        prediction_mapping = {**dataset['mapping'], 'sheet': 0, 'header_row': 0, 'skip_after_header': 0}
        prediction_features = candidate['features']
        group = prediction_mapping.get('group_column')
        if prediction_mapping['kind'] == 'tabular' and group:
            prediction_mapping['group_column'] = ''
            prediction_features = {**prediction_features, 'exclude': list(dict.fromkeys(
                [*prediction_features.get('exclude', []), group]))}
        prediction = {'action': 'predict', 'mapping': prediction_mapping, 'features': prediction_features,
            'engine': candidate['engine'], 'feature_columns': best['feature_columns'], 'classes': best.get('classes'), 'source': 'input/input.csv',
            'labels': 'input/labels.csv' if dataset['mapping']['kind'] == 'timeseries' else '', 'model': 'model', 'output': 'output'}
        if candidate.get('code_sha256'):
            prediction['transformer'] = 'transformer.py'
        z.writestr('predict.example.json', json.dumps(prediction, ensure_ascii=False, indent=2))
        z.writestr('README.md', '# 可复用预测模型\n\n指标来自固定验证划分，保留测试集未用于搜索。\n\n在平台使用“模型预测”积木，传入此研究、候选和无标签数据集。\n\n'
            '## 独立预测\n\n解压后，在当前目录创建 input/ 和 output/，将无标签数据保存为 input/input.csv；第一行是字段名，后续每行一条数据，不加标题或单位行。'
            '表格预测不要求用于训练划分的分组列；模型要求的样本标识及特征列仍需提供。'
            '时序还需要同样单行表头的 input/labels.csv，包含样本标识及预测时点，不需要目标标签。'
            '示例使用普通 CSV 的读取布局。若改用 XLSX 或含额外表头的文件，请在 predict.example.json 中调整 source、labels 路径，以及 mapping.sheet、header_row、skip_after_header；字段含义和特征方案仍沿用训练。\n\n'
            '本机保留原镜像时运行：\n\n```sh\nmkdir -p input output\ndocker run --rm --network none --cpus 4 --memory 4g '
            '-v "$PWD:/bundle:ro" -v "$PWD/output:/bundle/output:rw" -w /bundle ' + candidate['image'] + ' /bundle/predict.example.json\n```\n\n'
            '结果写入 output/predictions.csv。其他机器可按 environment.json 的 Python 版本建立独立环境，安装 requirements.lock，'
            '然后在解压目录执行 `python worker.py predict.example.json`。如包内有 modeling_preprocessing.py，应与 worker.py 一同保留；'
            '预处理按模型中已拟合的值执行，不在新样本上重新估计。包内没有原始训练数据；configuration.json 保存实验记录，model/ 保存模型及验证预测。只加载自己信任的模型文件。\n')
    temp.replace(archive)
    return archive, note
