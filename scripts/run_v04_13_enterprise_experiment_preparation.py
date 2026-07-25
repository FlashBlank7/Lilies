from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from agent_platform.formal_verification_contracts import OracleContract
from agent_platform.task_packages import TaskPackageManager


ROOT = Path(__file__).resolve().parents[1]
REVISION = 4
TASK_ROOT = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / "EXP-LILIES-001"
    / str(REVISION)
)
COMPOSE_PATH = (
    ROOT
    / "scripts"
    / "experiments"
    / "exp_lilies_001"
    / "compose.yaml"
)
EVIDENCE_PATH = (
    ROOT
    / "docs"
    / "evidence"
    / "v0.4.13"
    / "t01h"
    / "preparation.json"
)
RUNTIME_ENVIRONMENT_PATH = (
    ROOT
    / "docs"
    / "evidence"
    / "v0.4.13"
    / "t01h"
    / "runtime-environment.json"
)
SOURCE_PATHS = (
    "platform/backend/src/agent_platform/api.py",
    "platform/backend/src/agent_platform/config.py",
    "platform/backend/src/agent_platform/formal_source_provenance.py",
    "platform/backend/src/agent_platform/formal_verification_contracts.py",
    "platform/backend/src/agent_platform/formal_independent_verification.py",
    "platform/backend/src/agent_platform/independent_verifier.py",
    "platform/backend/src/agent_platform/local_lilies_bridge_api.py",
    "scripts/experiments/exp_lilies_001/attestation_server.py",
    "scripts/experiments/exp_lilies_001/compose.yaml",
    "scripts/experiments/exp_lilies_001/environment_control.py",
    "scripts/experiments/exp_lilies_001/fault_proxy.py",
    "scripts/experiments/exp_lilies_001/generate_package.py",
    "scripts/experiments/exp_lilies_001/provision_scoped_account.py",
    "scripts/experiments/exp_lilies_001/verify_host_snapshot.py",
    "scripts/run_v04_13_enterprise_experiment.py",
    "scripts/run_v04_13_enterprise_experiment_preparation.py",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _source_revision() -> str:
    entries = [
        {
            "path": relative,
            "digest": _digest((ROOT / relative).read_bytes()),
        }
        for relative in SOURCE_PATHS
    ]
    return _digest(_canonical_json(entries))


def _scenario_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    scenarios = sorted({str(record["scenario"]) for record in records})
    return {
        scenario: sum(record["scenario"] == scenario for record in records)
        for scenario in scenarios
    }


def _runtime_environment() -> dict[str, Any]:
    if not RUNTIME_ENVIRONMENT_PATH.exists():
        return {
            "status": "blocked_by_environment",
            "docker_daemon_probe": "not_run",
            "real_host_runs": 0,
            "stable_hidden_runs": {
                "completed": 0,
                "required": 3,
            },
            "blocked_by": "No runtime-environment evidence has been recorded.",
            "recheck_trigger": (
                "Record an authenticated Docker and model-authority preflight."
            ),
        }
    value = json.loads(RUNTIME_ENVIRONMENT_PATH.read_bytes())
    environment = value.get("environment") if isinstance(value, dict) else None
    if (
        value.get("schema_version")
        != "v0.4.13-t01h-runtime-environment-1"
        or value.get("stage_task_id") != "V04-13-T01H"
        or value.get("experiment_task_id") != "EXP-LILIES-001"
        or value.get("revision") != REVISION
        or not isinstance(environment, dict)
        or environment.get("status")
        not in {"ready", "blocked_by_environment"}
        or environment.get("real_host_runs") != 0
    ):
        raise RuntimeError("runtime-environment evidence is invalid")
    return environment


def _make_writable(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
        except FileNotFoundError:
            continue


def build_evidence() -> dict[str, Any]:
    environment = yaml.safe_load((TASK_ROOT / "environment.lock").read_bytes())
    compose_digest = _digest(COMPOSE_PATH.read_bytes())
    if environment["compose_digest"] != compose_digest:
        raise RuntimeError("environment lock does not bind the current compose file")
    temporary = Path(tempfile.mkdtemp(prefix="lilies-t01h-prepare-"))
    try:
        manager = TaskPackageManager(temporary)
        package = None
        for revision in range(1, REVISION + 1):
            package = manager.freeze_revision(TASK_ROOT.parent / str(revision))
        assert package is not None
        public = json.loads(
            (
                TASK_ROOT
                / "fixtures"
                / "public-inputs"
                / "debug-records.json"
            ).read_bytes()
        )
        hidden: dict[str, Any] = {}
        for seed in ("101", "202", "303"):
            plan = json.loads(
                (
                    TASK_ROOT
                    / "protected"
                    / "hidden-inputs"
                    / seed
                    / "seed-plan.json"
                ).read_bytes()
            )
            records = plan["records"]
            hidden[seed] = {
                "records": len(records),
                "scenario_counts": _scenario_counts(records),
                "pdfs": len(
                    list(
                        (
                            TASK_ROOT
                            / "protected"
                            / "hidden-inputs"
                            / seed
                            / "documents"
                        ).glob("*.pdf")
                    )
                ),
            }
        oracle = OracleContract.model_validate(
            json.loads(
                (
                    TASK_ROOT
                    / "protected"
                    / "oracle"
                    / "oracle.json"
                ).read_bytes()
            )
        )
        host_oracle = json.loads(
            (
                TASK_ROOT
                / "protected"
                / "oracle"
                / "host-oracle.json"
            ).read_bytes()
        )
        host_checks = host_oracle.get("checks")
        if (
            host_oracle.get("schema_version") != "1.0"
            or host_oracle.get("task_id") != "EXP-LILIES-001"
            or host_oracle.get("revision") != REVISION
            or not isinstance(host_checks, list)
            or any(not isinstance(item, dict) for item in host_checks)
        ):
            raise RuntimeError("independent host oracle is invalid")
        source_projects = [
            {
                "name": project.name,
                "release": project.release,
                "commit_sha": project.commit_sha,
                "image_digest": str(project.image_digest),
            }
            for project in package.task.source_projects
        ]
        return {
            "schema_version": "v0.4.13-t01h-preparation-1",
            "stage_task_id": "V04-13-T01H",
            "experiment_task_id": "EXP-LILIES-001",
            "revision": REVISION,
            "status": "preparation_passed_environment_not_run",
            "source_revision": _source_revision(),
            "package": {
                "public_summary_digest": package.record.public_summary_digest,
                "sealed_package_digest": package.record.sealed_package_digest,
                "verification_process_digest": (
                    package.record.verification_process_digest
                ),
                "immutable_file_count": len(package.record.immutable_files),
                "compose_digest": compose_digest,
                "source_projects": source_projects,
            },
            "dataset": {
                "public": {
                    "records": len(public["records"]),
                    "scenario_counts": _scenario_counts(public["records"]),
                    "pdfs": len(
                        list(
                            (
                                TASK_ROOT
                                / "fixtures"
                                / "public-inputs"
                                / "documents"
                            ).glob("*.pdf")
                        )
                    ),
                },
                "hidden": hidden,
                "stable_seed_count": len(hidden),
            },
            "oracle": {
                "check_count": len(oracle.checks) + len(host_checks),
                "archive_check_count": len(oracle.checks),
                "independent_host_check_count": len(host_checks),
                "record_identity_checks": sum(
                    check.check_id.endswith("-identity")
                    for check in oracle.checks
                ),
                "record_decision_checks": sum(
                    check.check_id.endswith("-decision")
                    for check in oracle.checks
                ),
                "record_host_write_checks": sum(
                    str(check.get("check_id", "")).endswith("-host-write-count")
                    for check in host_checks
                ),
                "xlsx_checks": sum(
                    check.kind.startswith("xlsx_") for check in oracle.checks
                ),
            },
            "environment": _runtime_environment(),
            "claim_ceiling": (
                "Frozen EXP-LILIES-001 package, deterministic dataset/oracle, "
                "compose configuration, fault proxy, environment attestation, "
                "formal assignment verifier orchestration, verifier-only host "
                "snapshot checks, and XLSX verification preparation only. No "
                "Paperless/InvenTree enterprise run, 36/36 oracle pass, or "
                "stable-seed result is claimed."
            ),
            "enterprise_denominator": {
                "required_hidden_records_per_run": 36,
                "required_runs": 3,
                "completed_runs": 0,
                "passed_runs": 0,
                "failed_runs": 0,
                "not_run_runs": 3,
            },
        }
    finally:
        _make_writable(temporary)
        shutil.rmtree(temporary)


def main() -> int:
    evidence = build_evidence()
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_bytes(_canonical_json(evidence) + b"\n")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
