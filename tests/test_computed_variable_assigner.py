from __future__ import annotations

import json

import pytest

from agent_platform.workflow_runtime import WorkflowRuntime


def test_computed_assignment_arithmetic_and_collection_reductions() -> None:
    context = {
        "inputs": {},
        "nodes": {
            "opening": {"balance": 206_352},
            "rows": {
                "items": [
                    {
                        "status": "matched",
                        "approved_new_ledger_amount_cents": 0,
                        "mutation_performed": True,
                    },
                    {
                        "status": "new_transaction",
                        "approved_new_ledger_amount_cents": -6_400,
                        "mutation_performed": True,
                    },
                    {
                        "status": "duplicate_input",
                        "approved_new_ledger_amount_cents": 0,
                        "mutation_performed": False,
                    },
                ]
            },
        },
    }
    approved_new = {
        "$sum": {
            "items": {"$ref": {"node_id": "rows", "path": ["items"]}},
            "path": ["approved_new_ledger_amount_cents"],
            "where": {"path": ["mutation_performed"], "equals": True},
        }
    }
    expected = {
        "$add": [
            {"$ref": {"node_id": "opening", "path": ["balance"]}},
            approved_new,
        ]
    }
    assert WorkflowRuntime._resolve_assignment(approved_new, context) == -6_400
    assert WorkflowRuntime._resolve_assignment(expected, context) == 199_952
    assert WorkflowRuntime._resolve_assignment({
        "$equals": [expected, 199_952],
    }, context) is True
    assert WorkflowRuntime._resolve_assignment({
        "$count": {
            "items": {"$ref": {"node_id": "rows", "path": ["items"]}},
            "where": {"path": ["status"], "equals": "duplicate_input"},
        }
    }, context) == 1
    assert WorkflowRuntime._resolve_assignment({
        "$length": {"$ref": {"node_id": "rows", "path": ["items"]}},
    }, context) == 3
    encoded = WorkflowRuntime._resolve_assignment(
        {
            "$json_encode": {
                "notes": "receipt:R-003",
                "category": "67dcbfc5-f75b-4b44-8e88-727242e03ff2",
            }
        },
        context,
    )
    assert json.loads(encoded) == {
        "category": "67dcbfc5-f75b-4b44-8e88-727242e03ff2",
        "notes": "receipt:R-003",
    }


def test_computed_assignment_rejects_unsafe_or_mistyped_expressions() -> None:
    context = {"inputs": {}, "nodes": {}}
    with pytest.raises(TypeError, match=r"\$add values must be numbers"):
        WorkflowRuntime._resolve_assignment({"$add": [1, "2"]}, context)
    with pytest.raises(TypeError, match="collection expression requires an array"):
        WorkflowRuntime._resolve_assignment(
            {"$sum": {"items": {"not": "an array"}, "path": ["amount"]}},
            context,
        )
    with pytest.raises(TypeError, match=r"\$json_encode value must be JSON serializable"):
        WorkflowRuntime._resolve_assignment({"$json_encode": {1, 2}}, context)
