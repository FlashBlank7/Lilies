import asyncio
import json

import pytest
from jsonschema.exceptions import SchemaError

from agent_platform.blocks import LLMConfig, build_block_registry
from agent_platform.models import Usage
from agent_platform.workflow_models import ApplicationSnapshot, NodeSpec
from agent_platform.workflow_runtime import NodeRun, WorkflowRuntime


SCHEMA = {
    "type": "object",
    "properties": {
        "requests": {"type": "array", "maxItems": 2, "items": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        }},
        "done": {"type": "boolean"},
    },
    "required": ["requests", "done"],
    "additionalProperties": False,
}


def fake_runtime(responses):
    runtime = object.__new__(WorkflowRuntime)
    runtime.runtime_model = "test"
    runtime.blocks = build_block_registry()
    calls, events = [], []

    async def model(*args, **kwargs):
        calls.append(args)
        return responses[min(len(calls) - 1, len(responses) - 1)], Usage()

    async def emit(*args):
        events.append(args)

    runtime._model_text, runtime._emit = model, emit
    return runtime, calls, events


def run_node(runtime, tmp_path, schema=SCHEMA):
    config = LLMConfig(prompt="Review", structured_output=schema)
    run = NodeRun(
        snapshot=ApplicationSnapshot(), node=NodeSpec(id="review", type="llm", title="Review"),
        config=config, context={"inputs": {}, "nodes": {}}, inputs={}, outputs={},
        workspace_path=str(tmp_path), run_id="run", scoped_id="review", state=None,
    )
    return asyncio.run(runtime._exec_l_l_m(run))


@pytest.mark.parametrize("response", [
    '<calls><invoke>{"path":"code.java"}</invoke></calls>',
    '{"requests":[],"done":"true"}',
    '{"requests":[{"path":7}],"done":false}',
    '{"requests":[],"done":true,"unexpected":1}',
    '{"requests":[{"path":"a"},{"path":"b"},{"path":"c"}],"done":false}',
])
def test_llm_rejects_json_that_does_not_match_declared_output(response, tmp_path):
    runtime, _, _ = fake_runtime([response])
    with pytest.raises(ValueError, match="structured output schema") as error:
        run_node(runtime, tmp_path)
    assert WorkflowRuntime._acceptance_failure_code(str(error.value)) == "structured_output_invalid"


def test_invalid_llm_output_retries_before_returning_to_downstream(tmp_path):
    expected = {"requests": [], "done": True}
    runtime, calls, events = fake_runtime(['{"path":"code.java"}', json.dumps(expected)])
    node = NodeSpec(id="review", type="llm", title="Review", config={
        "prompt": "Review", "structured_output": SCHEMA,
    }, retry={"enabled": True, "max_attempts": 2, "delay_seconds": 0})
    result = asyncio.run(runtime._execute_with_retry(
        ApplicationSnapshot(), node, {}, {}, str(tmp_path), "run", "review", None,
    ))
    assert result["structured"] == expected
    assert len(calls) == 2
    assert any(event[1] == "node.retry" for event in events)


def test_llm_output_supports_local_references_and_unions(tmp_path):
    schema = {"$defs": {"answer": {"type": "integer", "minimum": 1}}, "anyOf": [
        {"$ref": "#/$defs/answer"}, {"type": "null"},
    ]}
    runtime, _, _ = fake_runtime(["3"])
    assert run_node(runtime, tmp_path, schema)["structured"] == 3
    runtime, _, _ = fake_runtime(["0"])
    with pytest.raises(ValueError, match="structured output schema"):
        run_node(runtime, tmp_path, schema)


def test_plain_llm_output_remains_text(tmp_path):
    runtime, _, _ = fake_runtime(["A normal answer."])
    result = run_node(runtime, tmp_path, None)
    assert result["text"] == "A normal answer."
    assert "structured" not in result


def test_invalid_schema_fails_before_model_call(tmp_path):
    runtime, calls, _ = fake_runtime(["{}"])
    with pytest.raises(SchemaError, match="not-a-json-type"):
        run_node(runtime, tmp_path, {"type": "not-a-json-type"})
    assert calls == []


def test_output_schema_cannot_fetch_external_references(tmp_path, monkeypatch):
    import urllib.request
    from referencing.exceptions import Unresolvable

    fetches = []

    def fetch(*args, **kwargs):
        fetches.append(args)
        raise AssertionError("schema validation must not fetch external resources")

    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    runtime, _, _ = fake_runtime(["{}"])
    with pytest.raises(Unresolvable):
        run_node(runtime, tmp_path, {"$ref": "https://example.invalid/schema.json"})
    assert fetches == []
