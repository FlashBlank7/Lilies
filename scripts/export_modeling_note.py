"""Export a retrospective note for the saved energy demo; never fit or change models.

The parameters file is obtained by loading this study's own saved estimators in
its pinned image and reading get_params(). The note checks stored predictions
against the frozen input and split, then recomputes the reported metrics.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import statistics
import zipfile


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def write_csv(path, rows):
    with Path(path).open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as file:
        return list(csv.DictReader(file))


def metrics(rows):
    actual = [float(r['actual']) for r in rows]
    pred = [float(r['prediction']) for r in rows]
    squared = sum((a - p) ** 2 for a, p in zip(actual, pred))
    mean = statistics.mean(actual)
    return {'mae': statistics.mean(abs(a - p) for a, p in zip(actual, pred)),
            'rmse': math.sqrt(squared / len(rows)),
            'r2': 1 - squared / sum((a - mean) ** 2 for a in actual)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='a0f33411-6c1d-48b8-9902-30a1b385eecb')
    parser.add_argument('--study', default='f399005f-9d78-4628-8d6a-302a4e80e98c')
    parser.add_argument('--model-parameters', required=True)
    parser.add_argument('--output', default='docs/training-notes/energy-20260914')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    output = (repo / args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect('file:' + str(repo / 'data/agent_platform.db') + '?mode=ro', uri=True)
    def get(kind, ident):
        row = c.execute('SELECT document FROM modeling_objects WHERE project_id=? AND kind=? AND id=?', (args.project, kind, ident)).fetchone()
        if not row:
            raise ValueError('Object does not belong to this project')
        return json.loads(row[0])
    study = get('study', args.study)
    dataset = get('dataset', study['dataset_id'])
    candidates = [json.loads(r[0]) for r in c.execute('SELECT document FROM modeling_objects WHERE project_id=? AND kind=? AND parent_id=?', (args.project, 'candidate', args.study))]
    candidates.sort(key=lambda value: value['created_at'])
    c.close()
    root = repo / 'data/modeling' / args.project
    split_path = root / args.study / 'output/split.json'
    split = read_json(split_path)
    source = root / dataset['id'] / dataset['files']['source']['name']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == dataset['files']['source']['sha256']
    data = read_csv(source)
    assert study['evaluation']['problem'] == 'regression' and dataset['mapping']['kind'] == 'tabular'
    id_col, target = dataset['mapping']['id_column'], dataset['mapping']['target']
    train_rows, holdout_rows = set(split['development']), set(split['holdout'])
    assert train_rows.isdisjoint(holdout_rows)
    valid_order = [split['samples'][row] for _, valid in split['folds'] for row in valid]
    assert len(valid_order) == len(set(valid_order)) == len(train_rows)
    fold_for_row = {}
    baseline_rows = []
    folds = []
    for number, (train, valid) in enumerate(split['folds'], 1):
        assert set(train).isdisjoint(valid)
        assert set(train + valid) <= train_rows
        middle = statistics.median(float(data[split['samples'][i]][target]) for i in train)
        for i in valid:
            row = split['samples'][i]; fold_for_row[row] = number
            baseline_rows.append({'actual': data[row][target], 'prediction': middle})
        folds.append({'fold': number, 'train': len(train), 'validation': len(valid)})
    baseline = metrics(baseline_rows)['mae']
    assert math.isclose(baseline, study['baseline']['mae'], abs_tol=1e-10)
    membership = [{'row_index_zero_based': original, 'sample_id': data[original][id_col],
                   'role': 'final_test' if position in holdout_rows else 'development',
                   'validation_fold': fold_for_row.get(original, '')}
                  for position, original in enumerate(split['samples'])]
    write_csv(output / 'split-membership.csv', membership)
    shutil.copyfile(source, output / 'data.csv')
    shutil.copyfile(split_path, output / 'split.json')
    write_json(output / 'study.json', study)
    write_json(output / 'dataset.json', dataset)
    write_json(output / 'candidates.json', candidates)
    parameters = read_json(args.model_parameters)
    write_json(output / 'model-parameters.json', parameters)
    optuna = []
    for path in sorted((root / args.study / 'parameter-search').glob('*/optuna.db')):
        db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True); db.row_factory = sqlite3.Row
        for record in db.execute('SELECT * FROM trials').fetchall():
            trial = dict(record)
            values = [dict(row) for row in db.execute('SELECT param_name,param_value,distribution_json FROM trial_params WHERE trial_id=?', (trial['trial_id'],))]
            trial['parameters'] = {row['param_name']: row['param_value'] for row in values}
            trial['distributions'] = {row['param_name']: json.loads(row['distribution_json']) for row in values}
            trial['database'] = str(path.relative_to(repo))
            optuna.append(trial)
        db.close()
    write_json(output / 'optuna-history.json', optuna)
    history, predictions = [], []
    seconds = 0
    start = datetime.fromisoformat(study['created_at'])
    candidate_features = [value['features'] for value in candidates]
    assert all(value == candidate_features[0] for value in candidate_features)
    for candidate in candidates:
        for trial in sorted(candidate['trials'], key=lambda value: value['slot']):
            folder = root / candidate['id'] / 'output' / f'trial-{trial["slot"]}'
            saved = read_json(folder / 'result.json')
            assert saved['metrics'] == trial['metrics']
            rows = read_csv(folder / 'validation-predictions.csv')
            assert [int(row['sample']) for row in rows] == valid_order
            for row in rows:
                original = int(row['sample'])
                assert math.isclose(float(row['actual']), float(data[original][target]), abs_tol=1e-12)
                predictions.append({'trial': len(history) + 1, 'candidate_id': candidate['id'], 'slot': trial['slot'],
                    'row_index_zero_based': original, 'sample_id': data[original][id_col], 'fold': fold_for_row[original],
                    'actual': row['actual'], 'prediction': row['prediction'],
                    'absolute_error': abs(float(row['actual']) - float(row['prediction'])),
                    **{key: data[original][key] for key in candidate['features']['columns']}})
            actual = metrics(rows)
            assert all(math.isclose(actual[key], trial['metrics'][key], abs_tol=1e-10) for key in actual)
            db_trial = next(value for value in optuna if value['parameters'] == trial['parameters'] and value['number'] == trial['optuna_trial'])
            model = next(value for value in parameters if value['candidate_id'] == candidate['id'] and value['slot'] == trial['slot'])
            for key, value in trial['parameters'].items():
                assert model['estimator_parameters']['alpha' if key == 'regularization' else key] == value
            seconds += trial['seconds']
            history.append({'trial': len(history) + 1, 'candidate_id': candidate['id'], 'slot': trial['slot'],
                'model': model['estimator_class'], 'searched_parameters': json.dumps(trial['parameters'], ensure_ascii=False),
                **actual, 'baseline_mae': baseline, 'seconds': trial['seconds'], 'cumulative_trial_seconds': seconds,
                'completed_utc': db_trial['datetime_complete'],
                'wall_since_study_seconds': (datetime.fromisoformat(db_trial['datetime_complete']).replace(tzinfo=timezone.utc) - start).total_seconds(),
                **{f'fold_{i}_mae': value['mae'] for i, value in enumerate(trial['fold_metrics'], 1)},
                'run_id': candidate['run_id'], 'task_id': candidate['task_id']})
    write_csv(output / 'trial-history.csv', history)
    write_csv(output / 'validation-predictions.csv', predictions)
    final_path = root / args.study / 'holdout/output/holdout-predictions.csv'
    final_rows = read_csv(final_path)
    assert [int(row['sample']) for row in final_rows] == [split['samples'][i] for i in split['holdout']]
    final_metrics = metrics(final_rows)
    assert all(math.isclose(final_metrics[key], study['test_result']['metrics'][key], abs_tol=1e-10) for key in final_metrics)
    shutil.copyfile(final_path, output / 'holdout-predictions.csv')
    environment = repo / 'data/modeling/_environments' / study['image'].replace(':', '-')
    for name in ('worker.py', 'requirements.lock', 'environment.json'):
        shutil.copyfile(environment / name, output / name)
    benchmark = read_json(repo / 'docs/modeling-comparison.json')
    assert benchmark['native_codex']['id'] == study['id'] and benchmark['image'] == study['image']
    shutil.copyfile(repo / 'docs/modeling-comparison.json', output / 'benchmark.json')
    write_json(output / 'checks.json', {'source_sha256': dataset['files']['source']['sha256'],
        'folds': folds, 'same_validation_rows_all_trials': True, 'validation_does_not_contain_holdout': True,
        'features_unchanged': True, 'recomputed_validation_mae': [row['mae'] for row in history],
        'recomputed_baseline_mae': baseline, 'recomputed_holdout_metrics': final_metrics,
        'benchmark_limit': 'Historical aggregate summary only; per-model benchmark predictions and parameters were not retained. No new benchmark was trained for this note.'})
    plots(output, history, predictions, baseline, study, benchmark)
    first, forest, best = history[0]['mae'], history[1]['mae'], study['best']['score']
    table = '\n'.join(f'| {r["trial"]} | {r["model"]} | `{r["searched_parameters"]}` | {r["mae"]:.6f} | {r["rmse"]:.6f} | {r["r2"]:.6f} | {r["seconds"]:.3f} |' for r in history)
    note = f'''# Training Note：能耗构造数据的四次模型试验

这是从已保存的真实训练记录整理的复盘笔记。本次没有重新训练，没有改写历史结果。
场景是独立平台测试；不代表硅棒、氧预测或任何真实工业场景已达标。

## 1. 提升来自哪里

验证 MAE 从 **{first:.6f}** 降到 **{best:.6f}**，相对初步线性模型下降 **{(1-best/first)*100:.2f}%**。
输入字段、数据版本、评价划分和预处理保持不变，没有增加特征。

沿这条实际尝试路径，换用随机森林先把 MAE 降到 {forest:.6f}，下降 {(1-forest/first)*100:.2f}%；
随后森林参数改变，将误差进一步降低 {(1-best/forest)*100:.2f}%。这是前后结果的描述，并非严格隔离所有因素的因果归因。

从保存的开发集预测可见：能耗与 pressure 呈明显非线性关系，线性预测向中间值聚集，森林能更好地表达这种关系。
初步线性模型甚至稍差于中位数参照 {baseline:.6f}，因此起点较弱、构造数据规律清晰，是降幅显得很大的主要背景。
不能将这个幅度解释为复杂工业数据也能获得同样提升。

![开发集逐样本预测与非线性关系](prediction-diagnostics.png)

图中每个预测都来自该样本未参与训练的验证折。仅画 192 条开发数据，不使用最终测试结果解释选模。

## 2. 数据与固定划分

- 数据版本：`{dataset['id']}`；原始文件 `requirement-package/data.csv`，{len(data)} 条，无缺失、无重复。
- 目标 `energy`；特征 `pressure / temperature / line`；`sample_id` 只作标识。负压力按客户确认全部保留。
- 原数据 SHA-256：`{dataset['files']['source']['sha256']}`。
- 随机种子 **42**；48 条留作最终测试，其余 **192 条**做固定三折。每折 **128 条训练、64 条验证**，每个开发样本只进入一次验证。
- `line` 是特征和误差拆分字段；本次 A/B 均出现在训练和验证中，**不属于跨产线泛化验证**。这是针对独立构造样本约定的随机划分。
- 数值列在各训练折内中位数填补及标准化；类别列在训练折内众数填补及 OneHot 编码。无 SelectKBest、自定义转换或时序特征。
- 四次验证预测均核对同一套 192 个行号，且与最终测试无交集；MAE、RMSE、R² 已从 CSV 重新计算，和保存结果一致。

完整行号：[split.json](split.json)；逐样本角色：[split-membership.csv](split-membership.csv)；核对结果：[checks.json](checks.json)。
`split.json` 的 samples 对应原 CSV 的零基行号，CSV 表头不计入行号。

## 3. 模型换用、超参数查找与逐次结果

共 **2 个候选方案、4 次试验**：先运行一次 Ridge，再运行三次 RandomForestRegressor。
每次试验包含三折拟合和开发集重拟合，因此共 16 次主模型 fit；中位数参照另在各训练折计算。

| 试验 | 实际模型类 | 搜索得到的参数 | 验证 MAE | RMSE | R² | 试验耗时／秒 |
|---|---|---|---:|---:|---:|---:|
{table}

搜索使用 Optuna TPE，seed=42，`n_startup_trials=2`。线性和森林方向各有独立参数历史。
线性只试了一个 alpha：`regularization` 在 [0.001, 100] 对数范围采样，映射到 Ridge 的 alpha。
森林搜索 `max_depth ∈ [2,16]` 和 `min_samples_leaf ∈ [1,10]`；固定 `n_estimators=100, random_state=42, n_jobs=4, max_features=1.0, bootstrap=true`。
森林的前两次是启动采样，第三次才使用已有结果指导采样。没有进行大规模搜索，也没有证明找到全局最优。

统筹保存的换用理由：

> {candidates[1]['hypothesis']}

完整参数已从冻结镜像中的实际模型对象读取，见 [model-parameters.json](model-parameters.json)；
搜索分布和时间见 [optuna-history.json](optuna-history.json)；每折分数、任务和运行标识见 [trial-history.csv](trial-history.csv)。

## 4. 性能提升曲线

![实际试验改进曲线](improvement.png)

横轴分别为试验序号和从研究创建到该试验完成的墙钟时间。四个点对应四次独立模型试验；没有保存逐 epoch 损失曲线。
单次试验耗时合计 {seconds:.3f} 秒；研究计时器记录 {study['used_seconds']:.2f} 秒，包含数据处理、进程启动、调度与统筹批间等待，不能将两者混为训练速度。
原 Optuna 日期字段未带时区，此处按该计算容器的 UTC 时间与平台 UTC 记录对齐。

目标是验证 MAE ≤ 0.6；第四次达到目标后停止搜索。总预算 600 秒、10 次试验，本次实际使用 4 次。
最终选定模型随后在保留的 48 条测试数据上得到 MAE **{final_metrics['mae']:.6f}**；它不进入上述改进曲线。
研究此后禁止继续搜索。保留测试未参与 fit/参数选择，但资料曾可被读取，所以不包装为全程未见标签的盲测。

## 5. Benchmark：自主改进是否优于简单方案

![同数据同划分的历史对照](benchmark.png)

| 方案 | 最佳验证 MAE | 实际试验 | 保存的耗时口径 |
|---|---:|---:|---|
| 三种固定模型分别训练后选最优 | 0.460390 | 3 | 批次墙钟 4.40 秒，不含 Agent |
| 初始特征下 Optuna 小批搜索 | 1.032827 | 3 | 批次墙钟 4.09 秒，不含 Agent |
| AutoGluon 轻量 LightGBM | 1.532285 | 1 | 批次墙钟 4.37 秒，不含 Agent |
| Codex 换模型方向并搜索森林参数 | 0.518800 | 4 | 研究计时 89.96 秒，含 Agent 等待 |

使用同数据、同镜像、同评价划分，共同上限 600 秒／10 次试验；各策略没有用尽相同计算量，不能据此宣称等计算量或端到端速度排名。
**固定模型择优仍好于这次自主改进。** 目前证明了统筹能依据误差推进，尚未证明其普遍优于简单固定方案。
AutoGluon 仅启用 LightGBM，三次 Optuna 也很少，不能代表二者的完整能力。

历史对照见 [benchmark.json](benchmark.json)。这里存在记录缺口：只保存了各策略最佳分数、模型名称和耗时，
没有保留 benchmark 各模型逐样本预测、全部参数和获胜模型名称。该次运行时已核对固定划分相同，
但本次笔记不能再次从其逐样本预测回算 benchmark 指标；没有用新训练结果冒充历史结果。
这与平台内四次训练均有完整逐样本预测的情况不同。

## 6. 如何追溯

- [逐次训练表](trial-history.csv)、[768 条验证预测](validation-predictions.csv)、[最终测试预测](holdout-predictions.csv)。
- [候选原文与改动理由](candidates.json)、[研究原记录](study.json)、[数据说明和字段概况](dataset.json)、[冻结数据](data.csv)。
- [原 worker](worker.py)、[实际环境](environment.json)、[锁定依赖](requirements.lock)、[模型参数](model-parameters.json)。
- [应用内项目](http://127.0.0.1:3000/projects/{args.project}) → 数据与建模 → 查看结果 → 实验；各实验可进入关联运行。

研究标识 `{study['id']}`，镜像 `{study['image']}`。
原训练模型继续保存在平台内，可从项目下载。本笔记整理于 {datetime.now(timezone.utc).isoformat()}，是事后核对和可读整理；平台尚未自动生成这一份完整 Training Note。
'''
    (output / 'README.md').write_text(note)
    # Keep a portable archive with plots and raw tables; no platform code is changed.
    with zipfile.ZipFile(output.with_suffix('.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.iterdir()):
            if path.is_file():
                archive.write(path, output.name + '/' + path.name)
    print(json.dumps({'note': str(output / 'README.md'), 'trials': len(history), 'verified_predictions': len(predictions),
                      'mae_reduction_percent': (1-best/first)*100, 'benchmark_trace_gap': True}, ensure_ascii=False))


def plots(output, history, predictions, baseline, study, benchmark):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams['font.family'] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({'axes.spines.top': False, 'axes.spines.right': False, 'axes.labelcolor': '#202B3D',
                         'text.color': '#202B3D', 'xtick.color': '#56687F', 'ytick.color': '#56687F',
                         'axes.edgecolor': '#CED7E4', 'font.size': 11, 'axes.titlepad': 14, 'svg.fonttype': 'path'})
    blue, pale = '#3F639F', '#90A6C5'
    def save(fig, name):
        fig.savefig(output / (name + '.png'), dpi=180, facecolor='white')
        fig.savefig(output / (name + '.svg'), facecolor='white')
        plt.close(fig)
    scores = [row['mae'] for row in history]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout='constrained')
    for ax, x, label in [(axes[0], [row['trial'] for row in history], '试验序号'),
                          (axes[1], [row['wall_since_study_seconds'] for row in history], '研究创建后／秒（含等待）')]:
        ax.plot(x, scores, marker='o', color=blue, linewidth=2, label='固定三折验证 MAE')
        ax.axhline(baseline, color=pale, linestyle='--', label=f'中位数参照 {baseline:.3f}')
        ax.axhline(.6, color='#498276', linestyle=':', label='目标 0.6')
        if ax is axes[0]:
            for xx, score in zip(x, scores):
                ax.annotate(f'{score:.4f}', (xx, score), xytext=(0, 10), textcoords='offset points', ha='center', fontsize=9)
        ax.set(xlabel=label, ylabel='验证 MAE（越低越好）', ylim=(0, 5.3)); ax.grid(axis='y', alpha=.15)
    axes[0].set_xticks([1, 2, 3, 4], ['1 · Ridge', '2 · 森林', '3 · 森林', '4 · 森林'])
    axes[0].legend(loc='center right', fontsize=9)
    fig.suptitle('四次真实试验的误差变化 · 构造数据平台测试', fontsize=15)
    save(fig, 'improvement')
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.7), layout='constrained')
    for ax, ordinal, title in [(axes[0], 1, '初步 Ridge · 验证 MAE 4.6344'), (axes[1], 4, '最终森林 · 验证 MAE 0.5188')]:
        rows = [row for row in predictions if row['trial'] == ordinal]
        x = [float(row['pressure']) for row in rows]
        ax.scatter(x, [float(row['actual']) for row in rows], s=20, facecolors='none', edgecolors=pale, alpha=.8, label='真实 energy')
        ax.scatter(x, [float(row['prediction']) for row in rows], s=12, color=blue, alpha=.7, label='折外预测')
        ax.set(xlabel='pressure', ylabel='energy', title=title, ylim=(0, 23)); ax.grid(axis='y', alpha=.15); ax.legend(fontsize=9)
    fig.suptitle('相同的 192 条开发样本 · 特征与划分未改变', fontsize=15)
    save(fig, 'prediction-diagnostics')
    fig, ax = plt.subplots(figsize=(10, 4.8), layout='constrained')
    names = ['固定三种模型选最优', 'Codex 改进＋森林调参', 'Optuna 初始小批搜索', 'AutoGluon 轻量配置']
    values = [benchmark['results'][0]['mae'], study['best']['score'], benchmark['results'][1]['mae'], benchmark['results'][2]['mae']]
    bars = ax.barh(names, values, color=[pale, blue, pale, pale], height=.55)
    ax.bar_label(bars, labels=[f'{v:.4f}' for v in values], padding=8)
    ax.set(xlabel='最佳验证 MAE（同数据、同划分；越低越好）', xlim=(0, max(values)*1.22))
    ax.invert_yaxis(); ax.grid(axis='x', alpha=.15); ax.set_axisbelow(True)
    fig.suptitle('历史对照摘要 · 试验次数与耗时口径不同', fontsize=15)
    save(fig, 'benchmark')


if __name__ == '__main__':
    main()
