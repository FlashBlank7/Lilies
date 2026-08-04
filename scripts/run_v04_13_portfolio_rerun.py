"""Plan and record the v0.4.13 six-project Builder portfolio rerun.

This module is deliberately a thin orchestration boundary.  It does not contain
workflow graphs, project answers, or a second Builder.  A real execution adapter
must connect the plan to an isolated platform and the selected Builder actor.
The command-line interface in this revision is read-only: list, validate, and
dry-run only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import re
import stat
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

if __package__:
    from scripts.experiments import real_project_testkit
else:
    from experiments import real_project_testkit


ROOT = Path(__file__).resolve().parents[1]
STANDALONE_LILIES_ROOT = (ROOT.parent / "LiliesAgent").resolve()
REAL_PROJECT_TESTKIT_PATH = ROOT / "scripts" / "experiments" / "real_project_testkit.py"
MAX_SESSION_TOKENS = 1_000_000
MAX_BUILD_REQUIREMENT_CHARACTERS = 30_000
CONTRACT_REVISION = 8
LILIES_BUILDER_ACTOR = "lilies"
CODEX_FALLBACK_BUILDER_ACTOR = "codex_fallback"
CODEX_FORMAL_BUILDER_ACTOR = "codex"
# Backward-compatible name for the original r7/Lilies route.  It must never be
# used to relabel a Codex protocol actor.
BUILDER_ACTOR = LILIES_BUILDER_ACTOR
RSA_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
LOCAL_BUILD_ENDPOINT = "/api/v1/local-lilies/applications/{application_id}/builds"
LOCAL_STATUS_ENDPOINT = "/api/v1/local-lilies/status"
LOCAL_PAIR_ENDPOINT = "/api/v1/local-lilies/connections"
LOCAL_MESSAGES_ENDPOINT = "/api/v1/local-lilies/assignments/{assignment_id}/messages"
LOCAL_EVENTS_ENDPOINT = "/api/v1/local-lilies/assignments/{assignment_id}/events"
LOCAL_USAGE_ENDPOINT = "/api/v1/local-lilies/connections/{connection_id}/usage"

PHASES = (
    "environment_bootstrap",
    "daemon_discovery",
    "explicit_pairing",
    "assignment_provision",
    "builder_execution",
    "host_result_verification",
    "platform_archive_verification",
    "cleanup_reporting",
)
PhaseName = Literal[
    "environment_bootstrap",
    "daemon_discovery",
    "explicit_pairing",
    "assignment_provision",
    "builder_execution",
    "host_result_verification",
    "platform_archive_verification",
    "cleanup_reporting",
]
HOOK_NAMES = (
    "environment",
    "assignment_provision",
    "public_debug",
    "sealed_seed",
    "cleanup",
)
TOKEN_MONITORED_PHASES = frozenset(PHASES[3:])
EXPECTED_SEED_IDS: Mapping[str, tuple[str, str, str]] = {
    "EXP-LILIES-001": ("101", "202", "303"),
    "EXP-LILIES-002": ("2201", "2202", "2203"),
    "EXP-LILIES-003": ("3301", "3302", "3303"),
    "EXP-LILIES-004": ("4101", "4102", "4103"),
    "EXP-LILIES-005": ("101", "202", "303"),
    "EXP-LILIES-006": ("6101", "6202", "6303"),
}
REAL_ADAPTER_CAPABILITY_GAP = (
    "The deterministic plan has no real platform/daemon/process adapter for the selected "
    "actor yet; list, validate, "
    "and dry-run are the only authorized modes in this revision."
)
CORE_ADAPTER_CAPABILITIES = (
    "real_project_testkit_public_api",
    "fresh_application_receipt",
    "task_credential_receipt",
    "assignment_receipt",
    "public_event_projection",
    "token_runtime_receipt",
    "publication_receipt",
    "acceptance_broker_receipt",
    "archive_receipt",
    "non_destructive_cleanup_receipt",
)
LILIES_DAEMON_ADAPTER_CAPABILITIES = (
    "daemon_discovery_receipt",
    "explicit_pairing_receipt",
)
CODEX_FALLBACK_ADAPTER_CAPABILITIES = (
    "codex_authoritative_usage_evidence",
    "public_only_forbidden_assistance_scan_receipt",
    "persisted_fallback_prerequisite_receipt",
)
ACTOR_INAPPLICABLE_PHASES = frozenset({"daemon_discovery", "explicit_pairing"})
EVENT_PHASE_BINDINGS: Mapping[str, str] = {
    "fresh_application": "environment_bootstrap",
    "daemon_discovered": "daemon_discovery",
    "pairing_completed": "explicit_pairing",
    "task_credential_bound": "assignment_provision",
    "assignment_created": "assignment_provision",
    "builder_message": "builder_execution",
    "tool_called": "builder_execution",
    "tool_completed": "builder_execution",
    "publication_completed": "builder_execution",
    "acceptance_case_completed": "host_result_verification",
    "archive_completed": "platform_archive_verification",
    "usage_checkpoint": "cleanup_reporting",
    "cleanup_completed": "cleanup_reporting",
    "error_observed": "builder_execution",
    "fix_applied": "builder_execution",
}
EVENT_KIND_BINDINGS: Mapping[str, str] = {
    "fresh_application": "tool_result",
    "daemon_discovered": "tool_result",
    "pairing_completed": "tool_result",
    "task_credential_bound": "tool_result",
    "assignment_created": "tool_result",
    "builder_message": "message",
    "tool_called": "tool_call",
    "tool_completed": "tool_result",
    "publication_completed": "tool_result",
    "acceptance_case_completed": "run",
    "archive_completed": "artifact",
    "usage_checkpoint": "token_usage",
    "cleanup_completed": "tool_result",
    "error_observed": "error",
    "fix_applied": "fix",
}
AGGREGATE_EVENT_NAMES = frozenset(
    {"acceptance_case_completed", "archive_completed", "usage_checkpoint"}
)
MINIMUM_COMPLETED_EVENT_COUNTS: Mapping[str, int] = {
    "fresh_application": 1,
    "daemon_discovered": 1,
    "pairing_completed": 1,
    "task_credential_bound": 1,
    "assignment_created": 1,
    "builder_message": 1,
    "tool_called": 1,
    "tool_completed": 1,
    "publication_completed": 1,
    "acceptance_case_completed": 4,
    "archive_completed": 1,
    "usage_checkpoint": 1,
    "cleanup_completed": 1,
}


def _hook_capability(project_id: str, hook_name: str) -> str:
    return f"hook:{project_id}:{hook_name}"


CORE_PHASE_ACTIONS: Mapping[str, tuple[str, ...]] = {
    "environment_bootstrap": ("create one isolated fresh application and environment",),
    "daemon_discovery": ("verify the exact isolated daemon discovery receipt",),
    "explicit_pairing": ("pair once using the expected daemon fingerprint",),
    "assignment_provision": ("provision one fresh Builder assignment and session",),
    "builder_execution": ("run the selected Builder only through public platform tools",),
    "host_result_verification": (
        "use real_project_testkit.py to run public debug plus three sealed cases",
    ),
    "platform_archive_verification": ("verify and archive the immutable result",),
    "cleanup_reporting": ("persist the redacted report and signed cleanup receipt",),
}
PUBLIC_TOP_LEVEL_FILES = frozenset(
    {
        "requirement.md",
        "BUILDER_API_MANUAL.json",
        "CUSTOMER_REQUIREMENT_PACKAGE.json",
        "program-profile.json",
    }
)
FORBIDDEN_PATH_SEGMENTS = frozenset(
    {
        "protected",
        "hidden-inputs",
        "expected-state",
        "oracle",
        "platform-data",
        "platform_data",
        ".git",
    }
)
FORBIDDEN_COMMAND_FRAGMENTS = (
    "build_workflow_via_api.py",
    "run_v04_13_codex_builder.py",
    "sqlite3",
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
SAFE_GENERATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
UNSAFE_EVENT_SUMMARY_PATTERN = re.compile(
    r"(?i)(\b(?:password|passwd|secret|bearer|token|oracle|expected|actual|"
    r"credential|cookie|sealed|comparison)\b|api[_-]?key|authorization|"
    r"client[_ -]?secret|private[_ -]?key|protected/|hidden-inputs|"
    r"hidden[_ -]?seed|expected-state|raw[_ -]?(?:input|output)|"
    r"(?:input|output)[_ -]?payload|expected[_ -]?versus[_ -]?actual)"
)


class PortfolioRerunError(RuntimeError):
    """The portfolio rerun cannot advance without weakening its boundary."""


def _require_public_identifier(value: str, *, field_name: str) -> None:
    if (
        SAFE_GENERATION_PATTERN.fullmatch(value) is None
        or UNSAFE_EVENT_SUMMARY_PATTERN.search(value) is not None
    ):
        raise PortfolioRerunError(f"{field_name} is not a safe public identifier")


@dataclass(frozen=True)
class CommandShape:
    """A redacted task-author command template, never a Builder command."""

    argv: tuple[str, ...]
    moment: Literal["before_platform_run", "after_platform_run", "cleanup"]
    purpose: str


@dataclass(frozen=True)
class HookSpec:
    route: Literal["commands", "broker_capability", "gap"]
    commands: tuple[CommandShape, ...] = ()
    capability_id: str | None = None
    capability_gap: str | None = None

    @property
    def available(self) -> bool:
        return self.route != "gap"

    @classmethod
    def command_route(cls, commands: tuple[CommandShape, ...]) -> HookSpec:
        return cls(route="commands", commands=commands)

    @classmethod
    def broker_capability(cls, capability_id: str) -> HookSpec:
        return cls(route="broker_capability", capability_id=capability_id)

    @classmethod
    def gap(cls, reason: str) -> HookSpec:
        return cls(route="gap", capability_gap=reason)


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    revision: int
    public_materials: tuple[str, ...]
    hooks: Mapping[str, HookSpec]
    seed_ids: tuple[str, str, str]
    real_adapter_gap: str | None = REAL_ADAPTER_CAPABILITY_GAP

    @property
    def package_root(self) -> Path:
        return (
            ROOT
            / "docs"
            / "experiments"
            / "lilies-collaboration"
            / self.project_id
            / str(self.revision)
        )


@dataclass(frozen=True)
class CodexFallbackEligibility:
    """References task-author signed and durably persisted r8 prerequisite evidence."""

    contract_revision: int
    prerequisite_receipt_id: str
    prerequisite_payload_digest: str
    bounded_lilies_attempt_id: str
    bounded_lilies_attempt_report_digest: str
    bounded_lilies_terminal_receipt_id: str
    bounded_lilies_terminal_receipt_digest: str
    isolated_context_id: str
    public_material_allowlist_digest: str
    forbidden_assistance_scan_receipt_id: str
    forbidden_assistance_scan_digest: str
    freshness_identity_digest: str


@dataclass(frozen=True)
class BuilderActorProfile:
    """Protocol actor and report actor are distinct only for the r8 fallback."""

    formal_builder_actor: Literal["lilies", "codex"]
    builder_actor: Literal["lilies", "codex_fallback"]
    requires_daemon_access: bool

    @property
    def inapplicable_phases(self) -> frozenset[str]:
        if self.requires_daemon_access:
            return frozenset()
        return ACTOR_INAPPLICABLE_PHASES


LILIES_ACTOR_PROFILE = BuilderActorProfile(
    formal_builder_actor=LILIES_BUILDER_ACTOR,
    builder_actor=LILIES_BUILDER_ACTOR,
    requires_daemon_access=True,
)
CODEX_FALLBACK_ACTOR_PROFILE = BuilderActorProfile(
    formal_builder_actor=CODEX_FORMAL_BUILDER_ACTOR,
    builder_actor=CODEX_FALLBACK_BUILDER_ACTOR,
    requires_daemon_access=False,
)


def _require_supported_actor_profile(profile: BuilderActorProfile) -> None:
    if profile not in {LILIES_ACTOR_PROFILE, CODEX_FALLBACK_ACTOR_PROFILE}:
        raise PortfolioRerunError("Builder actor profile is not an r8 supported route")


@dataclass(frozen=True)
class RealProjectTestkitAPI:
    """Callable public-API surface used by real portfolio adapters."""

    run_workflow: Callable[..., dict[str, Any]]
    wait_run: Callable[..., dict[str, Any]]
    run_trace: Callable[..., list[dict[str, Any]]]
    write_report: Callable[[Path, dict[str, Any]], None]

    @classmethod
    def load(cls) -> RealProjectTestkitAPI:
        return cls(
            run_workflow=real_project_testkit.run_workflow,
            wait_run=real_project_testkit.wait_run,
            run_trace=real_project_testkit.run_trace,
            write_report=real_project_testkit.write_report,
        )


@dataclass(frozen=True)
class TokenUsageGroup:
    stage: str
    model: str
    recorded_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    unknown_calls: int = 0


@dataclass(frozen=True)
class TokenCheckpoint:
    session_id: str
    attempted_calls: int
    recorded_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    runtime_cap_tokens: int
    model_call_attempt_receipt_ids: tuple[str, ...] = ()
    model_call_receipt_ids: tuple[str, ...] = ()
    hard_stop_triggered: bool = False
    cap_reached_at_receipt_id: str | None = None
    hard_stop_fence_receipt_id: str | None = None
    post_hard_stop_attempts: int = 0
    unknown_calls: int = 0
    groups: tuple[TokenUsageGroup, ...] = ()


@dataclass(frozen=True)
class CodexTokenUsageEvidence:
    """Authoritative Codex usage when exposed, otherwise an explicit non-numeric state."""

    session_id: str
    availability: Literal["exact", "unavailable", "unknown"]
    authoritative_source: str
    attempted_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    reason: str | None = None


@dataclass(frozen=True)
class NamedDigest:
    name: str
    digest: str


@dataclass(frozen=True)
class SignedReceiptEnvelope:
    receipt_id: str
    issuer: str
    key_id: str
    issued_at: str
    semantic_type: str
    semantic_payload: Mapping[str, Any]
    payload_digest: str
    signature: str


@dataclass(frozen=True)
class ReceiptTrustRoot:
    """Task-author-supplied RSA public identity; no production key is embedded."""

    issuer: str
    key_id: str
    rsa_modulus: int
    rsa_exponent: int

    @property
    def verifier_digest(self) -> str:
        payload = (
            f"rsa-sha256:{self.issuer}:{self.key_id}:"
            f"{self.rsa_modulus}:{self.rsa_exponent}"
        ).encode("ascii")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class AdapterCapabilityReceipt:
    project_id: str
    adapter_id: str
    adapter_digest: str
    capability_digest: str
    capabilities: tuple[str, ...]
    envelope: SignedReceiptEnvelope


@dataclass(frozen=True)
class FreshDraftReceipt:
    receipt_id: str
    application_id: str
    draft_revision: int
    node_count: int
    edge_count: int
    draft_content_hash: str
    observed_at: str


@dataclass(frozen=True)
class DaemonAccessReceipt:
    discovery_receipt_id: str
    pairing_receipt_id: str
    task_credential_receipt_id: str
    connection_id: str
    daemon_fingerprint: str
    daemon_base_url: str
    task_credential_digest: str
    exact_discovery_match: bool
    exact_pairing_match: bool


@dataclass(frozen=True)
class TaskAccessReceipt:
    task_credential_receipt_id: str
    task_credential_digest: str


@dataclass(frozen=True)
class PublicationReceipt:
    receipt_id: str
    application_id: str
    published_version: int
    published_content_hash: str
    published_at: str


@dataclass(frozen=True)
class MutationGuardReceipt:
    published_version: int
    published_content_hash: str
    post_acceptance_version: int
    post_acceptance_content_hash: str
    mutations_after_publish: int


@dataclass(frozen=True)
class AcceptanceCaseReceipt:
    case_id: str
    run_id: str
    receipt_id: str
    environment_generation: str
    published_version: int
    published_content_hash: str
    status: Literal["passed", "failed"]
    aggregate_receipt_digest: str
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class MLLifecycleEvidence:
    application_id: str
    published_version: int
    workflow_content_hash: str
    archive_id: str
    chronological_split_receipt_digest: str
    training_window_end: str
    evaluation_window_start: str
    fit_train_receipt_digest: str
    trained_at: str
    evaluation_receipt_digest: str
    backtest_receipt_digest: str
    evaluated_at: str
    immutable_model_version: str
    model_content_digest: str
    promotion_receipt_digest: str
    promoted_at: str
    deployment_receipt_digest: str
    deployed_at: str
    inference_run_id: str
    inference_receipt_digest: str
    inferred_at: str
    retraining_trigger_receipt_digest: str
    retraining_evaluated_at: str


@dataclass(frozen=True)
class SafeExecutionEvidence:
    """Structured identity and aggregate receipts; never raw case data or diffs."""

    attempt_id: str
    builder_actor: str
    formal_builder_actor: str
    fallback_eligibility: CodexFallbackEligibility | None
    trusted_verifier_id: str
    trusted_verifier_digest: str
    receipt_chain_digest: str
    sibling_commit: str | None
    sibling_package_digest: str | None
    application_id: str
    assignment_receipt_id: str
    published_version: int
    published_content_hash: str
    assignment_id: str
    session_id: str
    environment_generation: str
    environment_receipt_id: str
    archive_id: str
    archive_receipt_id: str
    archive_digest: str
    public_material_digests: tuple[NamedDigest, ...]
    public_interface_digest: str
    fresh_empty_draft: FreshDraftReceipt
    task_access: TaskAccessReceipt
    daemon_access: DaemonAccessReceipt | None
    publication: PublicationReceipt
    mutation_guard: MutationGuardReceipt
    acceptance_receipts: tuple[AcceptanceCaseReceipt, ...]
    ml_lifecycle: MLLifecycleEvidence | None = None


@dataclass(frozen=True)
class ObservableEvent:
    """A safe public/aggregate event; private reasoning has no field here."""

    kind: Literal[
        "message",
        "tool_call",
        "tool_result",
        "error",
        "fix",
        "test",
        "run",
        "artifact",
        "token_usage",
    ]
    name: Literal[
        "fresh_application",
        "daemon_discovered",
        "pairing_completed",
        "task_credential_bound",
        "assignment_created",
        "builder_message",
        "tool_called",
        "tool_completed",
        "publication_completed",
        "acceptance_case_completed",
        "archive_completed",
        "usage_checkpoint",
        "cleanup_completed",
        "error_observed",
        "fix_applied",
    ]
    summary: str
    entity_id: str | None = None
    receipt_digest: str | None = None
    visibility: Literal["public", "aggregate_only"] = "public"
    safe_projection: Literal["platform_public", "aggregate_receipt"] = "platform_public"
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: str = ""


@dataclass(frozen=True)
class PhaseExecution:
    outcome: Literal["completed", "skipped", "failed", "not_applicable"]
    events: tuple[ObservableEvent, ...] = ()
    token_checkpoint: TokenCheckpoint | None = None
    codex_token_usage: CodexTokenUsageEvidence | None = None
    signed_receipts: tuple[SignedReceiptEnvelope, ...] = ()


@dataclass(frozen=True)
class PhaseSpan:
    phase: str
    started_at: str
    finished_at: str
    duration_seconds: float
    duration_percentage: float
    outcome: str


@dataclass(frozen=True)
class ProjectExecutionReport:
    attempt_id: str
    project_id: str
    manifest_revision: int
    builder_actor: Literal["lilies", "codex_fallback"]
    formal_builder_actor: Literal["lilies", "codex"]
    status: Literal["completed", "failed"]
    phases: tuple[PhaseSpan, ...]
    total_elapsed_seconds: float
    timing_residual_seconds: float
    observable_event_counts: Mapping[str, int]
    observable_events: tuple[ObservableEvent, ...]
    output_summaries: tuple[str, ...]
    final_token_checkpoint: TokenCheckpoint | None
    final_codex_token_usage: CodexTokenUsageEvidence | None
    execution_evidence: SafeExecutionEvidence | None
    serialized_report_body_digest: str
    failure: str | None = None
    cleanup_failure: str | None = None


def _python_command(script: str, *arguments: str, moment: str, purpose: str) -> CommandShape:
    return CommandShape(
        argv=(".venv/bin/python", script, *arguments),
        moment=moment,  # type: ignore[arg-type]
        purpose=purpose,
    )


def _docker_command(*arguments: str, moment: str, purpose: str) -> CommandShape:
    return CommandShape(
        argv=("docker", *arguments),
        moment=moment,  # type: ignore[arg-type]
        purpose=purpose,
    )


def _manifest_001() -> ProjectManifest:
    package = "docs/experiments/lilies-collaboration/EXP-LILIES-001/28"
    control = "scripts/experiments/exp_lilies_001/environment_control.py"
    common = ("--state-root", "{environment_state_root}", "--package-root", package)
    return ProjectManifest(
        project_id="EXP-LILIES-001",
        revision=28,
        public_materials=(
            "requirement.md",
            "CUSTOMER_REQUIREMENT_PACKAGE.json",
            "BUILDER_API_MANUAL.json",
        ),
        seed_ids=EXPECTED_SEED_IDS["EXP-LILIES-001"],
        hooks={
            "environment": HookSpec.command_route(
                (
                    _python_command(
                        control,
                        *common,
                        "up",
                        moment="before_platform_run",
                        purpose="start the isolated Paperless-ngx and InvenTree host",
                    ),
                    _python_command(
                        control,
                        *common,
                        "initialize",
                        moment="before_platform_run",
                        purpose="initialize the attempt-scoped customer state",
                    ),
                )
            ),
            "assignment_provision": HookSpec.broker_capability(
                "platform.assignment-provision.v1"
            ),
            "public_debug": HookSpec.command_route(
                (
                    _python_command(
                        control,
                        *common,
                        "seed",
                        "--seed",
                        "debug",
                        moment="before_platform_run",
                        purpose="load only the authorized public debug case",
                    ),
                    _python_command(
                        control,
                        *common,
                        "snapshot",
                        "--seed",
                        "debug",
                        "--phase",
                        "baseline",
                        moment="before_platform_run",
                        purpose="record the public-debug host baseline",
                    ),
                    _python_command(
                        control,
                        *common,
                        "snapshot",
                        "--seed",
                        "debug",
                        "--phase",
                        "final",
                        moment="after_platform_run",
                        purpose="record the public-debug host result",
                    ),
                )
            ),
            "sealed_seed": HookSpec.command_route(
                (
                    _python_command(
                        control,
                        *common,
                        "seed",
                        "--seed",
                        "{seed_id}",
                        moment="before_platform_run",
                        purpose="stream one sealed seed into the customer host",
                    ),
                    _python_command(
                        control,
                        *common,
                        "snapshot",
                        "--seed",
                        "{seed_id}",
                        "--phase",
                        "baseline",
                        moment="before_platform_run",
                        purpose="record the sealed host baseline",
                    ),
                    _python_command(
                        control,
                        *common,
                        "snapshot",
                        "--seed",
                        "{seed_id}",
                        "--phase",
                        "final",
                        moment="after_platform_run",
                        purpose="submit the sealed host result to the formal acceptance broker",
                    ),
                )
            ),
            "cleanup": HookSpec.command_route(
                (
                    _python_command(
                        control,
                        *common,
                        "down",
                        moment="cleanup",
                        purpose="stop the project host without deleting its volumes",
                    ),
                )
            ),
        },
    )


def _manifest_002() -> ProjectManifest:
    package = "docs/experiments/lilies-collaboration/EXP-LILIES-002/1"
    scripts = "scripts/experiments/exp_lilies_002"
    return ProjectManifest(
        project_id="EXP-LILIES-002",
        revision=1,
        public_materials=(
            "requirement.md",
            "CUSTOMER_REQUIREMENT_PACKAGE.json",
            "BUILDER_API_MANUAL.json",
        ),
        seed_ids=EXPECTED_SEED_IDS["EXP-LILIES-002"],
        hooks={
            "environment": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/environment_control.py",
                        "ensure",
                        moment="before_platform_run",
                        purpose="start and provision the controlled BookStack host",
                    ),
                )
            ),
            "assignment_provision": HookSpec.broker_capability(
                "platform.assignment-provision.v1"
            ),
            "public_debug": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_public_debug.py",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--bookstack-base",
                        "{bookstack_base}",
                        "--credential-file",
                        "{bookstack_credential_file}",
                        "--sources-file",
                        f"{package}/fixtures/public-inputs/bookstack-pages.json",
                        "--fixtures-file",
                        f"{package}/fixtures/public-inputs/permission-matrix.json",
                        "--cases-file",
                        f"{package}/fixtures/public-inputs/debug-cases.json",
                        "--workspace-root",
                        "{attempt_workspace}",
                        "--report-file",
                        "{public_debug_report}",
                        moment="before_platform_run",
                        purpose="exercise the public RAG cases and host lifecycle",
                    ),
                )
            ),
            "sealed_seed": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_sealed_seed.py",
                        "--seed",
                        "{seed_id}",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--bookstack-base",
                        "{bookstack_base}",
                        "--credential-file",
                        "{bookstack_credential_file}",
                        "--workspace-root",
                        "{seed_workspace}",
                        "--report-file",
                        "{sealed_summary_file}",
                        moment="before_platform_run",
                        purpose="run one isolated sealed BookStack acceptance seed",
                    ),
                )
            ),
            "cleanup": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/environment_control.py",
                        "stop",
                        moment="cleanup",
                        purpose="stop the BookStack host without deleting volumes",
                    ),
                )
            ),
        },
    )


def _manifest_003() -> ProjectManifest:
    scripts = "scripts/experiments/exp_lilies_003"
    gap = (
        "No single controlled lifecycle route currently starts, provisions, health-checks, "
        "and stops Home Assistant plus the notification sink."
    )
    return ProjectManifest(
        project_id="EXP-LILIES-003",
        revision=1,
        public_materials=(
            "requirement.md",
            "schemas/deliverables.json",
            "schemas/home-assistant-event.json",
        ),
        seed_ids=EXPECTED_SEED_IDS["EXP-LILIES-003"],
        hooks={
            "environment": HookSpec.gap(gap),
            "assignment_provision": HookSpec.broker_capability(
                "platform.assignment-provision.v1"
            ),
            "public_debug": HookSpec.gap(
                "The existing collector observes a subscription but does not drive the "
                "public cases through a generic reset-and-run contract."
            ),
            "sealed_seed": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_sealed_seed.py",
                        "--seed",
                        "{seed_id}",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--home-assistant-base",
                        "{home_assistant_base}",
                        "--credential-file",
                        "{home_assistant_credential_file}",
                        "--sink-base",
                        "{notification_sink_base}",
                        "--sink-token",
                        "{secret:notification_sink_token}",
                        "--report-file",
                        "{sealed_summary_file}",
                        moment="before_platform_run",
                        purpose="run one isolated sealed facility-event seed",
                    ),
                )
            ),
            "cleanup": HookSpec.gap(gap),
        },
    )


def _manifest_004() -> ProjectManifest:
    package = "docs/experiments/lilies-collaboration/EXP-LILIES-004/1"
    scripts = "scripts/experiments/exp_lilies_004"
    return ProjectManifest(
        project_id="EXP-LILIES-004",
        revision=1,
        public_materials=(
            "requirement.md",
            "CUSTOMER_REQUIREMENT_PACKAGE.json",
            "BUILDER_API_MANUAL.json",
        ),
        seed_ids=EXPECTED_SEED_IDS["EXP-LILIES-004"],
        hooks={
            "environment": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/environment_control.py",
                        "ensure",
                        moment="before_platform_run",
                        purpose="start and provision the controlled ThingsBoard host",
                    ),
                )
            ),
            "assignment_provision": HookSpec.broker_capability(
                "platform.assignment-provision.v1"
            ),
            "public_debug": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_public_debug.py",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--events-file",
                        f"{package}/fixtures/public-inputs/debug-events.json",
                        "--drift-file",
                        f"{package}/fixtures/public-inputs/drift-window.json",
                        "--workspace-root",
                        "{attempt_workspace}",
                        "--report-file",
                        "{public_debug_report}",
                        moment="before_platform_run",
                        purpose="exercise public inference, alarm, review, and drift cases",
                    ),
                )
            ),
            "sealed_seed": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_sealed_seed.py",
                        "--seed",
                        "{seed_id}",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--workspace-root",
                        "{seed_workspace}",
                        "--summary-file",
                        "{sealed_summary_file}",
                        moment="before_platform_run",
                        purpose="run one isolated predictive-maintenance seed",
                    ),
                )
            ),
            "cleanup": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/environment_control.py",
                        "stop",
                        moment="cleanup",
                        purpose="stop the ThingsBoard host without deleting volumes",
                    ),
                )
            ),
        },
    )


def _manifest_005() -> ProjectManifest:
    scripts = "scripts/experiments/exp_lilies_005"
    gap = (
        "No generic controlled route currently starts, resets, health-checks, and stops "
        "the Actual customer environment."
    )
    return ProjectManifest(
        project_id="EXP-LILIES-005",
        revision=1,
        public_materials=(
            "requirement.md",
            "CUSTOMER_REQUIREMENT_PACKAGE.json",
            "BUILDER_API_MANUAL.json",
            "schemas/receipt-batch.json",
            "schemas/reconciliation-output.json",
            "program-profile.json",
        ),
        seed_ids=EXPECTED_SEED_IDS["EXP-LILIES-005"],
        hooks={
            "environment": HookSpec.gap(gap),
            "assignment_provision": HookSpec.broker_capability(
                "platform.assignment-provision.v1"
            ),
            "public_debug": HookSpec.gap(
                "Only the sealed runner exists; there is no public reset-and-debug route "
                "with aggregate evidence for the authorized receipt fixture."
            ),
            "sealed_seed": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_sealed_seed.py",
                        "--seed",
                        "{seed_id}",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--actual-cli",
                        "{actual_cli}",
                        "--workspace-root",
                        "{seed_workspace}",
                        "--summary-file",
                        "{sealed_summary_file}",
                        moment="before_platform_run",
                        purpose="run one isolated accounting-reconciliation seed",
                    ),
                )
            ),
            "cleanup": HookSpec.gap(gap),
        },
    )


def _manifest_006() -> ProjectManifest:
    package = "docs/experiments/lilies-collaboration/EXP-LILIES-006/1"
    scripts = "scripts/experiments/exp_lilies_006"
    compose = f"{scripts}/compose.yaml"
    return ProjectManifest(
        project_id="EXP-LILIES-006",
        revision=1,
        public_materials=(
            "requirement.md",
            "CUSTOMER_REQUIREMENT_PACKAGE.json",
            "BUILDER_API_MANUAL.json",
            "schemas/erpnext-planning.openapi.json",
        ),
        seed_ids=EXPECTED_SEED_IDS["EXP-LILIES-006"],
        hooks={
            "environment": HookSpec.command_route(
                (
                    _docker_command(
                        "compose",
                        "-f",
                        compose,
                        "up",
                        "-d",
                        moment="before_platform_run",
                        purpose="start only the ERPNext project host",
                    ),
                    _python_command(
                        f"{scripts}/provision_erpnext.py",
                        "--base-url",
                        "{erpnext_base}",
                        "--administrator-password",
                        "{secret:erpnext_admin_password}",
                        moment="before_platform_run",
                        purpose="provision the attempt-scoped ERPNext business data",
                    ),
                )
            ),
            "assignment_provision": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/prepare_connector_via_api.py",
                        "--base-url",
                        "{platform_base}",
                        "--token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--erpnext-base",
                        "{erpnext_base}",
                        "--schema-file",
                        f"{package}/schemas/erpnext-planning.openapi.json",
                        moment="before_platform_run",
                        purpose="register the ERPNext connector through platform API",
                    ),
                )
            ),
            "public_debug": HookSpec.broker_capability(
                "formal-acceptance.public-debug.v1"
            ),
            "sealed_seed": HookSpec.command_route(
                (
                    _python_command(
                        f"{scripts}/run_sealed_seed.py",
                        "--seed",
                        "{seed_id}",
                        "--platform-base",
                        "{platform_base}",
                        "--platform-token",
                        "{secret:platform_token}",
                        "--application-id",
                        "{application_id}",
                        "--erpnext-base",
                        "{erpnext_base}",
                        "--erpnext-token",
                        "{secret:erpnext_token}",
                        "--workspace-root",
                        "{seed_workspace}",
                        moment="before_platform_run",
                        purpose="run one isolated replenishment-planning seed",
                    ),
                )
            ),
            "cleanup": HookSpec.command_route(
                (
                    _docker_command(
                        "compose",
                        "-f",
                        compose,
                        "down",
                        "--remove-orphans",
                        moment="cleanup",
                        purpose="stop the ERPNext host while retaining its volumes",
                    ),
                )
            ),
        },
    )


PROJECT_MANIFESTS: Mapping[str, ProjectManifest] = {
    manifest.project_id: manifest
    for manifest in (
        _manifest_001(),
        _manifest_002(),
        _manifest_003(),
        _manifest_004(),
        _manifest_005(),
        _manifest_006(),
    )
}


def _validate_public_material(relative_path: str) -> str | None:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        return f"public material path escapes the package: {relative_path}"
    if any(part.lower() in FORBIDDEN_PATH_SEGMENTS for part in path.parts):
        return f"public material path enters a forbidden boundary: {relative_path}"
    allowed = (
        relative_path in PUBLIC_TOP_LEVEL_FILES
        or relative_path.startswith("schemas/")
    )
    if not allowed:
        return f"public material type is not allowlisted: {relative_path}"
    return None


def _validate_command(command: CommandShape, *, cleanup: bool) -> list[str]:
    errors: list[str] = []
    if not command.argv:
        return ["task-author command is empty"]
    rendered = " ".join(command.argv).lower()
    if any(fragment in rendered for fragment in FORBIDDEN_COMMAND_FRAGMENTS):
        errors.append(f"task-author command uses a forbidden Builder route: {rendered}")
    for argument in command.argv:
        path = PurePosixPath(argument)
        if any(part.lower() in FORBIDDEN_PATH_SEGMENTS for part in path.parts):
            errors.append(f"task-author command exposes a forbidden path: {argument}")
    if cleanup and any(value in {"-v", "--volumes"} for value in command.argv):
        errors.append("cleanup must retain customer-environment volumes")
    return errors


def validate_manifest(manifest: ProjectManifest, *, read_materials: bool = True) -> list[str]:
    errors: list[str] = []
    if manifest.project_id not in {f"EXP-LILIES-{index:03d}" for index in range(1, 7)}:
        errors.append(f"unsupported project id: {manifest.project_id}")
    if manifest.revision < 1:
        errors.append(f"{manifest.project_id}: revision must be positive")
    expected_seed_ids = EXPECTED_SEED_IDS.get(manifest.project_id)
    if expected_seed_ids is not None and manifest.seed_ids != expected_seed_ids:
        errors.append(
            f"{manifest.project_id}: sealed seed ids must be "
            + ", ".join(expected_seed_ids)
        )
    if set(manifest.hooks) != set(HOOK_NAMES):
        errors.append(f"{manifest.project_id}: hook names are incomplete")

    for relative_path in manifest.public_materials:
        material_error = _validate_public_material(relative_path)
        if material_error:
            errors.append(f"{manifest.project_id}: {material_error}")
            continue
        if read_materials and not (manifest.package_root / relative_path).is_file():
            errors.append(f"{manifest.project_id}: public material is missing: {relative_path}")

    for name, hook in manifest.hooks.items():
        if hook.route == "commands":
            if not hook.commands:
                errors.append(f"{manifest.project_id}: {name} command route is empty")
            if hook.capability_id is not None or hook.capability_gap is not None:
                errors.append(f"{manifest.project_id}: {name} command route is ambiguous")
        elif hook.route == "broker_capability":
            if hook.commands or hook.capability_gap is not None:
                errors.append(f"{manifest.project_id}: {name} broker route is ambiguous")
            if hook.capability_id is None:
                errors.append(f"{manifest.project_id}: {name} broker capability is empty")
            else:
                try:
                    _require_public_identifier(
                        hook.capability_id,
                        field_name=f"{manifest.project_id}/{name} broker capability",
                    )
                except PortfolioRerunError as error:
                    errors.append(str(error))
        elif hook.route == "gap":
            if hook.commands or hook.capability_id is not None:
                errors.append(f"{manifest.project_id}: {name} gap route is ambiguous")
            if hook.capability_gap is None or not hook.capability_gap.strip():
                errors.append(f"{manifest.project_id}: {name} capability gap is empty")
        else:
            errors.append(f"{manifest.project_id}: {name} hook route is invalid")
        for command in hook.commands:
            errors.extend(
                f"{manifest.project_id}: {name}: {error}"
                for error in _validate_command(command, cleanup=name == "cleanup")
            )
    if read_materials and not errors:
        try:
            build_public_requirement(manifest)
        except PortfolioRerunError as error:
            errors.append(str(error))
    return errors


def build_public_requirement(
    manifest: ProjectManifest,
    actor_profile: BuilderActorProfile = LILIES_ACTOR_PROFILE,
) -> str:
    """Build the exact Builder prompt from explicitly allowlisted public files."""

    _require_supported_actor_profile(actor_profile)
    errors = [
        error
        for relative_path in manifest.public_materials
        if (error := _validate_public_material(relative_path)) is not None
    ]
    if errors:
        raise PortfolioRerunError("; ".join(errors))

    if actor_profile == LILIES_ACTOR_PROFILE:
        actor_instruction = "You are Lilies, the Builder."
    else:
        actor_instruction = (
            "You are Codex acting as the r8 fallback Builder. Your raw protocol identity "
            "remains codex; the evidence adapter may label only an eligible final route as "
            "codex_fallback. Do not claim to be Lilies."
        )
    sections = [
        (
            f"{actor_instruction} Build this workflow from a fresh empty application "
            "using only the public platform contract and tools granted by the platform. "
            "Do not inspect source code, platform storage, or sealed acceptance data. "
            "Explain actions through public messages; never expose private reasoning."
        )
    ]
    for relative_path in manifest.public_materials:
        path = manifest.package_root / relative_path
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise PortfolioRerunError(
                f"{manifest.project_id}: cannot read public material {relative_path}: {error}"
            ) from error
        sections.append(f"\n--- PUBLIC MATERIAL: {relative_path} ---\n{content.strip()}")
    requirement = "\n".join(sections)
    if len(requirement) > MAX_BUILD_REQUIREMENT_CHARACTERS:
        raise PortfolioRerunError(
            f"{manifest.project_id}: public Builder requirement has {len(requirement)} "
            f"characters, above the {MAX_BUILD_REQUIREMENT_CHARACTERS} bridge limit"
        )
    return requirement


def public_material_digest_receipts(manifest: ProjectManifest) -> tuple[NamedDigest, ...]:
    """Hash only the public contract files exposed to the Builder."""

    receipts: list[NamedDigest] = []
    for relative_path in manifest.public_materials:
        error = _validate_public_material(relative_path)
        if error is not None:
            raise PortfolioRerunError(error)
        path = manifest.package_root / relative_path
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise PortfolioRerunError(
                f"{manifest.project_id}: cannot hash public material {relative_path}: {error}"
            ) from error
        receipts.append(
            NamedDigest(
                name=relative_path,
                digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            )
        )
    return tuple(receipts)


def _require_sha256(value: str, *, field_name: str) -> None:
    if SHA256_PATTERN.fullmatch(value) is None:
        raise PortfolioRerunError(f"{field_name} is not a sha256 digest")


def _require_uuid(value: str, *, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise PortfolioRerunError(f"{field_name} is not a UUID") from error
    if str(parsed) != value.lower():
        raise PortfolioRerunError(f"{field_name} is not a canonical UUID")


def validate_codex_fallback_eligibility(
    eligibility: CodexFallbackEligibility,
) -> None:
    """Validate signed-reference shapes; truth is checked against receipts and ledger."""

    if eligibility.contract_revision != CONTRACT_REVISION:
        raise PortfolioRerunError("Codex fallback eligibility is not bound to contract r8")
    _require_uuid(
        eligibility.prerequisite_receipt_id,
        field_name="Codex fallback prerequisite receipt id",
    )
    _require_sha256(
        eligibility.prerequisite_payload_digest,
        field_name="Codex fallback prerequisite payload digest",
    )
    _require_uuid(
        eligibility.bounded_lilies_attempt_id,
        field_name="bounded Lilies attempt id",
    )
    _require_sha256(
        eligibility.bounded_lilies_attempt_report_digest,
        field_name="bounded Lilies attempt report digest",
    )
    _require_uuid(
        eligibility.bounded_lilies_terminal_receipt_id,
        field_name="bounded Lilies terminal receipt id",
    )
    _require_sha256(
        eligibility.bounded_lilies_terminal_receipt_digest,
        field_name="bounded Lilies terminal receipt digest",
    )
    _require_public_identifier(
        eligibility.isolated_context_id,
        field_name="isolated Codex context id",
    )
    _require_sha256(
        eligibility.public_material_allowlist_digest,
        field_name="public material allowlist digest",
    )
    _require_uuid(
        eligibility.forbidden_assistance_scan_receipt_id,
        field_name="forbidden-assistance scan receipt id",
    )
    for field_name, value in (
        ("forbidden-assistance scan digest", eligibility.forbidden_assistance_scan_digest),
        ("fallback freshness identity digest", eligibility.freshness_identity_digest),
    ):
        _require_sha256(value, field_name=field_name)


def _fallback_freshness_identities(
    evidence: SafeExecutionEvidence,
) -> dict[str, str]:
    assert evidence.fallback_eligibility is not None
    return {
        "application_id": evidence.application_id,
        "environment_generation": evidence.environment_generation,
        "assignment_id": evidence.assignment_id,
        "session_id": evidence.session_id,
        "isolated_context_id": evidence.fallback_eligibility.isolated_context_id,
    }


def _fallback_scan_payload(
    project_id: str,
    evidence: SafeExecutionEvidence,
) -> dict[str, Any]:
    assert evidence.fallback_eligibility is not None
    eligibility = evidence.fallback_eligibility
    return {
        "project_id": project_id,
        "attempt_id": evidence.attempt_id,
        "contract_revision": CONTRACT_REVISION,
        "formal_builder_actor": CODEX_FORMAL_BUILDER_ACTOR,
        "builder_actor": CODEX_FALLBACK_BUILDER_ACTOR,
        "isolated_context_id": eligibility.isolated_context_id,
        "public_material_allowlist_digest": (
            eligibility.public_material_allowlist_digest
        ),
        "task_scoped_public_api_only": True,
        "source_or_protected_content_exposed": False,
        "historical_attempt": False,
        "result": "pass",
    }


def _fallback_prerequisite_payload(
    project_id: str,
    evidence: SafeExecutionEvidence,
) -> dict[str, Any]:
    assert evidence.fallback_eligibility is not None
    eligibility = evidence.fallback_eligibility
    return {
        "project_id": project_id,
        "attempt_id": evidence.attempt_id,
        "contract_revision": CONTRACT_REVISION,
        "formal_builder_actor": CODEX_FORMAL_BUILDER_ACTOR,
        "builder_actor": CODEX_FALLBACK_BUILDER_ACTOR,
        "bounded_lilies_attempt_id": eligibility.bounded_lilies_attempt_id,
        "bounded_lilies_attempt_report_digest": (
            eligibility.bounded_lilies_attempt_report_digest
        ),
        "bounded_lilies_terminal_receipt_id": (
            eligibility.bounded_lilies_terminal_receipt_id
        ),
        "bounded_lilies_terminal_receipt_digest": (
            eligibility.bounded_lilies_terminal_receipt_digest
        ),
        "freshness_identities": _fallback_freshness_identities(evidence),
        "freshness_identity_digest": eligibility.freshness_identity_digest,
        "forbidden_assistance_scan_receipt_id": (
            eligibility.forbidden_assistance_scan_receipt_id
        ),
        "forbidden_assistance_scan_digest": (
            eligibility.forbidden_assistance_scan_digest
        ),
    }


def actor_profile_for_evidence(
    evidence: SafeExecutionEvidence,
) -> BuilderActorProfile:
    """Validate the recorded actor pair without rewriting either identity."""

    if evidence.builder_actor == LILIES_BUILDER_ACTOR:
        if evidence.formal_builder_actor != LILIES_BUILDER_ACTOR:
            raise PortfolioRerunError("Lilies evidence changed its raw protocol actor")
        if evidence.fallback_eligibility is not None:
            raise PortfolioRerunError("Lilies evidence cannot carry Codex fallback eligibility")
        if evidence.daemon_access is None:
            raise PortfolioRerunError("Lilies evidence requires daemon access")
        if evidence.sibling_commit is None or evidence.sibling_package_digest is None:
            raise PortfolioRerunError("Lilies evidence requires sibling package identity")
        return LILIES_ACTOR_PROFILE
    if evidence.builder_actor == CODEX_FALLBACK_BUILDER_ACTOR:
        if evidence.formal_builder_actor != CODEX_FORMAL_BUILDER_ACTOR:
            raise PortfolioRerunError("Codex fallback did not preserve formal_builder_actor=codex")
        if evidence.fallback_eligibility is None:
            raise PortfolioRerunError("Codex fallback eligibility evidence is missing")
        validate_codex_fallback_eligibility(evidence.fallback_eligibility)
        if evidence.daemon_access is not None:
            raise PortfolioRerunError("Codex fallback must not fabricate daemon access")
        if evidence.sibling_commit is not None or evidence.sibling_package_digest is not None:
            raise PortfolioRerunError("Codex fallback must not claim a Lilies sibling identity")
        return CODEX_FALLBACK_ACTOR_PROFILE
    if evidence.builder_actor == CODEX_FORMAL_BUILDER_ACTOR:
        raise PortfolioRerunError("historical Codex actor cannot be remapped into r8 fallback")
    raise PortfolioRerunError("execution evidence names an unsupported Builder actor")


def validate_completed_actor_phases(
    profile: BuilderActorProfile,
    spans: Sequence[PhaseSpan],
) -> None:
    """Enforce exact eight-phase accounting for the selected actor profile."""

    _require_supported_actor_profile(profile)
    if tuple(span.phase for span in spans) != PHASES:
        raise PortfolioRerunError("completed timing evidence does not contain all eight phases")
    for span in spans:
        if span.phase in profile.inapplicable_phases:
            if (
                span.duration_seconds != 0.0
                or span.duration_percentage != 0.0
                or span.started_at != span.finished_at
                or span.outcome != "not_applicable"
            ):
                raise PortfolioRerunError(
                    f"{span.phase} must be zero-duration not_applicable for Codex fallback"
                )
        elif span.duration_seconds <= 0 or span.outcome == "not_applicable":
            raise PortfolioRerunError(
                f"{span.phase} has invalid completed timing for the selected actor"
            )
    if not math.isclose(
        math.fsum(span.duration_percentage for span in spans),
        100.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise PortfolioRerunError("phase percentages do not sum to 100 percent")


def validate_actor_observable_events(
    profile: BuilderActorProfile,
    events: Sequence[ObservableEvent],
) -> None:
    _require_supported_actor_profile(profile)
    if not profile.requires_daemon_access and any(
        event.name in {"daemon_discovered", "pairing_completed"}
        or event.phase in ACTOR_INAPPLICABLE_PHASES
        for event in events
    ):
        raise PortfolioRerunError("Codex fallback must not fabricate daemon or pairing events")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PortfolioRerunError("receipt payload is not canonical JSON") from error


def _receipt_signing_bytes(envelope: SignedReceiptEnvelope) -> bytes:
    return _canonical_json_bytes(
        {
            "receipt_id": envelope.receipt_id,
            "issuer": envelope.issuer,
            "key_id": envelope.key_id,
            "issued_at": envelope.issued_at,
            "semantic_type": envelope.semantic_type,
            "semantic_payload": envelope.semantic_payload,
            "payload_digest": envelope.payload_digest,
        }
    )


def _execution_evidence_semantic_payload(
    project_id: str,
    evidence: SafeExecutionEvidence,
) -> dict[str, Any]:
    payload = asdict(evidence)
    for field_name in (
        "trusted_verifier_id",
        "trusted_verifier_digest",
        "receipt_chain_digest",
    ):
        payload.pop(field_name)
    return {
        "project_id": project_id,
        "attempt_id": evidence.attempt_id,
        "evidence": payload,
    }


def _safe_execution_from_semantic_payload(
    manifest: ProjectManifest,
    payload: Mapping[str, Any],
    envelopes: tuple[SignedReceiptEnvelope, ...],
    verifier: ReceiptTrustVerifier,
) -> SafeExecutionEvidence:
    if payload.get("project_id") != manifest.project_id:
        raise PortfolioRerunError("execution evidence receipt belongs to another project")
    raw = payload.get("evidence")
    if not isinstance(raw, Mapping):
        raise PortfolioRerunError("execution evidence receipt has no structured payload")
    try:
        public_materials = tuple(NamedDigest(**item) for item in raw["public_material_digests"])
        acceptance = tuple(AcceptanceCaseReceipt(**item) for item in raw["acceptance_receipts"])
        ml_raw = raw["ml_lifecycle"]
        ml_lifecycle = None if ml_raw is None else MLLifecycleEvidence(**ml_raw)
        fallback_raw = raw["fallback_eligibility"]
        fallback_eligibility = (
            None
            if fallback_raw is None
            else CodexFallbackEligibility(**fallback_raw)
        )
        daemon_raw = raw["daemon_access"]
        return SafeExecutionEvidence(
            attempt_id=raw["attempt_id"],
            builder_actor=raw["builder_actor"],
            formal_builder_actor=raw["formal_builder_actor"],
            fallback_eligibility=fallback_eligibility,
            trusted_verifier_id=verifier.verifier_id,
            trusted_verifier_digest=verifier.verifier_digest,
            receipt_chain_digest=_receipt_chain_digest(envelopes),
            sibling_commit=raw["sibling_commit"],
            sibling_package_digest=raw["sibling_package_digest"],
            application_id=raw["application_id"],
            assignment_receipt_id=raw["assignment_receipt_id"],
            published_version=raw["published_version"],
            published_content_hash=raw["published_content_hash"],
            assignment_id=raw["assignment_id"],
            session_id=raw["session_id"],
            environment_generation=raw["environment_generation"],
            environment_receipt_id=raw["environment_receipt_id"],
            archive_id=raw["archive_id"],
            archive_receipt_id=raw["archive_receipt_id"],
            archive_digest=raw["archive_digest"],
            public_material_digests=public_materials,
            public_interface_digest=raw["public_interface_digest"],
            fresh_empty_draft=FreshDraftReceipt(**raw["fresh_empty_draft"]),
            task_access=TaskAccessReceipt(**raw["task_access"]),
            daemon_access=(
                None if daemon_raw is None else DaemonAccessReceipt(**daemon_raw)
            ),
            publication=PublicationReceipt(**raw["publication"]),
            mutation_guard=MutationGuardReceipt(**raw["mutation_guard"]),
            acceptance_receipts=acceptance,
            ml_lifecycle=ml_lifecycle,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PortfolioRerunError("execution evidence receipt schema is invalid") from error


class ReceiptTrustVerifier:
    """Verify receipts against a runtime task-author RSA public identity."""

    def __init__(self, trust_root: ReceiptTrustRoot) -> None:
        _require_public_identifier(trust_root.issuer, field_name="trust-root issuer")
        _require_public_identifier(trust_root.key_id, field_name="trust-root key id")
        if (
            isinstance(trust_root.rsa_modulus, bool)
            or trust_root.rsa_modulus.bit_length() < 2_048
            or isinstance(trust_root.rsa_exponent, bool)
            or trust_root.rsa_exponent < 3
            or trust_root.rsa_exponent % 2 == 0
        ):
            raise PortfolioRerunError("task-author RSA trust root is invalid")
        self.trust_root = trust_root

    @property
    def verifier_id(self) -> str:
        return self.trust_root.issuer

    @property
    def verifier_digest(self) -> str:
        return self.trust_root.verifier_digest

    def _verify_signature(self, envelope: SignedReceiptEnvelope) -> bool:
        try:
            signature = base64.urlsafe_b64decode(envelope.signature + "==")
        except (ValueError, TypeError):
            return False
        modulus = self.trust_root.rsa_modulus
        modulus_bytes = (modulus.bit_length() + 7) // 8
        if len(signature) != modulus_bytes:
            return False
        signature_value = int.from_bytes(signature, "big")
        if signature_value >= modulus:
            return False
        encoded = pow(
            signature_value,
            self.trust_root.rsa_exponent,
            modulus,
        ).to_bytes(modulus_bytes, "big")
        digest_info = RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(
            _receipt_signing_bytes(envelope)
        ).digest()
        padding_size = modulus_bytes - len(digest_info) - 3
        if padding_size < 8:
            return False
        expected = b"\x00\x01" + b"\xff" * padding_size + b"\x00" + digest_info
        return hmac.compare_digest(encoded, expected)

    def verify_envelope(self, envelope: SignedReceiptEnvelope) -> bool:
        if (
            envelope.issuer != self.trust_root.issuer
            or envelope.key_id != self.trust_root.key_id
            or not envelope.semantic_type
            or SAFE_GENERATION_PATTERN.fullmatch(envelope.semantic_type) is None
        ):
            return False
        if envelope.payload_digest != _canonical_digest(envelope.semantic_payload):
            return False
        return self._verify_signature(envelope)

    def verify_adapter_capability(
        self,
        manifest: ProjectManifest,
        receipt: AdapterCapabilityReceipt,
    ) -> bool:
        expected_payload = {
            "project_id": receipt.project_id,
            "adapter_id": receipt.adapter_id,
            "adapter_digest": receipt.adapter_digest,
            "capabilities": list(receipt.capabilities),
        }
        return (
            receipt.envelope.semantic_type == "adapter_capability"
            and receipt.envelope.semantic_payload == expected_payload
            and receipt.capability_digest == _canonical_digest(expected_payload)
            and self.verify_envelope(receipt.envelope)
        )

    def verify_execution_receipt(
        self,
        manifest: ProjectManifest,
        envelope: SignedReceiptEnvelope,
    ) -> bool:
        return (
            envelope.semantic_payload.get("project_id") == manifest.project_id
            and self.verify_envelope(envelope)
        )

    def derive_execution_evidence(
        self,
        manifest: ProjectManifest,
        envelopes: tuple[SignedReceiptEnvelope, ...],
    ) -> SafeExecutionEvidence:
        roots = [
            envelope
            for envelope in envelopes
            if envelope.semantic_type == "execution_evidence"
        ]
        if len(roots) != 1 or not self.verify_execution_receipt(manifest, roots[0]):
            raise PortfolioRerunError("trusted execution evidence root is absent or ambiguous")
        return _safe_execution_from_semantic_payload(
            manifest,
            roots[0].semantic_payload,
            envelopes,
            self,
        )


def validate_signed_receipt_envelope(
    envelope: SignedReceiptEnvelope,
    verifier: ReceiptTrustVerifier,
) -> datetime:
    _require_uuid(envelope.receipt_id, field_name="signed receipt id")
    if envelope.issuer != verifier.trust_root.issuer:
        raise PortfolioRerunError("signed receipt issuer does not match the trust root")
    if envelope.key_id != verifier.trust_root.key_id:
        raise PortfolioRerunError("signed receipt key does not match the trust root")
    _require_public_identifier(envelope.semantic_type, field_name="receipt semantic type")
    _require_sha256(envelope.payload_digest, field_name="signed receipt payload digest")
    if envelope.payload_digest != _canonical_digest(envelope.semantic_payload):
        raise PortfolioRerunError("signed receipt payload digest is not canonical")
    if (
        len(envelope.signature) < 16
        or len(envelope.signature) > 2_048
        or re.fullmatch(r"[A-Za-z0-9_-]+", envelope.signature) is None
    ):
        raise PortfolioRerunError("signed receipt signature encoding is invalid")
    if not verifier.verify_envelope(envelope):
        raise PortfolioRerunError("signed receipt failed the task-author trust root")
    return _parse_observable_timestamp(envelope.issued_at)


def required_adapter_capabilities(
    manifest: ProjectManifest,
    actor_profile: BuilderActorProfile = LILIES_ACTOR_PROFILE,
) -> tuple[str, ...]:
    _require_supported_actor_profile(actor_profile)
    try:
        testkit_digest = f"sha256:{hashlib.sha256(REAL_PROJECT_TESTKIT_PATH.read_bytes()).hexdigest()}"
    except OSError as error:
        raise PortfolioRerunError("real-project public API testkit is unavailable") from error
    hook_capabilities: list[str] = []
    for hook_name, hook in manifest.hooks.items():
        if hook.route == "commands":
            hook_capabilities.append(
                f"{_hook_capability(manifest.project_id, hook_name)}:commands:"
                f"{_canonical_digest([asdict(command) for command in hook.commands])}"
            )
        elif hook.route == "broker_capability":
            assert hook.capability_id is not None
            hook_capabilities.append(
                f"{_hook_capability(manifest.project_id, hook_name)}:broker:"
                f"{hook.capability_id}"
            )
    actor_capabilities = (
        LILIES_DAEMON_ADAPTER_CAPABILITIES
        if actor_profile.requires_daemon_access
        else CODEX_FALLBACK_ADAPTER_CAPABILITIES
    )
    return (
        *CORE_ADAPTER_CAPABILITIES[:2],
        *actor_capabilities,
        *CORE_ADAPTER_CAPABILITIES[2:],
        f"real_project_testkit_digest:{testkit_digest}",
        *hook_capabilities,
    )


def validate_adapter_capability_receipt(
    manifest: ProjectManifest,
    receipt: AdapterCapabilityReceipt,
    verifier: ReceiptTrustVerifier,
    actor_profile: BuilderActorProfile = LILIES_ACTOR_PROFILE,
) -> None:
    _require_supported_actor_profile(actor_profile)
    if receipt.project_id != manifest.project_id:
        raise PortfolioRerunError("adapter capability receipt belongs to another project")
    _require_public_identifier(receipt.adapter_id, field_name="adapter identity")
    _require_sha256(receipt.adapter_digest, field_name="adapter digest")
    _require_sha256(receipt.capability_digest, field_name="adapter capability digest")
    expected_payload = {
        "project_id": receipt.project_id,
        "adapter_id": receipt.adapter_id,
        "adapter_digest": receipt.adapter_digest,
        "capabilities": list(receipt.capabilities),
    }
    expected_digest = _canonical_digest(expected_payload)
    if receipt.capability_digest != expected_digest:
        raise PortfolioRerunError("adapter capability digest is not canonical")
    if receipt.envelope.payload_digest != receipt.capability_digest:
        raise PortfolioRerunError("adapter envelope does not bind the capability digest")
    validate_signed_receipt_envelope(receipt.envelope, verifier)
    if len(set(receipt.capabilities)) != len(receipt.capabilities):
        raise PortfolioRerunError("adapter capability receipt contains duplicates")
    required = required_adapter_capabilities(manifest, actor_profile)
    if receipt.capabilities != required:
        raise PortfolioRerunError(
            "adapter capability receipt does not exactly match the required capabilities"
        )
    _require_public_identifier(verifier.verifier_id, field_name="receipt verifier identity")
    _require_sha256(verifier.verifier_digest, field_name="receipt verifier digest")
    if not verifier.verify_adapter_capability(manifest, receipt):
        raise PortfolioRerunError("trusted verifier rejected the adapter capability receipt")


def _receipt_chain_digest(envelopes: Sequence[SignedReceiptEnvelope]) -> str:
    payload = json.dumps(
        [asdict(envelope) for envelope in envelopes],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validate_ml_lifecycle(
    evidence: SafeExecutionEvidence,
    lifecycle: MLLifecycleEvidence,
    envelopes: Sequence[SignedReceiptEnvelope],
) -> None:
    if (
        lifecycle.application_id != evidence.application_id
        or lifecycle.published_version != evidence.published_version
        or lifecycle.workflow_content_hash != evidence.published_content_hash
        or lifecycle.archive_id != evidence.archive_id
    ):
        raise PortfolioRerunError("ML lifecycle is not bound to the accepted workflow archive")
    receipt_digests = (
        ("chronological split", lifecycle.chronological_split_receipt_digest),
        ("fit/train", lifecycle.fit_train_receipt_digest),
        ("evaluation", lifecycle.evaluation_receipt_digest),
        ("backtest", lifecycle.backtest_receipt_digest),
        ("promotion", lifecycle.promotion_receipt_digest),
        ("deployment", lifecycle.deployment_receipt_digest),
        ("inference", lifecycle.inference_receipt_digest),
        ("retraining trigger", lifecycle.retraining_trigger_receipt_digest),
    )
    for name, digest in (*receipt_digests, ("model content", lifecycle.model_content_digest)):
        _require_sha256(digest, field_name=f"ML {name} receipt")
    if len({digest for _, digest in receipt_digests}) != len(receipt_digests):
        raise PortfolioRerunError("ML lifecycle reuses a receipt across distinct stages")
    _require_public_identifier(
        lifecycle.immutable_model_version,
        field_name="ML model version",
    )
    _require_uuid(lifecycle.inference_run_id, field_name="ML inference run id")
    if lifecycle.inference_run_id not in {
        receipt.run_id for receipt in evidence.acceptance_receipts
    }:
        raise PortfolioRerunError("ML inference is not bound to an accepted run")
    ordered_times = tuple(
        _parse_observable_timestamp(value)
        for value in (
            lifecycle.training_window_end,
            lifecycle.evaluation_window_start,
            lifecycle.trained_at,
            lifecycle.evaluated_at,
            lifecycle.promoted_at,
            lifecycle.deployed_at,
            lifecycle.inferred_at,
            lifecycle.retraining_evaluated_at,
        )
    )
    if ordered_times[1] <= ordered_times[0]:
        raise PortfolioRerunError("ML evaluation window is not after the training window")
    if any(
        later < earlier
        for earlier, later in zip(ordered_times[:-1], ordered_times[1:], strict=True)
    ):
        raise PortfolioRerunError("ML lifecycle receipts are not chronological")

    common = {
        "project_id": "EXP-LILIES-006",
        "attempt_id": evidence.attempt_id,
        "application_id": lifecycle.application_id,
        "published_version": lifecycle.published_version,
        "workflow_content_hash": lifecycle.workflow_content_hash,
        "archive_id": lifecycle.archive_id,
        "immutable_model_version": lifecycle.immutable_model_version,
        "model_content_digest": lifecycle.model_content_digest,
    }
    expected_stage_payloads = {
        "chronological_split": {
            **common,
            "stage": "chronological_split",
            "occurred_at": lifecycle.evaluation_window_start,
            "training_window_end": lifecycle.training_window_end,
            "evaluation_window_start": lifecycle.evaluation_window_start,
        },
        "fit_train": {
            **common,
            "stage": "fit_train",
            "occurred_at": lifecycle.trained_at,
        },
        "evaluation": {
            **common,
            "stage": "evaluation",
            "occurred_at": lifecycle.evaluated_at,
        },
        "backtest": {
            **common,
            "stage": "backtest",
            "occurred_at": lifecycle.evaluated_at,
        },
        "promotion": {
            **common,
            "stage": "promotion",
            "occurred_at": lifecycle.promoted_at,
        },
        "deployment": {
            **common,
            "stage": "deployment",
            "occurred_at": lifecycle.deployed_at,
        },
        "inference": {
            **common,
            "stage": "inference",
            "occurred_at": lifecycle.inferred_at,
            "run_id": lifecycle.inference_run_id,
        },
        "retraining_trigger": {
            **common,
            "stage": "retraining_trigger",
            "occurred_at": lifecycle.retraining_evaluated_at,
        },
    }
    lifecycle_digest_by_stage = {
        "chronological_split": lifecycle.chronological_split_receipt_digest,
        "fit_train": lifecycle.fit_train_receipt_digest,
        "evaluation": lifecycle.evaluation_receipt_digest,
        "backtest": lifecycle.backtest_receipt_digest,
        "promotion": lifecycle.promotion_receipt_digest,
        "deployment": lifecycle.deployment_receipt_digest,
        "inference": lifecycle.inference_receipt_digest,
        "retraining_trigger": lifecycle.retraining_trigger_receipt_digest,
    }
    ml_envelopes = [
        envelope for envelope in envelopes if envelope.semantic_type == "ml_stage"
    ]
    for stage, expected_payload in expected_stage_payloads.items():
        digest = lifecycle_digest_by_stage[stage]
        matches = [
            envelope
            for envelope in ml_envelopes
            if envelope.payload_digest == digest
            and envelope.semantic_payload == expected_payload
        ]
        if len(matches) != 1:
            raise PortfolioRerunError(
                f"ML {stage} is not bound to one signed semantic stage receipt"
            )


def validate_execution_evidence(
    manifest: ProjectManifest,
    evidence: SafeExecutionEvidence,
    token_evidence: TokenCheckpoint | CodexTokenUsageEvidence,
    envelopes: Sequence[SignedReceiptEnvelope],
    verifier: ReceiptTrustVerifier,
    attempt_ledger: AttemptLedger | None = None,
) -> None:
    """Validate evidence derived only through a trusted signed-receipt verifier."""

    actor_profile = actor_profile_for_evidence(evidence)
    if actor_profile.requires_daemon_access:
        if not isinstance(token_evidence, TokenCheckpoint):
            raise PortfolioRerunError("Lilies execution requires the one-million-token ledger")
        validate_token_checkpoint(token_evidence)
    else:
        if not isinstance(token_evidence, CodexTokenUsageEvidence):
            raise PortfolioRerunError("Codex fallback requires actor-specific usage evidence")
        validate_codex_token_usage(token_evidence)
    if (
        evidence.trusted_verifier_id != verifier.verifier_id
        or evidence.trusted_verifier_digest != verifier.verifier_digest
    ):
        raise PortfolioRerunError("execution evidence is not bound to the trusted verifier")
    _require_sha256(evidence.trusted_verifier_digest, field_name="trusted verifier digest")
    _require_uuid(evidence.attempt_id, field_name="execution attempt id")
    receipt_context = {
        "project_id": manifest.project_id,
        "attempt_id": evidence.attempt_id,
    }
    envelope_times: list[datetime] = []
    envelope_ids: list[str] = []
    for envelope in envelopes:
        envelope_times.append(validate_signed_receipt_envelope(envelope, verifier))
        envelope_ids.append(envelope.receipt_id)
        if not verifier.verify_execution_receipt(manifest, envelope):
            raise PortfolioRerunError("trusted verifier rejected a signed execution receipt")
    if not envelopes or len(set(envelope_ids)) != len(envelope_ids):
        raise PortfolioRerunError("signed execution receipt ids are absent or duplicated")
    if any(
        later < earlier
        for earlier, later in zip(envelope_times[:-1], envelope_times[1:], strict=True)
    ):
        raise PortfolioRerunError("signed execution receipts are not chronological")
    if evidence.receipt_chain_digest != _receipt_chain_digest(envelopes):
        raise PortfolioRerunError("execution evidence does not bind the signed receipt chain")

    envelope_by_id = {envelope.receipt_id: envelope for envelope in envelopes}

    def require_receipt(
        receipt_id: str,
        semantic_type: str,
        semantic_payload: Mapping[str, Any],
    ) -> None:
        envelope = envelope_by_id.get(receipt_id)
        if envelope is None:
            raise PortfolioRerunError(f"trusted {semantic_type} receipt is missing")
        if (
            envelope.semantic_type != semantic_type
            or envelope.semantic_payload != semantic_payload
        ):
            raise PortfolioRerunError(
                f"trusted {semantic_type} receipt does not bind its semantic payload"
            )

    root_receipts = [
        envelope for envelope in envelopes if envelope.semantic_type == "execution_evidence"
    ]
    if len(root_receipts) != 1 or root_receipts[0].semantic_payload != (
        _execution_evidence_semantic_payload(manifest.project_id, evidence)
    ):
        raise PortfolioRerunError("execution evidence is not bound to one signed semantic root")
    token_semantic_type = (
        "token_checkpoint"
        if actor_profile.requires_daemon_access
        else "codex_token_usage"
    )
    token_payload_key = (
        "checkpoint" if actor_profile.requires_daemon_access else "usage"
    )
    token_receipts = [
        envelope
        for envelope in envelopes
        if envelope.semantic_type == token_semantic_type
        and envelope.semantic_payload
        == {
            **receipt_context,
            token_payload_key: asdict(token_evidence),
            "final": True,
        }
    ]
    if len(token_receipts) != 1:
        raise PortfolioRerunError("final actor token evidence has no unique trusted receipt")

    if actor_profile.requires_daemon_access:
        assert evidence.sibling_commit is not None
        assert evidence.sibling_package_digest is not None
        if GIT_COMMIT_PATTERN.fullmatch(evidence.sibling_commit) is None:
            raise PortfolioRerunError("sibling commit is not a fixed Git commit")
        _require_sha256(
            evidence.sibling_package_digest,
            field_name="sibling package digest",
        )
    for field_name, value in (
        ("application id", evidence.application_id),
        ("assignment receipt id", evidence.assignment_receipt_id),
        ("assignment id", evidence.assignment_id),
        ("session id", evidence.session_id),
        ("environment receipt id", evidence.environment_receipt_id),
        ("archive id", evidence.archive_id),
        ("archive receipt id", evidence.archive_receipt_id),
    ):
        _require_uuid(value, field_name=field_name)
    if evidence.session_id != token_evidence.session_id:
        raise PortfolioRerunError("evidence session does not match token accounting")
    if (
        not isinstance(evidence.published_version, int)
        or isinstance(evidence.published_version, bool)
        or evidence.published_version < 1
    ):
        raise PortfolioRerunError("published version is invalid")
    _require_sha256(evidence.published_content_hash, field_name="published content hash")
    _require_sha256(evidence.archive_digest, field_name="archive digest")
    _require_public_identifier(
        evidence.environment_generation,
        field_name="environment generation",
    )
    _require_sha256(evidence.public_interface_digest, field_name="public interface digest")
    if evidence.public_material_digests != public_material_digest_receipts(manifest):
        raise PortfolioRerunError("public material digest receipts do not match the Builder input")
    if (
        evidence.fallback_eligibility is not None
        and evidence.fallback_eligibility.public_material_allowlist_digest
        != _canonical_digest([asdict(item) for item in evidence.public_material_digests])
    ):
        raise PortfolioRerunError("Codex fallback allowlist digest does not bind public inputs")
    if actor_profile == CODEX_FALLBACK_ACTOR_PROFILE:
        assert evidence.fallback_eligibility is not None
        eligibility = evidence.fallback_eligibility
        freshness_digest = _canonical_digest(_fallback_freshness_identities(evidence))
        if eligibility.freshness_identity_digest != freshness_digest:
            raise PortfolioRerunError(
                "Codex fallback freshness digest does not bind attempt identities"
            )
        scan_payload = _fallback_scan_payload(manifest.project_id, evidence)
        if eligibility.forbidden_assistance_scan_digest != _canonical_digest(scan_payload):
            raise PortfolioRerunError(
                "Codex fallback scan digest does not bind the public-only scan"
            )
        require_receipt(
            eligibility.forbidden_assistance_scan_receipt_id,
            "public_only_forbidden_assistance_scan",
            scan_payload,
        )
        prerequisite_payload = _fallback_prerequisite_payload(
            manifest.project_id,
            evidence,
        )
        if eligibility.prerequisite_payload_digest != _canonical_digest(
            prerequisite_payload
        ):
            raise PortfolioRerunError(
                "Codex fallback prerequisite digest does not bind signed provenance"
            )
        require_receipt(
            eligibility.prerequisite_receipt_id,
            "codex_fallback_prerequisite",
            prerequisite_payload,
        )
        if attempt_ledger is None:
            raise PortfolioRerunError(
                "Codex fallback requires independently persisted attempt-ledger provenance"
            )
        attempt_ledger.validate_fallback_prerequisite(
            evidence.attempt_id,
            evidence,
            envelopes,
            verifier,
        )

    fresh = evidence.fresh_empty_draft
    if (
        fresh.application_id != evidence.application_id
        or fresh.draft_revision != 0
        or fresh.node_count != 0
        or fresh.edge_count != 0
    ):
        raise PortfolioRerunError("fresh application receipt does not prove an empty draft")
    _require_uuid(fresh.receipt_id, field_name="fresh draft receipt id")
    _require_sha256(fresh.draft_content_hash, field_name="fresh draft content hash")
    fresh_at = _parse_observable_timestamp(fresh.observed_at)

    task_access = evidence.task_access
    _require_uuid(
        task_access.task_credential_receipt_id,
        field_name="task credential receipt id",
    )
    _require_sha256(
        task_access.task_credential_digest,
        field_name="task credential digest",
    )
    access = evidence.daemon_access
    if actor_profile.requires_daemon_access:
        assert access is not None
        for field_name, value in (
            ("discovery receipt id", access.discovery_receipt_id),
            ("pairing receipt id", access.pairing_receipt_id),
            ("connection id", access.connection_id),
        ):
            _require_uuid(value, field_name=field_name)
        _require_sha256(access.daemon_fingerprint, field_name="daemon fingerprint")
        if (
            access.task_credential_receipt_id
            != task_access.task_credential_receipt_id
            or access.task_credential_digest != task_access.task_credential_digest
        ):
            raise PortfolioRerunError("Lilies daemon and task credential evidence diverge")
        daemon_url = urlsplit(access.daemon_base_url)
        try:
            daemon_port = daemon_url.port
        except ValueError as error:
            raise PortfolioRerunError("daemon receipt has an invalid loopback port") from error
        if (
            daemon_url.scheme != "http"
            or daemon_url.hostname != "127.0.0.1"
            or daemon_port is None
            or not 1 <= daemon_port <= 65_535
            or daemon_url.username is not None
            or daemon_url.password is not None
            or daemon_url.path not in {"", "/"}
            or daemon_url.query
            or daemon_url.fragment
        ):
            raise PortfolioRerunError(
                "daemon receipt is not bound to an exact loopback endpoint"
            )
        if access.exact_discovery_match is not True or access.exact_pairing_match is not True:
            raise PortfolioRerunError(
                "discovery and pairing receipts do not prove exact binding"
            )
    elif any(
        envelope.semantic_type in {"daemon_discovery", "explicit_pairing"}
        for envelope in envelopes
    ):
        raise PortfolioRerunError("Codex fallback must not fabricate daemon receipts")

    require_receipt(
        fresh.receipt_id,
        "fresh_application",
        {**receipt_context, "receipt": asdict(fresh)},
    )
    require_receipt(
        evidence.environment_receipt_id,
        "environment_generation",
        {
            **receipt_context,
            "application_id": evidence.application_id,
            "environment_generation": evidence.environment_generation,
        },
    )
    if actor_profile.requires_daemon_access:
        assert access is not None
        require_receipt(
            access.discovery_receipt_id,
            "daemon_discovery",
            {
                **receipt_context,
                "connection_id": access.connection_id,
                "daemon_fingerprint": access.daemon_fingerprint,
                "daemon_base_url": access.daemon_base_url,
            },
        )
        require_receipt(
            access.pairing_receipt_id,
            "explicit_pairing",
            {
                **receipt_context,
                "connection_id": access.connection_id,
                "daemon_fingerprint": access.daemon_fingerprint,
            },
        )
    require_receipt(
        task_access.task_credential_receipt_id,
        "task_credential",
        {
            **receipt_context,
            "assignment_id": evidence.assignment_id,
            "task_credential_digest": task_access.task_credential_digest,
        },
    )
    require_receipt(
        evidence.assignment_receipt_id,
        "assignment",
        {
            **receipt_context,
            "application_id": evidence.application_id,
            "assignment_id": evidence.assignment_id,
            "session_id": evidence.session_id,
            "formal_builder_actor": evidence.formal_builder_actor,
            "builder_actor": evidence.builder_actor,
            "sibling_commit": evidence.sibling_commit,
            "sibling_package_digest": evidence.sibling_package_digest,
            "fallback_eligibility": (
                None
                if evidence.fallback_eligibility is None
                else asdict(evidence.fallback_eligibility)
            ),
        },
    )

    publication = evidence.publication
    if (
        publication.application_id != evidence.application_id
        or publication.published_version != evidence.published_version
        or publication.published_content_hash != evidence.published_content_hash
    ):
        raise PortfolioRerunError("publication receipt does not bind the accepted version")
    _require_uuid(publication.receipt_id, field_name="publication receipt id")
    published_at = _parse_observable_timestamp(publication.published_at)
    if published_at < fresh_at:
        raise PortfolioRerunError("publication predates the fresh application receipt")
    require_receipt(
        publication.receipt_id,
        "publication",
        {**receipt_context, "receipt": asdict(publication)},
    )

    guard = evidence.mutation_guard
    if (
        guard.published_version != evidence.published_version
        or guard.post_acceptance_version != evidence.published_version
        or guard.published_content_hash != evidence.published_content_hash
        or guard.post_acceptance_content_hash != evidence.published_content_hash
        or isinstance(guard.mutations_after_publish, bool)
        or guard.mutations_after_publish != 0
    ):
        raise PortfolioRerunError("mutation guard does not prove one immutable accepted version")

    expected_cases = ("debug", *manifest.seed_ids)
    if tuple(receipt.case_id for receipt in evidence.acceptance_receipts) != expected_cases:
        raise PortfolioRerunError("acceptance receipt identities do not match public plus three seeds")
    previous_finished = published_at
    started_times: list[datetime] = []
    finished_times: list[datetime] = []
    aggregate_digests: list[str] = []
    for receipt in evidence.acceptance_receipts:
        _require_uuid(receipt.run_id, field_name=f"run id {receipt.case_id}")
        _require_uuid(receipt.receipt_id, field_name=f"case receipt id {receipt.case_id}")
        _require_public_identifier(
            receipt.environment_generation,
            field_name="case environment generation",
        )
        if (
            receipt.published_version != evidence.published_version
            or receipt.published_content_hash != evidence.published_content_hash
        ):
            raise PortfolioRerunError("acceptance receipt used a different published version")
        if receipt.status != "passed":
            raise PortfolioRerunError("completed project contains a failed acceptance receipt")
        _require_sha256(
            receipt.aggregate_receipt_digest,
            field_name=f"aggregate receipt {receipt.case_id}",
        )
        started_at = _parse_observable_timestamp(receipt.started_at)
        finished_at = _parse_observable_timestamp(receipt.finished_at)
        if started_at < previous_finished or finished_at < started_at:
            raise PortfolioRerunError("acceptance case receipts are not chronological")
        if finished_at == started_at:
            raise PortfolioRerunError("acceptance case receipt has no observable duration")
        started_times.append(started_at)
        finished_times.append(finished_at)
        aggregate_digests.append(receipt.aggregate_receipt_digest)
        previous_finished = finished_at
        require_receipt(
            receipt.receipt_id,
            "acceptance_case",
            {**receipt_context, "receipt": asdict(receipt)},
        )
    if len(set(started_times)) != 4 or len(set(finished_times)) != 4:
        raise PortfolioRerunError("acceptance case timestamps are not independent")
    if len(set(aggregate_digests)) != 4:
        raise PortfolioRerunError("acceptance cases reuse an aggregate receipt")

    daemon_unique_ids: tuple[str, ...] = ()
    daemon_required_receipt_ids: set[str] = set()
    if access is not None:
        daemon_unique_ids = (
            access.discovery_receipt_id,
            access.pairing_receipt_id,
            access.connection_id,
        )
        daemon_required_receipt_ids = {
            access.discovery_receipt_id,
            access.pairing_receipt_id,
        }
    fallback_unique_ids: tuple[str, ...] = ()
    fallback_required_receipt_ids: set[str] = set()
    if evidence.fallback_eligibility is not None:
        fallback_unique_ids = (
            evidence.fallback_eligibility.prerequisite_receipt_id,
            evidence.fallback_eligibility.forbidden_assistance_scan_receipt_id,
        )
        fallback_required_receipt_ids = set(fallback_unique_ids)
    unique_ids = (
        evidence.application_id,
        evidence.assignment_receipt_id,
        evidence.assignment_id,
        evidence.session_id,
        evidence.archive_id,
        evidence.archive_receipt_id,
        evidence.environment_receipt_id,
        fresh.receipt_id,
        task_access.task_credential_receipt_id,
        *daemon_unique_ids,
        *fallback_unique_ids,
        publication.receipt_id,
        *(receipt.run_id for receipt in evidence.acceptance_receipts),
        *(receipt.receipt_id for receipt in evidence.acceptance_receipts),
    )
    if len(set(unique_ids)) != len(unique_ids):
        raise PortfolioRerunError("application, assignment, session, run, or receipt ids collide")
    generations = (
        evidence.environment_generation,
        *(receipt.environment_generation for receipt in evidence.acceptance_receipts),
    )
    if len(set(generations)) != len(generations):
        raise PortfolioRerunError("environment generations are not independent")
    required_receipt_ids = {
        fresh.receipt_id,
        evidence.environment_receipt_id,
        task_access.task_credential_receipt_id,
        evidence.assignment_receipt_id,
        publication.receipt_id,
        evidence.archive_receipt_id,
        *(receipt.receipt_id for receipt in evidence.acceptance_receipts),
        *daemon_required_receipt_ids,
        *fallback_required_receipt_ids,
    }
    if not required_receipt_ids.issubset(set(envelope_ids)):
        raise PortfolioRerunError("signed receipt chain is missing a required identity receipt")
    require_receipt(
        evidence.archive_receipt_id,
        "archive",
        {
            **receipt_context,
            "application_id": evidence.application_id,
            "archive_id": evidence.archive_id,
            "archive_digest": evidence.archive_digest,
            "published_version": evidence.published_version,
            "published_content_hash": evidence.published_content_hash,
        },
    )
    cleanup_receipts = [
        envelope
        for envelope in envelopes
        if envelope.semantic_type == "cleanup"
        and envelope.semantic_payload
        == {
            **receipt_context,
            "application_id": evidence.application_id,
            "outcome": "completed",
        }
    ]
    if len(cleanup_receipts) != 1:
        raise PortfolioRerunError(
            "cleanup is not bound to one signed attempt/application receipt"
        )

    if manifest.project_id == "EXP-LILIES-006":
        if evidence.ml_lifecycle is None:
            raise PortfolioRerunError("project 6 has no complete ML lifecycle evidence")
        _validate_ml_lifecycle(evidence, evidence.ml_lifecycle, envelopes)
    elif evidence.ml_lifecycle is not None:
        raise PortfolioRerunError("non-ML project supplied an unrelated ML lifecycle receipt")


def validate_token_checkpoint(checkpoint: TokenCheckpoint) -> None:
    _require_uuid(checkpoint.session_id, field_name="token checkpoint session id")
    values = (
        checkpoint.attempted_calls,
        checkpoint.recorded_calls,
        checkpoint.input_tokens,
        checkpoint.output_tokens,
        checkpoint.total_tokens,
        checkpoint.runtime_cap_tokens,
        checkpoint.post_hard_stop_attempts,
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        raise PortfolioRerunError("token checkpoint contains an invalid counter")
    if checkpoint.total_tokens != checkpoint.input_tokens + checkpoint.output_tokens:
        raise PortfolioRerunError("token checkpoint total does not match its components")
    if checkpoint.runtime_cap_tokens != MAX_SESSION_TOKENS:
        raise PortfolioRerunError("runtime token cap echo does not equal one million")
    if checkpoint.attempted_calls != checkpoint.recorded_calls:
        raise PortfolioRerunError("every model-call attempt must have one settled receipt")
    if len(checkpoint.model_call_attempt_receipt_ids) != checkpoint.attempted_calls:
        raise PortfolioRerunError("model-call attempts do not match attempted call count")
    if len(checkpoint.model_call_receipt_ids) != checkpoint.recorded_calls:
        raise PortfolioRerunError("model-call receipts do not match recorded call count")
    if checkpoint.model_call_attempt_receipt_ids != checkpoint.model_call_receipt_ids:
        raise PortfolioRerunError("attempted and settled model-call histories diverge")
    if checkpoint.total_tokens > 0 and checkpoint.recorded_calls == 0:
        raise PortfolioRerunError("positive usage has no recorded model call")
    if len(set(checkpoint.model_call_receipt_ids)) != len(checkpoint.model_call_receipt_ids):
        raise PortfolioRerunError("model-call receipt ids are not unique")
    if any(
        SAFE_GENERATION_PATTERN.fullmatch(receipt_id) is None
        for receipt_id in checkpoint.model_call_receipt_ids
    ):
        raise PortfolioRerunError("model-call receipt id is not a safe identifier")
    if (
        not isinstance(checkpoint.unknown_calls, int)
        or isinstance(checkpoint.unknown_calls, bool)
        or checkpoint.unknown_calls < 0
    ):
        raise PortfolioRerunError("token checkpoint contains an invalid unknown-call counter")
    if checkpoint.unknown_calls != 0:
        raise PortfolioRerunError("token accounting is incomplete; stopping fail-closed")
    if checkpoint.post_hard_stop_attempts != 0:
        raise PortfolioRerunError("a model request was attempted after the token hard stop")
    if checkpoint.total_tokens > MAX_SESSION_TOKENS:
        raise PortfolioRerunError(
            f"Builder session exceeded the {MAX_SESSION_TOKENS} token hard limit"
        )
    if checkpoint.total_tokens == MAX_SESSION_TOKENS:
        if not checkpoint.hard_stop_triggered:
            raise PortfolioRerunError("runtime did not echo the token hard stop")
        if (
            not checkpoint.model_call_receipt_ids
            or checkpoint.cap_reached_at_receipt_id
            != checkpoint.model_call_receipt_ids[-1]
            or checkpoint.hard_stop_fence_receipt_id
            != checkpoint.model_call_receipt_ids[-1]
        ):
            raise PortfolioRerunError("token threshold is not bound to the final model call")
    elif (
        checkpoint.hard_stop_triggered
        or checkpoint.cap_reached_at_receipt_id is not None
        or checkpoint.hard_stop_fence_receipt_id is not None
    ):
        raise PortfolioRerunError("token hard stop was reported below the configured threshold")
    grouped_total = 0
    grouped_calls = 0
    group_keys: set[tuple[str, str]] = set()
    for group in checkpoint.groups:
        counters = (
            group.recorded_calls,
            group.input_tokens,
            group.output_tokens,
            group.total_tokens,
            group.unknown_calls,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counters
        ):
            raise PortfolioRerunError("grouped token usage contains an invalid counter")
        if (
            SAFE_GENERATION_PATTERN.fullmatch(group.stage) is None
            or SAFE_GENERATION_PATTERN.fullmatch(group.model) is None
        ):
            raise PortfolioRerunError("grouped token usage is missing stage or model")
        group_key = (group.stage, group.model)
        if group_key in group_keys:
            raise PortfolioRerunError("grouped token usage contains a duplicate stage/model")
        group_keys.add(group_key)
        if group.total_tokens != group.input_tokens + group.output_tokens:
            raise PortfolioRerunError("grouped token total does not match its components")
        if group.unknown_calls != 0:
            raise PortfolioRerunError("grouped token accounting is incomplete; stopping fail-closed")
        grouped_total += group.total_tokens
        grouped_calls += group.recorded_calls
    if checkpoint.total_tokens > 0 and not checkpoint.groups:
        raise PortfolioRerunError("positive token usage has no stage/model breakdown")
    if checkpoint.groups and grouped_total != checkpoint.total_tokens:
        raise PortfolioRerunError("grouped token usage does not match the session total")
    if checkpoint.groups and grouped_calls != checkpoint.recorded_calls:
        raise PortfolioRerunError("grouped calls do not match the session call count")


def validate_bounded_lilies_failure_report(prior: Mapping[str, Any]) -> None:
    """Require the exact bounded Lilies run promised by the r8 fallback contract."""

    report = prior.get("report_body")
    if (
        not isinstance(report, Mapping)
        or prior.get("report_digest") != _canonical_digest(report)
        or report.get("schema_version")
        != "v0.4.13-portfolio-rerun-report-body-r8-1"
        or report.get("attempt_id") != prior.get("attempt_id")
        or report.get("project_id") != prior.get("project_id")
        or report.get("formal_builder_actor") != LILIES_BUILDER_ACTOR
        or report.get("builder_actor") != LILIES_BUILDER_ACTOR
        or report.get("status") != "failed"
        or report.get("timing_complete") is not True
        or report.get("max_session_tokens") != MAX_SESSION_TOKENS
        or report.get("token_usage_authoritativeness") != "exact"
        or report.get("final_codex_token_usage") is not None
        or not isinstance(report.get("failure"), str)
        or not report["failure"].strip()
    ):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite report is incomplete or not an exact failure"
        )
    raw_checkpoint = report.get("final_token_checkpoint")
    if not isinstance(raw_checkpoint, Mapping):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite has no exact token checkpoint"
        )
    raw_groups = raw_checkpoint.get("groups")
    if not isinstance(raw_groups, (list, tuple)):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite token groups are malformed"
        )
    try:
        checkpoint = TokenCheckpoint(
            **{
                **raw_checkpoint,
                "model_call_attempt_receipt_ids": tuple(
                    raw_checkpoint.get("model_call_attempt_receipt_ids", ())
                ),
                "model_call_receipt_ids": tuple(
                    raw_checkpoint.get("model_call_receipt_ids", ())
                ),
                "groups": tuple(TokenUsageGroup(**group) for group in raw_groups),
            }
        )
    except (TypeError, ValueError) as error:
        raise PortfolioRerunError(
            "bounded Lilies prerequisite token checkpoint is malformed"
        ) from error
    validate_token_checkpoint(checkpoint)
    if checkpoint.total_tokens <= 0 or (
        prior.get("identity_bindings", {}).get("session_id")
        != checkpoint.session_id
    ):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite does not bind exact positive session usage"
        )
    raw_phases = report.get("phases")
    if not isinstance(raw_phases, list):
        raise PortfolioRerunError("bounded Lilies prerequisite phases are missing")
    try:
        phases = tuple(PhaseSpan(**item) for item in raw_phases)
    except (TypeError, ValueError) as error:
        raise PortfolioRerunError(
            "bounded Lilies prerequisite phases are malformed"
        ) from error
    validate_completed_actor_phases(LILIES_ACTOR_PROFILE, phases)
    if any(
        left.finished_at != right.started_at
        for left, right in zip(phases[:-1], phases[1:], strict=True)
    ):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite phases are not a contiguous partition"
        )
    attempt_started = _parse_observable_timestamp(prior.get("started_at"))
    attempt_finished = _parse_observable_timestamp(prior.get("finished_at"))
    if (
        _parse_observable_timestamp(phases[0].started_at) < attempt_started
        or _parse_observable_timestamp(phases[-1].finished_at) > attempt_finished
    ):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite phases fall outside the durable attempt"
        )
    total_elapsed = math.fsum(phase.duration_seconds for phase in phases)
    reported_total = report.get("total_elapsed_seconds")
    if (
        not isinstance(reported_total, (int, float))
        or isinstance(reported_total, bool)
        or not math.isfinite(float(reported_total))
        or not math.isclose(
            total_elapsed,
            float(reported_total),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        raise PortfolioRerunError(
            "bounded Lilies prerequisite timing denominator is invalid"
        )
    for phase in phases:
        wall_duration = (
            _parse_observable_timestamp(phase.finished_at)
            - _parse_observable_timestamp(phase.started_at)
        ).total_seconds()
        expected_percentage = phase.duration_seconds * 100.0 / total_elapsed
        if not math.isclose(
            wall_duration,
            phase.duration_seconds,
            rel_tol=0.0,
            abs_tol=1e-9,
        ) or not math.isclose(
            phase.duration_percentage,
            expected_percentage,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise PortfolioRerunError(
                "bounded Lilies prerequisite phase timing is not mutually exclusive"
            )


def validate_token_progress(
    previous: TokenCheckpoint | None,
    current: TokenCheckpoint,
) -> None:
    validate_token_checkpoint(current)
    if previous is None:
        return
    if current.session_id != previous.session_id:
        raise PortfolioRerunError("Builder session changed during one project")
    previous_counters = (
        previous.attempted_calls,
        previous.recorded_calls,
        previous.input_tokens,
        previous.output_tokens,
        previous.total_tokens,
    )
    current_counters = (
        current.attempted_calls,
        current.recorded_calls,
        current.input_tokens,
        current.output_tokens,
        current.total_tokens,
    )
    if any(current_value < previous_value for previous_value, current_value in zip(
        previous_counters,
        current_counters,
        strict=True,
    )):
        raise PortfolioRerunError("token or model-call counters moved backwards")
    if current.model_call_receipt_ids[: previous.recorded_calls] != (
        previous.model_call_receipt_ids
    ):
        raise PortfolioRerunError("model-call receipt history is not append-only")
    if current.model_call_attempt_receipt_ids[: previous.attempted_calls] != (
        previous.model_call_attempt_receipt_ids
    ):
        raise PortfolioRerunError("model-call attempt history is not append-only")
    if previous.total_tokens == MAX_SESSION_TOKENS and current != previous:
        raise PortfolioRerunError("a model-call receipt appeared after the token hard stop")
    previous_groups = {(group.stage, group.model): group for group in previous.groups}
    current_groups = {(group.stage, group.model): group for group in current.groups}
    if not set(previous_groups).issubset(current_groups):
        raise PortfolioRerunError("a stage/model usage group disappeared")
    for group_key, previous_group in previous_groups.items():
        current_group = current_groups[group_key]
        previous_group_counters = (
            previous_group.recorded_calls,
            previous_group.input_tokens,
            previous_group.output_tokens,
            previous_group.total_tokens,
        )
        current_group_counters = (
            current_group.recorded_calls,
            current_group.input_tokens,
            current_group.output_tokens,
            current_group.total_tokens,
        )
        if any(
            current_value < previous_value
            for previous_value, current_value in zip(
                previous_group_counters,
                current_group_counters,
                strict=True,
            )
        ):
            raise PortfolioRerunError("a stage/model usage counter moved backwards")


def validate_codex_token_usage(usage: CodexTokenUsageEvidence) -> None:
    _require_uuid(usage.session_id, field_name="Codex usage session id")
    _require_public_identifier(
        usage.authoritative_source,
        field_name="Codex usage authoritative source",
    )
    counters = (
        usage.attempted_calls,
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
    )
    if usage.availability == "exact":
        if usage.reason is not None:
            raise PortfolioRerunError("exact Codex token usage must not carry a gap reason")
        if any(
            value is None
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for value in counters
        ):
            raise PortfolioRerunError("exact Codex token usage has invalid counters")
        assert usage.input_tokens is not None
        assert usage.output_tokens is not None
        assert usage.total_tokens is not None
        if usage.total_tokens != usage.input_tokens + usage.output_tokens:
            raise PortfolioRerunError("exact Codex token total does not match components")
        return
    if usage.availability not in {"unavailable", "unknown"}:
        raise PortfolioRerunError("Codex token availability is invalid")
    if any(value is not None for value in counters):
        raise PortfolioRerunError(
            "unavailable Codex token usage must use null counters, never zero estimates"
        )
    if (
        usage.reason is None
        or not usage.reason.strip()
        or len(usage.reason) > 500
        or UNSAFE_EVENT_SUMMARY_PATTERN.search(usage.reason)
    ):
        raise PortfolioRerunError("unavailable Codex token usage has no safe reason")


def validate_codex_token_progress(
    previous: CodexTokenUsageEvidence | None,
    current: CodexTokenUsageEvidence,
) -> None:
    validate_codex_token_usage(current)
    if previous is None:
        return
    validate_codex_token_usage(previous)
    if current.session_id != previous.session_id:
        raise PortfolioRerunError("Codex Builder session changed during one project")
    if previous.availability == "exact" and current.availability != "exact":
        raise PortfolioRerunError("authoritative Codex token usage became unavailable")
    if previous.availability == current.availability == "exact":
        previous_values = (
            previous.attempted_calls,
            previous.input_tokens,
            previous.output_tokens,
            previous.total_tokens,
        )
        current_values = (
            current.attempted_calls,
            current.input_tokens,
            current.output_tokens,
            current.total_tokens,
        )
        assert all(value is not None for value in previous_values + current_values)
        if any(
            current_value < previous_value
            for previous_value, current_value in zip(
                previous_values,
                current_values,
                strict=True,
            )
        ):
            raise PortfolioRerunError("Codex token counters moved backwards")


def _parse_observable_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise PortfolioRerunError("observable event timestamp is not ISO-8601")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise PortfolioRerunError("observable event timestamp is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PortfolioRerunError("observable event timestamp must be UTC")
    return parsed


def _validated_observable_event(
    event: ObservableEvent,
    *,
    phase: str,
    previous_at: datetime | None,
) -> tuple[ObservableEvent, datetime]:
    allowed_kinds = {
        "message",
        "tool_call",
        "tool_result",
        "error",
        "fix",
        "test",
        "run",
        "artifact",
        "token_usage",
    }
    if event.kind not in allowed_kinds:
        raise PortfolioRerunError("observable event kind is not allowlisted")
    bound_phase = EVENT_PHASE_BINDINGS.get(event.name)
    if bound_phase is None or bound_phase != phase:
        raise PortfolioRerunError("observable event name is not allowed in this phase")
    if EVENT_KIND_BINDINGS[event.name] != event.kind:
        raise PortfolioRerunError("observable event kind does not match its structured name")
    if event.visibility not in {"public", "aggregate_only"}:
        raise PortfolioRerunError("observable event visibility is invalid")
    expected_visibility = (
        "aggregate_only" if event.name in AGGREGATE_EVENT_NAMES else "public"
    )
    if event.visibility != expected_visibility:
        raise PortfolioRerunError("observable event visibility does not match its structured name")
    expected_projection = "platform_public" if event.visibility == "public" else "aggregate_receipt"
    if event.safe_projection != expected_projection:
        raise PortfolioRerunError("observable event safe projection does not match visibility")
    if not event.summary.strip() or len(event.summary) > 4_000:
        raise PortfolioRerunError("observable event summary is empty or too large")
    if UNSAFE_EVENT_SUMMARY_PATTERN.search(event.summary):
        raise PortfolioRerunError("observable event summary contains a non-public data shape")
    if event.phase and event.phase != phase:
        raise PortfolioRerunError("observable event is attributed to the wrong phase")
    if event.entity_id is None or SAFE_GENERATION_PATTERN.fullmatch(event.entity_id) is None:
        raise PortfolioRerunError("observable event has no safe structured entity id")
    if UNSAFE_EVENT_SUMMARY_PATTERN.search(event.entity_id):
        raise PortfolioRerunError("observable event entity id contains a non-public data shape")
    if event.receipt_digest is None:
        raise PortfolioRerunError("observable event has no structured receipt digest")
    _require_sha256(event.receipt_digest, field_name="observable event receipt digest")
    parsed_at = _parse_observable_timestamp(event.at)
    if previous_at is not None and parsed_at < previous_at:
        raise PortfolioRerunError("observable events are not ordered by timestamp")
    return replace(event, phase=phase), parsed_at


def validate_completed_observable_chain(
    project_id: str,
    events: Sequence[ObservableEvent],
    spans: Sequence[PhaseSpan],
    evidence: SafeExecutionEvidence,
    token_evidence: TokenCheckpoint | CodexTokenUsageEvidence,
    envelopes: Sequence[SignedReceiptEnvelope],
    receipt_phases: Sequence[str],
) -> None:
    """Require a minimum public receipt chain and bind every event to its phase span."""

    actor_profile = actor_profile_for_evidence(evidence)
    validate_completed_actor_phases(actor_profile, spans)
    validate_actor_observable_events(actor_profile, events)
    if len(envelopes) != len(receipt_phases):
        raise PortfolioRerunError("signed receipt phase attribution is incomplete")
    if any(phase in actor_profile.inapplicable_phases for phase in receipt_phases):
        raise PortfolioRerunError("actor-inapplicable phases must not contain receipts")
    phase_spans = {span.phase: span for span in spans}
    counts = Counter(event.name for event in events)
    for name, minimum in MINIMUM_COMPLETED_EVENT_COUNTS.items():
        if (
            not actor_profile.requires_daemon_access
            and name in {"daemon_discovered", "pairing_completed"}
        ):
            if counts[name] != 0:
                raise PortfolioRerunError("Codex fallback fabricated daemon events")
            continue
        if counts[name] < minimum:
            raise PortfolioRerunError(f"completed observable chain is missing {name}")

    for event in events:
        span = phase_spans.get(event.phase)
        if span is None:
            raise PortfolioRerunError("observable event references an unknown phase span")
        event_at = _parse_observable_timestamp(event.at)
        started_at = _parse_observable_timestamp(span.started_at)
        finished_at = _parse_observable_timestamp(span.finished_at)
        if event_at < started_at or event_at > finished_at:
            raise PortfolioRerunError("observable event timestamp falls outside its phase span")
        event_receipts = [
            (envelope, receipt_phase)
            for envelope, receipt_phase in zip(envelopes, receipt_phases, strict=True)
            if envelope.semantic_type == "observable_event"
            and envelope.semantic_payload
            == {
                "project_id": project_id,
                "attempt_id": evidence.attempt_id,
                "event": asdict(event),
            }
        ]
        if len(event_receipts) != 1 or event_receipts[0][1] != event.phase:
            raise PortfolioRerunError(
                "observable event is not bound to one signed semantic receipt"
            )

    for envelope, phase in zip(envelopes, receipt_phases, strict=True):
        span = phase_spans.get(phase)
        if span is None:
            raise PortfolioRerunError("signed receipt references an unknown phase span")
        issued_at = _parse_observable_timestamp(envelope.issued_at)
        if (
            issued_at < _parse_observable_timestamp(span.started_at)
            or issued_at > _parse_observable_timestamp(span.finished_at)
        ):
            raise PortfolioRerunError("signed receipt timestamp falls outside its phase span")

    envelope_by_id = {envelope.receipt_id: envelope for envelope in envelopes}
    assignment_envelope = envelope_by_id.get(evidence.assignment_receipt_id)
    if assignment_envelope is None:
        raise PortfolioRerunError("assignment event has no trusted signed receipt")

    def require_binding(name: str, entity_id: str, receipt_digest: str) -> None:
        if not any(
            event.entity_id == entity_id and event.receipt_digest == receipt_digest
            for event in events
            if event.name == name
        ):
            raise PortfolioRerunError(f"{name} event is not bound to trusted evidence")

    require_binding(
        "fresh_application",
        evidence.application_id,
        evidence.fresh_empty_draft.draft_content_hash,
    )
    if actor_profile.requires_daemon_access:
        assert evidence.daemon_access is not None
        require_binding(
            "daemon_discovered",
            evidence.daemon_access.connection_id,
            evidence.daemon_access.daemon_fingerprint,
        )
        require_binding(
            "pairing_completed",
            evidence.daemon_access.connection_id,
            evidence.daemon_access.daemon_fingerprint,
        )
    require_binding(
        "task_credential_bound",
        evidence.assignment_id,
        evidence.task_access.task_credential_digest,
    )
    require_binding(
        "assignment_created",
        evidence.assignment_id,
        assignment_envelope.payload_digest,
    )

    case_events = [event for event in events if event.name == "acceptance_case_completed"]
    if tuple(event.entity_id for event in case_events) != tuple(
        receipt.run_id for receipt in evidence.acceptance_receipts
    ):
        raise PortfolioRerunError("acceptance events do not bind the four independent runs")
    if tuple(event.receipt_digest for event in case_events) != tuple(
        receipt.aggregate_receipt_digest for receipt in evidence.acceptance_receipts
    ):
        raise PortfolioRerunError("acceptance events do not bind the aggregate receipts")

    require_binding(
        "publication_completed",
        evidence.application_id,
        evidence.published_content_hash,
    )
    require_binding("archive_completed", evidence.archive_id, evidence.archive_digest)
    require_binding(
        "usage_checkpoint",
        evidence.session_id,
        _canonical_digest(asdict(token_evidence)),
    )
    if not any(
        event.entity_id == evidence.application_id
        for event in events
        if event.name == "cleanup_completed"
    ):
        raise PortfolioRerunError("cleanup event is not bound to the accepted application")


class PhaseTimeline:
    """Partition one complete wall-clock interval into exactly eight spans."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._active: tuple[str, float, datetime] | None = None
        self._raw: list[tuple[str, datetime, datetime, float, str]] = []
        self._initial_monotonic: float | None = None
        self._final_monotonic: float | None = None
        self._initial_wall: datetime | None = None
        self._final_wall: datetime | None = None
        self._last_wall: datetime | None = None

    def _boundary(self) -> tuple[float, datetime]:
        monotonic = self._monotonic()
        if not math.isfinite(monotonic):
            raise PortfolioRerunError("phase monotonic clock is not finite")
        wall = self._wall_time()
        if wall.tzinfo is None or wall.utcoffset() != timedelta(0):
            raise PortfolioRerunError("phase wall clock must be UTC")
        if self._last_wall is not None and wall < self._last_wall:
            raise PortfolioRerunError("phase wall clock moved backwards")
        self._last_wall = wall
        return monotonic, wall

    def start(self, phase: str) -> None:
        if phase not in PHASES:
            raise PortfolioRerunError(f"unknown rerun phase: {phase}")
        if self._active is not None:
            raise PortfolioRerunError("rerun phases may not overlap")
        if self._raw or self._initial_monotonic is not None:
            raise PortfolioRerunError("a phase gap cannot be opened after timing starts")
        expected = PHASES[len(self._raw)]
        if phase != expected:
            raise PortfolioRerunError(f"expected phase {expected}, got {phase}")
        started, started_at = self._boundary()
        self._initial_monotonic = started
        self._initial_wall = started_at
        self._active = (phase, started, started_at)

    def transition(self, outcome: str, next_phase: str) -> None:
        """Close one phase and open the next at the exact same boundary."""

        if self._active is None:
            raise PortfolioRerunError("no rerun phase is active")
        current_index = len(self._raw)
        if current_index >= len(PHASES) - 1:
            raise PortfolioRerunError("the final phase must be finished, not transitioned")
        expected = PHASES[current_index + 1]
        if next_phase != expected:
            raise PortfolioRerunError(f"expected phase {expected}, got {next_phase}")
        phase, started, started_at = self._active
        boundary, boundary_at = self._boundary()
        if boundary < started:
            raise PortfolioRerunError("phase monotonic clock moved backwards")
        duration = (boundary_at - started_at).total_seconds()
        if not math.isfinite(duration) or duration < 0:
            raise PortfolioRerunError("phase wall-clock duration is invalid")
        self._raw.append(
            (phase, started_at, boundary_at, duration, outcome)
        )
        self._active = (next_phase, boundary, boundary_at)

    def transition_not_applicable(self, next_phase: str) -> None:
        """Close an actor-inapplicable phase without inventing elapsed work."""

        if self._active is None:
            raise PortfolioRerunError("no rerun phase is active")
        current_index = len(self._raw)
        if current_index >= len(PHASES) - 1:
            raise PortfolioRerunError("the final phase must be finished, not transitioned")
        expected = PHASES[current_index + 1]
        if next_phase != expected:
            raise PortfolioRerunError(f"expected phase {expected}, got {next_phase}")
        phase, started, started_at = self._active
        self._raw.append((phase, started_at, started_at, 0.0, "not_applicable"))
        self._active = (next_phase, started, started_at)

    def finish(self, outcome: str) -> None:
        if self._active is None:
            raise PortfolioRerunError("no rerun phase is active")
        if self._active[0] != PHASES[-1] or len(self._raw) != len(PHASES) - 1:
            raise PortfolioRerunError("only the final reporting phase may finish the timeline")
        phase, started, started_at = self._active
        boundary, boundary_at = self._boundary()
        if boundary < started:
            raise PortfolioRerunError("phase monotonic clock moved backwards")
        wall_duration = (boundary_at - started_at).total_seconds()
        if not math.isfinite(wall_duration) or wall_duration < 0:
            raise PortfolioRerunError("phase wall-clock duration is invalid")
        self._raw.append(
            (phase, started_at, boundary_at, wall_duration, outcome)
        )
        self._active = None
        self._final_monotonic = boundary
        self._final_wall = boundary_at

    @property
    def active_phase(self) -> str | None:
        return None if self._active is None else self._active[0]

    @property
    def total_elapsed_seconds(self) -> float:
        if self._initial_wall is None or self._final_wall is None:
            raise PortfolioRerunError("timeline has not reached the final report boundary")
        total = (self._final_wall - self._initial_wall).total_seconds()
        if not math.isfinite(total) or total <= 0:
            raise PortfolioRerunError("completed timeline has no positive wall-clock duration")
        return total

    def spans(self) -> tuple[PhaseSpan, ...]:
        if self._active is not None:
            raise PortfolioRerunError("cannot summarize an active rerun phase")
        if len(self._raw) != len(PHASES):
            raise PortfolioRerunError("timeline does not contain all eight rerun phases")
        total = self.total_elapsed_seconds
        durations = [row[3] for row in self._raw]
        # Consecutive boundaries telescope to total elapsed.  Assign any
        # floating-point rounding residue to reporting so the denominator has
        # no ninth, unclassified bucket.
        durations[-1] += total - math.fsum(durations)
        percentages = [duration * 100.0 / total for duration in durations]
        percentages[-1] += 100.0 - math.fsum(percentages)
        if not math.isclose(math.fsum(percentages), 100.0, rel_tol=0.0, abs_tol=1e-12):
            raise PortfolioRerunError("phase percentages do not sum to 100 percent")
        return tuple(
            PhaseSpan(
                phase=row[0],
                started_at=row[1].isoformat(),
                finished_at=row[2].isoformat(),
                duration_seconds=durations[index],
                duration_percentage=percentages[index],
                outcome=row[4],
            )
            for index, row in enumerate(self._raw)
        )

    def partial_spans(self) -> tuple[PhaseSpan, ...]:
        """Return only measured, closed intervals after a clock failure."""

        total = math.fsum(row[3] for row in self._raw)
        return tuple(
            PhaseSpan(
                phase=row[0],
                started_at=row[1].isoformat(),
                finished_at=row[2].isoformat(),
                duration_seconds=row[3],
                duration_percentage=(row[3] * 100.0 / total if total > 0 else 0.0),
                outcome=row[4],
            )
            for row in self._raw
        )


def _required_evidence_plan(
    manifest: ProjectManifest,
    actor_profile: BuilderActorProfile,
) -> dict[str, Any]:
    """Describe the receipts enforced by the dataclasses and validators above."""

    signed_semantics = [
        "fresh_application",
        "environment_generation",
        "assignment",
        "observable_event",
        (
            "token_checkpoint"
            if actor_profile.requires_daemon_access
            else "codex_token_usage"
        ),
        "publication",
        "acceptance_case",
        "archive",
        "cleanup",
        "execution_evidence",
    ]
    if not actor_profile.requires_daemon_access:
        signed_semantics.extend(
            [
                "public_only_forbidden_assistance_scan",
                "codex_fallback_prerequisite",
            ]
        )
    return {
        "schema_version": "v0.4.13-portfolio-required-evidence-1",
        "safe_projection": ["platform_public", "aggregate_receipt"],
        "attempt_bound_identities": [
            "application_id",
            "environment_generation",
            "assignment_id",
            "session_id",
        ],
        "signed_semantics": signed_semantics,
        "acceptance_case_ids": ["debug", *manifest.seed_ids],
        "max_session_tokens": (
            MAX_SESSION_TOKENS if actor_profile.requires_daemon_access else None
        ),
        "formal_builder_actor": actor_profile.formal_builder_actor,
        "builder_actor": actor_profile.builder_actor,
        "daemon_access_required": actor_profile.requires_daemon_access,
        "actor_inapplicable_phases": sorted(actor_profile.inapplicable_phases),
        "ml_stage_bindings_required": manifest.project_id == "EXP-LILIES-006",
        "private_reasoning_collected": False,
    }

def project_plan(
    manifest: ProjectManifest,
    adapter_receipt: AdapterCapabilityReceipt | None = None,
    receipt_verifier: ReceiptTrustVerifier | None = None,
    *,
    attempt_id: str | None = None,
    actor_profile: BuilderActorProfile = LILIES_ACTOR_PROFILE,
) -> dict[str, Any]:
    """Return a redacted, non-executing plan for one fresh application."""

    _require_supported_actor_profile(actor_profile)
    adapter_receipt_validated = False
    if adapter_receipt is not None and receipt_verifier is not None:
        if type(receipt_verifier) is not ReceiptTrustVerifier:
            raise PortfolioRerunError("receipt verifier is not task-author configured")
        validate_adapter_capability_receipt(
            manifest,
            adapter_receipt,
            receipt_verifier,
            actor_profile,
        )
        adapter_receipt_validated = True

    hooks_by_phase = {
        "environment_bootstrap": ("environment",),
        "assignment_provision": ("assignment_provision",),
        "host_result_verification": ("public_debug", "sealed_seed"),
        "cleanup_reporting": ("cleanup",),
    }
    phases: list[dict[str, Any]] = []
    for phase in PHASES:
        item: dict[str, Any] = {
            "phase": phase,
            "actions": (
                []
                if phase in actor_profile.inapplicable_phases
                else list(CORE_PHASE_ACTIONS[phase])
            ),
            "applicability": (
                "not_applicable"
                if phase in actor_profile.inapplicable_phases
                else "required"
            ),
        }
        hook_names = hooks_by_phase.get(phase, ())
        if hook_names:
            item["task_author_hooks"] = [
                {
                    "name": hook_name,
                    "route": manifest.hooks[hook_name].route,
                    "route_declared": manifest.hooks[hook_name].available,
                    "available": (
                        adapter_receipt is not None
                        and adapter_receipt_validated
                        and manifest.hooks[hook_name].available
                        and any(
                            capability.startswith(
                                f"{_hook_capability(manifest.project_id, hook_name)}:"
                            )
                            for capability in adapter_receipt.capabilities
                        )
                    ),
                    "required_capability": _hook_capability(
                        manifest.project_id,
                        hook_name,
                    ),
                    "capability_gap": manifest.hooks[hook_name].capability_gap,
                    "broker_capability": manifest.hooks[hook_name].capability_id,
                    "commands": [
                        asdict(command) for command in manifest.hooks[hook_name].commands
                    ],
                }
                for hook_name in hook_names
            ]
        phases.append(item)
    if attempt_id is not None:
        _require_uuid(attempt_id, field_name="attempt id")
    return {
        "attempt_id": attempt_id,
        "project_id": manifest.project_id,
        "revision": manifest.revision,
        "fresh_application_required": True,
        "fresh_builder_session_required": True,
        "real_adapter": {
            "available": (
                manifest.real_adapter_gap is None
                and adapter_receipt is not None
                and adapter_receipt_validated
            ),
            "capability_receipt_present": adapter_receipt is not None,
            "capability_receipt_trusted": adapter_receipt_validated,
            "capability_gap": manifest.real_adapter_gap,
            "identity": None if adapter_receipt is None else adapter_receipt.adapter_id,
            "digest": None if adapter_receipt is None else adapter_receipt.adapter_digest,
            "capability_digest": (
                None if adapter_receipt is None else adapter_receipt.capability_digest
            ),
            "required_capabilities": list(
                required_adapter_capabilities(manifest, actor_profile)
            ),
        },
        "builder": {
            "formal_builder_actor": actor_profile.formal_builder_actor,
            "actor": actor_profile.builder_actor,
            "source_root": (
                str(STANDALONE_LILIES_ROOT)
                if actor_profile.requires_daemon_access
                else None
            ),
            "build_endpoint": (
                LOCAL_BUILD_ENDPOINT if actor_profile.requires_daemon_access else None
            ),
            "auto_publish": True,
            "max_session_tokens": (
                MAX_SESSION_TOKENS if actor_profile.requires_daemon_access else None
            ),
            "model_egress_default": "disabled",
            "observable_surfaces": (
                [
                    LOCAL_MESSAGES_ENDPOINT,
                    LOCAL_EVENTS_ENDPOINT,
                    LOCAL_USAGE_ENDPOINT,
                ]
                if actor_profile.requires_daemon_access
                else []
            ),
            "private_reasoning_collected": False,
        },
        "public_materials": list(manifest.public_materials),
        "acceptance_runs": ["debug", *manifest.seed_ids],
        "acceptance_invariants": {
            "same_immutable_published_version": True,
            "workflow_mutation_between_runs": False,
        },
        "required_execution_evidence": _required_evidence_plan(
            manifest,
            actor_profile,
        ),
        "phases": phases,
    }


PhaseExecutor = Callable[[ProjectManifest, str, Mapping[str, Any]], PhaseExecution]


def _safe_failure_reason(
    phase: str,
    error: BaseException,
    *,
    controlled: bool = False,
) -> str:
    if controlled and isinstance(error, PortfolioRerunError):
        return f"{phase}: {error}"
    return f"{phase}: {type(error).__name__}"


def _canonical_digest(value: Any) -> str:
    payload = _canonical_json_bytes(value)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _serialize_report_body_digest(value: Mapping[str, Any]) -> str:
    """Serialize the safe report body while cleanup/reporting still owns the clock."""

    return _canonical_digest(value)


class AttemptLedger:
    """Crash-safe, append-only attempt and freshness ledger for the external harness."""

    SCHEMA_VERSION = "v0.4.13-portfolio-attempt-ledger-r8-3"

    def __init__(
        self,
        path: Path,
        *,
        testkit: RealProjectTestkitAPI | None = None,
    ) -> None:
        self.path = path
        self.testkit = testkit or RealProjectTestkitAPI.load()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "attempts": [],
            "signed_terminal_attempts": [],
            "fallback_prerequisites": [],
        }

    def _load(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise PortfolioRerunError("attempt ledger must be a regular non-symlink file")
        if not self.path.exists():
            return self._empty()
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PortfolioRerunError("attempt ledger must be a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PortfolioRerunError("attempt ledger permissions must be 0600 or stricter")
        if metadata.st_size > 16 * 1024 * 1024:
            raise PortfolioRerunError("attempt ledger exceeds the bounded size")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PortfolioRerunError("attempt ledger is unreadable") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != self.SCHEMA_VERSION
            or not isinstance(payload.get("attempts"), list)
            or not isinstance(payload.get("signed_terminal_attempts"), list)
            or not isinstance(payload.get("fallback_prerequisites"), list)
        ):
            raise PortfolioRerunError("attempt ledger schema is invalid")
        return payload

    def _persist(self, payload: Mapping[str, Any]) -> None:
        _canonical_json_bytes(payload)
        try:
            self.testkit.write_report(self.path, dict(payload))
        except (OSError, TypeError, ValueError) as error:
            raise PortfolioRerunError("attempt ledger persistence failed") from error

    def start_attempt(
        self,
        *,
        attempt_id: str,
        project_id: str,
        manifest_revision: int,
        started_at: str,
        formal_builder_actor: str = LILIES_BUILDER_ACTOR,
        builder_actor: str = LILIES_BUILDER_ACTOR,
    ) -> None:
        _require_uuid(attempt_id, field_name="attempt id")
        _parse_observable_timestamp(started_at)
        payload = self._load()
        if any(row.get("attempt_id") == attempt_id for row in payload["attempts"]):
            raise PortfolioRerunError("attempt id already exists in the durable ledger")
        payload["attempts"].append(
            {
                "attempt_id": attempt_id,
                "project_id": project_id,
                "manifest_revision": manifest_revision,
                "formal_builder_actor": formal_builder_actor,
                "builder_actor": builder_actor,
                "started_at": started_at,
                "finished_at": None,
                "status": "running",
                "identity_bindings": {},
                "failure": None,
                "cleanup_failure": None,
                "report_digest": None,
                "report_body": None,
            }
        )
        self._persist(payload)

    def record_signed_terminal_attempt(
        self,
        envelope: SignedReceiptEnvelope,
        verifier: ReceiptTrustVerifier,
    ) -> None:
        validate_signed_receipt_envelope(envelope, verifier)
        if envelope.semantic_type != "attempt_terminal":
            raise PortfolioRerunError("terminal attempt receipt has the wrong semantic type")
        terminal = envelope.semantic_payload
        payload = self._load()
        row = next(
            (
                item
                for item in payload["attempts"]
                if item.get("attempt_id") == terminal.get("attempt_id")
            ),
            None,
        )
        if (
            row is None
            or row.get("status") != terminal.get("status")
            or row.get("report_digest") != terminal.get("report_digest")
            or row.get("project_id") != terminal.get("project_id")
            or row.get("formal_builder_actor")
            != terminal.get("formal_builder_actor")
            or row.get("builder_actor") != terminal.get("builder_actor")
        ):
            raise PortfolioRerunError(
                "terminal attempt receipt does not bind a persisted terminal row"
            )
        if terminal.get("status") not in {"completed", "failed"}:
            raise PortfolioRerunError("terminal attempt receipt status is invalid")
        if _parse_observable_timestamp(envelope.issued_at) < _parse_observable_timestamp(
            row["finished_at"]
        ):
            raise PortfolioRerunError(
                "terminal attempt receipt predates the persisted terminal state"
            )
        if any(
            item.get("attempt_id") == terminal.get("attempt_id")
            for item in payload["signed_terminal_attempts"]
        ):
            raise PortfolioRerunError("terminal attempt receipt was persisted more than once")
        payload["signed_terminal_attempts"].append(
            {
                "attempt_id": terminal["attempt_id"],
                "envelope": asdict(envelope),
            }
        )
        self._persist(payload)

    def register_fallback_prerequisite(
        self,
        *,
        attempt_id: str,
        scan_envelope: SignedReceiptEnvelope,
        prerequisite_envelope: SignedReceiptEnvelope,
        verifier: ReceiptTrustVerifier,
    ) -> None:
        validate_signed_receipt_envelope(scan_envelope, verifier)
        validate_signed_receipt_envelope(prerequisite_envelope, verifier)
        if scan_envelope.semantic_type != "public_only_forbidden_assistance_scan":
            raise PortfolioRerunError("fallback scan receipt has the wrong semantic type")
        if prerequisite_envelope.semantic_type != "codex_fallback_prerequisite":
            raise PortfolioRerunError("fallback prerequisite receipt has the wrong semantic type")
        payload = self._load()
        current = next(
            (row for row in payload["attempts"] if row.get("attempt_id") == attempt_id),
            None,
        )
        if current is None or current.get("status") != "running":
            raise PortfolioRerunError("fallback attempt is not running in the durable ledger")
        prerequisite = prerequisite_envelope.semantic_payload
        scan = scan_envelope.semantic_payload
        if (
            current.get("formal_builder_actor") != CODEX_FORMAL_BUILDER_ACTOR
            or current.get("builder_actor") != CODEX_FALLBACK_BUILDER_ACTOR
            or prerequisite.get("attempt_id") != attempt_id
            or prerequisite.get("project_id") != current.get("project_id")
            or scan.get("attempt_id") != attempt_id
            or scan.get("project_id") != current.get("project_id")
        ):
            raise PortfolioRerunError("fallback prerequisite actor or attempt binding is invalid")
        if (
            scan.get("contract_revision") != CONTRACT_REVISION
            or scan.get("formal_builder_actor") != CODEX_FORMAL_BUILDER_ACTOR
            or scan.get("builder_actor") != CODEX_FALLBACK_BUILDER_ACTOR
            or scan.get("task_scoped_public_api_only") is not True
            or scan.get("source_or_protected_content_exposed") is not False
            or scan.get("historical_attempt") is not False
            or scan.get("result") != "pass"
        ):
            raise PortfolioRerunError("signed public-only scan did not pass the r8 boundary")
        if (
            prerequisite.get("contract_revision") != CONTRACT_REVISION
            or prerequisite.get("formal_builder_actor") != CODEX_FORMAL_BUILDER_ACTOR
            or prerequisite.get("builder_actor") != CODEX_FALLBACK_BUILDER_ACTOR
            or prerequisite.get("forbidden_assistance_scan_receipt_id")
            != scan_envelope.receipt_id
            or prerequisite.get("forbidden_assistance_scan_digest")
            != scan_envelope.payload_digest
        ):
            raise PortfolioRerunError("fallback prerequisite does not bind its signed scan")
        prior = next(
            (
                row
                for row in payload["attempts"]
                if row.get("attempt_id")
                == prerequisite.get("bounded_lilies_attempt_id")
            ),
            None,
        )
        if (
            prior is None
            or prior.get("status") != "failed"
            or prior.get("formal_builder_actor") != LILIES_BUILDER_ACTOR
            or prior.get("builder_actor") != LILIES_BUILDER_ACTOR
            or prior.get("report_digest")
            != prerequisite.get("bounded_lilies_attempt_report_digest")
        ):
            raise PortfolioRerunError(
                "fallback prerequisite does not reference a persisted failed Lilies attempt"
            )
        validate_bounded_lilies_failure_report(prior)
        terminal_entry = next(
            (
                entry
                for entry in payload["signed_terminal_attempts"]
                if entry.get("attempt_id") == prior.get("attempt_id")
            ),
            None,
        )
        if terminal_entry is None or not isinstance(terminal_entry.get("envelope"), dict):
            raise PortfolioRerunError(
                "failed Lilies attempt has no persisted signed terminal receipt"
            )
        try:
            terminal_envelope = SignedReceiptEnvelope(**terminal_entry["envelope"])
        except (TypeError, ValueError) as error:
            raise PortfolioRerunError("persisted terminal receipt is malformed") from error
        validate_signed_receipt_envelope(terminal_envelope, verifier)
        if (
            terminal_envelope.receipt_id
            != prerequisite.get("bounded_lilies_terminal_receipt_id")
            or terminal_envelope.payload_digest
            != prerequisite.get("bounded_lilies_terminal_receipt_digest")
            or terminal_envelope.semantic_type != "attempt_terminal"
            or terminal_envelope.semantic_payload
            != {
                "project_id": prior["project_id"],
                "attempt_id": prior["attempt_id"],
                "formal_builder_actor": LILIES_BUILDER_ACTOR,
                "builder_actor": LILIES_BUILDER_ACTOR,
                "status": "failed",
                "report_digest": prior["report_digest"],
            }
        ):
            raise PortfolioRerunError(
                "fallback prerequisite does not bind the signed failed Lilies terminal"
            )
        if not (
            _parse_observable_timestamp(prior["finished_at"])
            <= _parse_observable_timestamp(terminal_envelope.issued_at)
            <= _parse_observable_timestamp(current["started_at"])
        ):
            raise PortfolioRerunError(
                "fallback prerequisite does not prove a prior Lilies terminal failure"
            )
        identities = prerequisite.get("freshness_identities")
        if not isinstance(identities, Mapping) or set(identities) != {
            "application_id",
            "environment_generation",
            "assignment_id",
            "session_id",
            "isolated_context_id",
        }:
            raise PortfolioRerunError("fallback freshness identities are incomplete")
        if prerequisite.get("freshness_identity_digest") != _canonical_digest(identities):
            raise PortfolioRerunError("fallback freshness identity digest is invalid")
        current_bindings = current.get("identity_bindings", {})
        if any(current_bindings.get(name) != value for name, value in identities.items()):
            raise PortfolioRerunError(
                "fallback prerequisite is not bound to persisted current identities"
            )
        identity_values = list(identities.values())
        if len(set(identity_values)) != len(identity_values):
            raise PortfolioRerunError("fallback freshness identities collide internally")
        prior_values = {
            value
            for row in payload["attempts"]
            if row.get("attempt_id") != attempt_id
            for value in row.get("identity_bindings", {}).values()
        }
        if any(value in prior_values for value in identity_values):
            raise PortfolioRerunError("fallback freshness identities collide with history")
        if any(
            entry.get("attempt_id") == attempt_id
            for entry in payload["fallback_prerequisites"]
        ):
            raise PortfolioRerunError("fallback prerequisite was persisted more than once")
        payload["fallback_prerequisites"].append(
            {
                "attempt_id": attempt_id,
                "scan_envelope": asdict(scan_envelope),
                "prerequisite_envelope": asdict(prerequisite_envelope),
            }
        )
        self._persist(payload)

    def validate_fallback_prerequisite(
        self,
        attempt_id: str,
        evidence: SafeExecutionEvidence,
        envelopes: Sequence[SignedReceiptEnvelope],
        verifier: ReceiptTrustVerifier,
    ) -> None:
        assert evidence.fallback_eligibility is not None
        eligibility = evidence.fallback_eligibility
        envelope_by_id = {envelope.receipt_id: envelope for envelope in envelopes}
        scan = envelope_by_id.get(eligibility.forbidden_assistance_scan_receipt_id)
        prerequisite = envelope_by_id.get(eligibility.prerequisite_receipt_id)
        if scan is None or prerequisite is None:
            raise PortfolioRerunError("fallback signed provenance receipts are missing")
        payload = self._load()
        entries = [
            item
            for item in payload["fallback_prerequisites"]
            if item.get("attempt_id") == attempt_id
        ]
        entry = entries[0] if len(entries) == 1 else None
        if (
            entry is None
            or entry.get("scan_envelope") != asdict(scan)
            or entry.get("prerequisite_envelope") != asdict(prerequisite)
        ):
            raise PortfolioRerunError("fallback signed provenance is not durably persisted")
        validate_signed_receipt_envelope(scan, verifier)
        validate_signed_receipt_envelope(prerequisite, verifier)
        current = next(
            (row for row in payload["attempts"] if row.get("attempt_id") == attempt_id),
            None,
        )
        prerequisite_payload = prerequisite.semantic_payload
        prior = next(
            (
                row
                for row in payload["attempts"]
                if row.get("attempt_id")
                == prerequisite_payload.get("bounded_lilies_attempt_id")
            ),
            None,
        )
        if (
            current is None
            or current.get("status") != "running"
            or current.get("formal_builder_actor") != CODEX_FORMAL_BUILDER_ACTOR
            or current.get("builder_actor") != CODEX_FALLBACK_BUILDER_ACTOR
            or prior is None
            or prior.get("status") != "failed"
            or prior.get("formal_builder_actor") != LILIES_BUILDER_ACTOR
            or prior.get("builder_actor") != LILIES_BUILDER_ACTOR
            or prior.get("report_digest")
            != prerequisite_payload.get("bounded_lilies_attempt_report_digest")
        ):
            raise PortfolioRerunError(
                "fallback persisted attempt provenance changed before final validation"
            )
        validate_bounded_lilies_failure_report(prior)
        terminal_entries = [
            item
            for item in payload["signed_terminal_attempts"]
            if item.get("attempt_id") == prior.get("attempt_id")
        ]
        if len(terminal_entries) != 1 or not isinstance(
            terminal_entries[0].get("envelope"),
            dict,
        ):
            raise PortfolioRerunError(
                "fallback persisted terminal receipt is absent or duplicated"
            )
        try:
            terminal = SignedReceiptEnvelope(**terminal_entries[0]["envelope"])
        except (TypeError, ValueError) as error:
            raise PortfolioRerunError(
                "fallback persisted terminal receipt is malformed"
            ) from error
        validate_signed_receipt_envelope(terminal, verifier)
        if (
            terminal.receipt_id
            != prerequisite_payload.get("bounded_lilies_terminal_receipt_id")
            or terminal.payload_digest
            != prerequisite_payload.get("bounded_lilies_terminal_receipt_digest")
            or terminal.semantic_type != "attempt_terminal"
            or terminal.semantic_payload
            != {
                "project_id": prior["project_id"],
                "attempt_id": prior["attempt_id"],
                "formal_builder_actor": LILIES_BUILDER_ACTOR,
                "builder_actor": LILIES_BUILDER_ACTOR,
                "status": "failed",
                "report_digest": prior["report_digest"],
            }
        ):
            raise PortfolioRerunError(
                "fallback persisted terminal receipt no longer binds the Lilies failure"
            )
        if not (
            _parse_observable_timestamp(prior["finished_at"])
            <= _parse_observable_timestamp(terminal.issued_at)
            <= _parse_observable_timestamp(current["started_at"])
        ):
            raise PortfolioRerunError(
                "fallback persisted Lilies failure is not prior to the Codex attempt"
            )
        identities = prerequisite_payload.get("freshness_identities")
        if not isinstance(identities, Mapping) or any(
            current.get("identity_bindings", {}).get(name) != value
            for name, value in identities.items()
        ):
            raise PortfolioRerunError(
                "fallback persisted freshness identities changed before final validation"
            )
        identity_values = list(identities.values())
        historical_values = {
            value
            for row in payload["attempts"]
            if row.get("attempt_id") != attempt_id
            for value in row.get("identity_bindings", {}).values()
        }
        if (
            len(set(identity_values)) != len(identity_values)
            or any(value in historical_values for value in identity_values)
        ):
            raise PortfolioRerunError(
                "fallback persisted freshness identities are not unique"
            )

    def bind_identities(
        self,
        attempt_id: str,
        identities: Mapping[str, str],
    ) -> tuple[str, ...]:
        payload = self._load()
        current = next(
            (row for row in payload["attempts"] if row.get("attempt_id") == attempt_id),
            None,
        )
        if current is None or current.get("status") != "running":
            raise PortfolioRerunError("attempt is not running in the durable ledger")
        collisions: list[str] = []
        prior_values = {
            value
            for row in payload["attempts"]
            if row.get("attempt_id") != attempt_id
            for value in row.get("identity_bindings", {}).values()
        }
        for name, value in identities.items():
            if not isinstance(name, str) or not isinstance(value, str) or not value:
                raise PortfolioRerunError("attempt identity binding is invalid")
            if value in prior_values:
                collisions.append(name)
        current["identity_bindings"] = dict(sorted(identities.items()))
        self._persist(payload)
        return tuple(sorted(collisions))

    def finalize_attempt(
        self,
        *,
        attempt_id: str,
        status_value: Literal["completed", "failed"],
        finished_at: str,
        failure: str | None,
        cleanup_failure: str | None,
        report_digest: str,
        report_body: Mapping[str, Any],
    ) -> None:
        _parse_observable_timestamp(finished_at)
        _require_sha256(report_digest, field_name="attempt report digest")
        if _canonical_digest(report_body) != report_digest:
            raise PortfolioRerunError("attempt report body does not match its digest")
        payload = self._load()
        current = next(
            (row for row in payload["attempts"] if row.get("attempt_id") == attempt_id),
            None,
        )
        if current is None or current.get("status") != "running":
            raise PortfolioRerunError("attempt cannot be finalized more than once")
        current.update(
            {
                "finished_at": finished_at,
                "status": status_value,
                "failure": failure,
                "cleanup_failure": cleanup_failure,
                "report_digest": report_digest,
                "report_body": dict(report_body),
            }
        )
        self._persist(payload)

    def snapshot(self) -> Mapping[str, Any]:
        return self._load()


def execute_portfolio(
    manifests: Sequence[ProjectManifest],
    executor: PhaseExecutor,
    *,
    receipt_verifier: ReceiptTrustVerifier,
    adapter_receipts: Mapping[str, AdapterCapabilityReceipt],
    attempt_ledger: AttemptLedger,
    monotonic: Callable[[], float] = time.monotonic,
    wall_time: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    attempt_id_factory: Callable[[], str] = lambda: str(uuid4()),
    actor_profile: BuilderActorProfile = LILIES_ACTOR_PROFILE,
) -> tuple[ProjectExecutionReport, ...]:
    """Gate the exact portfolio around an injected real-project adapter."""

    _require_supported_actor_profile(actor_profile)
    expected_order = tuple(PROJECT_MANIFESTS)
    if tuple(manifest.project_id for manifest in manifests) != expected_order:
        raise PortfolioRerunError(
            "portfolio execution requires the exact six-project manifest order"
        )
    if type(receipt_verifier) is not ReceiptTrustVerifier:
        raise PortfolioRerunError("receipt verifier is not task-author configured")
    if executor is receipt_verifier:
        raise PortfolioRerunError("executor and receipt verifier must be independent")
    validation_errors = [
        error for manifest in manifests for error in validate_manifest(manifest)
    ]
    if validation_errors:
        raise PortfolioRerunError("; ".join(validation_errors))
    readiness_gaps = [
        f"{manifest.project_id}/{name}: {hook.capability_gap}"
        for manifest in manifests
        for name, hook in manifest.hooks.items()
        if not hook.available
    ]
    readiness_gaps.extend(
        f"{manifest.project_id}/real_adapter: {manifest.real_adapter_gap}"
        for manifest in manifests
        if manifest.real_adapter_gap is not None
    )
    if readiness_gaps:
        raise PortfolioRerunError(
            "portfolio hooks are not execution-ready: " + "; ".join(readiness_gaps)
        )
    if set(adapter_receipts) != set(expected_order):
        raise PortfolioRerunError(
            "adapter capability receipts must match the exact six-project portfolio"
        )
    adapter_receipt_ids: set[str] = set()
    for manifest in manifests:
        receipt = adapter_receipts[manifest.project_id]
        validate_adapter_capability_receipt(
            manifest,
            receipt,
            receipt_verifier,
            actor_profile,
        )
        if receipt.envelope.receipt_id in adapter_receipt_ids:
            raise PortfolioRerunError("adapter capability receipt was reused across projects")
        adapter_receipt_ids.add(receipt.envelope.receipt_id)

    reports: list[ProjectExecutionReport] = []
    for manifest in manifests:
        adapter_receipt = adapter_receipts[manifest.project_id]
        attempt_id = attempt_id_factory()
        _require_uuid(attempt_id, field_name="attempt id")
        attempt_ledger.start_attempt(
            attempt_id=attempt_id,
            project_id=manifest.project_id,
            manifest_revision=manifest.revision,
            started_at=datetime.now(timezone.utc).isoformat(),
            formal_builder_actor=actor_profile.formal_builder_actor,
            builder_actor=actor_profile.builder_actor,
        )

        failure: str | None = None
        cleanup_failure: str | None = None
        plan: Mapping[str, Any] = {
            "attempt_id": attempt_id, "project_id": manifest.project_id,
            "revision": manifest.revision, "cleanup_only": True,
            "formal_builder_actor": actor_profile.formal_builder_actor,
            "builder_actor": actor_profile.builder_actor,
        }
        timeline = PhaseTimeline(monotonic=monotonic, wall_time=wall_time)
        timeline_healthy = True
        events: list[ObservableEvent] = []
        signed_receipts: list[SignedReceiptEnvelope] = []
        signed_receipt_phases: list[str] = []
        signed_receipt_ids: set[str] = set()
        final_checkpoint: TokenCheckpoint | None = None
        final_codex_usage: CodexTokenUsageEvidence | None = None
        execution_evidence: SafeExecutionEvidence | None = None
        active_session: str | None = None
        fresh_application_id: str | None = None
        last_event_at: datetime | None = None
        last_receipt_at: datetime | None = None
        cleanup_calls = 0
        freshness_bindings: dict[str, str] = {}

        def add_binding(name: str, value: Any) -> None:
            if not isinstance(value, str) or not value:
                raise PortfolioRerunError(
                    f"signed freshness identity {name} is invalid"
                )
            existing = freshness_bindings.get(name)
            if existing is not None and existing != value:
                raise PortfolioRerunError(
                    f"signed freshness identity {name} changed during the attempt"
                )
            freshness_bindings[name] = value

        def collect_freshness(envelope: SignedReceiptEnvelope) -> None:
            nonlocal fresh_application_id
            payload = envelope.semantic_payload
            semantic_type = envelope.semantic_type
            add_binding(
                f"signed_receipt:{semantic_type}:{envelope.receipt_id}",
                envelope.receipt_id,
            )
            if payload.get("attempt_id") != attempt_id:
                raise PortfolioRerunError(
                    "signed execution receipt is not bound to the current attempt"
                )
            if semantic_type == "fresh_application":
                receipt = payload.get("receipt")
                if not isinstance(receipt, Mapping):
                    raise PortfolioRerunError("fresh application receipt is malformed")
                application_id = receipt.get("application_id")
                add_binding("application_id", application_id)
                fresh_application_id = application_id
            elif semantic_type == "environment_generation":
                add_binding("application_id", payload.get("application_id"))
                add_binding(
                    "environment_generation",
                    payload.get("environment_generation"),
                )
                add_binding("environment_receipt_id", envelope.receipt_id)
            elif semantic_type == "assignment":
                add_binding("application_id", payload.get("application_id"))
                add_binding("assignment_id", payload.get("assignment_id"))
                add_binding("session_id", payload.get("session_id"))
            elif semantic_type == "task_credential":
                add_binding("assignment_id", payload.get("assignment_id"))
                add_binding(
                    "task_credential_digest",
                    payload.get("task_credential_digest"),
                )
            elif semantic_type == "daemon_discovery":
                add_binding("connection_id", payload.get("connection_id"))
            elif semantic_type == "codex_fallback_prerequisite":
                identities = payload.get("freshness_identities")
                if not isinstance(identities, Mapping):
                    raise PortfolioRerunError(
                        "fallback prerequisite freshness identities are malformed"
                    )
                for name, value in identities.items():
                    add_binding(str(name), value)
            elif semantic_type == "archive":
                add_binding("archive_id", payload.get("archive_id"))
            elif semantic_type == "acceptance_case":
                receipt = payload.get("receipt")
                if not isinstance(receipt, Mapping):
                    raise PortfolioRerunError("acceptance receipt is malformed")
                case_id = receipt.get("case_id")
                if not isinstance(case_id, str) or not case_id:
                    raise PortfolioRerunError("acceptance case identity is invalid")
                add_binding(
                    f"case:{case_id}:environment_generation",
                    receipt.get("environment_generation"),
                )
                add_binding(f"case:{case_id}:run_id", receipt.get("run_id"))
            elif semantic_type in {"token_checkpoint", "codex_token_usage"}:
                token_payload = payload.get(
                    "checkpoint" if semantic_type == "token_checkpoint" else "usage"
                )
                if not isinstance(token_payload, Mapping):
                    raise PortfolioRerunError("token receipt is malformed")
                add_binding("session_id", token_payload.get("session_id"))
                if semantic_type == "token_checkpoint":
                    attempt_ids = token_payload.get("model_call_attempt_receipt_ids")
                    if not isinstance(attempt_ids, (list, tuple)):
                        raise PortfolioRerunError(
                            "model-call attempt history is malformed"
                        )
                    for index, receipt_id in enumerate(attempt_ids):
                        add_binding(f"model_call_attempt:{index}", receipt_id)

        def persist_freshness() -> None:
            collisions = attempt_ledger.bind_identities(
                attempt_id,
                freshness_bindings,
            )
            if collisions:
                raise PortfolioRerunError(
                    "durable freshness collision in " + ", ".join(collisions)
                )

        def absorb_result(result: PhaseExecution, *, phase: str) -> None:
            nonlocal active_session
            nonlocal final_checkpoint
            nonlocal final_codex_usage
            nonlocal last_event_at
            nonlocal last_receipt_at

            validated: list[tuple[SignedReceiptEnvelope, datetime]] = []
            pending_ids: set[str] = set()
            pending_last = last_receipt_at
            for envelope in result.signed_receipts:
                issued_at = validate_signed_receipt_envelope(
                    envelope,
                    receipt_verifier,
                )
                if not receipt_verifier.verify_execution_receipt(manifest, envelope):
                    raise PortfolioRerunError(
                        "trusted verifier rejected a signed execution receipt"
                    )
                if (
                    envelope.receipt_id in signed_receipt_ids
                    or envelope.receipt_id in pending_ids
                ):
                    raise PortfolioRerunError("signed execution receipt was replayed")
                if pending_last is not None and issued_at < pending_last:
                    raise PortfolioRerunError(
                        "signed execution receipts are not chronological"
                    )
                collect_freshness(envelope)
                validated.append((envelope, issued_at))
                pending_ids.add(envelope.receipt_id)
                pending_last = issued_at

            persist_freshness()

            fallback_scans = [
                envelope
                for envelope, _ in validated
                if envelope.semantic_type
                == "public_only_forbidden_assistance_scan"
            ]
            fallback_prerequisites = [
                envelope
                for envelope, _ in validated
                if envelope.semantic_type == "codex_fallback_prerequisite"
            ]
            if fallback_scans or fallback_prerequisites:
                if (
                    actor_profile != CODEX_FALLBACK_ACTOR_PROFILE
                    or phase != "assignment_provision"
                    or len(fallback_scans) != 1
                    or len(fallback_prerequisites) != 1
                ):
                    raise PortfolioRerunError(
                        "fallback scan and prerequisite receipts must appear once together "
                        "during assignment provision"
                    )
                attempt_ledger.register_fallback_prerequisite(
                    attempt_id=attempt_id,
                    scan_envelope=fallback_scans[0],
                    prerequisite_envelope=fallback_prerequisites[0],
                    verifier=receipt_verifier,
                )

            if phase in TOKEN_MONITORED_PHASES:
                if actor_profile.requires_daemon_access:
                    if result.token_checkpoint is None or result.codex_token_usage is not None:
                        raise PortfolioRerunError(
                            f"{manifest.project_id}/{phase}: missing Lilies token checkpoint"
                        )
                    validate_token_progress(final_checkpoint, result.token_checkpoint)
                    token_evidence: TokenCheckpoint | CodexTokenUsageEvidence = (
                        result.token_checkpoint
                    )
                    semantic_type = "token_checkpoint"
                    payload_key = "checkpoint"
                else:
                    if result.codex_token_usage is None or result.token_checkpoint is not None:
                        raise PortfolioRerunError(
                            f"{manifest.project_id}/{phase}: missing Codex usage evidence"
                        )
                    validate_codex_token_progress(
                        final_codex_usage,
                        result.codex_token_usage,
                    )
                    token_evidence = result.codex_token_usage
                    semantic_type = "codex_token_usage"
                    payload_key = "usage"
                expected_payload = {
                    "project_id": manifest.project_id,
                    "attempt_id": attempt_id,
                    payload_key: asdict(token_evidence),
                    "final": phase == PHASES[-1],
                }
                token_receipts = [
                    envelope
                    for envelope, _ in validated
                    if envelope.semantic_type == semantic_type
                    and envelope.semantic_payload == expected_payload
                ]
                if len(token_receipts) != 1:
                    raise PortfolioRerunError(
                        f"{manifest.project_id}/{phase}: token evidence is not "
                        "bound to one signed attempt/settlement receipt"
                    )
                if active_session is None:
                    active_session = token_evidence.session_id
                if actor_profile.requires_daemon_access:
                    assert isinstance(token_evidence, TokenCheckpoint)
                    final_checkpoint = token_evidence
                else:
                    assert isinstance(token_evidence, CodexTokenUsageEvidence)
                    final_codex_usage = token_evidence
            elif result.token_checkpoint is not None or result.codex_token_usage is not None:
                raise PortfolioRerunError(
                    f"{manifest.project_id}/{phase}: unexpected token evidence"
                )

            projected_events: list[tuple[ObservableEvent, datetime]] = []
            projected_previous = last_event_at
            for event in result.events:
                projected, parsed_at = _validated_observable_event(
                    event,
                    phase=phase,
                    previous_at=projected_previous,
                )
                matches = [
                    envelope
                    for envelope, _ in validated
                    if envelope.semantic_type == "observable_event"
                    and envelope.semantic_payload
                    == {
                        "project_id": manifest.project_id,
                        "attempt_id": attempt_id,
                        "event": asdict(projected),
                    }
                ]
                if len(matches) != 1:
                    raise PortfolioRerunError(
                        "observable event is not bound to one signed semantic receipt"
                    )
                projected_events.append((projected, parsed_at))
                projected_previous = parsed_at

            for envelope, issued_at in validated:
                signed_receipts.append(envelope)
                signed_receipt_phases.append(phase)
                signed_receipt_ids.add(envelope.receipt_id)
                last_receipt_at = issued_at
            for projected, parsed_at in projected_events:
                events.append(projected)
                last_event_at = parsed_at

        def invoke_cleanup() -> None:
            nonlocal cleanup_calls
            nonlocal cleanup_failure
            cleanup_calls += 1
            if cleanup_calls != 1:
                raise PortfolioRerunError("cleanup was invoked more than once")
            try:
                result = executor(manifest, PHASES[-1], plan)
            except BaseException as error:
                cleanup_failure = _safe_failure_reason(PHASES[-1], error)
                return
            try:
                absorb_result(result, phase=PHASES[-1])
                cleanup_receipts = [
                    envelope
                    for envelope in result.signed_receipts
                    if envelope.semantic_type == "cleanup"
                    and envelope.semantic_payload
                    == {
                        "project_id": manifest.project_id,
                        "attempt_id": attempt_id,
                        "application_id": fresh_application_id,
                        "outcome": "completed",
                    }
                ]
                if fresh_application_id is None or len(cleanup_receipts) != 1:
                    raise PortfolioRerunError(
                        "cleanup is not bound to one signed attempt/application receipt"
                    )
                if result.outcome != "completed":
                    raise PortfolioRerunError(
                        f"executor reported {result.outcome}"
                    )
            except BaseException as error:
                cleanup_failure = _safe_failure_reason(
                    PHASES[-1],
                    error,
                    controlled=isinstance(error, PortfolioRerunError),
                )

        try:
            timeline.start(PHASES[0])
        except BaseException as error:
            timeline_healthy = False
            failure = _safe_failure_reason(
                PHASES[0],
                error,
                controlled=isinstance(error, PortfolioRerunError),
            )
        if timeline_healthy:
            try:
                plan = project_plan(
                    manifest,
                    adapter_receipt,
                    receipt_verifier,
                    attempt_id=attempt_id,
                    actor_profile=actor_profile,
                )
            except BaseException as error:
                failure = _safe_failure_reason(
                    PHASES[0],
                    error,
                    controlled=isinstance(error, PortfolioRerunError),
                )

        if timeline_healthy:
            for index, phase in enumerate(PHASES[:-1]):
                if phase in actor_profile.inapplicable_phases:
                    try:
                        timeline.transition_not_applicable(PHASES[index + 1])
                    except BaseException as error:
                        timeline_healthy = False
                        failure = failure or _safe_failure_reason(
                            phase,
                            error,
                            controlled=isinstance(error, PortfolioRerunError),
                        )
                        break
                    continue
                outcome = "skipped"
                if failure is None:
                    try:
                        result = executor(manifest, phase, plan)
                        absorb_result(result, phase=phase)
                        outcome = result.outcome
                        if outcome != "completed":
                            failure = f"{phase}: executor reported {outcome}"
                    except BaseException as error:
                        failure = _safe_failure_reason(
                            phase,
                            error,
                            controlled=isinstance(error, PortfolioRerunError),
                        )
                        outcome = "failed"
                try:
                    timeline.transition(outcome, PHASES[index + 1])
                except BaseException as error:
                    timeline_healthy = False
                    failure = failure or _safe_failure_reason(
                        phase,
                        error,
                        controlled=isinstance(error, PortfolioRerunError),
                    )
                    break

        invoke_cleanup()

        spans: tuple[PhaseSpan, ...]
        if timeline_healthy and timeline.active_phase == PHASES[-1]:
            try:
                timeline.finish(
                    "failed"
                    if failure is not None or cleanup_failure is not None
                    else "completed"
                )
                spans = timeline.spans()
            except BaseException as error:
                timeline_healthy = False
                failure = failure or _safe_failure_reason(
                    PHASES[-1],
                    error,
                    controlled=isinstance(error, PortfolioRerunError),
                )
                spans = timeline.partial_spans()
        else:
            spans = timeline.partial_spans()
        total_elapsed = math.fsum(span.duration_seconds for span in spans)
        if failure is None and cleanup_failure is None:
            if len(spans) != len(PHASES):
                failure = "cleanup_reporting: timing evidence is incomplete"
            else:
                try:
                    validate_completed_actor_phases(actor_profile, spans)
                except PortfolioRerunError:
                    failure = "cleanup_reporting: completed timing evidence is invalid"

        if failure is None and cleanup_failure is None:
            try:
                if actor_profile.requires_daemon_access:
                    if final_checkpoint is None or final_codex_usage is not None:
                        raise PortfolioRerunError(
                            "completed Lilies project has no final token checkpoint"
                        )
                    final_token_evidence: TokenCheckpoint | CodexTokenUsageEvidence = (
                        final_checkpoint
                    )
                else:
                    if final_codex_usage is None or final_checkpoint is not None:
                        raise PortfolioRerunError(
                            "completed Codex project has no final usage evidence"
                        )
                    final_token_evidence = final_codex_usage
                execution_evidence = receipt_verifier.derive_execution_evidence(
                    manifest,
                    tuple(signed_receipts),
                )
                if execution_evidence.attempt_id != attempt_id:
                    raise PortfolioRerunError(
                        "execution evidence belongs to another attempt"
                    )
                if actor_profile_for_evidence(execution_evidence) != actor_profile:
                    raise PortfolioRerunError(
                        "execution evidence actor does not match the selected route"
                    )
                validate_execution_evidence(
                    manifest,
                    execution_evidence,
                    final_token_evidence,
                    tuple(signed_receipts),
                    receipt_verifier,
                    attempt_ledger,
                )
                validate_completed_observable_chain(
                    manifest.project_id,
                    events,
                    spans,
                    execution_evidence,
                    final_token_evidence,
                    signed_receipts,
                    signed_receipt_phases,
                )
                persist_freshness()
            except BaseException as error:
                cleanup_failure = _safe_failure_reason(
                    PHASES[-1],
                    error,
                    controlled=isinstance(error, PortfolioRerunError),
                )

        status: Literal["completed", "failed"] = (
            "failed" if failure is not None or cleanup_failure is not None else "completed"
        )
        if status == "failed" and len(spans) == len(PHASES):
            spans = (*spans[:-1], replace(spans[-1], outcome="failed"))
        event_counts = dict(Counter(event.name for event in events))
        output_summaries = tuple(
            event.summary
            for event in events
            if event.kind in {"tool_result", "test", "run", "artifact"}
        )

        def build_report_body() -> dict[str, Any]:
            return {
                "schema_version": "v0.4.13-portfolio-rerun-report-body-r8-1",
                "attempt_id": attempt_id,
                "project_id": manifest.project_id,
                "manifest_revision": manifest.revision,
                "formal_builder_actor": actor_profile.formal_builder_actor,
                "builder_actor": actor_profile.builder_actor,
                "receipt_trust_root": asdict(receipt_verifier.trust_root),
                "status": status,
                "timing_complete": len(spans) == len(PHASES),
                "phases": [asdict(span) for span in spans],
                "total_elapsed_seconds": total_elapsed,
                "timing_residual_seconds": 0.0,
                "observable_event_counts": event_counts,
                "observable_events": [asdict(event) for event in events],
                "output_summaries": list(output_summaries),
                "max_session_tokens": (
                    MAX_SESSION_TOKENS
                    if actor_profile.requires_daemon_access
                    else None
                ),
                "token_usage_authoritativeness": (
                    "exact"
                    if final_checkpoint is not None
                    else (
                        None
                        if final_codex_usage is None
                        else final_codex_usage.availability
                    )
                ),
                "final_token_checkpoint": (
                    None if final_checkpoint is None else asdict(final_checkpoint)
                ),
                "final_codex_token_usage": (
                    None if final_codex_usage is None else asdict(final_codex_usage)
                ),
                "execution_evidence": (
                    None if execution_evidence is None else asdict(execution_evidence)
                ),
                "signed_receipts": [
                    asdict(envelope) for envelope in signed_receipts
                ],
                "signed_receipt_phases": list(signed_receipt_phases),
                "signed_receipt_chain_digest": _receipt_chain_digest(signed_receipts),
                "failure": failure or cleanup_failure,
                "cleanup_failure": cleanup_failure,
            }

        report_body = build_report_body()
        try:
            report_digest = _serialize_report_body_digest(report_body)
        except BaseException as error:
            cleanup_failure = cleanup_failure or _safe_failure_reason(
                PHASES[-1],
                error,
                controlled=isinstance(error, PortfolioRerunError),
            )
            status = "failed"
            if len(spans) == len(PHASES):
                spans = (*spans[:-1], replace(spans[-1], outcome="failed"))
            report_body = build_report_body()
            report_digest = _canonical_digest(report_body)
        attempt_ledger.finalize_attempt(
            attempt_id=attempt_id,
            status_value=status,
            finished_at=datetime.now(timezone.utc).isoformat(),
            failure=failure or cleanup_failure,
            cleanup_failure=cleanup_failure,
            report_digest=report_digest,
            report_body=report_body,
        )
        reports.append(
            ProjectExecutionReport(
                attempt_id=attempt_id,
                project_id=manifest.project_id,
                manifest_revision=manifest.revision,
                builder_actor=actor_profile.builder_actor,
                formal_builder_actor=actor_profile.formal_builder_actor,
                status=status,
                phases=spans,
                total_elapsed_seconds=total_elapsed,
                timing_residual_seconds=0.0,
                observable_event_counts=event_counts,
                observable_events=tuple(events),
                output_summaries=output_summaries,
                final_token_checkpoint=final_checkpoint,
                final_codex_token_usage=final_codex_usage,
                execution_evidence=execution_evidence,
                serialized_report_body_digest=report_digest,
                failure=failure or cleanup_failure,
                cleanup_failure=cleanup_failure,
            )
        )
        if status == "failed":
            break
    return tuple(reports)


def _selected_manifests(project_ids: Sequence[str]) -> list[ProjectManifest]:
    if not project_ids:
        return list(PROJECT_MANIFESTS.values())
    unknown = sorted(set(project_ids) - set(PROJECT_MANIFESTS))
    if unknown:
        raise PortfolioRerunError(f"unknown project ids: {', '.join(unknown)}")
    selected = set(project_ids)
    return [manifest for key, manifest in PROJECT_MANIFESTS.items() if key in selected]


def _validation_payload(
    manifests: Sequence[ProjectManifest],
    actor_profile: BuilderActorProfile = LILIES_ACTOR_PROFILE,
) -> dict[str, Any]:
    _require_supported_actor_profile(actor_profile)
    errors = [error for manifest in manifests for error in validate_manifest(manifest)]
    gaps = [
        {
            "project_id": manifest.project_id,
            "hook": name,
            "gap": hook.capability_gap,
        }
        for manifest in manifests
        for name, hook in manifest.hooks.items()
        if not hook.available
    ]
    gaps.extend(
        {
            "project_id": manifest.project_id,
            "hook": "real_adapter",
            "gap": manifest.real_adapter_gap,
        }
        for manifest in manifests
        if manifest.real_adapter_gap is not None
    )
    return {
        "schema_version": "v0.4.13-portfolio-rerun-plan-1",
        "structurally_valid": not errors,
        "execution_ready": not errors and not gaps,
        "errors": errors,
        "capability_gaps": gaps,
        "contract_revision": CONTRACT_REVISION,
        "formal_builder_actor": actor_profile.formal_builder_actor,
        "builder_actor": actor_profile.builder_actor,
        "max_session_tokens": (
            MAX_SESSION_TOKENS if actor_profile.requires_daemon_access else None
        ),
        "project_order": [manifest.project_id for manifest in manifests],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List, validate, or dry-run the six-project r8 Builder rerun."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="list projects and hook readiness")
    action.add_argument("--validate", action="store_true", help="validate without executing")
    action.add_argument("--dry-run", action="store_true", help="print the sequential run plan")
    parser.add_argument(
        "--builder-actor",
        choices=(LILIES_BUILDER_ACTOR, CODEX_FALLBACK_BUILDER_ACTOR),
        default=LILIES_BUILDER_ACTOR,
        help="select the Lilies route or the explicitly eligible r8 Codex fallback route",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        choices=tuple(PROJECT_MANIFESTS),
        help="select a project; repeat to select more than one",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifests = _selected_manifests(args.project)
        actor_profile = (
            LILIES_ACTOR_PROFILE
            if args.builder_actor == LILIES_BUILDER_ACTOR
            else CODEX_FALLBACK_ACTOR_PROFILE
        )
        validation = _validation_payload(manifests, actor_profile)
        if args.validate:
            payload: Any = validation
        elif args.list:
            payload = {
                "project_order": validation["project_order"],
                "projects": [
                    {
                        "project_id": manifest.project_id,
                        "revision": manifest.revision,
                        "hooks": {
                            name: {
                                "available": hook.available,
                                "capability_gap": hook.capability_gap,
                            }
                            for name, hook in manifest.hooks.items()
                        },
                    }
                    for manifest in manifests
                ],
            }
        else:
            payload = {
                "validation": validation,
                "execution_performed": False,
                "plans": [
                    project_plan(manifest, actor_profile=actor_profile)
                    for manifest in manifests
                ],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if not validation["structurally_valid"]:
            return 2
        if not args.list and not validation["execution_ready"]:
            return 2
        return 0
    except PortfolioRerunError as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
