"""Reference errors suggest actual paths without blaming the wrong node."""
from copy import deepcopy
import json

import pytest

from agent_platform.workflow_runtime import WorkflowRuntime, WorkflowReferenceResolutionError


def _reference(path):
    return {'$ref': {'node_id': 'start', 'path': path}}


def _resolve_failure(path, context):
    with pytest.raises(WorkflowReferenceResolutionError) as caught:
        WorkflowRuntime._resolve(_reference(path), context)
    return str(caught.value)


CONTEXT = {'nodes': {'start': {'output': {'sales_records': [{'amount': 1}]}}}}


@pytest.mark.parametrize('value', [[{'amount': 1}], [], 0, None])
def test_wrapped_field_is_found_and_only_changing_reference_resolves(value):
    context = {'nodes': {'start': {'output': {'rows': value}, 'logs': ''}}}
    original = deepcopy(context)
    message = _resolve_failure(['rows'], context)
    assert "node='start'" in message and "path=['rows']" in message
    assert '["output", "rows"]' in message
    assert '先改引用方' in message
    assert '修不好' not in message and '根本没产出' not in message
    assert WorkflowRuntime._resolve(_reference(['output', 'rows']), context) == value
    assert context == original


def test_exact_wrapped_path_is_found_beyond_display_limit_and_through_arrays():
    output = {f'other_{index}': index for index in range(12)}
    output['records'] = [{'rows': ['private-row-value']}]
    context = {'nodes': {'start': {'output': output}}}
    message = _resolve_failure(['records', 0, 'rows'], context)
    corrected = ['output', 'records', 0, 'rows']
    assert json.dumps(corrected) in message
    assert '先改引用方' in message and 'private-row-value' not in message
    assert WorkflowRuntime._resolve(_reference(corrected), context) == ['private-row-value']


@pytest.mark.parametrize('field', ['带"引号的字段', 'literal.field'])
def test_suggested_paths_escape_real_field_names(field):
    context = {'nodes': {'start': {'output': {field: [1]}}}}
    message = _resolve_failure([field], context)
    corrected = ['output', field]
    assert json.dumps(corrected, ensure_ascii=False) in message
    assert WorkflowRuntime._resolve(_reference(corrected), context) == [1]


@pytest.mark.parametrize('path', [['output', 'sales'], ['unrelated_field']])
def test_failure_names_requested_and_available_paths_without_repeating_advice(path):
    message = _resolve_failure(path, CONTEXT)
    assert 'start' in message and path[-1] in message
    assert '["output", "sales_records"]' in message
    sentences = [s.strip() for s in message.split('。') if len(s.strip()) > 12]
    assert len(sentences) == len(set(sentences))
    assert '修不好' not in message


def test_near_miss_suggests_reference_correction():
    message = _resolve_failure(['output', 'sales'], CONTEXT)
    assert '先改引用方' in message
    assert WorkflowRuntime._resolve(_reference(['output', 'sales_records']), CONTEXT) == [{'amount': 1}]


@pytest.mark.parametrize('output', [{'sales_records': []}, {}])
def test_missing_field_requires_inspection_before_changing_producer(output):
    message = _resolve_failure(['unrelated_field'], {'nodes': {'start': {'output': output}}})
    assert 'unrelated_field' in message
    assert '核对字段名称和节点的完整输出' in message
    assert '若所需字段确实未产出' in message
    assert '根本没产出' not in message and '修不好' not in message
