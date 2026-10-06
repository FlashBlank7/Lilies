"""The input manual's example must accept real inputs and expose its fields."""
import jsonschema

from tests.test_projects import configured, edge, graph, node, ref  # noqa: F401


def test_input_manual_example_runs_with_defaults_explicit_values_and_missing_fields(configured):
    client, _, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    response = client.post(base + '/agent-tools', json={
        'name': 'block_catalog', 'arguments': {'block_type': 'start'}})
    assert response.status_code == 200, response.text
    manual = response.json()['manual']
    config = manual['examples'][0]['config']
    jsonschema.validate(config, manual['config_schema'])
    graph(client, pid, [node('start', 'start', **config), node('end', 'end', outputs={
        'source': ref('start', 'source'), 'batch_size': ref('start', 'batch_size')})], [edge('start', 'end')])
    for inputs, expected in [({'source': 'sample.csv'}, {'source': 'sample.csv', 'batch_size': 20}),
                             ({'source': 'other.csv', 'batch_size': 0}, {'source': 'other.csv', 'batch_size': 0})]:
        run = client.post(base + '/agent-tools', json={
            'name': 'workflow_run', 'arguments': {'action': 'start', 'inputs': inputs}})
        assert run.status_code == 200, run.text
        assert run.json()['status'] == 'succeeded' and run.json()['outputs'] == expected
    missing = client.post(base + '/agent-tools', json={
        'name': 'workflow_run', 'arguments': {'action': 'start', 'inputs': {}}})
    assert missing.status_code == 200, missing.text
    assert missing.json()['status'] == 'failed' and 'source' in missing.json()['error']
