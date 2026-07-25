from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PIPELINE_QUALIFICATION_SCHEMA_VERSION = "v0.4.13-pipeline-qualification-1"
PIPELINE_QUALIFICATION_TASK_ID = "V04-13-T01G"
PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS = 100
PIPELINE_QUALIFICATION_CASE_IDS = tuple(
    f"PIPE-Q{number:02d}" for number in range(1, 29)
)
PIPELINE_QUALIFICATION_SOURCE_SCOPES = (
    "platform/backend/src",
    "platform/frontend",
    "tests",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    ".env.example",
    "Dockerfile",
    "Dockerfile.sandbox",
)
PIPELINE_QUALIFICATION_UNTRACKED_SOURCE_SCOPES = (
    "platform/backend/src",
    "platform/frontend",
    "tests",
    "scripts",
)

QualificationStatus = Literal[
    "passed",
    "failed",
    "not_run",
    "blocked_by_environment",
]
SurfaceStatus = Literal[
    "passed",
    "failed",
    "not_collected",
    "not_applicable",
    "blocked_by_environment",
]


def _combine_statuses(*statuses: QualificationStatus) -> QualificationStatus:
    for candidate in ("failed", "blocked_by_environment", "not_run", "passed"):
        if candidate in statuses:
            return candidate  # type: ignore[return-value]
    return "not_run"


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def qualification_source_revision(root: Path) -> str:
    """Return a commit-independent digest of relevant source/config content."""

    repository = root.expanduser().resolve()

    def git_output(*arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            env={
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
            },
        )
        return completed.stdout

    try:
        tracked = git_output(
            "ls-files",
            "--cached",
            "-z",
            "--",
            *PIPELINE_QUALIFICATION_SOURCE_SCOPES,
        )
        untracked = git_output(
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *PIPELINE_QUALIFICATION_UNTRACKED_SOURCE_SCOPES,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"

    snapshot = hashlib.sha256()
    snapshot.update(b"lilies-qualification-source-content-v1")
    retained = 0
    listed = set(tracked.split(b"\0")) | set(untracked.split(b"\0"))
    for raw_path in sorted(listed - {b""}):
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            return "unavailable"
        path = repository / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            # A tracked deletion and its later committed state are the same
            # current source tree: the path is absent in both.
            continue
        try:
            retained += 1
            snapshot.update(b"\0path\0" + raw_path)
            if path.is_symlink():
                snapshot.update(
                    b"\0symlink\0" + os.fsencode(os.readlink(path))
                )
            elif path.is_file():
                snapshot.update(
                    b"\0file\0"
                    + (b"executable" if metadata.st_mode & 0o111 else b"plain")
                    + hashlib.sha256(path.read_bytes()).digest()
                )
            else:
                return "unavailable"
        except OSError:
            return "unavailable"
    if retained == 0:
        return "unavailable"
    return f"sha256:{snapshot.hexdigest()}"


@dataclass(frozen=True)
class PipelineQualificationCase:
    case_id: str
    scenario: str
    required_result: str
    command_ids: tuple[str, ...]
    mandatory: bool = True
    surface_group: Literal["formal", "development"] = "formal"


@dataclass(frozen=True)
class QualificationCommandSpec:
    command_id: str
    case_ids: tuple[str, ...]
    argv: tuple[str, ...]


@dataclass(frozen=True)
class FaultInjectionLane:
    lane: Literal["reconnect", "idempotency", "lease", "concurrency"]
    case_ids: tuple[str, ...]
    command_id: str
    zero_counters: tuple[str, ...]
    success_counter: str


_PYTEST = (".venv/bin/python", "-m", "pytest", "-q")


PIPELINE_QUALIFICATION_COMMANDS = (
    QualificationCommandSpec(
        "q01-customer-surface-hidden",
        ("PIPE-Q01",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_daemon_tools.py::"
            "test_customer_assignment_has_no_collaboration_projection_tools_or_prompt",
            "tests/test_v04_13_collaboration_platform_wiring.py::"
            "test_ordinary_blackbox_contract_never_lists_collaboration_routes",
        ),
    ),
    QualificationCommandSpec(
        "q02-formal-channel-binding",
        ("PIPE-Q02",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_service.py::"
            "test_formal_only_activation_and_channel_bound_lilies_credential",
            "tests/test_v04_13_collaboration_daemon_tools.py::"
            "test_formal_assignment_gets_exactly_four_tools_with_context_recall_mode",
        ),
    ),
    QualificationCommandSpec(
        "q03-manual-report-routing",
        ("PIPE-Q03",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_service.py::"
            "test_platform_report_routing_is_schema_and_task_setting_driven",
        ),
    ),
    QualificationCommandSpec(
        "q04-preapproval-inbox-privacy",
        ("PIPE-Q04",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_service.py::"
            "test_developer_inbox_hides_all_preapproval_report_content",
        ),
    ),
    QualificationCommandSpec(
        "q05-concurrent-single-side-effect",
        ("PIPE-Q05",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipeline_fault_concurrency_runs_one_hundred_serialized_iterations",
            "tests/test_v04_13_collaboration_fault_injection.py::"
            "test_receipts_serialize_concurrent_revise_approval_release_and_close",
        ),
    ),
    QualificationCommandSpec(
        "q06-incomplete-evidence",
        ("PIPE-Q06",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q06_incomplete_report_cannot_be_approved",
        ),
    ),
    QualificationCommandSpec(
        "q07-task-local-auto-forward",
        ("PIPE-Q07",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q07_auto_forward_routes_report_but_not_permission",
        ),
    ),
    QualificationCommandSpec(
        "q08-new-task-manual-default",
        ("PIPE-Q08",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q08_new_task_does_not_inherit_auto_forward",
        ),
    ),
    QualificationCommandSpec(
        "q09-reconnect-100",
        ("PIPE-Q09",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_fault_injection.py::"
            "test_one_hundred_subscriber_overflows_reconnect_from_durable_cursor",
        ),
    ),
    QualificationCommandSpec(
        "q10-subscriber-overflow",
        ("PIPE-Q10",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_service.py::"
            "test_subscriber_overflow_disconnects_notification_only",
        ),
    ),
    QualificationCommandSpec(
        "q11-q12-idempotency-100",
        ("PIPE-Q11", "PIPE-Q12"),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_sqlite_integration.py::"
            "test_one_hundred_identical_replays_are_one_durable_result_and_conflict_on_drift",
        ),
    ),
    QualificationCommandSpec(
        "q13-lease-100",
        ("PIPE-Q13",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_fault_injection.py::"
            "test_one_hundred_lease_fault_retry_expiry_cycles_reject_stale_owners",
        ),
    ),
    QualificationCommandSpec(
        "q14-substantive-developer-response",
        ("PIPE-Q14",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q14_bare_ok_is_rejected_without_waking_lilies",
        ),
    ),
    QualificationCommandSpec(
        "q15-contract-refresh-before-resume",
        ("PIPE-Q15",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_daemon_tools.py::"
            "test_reprobe_requires_contract_fetch_after_delivered_response_cursor",
        ),
    ),
    QualificationCommandSpec(
        "q16-task-gap-direct-route",
        ("PIPE-Q16",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q16_task_gap_routes_to_author_without_user_question",
        ),
    ),
    QualificationCommandSpec(
        "q17-real-environment-failure",
        ("PIPE-Q17",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_state_machine_sqlite.py::"
            "test_environment_failure_routes_through_real_health_restore_and_lilies_check",
            "tests/test_v04_13_independent_verification.py::"
            "test_protocol_mock_archive_cannot_be_independently_verified",
        ),
    ),
    QualificationCommandSpec(
        "q18-permission-is-not-gap",
        ("PIPE-Q18",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q18_permission_denial_stays_outside_capability_reports",
        ),
    ),
    QualificationCommandSpec(
        "q19-oracle-failure-is-terminal",
        ("PIPE-Q19",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_storage_transactions.py::"
            "test_closed_claim_verification_cas_resolves_only_capability_reports",
            "tests/test_v04_13_collaboration_compaction.py::"
            "test_compaction_preserves_claim_verdict_revision_and_bound_differences",
        ),
    ),
    QualificationCommandSpec(
        "q20-storage-redaction",
        ("PIPE-Q20",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_storage_security.py::"
            "test_storage_boundary_redacts_plaintext_before_database_write",
            "tests/test_v04_13_collaboration_daemon_tools.py::"
            "test_collaboration_tool_input_is_redacted_before_every_daemon_projection",
        ),
    ),
    QualificationCommandSpec(
        "q21-closed-channel-read-only",
        ("PIPE-Q21",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_qualification.py::"
            "test_pipe_q21_closed_channel_rejects_writes_but_keeps_history",
        ),
    ),
    QualificationCommandSpec(
        "q22-daemon-wait-restart",
        ("PIPE-Q22",),
        _PYTEST
        + (
            "tests/test_v04_13_collaboration_daemon_waiting.py::"
            "test_restart_resumes_same_waiting_turn_report_and_cursor",
        ),
    ),
    QualificationCommandSpec(
        "q23-developer-workspace-isolation",
        ("PIPE-Q23",),
        _PYTEST
        + (
            "tests/test_v04_13_formal_assignment_broker.py::"
            "test_broker_workspace_has_no_protected_repository_or_platform_data",
            "tests/test_v04_13_collaboration_service.py::"
            "test_developer_inbox_hides_all_preapproval_report_content",
        ),
    ),
    QualificationCommandSpec(
        "q24-platform-neutral-assignment",
        ("PIPE-Q24",),
        _PYTEST
        + (
            "tests/test_v04_13_collaborative_development_api.py::"
            "test_standalone_api_is_role_scoped_durable_and_builder_independent",
            "tests/test_v04_13_development_workspace_broker.py::"
            "test_broker_creates_independent_idempotent_role_workspaces",
        ),
    ),
    QualificationCommandSpec(
        "q25-development-only-tools",
        ("PIPE-Q25",),
        _PYTEST
        + (
            "tests/test_v04_13_collaborative_development_qualification.py::"
            "test_q24_q25_development_role_is_explicit_and_builder_surfaces_stay_clean",
            "tests/test_v04_13_lilies_development_tools.py",
        ),
    ),
    QualificationCommandSpec(
        "q26-manual-autonomous-dispatch",
        ("PIPE-Q26",),
        _PYTEST
        + (
            "tests/test_v04_13_collaborative_development_qualification.py::"
            "test_q26_manual_waits_and_autonomous_persists_dispatch",
            "tests/test_v04_13_collaborative_development_dispatcher.py::"
            "test_dispatch_delivery_records_exact_grant_digest_once",
        ),
    ),
    QualificationCommandSpec(
        "q27-authority-expansion-pauses",
        ("PIPE-Q27",),
        _PYTEST
        + (
            "tests/test_v04_13_collaborative_development_dispatcher.py::"
            "test_autonomous_dispatch_pauses_on_authority_expansion_and_keeps_grant",
            "tests/test_v04_13_lilies_development_tools.py::"
            "test_autonomous_handoff_is_identical_or_strictly_narrower",
            "tests/test_v04_13_lilies_development_tools.py::"
            "test_process_sandbox_blocks_internal_authority_expansion",
        ),
    ),
    QualificationCommandSpec(
        "q28-standalone-full-cycle",
        ("PIPE-Q28",),
        _PYTEST
        + (
            "tests/test_v04_13_collaborative_development_qualification.py::"
            "test_q28_plain_git_fixture_completes_rework_accept_and_archive",
        ),
    ),
    QualificationCommandSpec(
        "q28-standalone-cli-worker",
        ("PIPE-Q28",),
        _PYTEST
        + (
            "tests/test_v04_13_collaborative_development_worker.py",
        ),
    ),
)


PIPELINE_QUALIFICATION_CASES = (
    PipelineQualificationCase(
        "PIPE-Q01",
        "ordinary customer session",
        "Collaboration tools, routes, and prompt material are undiscoverable.",
        ("q01-customer-surface-hidden",),
    ),
    PipelineQualificationCase(
        "PIPE-Q02",
        "formal task collaboration injection",
        "The formal Lilies credential can access only its bound channel.",
        ("q02-formal-channel-binding",),
    ),
    PipelineQualificationCase(
        "PIPE-Q03",
        "complete capability report in manual mode",
        "The report reaches awaiting_user_review.",
        ("q03-manual-report-routing",),
    ),
    PipelineQualificationCase(
        "PIPE-Q04",
        "developer query before approval",
        "The inbox discloses no report body or inferable report metadata.",
        ("q04-preapproval-inbox-privacy",),
    ),
    PipelineQualificationCase(
        "PIPE-Q05",
        "duplicate and concurrent approval",
        "Exactly one approval message and one durable side effect are created.",
        ("q05-concurrent-single-side-effect",),
    ),
    PipelineQualificationCase(
        "PIPE-Q06",
        "report missing evidence",
        "The report remains needs_more_evidence and cannot be approved.",
        ("q06-incomplete-evidence",),
    ),
    PipelineQualificationCase(
        "PIPE-Q07",
        "task-local auto-forward",
        "A complete report routes automatically while permission remains user-owned.",
        ("q07-task-local-auto-forward",),
    ),
    PipelineQualificationCase(
        "PIPE-Q08",
        "next task creation",
        "A new channel starts in manual approval mode.",
        ("q08-new-task-manual-default",),
    ),
    PipelineQualificationCase(
        "PIPE-Q09",
        "reconnect after 100 messages",
        "The durable ack cursor restores every message in order without duplicates.",
        ("q09-reconnect-100",),
    ),
    PipelineQualificationCase(
        "PIPE-Q10",
        "subscriber memory queue overflow",
        "The subscriber disconnects without deleting durable messages.",
        ("q10-subscriber-overflow",),
    ),
    PipelineQualificationCase(
        "PIPE-Q11",
        "same idempotency key and payload replay",
        "Every replay returns the original record without another side effect.",
        ("q11-q12-idempotency-100",),
    ),
    PipelineQualificationCase(
        "PIPE-Q12",
        "same idempotency key with a changed payload",
        "The mutation returns conflict and preserves the original record.",
        ("q11-q12-idempotency-100",),
    ),
    PipelineQualificationCase(
        "PIPE-Q13",
        "expired developer lease",
        "The report becomes leasable again and rejects every stale owner.",
        ("q13-lease-100",),
    ),
    PipelineQualificationCase(
        "PIPE-Q14",
        "DeveloperResponse containing only OK",
        "Schema validation rejects it and no Lilies wake-up is persisted.",
        ("q14-substantive-developer-response",),
    ),
    PipelineQualificationCase(
        "PIPE-Q15",
        "new platform contract digest",
        "Lilies refreshes the public contract before deciding to resume.",
        ("q15-contract-refresh-before-resume",),
    ),
    PipelineQualificationCase(
        "PIPE-Q16",
        "missing task specification",
        "The report routes directly to the task author without asking the user.",
        ("q16-task-gap-direct-route",),
    ),
    PipelineQualificationCase(
        "PIPE-Q17",
        "real environment unavailable",
        "The formal run is environment_failed and mock evidence cannot pass it.",
        ("q17-real-environment-failure",),
    ),
    PipelineQualificationCase(
        "PIPE-Q18",
        "permission denied",
        "The event remains a permission outcome and cannot become a platform gap.",
        ("q18-permission-is-not-gap",),
    ),
    PipelineQualificationCase(
        "PIPE-Q19",
        "Lilies claim with failed oracle verification",
        "The claim is verification_failed and the task remains incomplete.",
        ("q19-oracle-failure-is-terminal",),
    ),
    PipelineQualificationCase(
        "PIPE-Q20",
        "report containing bearer or cookie material",
        "The durable record and read projection contain only redacted values.",
        ("q20-storage-redaction",),
    ),
    PipelineQualificationCase(
        "PIPE-Q21",
        "closed channel",
        "New writes fail closed while historical reads remain available.",
        ("q21-closed-channel-read-only",),
    ),
    PipelineQualificationCase(
        "PIPE-Q22",
        "daemon restart while waiting for a response",
        "The same session, report, wait, and durable cursor resume.",
        ("q22-daemon-wait-restart",),
    ),
    PipelineQualificationCase(
        "PIPE-Q23",
        "developer attempts direct data or oracle access",
        "Protected and platform data are absent; only approved inbox data is visible.",
        ("q23-developer-workspace-isolation",),
    ),
    PipelineQualificationCase(
        "PIPE-Q24",
        "create a DevelopmentAssignment for unrelated software",
        (
            "Lilies and Codex receive independent persistent roles and workspaces "
            "without an application, Builder, task package, or oracle."
        ),
        ("q24-platform-neutral-assignment",),
        surface_group="development",
    ),
    PipelineQualificationCase(
        "PIPE-Q25",
        "Lilies acts as explicit developer and reviewer",
        (
            "Development search, bounded process, Git diff, and test tools exist "
            "only in the explicit role context."
        ),
        ("q25-development-only-tools",),
        surface_group="development",
    ),
    PipelineQualificationCase(
        "PIPE-Q26",
        "switch manual and autonomous handoff",
        (
            "Manual waits for dispatch; autonomous durably advances Codex work and "
            "Lilies review; each new assignment defaults to manual."
        ),
        ("q26-manual-autonomous-dispatch",),
        surface_group="development",
    ),
    PipelineQualificationCase(
        "PIPE-Q27",
        "an autonomous worker requests undeclared authority",
        (
            "Dispatch pauses with a durable authorization request and does not "
            "change path, argv, host, side-effect, secret, or budget grants."
        ),
        ("q27-authority-expansion-pauses",),
        surface_group="development",
    ),
    PipelineQualificationCase(
        "PIPE-Q28",
        "the workflow platform and Studio are absent",
        (
            "The standalone API, CLI, and worker complete an unrelated Git work "
            "item through result, rework or acceptance, close, and archive."
        ),
        ("q28-standalone-full-cycle", "q28-standalone-cli-worker"),
        surface_group="development",
    ),
)


FAULT_INJECTION_LANES = (
    FaultInjectionLane(
        "reconnect",
        ("PIPE-Q09",),
        "q09-reconnect-100",
        ("lost_messages", "duplicate_deliveries"),
        "recovered_connections",
    ),
    FaultInjectionLane(
        "idempotency",
        ("PIPE-Q11", "PIPE-Q12"),
        "q11-q12-idempotency-100",
        ("duplicate_side_effects", "payload_drift_mutations"),
        "stable_replays",
    ),
    FaultInjectionLane(
        "lease",
        ("PIPE-Q13",),
        "q13-lease-100",
        ("lost_messages", "stale_owner_mutations"),
        "recovered_expired_leases",
    ),
    FaultInjectionLane(
        "concurrency",
        ("PIPE-Q05",),
        "q05-concurrent-single-side-effect",
        ("lost_messages", "duplicate_side_effects"),
        "serialized_iterations",
    ),
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QualificationSurfaceResult(_FrozenModel):
    status: SurfaceStatus
    source: str = Field(min_length=1, max_length=1_000)
    summary: str = Field(min_length=1, max_length=10_000)
    observations: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    claim_ceiling: str | None = Field(default=None, min_length=1, max_length=2_000)
    recheck_trigger: str | None = Field(default=None, min_length=1, max_length=2_000)
    digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def collected_surface_has_evidence_digest(self) -> QualificationSurfaceResult:
        if self.status in {"passed", "failed", "blocked_by_environment"}:
            if not self.observations or self.digest is None:
                raise ValueError(
                    "a collected surface requires structured observations and a digest"
                )
            if not hmac.compare_digest(
                self.digest,
                canonical_digest(self.observations),
            ):
                raise ValueError("surface digest does not bind its observations")
            if self.status == "blocked_by_environment" and (
                self.claim_ceiling is None
                or not self.claim_ceiling.strip()
                or self.recheck_trigger is None
                or not self.recheck_trigger.strip()
            ):
                raise ValueError(
                    "blocked surface requires a claim ceiling and recheck trigger"
                )
        elif self.observations or self.digest is not None:
            raise ValueError(
                "an uncollected or inapplicable surface cannot claim observations"
            )
        return self


class QualificationPytestOutcomes(_FrozenModel):
    collected: int = Field(ge=1)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)
    xfailed: int = Field(ge=0)
    xpassed: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_match_collection(self) -> QualificationPytestOutcomes:
        if (
            self.passed
            + self.failed
            + self.errors
            + self.skipped
            + self.xfailed
            + self.xpassed
            != self.collected
        ):
            raise ValueError("pytest qualification outcome counts do not match collection")
        return self


class QualificationCommandResult(_FrozenModel):
    command_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,120}$")
    case_ids: list[str] = Field(min_length=1, max_length=100)
    argv: list[str] = Field(min_length=1, max_length=1_000)
    status: QualificationStatus
    exit_code: int | None = None
    duration_ms: float = Field(ge=0)
    output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pytest_outcomes: QualificationPytestOutcomes | None = None

    @field_validator("case_ids")
    @classmethod
    def case_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("command case IDs must be unique")
        return value

    @model_validator(mode="after")
    def exit_code_matches_status(self) -> QualificationCommandResult:
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("a passed command requires exit_code=0")
        if self.status == "failed" and (self.exit_code is None or self.exit_code == 0):
            raise ValueError("a failed command requires a non-zero exit code")
        if self.status in {"not_run", "blocked_by_environment"} and self.exit_code is not None:
            raise ValueError("an unexecuted command must omit exit_code")
        is_pytest = self.argv[1:3] == ["-m", "pytest"]
        if is_pytest and self.status == "passed":
            if self.pytest_outcomes is None:
                raise ValueError("a passed pytest command requires retained outcomes")
            outcomes = self.pytest_outcomes
            if (
                outcomes.failed
                or outcomes.errors
                or outcomes.skipped
                or outcomes.xfailed
                or outcomes.xpassed
                or outcomes.passed != outcomes.collected
            ):
                raise ValueError(
                    "a mandatory pytest command cannot pass with skip or xfail outcomes"
                )
        return self


class QualificationCaseResult(_FrozenModel):
    case_id: str = Field(pattern=r"^PIPE-Q[0-9]{2,}$")
    scenario: str = Field(min_length=1, max_length=10_000)
    required_result: str = Field(min_length=1, max_length=10_000)
    mandatory: bool = True
    xfail: Literal[False] = False
    status: QualificationStatus
    command_ids: list[str] = Field(min_length=1, max_length=100)
    api_result: QualificationSurfaceResult
    browser_result: QualificationSurfaceResult
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FaultInjectionIteration(_FrozenModel):
    lane: Literal["reconnect", "idempotency", "lease", "concurrency"]
    iteration: int = Field(ge=1, le=PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS)
    status: QualificationStatus
    counters: dict[str, int]
    command_id: str
    command: list[str] = Field(min_length=1, max_length=1_000)
    output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    record_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_digest_matches(self) -> FaultInjectionIteration:
        lane = next(
            item for item in FAULT_INJECTION_LANES if item.lane == self.lane
        )
        command = command_specs_by_id()[lane.command_id]
        if self.command_id != lane.command_id:
            raise ValueError("fault-injection iteration command ID changed")
        if self.command != list(command.argv):
            raise ValueError("fault-injection iteration argv changed")
        expected_counter_keys = {
            "attempted_iterations",
            lane.success_counter,
            *lane.zero_counters,
        }
        if set(self.counters) != expected_counter_keys:
            raise ValueError("fault-injection iteration counter schema changed")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.counters.values()
        ):
            raise ValueError(
                "fault-injection iteration counters must be non-negative integers"
            )
        if self.status != "passed":
            raise ValueError(
                "qualification retains only actually completed passing iterations"
            )
        if (
            self.counters["attempted_iterations"] != 1
            or self.counters[lane.success_counter] != 1
            or any(self.counters[name] != 0 for name in lane.zero_counters)
        ):
            raise ValueError("fault-injection iteration did not meet its invariants")
        expected = canonical_digest(
            self.model_dump(
                mode="json",
                exclude={"record_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.record_digest):
            raise ValueError("fault-injection iteration digest changed")
        return self


class FaultInjectionLaneResult(_FrozenModel):
    lane: Literal["reconnect", "idempotency", "lease", "concurrency"]
    case_ids: list[str] = Field(min_length=1, max_length=100)
    command_id: str
    status: QualificationStatus
    required_iterations: Literal[100] = PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS
    verified_iterations: int = Field(ge=0, le=PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS)
    counters: dict[str, int]
    iterations: list[FaultInjectionIteration] = Field(
        min_length=PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS,
        max_length=PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS,
    )

    @model_validator(mode="after")
    def iterations_are_contiguous(self) -> FaultInjectionLaneResult:
        lane = next(
            item for item in FAULT_INJECTION_LANES if item.lane == self.lane
        )
        if self.case_ids != list(lane.case_ids):
            raise ValueError("fault-injection lane case binding changed")
        if self.command_id != lane.command_id:
            raise ValueError("fault-injection lane command binding changed")
        if [item.iteration for item in self.iterations] != list(
            range(1, self.required_iterations + 1)
        ):
            raise ValueError("fault-injection iteration numbers must be contiguous")
        if any(item.lane != self.lane for item in self.iterations):
            raise ValueError("fault-injection iteration lane changed")
        if any(item.command_id != self.command_id for item in self.iterations):
            raise ValueError("fault-injection iteration command changed")
        if len({item.record_digest for item in self.iterations}) != self.required_iterations:
            raise ValueError("fault-injection iteration records must be individually bound")
        if len({item.output_digest for item in self.iterations}) != self.required_iterations:
            raise ValueError("fault-injection iterations must retain per-call output evidence")
        passed_iterations = sum(item.status == "passed" for item in self.iterations)
        if self.verified_iterations != passed_iterations:
            raise ValueError("verified iteration count does not match iteration records")
        if (
            self.status != "passed"
            or self.verified_iterations != self.required_iterations
        ):
            raise ValueError("a qualification lane must pass all required iterations")
        aggregate: dict[str, int] = {}
        for item in self.iterations:
            for key, value in item.counters.items():
                aggregate[key] = aggregate.get(key, 0) + value
        expected_counter_keys = {
            "attempted_iterations",
            lane.success_counter,
            *lane.zero_counters,
        }
        if set(self.counters) != expected_counter_keys or self.counters != aggregate:
            raise ValueError("fault-injection aggregate counters changed")
        if (
            self.counters["attempted_iterations"] != self.required_iterations
            or self.counters[lane.success_counter] != self.required_iterations
            or any(self.counters[name] != 0 for name in lane.zero_counters)
        ):
            raise ValueError("fault-injection lane did not meet its invariants")
        return self


class FaultInjectionQualification(_FrozenModel):
    required_iterations_per_lane: Literal[100] = (
        PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS
    )
    lanes: list[FaultInjectionLaneResult] = Field(min_length=4, max_length=4)
    total_iteration_records: Literal[400] = 400

    @model_validator(mode="after")
    def all_four_lanes_are_present(self) -> FaultInjectionQualification:
        if [item.lane for item in self.lanes] != [
            "reconnect",
            "idempotency",
            "lease",
            "concurrency",
        ]:
            raise ValueError("qualification must retain all four fault-injection lanes")
        if sum(len(item.iterations) for item in self.lanes) != self.total_iteration_records:
            raise ValueError("qualification must retain exactly 400 iteration records")
        all_iterations = [
            iteration for lane in self.lanes for iteration in lane.iterations
        ]
        if len({item.record_digest for item in all_iterations}) != 400:
            raise ValueError("all fault-injection records must be globally unique")
        if len({item.output_digest for item in all_iterations}) != 400:
            raise ValueError("all fault-injection outputs must be globally unique")
        return self


def _fault_status_for_case(
    fault: FaultInjectionQualification,
    case_id: str,
) -> QualificationStatus:
    expected_lanes = [
        lane.lane for lane in FAULT_INJECTION_LANES if case_id in lane.case_ids
    ]
    if not expected_lanes:
        return "passed"
    linked = [lane for lane in fault.lanes if case_id in lane.case_ids]
    if [lane.lane for lane in linked] != expected_lanes:
        raise ValueError(f"{case_id} fault-injection lane binding changed")
    return _combine_statuses(*(lane.status for lane in linked))


def _has_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _has_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_dispatch_history(
    history: Any,
    *,
    roles: Sequence[str],
    outbox_kinds: Sequence[str],
    execution_mode: str,
    assignment_id: str | None = None,
) -> list[Mapping[str, Any]]:
    if not isinstance(history, list) or len(history) != len(roles):
        raise ValueError("dispatch history does not have the required exact length")
    if any(not isinstance(item, Mapping) for item in history):
        raise ValueError("dispatch history entries must be objects")
    parsed: list[Mapping[str, Any]] = list(history)
    if [item.get("destination_role") for item in parsed] != list(roles):
        raise ValueError("dispatch history role sequence changed")
    if [item.get("outbox_kind") for item in parsed] != list(outbox_kinds):
        raise ValueError("dispatch history kind sequence changed")
    if any(
        item.get("status") != "delivered"
        or item.get("execution_mode") != execution_mode
        or item.get("attempt") != 1
        or not _has_sha256(item.get("grant_digest"))
        or not _has_nonempty_text(item.get("invocation_fence_id"))
        or not _has_nonempty_text(item.get("dispatch_id"))
        or not _has_nonempty_text(item.get("outbox_id"))
        or not _has_nonempty_text(item.get("assignment_id"))
        or not _has_nonempty_text(item.get("work_item_id"))
        for item in parsed
    ):
        raise ValueError("dispatch history contains an incomplete delivery")
    for key in ("dispatch_id", "outbox_id", "invocation_fence_id"):
        values = [str(item[key]) for item in parsed]
        if len(values) != len(set(values)):
            raise ValueError(f"dispatch history {key} values must be unique")
    assignment_ids = {str(item["assignment_id"]) for item in parsed}
    work_item_ids = {str(item["work_item_id"]) for item in parsed}
    if len(assignment_ids) != 1 or len(work_item_ids) != 1:
        raise ValueError("dispatch history must remain bound to one assignment and item")
    if assignment_id is not None and assignment_ids != {assignment_id}:
        raise ValueError("dispatch history assignment binding changed")
    return parsed


def _validate_tool_usage_history(
    history: Any,
    *,
    result_ids: Sequence[str],
    review_ids: Sequence[str],
) -> list[Mapping[str, Any]]:
    if not isinstance(history, list) or len(history) != 7:
        raise ValueError("tool usage history must contain exactly seven actual calls")
    if any(not isinstance(item, Mapping) for item in history):
        raise ValueError("tool usage history entries must be objects")
    parsed: list[Mapping[str, Any]] = list(history)
    if len({str(item.get("reservation_id")) for item in parsed}) != len(parsed):
        raise ValueError("tool usage reservation IDs must be unique")
    if len({str(item.get("usage_id")) for item in parsed}) != len(parsed):
        raise ValueError("tool usage IDs must be unique")
    if any(
        item.get("status") != "completed"
        or item.get("tool_calls") != 1
        or not _has_nonempty_text(item.get("reservation_id"))
        or not _has_nonempty_text(item.get("usage_id"))
        or not _has_sha256(item.get("request_digest"))
        or not _has_sha256(item.get("response_digest"))
        or isinstance(item.get("commands"), bool)
        or item.get("commands") not in {0, 1}
        for item in parsed
    ):
        raise ValueError("tool usage history contains an incomplete reservation")
    tool_counts = {
        tool: sum(item.get("tool_name") == tool for item in parsed)
        for tool in ("process_run", "git_diff", "workspace_patch")
    }
    actor_counts = {
        role: sum(item.get("actor_role") == role for item in parsed)
        for role in ("codex", "lilies")
    }
    if (
        tool_counts
        != {"process_run": 4, "git_diff": 2, "workspace_patch": 1}
        or actor_counts != {"codex": 5, "lilies": 2}
        or sum(int(item["commands"]) for item in parsed) != 6
        or any(
            item.get("commands")
            != (1 if item.get("tool_name") in {"process_run", "git_diff"} else 0)
            for item in parsed
        )
        or any(
            item.get("tool_name") in {"process_run", "git_diff"}
            and not _has_sha256(item.get("output_digest"))
            for item in parsed
        )
    ):
        raise ValueError("tool and command usage totals changed")
    result_consumers = {
        str(item.get("consumer_id"))
        for item in parsed
        if item.get("consumer_type") == "result"
    }
    review_consumers = {
        str(item.get("consumer_id"))
        for item in parsed
        if item.get("consumer_type") == "review"
    }
    if (
        result_consumers != set(result_ids)
        or review_consumers != set(review_ids)
        or sum(item.get("consumer_type") is None for item in parsed) != 3
        or any(
            item.get("tool_name") == "process_run"
            and item.get("consumer_type") not in {"result", "review"}
            for item in parsed
        )
        or any(
            item.get("tool_name") != "process_run"
            and (
                item.get("consumer_type") is not None
                or item.get("consumer_id") is not None
            )
            for item in parsed
        )
    ):
        raise ValueError("command receipts are not bound to the actual usage ledger")
    return parsed


def _validate_reusable_scenario(
    record: Any,
    *,
    mode: str,
) -> None:
    if not isinstance(record, Mapping):
        raise ValueError(f"{mode} reusable-development record is missing")
    expected_history_roles = (
        ["codex", "codex"]
        if mode == "manual_dispatch"
        else ["codex", "lilies", "codex", "lilies"]
    )
    expected_history_kinds = (
        ["work_dispatch", "work_dispatch"]
        if mode == "manual_dispatch"
        else ["work_dispatch", "lilies_review", "work_dispatch", "lilies_review"]
    )
    assignment_id = str(record.get("assignment_id", ""))
    _validate_dispatch_history(
        record.get("dispatch_history"),
        roles=expected_history_roles,
        outbox_kinds=expected_history_kinds,
        execution_mode=mode,
        assignment_id=assignment_id,
    )
    results = record.get("results")
    snapshots = record.get("independent_review_snapshots")
    review_ids = record.get("review_ids")
    event_history = record.get("store_event_history")
    tool_usage_history = record.get("tool_usage_history")
    checkpoints = record.get("checkpoints")
    if (
        record.get("status") != "passed"
        or record.get("mode") != mode
        or not assignment_id
        or not str(record.get("software_id", "")).startswith(
            "plain-python-library-"
        )
        or not str(record.get("baseline_commit", "")).strip()
        or record.get("enterprise_denominator") is not False
        or record.get("workflow_application_required") is not False
        or record.get("builder_required") is not False
        or record.get("task_package_required") is not False
        or record.get("oracle_required") is not False
        or record.get("review_verdicts") != ["rework", "accepted"]
        or record.get("restart_store_history_equal") is not True
        or record.get("restart_tool_usage_equal") is not True
        or record.get("restart_dispatch_history_equal") is not True
        or record.get("original_grants_unchanged") is not True
        or record.get("source_repository_unchanged") is not True
        or record.get("final_assignment_status") != "archived"
        or record.get("final_work_item_status") != "closed"
        or record.get("executed_lifecycle")
        != [
            "work_item",
            "result",
            "rework",
            "independent_lilies_review",
            "accept",
            "close",
            "stop",
            "archive",
        ]
        or not isinstance(results, list)
        or len(results) != 2
        or [item.get("passed") for item in results if isinstance(item, Mapping)]
        != [False, True]
        or [item.get("exit_code") for item in results if isinstance(item, Mapping)]
        != [1, 0]
        or any(
            not isinstance(item, Mapping)
            or not _has_sha256(item.get("diff_digest"))
            or not _has_sha256(item.get("output_digest"))
            or not _has_nonempty_text(item.get("result_id"))
            for item in results
        )
        or not isinstance(review_ids, list)
        or len(review_ids) != 2
        or any(not _has_nonempty_text(item) for item in review_ids)
        or len(set(review_ids)) != 2
        or not isinstance(snapshots, list)
        or len(snapshots) != 2
        or [item.get("changed_paths") for item in snapshots if isinstance(item, Mapping)]
        != [[], ["src/mathlib.py"]]
        or any(
            not isinstance(item, Mapping)
            or item.get("source_repository_unchanged") is not True
            or item.get("promotion_state") != "review_snapshot_only"
            or not _has_sha256(item.get("receipt_digest"))
            or not _has_sha256(item.get("snapshot_digest"))
            for item in snapshots
        )
        or not isinstance(event_history, list)
        or not event_history
        or not isinstance(checkpoints, list)
        or not checkpoints
    ):
        raise ValueError(f"{mode} reusable-development lifecycle is incomplete")
    _validate_tool_usage_history(
        tool_usage_history,
        result_ids=[str(item["result_id"]) for item in results],
        review_ids=[str(item) for item in review_ids],
    )
    event_types = {
        item.get("event_type")
        for item in event_history
        if isinstance(item, Mapping)
    }
    if not {
        "assignment.created",
        "work_item.created",
        "work_item.awaiting_dispatch",
        "work_item.leased",
        "work_item.working",
        "work_item.result_submitted",
        "work_item.rework",
        "work_item.accepted",
        "work_item.closed",
        "assignment.stopped",
        "assignment.archived",
    }.issubset(event_types):
        raise ValueError(f"{mode} reusable-development events are incomplete")
    if mode == "manual_dispatch":
        if (
            record.get("manual_waited_before_dispatch") is not True
            or record.get("manual_waited_for_review") is not True
            or record.get("manual_waited_after_rework") is not True
        ):
            raise ValueError("manual dispatch did not retain all wait boundaries")
    elif (
        record.get("manual_waited_before_dispatch") is not False
        or record.get("manual_waited_for_review") is not False
        or record.get("manual_waited_after_rework") is not False
    ):
        raise ValueError("autonomous dispatch unexpectedly recorded manual waits")


def _validate_standalone_q28(record: Any) -> None:
    if not isinstance(record, Mapping):
        raise ValueError("standalone Q28 API/CLI evidence is missing")
    expected_lifecycle = [
        "work_item",
        "result",
        "rework",
        "independent_lilies_review",
        "accept",
        "close",
        "stop",
        "archive",
    ]
    expected_commands = [
        "create",
        "status",
        "work-create",
        "dispatch",
        "lease",
        "start",
        "result",
        "result-show",
        "review-prepare",
        "review-prepare",
        "review",
        "dispatch",
        "lease",
        "start",
        "result",
        "result-show",
        "review-prepare",
        "review-prepare",
        "review",
        "close",
        "stop",
        "archive",
        "status",
        "events",
    ]
    operations = record.get("cli_operations")
    if (
        record.get("status") != "passed"
        or record.get("executed_lifecycle") != expected_lifecycle
        or record.get("review_verdicts") != ["rework", "accepted"]
        or record.get("result_test_passes") != [False, True]
        or record.get("final_assignment_status") != "archived"
        or record.get("final_work_item_status") != "closed"
        or record.get("source_repository_unchanged") is not True
        or record.get("token_material_persisted") is not False
        or record.get("state_transition_transport")
        != "independent_cli_processes_over_loopback_http"
        or record.get("state_transition_service_substitution") is not False
        or not isinstance(operations, list)
        or len(operations) != len(expected_commands)
        or record.get("cli_process_count") != len(operations)
    ):
        raise ValueError("standalone Q28 lifecycle or process boundary is incomplete")
    if any(not isinstance(item, Mapping) for item in operations):
        raise ValueError("standalone Q28 CLI operation is not an object")
    parsed_operations: list[Mapping[str, Any]] = list(operations)
    commands = [item.get("command") for item in parsed_operations]
    if (
        commands != expected_commands
        or record.get("successful_cli_commands") != expected_commands
        or any(
            item.get("exit_code") != 0
            or item.get("process_boundary") != "new_cli_subprocess"
            or not isinstance(item.get("semantic_response"), Mapping)
            or item.get("response_digest")
            != canonical_digest(item["semantic_response"])
            for item in parsed_operations
        )
    ):
        raise ValueError(
            "standalone Q28 CLI sequence, subprocess boundary, or response digest changed"
        )

    result_operations = [
        item for item in parsed_operations if item["command"] == "result"
    ]
    result_reads = [
        item for item in parsed_operations if item["command"] == "result-show"
    ]
    prepares = [
        item for item in parsed_operations if item["command"] == "review-prepare"
    ]
    reviews = [
        item for item in parsed_operations if item["command"] == "review"
    ]
    if (
        [
            item["semantic_response"].get("status")
            for item in result_operations
        ]
        != ["ready_for_lilies_review", "ready_for_lilies_review"]
        or [
            item["semantic_response"].get("test_passed")
            for item in result_reads
        ]
        != [False, True]
        or [
            item["semantic_response"].get("verdict")
            for item in reviews
        ]
        != ["rework", "accepted"]
        or [
            item["semantic_response"].get("status")
            for item in reviews
        ]
        != ["awaiting_dispatch", "accepted"]
        or len(prepares) != 4
    ):
        raise ValueError("standalone Q28 result/review transition sequence changed")

    handoffs = record.get("result_handoffs")
    if not isinstance(handoffs, list) or len(handoffs) != 2 or any(
        not isinstance(item, Mapping) for item in handoffs
    ):
        raise ValueError("standalone Q28 requires exactly two result handoffs")
    parsed_handoffs: list[Mapping[str, Any]] = list(handoffs)
    result_ids = [str(item.get("result_id", "")) for item in parsed_handoffs]
    snapshots = [item.get("review_snapshot") for item in parsed_handoffs]
    if (
        len(set(result_ids)) != 2
        or any(not _has_nonempty_text(item) for item in result_ids)
        or [item.get("verdict") for item in parsed_handoffs]
        != ["rework", "accepted"]
        or any(
            item.get("read_by_lilies_cli") is not True
            or item.get("review_prepare_replayed") is not True
            for item in parsed_handoffs
        )
        or any(not isinstance(item, Mapping) for item in snapshots)
    ):
        raise ValueError("standalone Q28 result handoff bindings are incomplete")
    parsed_snapshots: list[Mapping[str, Any]] = list(snapshots)  # type: ignore[arg-type]
    if (
        [item.get("result_id") for item in parsed_snapshots] != result_ids
        or [item.get("changed_paths") for item in parsed_snapshots]
        != [[], ["src/mathlib.py"]]
        or any(
            item.get("promotion_state") != "review_snapshot_only"
            or item.get("source_repository_unchanged") is not True
            or not _has_nonempty_text(item.get("receipt_id"))
            or not _has_nonempty_text(item.get("review_snapshot_id"))
            or not _has_sha256(item.get("diff_digest"))
            or not _has_sha256(item.get("snapshot_digest"))
            or not _has_sha256(item.get("receipt_digest"))
            for item in parsed_snapshots
        )
        or len({str(item["receipt_id"]) for item in parsed_snapshots}) != 2
        or len({str(item["review_snapshot_id"]) for item in parsed_snapshots}) != 2
        or [
            item["semantic_response"].get("result_id")
            for item in result_reads
        ]
        != result_ids
        or [
            item["semantic_response"].get("diff_digest")
            for item in result_reads
        ]
        != [item["diff_digest"] for item in parsed_snapshots]
        or prepares[0]["semantic_response"] != parsed_snapshots[0]
        or prepares[1]["semantic_response"] != parsed_snapshots[0]
        or prepares[2]["semantic_response"] != parsed_snapshots[1]
        or prepares[3]["semantic_response"] != parsed_snapshots[1]
    ):
        raise ValueError("standalone Q28 trusted review receipt binding changed")

    direct_operations = record.get("direct_api_operations")
    if not isinstance(direct_operations, list) or any(
        not isinstance(item, Mapping) for item in direct_operations
    ):
        raise ValueError("standalone Q28 direct API evidence is missing")
    direct_by_resource = {
        str(item.get("resource")): item for item in direct_operations
    }
    if set(direct_by_resource) != {
        "assignment_status",
        "durable_assignment_events",
        "archived_assignment_status",
    }:
        raise ValueError("standalone Q28 direct API resources changed")
    if any(
        item.get("method") != "GET" or item.get("http_status") != 200
        for item in direct_by_resource.values()
    ):
        raise ValueError("standalone Q28 direct API operation did not pass")
    for resource in ("assignment_status", "archived_assignment_status"):
        operation = direct_by_resource[resource]
        if (
            not isinstance(operation.get("semantic_response"), Mapping)
            or operation.get("response_digest")
            != canonical_digest(operation["semantic_response"])
        ):
            raise ValueError("standalone Q28 status response digest changed")
    event_operation = direct_by_resource["durable_assignment_events"]
    event_types = event_operation.get("event_types")
    next_cursor = event_operation.get("next_cursor")
    required_events = {
        "work_item.result_submitted",
        "work_item.rework",
        "work_item.accepted",
        "work_item.closed",
        "assignment.stopped",
        "assignment.archived",
    }
    if (
        not isinstance(event_types, list)
        or event_types.count("work_item.result_submitted") != 2
        or not required_events.issubset(set(event_types))
        or not isinstance(next_cursor, int)
        or next_cursor < len(event_types)
        or event_operation.get("response_digest")
        != canonical_digest(
            {
                "event_types": event_types,
                "next_cursor": next_cursor,
            }
        )
        or not _has_sha256(record.get("server_log_digest"))
    ):
        raise ValueError("standalone Q28 terminal event or digest binding changed")


def _validate_required_extra_evidence(
    evidence: Sequence[Mapping[str, Any]],
    *,
    expected_source_revision: str,
) -> None:
    required_kinds = {
        "reusable_collaborative_development",
        "bounded_live_lilies_codex_handoff",
        "durable_autonomous_dispatch_history",
    }
    by_kind: dict[str, Mapping[str, Any]] = {}
    for item in evidence:
        kind = item.get("kind")
        if not isinstance(kind, str) or kind in by_kind:
            raise ValueError("qualification extra evidence kinds must be unique strings")
        by_kind[kind] = item
    if set(by_kind) != required_kinds:
        raise ValueError(
            "qualification requires reusable, live-handoff, and durable-dispatch evidence"
        )
    if any(item.get("enterprise_denominator") is not False for item in by_kind.values()):
        raise ValueError("qualification extra evidence is outside the enterprise denominator")
    if any(
        item.get("source_revision") != expected_source_revision
        for item in by_kind.values()
    ):
        raise ValueError("qualification extra evidence source revision is stale")

    reusable = by_kind["reusable_collaborative_development"]
    manual = reusable.get("manual")
    autonomous = reusable.get("autonomous")
    standalone = reusable.get("standalone_api_cli")
    if (
        reusable.get("stage_task_id") != PIPELINE_QUALIFICATION_TASK_ID
        or reusable.get("roles") != ["lilies", "codex"]
        or set(reusable.get("authority_dimensions", ()))
        != {
            "workspace_paths",
            "argv",
            "network_hosts",
            "side_effects",
            "secret_refs",
            "budgets",
        }
        or reusable.get("status") != "passed"
        or reusable.get("executed_lifecycle")
        != [
            "work_item",
            "result",
            "rework",
            "independent_lilies_review",
            "accept",
            "close",
            "stop",
            "archive",
        ]
        or reusable.get("lifecycle")
        != [
            "work_item",
            "result",
            "rework",
            "independent_lilies_review",
            "accept",
            "close",
            "archive",
        ]
        or reusable.get("workflow_application_required") is not False
        or reusable.get("builder_required") is not False
        or reusable.get("original_grants_unchanged") is not True
        or not isinstance(standalone, Mapping)
        or standalone.get("status") != "passed"
        or standalone.get("final_assignment_status") != "archived"
        or standalone.get("token_material_persisted") is not False
        or not isinstance(standalone.get("server"), Mapping)
        or standalone["server"].get("health_http_status") != 200
        or standalone["server"].get("workflow_platform_required") is not False
        or standalone["server"].get("enterprise_denominator") is not False
        or reusable.get("standalone_api_cli_digest")
        != canonical_digest([standalone])
    ):
        raise ValueError("reusable collaborative-development evidence is incomplete")
    _validate_reusable_scenario(manual, mode="manual_dispatch")
    _validate_reusable_scenario(autonomous, mode="autonomous")
    _validate_standalone_q28(standalone)
    reusable_unsigned = {
        key: value for key, value in reusable.items() if key != "evidence_digest"
    }
    if reusable.get("evidence_digest") != canonical_digest(reusable_unsigned):
        raise ValueError("reusable development evidence digest changed")

    live = by_kind["bounded_live_lilies_codex_handoff"]
    record = live.get("record")
    if not isinstance(record, Mapping):
        raise ValueError("live handoff record is missing")
    codex = record.get("codex_implementation")
    lilies = record.get("lilies_review")
    fixture = record.get("software_fixture")
    authority = record.get("authority")
    effective_authority = record.get("effective_handler_authority")
    budget_ledger = record.get("budget_ledger")
    lifecycle = record.get("actual_lifecycle")
    live_history = (
        lifecycle.get("dispatch_history")
        if isinstance(lifecycle, Mapping)
        else None
    )
    lilies_tools = (
        lilies.get("successful_tool_names")
        if isinstance(lilies, Mapping)
        else None
    )
    provider_cost_control = (
        record.get("provider_cost_control")
    )
    codex_usage = codex.get("usage") if isinstance(codex, Mapping) else None
    codex_proxy = (
        codex.get("provider_proxy") if isinstance(codex, Mapping) else None
    )
    codex_proxy_observations = (
        codex_proxy.get("observations")
        if isinstance(codex_proxy, Mapping)
        else None
    )
    if (
        live.get("status") != "passed"
        or live.get("stage_task_id") != PIPELINE_QUALIFICATION_TASK_ID
        or record.get("status") != "passed"
        or record.get("source_revision") != expected_source_revision
        or record.get("schema_version") != "2.0"
        or record.get("stage_task_id") != PIPELINE_QUALIFICATION_TASK_ID
        or record.get("enterprise_denominator") is not False
        or record.get("assignment_status") != "archived"
        or record.get("work_item_status") != "closed"
        or not isinstance(codex, Mapping)
        or codex.get("exit_code") != 0
        or codex.get("changed_files") != ["src/mathlib.py"]
        or codex.get("inherited_full_environment") is not False
        or codex.get("other_role_grant_visible_to_model") is not False
        or codex.get("provider") != "openai-codex-cli"
        or not _has_nonempty_text(codex.get("model"))
        or codex.get("command_count") != 0
        or codex.get("file_or_external_tool_events") != 0
        or codex.get("workspace_supplied_to_model_process") is not False
        or codex.get("outer_filesystem_sandbox") != "macos-seatbelt"
        or not isinstance(codex_usage, Mapping)
        or not isinstance(codex_usage.get("input_tokens"), int)
        or isinstance(codex_usage.get("input_tokens"), bool)
        or codex_usage["input_tokens"] <= 0
        or not isinstance(codex_usage.get("output_tokens"), int)
        or isinstance(codex_usage.get("output_tokens"), bool)
        or codex_usage["output_tokens"] <= 0
        or not isinstance(codex_proxy, Mapping)
        or codex_proxy.get("transport") != "loopback-connect-proxy"
        or (
            not isinstance(codex_proxy_observations, list)
            or codex_proxy.get("denied_connections")
            != sum(
                item.get("allowed") is False
                for item in codex_proxy_observations
                if isinstance(item, Mapping)
            )
        )
        or not isinstance(codex_proxy.get("allowed_hosts"), list)
        or set(codex_proxy["allowed_hosts"])
        != {"api.openai.com", "auth.openai.com", "chatgpt.com"}
        or not isinstance(codex_proxy_observations, list)
        or not codex_proxy_observations
        or not any(
            isinstance(item, Mapping) and item.get("allowed") is True
            for item in codex_proxy_observations
        )
        or any(
            not isinstance(item, Mapping)
            or item.get("port") != 443
            or not isinstance(item.get("allowed"), bool)
            or (
                item.get("allowed") is True
                and (
                    item.get("upstream_connected") is not True
                    or item.get("host") not in codex_proxy["allowed_hosts"]
                )
            )
            or (
                item.get("allowed") is False
                and (
                    item.get("upstream_connected") is True
                    or item.get("host") in codex_proxy["allowed_hosts"]
                    or item.get("client_to_provider_bytes") != 0
                    or item.get("provider_to_client_bytes") != 0
                )
            )
            for item in codex_proxy_observations
        )
        or not _has_sha256(codex.get("source_read_digest"))
        or not _has_sha256(codex.get("proposal_digest"))
        or not _has_sha256(codex.get("trusted_patch_digest"))
        or not _has_nonempty_text(codex.get("result_id"))
        or not isinstance(codex.get("diff"), str)
        or not str(codex["diff"]).strip()
        or codex.get("diff_digest") != canonical_digest(codex["diff"])
        or not str(codex.get("broker_diff_digest", "")).startswith("sha256:")
        or not isinstance(codex.get("test"), Mapping)
        or codex["test"].get("exit_code") != 0
        or not isinstance(lilies, Mapping)
        or not isinstance(lilies.get("review"), Mapping)
        or lilies["review"].get("verdict") != "accepted"
        or not isinstance(lilies["review"].get("acceptance_checks"), list)
        or len(lilies["review"]["acceptance_checks"]) != 3
        or not isinstance(lilies.get("frozen_acceptance"), list)
        or len(lilies["frozen_acceptance"]) != 3
        or [
            item.get("criterion")
            for item in lilies["review"]["acceptance_checks"]
            if isinstance(item, Mapping)
        ]
        != lilies["frozen_acceptance"]
        or not isinstance(lilies.get("model_acceptance_checks"), list)
        or len(lilies["model_acceptance_checks"]) != 3
        or any(
            not isinstance(item, Mapping) or item.get("passed") is not True
            for item in lilies["review"]["acceptance_checks"]
        )
        or lilies.get("independent_snapshot") is not True
        or not str(
            lilies.get("review_snapshot_receipt_digest", "")
        ).startswith("sha256:")
        or not isinstance(lilies.get("tool_calls"), list)
        or len(lilies["tool_calls"]) != 3
        or not isinstance(lilies_tools, list)
        or set(lilies_tools)
        != {
            "workspace_read",
            "git_diff",
            "process_run",
        }
        or set(lilies.get("mandatory_tool_names", ())) != set(lilies_tools)
        or lilies.get("denied_tool_calls") != 0
        or [item.get("name") for item in lilies["tool_calls"]]
        != ["workspace_read", "git_diff", "process_run"]
        or any(
            not isinstance(item, Mapping)
            or item.get("is_error") is not False
            or not _has_sha256(item.get("input_digest"))
            or not _has_sha256(item.get("result_digest"))
            for item in lilies["tool_calls"]
        )
        or not isinstance(lilies.get("usage"), list)
        or not lilies["usage"]
        or not isinstance(provider_cost_control, list)
        or not provider_cost_control
        or any(
            not isinstance(item, Mapping)
            or item.get("settled") is not True
            or item.get("worst_case_cost_usd") != 1.0
            or not _has_nonempty_text(item.get("reservation_id"))
            or not _has_nonempty_text(item.get("provider_request_id"))
            or item.get("provider") not in {"deepseek", "openai-codex-cli"}
            or not _has_nonempty_text(item.get("model"))
            or not isinstance(item.get("actual_cost_usd"), (int, float))
            or isinstance(item.get("actual_cost_usd"), bool)
            or item["actual_cost_usd"] < 0
            or (
                item.get("provider") == "deepseek"
                and item["actual_cost_usd"] <= 0
            )
            or not isinstance(item.get("input_tokens"), int)
            or isinstance(item.get("input_tokens"), bool)
            or item["input_tokens"] < 0
            or not isinstance(item.get("output_tokens"), int)
            or isinstance(item.get("output_tokens"), bool)
            or item["output_tokens"] <= 0
            for item in provider_cost_control
        )
        or {
            item.get("provider")
            for item in provider_cost_control
            if isinstance(item, Mapping)
        }
        != {"deepseek", "openai-codex-cli"}
        or not _has_nonempty_text(lilies.get("provider"))
        or not _has_nonempty_text(lilies.get("model"))
        or not isinstance(fixture, Mapping)
        or fixture.get("kind") != "unrelated_plain_python_git_repository"
        or fixture.get("source_unchanged") is not True
        or not isinstance(authority, Mapping)
        or set(authority) != {"lilies", "codex"}
        or not isinstance(effective_authority, Mapping)
        or set(effective_authority) != {"lilies", "codex"}
        or not isinstance(budget_ledger, Mapping)
        or budget_ledger.get("tool_calls") != 8
        or budget_ledger.get("commands") != 5
        or budget_ledger.get("completed_records") != 8
        or budget_ledger.get("within_assignment_budget") is not True
        or not isinstance(budget_ledger.get("by_role"), Mapping)
        or budget_ledger["by_role"].get("codex")
        != {"tool_calls": 5, "commands": 3}
        or budget_ledger["by_role"].get("lilies")
        != {"tool_calls": 3, "commands": 2}
        or not isinstance(budget_ledger.get("provider_reservations"), int)
        or isinstance(budget_ledger.get("provider_reservations"), bool)
        or budget_ledger["provider_reservations"] < 1
        or budget_ledger.get("provider_settled")
        != budget_ledger["provider_reservations"]
        or budget_ledger["provider_reservations"]
        != len(provider_cost_control)
        or not isinstance(lifecycle, Mapping)
        or lifecycle.get("required_events_present") is not True
        or lifecycle.get("dispatch_history_restart_equal") is not True
        or lifecycle.get("original_grants_unchanged") is not True
        or lifecycle.get("independent_review_snapshot") is not True
    ):
        raise ValueError("bounded live Lilies-Codex handoff evidence is incomplete")
    parsed_live_history = _validate_dispatch_history(
        live_history,
        roles=["codex", "lilies"],
        outbox_kinds=["work_dispatch", "lilies_review"],
        execution_mode="autonomous",
        assignment_id=str(record.get("assignment_id", "")),
    )
    if (
        not isinstance(authority.get("codex"), Mapping)
        or not isinstance(authority.get("lilies"), Mapping)
        or authority["codex"].get("grant_digest")
        != parsed_live_history[0].get("grant_digest")
        or authority["lilies"].get("grant_digest")
        != parsed_live_history[1].get("grant_digest")
    ):
        raise ValueError("live handoff role authority is not bound to dispatch")
    for role in ("codex", "lilies"):
        original = authority[role]
        effective = effective_authority[role]
        if (
            not isinstance(effective, Mapping)
            or not _has_sha256(original.get("grant_digest"))
            or not _has_sha256(effective.get("grant_digest"))
            or not _has_nonempty_text(original.get("workspace_id"))
            or not _has_nonempty_text(effective.get("workspace_id"))
            or not _has_nonempty_text(original.get("baseline_commit"))
            or original.get("baseline_commit")
            != effective.get("baseline_commit")
            or original.get("grant_revision")
            != effective.get("grant_revision")
            or not isinstance(original.get("allowed_paths"), list)
            or not isinstance(effective.get("allowed_paths"), list)
            or not set(effective["allowed_paths"]).issubset(
                original["allowed_paths"]
            )
            or not isinstance(original.get("allowed_argv"), list)
            or not isinstance(effective.get("allowed_argv"), list)
            or not {
                tuple(argv)
                for argv in effective["allowed_argv"]
                if isinstance(argv, list)
            }.issubset(
                {
                    tuple(argv)
                    for argv in original["allowed_argv"]
                    if isinstance(argv, list)
                }
            )
            or not isinstance(original.get("allowed_hosts"), list)
            or not isinstance(effective.get("allowed_hosts"), list)
            or not set(effective["allowed_hosts"]).issubset(
                original["allowed_hosts"]
            )
            or not isinstance(original.get("allowed_side_effects"), list)
            or not isinstance(effective.get("allowed_side_effects"), list)
            or not set(effective["allowed_side_effects"]).issubset(
                original["allowed_side_effects"]
            )
            or not isinstance(original.get("secret_refs"), list)
            or not isinstance(effective.get("secret_refs"), list)
            or not set(effective["secret_refs"]).issubset(
                original["secret_refs"]
            )
            or effective.get("provider_dispatch_grant_digest")
            != effective.get("grant_digest")
            or not _has_sha256(
                effective.get("provider_capability_digest")
            )
        ):
            raise ValueError(
                f"live handoff effective {role} authority is not grant-bound"
            )
    if (
        authority["codex"].get("grant_digest")
        != effective_authority["codex"].get("grant_digest")
        or authority["codex"].get("workspace_id")
        != effective_authority["codex"].get("workspace_id")
        or authority["lilies"].get("workspace_id")
        == effective_authority["lilies"].get("workspace_id")
        or effective_authority["lilies"].get("workspace_id")
        != lilies.get("review_snapshot_id")
        or {
            "workspace_write",
            "external_mutation",
        }.intersection(
            effective_authority["lilies"].get(
                "allowed_side_effects",
                (),
            )
        )
    ):
        raise ValueError(
            "live handoff original and effective role authority boundaries changed"
        )
    expected_provider_roles = {
        "openai-codex-cli": "codex",
        "deepseek": "lilies",
    }
    for cost in provider_cost_control:
        role = expected_provider_roles[cost["provider"]]
        effective = effective_authority[role]
        if (
            cost.get("dispatch_grant_digest")
            != effective.get("grant_digest")
            or cost.get("provider_capability_digest")
            != effective.get("provider_capability_digest")
            or cost.get("provider_hosts")
            != effective.get("allowed_hosts")
            or cost.get("secret_refs")
            != effective.get("secret_refs")
            or cost.get("provider_side_effects")
            != effective.get("allowed_side_effects")
            or not _has_nonempty_text(cost.get("credential_identity"))
            or not _has_sha256(
                cost.get("authorization_evidence_digest")
            )
            or not _has_sha256(cost.get("receipt_evidence_digest"))
        ):
            raise ValueError(
                "live handoff provider cost is not bound to effective authority"
            )
    actual_events = lifecycle.get("events")
    if not isinstance(actual_events, list) or not {
        "assignment.created",
        "work_item.created",
        "work_item.leased",
        "work_item.working",
        "work_item.result_submitted",
        "work_item.accepted",
        "work_item.closed",
        "assignment.stopped",
        "assignment.archived",
    }.issubset(actual_events):
        raise ValueError("live handoff durable event sequence is incomplete")
    live_unsigned = {
        key: value
        for key, value in record.items()
        if key != "evidence_digest"
    }
    if record.get("evidence_digest") != canonical_digest(live_unsigned):
        raise ValueError("live handoff record digest changed")
    live_wrapper_unsigned = {
        key: value for key, value in live.items() if key != "evidence_digest"
    }
    if live.get("evidence_digest") != canonical_digest(live_wrapper_unsigned):
        raise ValueError("live handoff wrapper digest changed")

    durable = by_kind["durable_autonomous_dispatch_history"]
    durable_record = durable.get("record")
    if not isinstance(durable_record, Mapping):
        raise ValueError("durable dispatch record is missing")
    history = durable_record.get("history")
    if (
        durable.get("status") != "passed"
        or durable.get("stage_task_id") != PIPELINE_QUALIFICATION_TASK_ID
        or durable_record.get("status") != "passed"
        or durable_record.get("source_revision") != expected_source_revision
        or durable_record.get("execution_mode") != "autonomous"
        or durable_record.get("restart_history_equal") is not True
        or durable_record.get("restart_store_history_equal") is not True
        or durable_record.get("restart_tool_usage_equal") is not True
        or durable_record.get("original_grants_unchanged") is not True
        or durable_record.get("source_repository_unchanged") is not True
        or durable_record.get("final_assignment_status") != "archived"
        or durable_record.get("final_work_item_status") != "closed"
        or durable_record.get("history_digest") != canonical_digest(history)
        or durable_record.get("store_history_digest")
        != canonical_digest(durable_record.get("store_event_history"))
        or durable_record.get("tool_usage_digest")
        != canonical_digest(durable_record.get("tool_usage_history"))
    ):
        raise ValueError("durable autonomous-dispatch evidence is incomplete")
    parsed_history = _validate_dispatch_history(
        history,
        roles=["codex", "lilies", "codex", "lilies"],
        outbox_kinds=[
            "work_dispatch",
            "lilies_review",
            "work_dispatch",
            "lilies_review",
        ],
        execution_mode="autonomous",
        assignment_id=str(durable_record.get("assignment_id", "")),
    )
    if (
        parsed_history[0].get("grant_digest")
        != parsed_history[2].get("grant_digest")
        or parsed_history[1].get("grant_digest")
        != parsed_history[3].get("grant_digest")
    ):
        raise ValueError("durable dispatch role grants changed across rework")
    result_ids = durable_record.get("result_ids")
    review_ids = durable_record.get("review_ids")
    if (
        not isinstance(result_ids, list)
        or len(result_ids) != 2
        or any(not _has_nonempty_text(item) for item in result_ids)
        or len(set(result_ids)) != 2
        or not isinstance(review_ids, list)
        or len(review_ids) != 2
        or any(not _has_nonempty_text(item) for item in review_ids)
        or len(set(review_ids)) != 2
    ):
        raise ValueError("durable result and review ledger bindings are incomplete")
    _validate_tool_usage_history(
        durable_record.get("tool_usage_history"),
        result_ids=[str(item) for item in result_ids],
        review_ids=[str(item) for item in review_ids],
    )
    store_events = durable_record.get("store_event_history")
    if not isinstance(store_events, list) or not {
        "work_item.rework",
        "work_item.accepted",
        "work_item.closed",
        "assignment.archived",
    }.issubset(
        item.get("event_type")
        for item in store_events
        if isinstance(item, Mapping)
    ):
        raise ValueError("durable autonomous-dispatch events are incomplete")
    durable_unsigned = {
        key: value
        for key, value in durable_record.items()
        if key != "evidence_digest"
    }
    if durable_record.get("evidence_digest") != canonical_digest(durable_unsigned):
        raise ValueError("durable dispatch record digest changed")
    durable_wrapper_unsigned = {
        key: value for key, value in durable.items() if key != "evidence_digest"
    }
    if durable.get("evidence_digest") != canonical_digest(durable_wrapper_unsigned):
        raise ValueError("durable dispatch wrapper digest changed")


class QualificationSummary(_FrozenModel):
    total: int = Field(ge=28)
    mandatory: int = Field(ge=28)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    not_run: int = Field(ge=0)
    blocked_by_environment: int = Field(ge=0)
    mandatory_xfail: Literal[0] = 0

    @model_validator(mode="after")
    def counts_match(self) -> QualificationSummary:
        if self.passed + self.failed + self.not_run + self.blocked_by_environment != self.total:
            raise ValueError("qualification summary counts do not match total")
        return self


class PipelineQualificationBundle(_FrozenModel):
    schema_version: Literal["v0.4.13-pipeline-qualification-1"] = (
        PIPELINE_QUALIFICATION_SCHEMA_VERSION
    )
    qualification_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stage_task_id: Literal["V04-13-T01G"] = PIPELINE_QUALIFICATION_TASK_ID
    enterprise_denominator: Literal[False] = False
    source_revision: str = Field(min_length=1, max_length=1_000)
    generated_at: str = Field(min_length=20, max_length=40)
    status: QualificationStatus
    cases: list[QualificationCaseResult] = Field(min_length=28, max_length=1_000)
    commands: list[QualificationCommandResult] = Field(min_length=1, max_length=1_000)
    fault_injection: FaultInjectionQualification
    extra_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    summary: QualificationSummary
    bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bundle_is_complete_and_digest_matches(self) -> PipelineQualificationBundle:
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("qualification case IDs must be unique")
        if tuple(case_ids) != PIPELINE_QUALIFICATION_CASE_IDS:
            raise ValueError("PIPE-Q01 through PIPE-Q28 must be present in contract order")
        if any(not item.mandatory for item in self.cases):
            raise ValueError("PIPE-Q01 through PIPE-Q28 are mandatory")
        if any(item.xfail for item in self.cases):
            raise ValueError("mandatory xfail is forbidden")
        command_by_id = {item.command_id: item for item in self.commands}
        command_ids = set(command_by_id)
        if len(command_ids) != len(self.commands):
            raise ValueError("qualification command IDs must be unique")
        if len(self.commands) != len(PIPELINE_QUALIFICATION_COMMANDS):
            raise ValueError("qualification commands must match the fixed catalog")
        for definition, result in zip(
            PIPELINE_QUALIFICATION_COMMANDS,
            self.commands,
            strict=True,
        ):
            if (
                result.command_id != definition.command_id
                or result.case_ids != list(definition.case_ids)
                or result.argv != list(definition.argv)
            ):
                raise ValueError("qualification command catalog binding changed")
        if any(
            command_id not in command_ids
            for item in self.cases
            for command_id in item.command_ids
        ):
            raise ValueError("qualification case references a missing command")
        for definition, result in zip(
            PIPELINE_QUALIFICATION_CASES,
            self.cases,
            strict=True,
        ):
            if (
                result.case_id != definition.case_id
                or result.scenario != definition.scenario
                or result.required_result != definition.required_result
                or result.command_ids != list(definition.command_ids)
            ):
                raise ValueError("qualification case catalog binding changed")
            linked = [command_by_id[item] for item in definition.command_ids]
            expected_status = _combine_statuses(
                _case_status(
                    linked,
                    api_result=result.api_result,
                    browser_result=result.browser_result,
                ),
                _fault_status_for_case(self.fault_injection, result.case_id),
            )
            if result.status != expected_status:
                raise ValueError(f"{result.case_id} status does not match its commands")
            expected_evidence = canonical_digest(
                {
                    "case_id": result.case_id,
                    "status": result.status,
                    "command_output_digests": [
                        item.output_digest for item in linked
                    ],
                    "api_result": result.api_result,
                    "browser_result": result.browser_result,
                    "fault_lane_digests": [
                        canonical_digest(lane)
                        for lane in self.fault_injection.lanes
                        if result.case_id in lane.case_ids
                    ],
                }
            )
            if not hmac.compare_digest(expected_evidence, result.evidence_digest):
                raise ValueError(f"{result.case_id} evidence digest changed")
        _validate_required_extra_evidence(
            self.extra_evidence,
            expected_source_revision=self.source_revision,
        )
        counts = {
            status: sum(item.status == status for item in self.cases)
            for status in (
                "passed",
                "failed",
                "not_run",
                "blocked_by_environment",
            )
        }
        if self.summary.model_dump() != {
            "total": len(self.cases),
            "mandatory": sum(item.mandatory for item in self.cases),
            **counts,
            "mandatory_xfail": 0,
        }:
            raise ValueError("qualification summary does not match cases")
        mandatory_cases = [item for item in self.cases if item.mandatory]
        if all(item.status == "passed" for item in mandatory_cases):
            expected_bundle_status: QualificationStatus = "passed"
        elif any(item.status == "failed" for item in mandatory_cases):
            expected_bundle_status = "failed"
        elif any(
            item.status == "blocked_by_environment" for item in mandatory_cases
        ):
            expected_bundle_status = "blocked_by_environment"
        else:
            expected_bundle_status = "not_run"
        if self.status != expected_bundle_status:
            raise ValueError("qualification bundle status does not match mandatory cases")
        expected = canonical_digest(
            self.model_dump(
                mode="json",
                exclude={"bundle_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.bundle_digest):
            raise ValueError("pipeline qualification bundle digest changed")
        return self


def placeholder_surface(
    surface: Literal["api", "browser"],
) -> QualificationSurfaceResult:
    return QualificationSurfaceResult(
        status="not_collected",
        source=f"{surface}:placeholder",
        summary=(
            f"{surface} evidence has not been attached to this deterministic "
            "qualification bundle; this placeholder is not a passing result."
        ),
    )


def _case_status(
    command_results: Sequence[QualificationCommandResult],
    *,
    api_result: QualificationSurfaceResult,
    browser_result: QualificationSurfaceResult,
) -> QualificationStatus:
    statuses = {item.status for item in command_results}
    if "failed" in statuses:
        return "failed"
    if "blocked_by_environment" in statuses:
        return "blocked_by_environment"
    if statuses != {"passed"}:
        return "not_run"
    if api_result.status == "failed" or browser_result.status == "failed":
        return "failed"
    if api_result.status == "blocked_by_environment":
        return "blocked_by_environment"
    if api_result.status != "passed":
        return "not_run"
    # Q01-Q28 behavior is accepted by its fixed command, actual API, and fault
    # evidence.  Browser evidence remains attached to every case, but an
    # unavailable browser is an evidence-level claim ceiling rather than a
    # reason to erase already-observed protocol behavior.  An actual browser
    # failure above still fails the case.
    return "passed"


def build_fault_injection_qualification(
    records: Sequence[FaultInjectionIteration | Mapping[str, Any]],
) -> FaultInjectionQualification:
    """Build the four-lane result only from actual per-operation records."""

    parsed = [
        item
        if isinstance(item, FaultInjectionIteration)
        else FaultInjectionIteration.model_validate(item)
        for item in records
    ]
    if len(parsed) != 400:
        raise ValueError("qualification requires exactly 400 fault records")

    lanes: list[FaultInjectionLaneResult] = []
    for definition in FAULT_INJECTION_LANES:
        iterations = sorted(
            (item for item in parsed if item.lane == definition.lane),
            key=lambda item: item.iteration,
        )
        if len(iterations) != PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS:
            raise ValueError(
                f"{definition.lane} requires exactly 100 actual iteration records"
            )
        counters: dict[str, int] = {}
        for iteration in iterations:
            for key, value in iteration.counters.items():
                counters[key] = counters.get(key, 0) + value
        lanes.append(
            FaultInjectionLaneResult(
                lane=definition.lane,
                case_ids=list(definition.case_ids),
                command_id=definition.command_id,
                status="passed",
                verified_iterations=len(iterations),
                counters=counters,
                iterations=iterations,
            )
        )
    return FaultInjectionQualification(lanes=lanes)


def _normalized_generated_at(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("generated_at must use UTC")
        return value.isoformat().replace("+00:00", "Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("generated_at must use UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def build_pipeline_qualification_bundle(
    command_results: Sequence[QualificationCommandResult | Mapping[str, Any]],
    *,
    source_revision: str,
    generated_at: datetime | str | None = None,
    api_result: QualificationSurfaceResult | Mapping[str, Any] | None = None,
    browser_result: QualificationSurfaceResult | Mapping[str, Any] | None = None,
    development_api_result: QualificationSurfaceResult | Mapping[str, Any] | None = None,
    development_browser_result: (
        QualificationSurfaceResult | Mapping[str, Any] | None
    ) = None,
    fault_injection: FaultInjectionQualification | Mapping[str, Any] | None = None,
    extra_evidence: Sequence[Mapping[str, Any]] = (),
) -> PipelineQualificationBundle:
    """Build a digest-bound Q01-Q28 qualification result.

    The fixed catalog is deliberately platform-infrastructure evidence and is
    never part of the enterprise workflow denominator.
    """

    parsed_commands = [
        item
        if isinstance(item, QualificationCommandResult)
        else QualificationCommandResult.model_validate(item)
        for item in command_results
    ]
    all_commands = parsed_commands
    command_by_id = {item.command_id: item for item in all_commands}
    if len(command_by_id) != len(all_commands):
        raise ValueError("qualification command IDs must be unique")
    if len(all_commands) != len(PIPELINE_QUALIFICATION_COMMANDS):
        raise ValueError("qualification commands must match the fixed catalog")
    for definition, result in zip(
        PIPELINE_QUALIFICATION_COMMANDS,
        all_commands,
        strict=True,
    ):
        if (
            result.command_id != definition.command_id
            or result.case_ids != list(definition.case_ids)
            or result.argv != list(definition.argv)
        ):
            raise ValueError("qualification command catalog binding changed")

    required_command_ids = {
        command_id
        for case in PIPELINE_QUALIFICATION_CASES
        for command_id in case.command_ids
    }
    missing_commands = sorted(required_command_ids - set(command_by_id))
    if missing_commands:
        raise ValueError(
            "qualification is missing required commands: " + ", ".join(missing_commands)
        )

    parsed_api = (
        placeholder_surface("api")
        if api_result is None
        else (
            api_result
            if isinstance(api_result, QualificationSurfaceResult)
            else QualificationSurfaceResult.model_validate(api_result)
        )
    )
    parsed_browser = (
        placeholder_surface("browser")
        if browser_result is None
        else (
            browser_result
            if isinstance(browser_result, QualificationSurfaceResult)
            else QualificationSurfaceResult.model_validate(browser_result)
        )
    )
    if parsed_browser.status in {"not_collected", "not_applicable"}:
        raise ValueError(
            "qualification requires a formal browser result or structured "
            "blocked-by-environment evidence debt"
        )
    parsed_development_api = (
        placeholder_surface("api")
        if development_api_result is None
        else (
            development_api_result
            if isinstance(development_api_result, QualificationSurfaceResult)
            else QualificationSurfaceResult.model_validate(development_api_result)
        )
    )
    parsed_development_browser = (
        QualificationSurfaceResult(
            status="not_applicable",
            source="collaborative-development:no-browser-dependency",
            summary=(
                "The platform-neutral API, CLI, and worker do not require a "
                "browser or the Developer Studio adapter."
            ),
        )
        if development_browser_result is None
        else (
            development_browser_result
            if isinstance(development_browser_result, QualificationSurfaceResult)
            else QualificationSurfaceResult.model_validate(development_browser_result)
        )
    )
    if fault_injection is None:
        raise ValueError("qualification requires actual per-iteration fault evidence")
    parsed_fault = (
        fault_injection
        if isinstance(fault_injection, FaultInjectionQualification)
        else FaultInjectionQualification.model_validate(fault_injection)
    )

    cases: list[QualificationCaseResult] = []
    for definition in PIPELINE_QUALIFICATION_CASES:
        linked = [command_by_id[item] for item in definition.command_ids]
        case_api = (
            parsed_api
            if definition.surface_group == "formal"
            else parsed_development_api
        )
        case_browser = (
            parsed_browser
            if definition.surface_group == "formal"
            else parsed_development_browser
        )
        status = _combine_statuses(
            _case_status(
                linked,
                api_result=case_api,
                browser_result=case_browser,
            ),
            _fault_status_for_case(parsed_fault, definition.case_id),
        )
        digest_input = {
            "case_id": definition.case_id,
            "status": status,
            "command_output_digests": [item.output_digest for item in linked],
            "api_result": case_api,
            "browser_result": case_browser,
            "fault_lane_digests": [
                canonical_digest(lane)
                for lane in parsed_fault.lanes
                if definition.case_id in lane.case_ids
            ],
        }
        cases.append(
            QualificationCaseResult(
                case_id=definition.case_id,
                scenario=definition.scenario,
                required_result=definition.required_result,
                mandatory=definition.mandatory,
                status=status,
                command_ids=list(definition.command_ids),
                api_result=case_api,
                browser_result=case_browser,
                evidence_digest=canonical_digest(digest_input),
            )
        )
    summary_counts = {
        status: sum(item.status == status for item in cases)
        for status in (
            "passed",
            "failed",
            "not_run",
            "blocked_by_environment",
        )
    }
    summary = QualificationSummary(
        total=len(cases),
        mandatory=sum(item.mandatory for item in cases),
        **summary_counts,
    )
    overall_status: QualificationStatus
    mandatory_cases = [item for item in cases if item.mandatory]
    if all(item.status == "passed" for item in mandatory_cases):
        overall_status = "passed"
    elif any(item.status == "failed" for item in mandatory_cases):
        overall_status = "failed"
    elif any(item.status == "blocked_by_environment" for item in mandatory_cases):
        overall_status = "blocked_by_environment"
    else:
        overall_status = "not_run"

    normalized_time = _normalized_generated_at(generated_at)
    qualification_id = canonical_digest(
        {
            "schema_version": PIPELINE_QUALIFICATION_SCHEMA_VERSION,
            "source_revision": source_revision,
            "case_ids": [item.case_id for item in cases],
            "command_output_digests": [item.output_digest for item in all_commands],
        }
    )
    payload = {
        "schema_version": PIPELINE_QUALIFICATION_SCHEMA_VERSION,
        "qualification_id": qualification_id,
        "stage_task_id": PIPELINE_QUALIFICATION_TASK_ID,
        "enterprise_denominator": False,
        "source_revision": source_revision,
        "generated_at": normalized_time,
        "status": overall_status,
        "cases": cases,
        "commands": all_commands,
        "fault_injection": parsed_fault,
        "extra_evidence": [dict(item) for item in extra_evidence],
        "summary": summary,
    }
    return PipelineQualificationBundle(
        **payload,
        bundle_digest=canonical_digest(payload),
    )


def command_specs_by_id() -> dict[str, QualificationCommandSpec]:
    return {item.command_id: item for item in PIPELINE_QUALIFICATION_COMMANDS}
