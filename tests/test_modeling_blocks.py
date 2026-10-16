"""Purpose-specific public contracts, legacy drafts and real workflow reuse."""
from copy import deepcopy

import pytest

from agent_platform.blocks import build_block_registry
from agent_platform.workflow_models import NodeSpec
from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_projects import configured, edge, graph, node, ref, start  # noqa: F401


FIELDS = {
    'data_analysis': {'source_path', 'labels_path', 'mapping', 'dataset_id', 'analysis'},
    'feature_extract': {'dataset_id', 'features'},
    'model_train': {'dataset_id', 'features', 'evaluation', 'budget', 'candidate', 'study_id', 'candidate_id', 'finalize'},
    'model_predict': {'source_path', 'labels_path', 'mapping', 'dataset_id', 'model_ref', 'study_id', 'candidate_id', 'slot'},
}
# The old shared schema allowed all these fields on every modeling node.
LEGACY_DEFAULTS = {'source_path': '', 'labels_path': '', 'mapping': {}, 'evaluation': {},
    'budget': {}, 'candidate': {}, 'model_ref': '', 'slot': None, 'dataset_id': '',
    'study_id': '', 'candidate_id': '', 'features': {}, 'finalize': False, 'analysis': {}}


@pytest.mark.parametrize('kind', FIELDS)
def test_catalog_defaults_schema_examples_and_editor_match_each_purpose(kind):
    registry = build_block_registry()
    definition = registry.get(kind)
    assert set(definition.config_schema['properties']) == FIELDS[kind]
    assert set(definition.default_config) <= FIELDS[kind]
    assert all(field['path'].split('.')[0] in FIELDS[kind] for field in definition.editor['fields'])
    manual = registry.manual(kind)
    assert manual['config_schema'] == definition.config_schema
    for example in manual['examples']:
        registry.validate_node(NodeSpec(id='step', title=kind, type=kind, config=example['config']))


@pytest.mark.parametrize('kind', FIELDS)
def test_legacy_defaults_load_without_mutating_the_saved_configuration(kind):
    registry = build_block_registry()
    value = deepcopy(LEGACY_DEFAULTS)
    value['dataset_id'] = ref('$inputs', 'dataset')
    original = deepcopy(value)
    parsed = registry.validate_node(NodeSpec(id='step', title=kind, type=kind, config=value))
    assert parsed.model_dump()['dataset_id'] == original['dataset_id']
    assert set(parsed.model_dump()) == FIELDS[kind]
    assert value == original


@pytest.mark.parametrize('kind, misplaced', [
    ('data_analysis', {'candidate': {'engine': 'sklearn'}}),
    ('feature_extract', {'source_path': 'requirement-package/data.csv'}),
    ('model_train', {'analysis': {'operation': 'spearman'}}),
    ('model_predict', {'budget': {'seconds': 60}}),
    ('model_predict', {'features': {'columns': ['x']}}),
])
def test_configured_options_for_another_purpose_are_not_silently_ignored(kind, misplaced):
    registry = build_block_registry()
    with pytest.raises(ValueError, match=next(iter(misplaced))):
        registry.validate_node(NodeSpec(id='step', title=kind, type=kind, config=misplaced))


@pytest.mark.parametrize('analysis', [
    {'operation': 'unknown'}, {'operation': 'spearman', 'columns': [42, 'x']},
    {'operation': 'outliers', 'lower': 10, 'upper': 0},
])
def test_literal_analysis_errors_remain_invalid_when_references_are_supported(analysis):
    registry = build_block_registry()
    with pytest.raises(ValueError):
        registry.validate_node(NodeSpec(id='step', title='分析', type='data_analysis', config={'analysis': analysis}))


@pytest.mark.parametrize('kind', FIELDS)
def test_empty_modeling_drafts_save_without_runtime_resources(configured, monkeypatch, kind):
    client, app, project, settings = configured
    async def unavailable(*args, **kwargs):
        pytest.fail('Saving a draft must not inspect or start a modeling environment')
    monkeypatch.setattr(app.state.services.modeling, 'image', unavailable)
    monkeypatch.setattr(app.state.services.modeling, 'snapshot_environment', unavailable)
    config = deepcopy(app.state.services.blocks.get(kind).default_config)
    if kind == 'data_analysis':
        config['analysis'] = {'operation': 'spearman', 'columns': []}
    if kind == 'feature_extract':
        config['features'] = {'preprocessing': {'numeric_missing': 'mean', 'categorical_missing': 'preserve', 'outliers': 'iqr_clip', 'outlier_columns': []}}
    graph(client, project['id'], [node('start', 'start'), node('step', kind, **config), node('end', 'end')],
          [edge('start', 'step'), edge('step', 'end')])
    saved = client.get('/api/v1/applications/' + project['id'] + '/draft').json()
    assert next(n for n in saved['snapshot']['workflow']['nodes'] if n['id'] == 'step')['config'] == config


def test_legacy_workflow_analyzes_extracts_trains_and_predicts_new_data(real_compute):
    (client, app, project, settings), _ = real_compute
    pid = project['id']; base = '/api/v1/projects/' + pid
    package = settings.workspace_root / pid / 'requirement-package'
    package.mkdir(exist_ok=True)
    (package / 'train.csv').write_text('x,y\n' + '\n'.join(f'{i},{i * 2}' for i in range(40)))
    (package / 'new.csv').write_text('x\n42\n43\n')

    def legacy(ident, kind, **config):
        return node(ident, kind, **{**deepcopy(LEGACY_DEFAULTS), **config})

    graph(client, pid, [node('start', 'start'),
        legacy('data', 'data_analysis', source_path='requirement-package/train.csv', mapping={'target': 'y'}),
        legacy('features', 'feature_extract', dataset_id=ref('data', 'output', 'dataset_id'), features={'columns': ['x']}),
        legacy('train', 'model_train', dataset_id=ref('features', 'output', 'dataset_id'), features=ref('features', 'output', 'feature_plan'),
               candidate={'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}, evaluation={'holdout_fraction': 0}),
        legacy('predict', 'model_predict', source_path='requirement-package/new.csv', study_id=ref('train', 'output', 'study_id'), candidate_id=ref('train', 'output', 'id')),
        node('end', 'end', outputs={'result': ref('predict', 'output')})],
        [edge('start', 'data'), edge('data', 'features'), edge('features', 'train'), edge('train', 'predict'), edge('predict', 'end')])
    task = wait_task(client, base, start(client, base, 'legacy-modeling', workflow_id=pid))
    assert task['status'] == 'succeeded', task
    result = task['outputs']['result']
    assert result['rows'] == 2
    assert result['model_version']['study_id']
    artifact = client.get(base + '/' + result['artifact'])
    assert artifact.status_code == 200
    assert 'prediction' in artifact.text and len(artifact.text.strip().splitlines()) == 3
    saved = client.get('/api/v1/applications/' + pid + '/draft').json()
    prediction = next(n for n in saved['snapshot']['workflow']['nodes'] if n['id'] == 'predict')
    assert prediction['config']['budget'] == {}  # Reading/running did not rewrite the draft.
