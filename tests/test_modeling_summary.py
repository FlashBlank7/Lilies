"""Saved modeling summaries expose split sizes without training or changing history."""
import asyncio
from copy import deepcopy

import pytest

from agent_platform.modeling_summary import study_summary
from tests.test_modeling import modeling, setup  # noqa: F401
from tests.test_projects import configured  # noqa: F401


@pytest.mark.parametrize('split,counts', [
    ({}, {}),
    ({'development': None, 'holdout': 'unknown', 'folds': {'count': 3}}, {}),
    ({'development': [], 'holdout': [], 'folds': []},
     {'development_samples': 0, 'holdout_samples': 0, 'folds': 0}),
    ({'development': [0, 1], 'holdout': None, 'folds': []},
     {'development_samples': 2, 'folds': 0}),
])
def test_study_summary_only_counts_saved_arrays(split, counts):
    metadata = {'missing_labels': 1, 'evaluation_label': '仅验证集结果'}
    study = {'id': 's', 'evaluation': {'metric': 'mae'}, 'split': {**metadata, **split}}
    before = deepcopy(study)
    assert study_summary(study)['split'] == {**metadata, **counts}
    assert study == before


def test_http_study_summary_counts_saved_split_without_changing_detail(modeling, monkeypatch):
    (client, app, project, settings), service = modeling

    async def unexpected_compute(*args, **kwargs):
        raise AssertionError('Reading a study summary must not start training')

    monkeypatch.setattr(service, 'compute', unexpected_compute)
    base, dataset, study, candidate = setup(client, project, settings)
    development = list(range(48))
    split = {'samples': list(range(60)), 'development': development, 'holdout': list(range(48, 60)),
             'folds': [[development[16:], development[:16]],
                       [development[:16] + development[32:], development[16:32]],
                       [development[:32], development[32:]]],
             'missing_labels': 0, 'evaluation_label': '保留测试集（尚未使用）'}
    study['split'] = deepcopy(split)
    asyncio.run(service.put(project['id'], 'study', study))
    detail_url = base + '/modeling/studies/' + study['id']
    before = client.get(detail_url).json()

    response = client.get(base + '/modeling/studies?summary=true')
    assert response.status_code == 200, response.text
    summary = next(value for value in response.json() if value['id'] == study['id'])
    assert summary['split'] == {'development_samples': 48, 'holdout_samples': 12, 'folds': 3,
                                'missing_labels': 0, 'evaluation_label': '保留测试集（尚未使用）'}
    assert summary['evaluation'] == study['evaluation']
    assert summary['image'] == study['image']  # Existing HTTP summary fields remain available.
    assert 'request' not in summary
    assert before['split'] == split
    assert client.get(detail_url).json() == before
    assert client.get(base + '/modeling/studies').json() == [before]
    assert client.get(base + '/tasks').json() == []
