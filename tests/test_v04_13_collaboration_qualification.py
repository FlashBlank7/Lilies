from __future__ import annotations

import asyncio
import sqlite3
import stat
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaboration_models import (
    ApprovalDecisionRequest,
    ChannelCloseRequest,
    ChannelSettingsRequest,
    CollaborationChannel,
    CollaborationReportPayload,
    DeveloperResponse,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_qualification import (
    FAULT_INJECTION_LANES,
    PIPELINE_QUALIFICATION_CASE_IDS,
    PIPELINE_QUALIFICATION_CASES,
    PIPELINE_QUALIFICATION_COMMANDS,
    PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS,
    QualificationCommandResult,
    QualificationPytestOutcomes,
    QualificationSurfaceResult,
    build_fault_injection_qualification,
    build_pipeline_qualification_bundle,
    canonical_digest,
    command_specs_by_id,
    qualification_source_revision,
)
from agent_platform.collaboration_service import (
    CollaborationClosed,
    CollaborationConflict,
    CollaborationPrincipal,
    CollaborationService,
    _studio_derived_status,
)
from agent_platform.collaboration_storage import (
    CollaborationNotFound as StorageNotFound,
    CollaborationStore,
)
from agent_platform.lilies_models import AssignmentMode, CollaborationScope
from agent_platform.qualification_fault_recorder import record_fault_iteration
from tests.test_v04_13_collaboration_models import developer_response_payload
from tests.test_v04_13_collaboration_sqlite_integration import (
    _control_message,
    _report_payload,
    _store_with_channel,
)
from scripts.run_v04_13_pipeline_qualification import _run_command


FIXED_TIME = datetime(2026, 7, 24, 3, 4, 5, tzinfo=timezone.utc)


def _principals(
    channel: dict[str, object],
) -> tuple[CollaborationPrincipal, CollaborationPrincipal, CollaborationPrincipal]:
    channel_id = UUID(str(channel["channel_id"]))
    assignment_id = UUID(str(channel["assignment_id"]))
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(channel["lilies_session_id"]),
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )
    developer = CollaborationPrincipal(
        role=SenderRole.codex,
        sender_id="codex-developer",
        scopes=frozenset({"collaboration.developer"}),
    )
    return lilies, user, developer


def _passed_command_results() -> list[QualificationCommandResult]:
    return [
        QualificationCommandResult(
            command_id=spec.command_id,
            case_ids=list(spec.case_ids),
            argv=list(spec.argv),
            status="passed",
            exit_code=0,
            duration_ms=1,
            output_digest=canonical_digest(
                {"command_id": spec.command_id, "result": "passed"}
            ),
            pytest_outcomes=QualificationPytestOutcomes(
                collected=1,
                passed=1,
                failed=0,
                errors=0,
                skipped=0,
                xfailed=0,
                xpassed=0,
            ),
        )
        for spec in PIPELINE_QUALIFICATION_COMMANDS
    ]


def _fault_record_payloads() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    commands = command_specs_by_id()
    for lane in FAULT_INJECTION_LANES:
        for iteration in range(1, 101):
            counters = {
                "attempted_iterations": 1,
                lane.success_counter: 1,
                **{counter: 0 for counter in lane.zero_counters},
            }
            payload: dict[str, object] = {
                "lane": lane.lane,
                "iteration": iteration,
                "status": "passed",
                "counters": counters,
                "command_id": lane.command_id,
                "command": list(commands[lane.command_id].argv),
                "output_digest": canonical_digest(
                    {
                        "lane": lane.lane,
                        "iteration": iteration,
                        "actual_output": "passed",
                    }
                ),
            }
            records.append(
                {
                    **payload,
                    "record_digest": canonical_digest(payload),
                }
            )
    return records


def _passed_surface(source: str) -> QualificationSurfaceResult:
    observations = [{"surface": source, "status": "passed"}]
    return QualificationSurfaceResult(
        status="passed",
        source=source,
        summary=f"{source} completed with independently retained evidence.",
        observations=observations,
        digest=canonical_digest(observations),
    )


def _blocked_browser_surface() -> QualificationSurfaceResult:
    observations = [
        {
            "operation": "browser_discovery",
            "requested_url": "http://127.0.0.1:3000/",
            "result": "No browser is available",
            "available_browsers": [],
        }
    ]
    return QualificationSurfaceResult(
        status="blocked_by_environment",
        source="browser-runtime:actual-discovery",
        summary="The supported browser runtime exposed no controllable browser.",
        observations=observations,
        claim_ceiling=(
            "Q behavior is API/deterministic verified; rendered interaction, "
            "layout, console, network, and screenshot claims are not verified."
        ),
        recheck_trigger="A supported browser appears in runtime discovery.",
        digest=canonical_digest(observations),
    )


def _required_extra_evidence() -> list[dict[str, object]]:
    source_revision = "qualification-test-revision"
    lifecycle = [
        "work_item",
        "result",
        "rework",
        "independent_lilies_review",
        "accept",
        "close",
        "stop",
        "archive",
    ]
    event_types = [
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
    ]

    def dispatch_history(
        *,
        assignment_id: str,
        work_item_id: str,
        roles: list[str],
        mode: str,
    ) -> list[dict[str, object]]:
        histories: list[dict[str, object]] = []
        for index, role in enumerate(roles, start=1):
            histories.append(
                {
                    "dispatch_id": f"dispatch-{assignment_id}-{index}",
                    "outbox_id": f"outbox-{assignment_id}-{index}",
                    "attempt": 1,
                    "assignment_id": assignment_id,
                    "work_item_id": work_item_id,
                    "destination_role": role,
                    "outbox_kind": (
                        "work_dispatch" if role == "codex" else "lilies_review"
                    ),
                    "execution_mode": mode,
                    "grant_digest": canonical_digest(f"{assignment_id}:{role}"),
                    "status": "delivered",
                    "invocation_fence_id": (
                        f"invocation-fence-{assignment_id}-{index}"
                    ),
                }
            )
        return histories

    def tool_usage_history(mode: str) -> list[dict[str, object]]:
        result_ids = [f"{mode}-result-1", f"{mode}-result-2"]
        review_ids = [f"{mode}-review-1", f"{mode}-review-2"]
        calls = [
            ("codex", "process_run", 1, "result", result_ids[0]),
            ("codex", "git_diff", 1, None, None),
            ("lilies", "process_run", 1, "review", review_ids[0]),
            ("codex", "workspace_patch", 0, None, None),
            ("codex", "process_run", 1, "result", result_ids[1]),
            ("codex", "git_diff", 1, None, None),
            ("lilies", "process_run", 1, "review", review_ids[1]),
        ]
        return [
            {
                "reservation_id": f"{mode}-usage-reservation-{index}",
                "actor_role": actor_role,
                "usage_id": f"{mode}-stable-usage-{index}",
                "tool_name": tool_name,
                "request_digest": canonical_digest(
                    f"{mode}:request:{index}"
                ),
                "tool_calls": 1,
                "commands": commands,
                "command_cwd": (
                    "tests"
                    if tool_name == "process_run"
                    else ("src" if tool_name == "git_diff" else None)
                ),
                "status": "completed",
                "response_digest": canonical_digest(
                    f"{mode}:response:{index}"
                ),
                "output_digest": (
                    canonical_digest(f"{mode}:output:{index}")
                    if commands
                    else None
                ),
                "consumer_type": consumer_type,
                "consumer_id": consumer_id,
            }
            for index, (
                actor_role,
                tool_name,
                commands,
                consumer_type,
                consumer_id,
            ) in enumerate(calls, start=1)
        ]

    def scenario(mode: str) -> dict[str, object]:
        assignment_id = f"{mode}-assignment"
        work_item_id = f"{mode}-work-item"
        roles = (
            ["codex", "codex"]
            if mode == "manual_dispatch"
            else ["codex", "lilies", "codex", "lilies"]
        )
        return {
            "status": "passed",
            "mode": mode,
            "assignment_id": assignment_id,
            "software_id": f"plain-python-library-{mode}",
            "baseline_commit": "a" * 40,
            "enterprise_denominator": False,
            "workflow_application_required": False,
            "builder_required": False,
            "task_package_required": False,
            "oracle_required": False,
            "manual_waited_before_dispatch": mode == "manual_dispatch",
            "manual_waited_for_review": mode == "manual_dispatch",
            "manual_waited_after_rework": mode == "manual_dispatch",
            "checkpoints": [{"step": step} for step in lifecycle],
            "results": [
                {
                    "result_id": f"{mode}-result-1",
                    "passed": False,
                    "exit_code": 1,
                    "diff_digest": canonical_digest(f"{mode}:diff:1"),
                    "output_digest": canonical_digest(f"{mode}:output:1"),
                },
                {
                    "result_id": f"{mode}-result-2",
                    "passed": True,
                    "exit_code": 0,
                    "diff_digest": canonical_digest(f"{mode}:diff:2"),
                    "output_digest": canonical_digest(f"{mode}:output:2"),
                },
            ],
            "review_ids": [f"{mode}-review-1", f"{mode}-review-2"],
            "independent_review_snapshots": [
                {
                    "changed_paths": [],
                    "source_repository_unchanged": True,
                    "promotion_state": "review_snapshot_only",
                    "receipt_digest": canonical_digest(f"{mode}:receipt:1"),
                    "snapshot_digest": canonical_digest(f"{mode}:snapshot:1"),
                },
                {
                    "changed_paths": ["src/mathlib.py"],
                    "source_repository_unchanged": True,
                    "promotion_state": "review_snapshot_only",
                    "receipt_digest": canonical_digest(f"{mode}:receipt:2"),
                    "snapshot_digest": canonical_digest(f"{mode}:snapshot:2"),
                },
            ],
            "review_verdicts": ["rework", "accepted"],
            "dispatch_history": dispatch_history(
                assignment_id=assignment_id,
                work_item_id=work_item_id,
                roles=roles,
                mode=mode,
            ),
            "store_event_history": [
                {"event_type": event_type} for event_type in event_types
            ],
            "tool_usage_history": tool_usage_history(mode),
            "restart_store_history_equal": True,
            "restart_tool_usage_equal": True,
            "restart_dispatch_history_equal": True,
            "original_grants_unchanged": True,
            "source_repository_unchanged": True,
            "final_assignment_status": "archived",
            "final_work_item_status": "closed",
            "executed_lifecycle": lifecycle,
        }

    standalone_result_ids = ["standalone-result-1", "standalone-result-2"]
    standalone_snapshots = [
        {
            "receipt_id": "standalone-receipt-1",
            "review_snapshot_id": "standalone-snapshot-1",
            "result_id": standalone_result_ids[0],
            "baseline_commit": "b" * 40,
            "diff_digest": canonical_digest("standalone-diff-1"),
            "snapshot_digest": canonical_digest("standalone-snapshot-digest-1"),
            "receipt_digest": canonical_digest("standalone-receipt-digest-1"),
            "changed_paths": [],
            "promotion_state": "review_snapshot_only",
            "source_repository_unchanged": True,
        },
        {
            "receipt_id": "standalone-receipt-2",
            "review_snapshot_id": "standalone-snapshot-2",
            "result_id": standalone_result_ids[1],
            "baseline_commit": "b" * 40,
            "diff_digest": canonical_digest("standalone-diff-2"),
            "snapshot_digest": canonical_digest("standalone-snapshot-digest-2"),
            "receipt_digest": canonical_digest("standalone-receipt-digest-2"),
            "changed_paths": ["src/mathlib.py"],
            "promotion_state": "review_snapshot_only",
            "source_repository_unchanged": True,
        },
    ]
    standalone_event_types = [
        "assignment.created",
        "work_item.created",
        "work_item.awaiting_dispatch",
        "work_item.leased",
        "work_item.working",
        "work_item.result_submitted",
        "work_item.rework",
        "work_item.awaiting_dispatch",
        "work_item.leased",
        "work_item.working",
        "work_item.result_submitted",
        "work_item.accepted",
        "work_item.closed",
        "assignment.stopped",
        "assignment.archived",
    ]

    def standalone_cli_operation(
        command: str,
        semantic_response: dict[str, object],
    ) -> dict[str, object]:
        return {
            "command": command,
            "exit_code": 0,
            "process_boundary": "new_cli_subprocess",
            "semantic_response": semantic_response,
            "response_digest": canonical_digest(semantic_response),
        }

    standalone_cli_operations = [
        standalone_cli_operation(
            "create",
            {"assignment_id": "standalone-assignment", "status": "active"},
        ),
        standalone_cli_operation(
            "status",
            {"assignment_id": "standalone-assignment", "status": "active"},
        ),
        standalone_cli_operation(
            "work-create",
            {"work_item_id": "standalone-work-item", "status": "proposed"},
        ),
        standalone_cli_operation(
            "dispatch",
            {
                "work_item_id": "standalone-work-item",
                "status": "awaiting_dispatch",
            },
        ),
        standalone_cli_operation(
            "lease",
            {"lease_id": "standalone-lease-1", "owner_role": "codex"},
        ),
        standalone_cli_operation(
            "start",
            {"work_item_id": "standalone-work-item", "status": "working"},
        ),
        standalone_cli_operation(
            "result",
            {
                "work_item_id": "standalone-work-item",
                "status": "ready_for_lilies_review",
            },
        ),
        standalone_cli_operation(
            "result-show",
            {
                "result_id": standalone_result_ids[0],
                "diff_digest": standalone_snapshots[0]["diff_digest"],
                "test_passed": False,
            },
        ),
        standalone_cli_operation("review-prepare", standalone_snapshots[0]),
        standalone_cli_operation("review-prepare", standalone_snapshots[0]),
        standalone_cli_operation(
            "review",
            {
                "work_item_id": "standalone-work-item",
                "status": "awaiting_dispatch",
                "verdict": "rework",
            },
        ),
        standalone_cli_operation(
            "dispatch",
            {
                "work_item_id": "standalone-work-item",
                "status": "awaiting_dispatch",
            },
        ),
        standalone_cli_operation(
            "lease",
            {"lease_id": "standalone-lease-2", "owner_role": "codex"},
        ),
        standalone_cli_operation(
            "start",
            {"work_item_id": "standalone-work-item", "status": "working"},
        ),
        standalone_cli_operation(
            "result",
            {
                "work_item_id": "standalone-work-item",
                "status": "ready_for_lilies_review",
            },
        ),
        standalone_cli_operation(
            "result-show",
            {
                "result_id": standalone_result_ids[1],
                "diff_digest": standalone_snapshots[1]["diff_digest"],
                "test_passed": True,
            },
        ),
        standalone_cli_operation("review-prepare", standalone_snapshots[1]),
        standalone_cli_operation("review-prepare", standalone_snapshots[1]),
        standalone_cli_operation(
            "review",
            {
                "work_item_id": "standalone-work-item",
                "status": "accepted",
                "verdict": "accepted",
            },
        ),
        standalone_cli_operation(
            "close",
            {"work_item_id": "standalone-work-item", "status": "closed"},
        ),
        standalone_cli_operation(
            "stop",
            {"assignment_id": "standalone-assignment", "status": "stopped"},
        ),
        standalone_cli_operation(
            "archive",
            {"assignment_id": "standalone-assignment", "status": "archived"},
        ),
        standalone_cli_operation(
            "status",
            {"assignment_id": "standalone-assignment", "status": "archived"},
        ),
        standalone_cli_operation(
            "events",
            {
                "assignment_id": "standalone-assignment",
                "event_types": standalone_event_types,
                "next_cursor": len(standalone_event_types),
            },
        ),
    ]
    standalone_status = {
        "assignment_id": "standalone-assignment",
        "status": "active",
        "work_item_counts": {"closed": 1},
    }
    standalone_archived_status = {
        "assignment_id": "standalone-assignment",
        "status": "archived",
        "work_item_counts": {"closed": 1},
    }
    standalone = {
        "status": "passed",
        "server": {
            "health_http_status": 200,
            "service": "collaborative-development",
            "workflow_platform_required": False,
            "enterprise_denominator": False,
        },
        "cli_operations": standalone_cli_operations,
        "state_transition_transport": (
            "independent_cli_processes_over_loopback_http"
        ),
        "state_transition_service_substitution": False,
        "role_evidence_generation": (
            "production_workspace_tools_in_qualification_orchestrator"
        ),
        "cli_process_count": len(standalone_cli_operations),
        "executed_lifecycle": lifecycle,
        "review_verdicts": ["rework", "accepted"],
        "result_test_passes": [False, True],
        "result_handoffs": [
            {
                "result_id": result_id,
                "read_by_lilies_cli": True,
                "review_snapshot": snapshot,
                "review_prepare_replayed": True,
                "verdict": verdict,
            }
            for result_id, snapshot, verdict in zip(
                standalone_result_ids,
                standalone_snapshots,
                ("rework", "accepted"),
                strict=True,
            )
        ],
        "direct_api_operations": [
            {
                "method": "GET",
                "resource": "assignment_status",
                "http_status": 200,
                "semantic_response": standalone_status,
                "response_digest": canonical_digest(standalone_status),
            },
            {
                "method": "GET",
                "resource": "durable_assignment_events",
                "http_status": 200,
                "event_types": standalone_event_types,
                "next_cursor": len(standalone_event_types),
                "response_digest": canonical_digest(
                    {
                        "event_types": standalone_event_types,
                        "next_cursor": len(standalone_event_types),
                    }
                ),
            },
            {
                "method": "GET",
                "resource": "archived_assignment_status",
                "http_status": 200,
                "semantic_response": standalone_archived_status,
                "response_digest": canonical_digest(
                    standalone_archived_status
                ),
            },
        ],
        "successful_cli_commands": [
            item["command"] for item in standalone_cli_operations
        ],
        "final_assignment_status": "archived",
        "final_work_item_status": "closed",
        "source_repository_unchanged": True,
        "credential_transport": "separate_ephemeral_mode_0600_token_files",
        "token_material_persisted": False,
        "server_log_digest": canonical_digest("standalone-server-log"),
    }
    reusable: dict[str, object] = {
        "kind": "reusable_collaborative_development",
        "stage_task_id": "V04-13-T01G",
        "source_revision": source_revision,
        "enterprise_denominator": False,
        "status": "passed",
        "roles": ["lilies", "codex"],
        "authority_dimensions": [
            "workspace_paths",
            "argv",
            "network_hosts",
            "side_effects",
            "secret_refs",
            "budgets",
        ],
        "lifecycle": [item for item in lifecycle if item != "stop"],
        "executed_lifecycle": lifecycle,
        "manual": scenario("manual_dispatch"),
        "autonomous": scenario("autonomous"),
        "standalone_api_cli": standalone,
        "standalone_api_cli_digest": canonical_digest([standalone]),
        "workflow_application_required": False,
        "builder_required": False,
        "original_grants_unchanged": True,
    }
    reusable["evidence_digest"] = canonical_digest(reusable)

    live_assignment_id = "live-assignment"
    live_work_item_id = "live-work-item"
    live_history = dispatch_history(
        assignment_id=live_assignment_id,
        work_item_id=live_work_item_id,
        roles=["codex", "lilies"],
        mode="autonomous",
    )
    required_tools = ["git_diff", "process_run", "workspace_read"]
    tool_order = ["workspace_read", "git_diff", "process_run"]
    live_diff = "diff --git a/src/mathlib.py b/src/mathlib.py\n+return 4\n"
    frozen_acceptance = [
        "The implementation returns arithmetic addition, not subtraction.",
        "The frozen two-case tests/check.py command exits with status 0.",
        "Only src/mathlib.py differs from the frozen baseline.",
    ]
    codex_capability_digest = canonical_digest("codex-capability")
    lilies_capability_digest = canonical_digest("lilies-capability")
    effective_lilies_grant_digest = canonical_digest(
        "effective-lilies-review-grant"
    )
    live_record: dict[str, object] = {
        "schema_version": "2.0",
        "status": "passed",
        "stage_task_id": "V04-13-T01G",
        "source_revision": source_revision,
        "enterprise_denominator": False,
        "assignment_id": live_assignment_id,
        "assignment_status": "archived",
        "work_item_status": "closed",
        "codex_implementation": {
            "exit_code": 0,
            "result_id": "live-result",
            "changed_files": ["src/mathlib.py"],
            "diff": live_diff,
            "diff_digest": canonical_digest(live_diff),
            "broker_diff_digest": canonical_digest("broker-diff"),
            "inherited_full_environment": False,
            "other_role_grant_visible_to_model": False,
            "provider": "openai-codex-cli",
            "model": "test-codex-model",
            "command_count": 0,
            "file_or_external_tool_events": 0,
            "workspace_supplied_to_model_process": False,
            "outer_filesystem_sandbox": "macos-seatbelt",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "provider_proxy": {
                "transport": "loopback-connect-proxy",
                "allowed_hosts": [
                    "api.openai.com",
                    "auth.openai.com",
                    "chatgpt.com",
                ],
                "denied_connections": 0,
                "observations": [
                    {
                        "host": "chatgpt.com",
                        "port": 443,
                        "allowed": True,
                        "upstream_connected": True,
                    }
                ],
            },
            "source_read_digest": canonical_digest("source-read"),
            "proposal_digest": canonical_digest("proposal"),
            "trusted_patch_digest": canonical_digest("patch"),
            "test": {"exit_code": 0},
        },
        "lilies_review": {
            "review": {
                "verdict": "accepted",
                "acceptance_checks": [
                    {
                        "criterion": criterion,
                        "passed": True,
                        "evidence": f"evidence-{index}",
                    }
                    for index, criterion in enumerate(
                        frozen_acceptance,
                        start=1,
                    )
                ],
            },
            "frozen_acceptance": frozen_acceptance,
            "model_acceptance_checks": [
                {
                    "criterion": f"model criterion {index}",
                    "passed": True,
                    "evidence": f"evidence-{index}",
                }
                for index in range(1, 4)
            ],
            "provider": "test-provider",
            "model": "test-model",
            "usage": [{"input_tokens": 1, "output_tokens": 1}],
            "provider_cost_control": [
                {
                    "reservation_id": f"provider-reservation-{index}",
                    "provider_request_id": f"provider-request-{index}",
                    "worst_case_cost_usd": 1.0,
                    "actual_cost_usd": 0.01,
                    "settled": True,
                }
                for index in range(1, 4)
            ],
            "independent_snapshot": True,
            "review_snapshot_receipt_digest": canonical_digest(
                "review-snapshot"
            ),
            "review_snapshot_id": "lilies-review-snapshot",
            "successful_tool_names": required_tools,
            "mandatory_tool_names": required_tools,
            "denied_tool_calls": 0,
            "tool_calls": [
                {
                    "name": name,
                    "input_digest": canonical_digest(f"{name}:input"),
                    "result_digest": canonical_digest(f"{name}:result"),
                    "is_error": False,
                }
                for name in tool_order
            ],
        },
        "provider_cost_control": [
            {
                "reservation_id": "provider-reservation-codex",
                "provider_request_id": "provider-request-codex",
                "provider": "openai-codex-cli",
                "model": "test-codex-model",
                "worst_case_cost_usd": 1.0,
                "actual_cost_usd": 0.0,
                "input_tokens": 10,
                "output_tokens": 5,
                "provider_capability_digest": codex_capability_digest,
                "dispatch_grant_digest": live_history[0]["grant_digest"],
                "provider_hosts": [
                    "api.openai.com",
                    "auth.openai.com",
                    "chatgpt.com",
                ],
                "secret_refs": ["codex-cli-session"],
                "provider_side_effects": [
                    "workspace_write",
                    "process_execute",
                    "network_access",
                ],
                "credential_identity": "codex-cli-subscription",
                "authorization_evidence_digest": canonical_digest(
                    "codex-authorization"
                ),
                "receipt_evidence_digest": canonical_digest("codex-receipt"),
                "settled": True,
            },
            *[
                {
                    "reservation_id": (
                        f"provider-reservation-deepseek-{index}"
                    ),
                    "provider_request_id": (
                        f"provider-request-deepseek-{index}"
                    ),
                    "provider": "deepseek",
                    "model": "test-lilies-model",
                    "worst_case_cost_usd": 1.0,
                    "actual_cost_usd": 0.01,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "provider_capability_digest": lilies_capability_digest,
                    "dispatch_grant_digest": effective_lilies_grant_digest,
                    "provider_hosts": ["api.deepseek.com"],
                    "secret_refs": ["deepseek-runtime-credential"],
                    "provider_side_effects": [
                        "process_execute",
                        "network_access",
                    ],
                    "credential_identity": "deepseek-api-account",
                    "authorization_evidence_digest": canonical_digest(
                        f"deepseek-authorization-{index}"
                    ),
                    "receipt_evidence_digest": canonical_digest(
                        f"deepseek-receipt-{index}"
                    ),
                    "settled": True,
                }
                for index in range(1, 3)
            ],
        ],
        "software_fixture": {
            "kind": "unrelated_plain_python_git_repository",
            "source_unchanged": True,
        },
        "authority": {
            "codex": {
                "grant_digest": live_history[0]["grant_digest"],
                "workspace_id": "codex-workspace",
                "workspace": "<codex-workspace>",
                "baseline_commit": "a" * 40,
                "grant_revision": 1,
                "allowed_paths": ["src", "tests"],
                "allowed_argv": [["codex"], ["python", "check.py"]],
                "allowed_hosts": [
                    "api.openai.com",
                    "auth.openai.com",
                    "chatgpt.com",
                ],
                "allowed_side_effects": [
                    "workspace_write",
                    "process_execute",
                    "network_access",
                ],
                "secret_refs": ["codex-cli-session"],
            },
            "lilies": {
                "grant_digest": live_history[1]["grant_digest"],
                "workspace_id": "lilies-original-workspace",
                "workspace": "<lilies-workspace>",
                "baseline_commit": "a" * 40,
                "grant_revision": 1,
                "allowed_paths": ["src", "tests"],
                "allowed_argv": [["python", "check.py"]],
                "allowed_hosts": ["api.deepseek.com"],
                "allowed_side_effects": [
                    "process_execute",
                    "network_access",
                ],
                "secret_refs": ["deepseek-runtime-credential"],
            },
        },
        "effective_handler_authority": {
            "codex": {
                "grant_digest": live_history[0]["grant_digest"],
                "workspace_id": "codex-workspace",
                "workspace": "<codex-workspace>",
                "baseline_commit": "a" * 40,
                "grant_revision": 1,
                "allowed_paths": ["src", "tests"],
                "allowed_argv": [["codex"], ["python", "check.py"]],
                "allowed_hosts": [
                    "api.openai.com",
                    "auth.openai.com",
                    "chatgpt.com",
                ],
                "allowed_side_effects": [
                    "workspace_write",
                    "process_execute",
                    "network_access",
                ],
                "secret_refs": ["codex-cli-session"],
                "provider_capability_digest": codex_capability_digest,
                "provider_dispatch_grant_digest": live_history[0][
                    "grant_digest"
                ],
            },
            "lilies": {
                "grant_digest": effective_lilies_grant_digest,
                "workspace_id": "lilies-review-snapshot",
                "workspace": "<lilies-workspace>",
                "baseline_commit": "a" * 40,
                "grant_revision": 1,
                "allowed_paths": ["src", "tests"],
                "allowed_argv": [["python", "check.py"]],
                "allowed_hosts": ["api.deepseek.com"],
                "allowed_side_effects": [
                    "process_execute",
                    "network_access",
                ],
                "secret_refs": ["deepseek-runtime-credential"],
                "provider_capability_digest": lilies_capability_digest,
                "provider_dispatch_grant_digest": (
                    effective_lilies_grant_digest
                ),
            },
        },
        "budget_ledger": {
            "tool_calls": 8,
            "commands": 5,
            "completed_records": 8,
            "by_role": {
                "codex": {"tool_calls": 5, "commands": 3},
                "lilies": {"tool_calls": 3, "commands": 2},
            },
            "provider_reservations": 3,
            "provider_settled": 3,
            "within_assignment_budget": True,
        },
        "actual_lifecycle": {
            "events": event_types,
            "required_events_present": True,
            "dispatch_history_restart_equal": True,
            "original_grants_unchanged": True,
            "independent_review_snapshot": True,
            "dispatch_history": live_history,
        },
    }
    live_record["evidence_digest"] = canonical_digest(live_record)
    live: dict[str, object] = {
        "kind": "bounded_live_lilies_codex_handoff",
        "stage_task_id": "V04-13-T01G",
        "source_revision": source_revision,
        "enterprise_denominator": False,
        "status": "passed",
        "record": live_record,
    }
    live["evidence_digest"] = canonical_digest(live)

    durable_assignment_id = "durable-assignment"
    durable_history = dispatch_history(
        assignment_id=durable_assignment_id,
        work_item_id="durable-work-item",
        roles=["codex", "lilies", "codex", "lilies"],
        mode="autonomous",
    )
    durable_events = [{"event_type": event_type} for event_type in event_types]
    durable_tool_usage = tool_usage_history("durable")
    durable_record: dict[str, object] = {
        "status": "passed",
        "source_revision": source_revision,
        "assignment_id": durable_assignment_id,
        "execution_mode": "autonomous",
        "result_ids": ["durable-result-1", "durable-result-2"],
        "review_ids": ["durable-review-1", "durable-review-2"],
        "history": durable_history,
        "store_event_history": durable_events,
        "tool_usage_history": durable_tool_usage,
        "restart_history_equal": True,
        "restart_store_history_equal": True,
        "restart_tool_usage_equal": True,
        "original_grants_unchanged": True,
        "source_repository_unchanged": True,
        "final_assignment_status": "archived",
        "final_work_item_status": "closed",
        "history_digest": canonical_digest(durable_history),
        "store_history_digest": canonical_digest(durable_events),
        "tool_usage_digest": canonical_digest(durable_tool_usage),
    }
    durable_record["evidence_digest"] = canonical_digest(durable_record)
    durable: dict[str, object] = {
        "kind": "durable_autonomous_dispatch_history",
        "stage_task_id": "V04-13-T01G",
        "source_revision": source_revision,
        "enterprise_denominator": False,
        "status": "passed",
        "record": durable_record,
    }
    durable["evidence_digest"] = canonical_digest(durable)
    return [reusable, live, durable]


def _passed_bundle(**overrides: object) -> object:
    arguments: dict[str, object] = {
        "source_revision": "qualification-test-revision",
        "generated_at": FIXED_TIME,
        "api_result": _passed_surface("formal-api"),
        "browser_result": _passed_surface("formal-browser"),
        "development_api_result": _passed_surface("development-api"),
        "fault_injection": build_fault_injection_qualification(
            _fault_record_payloads()
        ),
        "extra_evidence": _required_extra_evidence(),
    }
    arguments.update(overrides)
    return build_pipeline_qualification_bundle(
        _passed_command_results(),
        **arguments,
    )


def test_pipeline_catalog_is_exactly_pipe_q01_through_q28() -> None:
    assert tuple(item.case_id for item in PIPELINE_QUALIFICATION_CASES) == (
        PIPELINE_QUALIFICATION_CASE_IDS
    )
    assert len(PIPELINE_QUALIFICATION_CASES) == 28
    assert all(item.mandatory for item in PIPELINE_QUALIFICATION_CASES)
    assert all(item.command_ids for item in PIPELINE_QUALIFICATION_CASES)

    commands = command_specs_by_id()
    assert len(commands) == len(PIPELINE_QUALIFICATION_COMMANDS)
    assert all(
        command_id in commands
        for case in PIPELINE_QUALIFICATION_CASES
        for command_id in case.command_ids
    )
    assert {item.lane for item in FAULT_INJECTION_LANES} == {
        "reconnect",
        "idempotency",
        "lease",
        "concurrency",
    }


def test_pipeline_bundle_records_every_case_surface_command_and_iteration() -> None:
    bundle = _passed_bundle()

    assert bundle.status == "passed"
    assert bundle.enterprise_denominator is False
    assert [item.case_id for item in bundle.cases] == list(
        PIPELINE_QUALIFICATION_CASE_IDS
    )
    assert all(item.xfail is False for item in bundle.cases)
    assert all(item.status == "passed" for item in bundle.cases)
    assert all(item.api_result.status == "passed" for item in bundle.cases)
    assert all(
        item.browser_result.status == "passed"
        for item in bundle.cases[:23]
    )
    assert all(
        item.browser_result.status == "not_applicable"
        for item in bundle.cases[23:]
    )
    assert all(item.evidence_digest.startswith("sha256:") for item in bundle.cases)
    assert bundle.summary.model_dump() == {
        "total": 28,
        "mandatory": 28,
        "passed": 28,
        "failed": 0,
        "not_run": 0,
        "blocked_by_environment": 0,
        "mandatory_xfail": 0,
    }

    fault = bundle.fault_injection
    assert fault.required_iterations_per_lane == 100
    assert fault.total_iteration_records == 400
    assert len(fault.lanes) == 4
    for lane in fault.lanes:
        assert lane.verified_iterations == PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS
        assert len(lane.iterations) == PIPELINE_QUALIFICATION_REQUIRED_ITERATIONS
        assert [item.iteration for item in lane.iterations] == list(range(1, 101))
        assert all(item.command and item.output_digest for item in lane.iterations)
        assert all(item.record_digest.startswith("sha256:") for item in lane.iterations)
        assert all(
            count == 0
            for key, count in lane.counters.items()
            if key in {"lost_messages", "duplicate_deliveries", "duplicate_side_effects",
                       "payload_drift_mutations", "stale_owner_mutations"}
        )

    rebuilt = bundle.__class__.model_validate(bundle.model_dump(mode="json"))
    assert rebuilt == bundle


def test_browser_environment_debt_does_not_erase_actual_q_results() -> None:
    bundle = _passed_bundle(browser_result=_blocked_browser_surface())

    assert bundle.status == "passed"
    assert all(item.status == "passed" for item in bundle.cases)
    assert all(
        item.browser_result.status == "blocked_by_environment"
        for item in bundle.cases[:23]
    )
    browser_result = bundle.cases[0].browser_result
    assert browser_result.claim_ceiling is not None
    assert browser_result.recheck_trigger is not None
    assert browser_result.digest == canonical_digest(browser_result.observations)
    assert bundle.summary.blocked_by_environment == 0


def test_pipeline_bundle_cannot_omit_formal_browser_debt() -> None:
    with pytest.raises(ValueError, match="formal browser result"):
        _passed_bundle(browser_result=None)


def test_source_revision_is_content_bound_and_commit_independent(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "platform" / "backend" / "src" / "sample.py"
    documentation = repository / "docs" / "evidence.md"
    source.parent.mkdir(parents=True)
    documentation.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    documentation.write_text("first evidence\n", encoding="utf-8")

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Qualification Test",
        "-c",
        "user.email=qualification@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    initial = qualification_source_revision(repository)
    assert initial.startswith("sha256:")

    documentation.write_text("changed evidence only\n", encoding="utf-8")
    assert qualification_source_revision(repository) == initial
    git("add", "docs/evidence.md")
    git(
        "-c",
        "user.name=Qualification Test",
        "-c",
        "user.email=qualification@example.invalid",
        "commit",
        "-qm",
        "evidence only",
    )
    assert qualification_source_revision(repository) == initial

    source.write_text("VALUE = 2\n", encoding="utf-8")
    changed_content = qualification_source_revision(repository)
    assert changed_content != initial
    source.chmod(source.stat().st_mode | stat.S_IXUSR)
    changed_mode = qualification_source_revision(repository)
    assert changed_mode != changed_content

    untracked = repository / "scripts" / "new_tool.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("print('new')\n", encoding="utf-8")
    changed_untracked = qualification_source_revision(repository)
    assert changed_untracked != changed_mode

    renamed = source.with_name("renamed.py")
    source.rename(renamed)
    changed_path = qualification_source_revision(repository)
    assert changed_path != changed_untracked

    link = source.with_name("sample-link")
    link.symlink_to(renamed.name)
    assert qualification_source_revision(repository) != changed_path


def test_source_revision_ignores_untracked_root_config_but_binds_it_when_tracked(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "platform" / "backend" / "src" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "platform/backend/src/sample.py")
    git(
        "-c",
        "user.name=Qualification Test",
        "-c",
        "user.email=qualification@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    initial = qualification_source_revision(repository)

    lockfile = repository / "uv.lock"
    lockfile.write_text("version = 1\n", encoding="utf-8")
    assert qualification_source_revision(repository) == initial

    git("add", "uv.lock")
    tracked_lockfile = qualification_source_revision(repository)
    assert tracked_lockfile != initial

    git(
        "-c",
        "user.name=Qualification Test",
        "-c",
        "user.email=qualification@example.invalid",
        "commit",
        "-qm",
        "track lockfile",
    )
    assert qualification_source_revision(repository) == tracked_lockfile

    lockfile.write_text("version = 2\n", encoding="utf-8")
    assert qualification_source_revision(repository) != tracked_lockfile


def test_pipeline_bundle_rejects_missing_or_changed_fixed_commands() -> None:
    commands = _passed_command_results()
    with pytest.raises(ValueError, match="fixed catalog"):
        build_pipeline_qualification_bundle(
            commands[:-1],
            source_revision="qualification-test-missing-command",
            generated_at=FIXED_TIME,
        )

    changed = commands[0].model_copy(
        update={"argv": ["pytest", "not-the-locked-command"]}
    )
    with pytest.raises(ValueError, match="catalog binding"):
        build_pipeline_qualification_bundle(
            [changed, *commands[1:]],
            source_revision="qualification-test-changed-command",
            generated_at=FIXED_TIME,
        )


def test_surface_and_pytest_passes_require_bound_actual_observations() -> None:
    observations = [{"probe": "health", "actual_status": 200}]
    with pytest.raises(ValidationError, match="does not bind"):
        QualificationSurfaceResult(
            status="passed",
            source="live-http",
            summary="one actual probe",
            observations=observations,
            digest="sha256:" + ("0" * 64),
        )

    with pytest.raises(ValidationError, match="claim ceiling"):
        QualificationSurfaceResult(
            status="blocked_by_environment",
            source="browser-runtime",
            summary="No browser available.",
            observations=[{"available_browsers": []}],
            digest=canonical_digest([{"available_browsers": []}]),
        )

    with pytest.raises(ValidationError, match="skip or xfail"):
        QualificationCommandResult(
            command_id="mandatory-xfail-probe",
            case_ids=["PIPE-Q01"],
            argv=[".venv/bin/python", "-m", "pytest", "-q", "tests/example.py"],
            status="passed",
            exit_code=0,
            duration_ms=1,
            output_digest=canonical_digest("pytest-output"),
            pytest_outcomes=QualificationPytestOutcomes(
                collected=1,
                passed=0,
                failed=0,
                errors=0,
                skipped=0,
                xfailed=1,
                xpassed=0,
            ),
        )


@pytest.mark.parametrize(
    ("decorator", "assertion", "outcome"),
    [
        (
            "@pytest.mark.skip(reason='qualification must reject this')",
            "assert True",
            "skipped",
        ),
        (
            "@pytest.mark.xfail(reason='qualification must reject this')",
            "assert False",
            "xfailed",
        ),
    ],
)
def test_mandatory_runner_rejects_real_pytest_skip_or_xfail(
    tmp_path: Path,
    decorator: str,
    assertion: str,
    outcome: str,
) -> None:
    guarded_test = tmp_path / "test_mandatory_guard.py"
    guarded_test.write_text(
        "import pytest\n\n"
        f"{decorator}\n"
        "def test_mandatory_behavior():\n"
        f"    {assertion}\n",
        encoding="utf-8",
    )
    result_path = tmp_path / "pytest-outcomes.json"

    result = _run_command(
        command_id="mandatory-skip-probe",
        case_ids=("PIPE-Q01",),
        argv=(sys.executable, "-m", "pytest", "-q", str(guarded_test)),
        timeout_seconds=30,
        environment={},
        pytest_result_path=result_path,
    )

    assert result.status == "failed"
    assert result.exit_code == 86
    assert result.pytest_outcomes is not None
    assert result.pytest_outcomes.collected == 1
    assert getattr(result.pytest_outcomes, outcome) == 1
    assert result_path.stat().st_mode & 0o777 == 0o600


def test_extra_evidence_rejects_stale_or_truncated_self_hashed_records() -> None:
    stale = deepcopy(_required_extra_evidence())
    stale[0]["source_revision"] = "different-revision"
    stale[0]["evidence_digest"] = canonical_digest(
        {
            key: value
            for key, value in stale[0].items()
            if key != "evidence_digest"
        }
    )
    with pytest.raises(ValueError, match="source revision is stale"):
        _passed_bundle(extra_evidence=stale)

    truncated = deepcopy(_required_extra_evidence())
    reusable = truncated[0]
    reusable.pop("manual")
    reusable["evidence_digest"] = canonical_digest(
        {
            key: value
            for key, value in reusable.items()
            if key != "evidence_digest"
        }
    )
    with pytest.raises(ValueError, match="manual_dispatch"):
        _passed_bundle(extra_evidence=truncated)

    short_history = deepcopy(_required_extra_evidence())
    durable = short_history[2]
    record = durable["record"]
    assert isinstance(record, dict)
    record["history"] = record["history"][:1]  # type: ignore[index]
    record["history_digest"] = canonical_digest(record["history"])
    record["evidence_digest"] = canonical_digest(
        {
            key: value
            for key, value in record.items()
            if key != "evidence_digest"
        }
    )
    durable["evidence_digest"] = canonical_digest(
        {
            key: value
            for key, value in durable.items()
            if key != "evidence_digest"
        }
    )
    with pytest.raises(ValueError, match="exact length"):
        _passed_bundle(extra_evidence=short_history)

    short_usage = deepcopy(_required_extra_evidence())
    durable = short_usage[2]
    record = durable["record"]
    assert isinstance(record, dict)
    usage_history = record["tool_usage_history"]
    assert isinstance(usage_history, list)
    record["tool_usage_history"] = usage_history[:-1]
    record["tool_usage_digest"] = canonical_digest(
        record["tool_usage_history"]
    )
    record["evidence_digest"] = canonical_digest(
        {
            key: value
            for key, value in record.items()
            if key != "evidence_digest"
        }
    )
    durable["evidence_digest"] = canonical_digest(
        {
            key: value
            for key, value in durable.items()
            if key != "evidence_digest"
        }
    )
    with pytest.raises(ValueError, match="exactly seven"):
        _passed_bundle(extra_evidence=short_usage)


def test_standalone_q28_rejects_old_weak_or_event_digest_fabrication() -> None:
    def rebind(evidence: list[dict[str, object]]) -> None:
        reusable = evidence[0]
        standalone = reusable["standalone_api_cli"]
        reusable["standalone_api_cli_digest"] = canonical_digest([standalone])
        reusable["evidence_digest"] = canonical_digest(
            {
                key: value
                for key, value in reusable.items()
                if key != "evidence_digest"
            }
        )

    old_weak = deepcopy(_required_extra_evidence())
    old_weak[0]["standalone_api_cli"] = {
        "status": "passed",
        "server": {
            "health_http_status": 200,
            "workflow_platform_required": False,
            "enterprise_denominator": False,
        },
        "final_assignment_status": "archived",
        "token_material_persisted": False,
    }
    rebind(old_weak)
    with pytest.raises(ValueError, match="standalone Q28"):
        _passed_bundle(extra_evidence=old_weak)

    missing_terminal_event = deepcopy(_required_extra_evidence())
    standalone = missing_terminal_event[0]["standalone_api_cli"]
    assert isinstance(standalone, dict)
    operations = standalone["direct_api_operations"]
    assert isinstance(operations, list)
    event_operation = next(
        item
        for item in operations
        if item["resource"] == "durable_assignment_events"
    )
    event_operation["event_types"] = [
        event_type
        for event_type in event_operation["event_types"]
        if event_type != "work_item.accepted"
    ]
    event_operation["response_digest"] = canonical_digest(
        {
            "event_types": event_operation["event_types"],
            "next_cursor": event_operation["next_cursor"],
        }
    )
    rebind(missing_terminal_event)
    with pytest.raises(ValueError, match="terminal event"):
        _passed_bundle(extra_evidence=missing_terminal_event)

    forged_event_digest = deepcopy(_required_extra_evidence())
    standalone = forged_event_digest[0]["standalone_api_cli"]
    assert isinstance(standalone, dict)
    operations = standalone["direct_api_operations"]
    assert isinstance(operations, list)
    event_operation = next(
        item
        for item in operations
        if item["resource"] == "durable_assignment_events"
    )
    event_operation["response_digest"] = canonical_digest("forged-events")
    rebind(forged_event_digest)
    with pytest.raises(ValueError, match="digest binding"):
        _passed_bundle(extra_evidence=forged_event_digest)


def test_fault_bundle_rejects_counter_command_and_digest_fabrication() -> None:
    records = _fault_record_payloads()

    negative = [dict(item) for item in records]
    negative[0] = {
        **negative[0],
        "counters": {
            **negative[0]["counters"],  # type: ignore[dict-item]
            "lost_messages": -1,
        },
    }
    unsigned = {
        key: value for key, value in negative[0].items() if key != "record_digest"
    }
    negative[0]["record_digest"] = canonical_digest(unsigned)
    with pytest.raises(ValueError, match="non-negative"):
        build_fault_injection_qualification(negative)

    changed_command = [dict(item) for item in records]
    changed_command[0] = {
        **changed_command[0],
        "command": ["pytest", "different"],
    }
    unsigned = {
        key: value
        for key, value in changed_command[0].items()
        if key != "record_digest"
    }
    changed_command[0]["record_digest"] = canonical_digest(unsigned)
    with pytest.raises(ValueError, match="argv changed"):
        build_fault_injection_qualification(changed_command)

    duplicate_output = [dict(item) for item in records]
    duplicate_output[1] = {
        **duplicate_output[1],
        "output_digest": duplicate_output[0]["output_digest"],
    }
    unsigned = {
        key: value
        for key, value in duplicate_output[1].items()
        if key != "record_digest"
    }
    duplicate_output[1]["record_digest"] = canonical_digest(unsigned)
    with pytest.raises(ValueError, match="output evidence"):
        build_fault_injection_qualification(duplicate_output)


def test_live_handoff_status_is_covered_by_record_and_wrapper_digests() -> None:
    evidence = _required_extra_evidence()
    live = dict(evidence[1])
    record = dict(live["record"])  # type: ignore[arg-type]
    record["status"] = "failed"
    record["evidence_digest"] = canonical_digest(
        {key: value for key, value in record.items() if key != "evidence_digest"}
    )
    record["status"] = "passed"
    live["record"] = record
    live["evidence_digest"] = canonical_digest(
        {key: value for key, value in live.items() if key != "evidence_digest"}
    )
    evidence[1] = live
    with pytest.raises(ValueError, match="record digest changed"):
        _passed_bundle(extra_evidence=evidence)


@pytest.mark.asyncio
async def test_pipe_q06_incomplete_report_cannot_be_approved(tmp_path: Path) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(store=store, enabled=True, now=lambda: FIXED_TIME)
    lilies, user, developer = _principals(channel)
    channel_id = UUID(str(channel["channel_id"]))
    report_id = uuid4()
    incomplete = _report_payload(report_id)
    incomplete["evidence_refs"] = []
    submitted = await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="pipe-q06-submit-incomplete-0001",
            expected_channel_revision=int(channel["revision"]),
            report=CollaborationReportPayload.model_validate(incomplete),
        ),
    )
    assert (submitted["status"], submitted["route"]) == (
        "needs_more_evidence",
        "capability_approval",
    )

    with pytest.raises(CollaborationConflict) as rejected:
        await service.decide_report(
            principal=user,
            report_id=report_id,
            request=ApprovalDecisionRequest(
                idempotency_key="pipe-q06-approve-incomplete-0001",
                expected_report_revision=int(submitted["revision"]),
                decision="approve",
            ),
        )
    assert rejected.value.code == "report_not_awaiting_review"
    inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route=None,
    )
    assert inbox["reports"] == []
    assert (await store.get_report(report_id))["status"] == "needs_more_evidence"


@pytest.mark.asyncio
async def test_pipe_q07_auto_forward_routes_report_but_not_permission(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(store=store, enabled=True, now=lambda: FIXED_TIME)
    lilies, user, developer = _principals(channel)
    channel_id = UUID(str(channel["channel_id"]))
    updated = await service.set_channel_approval_mode(
        principal=user,
        channel_id=channel_id,
        request=ChannelSettingsRequest(
            idempotency_key="pipe-q07-auto-forward-setting-0001",
            expected_channel_revision=int(channel["revision"]),
            approval_mode="auto_forward",
            confirmed=True,
        ),
    )
    report_id = uuid4()
    submitted = await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="pipe-q07-auto-forward-report-0001",
            expected_channel_revision=int(updated["revision"]),
            report=CollaborationReportPayload.model_validate(_report_payload(report_id)),
        ),
    )
    assert (submitted["status"], submitted["route"]) == (
        "approved_for_codex",
        "developer",
    )
    inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route=None,
    )
    assert [item["report_id"] for item in inbox["reports"]] == [str(report_id)]

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_approvals WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_outbox "
            "WHERE destination='developer_inbox' AND payload_json LIKE ?",
            (f'%{report_id}%',),
        ).fetchone() == (1,)

    permission_status = _studio_derived_status(
        channel=CollaborationChannel.model_validate(
            await store.get_channel(channel_id)
        ),
        reports=[],
        active_leases=[],
        claims=[],
        context={
            "assignment": {
                "connection_status": "connected",
                "daemon_status": "waiting_permission",
                "status": "running",
            }
        },
        unread_count=0,
    )
    assert permission_status["current_block"]["code"] == "runtime_permission"
    assert permission_status["owner"]["role"] == "user"
    assert permission_status["next_action"]["code"] == "resolve_permission"


@pytest.mark.asyncio
async def test_pipe_q08_new_task_does_not_inherit_auto_forward(
    tmp_path: Path,
) -> None:
    store, _, first_channel = await _store_with_channel(tmp_path)
    operation_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    service = CollaborationService(
        store=store,
        enabled=True,
        now=lambda: operation_time,
    )
    _, user, _ = _principals(first_channel)
    first_id = UUID(str(first_channel["channel_id"]))
    updated = await service.set_channel_approval_mode(
        principal=user,
        channel_id=first_id,
        request=ChannelSettingsRequest(
            idempotency_key="pipe-q08-first-auto-forward-0001",
            expected_channel_revision=int(first_channel["revision"]),
            approval_mode="auto_forward",
            confirmed=True,
        ),
    )
    assert updated["approval_mode"] == "auto_forward"

    second = await service.create_formal_channel(
        assignment_mode=AssignmentMode.formal_experiment,
        task_id="EXP-LILIES-PIPE-Q08-NEXT",
        task_revision=1,
        assignment_id=uuid4(),
        lilies_session_id=uuid4(),
        application_ids=[uuid4()],
        collaboration_enabled=True,
        user_notified=True,
        expires_at=operation_time + timedelta(hours=2),
        retention_until=operation_time + timedelta(days=30),
        idempotency_key="pipe-q08-second-channel-0001",
        max_report_evidence_rounds=3,
    )
    assert second.channel.approval_mode.value == "manual"
    assert (await store.get_channel(first_id))["approval_mode"] == "auto_forward"
    assert (
        await store.get_channel(second.channel.channel_id)
    )["approval_mode"] == "manual"


@pytest.mark.asyncio
async def test_pipeline_fault_concurrency_runs_one_hundred_serialized_iterations(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(str(channel["channel_id"]))
    message_ids: list[str] = []

    for iteration in range(1, 101):
        request = _control_message(
            channel_id,
            f"pipe-concurrency-{iteration:03d}",
        )
        writers = [CollaborationStore(database) for _ in range(8)]
        results = await asyncio.gather(
            *(writer.append_message(request) for writer in writers)
        )
        assert all(item == results[0] for item in results)
        assert int(results[0]["seq"]) == iteration + 1
        message_ids.append(str(results[0]["message_id"]))
        record_fault_iteration(
            lane="concurrency",
            iteration=iteration,
            command_id="q05-concurrent-single-side-effect",
            command=command_specs_by_id()["q05-concurrent-single-side-effect"].argv,
            counters={
                "attempted_iterations": 1,
                "serialized_iterations": 1,
                "lost_messages": 0,
                "duplicate_side_effects": 0,
            },
            output={
                "message_id": results[0]["message_id"],
                "seq": results[0]["seq"],
                "concurrent_writers": len(results),
                "identical_results": all(item == results[0] for item in results),
            },
        )

    assert len(message_ids) == len(set(message_ids)) == 100
    persisted = await store.list_messages(
        channel_id,
        after_seq=0,
        limit=1_000,
    )
    assert len(persisted) == 101
    assert [int(item["seq"]) for item in persisted] == list(range(1, 102))
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (101,)
        assert connection.execute(
            "SELECT COUNT(DISTINCT idempotency_key) "
            "FROM collaboration_messages WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (101,)


@pytest.mark.asyncio
async def test_pipe_q14_bare_ok_is_rejected_without_waking_lilies(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(str(channel["channel_id"]))
    before = await store.list_messages(channel_id, after_seq=0, limit=100)
    with pytest.raises(ValidationError, match="substantive generic changes"):
        DeveloperResponse.model_validate(
            developer_response_payload(changes=["OK"])
        )
    after = await store.list_messages(channel_id, after_seq=0, limit=100)
    assert after == before
    assert [item["message_type"] for item in after] == ["control"]


@pytest.mark.asyncio
async def test_pipe_q16_task_gap_routes_to_author_without_user_question(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(store=store, enabled=True, now=lambda: FIXED_TIME)
    lilies, user, developer = _principals(channel)
    channel_id = UUID(str(channel["channel_id"]))
    report_id = uuid4()
    payload = _report_payload(report_id)
    payload.update(
        {
            "category": "task_spec_gap",
            "summary": "The frozen task omits a required business decision.",
            "actual": "No public requirement defines the required decision.",
            "requested_outcome": "Amend the immutable task package.",
        }
    )
    submitted = await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="pipe-q16-task-gap-0001",
            expected_channel_revision=int(channel["revision"]),
            report=CollaborationReportPayload.model_validate(payload),
        ),
    )
    assert (submitted["status"], submitted["route"]) == (
        "routed_to_task_author",
        "task_author",
    )
    inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route="task_author",
    )
    assert [item["report_id"] for item in inbox["reports"]] == [str(report_id)]
    assert inbox["pending_user_action"] is False

    with pytest.raises(CollaborationConflict) as rejected:
        await service.decide_report(
            principal=user,
            report_id=report_id,
            request=ApprovalDecisionRequest(
                idempotency_key="pipe-q16-user-decision-denied-0001",
                expected_report_revision=int(submitted["revision"]),
                decision="approve",
            ),
        )
    assert rejected.value.code == "report_not_approvable"


@pytest.mark.asyncio
async def test_pipe_q18_permission_denial_stays_outside_capability_reports(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(str(channel["channel_id"]))
    status = _studio_derived_status(
        channel=CollaborationChannel.model_validate(
            await store.get_channel(channel_id)
        ),
        reports=[],
        active_leases=[],
        claims=[],
        context={
            "assignment": {
                "connection_status": "connected",
                "daemon_status": "waiting_permission",
                "status": "running",
            }
        },
        unread_count=0,
    )
    assert status["current_block"]["code"] == "runtime_permission"
    assert status["owner"]["role"] == "user"
    assert status["next_action"]["code"] == "resolve_permission"

    invalid_report = _report_payload(uuid4())
    invalid_report["category"] = "permission_request"
    with pytest.raises(ValidationError, match="category"):
        CollaborationReportPayload.model_validate(invalid_report)
    assert await store.list_reports(
        channel_id=channel_id,
        after=0,
        limit=100,
    ) == []


@pytest.mark.asyncio
async def test_pipe_q21_closed_channel_rejects_writes_but_keeps_history(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(store=store, enabled=True, now=lambda: FIXED_TIME)
    lilies, user, _ = _principals(channel)
    channel_id = UUID(str(channel["channel_id"]))
    closed = await service.close_channel(
        principal=user,
        channel_id=channel_id,
        request=ChannelCloseRequest(
            idempotency_key="pipe-q21-close-0001",
            expected_channel_revision=int(channel["revision"]),
            reason="Close the qualification fixture after preserving history.",
        ),
    )
    assert closed["status"] == "closed"

    report_id = uuid4()
    with pytest.raises(CollaborationClosed) as rejected:
        await service.submit_report(
            principal=lilies,
            channel_id=channel_id,
            request=ReportSubmitRequest(
                idempotency_key="pipe-q21-write-after-close-0001",
                expected_channel_revision=int(closed["revision"]),
                report=CollaborationReportPayload.model_validate(
                    _report_payload(report_id)
                ),
            ),
        )
    assert rejected.value.status_code == 410
    history = await service.list_events(
        principal=user,
        channel_id=channel_id,
        after=0,
        limit=100,
    )
    assert [item["message_type"] for item in history] == ["control", "control"]
    assert history[-1]["payload"]["kind"] == "channel_closed"
    with pytest.raises(StorageNotFound):
        await store.get_report(report_id)
