from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_scripts_use_frozen_historical_manifests() -> None:
    offenders = []
    for path in sorted((ROOT / "scripts").glob("v03_*.py")):
        text = path.read_text(encoding="utf-8")
        if "docs/testing/regression_lanes.json" in text:
            offenders.append(path.name)
        if 'ROOT / "docs" / "testing" / "regression_lanes.json"' in text:
            offenders.append(path.name)

    assert offenders == []
    assert (ROOT / "docs/testing/historical/v0.3.55_regression_lanes.json").exists()
    assert (ROOT / "docs/testing/historical/v0.3.56_regression_lanes.json").exists()


def test_v03_script_defaults_cannot_pollute_active_workingon() -> None:
    checked = []
    for path in sorted((ROOT / "scripts").glob("v03_*.py")):
        default_line = next(
            (
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("DEFAULT_OUTPUT = ")
            ),
            "",
        )
        if not default_line:
            continue
        patch_version = path.name.split("_", 2)[1]
        checked.append(path.name)
        assert '"workingon"' not in default_line
        assert (
            f'ROOT / ".tmp" / "historical-evidence" / "v0.3.{patch_version}"'
            in default_line
        )

    assert checked


def test_archived_conflict_manifest_is_exact_and_evidence_backed() -> None:
    manifest = json.loads((ROOT / "docs/testing/regression_lanes.json").read_text(encoding="utf-8"))
    diagnostic = next(lane for lane in manifest["lanes"] if lane["id"] == "full_historical_diagnostic")
    nodeids = [
        nodeid
        for family in diagnostic["known_conflict_families"]
        for nodeid in family["failure_nodeids"]
    ]

    assert len(nodeids) == 85
    assert len(nodeids) == len(set(nodeids))
    assert {family["classification"] for family in diagnostic["known_conflict_families"]} == {
        "archived_expectation_conflict"
    }
    for family in diagnostic["known_conflict_families"]:
        assert family["reason"].strip()
        assert family["current_behavior_evidence"]
        assert all((ROOT / path).exists() for path in family["current_behavior_evidence"])


def test_pytest_policy_uses_strict_expected_failures() -> None:
    module = load_module("tests/conftest.py", "v043_conftest_under_test")
    conflicts = module.archived_expectation_conflicts()
    source = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")

    assert len(conflicts) == 85
    assert "strict=True" in source
    assert set(conflicts.values()) == {
        "complexity_router_default_timeline",
        "draft_patch_preview_support_timeline",
        "mixed_studio_governance_surface_superseded",
        "embedded_try_run_surface_superseded",
        "embedded_monitor_and_quick_actions_superseded",
        "persona_specific_customer_runtime_superseded",
        "markdown_result_mount_location_superseded",
    }


def test_junit_inventory_separates_known_and_current_failures(tmp_path: Path) -> None:
    module = load_module(
        "scripts/v04_03_regression_time_boundary.py",
        "v043_regression_inventory_under_test",
    )
    junit = tmp_path / "result.xml"
    junit.write_text(
        """<?xml version="1.0"?>
<testsuites><testsuite tests="3" failures="2" errors="0" skipped="1">
  <testcase classname="tests.test_v02_75_complexity_router_default_enablement_boundary" name="test_v02_75_decision_requires_live_validation_before_default_change"><failure message="old default"/></testcase>
  <testcase classname="tests.test_workflow" name="test_unknown_current_behavior"><failure message="regression"/></testcase>
  <testcase classname="tests.test_v02_120_e08_draft_patch_preview_worker_offload_handler" name="test_v02_120_draft_patch_preview_worker_handler_fails_unsupported"><skipped type="pytest.xfail" message="archived expectation conflict: draft_patch_preview_support_timeline"/></testcase>
</testsuite></testsuites>
""",
        encoding="utf-8",
    )

    result = module.classify(junit)

    assert result["stage"] == "v0.4.8"
    assert result["classification_counts"] == {
        "archived_expectation_conflict": 1,
        "current_regression": 1,
    }
    assert result["blocking_current_regressions"] == [
        "tests/test_workflow.py::test_unknown_current_behavior"
    ]
    assert result["expected_conflict_count"] == 1
    assert result["expected_conflict_family_counts"] == {
        "draft_patch_preview_support_timeline": 1
    }
    assert result["unknown_expected_conflicts"] == []


def test_current_gate_declares_existing_tests_and_exact_source() -> None:
    manifest = json.loads((ROOT / "docs/testing/regression_lanes.json").read_text(encoding="utf-8"))
    gate = next(lane for lane in manifest["lanes"] if lane["id"] == manifest["policy"]["current_gate"])

    assert manifest["version"] == "v0.4.8"
    assert manifest["source_stage_report"].endswith(
        "v0.4.8_evaluation_harness_profiles_and_environments.md"
    )
    assert gate["status"] == "gating"
    assert gate["test_files"]
    assert all((ROOT / nodeid.split("::", 1)[0]).exists() for nodeid in gate["test_files"])
    assert all(nodeid in gate["command"] for nodeid in gate["test_files"])


def test_v048_runtime_version_progresses_without_breaking_prior_v04_evidence() -> None:
    from agent_platform import __version__

    historical_check = (ROOT / "scripts/v04_00_ai_requirement_intake.py").read_text(
        encoding="utf-8"
    )

    assert __version__ == "v0.4.8"
    assert "runtime_version = re.search" in historical_check
    assert "int(runtime_version.group(1)) >= 1" in historical_check
