from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.capability_generality_gate import (
    CapabilityGeneralityGate,
    CapabilityGeneralityViolation,
)
from tests.test_v04_13_formal_source_promotion import (
    _git,
    _promote,
    _setup_projection,
    _write,
)


PROJECT_MANIFESTS = {
    "EXP-LILIES-001": {
        "project_id": "EXP-LILIES-001",
        "host_projects": [
            {
                "name": "paperless-ngx",
                "repository": "https://github.com/paperless-ngx/paperless-ngx",
            },
            {
                "name": "InvenTree",
                "repository": "https://github.com/inventree/InvenTree",
            },
        ],
    },
    "EXP-LILIES-004": {
        "project_id": "EXP-LILIES-004",
        "host_projects": [
            {
                "name": "ThingsBoard Community Edition",
                "repository": "https://github.com/thingsboard/thingsboard",
            }
        ],
    },
}


def _gate() -> CapabilityGeneralityGate:
    return CapabilityGeneralityGate.from_project_manifests(PROJECT_MANIFESTS)


def test_platform_source_rejects_paperless_and_inventree_implementations() -> None:
    result = _gate().inspect_delta(
        {
            "platform/backend/src/agent_platform/standard_connector_catalog.py": (
                None,
                (
                    'PAPERLESS_OPERATIONS = {"paperless.documents": "/api/documents/"}\n'
                    "class InvenTreeFieldMapping:\n"
                    '    fields = {"supplier": "company"}\n'
                ).encode(),
            )
        }
    )

    assert result.passed is False
    assert {
        (finding.project_id, finding.marker, finding.path, finding.reason)
        for finding in result.findings
    } >= {
        (
            "EXP-LILIES-001",
            "paperless",
            "platform/backend/src/agent_platform/standard_connector_catalog.py",
            "host_marker_in_platform_product_source",
        ),
        (
            "EXP-LILIES-001",
            "inventree",
            "platform/backend/src/agent_platform/standard_connector_catalog.py",
            "host_marker_in_platform_product_source",
        ),
    }
    paperless = next(
        finding
        for finding in result.findings
        if finding.marker == "paperless"
    )
    assert paperless.line == 1
    assert "catalog" in paperless.matched_constructs
    assert "operation" in paperless.matched_constructs
    assert "path=platform/backend/src/agent_platform/standard_connector_catalog.py:1" in (
        paperless.public_detail
    )


def test_another_project_host_wrapper_is_rejected() -> None:
    result = _gate().inspect_delta(
        {
            "platform/frontend/lib/connector-wrapper.ts": (
                None,
                (
                    "export const thingsboardWrapper = "
                    "(payload: unknown) => ({ topic: 'v1/devices/me/telemetry', payload });\n"
                ).encode(),
            )
        }
    )

    finding = next(
        finding
        for finding in result.findings
        if finding.marker == "thingsboard"
    )
    assert finding.project_id == "EXP-LILIES-004"
    assert finding.reason == "host_marker_in_platform_product_source"
    assert "wrapper" in finding.matched_constructs


def test_generic_builder_runner_cannot_embed_project_host_branch() -> None:
    result = _gate().inspect_delta(
        {
            "scripts/run_v04_13_codex_builder.py": (
                b"def run_registered_operation(operation):\n    return operation()\n",
                (
                    "def run_registered_operation(operation):\n"
                    "    if operation == 'inventree.purchase_orders':\n"
                    "        return inventree_wrapper(operation)\n"
                    "    return operation()\n"
                ).encode(),
            )
        }
    )

    finding = next(
        finding
        for finding in result.findings
        if finding.marker == "inventree"
    )
    assert finding.reason == "host_marker_in_generic_builder_runner"
    assert finding.path == "scripts/run_v04_13_codex_builder.py"
    assert "wrapper" in finding.matched_constructs


def test_neutral_generic_connector_source_passes() -> None:
    result = _gate().inspect_delta(
        {
            "platform/backend/src/agent_platform/connector_registry.py": (
                b"REGISTRY_VERSION = 1\n",
                (
                    "REGISTRY_VERSION = 2\n\n"
                    "class OpenApiConnectorRegistry:\n"
                    "    def register(self, specification, secret_reference):\n"
                    "        return self.validate(specification, secret_reference)\n"
                ).encode(),
            )
        }
    )

    assert result.passed is True
    assert result.findings == ()


def test_project_configuration_and_experiment_data_may_name_hosts() -> None:
    result = _gate().inspect_delta(
        {
            "docs/experiments/lilies-collaboration/EXP-LILIES-001/project.json": (
                None,
                b'{"hosts":["paperless-ngx","InvenTree"]}\n',
            ),
            "scripts/experiments/exp_lilies_001/environment_control.py": (
                None,
                b"PAPERLESS_URL = 'http://paperless.local'\n",
            ),
        }
    )

    assert result.passed is True


def test_prebuilt_final_graph_is_rejected_from_capability_delta() -> None:
    result = _gate().inspect_delta(
        {
            "platform/backend/src/agent_platform/generated_repair.py": (
                None,
                (
                    "FINAL_WORKFLOW = WorkflowSpec(\n"
                    "    nodes=[{'id': 'extract'}],\n"
                    "    edges=[],\n"
                    ")\n"
                ).encode(),
            )
        }
    )

    assert [
        (finding.marker, finding.reason)
        for finding in result.findings
    ] == [
        (
            "final_workflow",
            "prebuilt_final_workflow_in_capability_source",
        )
    ]


def test_formal_promotion_rejects_before_git_or_intent_mutation(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    coordinator = context["coordinator"]
    coordinator._promotion_generality_gate = _gate()
    source_path = (
        context["workspace"]
        / "source/platform/backend/src/agent_platform/generic.py"
    )
    _write(
        source_path,
        (
            "VALUE = 2\n"
            "def paperless_adapter(payload):\n"
            "    return {'document': payload}\n"
        ),
    )

    with pytest.raises(
        CapabilityGeneralityViolation,
        match=(
            "path=platform/backend/src/agent_platform/generic.py:2, "
            "project=EXP-LILIES-001, marker=paperless"
        ),
    ):
        _promote(context)

    assert _git(context["repository"], "rev-parse", "HEAD") == context["baseline"]
    intent_path = (
        context["state_root"]
        / "assignments"
        / str(context["assignment_id"])
        / "promotions"
        / str(context["response_id"])
        / "intent.json"
    )
    assert not intent_path.exists()
