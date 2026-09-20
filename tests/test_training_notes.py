from agent_platform.training_notes import make_note, render_note, comparison, curve_svg


def test_comparison_keeps_failed_and_worse_models_and_metric_direction():
    study = {'evaluation': {'metric': 'accuracy'}}
    candidates = [
        {'id': 'later', 'created_at': '2026-09-15', 'trials': [{'slot': 0, 'model': 'forest', 'status': 'failed', 'seconds': 2}, {'slot': 1, 'model': 'forest', 'status': 'completed', 'metrics': {'accuracy': .7}, 'seconds': 3}]},
        {'id': 'first', 'created_at': '2026-09-14', 'trials': [{'slot': 0, 'model': 'linear', 'status': 'completed', 'metrics': {'accuracy': .9}, 'seconds': 1}]},
    ]
    rows = comparison(study, candidates)
    assert [r['score'] for r in rows] == [.9, None, .7]
    assert [r['best_score'] for r in rows] == [.9, .9, .9]
    assert [r['cumulative_seconds'] for r in rows] == [1, 3, 6]
    assert 'Trial 3: 0.7' in curve_svg({'comparison': rows, 'evaluation': study['evaluation']})


def test_legacy_note_discloses_missing_fields_and_never_borrows_another_model_test():
    trial = {'slot': 0, 'model': 'linear', 'status': 'completed', 'metrics': {'mae': 2}, 'seconds': 1}
    candidate = {'id': 'c', 'created_at': '2026-09-14', 'image': 'original-image', 'features': {}, 'trials': [trial]}
    study = {'id': 's', 'evaluation': {'metric': 'mae', 'split': 'random', 'seed': 42}, 'budget': {}, 'status': 'sealed', 'best': {'candidate_id': 'different', 'slot': 0}, 'test_result': {'metrics': {'mae': .01}}}
    dataset = {'id': 'd', 'name': '历史数据'}
    note = make_note('p', dataset, study, candidate, trial, None, [candidate], [])
    assert note['test_result'] is None
    assert len(note['gaps']) == 5
    assert note['trial'] == trial
    assert '不猜测默认参数' in render_note(note)
    assert '尚无结果' not in render_note(note).split('## 本次改动')[0]
