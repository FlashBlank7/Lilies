from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_validator() -> Any:
    module_path = ROOT / "scripts" / "validate_real_project_portfolio.py"
    spec = importlib.util.spec_from_file_location(
        "real_project_portfolio_validator_under_test", module_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def copy_validation_fixture(tmp_path: Path) -> Path:
    collaboration = Path("docs/experiments/lilies-collaboration")
    target = tmp_path / collaboration
    target.parent.mkdir(parents=True)
    shutil.copytree(ROOT / collaboration, target)
    evolution = tmp_path / "docs/evolution-control"
    (evolution / "stage-contracts").mkdir(parents=True)
    shutil.copy2(
        ROOT / "docs/evolution-control/report_intents.json",
        evolution / "report_intents.json",
    )
    shutil.copy2(
        ROOT / "docs/evolution-control/stage-contracts/v0.4.13-r3.json",
        evolution / "stage-contracts/v0.4.13-r3.json",
    )
    return tmp_path


def rewrite_json(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def refresh_manifest_locks(fixture: Path, module: Any) -> None:
    portfolio_path = fixture / module.PORTFOLIO_PATH
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    manifest_bytes: list[bytes] = []
    for member in portfolio["projects"]:
        manifest_path = fixture / member["manifest"]
        content = manifest_path.read_bytes()
        manifest_bytes.append(content)
        manifest = json.loads(content)
        member["manifest_sha256"] = hashlib.sha256(content).hexdigest()
        member["latest_revision"] = manifest["latest_revision"]
        member["status"] = manifest["status"]
    portfolio["member_manifest_set_sha256"] = hashlib.sha256(
        b"".join(manifest_bytes)
    ).hexdigest()
    portfolio_path.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_current_real_project_portfolio_is_valid() -> None:
    module = load_validator()

    assert module.validate_portfolio() == []


def test_twelve_revisions_are_one_project_not_twelve_projects() -> None:
    portfolio = json.loads(
        (
            ROOT
            / "docs/experiments/lilies-collaboration/portfolio-v04-13-t01h.json"
        ).read_text(encoding="utf-8")
    )
    first = portfolio["projects"][0]

    assert portfolio["required_project_count"] == 6
    assert len(portfolio["projects"]) == 6
    assert first["project_id"] == "EXP-LILIES-001"
    assert first["latest_revision"] == 12
    assert [item["project_id"] for item in portfolio["projects"]] == [
        f"EXP-LILIES-{number:03d}" for number in range(1, 7)
    ]


def test_portfolio_rejects_duplicate_project_members(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    path = fixture / module.PORTFOLIO_PATH

    rewrite_json(
        path,
        lambda payload: payload["projects"].__setitem__(
            5, dict(payload["projects"][0])
        ),
    )

    errors = module.validate_portfolio(fixture)

    assert any("EXP-LILIES-001 through EXP-LILIES-006" in error for error in errors)
    assert "portfolio project IDs are not unique" in errors


def test_portfolio_rejects_non_http_protocol_reordering(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    path = fixture / module.PORTFOLIO_PATH

    rewrite_json(
        path,
        lambda payload: payload["protocol_sequence"].reverse(),
    )

    errors = module.validate_portfolio(fixture)

    assert any("Home Assistant WebSocket" in error for error in errors)


def test_capability_lane_cannot_enter_customer_denominator(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    path = fixture / module.GAP_PATH

    rewrite_json(
        path,
        lambda payload: payload.__setitem__("enterprise_denominator", True),
    )

    errors = module.validate_portfolio(fixture)

    assert "platform capability lane must remain outside enterprise denominator" in errors


def test_later_project_cannot_start_before_previous_project_passes(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    project_path = (
        fixture
        / "docs/experiments/lilies-collaboration/EXP-LILIES-002/project.json"
    )

    def start_project(payload: dict[str, Any]) -> None:
        payload["status"] = "active"
        payload["latest_revision"] = 0

    rewrite_json(project_path, start_project)
    refresh_manifest_locks(fixture, module)

    errors = module.validate_portfolio(fixture)

    assert "EXP-LILIES-002 started before every previous project passed" in errors
    assert "active project ID and project statuses are inconsistent" in errors


def test_replacement_project_requires_baseline_compatibility_and_rollback(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    project_path = (
        fixture
        / "docs/experiments/lilies-collaboration/EXP-LILIES-003/project.json"
    )

    rewrite_json(
        project_path,
        lambda payload: payload.pop("replacement_contract"),
    )
    refresh_manifest_locks(fixture, module)

    errors = module.validate_portfolio(fixture)

    assert "EXP-LILIES-003 replacement has no compatibility contract" in errors


def test_capability_entry_requires_bound_origin_review_and_rerun(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    gap_path = fixture / module.GAP_PATH

    def add_malformed_entry(payload: dict[str, Any]) -> None:
        payload["entries"] = [
            {
                "capability_id": "CAP-LILIES-BAD",
                "origin": {
                    "project_id": "EXP-LILIES-999",
                    "task_revision": 1,
                    "attempt_id": "",
                    "evidence_digest": "not-a-digest",
                },
                "generality_evidence": {},
                "routing_evidence": "",
                "implementation_diff": "",
                "platform_tests": "",
                "independent_review": "",
                "affected_project_reruns": [],
                "contains_project_specific_adapter": True,
                "contains_field_mapping": False,
                "contains_webhook_handler": False,
                "contains_sdk_wrapper": False,
                "contains_final_workflow": False,
                "contains_oracle_material": False,
                "enterprise_denominator": False,
                "status": "implemented_verified",
            }
        ]

    rewrite_json(gap_path, add_malformed_entry)

    errors = module.validate_portfolio(fixture)

    assert any("unknown origin project" in error for error in errors)
    assert any("invalid evidence digest" in error for error in errors)
    assert any("does not reject contains_project_specific_adapter" in error for error in errors)
    assert any("has no independent_review" in error for error in errors)
    assert any("has no affected-project rerun" in error for error in errors)


def test_portfolio_cannot_close_with_unpassed_projects(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    portfolio_path = fixture / module.PORTFOLIO_PATH

    rewrite_json(
        portfolio_path,
        lambda payload: payload.__setitem__("execution_status", "closed"),
    )

    errors = module.validate_portfolio(fixture)

    assert "closed portfolio contains a project that did not pass" in errors


def test_negative_latest_revision_is_rejected(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    project_path = (
        fixture
        / "docs/experiments/lilies-collaboration/EXP-LILIES-002/project.json"
    )

    rewrite_json(
        project_path,
        lambda payload: payload.__setitem__("latest_revision", -1),
    )
    refresh_manifest_locks(fixture, module)

    errors = module.validate_portfolio(fixture)

    assert "EXP-LILIES-002 latest_revision must be a non-negative integer" in errors
