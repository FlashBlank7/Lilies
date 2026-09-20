"""Readable per-trial notes derived from saved computation, without model calls."""
from __future__ import annotations

import csv
from html import escape
import io
import json
import math

MODEL_NAMES = {'linear': '线性模型', 'forest': '随机森林', 'hist_gradient': '梯度提升', 'svm': '支持向量机', 'autogluon': 'AutoGluon'}


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def cell(value):
    if isinstance(value, float):
        return f'{value:.6g}'
    return str(value if value is not None else '未记录').replace('|', '\\|').replace('\n', ' ')


def comparison(study, candidates):
    metric = study['evaluation']['metric']
    trials = [(c, t) for c in candidates for t in c.get('trials', [])]
    trials.sort(key=lambda pair: (pair[1].get('completed_at') or pair[0]['created_at'], pair[0]['id'], pair[1]['slot']))
    rows, best, seconds = [], None, 0
    for c, t in trials:
        score = t.get('metrics', {}).get(metric) if t['status'] == 'completed' else None
        if score is not None:
            best = score if best is None else (min(best, score) if metric in {'mae', 'rmse'} else max(best, score))
        seconds += t.get('seconds', 0)
        rows.append({'sequence': len(rows) + 1, 'candidate_id': c['id'], 'slot': t['slot'], 'model': t['model'],
                     'status': t['status'], 'score': score, 'best_score': best, 'seconds': t.get('seconds'),
                     'cumulative_seconds': seconds, 'completed_at': t.get('completed_at'),
                     'parameters': t.get('parameters', {}), 'hypothesis': c.get('hypothesis', ''),
                     'task_id': t.get('task_id') or c.get('task_id'), 'run_id': t.get('run_id') or c.get('run_id')})
    return rows


def make_note(project_id, dataset, study, candidate, trial, split, candidates, files):
    slot = trial['slot']
    best = study.get('best') or {}
    selected = best.get('candidate_id') == candidate['id'] and best.get('slot') == slot
    gaps = []
    if trial['status'] == 'completed' and not trial.get('effective_model'):
        gaps.append('本次记录没有完整的实际模型参数；仅展示已保存的配置和搜索值，不猜测默认参数。')
    if not trial.get('search'):
        gaps.append('本次记录没有保存搜索分布及采样器配置。')
    if not trial.get('started_at') or not trial.get('completed_at'):
        gaps.append('本次记录没有精确的试验起止时间；历史曲线按候选创建时间和试验序号排列。')
    if split is None:
        gaps.append('固定样本划分文件缺失，无法导出逐样本划分。')
    if trial['status'] == 'completed' and 'validation-predictions.csv' not in files:
        gaps.append('验证预测文件缺失，当前仅能查看已保存的指标。')
    return {'schema_version': 1, 'project_id': project_id, 'study_id': study['id'], 'candidate_id': candidate['id'], 'slot': slot,
            'title': f"{MODEL_NAMES.get(trial['model'], trial['model'])} · 试验 {slot + 1} · 训练笔记",
            'dataset': {k: dataset.get(k) for k in ('id', 'name', 'replaces_id', 'files', 'mapping')},
            'evaluation': study['evaluation'], 'split': split, 'image': candidate['image'],
            'features': candidate['features'], 'feature_descriptions': candidate.get('features_result', []),
            'code_sha256': candidate.get('code_sha256'), 'hypothesis': candidate.get('hypothesis', ''),
            'parent_candidate_id': candidate.get('parent_id'), 'feedback_task_id': candidate.get('feedback_task_id'),
            'task_id': trial.get('task_id') or candidate.get('task_id'), 'run_id': trial.get('run_id') or candidate.get('run_id'),
            'trial': trial, 'requested_parameters': candidate.get('parameters', {}),
            'search_space': candidate.get('search_space', {}), 'autogluon_hyperparameters': candidate.get('autogluon_hyperparameters'),
            'search_strategy': study.get('search_strategy', 'codex'), 'search_decision': candidate.get('search_decision'),
            'test_result': study.get('test_result') if selected else None, 'is_current_best': selected,
            'comparison': comparison(study, candidates), 'budget': study['budget'], 'study_status': study['status'],
            'files': files, 'gaps': gaps,
            'scope': '同研究的固定数据和评价划分；验证指标用于搜索。不同研究不混排，未登记的外部 benchmark 不自动合并。'}


def render_note(note):
    t, rule, split = note['trial'], note['evaluation'], note['split'] or {}
    metric = rule['metric']
    status = {'completed': '已完成', 'failed': '失败，未交付模型'}.get(t['status'], t['status'])
    parts = [f"# {note['title']}", '由平台根据持久化记录自动生成。原始试验不会因后续比较、反馈或编辑而改写。',
             f"状态：{status}。验证 {metric.upper()}：{cell(t.get('metrics', {}).get(metric))}。当前研究最佳：{'是' if note['is_current_best'] else '否'}。",
             '## 本次改动', note['hypothesis'] or '未提供改动说明。',
             f"父候选：{note['parent_candidate_id'] or '无'}；反馈任务：{note['feedback_task_id'] or '无'}。",
             '## 数据与划分', f"数据集：{note['dataset']['name']}（{note['dataset']['id']}）。",
             f"划分方式：{dict(random='随机划分', group='按组隔离', time='时间向前验证').get(rule['split'], rule['split'])}；随机种子：{rule['seed']}；开发样本：{len(split.get('development', [])) if note['split'] is not None else '未记录'}；保留测试：{len(split.get('holdout', [])) if note['split'] is not None else '未记录'}；缺失标签：{split.get('missing_labels', '未记录')}。",
             '划分索引指向 split.json 的 samples；samples 保存源数据行号，不能将时序清洗后的索引直接当作原 CSV 行号。',
             '| 折 | 训练样本 | 验证样本 |\n|---|---:|---:|\n' + '\n'.join(f'| {i+1} | {len(a)} | {len(b)} |' for i, (a, b) in enumerate(split.get('folds', []))),
             '字段映射、数据版本与文件散列：\n\n```json\n' + pretty(note['dataset']) + '\n```',
             '评价规则：\n\n```json\n' + pretty(rule) + '\n```',
             '## 特征与预处理', '训练规则：需要拟合的预处理仅在训练折内拟合；成功模型最终在开发数据上重拟合。',
             '```json\n' + pretty({'plan': note['features'], 'columns': t.get('feature_columns'), 'descriptions': note['feature_descriptions'], 'code_sha256': note['code_sha256']}) + '\n```',
             '## 实际模型与超参数', '实际模型参数来自本次拟合对象；搜索值与完整参数分别保存。' if t['status'] == 'completed' else '训练未成功，未产出可用模型。保留传入参数、搜索值和失败原因。',
             '```json\n' + pretty({'effective_model': t.get('effective_model'), 'requested_parameters': note['requested_parameters'], 'search_space': note.get('search_space'), 'autogluon_hyperparameters': note.get('autogluon_hyperparameters'), 'searched_parameters': t.get('parameters'), 'search': t.get('search')}) + '\n```',
             '## 预测检查', '\n\n'.join(t.get('warnings', [])) or ('各折未发现对不同实测值始终给出相同预测。' if 'diagnostics' in t else '本次没有保存逐折预测检查。'),
             '## 评价结果', '```json\n' + pretty({k: t.get(k) for k in ('metrics', 'baseline', 'fold_metrics', 'diagnostics', 'group_errors', 'importance', 'error')}) + '\n```',
             '模型内部重要性不表示因果关系。',
             '保留测试结果（仅属于被最终选定的这个模型，不参与搜索曲线）：\n\n```json\n' + pretty(note['test_result']) + '\n```' if note['test_result'] else '这个模型没有已保存的最终测试结果；上面的指标为验证结果。',
             '## 同条件模型比较与改进曲线', note['scope'],
             f"指标 {metric} {'越低越好' if metric in {'mae', 'rmse'} else '越高越好'}。curve.svg 按实际试验展示，comparison.csv 保存完整数值；没有逐 epoch 曲线时不生成。耗时累计只相加试验秒数，不含 Agent 等待。",
             '| 序号 | 候选／试验 | 模型 | 状态 | 指标 | 历次最佳 | 秒 |\n|---|---|---|---|---:|---:|---:|\n' + '\n'.join(
                 '| ' + ' | '.join(cell(v) for v in [r['sequence'], f"{r['candidate_id']} / {r['slot'] + 1}", r['model'], r['status'], r['score'], r['best_score'], r['seconds']]) + ' |' for r in note['comparison']),
             '## 运行与环境', '```json\n' + pretty({'project_id': note['project_id'], 'study_id': note['study_id'], 'candidate_id': note['candidate_id'], 'slot': note['slot'], 'task_id': note['task_id'], 'run_id': note['run_id'], 'image': note['image'], 'started_at': t.get('started_at'), 'completed_at': t.get('completed_at'), 'seconds': t.get('seconds'), 'budget': note['budget']}) + '\n```',
             '记录包包含固定划分、可用的验证预测、参数与环境；模型下载包同时包含此笔记。源数据按数据集版本保存，记录包不重复打包源数据。',
             '## 记录缺口', '\n'.join('- ' + gap for gap in note['gaps']) if note['gaps'] else ('本次试验的参数、划分、指标与起止时间均已记录。' if t['status'] == 'completed' else '失败原因、传入参数、划分与起止时间已记录；没有生成成功指标或模型。')]
    if note.get('search_decision'):
        decision = note['search_decision']
        parts[6:6] = ['## 搜索策略',
            f"AIDE 搜索策略 + 本机 Codex：{decision['label']}。分数由平台固定评价器计算。",
            f"选择的父方案：{decision['parent_id'] or '无（独立草案）'}；选择时验证分数：{cell(decision['parent_score'])}。",
            '```json\n' + pretty(decision) + '\n```']
    return '\n\n'.join(parts) + '\n'


def comparison_csv(note):
    buffer = io.StringIO()
    rows = note['comparison']
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, 'parameters': json.dumps(row['parameters'], ensure_ascii=False)})
    return '\ufeff' + buffer.getvalue()


def curve_svg(note):
    rows = note['comparison']
    valid = [r for r in rows if r['score'] is not None and math.isfinite(r['score'])]
    if not valid:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="80"><text x="20" y="40">No completed evaluation</text></svg>'
    low, high = min(r['score'] for r in valid), max(r['score'] for r in valid)
    span = high - low or max(abs(high) * .1, 1)
    low -= span * .1; high += span * .1
    x = lambda r: 85 + 650 * (r['sequence'] - 1) / max(len(rows) - 1, 1)
    y = lambda v: 260 - 210 * (v - low) / (high - low)
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="320" viewBox="0 0 800 320">',
           '<rect width="800" height="320" fill="white"/>',
           f'<text x="85" y="25" fill="#202B3D">{escape(note["evaluation"]["metric"].upper())} · validation / trial</text>']
    for i in range(5):
        value = low + (high-low)*i/4
        svg.append(f'<path d="M85 {y(value)} H750" stroke="#E4EAF2"/><text x="5" y="{y(value)+4}" font-size="12">{value:.4g}</text>')
    points = ' '.join(f'{x(r)},{y(r["best_score"])}' for r in rows if r['best_score'] is not None)
    svg.append(f'<polyline points="{points}" fill="none" stroke="#90A6C5" stroke-dasharray="5 4"/>')
    for r in valid:
        svg.append(f'<circle cx="{x(r)}" cy="{y(r["score"])}" r="4" fill="#3F639F"><title>Trial {r["sequence"]}: {r["score"]}</title></circle>')
    svg.append(f'<text x="85" y="290">1</text><text x="735" y="290">{len(rows)}</text><text x="260" y="310">Dots: trial score · dashed: best so far</text></svg>')
    return ''.join(svg)


def write_bundle(archive, note):
    archive.writestr('training-note.md', render_note(note))
    archive.writestr('training-note.json', pretty(note))
    archive.writestr('comparison.csv', comparison_csv(note))
    archive.writestr('curve.svg', curve_svg(note))
    if note['split'] is not None:
        archive.writestr('split.json', pretty(note['split']))
