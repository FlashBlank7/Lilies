"""An existing pipeline is bound immutably and uses the shared prediction service."""
import csv
import io

from tests.test_projects import configured, start  # noqa: F401
from tests.test_modeling import modeling, real_compute, setup, wait_task  # noqa: F401


def test_real_pipeline_import_prediction_and_version_isolation(real_compute):
    (client, app, project, settings), service = real_compute
    base, dataset, study, candidate = setup(client, project, settings)
    task = wait_task(client, base, start(client, base, 'train'))
    assert task['status'] == 'succeeded', task.get('error')
    model_file = service.path(project['id'], candidate['id']) / 'output/trial-0/model.joblib'
    uploaded = client.post(base + '/materials', files={'file': ('model.joblib', model_file.read_bytes(), 'application/octet-stream')}).json()
    environments = client.get(base + '/model-environments').json()
    assert 'lilies-modeling:20260922-acceptance' in environments
    request = {'name': '已有模型', 'source_path': uploaded['path'], 'environment': 'lilies-modeling:20260922-acceptance', 'mapping': dataset['mapping']}
    imported = client.post(base + '/models/imported/import', json=request)
    assert imported.status_code == 201, imported.text
    first = imported.json()['value']['import_id']
    assert client.get(base + '/models').json()[0]['status'] == 'ready'
    predict = client.post(base + '/models/imported/predict', json={'dataset_id': dataset['id'], 'request_key': 'predict-old'})
    assert predict.status_code == 202, predict.text
    old = wait_task(client, base, predict.json())
    assert old['status'] == 'succeeded', old.get('error')
    assert old['outputs']['model_version']['import_id'] == first
    assert old['outputs']['rows'] == 60
    assert old['outputs']['input_source']['id_column'] == 'id'
    assert old['outputs']['input_source']['dataset_id'] == dataset['id']
    # Failed binding is local to the import and leaves the last usable version.
    wrong = client.post(base + '/models/imported/import', json={**request, 'expected_revision': 1, 'feature_columns': ['wrong']})
    assert wrong.status_code == 422, wrong.text
    assert client.get(base + '/models').json()[0]['import_id'] == first
    assert client.post(base + '/models/imported/import', json=request).status_code == 409
    newer = client.post(base + '/models/imported/import', json={**request, 'expected_revision': 1})
    assert newer.status_code == 201, newer.text
    second = newer.json()['value']['import_id']
    assert first != second
    assert client.get(base + '/tasks/' + old['id']).json()['outputs']['model_version']['import_id'] == first
    fresh = client.post(base + '/models/imported/predict', json={'dataset_id': dataset['id'], 'request_key': 'predict-new'})
    new = wait_task(client, base, fresh.json())
    assert new['status'] == 'succeeded'
    assert new['outputs']['model_version']['import_id'] == second
    assert new['outputs']['preview'] == old['outputs']['preview']
    # Use the same real saved preprocessing pipeline without an ID mapping.
    # CSV physical lines (blank lines / multiline cells) and XLSX worksheet
    # positions must not be confused with parsed input record numbers.
    record_import = client.post(base + '/models/records/import', json={**request,
        'mapping': {**dataset['mapping'], 'id_column': '', 'sheet': '预测清单'}})
    assert record_import.status_code == 201, record_import.text
    csv_bytes = b'id,x,category,y,note\n001,1,a,,"two\nlines"\n\n002,,b,,missing\n003,3,a,,last\n'
    from openpyxl import Workbook
    book = Workbook(); book.active.title = 'other'; samples = book.create_sheet('预测清单')
    for row in [('id', 'x', 'category', 'y', 'note'), ('001', 1, 'a', None, 'two\nlines'),
                ('002', None, 'b', None, 'missing'), ('003', 3, 'a', None, 'last')]:
        samples.append(row)
    stream = io.BytesIO(); book.save(stream)
    predictions = []
    for extension, content in [('csv', csv_bytes), ('xlsx', stream.getvalue())]:
        data = client.post(base + '/datasets/upload', files={'file': ('records.' + extension, content, 'application/octet-stream')}).json()
        requested = client.post(base + '/models/records/predict', json={'dataset_id': data['id'], 'request_key': 'records-' + extension})
        assert requested.status_code == 202, requested.text
        predicted = wait_task(client, base, requested.json())
        assert predicted['status'] == 'succeeded', predicted.get('error')
        output = predicted['outputs']
        assert output['rows'] == 3
        assert [row['input_record'] for row in output['preview']] == [1, 2, 3]
        assert output['input_source'] == {'dataset_id': data['id'], 'path': data['files']['source']['original'],
                                         'table': 'source', 'record_column': 'input_record',
                                         **({'sheet': '预测清单'} if extension == 'xlsx' else {})}
        download = client.get(base + '/' + output['artifact'])
        assert download.status_code == 200
        records = list(csv.DictReader(io.StringIO(download.text)))
        assert [row['input_record'] for row in records] == ['1', '2', '3']
        assert [float(row['prediction']) for row in records] == [row['prediction'] for row in output['preview']]
        predictions.append(output['preview'])
        assert client.get(base + '/tasks/' + predicted['id']).json()['outputs']['input_source'] == output['input_source']
        assert (service.path(project['id'], data['id']) / data['files']['source']['name']).read_bytes() == content
    assert predictions[0] == predictions[1]
    # Runtime verifies bytes; existence alone does not make a package usable.
    (service.path(project['id'], second) / 'model.joblib').write_bytes(b'changed')
    failure = client.post(base + '/models/imported/predict', json={'dataset_id': dataset['id'], 'request_key': 'changed'})
    failed = wait_task(client, base, failure.json())
    assert failed['status'] == 'failed' and '内容改变' in failed['error']


def test_import_rejects_foreign_sources_without_rebinding(configured):
    client, app, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/models/test/import', json={'name': '模型', 'source_path': '../foreign/model.pkl', 'environment': 'lilies-modeling:20260922-acceptance'})
    assert response.status_code == 422
    assert client.get(base + '/models').json() == []
