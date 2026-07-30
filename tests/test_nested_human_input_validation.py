from agent_platform.applications import ApplicationService
from agent_platform.workflow_models import (
    ApplicationSnapshot,
    NodeSpec,
    WorkflowSpec,
    WorkflowTestCase,
)


def _snapshot(
    simulated_human_inputs: dict[str, dict[str, object]],
) -> ApplicationSnapshot:
    nested = WorkflowSpec(
        nodes=[
            NodeSpec(
                id="nested_review",
                type="human_input",
                title="Nested review",
                config={
                    "fields": [
                        {
                            "name": "decision",
                            "label": "Decision",
                            "type": "string",
                            "required": True,
                        }
                    ]
                },
            )
        ]
    )
    return ApplicationSnapshot(
        name="Nested human validation",
        description="",
        requirement="Validate simulated inputs in nested public workflows.",
        workflow=WorkflowSpec(
            nodes=[
                NodeSpec(
                    id="iterate",
                    type="iteration",
                    title="Iterate",
                    config={"workflow": nested.model_dump(mode="json")},
                )
            ]
        ),
        tests=[
            WorkflowTestCase(
                id="nested-human",
                name="Nested human",
                requirement="Inject the nested review decision.",
                simulated_human_inputs=simulated_human_inputs,
            )
        ],
    )


def test_simulated_human_input_validation_discovers_nested_workflow_nodes() -> None:
    service = object.__new__(ApplicationService)

    assert service._validate_simulated_human_inputs(
        _snapshot({"nested_review": {"decision": "hold"}})
    ) == []


def test_simulated_human_input_validation_still_fails_closed() -> None:
    service = object.__new__(ApplicationService)

    assert service._validate_simulated_human_inputs(
        _snapshot({"unknown_review": {"decision": "hold"}})
    ) == [
        "test nested-human simulated human input references unknown node: "
        "unknown_review"
    ]
    assert service._validate_simulated_human_inputs(
        _snapshot({"nested_review": {}})
    ) == [
        "test nested-human simulated human input for nested_review "
        "is missing required fields: ['decision']"
    ]
