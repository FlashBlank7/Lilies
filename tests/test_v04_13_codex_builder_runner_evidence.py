from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest

from scripts import run_v04_13_codex_builder as runner


def _digest(value: bytes = b"evidence") -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_private(path: Path, value: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(0o600)
    return _digest(value)


def _rollout_budget(
    limit_tokens: int = runner.CODEX_ROLLOUT_TOKEN_LIMIT,
) -> dict[str, object]:
    return {
        "enforcement": "codex_cli_rollout_budget",
        "limit_tokens": limit_tokens,
        "maximum_allowed_limit_tokens": (
            runner.MAX_CODEX_ROLLOUT_TOKEN_LIMIT
        ),
        "continues_on_exact_thread_resume": False,
        "token_weights": {
            "sampling": 1.0,
            "prefill": 1.0,
        },
        "multi_agent_enabled": False,
        "config_supported": {
            "rollout_budget": True,
            "multi_agent": False,
            "multi_agent_v2": False,
        },
    }


def _reported_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
) -> dict[str, object]:
    return {
        "receipt_status": "reported",
        "usage_receipt_count": 1,
        "model_call_count": 1,
        "model_call_count_support": (
            "inferred_from_codex_turn_completed_events"
        ),
        "unknown_usage_model_calls": 0,
        "fields": {
            "input_tokens": {
                "support": "reported",
                "value": input_tokens,
            },
            "output_tokens": {
                "support": "reported",
                "value": output_tokens,
            },
            "cache_read_input_tokens": {
                "support": "reported",
                "value": cache_read_input_tokens,
            },
            "cache_creation_input_tokens": {"support": "not_reported"},
            "reasoning_tokens": {"support": "not_reported"},
        },
    }


def test_owner_urls_are_real_owner_detail_export_and_information_flow() -> None:
    urls = runner._owner_observation_urls(
        platform_url="http://127.0.0.1:18120",
        owner_ui_url="http://127.0.0.1:3003",
        application_id=str(uuid4()),
        assignment_id=str(uuid4()),
        channel_id=str(uuid4()),
    )

    assert set(urls) == {
        "application_api_url",
        "application_studio_url",
        "collaboration_detail_api_url",
        "collaboration_export_api_url",
        "collaboration_event_stream_api_url",
        "collaboration_information_flow_studio_url",
    }
    assert urls["collaboration_detail_api_url"].endswith(
        urls["collaboration_information_flow_studio_url"].split("channel=", 1)[1]
    )
    assert urls["collaboration_export_api_url"].endswith("/export")
    assert "/developer/collaboration?channel=" in urls[
        "collaboration_information_flow_studio_url"
    ]
    assert urls["collaboration_event_stream_api_url"].endswith("/events?after=0")
    assert all("/api/v1/collaboration/" not in value for value in urls.values())


def test_usage_receipt_preserves_per_field_support_and_missing_is_not_zero(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "usage.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 13,
                            "output_tokens": 5,
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    receipt = runner._usage_accounting_from_transcript(transcript)

    assert receipt["receipt_status"] == "reported"
    assert receipt["model_call_count"] == 1
    assert (
        receipt["model_call_count_support"]
        == "inferred_from_codex_turn_completed_events"
    )
    assert receipt["fields"]["input_tokens"] == {
        "support": "reported",
        "value": 13,
    }
    assert receipt["fields"]["output_tokens"] == {
        "support": "reported",
        "value": 5,
    }
    assert receipt["fields"]["reasoning_tokens"] == {
        "support": "not_reported"
    }

    no_receipt = tmp_path / "no-usage.jsonl"
    no_receipt.write_text(
        json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
        encoding="utf-8",
    )
    missing = runner._usage_accounting_from_transcript(no_receipt)
    assert missing["receipt_status"] == "not_reported"
    assert missing["model_call_count"] is None
    assert all(
        field == {"support": "not_reported"}
        for field in missing["fields"].values()
    )


@pytest.mark.asyncio
async def test_missing_usage_never_writes_zero_model_usage_and_exit_zero_is_paused() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Harness:
        async def record_usage(self, _task_id: str, _kind: str, **kwargs: object) -> None:
            calls.append(("usage", dict(kwargs)))

        async def record_model_usage(
            self,
            _task_id: str,
            _usage: object,
            **kwargs: object,
        ) -> None:
            calls.append(("model_usage", dict(kwargs)))

        async def finish_task(
            self,
            _task_id: str,
            **kwargs: object,
        ) -> None:
            calls.append(("finish", dict(kwargs)))

    receipt = {
        "application_id": str(uuid4()),
        "assignment_id": str(uuid4()),
        "session_id": str(uuid4()),
    }
    await runner._finish_codex_harness_task(
        Namespace(harness=Harness()),
        task_id="external-process",
        receipt=receipt,
        model="gpt-test",
        result={
            "transcript_digest": _digest(),
            "process_execution_status": "completed",
            "usage_accounting": {
                **runner._usage_accounting(None),
                "model_call_count": 1,
                "model_call_count_support": "inferred_from_codex_process_receipt",
                "unknown_usage_model_calls": 1,
            },
        },
        succeeded=True,
    )

    assert [name for name, _ in calls] == ["usage", "finish"]
    finish = calls[1][1]
    assert finish["status"] == "paused"
    assert finish["metadata"]["process_execution_status"] == "completed"
    assert finish["metadata"]["business_outcome"] == "unknown"
    assert finish["metadata"]["project_success"] is False
    assert finish["metadata"]["success_aggregation_eligible"] is False
    assert finish["metadata"]["usage_receipt_status"] == "not_reported"
    assert finish["metadata"]["model_call_count"] == 1
    assert (
        finish["metadata"]["model_call_count_support"]
        == "inferred_from_codex_process_receipt"
    )
    assert finish["metadata"]["unknown_usage_model_calls"] == 1


@pytest.mark.asyncio
async def test_harness_receipt_carries_frozen_turn_budget_without_fake_usd_limit() -> None:
    captured: dict[str, object] = {}

    class Harness:
        async def start_task(
            self,
            task_id: str,
            **kwargs: object,
        ) -> None:
            captured.update({"task_id": task_id, **kwargs})

    receipt = {
        "application_id": str(uuid4()),
        "assignment_id": str(uuid4()),
        "session_id": str(uuid4()),
    }
    await runner._start_codex_harness_task(
        Namespace(harness=Harness()),
        receipt=receipt,
        seed="debug",
        model="gpt-test",
        invocation={
            "invocation_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            "max_build_repair_turns": 120,
            "rollout_token_limit": runner.CODEX_ROLLOUT_TOKEN_LIMIT,
            "rollout_budget_requirement": _rollout_budget(),
            "cumulative_rollout_budget_enforcement": (
                runner._runner_cumulative_rollout_budget(
                    cumulative_reported_weighted_tokens=0,
                    remaining_tokens=runner.CODEX_ROLLOUT_TOKEN_LIMIT,
                )
            ),
            "subscription_cost_support": (
                "unsupported_no_realtime_usd_meter"
            ),
        },
    )

    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["max_build_repair_turns"] == 120
    assert metadata["subscription_cost_support"] == (
        "unsupported_no_realtime_usd_meter"
    )
    assert metadata["realtime_cost_limit_usd"] is None
    assert metadata["rollout_budget_requirement"] == _rollout_budget()
    assert metadata["rollout_budget_verification"] == "pending_child_preflight"
    assert metadata["success_aggregation_eligible"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timed_out", "process_exit_code", "wrapper_exit_code", "with_usage"),
    (
        pytest.param(True, 124, 3, False, id="timeout-with-unknown-usage"),
        pytest.param(False, 17, 3, True, id="nonzero-with-reported-usage"),
        pytest.param(False, 0, 0, False, id="success-with-unknown-usage"),
    ),
)
async def test_evidence_bearing_child_is_accounted_without_business_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timed_out: bool,
    process_exit_code: int,
    wrapper_exit_code: int,
    with_usage: bool,
) -> None:
    paths = runner._codex_child_paths(tmp_path, "debug", 1)
    thread_id = str(uuid4())
    transcript_events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": thread_id}
    ]
    usage = {}
    if with_usage:
        usage = {
            "input_tokens": 31,
            "cached_input_tokens": 7,
            "output_tokens": 9,
        }
        transcript_events.append(
            {"type": "turn.completed", "usage": usage}
        )
    transcript = (
        "\n".join(json.dumps(item) for item in transcript_events) + "\n"
    ).encode()
    stderr = b"provider process did not complete successfully\n"

    class Process:
        @staticmethod
        def wait() -> int:
            return wrapper_exit_code

    def fake_managed_process(
        _stack: ExitStack,
        arguments: tuple[str, ...],
        *,
        environment: object,
        log_path: Path,
    ) -> Process:
        assert environment
        assert log_path.name.endswith("invocation-0001.log")
        budget_index = arguments.index("--rollout-token-limit")
        assert arguments[budget_index + 1] == str(
            runner.CODEX_ROLLOUT_TOKEN_LIMIT
        )
        session = (
            paths["runtime_root"]
            / "codex-home"
            / "sessions"
            / f"rollout-test-{thread_id}.jsonl"
        )
        resume_state_digest = _write_private(session, b"durable-session\n")
        transcript_digest = _write_private(paths["transcript"], transcript)
        stderr_digest = _write_private(paths["stderr_log"], stderr)
        runner.enterprise_runner._atomic_private_json(
            paths["result"],
            {
                "schema_version": "v0.4.13-t01h-codex-builder-child-1",
                "builder_actor": "codex",
                "thread_id": thread_id,
                "resume_state_path": str(session),
                "resume_state_digest": resume_state_digest,
                "exit_code": process_exit_code,
                "timed_out": timed_out,
                "usage": usage,
                "rollout_budget": _rollout_budget(),
                "public_api_manual_digest": _digest(b"manual"),
                "transcript_digest": transcript_digest,
                "stderr_digest": stderr_digest,
                "formal_archive_supported": True,
            },
        )
        return Process()

    monkeypatch.setattr(
        runner.enterprise_runner,
        "_managed_process",
        fake_managed_process,
    )
    with ExitStack() as stack:
        result, returned_paths = await runner._launch_codex_child(
            stack,
            state_root=tmp_path,
            seed="debug",
            handoff_path=tmp_path / "handoff.json",
            model="gpt-test",
            timeout_seconds=30,
            inherited_environment={"PATH": "/usr/bin"},
            rollout_token_limit=runner.CODEX_ROLLOUT_TOKEN_LIMIT,
        )

    assert returned_paths == paths
    assert result["process_execution_status"] == (
        "timed_out"
        if timed_out
        else "completed" if process_exit_code == 0 else "exited_nonzero"
    )
    assert result["child_wrapper_exit_code"] == wrapper_exit_code
    assert result["rollout_budget"] == _rollout_budget()
    assert result["usage_accounting"]["model_call_count"] == 1
    if with_usage:
        assert result["usage_accounting"]["receipt_status"] == "reported"
        assert result["usage_accounting"]["unknown_usage_model_calls"] == 0
        assert result["usage_accounting"]["fields"]["input_tokens"] == {
            "support": "reported",
            "value": 31,
        }
    else:
        assert result["usage_accounting"]["receipt_status"] == "not_reported"
        assert result["usage_accounting"]["unknown_usage_model_calls"] == 1
        assert (
            result["usage_accounting"]["model_call_count_support"]
            == "inferred_from_codex_process_receipt"
        )

    calls: list[tuple[str, dict[str, object]]] = []

    class Harness:
        async def record_usage(
            self,
            _task_id: str,
            usage_type: str,
            **kwargs: object,
        ) -> None:
            calls.append((usage_type, dict(kwargs)))

        async def record_model_usage(
            self,
            _task_id: str,
            _usage: object,
            **kwargs: object,
        ) -> None:
            calls.append(("model_usage", dict(kwargs)))

        async def finish_task(
            self,
            _task_id: str,
            **kwargs: object,
        ) -> None:
            calls.append(("finish", dict(kwargs)))

    await runner._finish_codex_harness_task(
        Namespace(harness=Harness()),
        task_id="accounted-failed-process",
        receipt={
            "application_id": str(uuid4()),
            "assignment_id": str(uuid4()),
            "session_id": str(uuid4()),
        },
        model="gpt-test",
        result=result,
        succeeded=process_exit_code == 0 and not timed_out,
    )

    assert calls[0][0] == "model_call"
    assert ("model_usage" in [name for name, _ in calls]) is with_usage
    finish = calls[-1]
    assert finish[0] == "finish"
    assert finish[1]["status"] == "paused"
    assert finish[1]["metadata"]["business_outcome"] == "unknown"
    assert finish[1]["metadata"]["project_success"] is False
    assert finish[1]["metadata"]["unknown_usage_model_calls"] == (
        0 if with_usage else 1
    )
    assert finish[1]["metadata"]["rollout_budget_receipt"] == _rollout_budget()
    assert (
        finish[1]["metadata"]["rollout_budget_verification"]
        == "verified_child_receipt"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_budget",
    (
        pytest.param(None, id="missing"),
        pytest.param(
            _rollout_budget() | {"enforcement": "advisory_only"},
            id="enforcement",
        ),
        pytest.param(
            _rollout_budget() | {"limit_tokens": 999_999},
            id="limit",
        ),
        pytest.param(
            _rollout_budget()
            | {"maximum_allowed_limit_tokens": 1_000_001},
            id="maximum",
        ),
        pytest.param(
            _rollout_budget()
            | {"continues_on_exact_thread_resume": True},
            id="resume-continuity",
        ),
        pytest.param(
            _rollout_budget() | {"multi_agent_enabled": True},
            id="multi-agent",
        ),
        pytest.param(
            _rollout_budget()
            | {
                "token_weights": {
                    "sampling": 0.5,
                    "prefill": 1.0,
                }
            },
            id="sampling-weight",
        ),
        pytest.param(
            _rollout_budget()
            | {
                "config_supported": {
                    "rollout_budget": False,
                    "multi_agent": False,
                    "multi_agent_v2": False,
                }
            },
            id="unsupported-rollout-config",
        ),
        pytest.param(
            _rollout_budget()
            | {
                "config_supported": {
                    "rollout_budget": True,
                    "multi_agent": False,
                    "multi_agent_v2": False,
                    "unverified_extra": False,
                }
            },
            id="extra-config-key",
        ),
    ),
)
async def test_tampered_rollout_budget_receipt_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_budget: object,
) -> None:
    paths = runner._codex_child_paths(tmp_path, "debug", 1)

    class Process:
        @staticmethod
        def wait() -> int:
            return 0

    def fake_managed_process(
        _stack: ExitStack,
        arguments: tuple[str, ...],
        *,
        environment: object,
        log_path: Path,
    ) -> Process:
        assert environment
        assert log_path.name.endswith("invocation-0001.log")
        limit_index = arguments.index("--rollout-token-limit")
        assert arguments[limit_index + 1] == "1000000"
        runner.enterprise_runner._atomic_private_json(
            paths["result"],
            {
                "schema_version": "v0.4.13-t01h-codex-builder-child-1",
                "builder_actor": "codex",
                "thread_id": str(uuid4()),
                "exit_code": 0,
                "timed_out": False,
                "usage": {},
                "rollout_budget": bad_budget,
                "public_api_manual_digest": _digest(b"manual"),
                "formal_archive_supported": True,
            },
        )
        return Process()

    monkeypatch.setattr(
        runner.enterprise_runner,
        "_managed_process",
        fake_managed_process,
    )
    with ExitStack() as stack, pytest.raises(
        runner.CodexBuilderRunnerError,
        match="rollout-budget receipt",
    ):
        await runner._launch_codex_child(
            stack,
            state_root=tmp_path,
            seed="debug",
            handoff_path=tmp_path / "handoff.json",
            model="gpt-test",
            timeout_seconds=30,
            inherited_environment={"PATH": "/usr/bin"},
            rollout_token_limit=runner.CODEX_ROLLOUT_TOKEN_LIMIT,
        )


@pytest.mark.asyncio
async def test_killed_child_without_result_is_attempted_unknown_not_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        @staticmethod
        def wait() -> int:
            return 137

    monkeypatch.setattr(
        runner.enterprise_runner,
        "_managed_process",
        lambda *_args, **_kwargs: Process(),
    )
    with ExitStack() as stack, pytest.raises(
        runner.CodexBuilderChildExitError
    ) as captured:
        await runner._launch_codex_child(
            stack,
            state_root=tmp_path,
            seed="debug",
            handoff_path=tmp_path / "handoff.json",
            model="gpt-test",
            timeout_seconds=30,
            inherited_environment={"PATH": "/usr/bin"},
        )

    attempted = captured.value.accounting_result
    assert attempted["accounting_evidence_level"] == "child_wrapper_attempt_only"
    assert attempted["child_wrapper_exit_code"] == 137
    assert attempted["usage_accounting"]["model_call_count"] == 1
    assert attempted["usage_accounting"]["unknown_usage_model_calls"] == 1

    calls: list[tuple[str, dict[str, object]]] = []

    class Harness:
        async def record_usage(
            self,
            _task_id: str,
            usage_type: str,
            **kwargs: object,
        ) -> None:
            calls.append((usage_type, dict(kwargs)))

        async def record_model_usage(
            self,
            _task_id: str,
            _usage: object,
            **kwargs: object,
        ) -> None:
            calls.append(("model_usage", dict(kwargs)))

        async def finish_task(
            self,
            _task_id: str,
            **kwargs: object,
        ) -> None:
            calls.append(("finish", dict(kwargs)))

    await runner._finish_codex_harness_task(
        Namespace(harness=Harness()),
        task_id="killed-child",
        receipt={
            "application_id": str(uuid4()),
            "assignment_id": str(uuid4()),
            "session_id": str(uuid4()),
        },
        model="gpt-test",
        result=attempted,
        succeeded=False,
    )

    assert [name for name, _ in calls] == ["model_call", "finish"]
    assert calls[-1][1]["status"] == "failed"
    assert calls[-1][1]["metadata"]["unknown_usage_model_calls"] == 1


def _owner_state(
    tmp_path: Path,
    *,
    seed: str,
    project_id: str,
    project_revision: int,
    application_id: str,
    assignment_id: str,
    session_id: str,
    channel_id: str,
    invocations: list[dict[str, object]],
) -> Path:
    path = tmp_path / f"codex-builder-seed-{seed}.json"
    runner.enterprise_runner._atomic_private_json(
        path,
        {
            "schema_version": "v0.4.13-t01h-codex-builder-state-1",
            "task_id": runner.TASK_ID,
            "revision": runner.REVISION,
            "seed": seed,
            "builder_actor": "codex",
            "bootstrap": {
                "task_id": project_id,
                "revision": project_revision,
                "application_id": application_id,
                "assignment_id": assignment_id,
                "session_id": session_id,
                "channel_id": channel_id,
            },
            "owner_observation_urls": {
                "collaboration_export_api_url": (
                    "http://127.0.0.1:18120/api/v1/studio/collaboration/"
                    f"channels/{channel_id}/export"
                ),
            },
            "codex_invocations": invocations,
        },
    )
    runner.enterprise_runner._atomic_private_json(
        tmp_path / "runner-secrets.json",
        {
            "schema_version": "1.1",
            "task_id": runner.TASK_ID,
            "platform_api_token": "p" * 48,
            "platform_envelope_key": "e" * 48,
            "collaboration_developer_token": "d" * 48,
            "collaboration_verifier_token": "v" * 48,
            "formal_hidden_seed_key": "h" * 48,
            "collaborative_development_signing_key": "s" * 48,
        },
    )
    return path


def _persist_resumable_invocation(
    state_path: Path,
    *,
    invocation_index: int,
    thread_id: str,
    usage_accounting: dict[str, object] | None,
    rollout_budget: dict[str, object] | None,
) -> None:
    state = json.loads(state_path.read_bytes())
    invocation = state["codex_invocations"][invocation_index - 1]
    runtime = Path(invocation["runtime_root"])
    session = (
        runtime
        / "codex-home"
        / "sessions"
        / f"rollout-{thread_id}.jsonl"
    )
    session.parent.mkdir(parents=True, exist_ok=True)
    if not session.exists():
        session.write_text("{}\n", encoding="utf-8")
    invocation.update(
        {
            "status": (
                "process_only_completed_business_outcome_unknown"
            ),
            "business_outcome": "unknown",
            "formal_archive_supported": True,
            "thread_id": thread_id,
        }
    )
    if usage_accounting is not None:
        invocation["usage_accounting"] = usage_accounting
    if rollout_budget is not None:
        invocation["rollout_budget"] = rollout_budget
    runner.enterprise_runner._atomic_private_json(state_path, state)


def _prepare_first_invocation_fixture(
    tmp_path: Path,
    *,
    seed: str,
) -> tuple[Path, dict[str, object], dict[str, object], str]:
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    session_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed=seed,
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        channel_id=str(uuid4()),
        invocations=[],
    )
    receipt: dict[str, object] = {
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION,
        "application_id": application_id,
        "assignment_id": assignment_id,
        "session_id": session_id,
    }
    first = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=False,
    )
    return state_path, receipt, first, str(uuid4())


def _invocation(
    tmp_path: Path,
    *,
    index: int,
    project_id: str,
    project_revision: int,
    application_id: str,
    assignment_id: str,
    session_id: str,
    thread_id: str,
    started_at: str,
    finished_at: str,
    resume_thread_id: str | None,
) -> dict[str, object]:
    transcript = tmp_path / "observations" / f"transcript-{index}.jsonl"
    result = tmp_path / "observations" / f"result-{index}.json"
    transcript_digest = _write_private(transcript, f"transcript-{index}".encode())
    result_digest = _write_private(result, f"result-{index}".encode())
    return {
        "invocation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "invocation_index": index,
        "builder_actor": "codex",
        "project_id": project_id,
        "project_revision": project_revision,
        "application_id": application_id,
        "assignment_id": assignment_id,
        "session_id": session_id,
        "status": "process_only_completed_business_outcome_unknown",
        "business_outcome": "unknown",
        "project_success": False,
        "formal_archive_supported": True,
        "thread_id": thread_id,
        "resume_thread_id": resume_thread_id,
        "transcript_path": str(transcript),
        "transcript_digest": transcript_digest,
        "result_path": str(result),
        "result_digest": result_digest,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _repair_db(
    tmp_path: Path,
    *,
    project_id: str,
    project_revision: int,
    application_id: str,
    assignment_id: str,
    session_id: str,
    channel_id: str,
    report_id: str,
    report_attempt_id: str,
    response_id: str,
    reprobe_id: str,
    report_digest: str,
    response_digest: str,
    reprobe_digest: str,
    contract_digest: str,
) -> dict[str, str]:
    report_message_id = str(uuid4())
    approval_message_id = str(uuid4())
    developer_message_id = str(uuid4())
    reprobe_message_id = str(uuid4())
    lease_id = str(uuid4())
    intent_digest = _digest(b"promotion-intent")
    path = tmp_path / "platform-data" / "agent_platform.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE collaboration_channels(
          channel_id TEXT,task_id TEXT,task_revision INTEGER,assignment_id TEXT,
          lilies_session_id TEXT,application_ids_json TEXT,status TEXT,revision INTEGER
        );
        CREATE TABLE collaboration_reports(
          report_id TEXT,channel_id TEXT,message_id TEXT,category TEXT,status TEXT,route TEXT,
          phase TEXT,severity TEXT,revision INTEGER,payload_json TEXT,
          payload_digest TEXT,created_at TEXT
        );
        CREATE TABLE collaboration_report_revisions(
          report_id TEXT,revision INTEGER,status TEXT,route TEXT,phase TEXT,
          severity TEXT,payload_json TEXT,payload_digest TEXT,created_at TEXT
        );
        CREATE TABLE collaboration_developer_responses(
          response_id TEXT,channel_id TEXT,report_id TEXT,lease_id TEXT,outcome TEXT,
          expected_report_revision INTEGER,resulting_report_revision INTEGER,
          request_digest TEXT,payload_json TEXT,message_id TEXT,created_at TEXT
        );
        CREATE TABLE collaboration_reprobes(
          reprobe_id TEXT,channel_id TEXT,report_id TEXT,outcome TEXT,
          request_digest TEXT,payload_json TEXT,message_id TEXT,created_at TEXT
        );
        """
    )
    report_payload = {
        "expected": "generic platform capability",
        "actual": "public contract cannot express it",
        "missing_contract": "generic capability operation",
        "manuals_checked": [{"manual_id": "public", "version": "1", "digest": _digest()}],
        "attempted_routes": [{"attempt_id": report_attempt_id}],
        "evidence_refs": [{"evidence_id": "attempt", "digest": _digest()}],
    }
    connection.execute(
        "INSERT INTO collaboration_channels VALUES(?,?,?,?,?,?,?,?)",
        (
            channel_id,
            project_id,
            project_revision,
            assignment_id,
            session_id,
            json.dumps([application_id]),
            "active",
            7,
        ),
    )
    connection.execute(
        "INSERT INTO collaboration_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            report_id,
            channel_id,
            report_message_id,
            "platform_capability_gap",
            "lilies_verified",
            "developer",
            "run",
            "blocking",
            3,
            json.dumps(report_payload),
            report_digest,
            "2026-07-26T00:02:00+00:00",
        ),
    )
    for revision, status, digest in (
        (1, "approved_for_codex", report_digest),
        (2, "ready_for_lilies_verification", _digest(b"report-r2")),
        (3, "lilies_verified", _digest(b"report-r3")),
    ):
        connection.execute(
            "INSERT INTO collaboration_report_revisions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                report_id,
                revision,
                status,
                "developer",
                "run",
                "blocking",
                json.dumps(report_payload),
                digest,
                f"2026-07-26T00:0{revision + 1}:00+00:00",
            ),
        )
    connection.execute(
        "INSERT INTO collaboration_developer_responses VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            response_id,
            channel_id,
            report_id,
            lease_id,
            "implemented",
            1,
            2,
            response_digest,
            json.dumps(
                {
                    "commit_sha": "a" * 40,
                    "implementation_diff_digest": intent_digest,
                    "new_contract_digest": contract_digest,
                    "generic_capability_changes": ["generic RAG block"],
                    "generality_rationale": (
                        "The typed contract applies to every platform project "
                        "and contains no host-specific mapping."
                    ),
                    "tests_run": [{"test_id": "generic", "exit_code": 0}],
                }
            ),
            developer_message_id,
            "2026-07-26T00:04:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO collaboration_reprobes VALUES(?,?,?,?,?,?,?,?)",
        (
            reprobe_id,
            channel_id,
            report_id,
            "lilies_verified",
            reprobe_digest,
            json.dumps(
                {
                    "report_revision": 2,
                    "contract_digest": contract_digest,
                }
            ),
            reprobe_message_id,
            "2026-07-26T00:06:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    path.chmod(0o600)
    promotion_payload = {
        "schema_version": "1.0",
        "assignment_id": assignment_id,
        "channel_id": channel_id,
        "report_id": report_id,
        "report_revision": 1,
        "lease_id": lease_id,
        "response_id": response_id,
        "workspace_manifest_digest": _digest(b"workspace-manifest"),
        "source_manifest_digest": _digest(b"source-manifest"),
        "intent_digest": intent_digest,
        "branch_ref": "refs/heads/test-promotion",
        "parent_commit_sha": "c" * 40,
        "parent_tree_sha": "d" * 40,
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "changed_paths": [
            "platform/backend/src/agent_platform/generic_rag.py"
        ],
        "object_state": "object_created",
        "activation_state": "activated",
        "reload_status": "not_required",
        "object_created_at": "2026-07-26T00:03:30Z",
        "activated_at": "2026-07-26T00:03:31Z",
        "process_instance_id": str(uuid4()),
    }
    promotion_payload["receipt_digest"] = _digest(
        runner.enterprise_runner._canonical_json(promotion_payload)
    )
    promotion = (
        runner.formal_source_provenance.DeveloperSourcePromotionReceipt.model_validate(
            promotion_payload
        )
    )
    promotion_path = (
        tmp_path
        / "platform-data"
        / "formal-source-provenance"
        / "assignments"
        / assignment_id
        / "promotions"
        / response_id
        / "activated.json"
    )
    promotion_path.parent.mkdir(parents=True)
    promotion_path.write_bytes(
        runner.formal_source_provenance._canonical_json(promotion)
    )
    promotion_path.chmod(0o400)
    return {
        "report_message_id": report_message_id,
        "approval_message_id": approval_message_id,
        "developer_message_id": developer_message_id,
        "reprobe_message_id": reprobe_message_id,
        "lease_id": lease_id,
        "intent_digest": intent_digest,
    }


def _collaboration_export(
    *,
    channel_id: str,
    report_id: str,
    response_id: str,
    reprobe_id: str,
    records: dict[str, str],
) -> dict[str, object]:
    messages = [
        {
            "message_id": records["report_message_id"],
            "seq": 1,
            "correlation_id": report_id,
            "causal_parent_id": None,
            "payload_schema": "collaboration.report.v1",
            "payload": {"report_id": report_id},
        },
        {
            "message_id": records["approval_message_id"],
            "seq": 2,
            "correlation_id": report_id,
            "causal_parent_id": records["report_message_id"],
            "payload_schema": "collaboration.approval.v1",
            "payload": {"report_id": report_id},
        },
        {
            "message_id": records["developer_message_id"],
            "seq": 3,
            "correlation_id": report_id,
            "causal_parent_id": records["approval_message_id"],
            "payload_schema": "collaboration.developer_response.v1",
            "payload": {
                "report_id": report_id,
                "response_id": response_id,
            },
        },
        {
            "message_id": records["reprobe_message_id"],
            "seq": 4,
            "correlation_id": report_id,
            "causal_parent_id": records["developer_message_id"],
            "payload_schema": "collaboration.lilies_reprobe_result.v1",
            "payload": {
                "report_id": report_id,
                "reprobe_id": reprobe_id,
            },
        },
    ]
    exported = {
        "schema_version": "1.0",
        "complete": True,
        "counts": {
            "messages": len(messages),
            "reports": 1,
            "developer_responses": 1,
            "reprobes": 1,
        },
        "watermark": {
            "min_message_seq": 1,
            "max_message_seq": len(messages),
            "next_seq": len(messages) + 1,
        },
        "messages": messages,
        "reports": [{"report_id": report_id}],
        "developer_responses": [{"response_id": response_id}],
        "reprobes": [{"reprobe_id": reprobe_id}],
        "claims": [],
    }
    return {
        "schema_version": "1.0",
        "channel_id": channel_id,
        "export": exported,
        "counters": {
            "messages": len(messages),
            "correlations": 1,
            "reports": 1,
            "claims": 0,
        },
        "digest": _digest(
            runner.enterprise_runner._canonical_json(exported)
        ),
    }


def test_verified_repair_cycle_binds_db_revisions_attempts_and_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = "debug"
    project_id = runner.TASK_ID
    project_revision = runner.REVISION
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    session_id = str(uuid4())
    channel_id = str(uuid4())
    report_id = str(uuid4())
    report_attempt_id = str(uuid4())
    response_id = str(uuid4())
    reprobe_id = str(uuid4())
    report_digest = _digest(b"report-r1")
    response_digest = _digest(b"developer-response")
    reprobe_digest = _digest(b"reprobe")
    contract_digest = _digest(b"new-contract")
    first = _invocation(
        tmp_path,
        index=1,
        project_id=project_id,
        project_revision=project_revision,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        thread_id="thread-one",
        started_at="2026-07-26T00:01:00+00:00",
        finished_at="2026-07-26T00:03:00+00:00",
        resume_thread_id=None,
    )
    second = _invocation(
        tmp_path,
        index=2,
        project_id=project_id,
        project_revision=project_revision,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        thread_id="thread-one",
        started_at="2026-07-26T00:05:00+00:00",
        finished_at="2026-07-26T00:07:00+00:00",
        resume_thread_id="thread-one",
    )
    _owner_state(
        tmp_path,
        seed=seed,
        project_id=project_id,
        project_revision=project_revision,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        channel_id=channel_id,
        invocations=[first, second],
    )
    records = _repair_db(
        tmp_path,
        project_id=project_id,
        project_revision=project_revision,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        channel_id=channel_id,
        report_id=report_id,
        report_attempt_id=report_attempt_id,
        response_id=response_id,
        reprobe_id=reprobe_id,
        report_digest=report_digest,
        response_digest=response_digest,
        reprobe_digest=reprobe_digest,
        contract_digest=contract_digest,
    )
    export = _collaboration_export(
        channel_id=channel_id,
        report_id=report_id,
        response_id=response_id,
        reprobe_id=reprobe_id,
        records=records,
    )
    monkeypatch.setattr(
        runner.enterprise_runner,
        "_request_json",
        lambda *_args, **_kwargs: export,
    )
    common = {
        "state_root": tmp_path,
        "seed": seed,
        "cycle_id": "cycle-real-001",
        "project_id": project_id,
        "session_id": session_id,
        "channel_revision": 7,
        "report_id": report_id,
        "project_revision": project_revision,
        "report_attempt_id": report_attempt_id,
    }
    builder = runner._verified_repair_binding(
        **common,
        phase="builder_report",
        record_ref=report_id,
        record_digest=report_digest,
        outcome="platform_capability_gap",
        report_revision=1,
        attempt_id=str(first["attempt_id"]),
    )
    first_result = runner._record_repair_phase(
        tmp_path,
        seed=seed,
        cycle_id="cycle-real-001",
        project_id=project_id,
        phase="builder_report",
        session_id=session_id,
        record_ref=report_id,
        record_digest=report_digest,
        outcome="platform_capability_gap",
        verified_binding=builder,
    )
    assert first_result["closure_eligible"] is False

    database = sqlite3.connect(
        tmp_path / "platform-data" / "agent_platform.db"
    )
    database.execute(
        """
        UPDATE collaboration_developer_responses
        SET expected_report_revision=2
        WHERE response_id=?
        """,
        (response_id,),
    )
    database.commit()
    database.close()
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="developer supplementation binding",
    ):
        runner._verified_repair_binding(
            **common,
            phase="development_enablement",
            record_ref=response_id,
            record_digest=response_digest,
            outcome="implemented",
            report_revision=2,
            attempt_id=str(first["attempt_id"]),
        )
    database = sqlite3.connect(
        tmp_path / "platform-data" / "agent_platform.db"
    )
    database.execute(
        """
        UPDATE collaboration_developer_responses
        SET expected_report_revision=1
        WHERE response_id=?
        """,
        (response_id,),
    )
    database.commit()
    database.close()

    database = sqlite3.connect(
        tmp_path / "platform-data" / "agent_platform.db"
    )
    stored_payload = json.loads(
        database.execute(
            """
            SELECT payload_json FROM collaboration_developer_responses
            WHERE response_id=?
            """,
            (response_id,),
        ).fetchone()[0]
    )
    tampered_payload = dict(stored_payload)
    tampered_payload["implementation_diff_digest"] = _digest(b"other-diff")
    database.execute(
        """
        UPDATE collaboration_developer_responses SET payload_json=?
        WHERE response_id=?
        """,
        (json.dumps(tampered_payload), response_id),
    )
    database.commit()
    database.close()
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="trusted source promotion",
    ):
        runner._verified_repair_binding(
            **common,
            phase="development_enablement",
            record_ref=response_id,
            record_digest=response_digest,
            outcome="implemented",
            report_revision=2,
            attempt_id=str(first["attempt_id"]),
        )
    database = sqlite3.connect(
        tmp_path / "platform-data" / "agent_platform.db"
    )
    database.execute(
        """
        UPDATE collaboration_developer_responses SET payload_json=?
        WHERE response_id=?
        """,
        (json.dumps(stored_payload), response_id),
    )
    database.commit()
    database.close()

    developer = runner._verified_repair_binding(
        **common,
        phase="development_enablement",
        record_ref=response_id,
        record_digest=response_digest,
        outcome="implemented",
        report_revision=2,
        attempt_id=str(first["attempt_id"]),
    )
    assert developer["source_promotion_verified"] is True
    assert developer["implementation_diff_digest"] == records["intent_digest"]
    runner._record_repair_phase(
        tmp_path,
        seed=seed,
        cycle_id="cycle-real-001",
        project_id=project_id,
        phase="development_enablement",
        session_id=session_id,
        record_ref=response_id,
        record_digest=response_digest,
        outcome="implemented",
        verified_binding=developer,
    )
    rerun = runner._verified_repair_binding(
        **common,
        phase="same_project_rerun",
        record_ref=reprobe_id,
        record_digest=reprobe_digest,
        outcome="lilies_verified",
        report_revision=2,
        attempt_id=str(second["attempt_id"]),
    )
    assert rerun["history_replay_complete"] is True
    assert rerun["history_export_message_count"] == 4
    result = runner._record_repair_phase(
        tmp_path,
        seed=seed,
        cycle_id="cycle-real-001",
        project_id=project_id,
        phase="same_project_rerun",
        session_id=session_id,
        record_ref=reprobe_id,
        record_digest=reprobe_digest,
        outcome="lilies_verified",
        verified_binding=rerun,
    )

    assert result["verified_complete"] is True
    assert result["closure_eligible"] is True
    ledger = json.loads(runner._repair_ledger_path(tmp_path, seed).read_bytes())
    cycle = ledger["cycles"][0]
    assert cycle["closure_eligible"] is True
    assert [item["verified_binding"]["record_kind"] for item in cycle["segments"]] == [
        "collaboration_report_revision",
        "collaboration_developer_response",
        "collaboration_reprobe",
    ]
    assert cycle["segments"][2]["verified_binding"]["attempt_id"] == second["attempt_id"]


def test_collaboration_history_replay_fails_closed_on_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel_id = str(uuid4())
    report_id = str(uuid4())
    response_id = str(uuid4())
    reprobe_id = str(uuid4())
    records = {
        "report_message_id": str(uuid4()),
        "approval_message_id": str(uuid4()),
        "developer_message_id": str(uuid4()),
        "reprobe_message_id": str(uuid4()),
    }
    state_path = _owner_state(
        tmp_path,
        seed="debug",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION,
        application_id=str(uuid4()),
        assignment_id=str(uuid4()),
        session_id=str(uuid4()),
        channel_id=channel_id,
        invocations=[],
    )
    state = runner.enterprise_runner._read_private_json(state_path)
    baseline = _collaboration_export(
        channel_id=channel_id,
        report_id=report_id,
        response_id=response_id,
        reprobe_id=reprobe_id,
        records=records,
    )

    changed_digest = json.loads(json.dumps(baseline))
    changed_digest["digest"] = _digest(b"changed")
    incomplete = json.loads(json.dumps(baseline))
    incomplete["export"]["complete"] = False
    broken_parent = json.loads(json.dumps(baseline))
    broken_parent["export"]["messages"][2]["causal_parent_id"] = None
    broken_sequence = json.loads(json.dumps(baseline))
    broken_sequence["export"]["messages"][2]["seq"] = 9
    for candidate in (incomplete, broken_parent, broken_sequence):
        candidate["digest"] = _digest(
            runner.enterprise_runner._canonical_json(candidate["export"])
        )

    for candidate in (
        changed_digest,
        incomplete,
        broken_parent,
        broken_sequence,
    ):
        monkeypatch.setattr(
            runner.enterprise_runner,
            "_request_json",
            lambda *_args, _candidate=candidate, **_kwargs: _candidate,
        )
        with pytest.raises(runner.CodexBuilderRunnerError):
            runner._collaboration_history_replay(
                tmp_path,
                state=state,
                channel_id=channel_id,
                report_id=report_id,
                builder_message_id=records["report_message_id"],
                developer_message_id=records["developer_message_id"],
                developer_response_id=response_id,
                reprobe_message_id=records["reprobe_message_id"],
                reprobe_id=reprobe_id,
            )


def test_developer_promotion_receipt_requires_immutable_canonical_file(
    tmp_path: Path,
) -> None:
    assignment_id = str(uuid4())
    response_id = str(uuid4())
    records = _repair_db(
        tmp_path,
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION,
        application_id=str(uuid4()),
        assignment_id=assignment_id,
        session_id=str(uuid4()),
        channel_id=str(uuid4()),
        report_id=str(uuid4()),
        report_attempt_id=str(uuid4()),
        response_id=response_id,
        reprobe_id=str(uuid4()),
        report_digest=_digest(b"promotion-report"),
        response_digest=_digest(b"promotion-response"),
        reprobe_digest=_digest(b"promotion-reprobe"),
        contract_digest=_digest(b"promotion-contract"),
    )
    receipt = runner._developer_source_promotion_receipt(
        tmp_path,
        assignment_id=assignment_id,
        response_id=response_id,
    )
    assert receipt.intent_digest == records["intent_digest"]

    path = (
        tmp_path
        / "platform-data"
        / "formal-source-provenance"
        / "assignments"
        / assignment_id
        / "promotions"
        / response_id
        / "activated.json"
    )
    path.chmod(0o600)
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="trusted immutable source promotion",
    ):
        runner._developer_source_promotion_receipt(
            tmp_path,
            assignment_id=assignment_id,
            response_id=response_id,
        )


def test_unverified_internal_append_is_never_closure_eligible(tmp_path: Path) -> None:
    session_id = str(uuid4())
    for phase in runner.REPAIR_PHASES:
        result = runner._record_repair_phase(
            tmp_path,
            seed="101",
            cycle_id="internal-only",
            project_id=runner.TASK_ID,
            phase=phase,
            session_id=session_id,
            record_ref=f"{phase}.json",
            record_digest=_digest(phase.encode()),
            outcome="fixture",
        )
    assert result["complete"] is True
    assert result["verified_complete"] is False
    assert result["closure_eligible"] is False


def test_resume_reuses_thread_runtime_and_allocates_new_invocation_outputs(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    session_id = str(uuid4())
    channel_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed="202",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        channel_id=channel_id,
        invocations=[],
    )
    receipt = {
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION,
        "application_id": application_id,
        "assignment_id": assignment_id,
        "session_id": session_id,
    }
    first = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=False,
    )
    budget_state = json.loads(state_path.read_bytes())["codex_budget"]
    assert budget_state == {
        "max_build_repair_turns": 120,
        "rollout_budget_requirement": _rollout_budget(),
        "cumulative_rollout_budget_enforcement": {
            "enforcement": (
                "runner_persisted_reported_usage_remaining_budget"
            ),
            "cumulative_limit_tokens": 1_000_000,
            "weighted_token_formula": (
                "output_tokens+max(0,input_tokens-cache_read_input_tokens)"
            ),
            "token_weights": {
                "sampling": 1.0,
                "prefill": 1.0,
            },
            "prior_usage_requirement": "reported_only_fail_closed",
            "cumulative_reported_weighted_tokens": 0,
            "remaining_tokens": 1_000_000,
            "next_invocation_cli_limit_tokens": 1_000_000,
        },
        "cumulative_limit_tokens": 1_000_000,
        "cumulative_reported_weighted_tokens": 0,
        "remaining_tokens_before_invocation": 1_000_000,
        "current_invocation_cli_limit_tokens": 1_000_000,
        "prepared_invocation_count": 1,
        "remaining_invocations": 119,
        "subscription_cost_support": "unsupported_no_realtime_usd_meter",
        "realtime_cost_limit_usd": None,
    }
    runtime = Path(str(first["runtime_root"]))
    runtime.mkdir(parents=True)
    thread_id = str(uuid4())
    sessions = runtime / "codex-home" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"rollout-{thread_id}.jsonl").write_text(
        "{}\n",
        encoding="utf-8",
    )
    runner._mark_codex_invocation_running(
        state_path,
        invocation_id=str(first["invocation_id"]),
    )
    state = json.loads(state_path.read_bytes())
    state["codex_invocations"][0].update(
        {
            "status": "process_only_completed_business_outcome_unknown",
            "business_outcome": "unknown",
            "formal_archive_supported": True,
            "thread_id": thread_id,
            "rollout_budget": _rollout_budget(),
            "usage_accounting": _reported_usage(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=30,
            ),
        }
    )
    runner.enterprise_runner._atomic_private_json(state_path, state)

    second = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )

    assert second["invocation_index"] == 2
    assert second["rollout_token_limit"] == 999_910
    assert second["rollout_budget_requirement"] == _rollout_budget(999_910)
    assert second["cumulative_rollout_budget_enforcement"][
        "cumulative_reported_weighted_tokens"
    ] == 90
    assert second["cumulative_rollout_budget_enforcement"][
        "remaining_tokens"
    ] == 999_910
    assert second["resume_thread_id"] == thread_id
    assert second["runtime_root"] == first["runtime_root"]
    assert second["transcript_path"] != first["transcript_path"]
    assert second["result_path"] != first["result_path"]
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="no terminal process receipt",
    ):
        runner._prepare_codex_invocation(
            state_path,
            receipt=receipt,
            resume=False,
        )


def test_adjacent_revision_can_explicitly_replace_isolated_codex_context(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    prior_assignment_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed="debug",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION - 1,
        application_id=application_id,
        assignment_id=prior_assignment_id,
        session_id=str(uuid4()),
        channel_id=str(uuid4()),
        invocations=[],
    )
    prior = runner._prepare_codex_invocation(
        state_path,
        receipt={
            "task_id": runner.TASK_ID,
            "revision": runner.REVISION - 1,
            "application_id": application_id,
            "assignment_id": prior_assignment_id,
            "session_id": str(uuid4()),
        },
        resume=False,
    )
    state = json.loads(state_path.read_bytes())
    state["codex_invocations"][0].update(
        {
            "status": "process_failed",
            "business_outcome": "unknown",
            "formal_archive_supported": True,
        }
    )
    runner.enterprise_runner._atomic_private_json(state_path, state)
    new_assignment_id = str(uuid4())

    replacement = runner._prepare_codex_invocation(
        state_path,
        receipt={
            "task_id": runner.TASK_ID,
            "revision": runner.REVISION,
            "application_id": application_id,
            "assignment_id": new_assignment_id,
            "session_id": str(uuid4()),
        },
        resume=False,
        replace_context=True,
    )

    assert replacement["invocation_index"] == 2
    assert replacement["resume_thread_id"] is None
    assert replacement["rollout_token_limit"] == 1_000_000
    assert replacement["runtime_root"] != prior["runtime_root"]
    assert replacement["runtime_root"].endswith(
        f"seed-debug-assignment-{new_assignment_id}"
    )
    replacement_evidence = replacement["replacement_context"]
    assert replacement_evidence["prior_assignment_ids"] == [
        prior_assignment_id
    ]
    assert replacement_evidence["new_assignment_id"] == new_assignment_id
    assert replacement_evidence["prior_project_revision"] == (
        runner.REVISION - 1
    )
    assert replacement_evidence["new_project_revision"] == runner.REVISION
    assert replacement_evidence["public_api_boundary_unchanged"] is True
    persisted = json.loads(state_path.read_bytes())
    assert len(persisted["codex_invocations"]) == 2
    assert persisted["codex_invocations"][0]["status"] == "process_failed"
    assert persisted["codex_context_replacements"] == [
        replacement_evidence
    ]


def test_replacement_context_resume_uses_only_replacement_generation_budget(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    prior_assignment_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed="replacement-resume",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION - 1,
        application_id=application_id,
        assignment_id=prior_assignment_id,
        session_id=str(uuid4()),
        channel_id=str(uuid4()),
        invocations=[],
    )
    prior = runner._prepare_codex_invocation(
        state_path,
        receipt={
            "task_id": runner.TASK_ID,
            "revision": runner.REVISION - 1,
            "application_id": application_id,
            "assignment_id": prior_assignment_id,
            "session_id": str(uuid4()),
        },
        resume=False,
    )
    _persist_resumable_invocation(
        state_path,
        invocation_index=1,
        thread_id=str(uuid4()),
        usage_accounting=_reported_usage(
            input_tokens=400,
            output_tokens=100,
            cache_read_input_tokens=200,
        ),
        rollout_budget=_rollout_budget(),
    )
    new_assignment_id = str(uuid4())
    new_session_id = str(uuid4())
    receipt = {
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION,
        "application_id": application_id,
        "assignment_id": new_assignment_id,
        "session_id": new_session_id,
    }
    replacement = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=False,
        replace_context=True,
    )
    assert replacement["runtime_root"] != prior["runtime_root"]
    replacement_thread_id = str(uuid4())
    _persist_resumable_invocation(
        state_path,
        invocation_index=2,
        thread_id=replacement_thread_id,
        usage_accounting=_reported_usage(
            input_tokens=180,
            output_tokens=40,
            cache_read_input_tokens=80,
        ),
        rollout_budget=_rollout_budget(),
    )

    resumed = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )

    assert resumed["invocation_index"] == 3
    assert resumed["resume_thread_id"] == replacement_thread_id
    assert resumed["runtime_root"] == replacement["runtime_root"]
    assert resumed["rollout_token_limit"] == 999_860
    assert resumed["rollout_budget_requirement"] == _rollout_budget(999_860)
    assert resumed["cumulative_rollout_budget_enforcement"][
        "cumulative_reported_weighted_tokens"
    ] == 140


def test_replacement_context_requires_fresh_assignment_and_adjacent_revision(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed="debug",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION - 1,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=str(uuid4()),
        channel_id=str(uuid4()),
        invocations=[
            {
                "invocation_id": str(uuid4()),
                "assignment_id": assignment_id,
                "project_revision": runner.REVISION - 1,
                "status": "process_failed",
            }
        ],
    )

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="fresh formal assignment",
    ):
        runner._prepare_codex_invocation(
            state_path,
            receipt={
                "task_id": runner.TASK_ID,
                "revision": runner.REVISION,
                "application_id": application_id,
                "assignment_id": assignment_id,
                "session_id": str(uuid4()),
            },
            resume=False,
            replace_context=True,
        )

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="adjacent project revision",
    ):
        runner._prepare_codex_invocation(
            state_path,
            receipt={
                "task_id": runner.TASK_ID,
                "revision": runner.REVISION + 1,
                "application_id": application_id,
                "assignment_id": str(uuid4()),
                "session_id": str(uuid4()),
            },
            resume=False,
            replace_context=True,
        )


def test_multiple_resumes_use_only_persisted_cumulative_remaining_budget(
    tmp_path: Path,
) -> None:
    state_path, receipt, _first, thread_id = (
        _prepare_first_invocation_fixture(tmp_path, seed="multi-budget")
    )
    _persist_resumable_invocation(
        state_path,
        invocation_index=1,
        thread_id=thread_id,
        usage_accounting=_reported_usage(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=30,
        ),
        rollout_budget=_rollout_budget(1_000_000),
    )

    second = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )
    assert second["rollout_token_limit"] == 999_910
    _persist_resumable_invocation(
        state_path,
        invocation_index=2,
        thread_id=thread_id,
        usage_accounting=_reported_usage(
            input_tokens=200,
            output_tokens=50,
            cache_read_input_tokens=100,
        ),
        rollout_budget=_rollout_budget(999_910),
    )

    third = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )

    assert third["rollout_token_limit"] == 999_760
    assert third["rollout_budget_requirement"] == _rollout_budget(999_760)
    enforcement = third["cumulative_rollout_budget_enforcement"]
    assert enforcement["cumulative_reported_weighted_tokens"] == 240
    assert enforcement["remaining_tokens"] == 999_760
    persisted = json.loads(state_path.read_bytes())["codex_budget"]
    assert persisted["cumulative_reported_weighted_tokens"] == 240
    assert persisted["current_invocation_cli_limit_tokens"] == 999_760


def test_verified_pre_provider_authority_guard_failure_can_resume_without_rewriting_history(
    tmp_path: Path,
) -> None:
    state_path, receipt, _first, thread_id = (
        _prepare_first_invocation_fixture(
            tmp_path,
            seed="authority-preflight-retry",
        )
    )
    _persist_resumable_invocation(
        state_path,
        invocation_index=1,
        thread_id=thread_id,
        usage_accounting=_reported_usage(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=30,
        ),
        rollout_budget=_rollout_budget(),
    )
    second = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )
    runner._mark_codex_invocation_running(
        state_path,
        invocation_id=str(second["invocation_id"]),
    )
    wrapper_failure = runner.CodexBuilderChildExitError(
        "preflight",
        wrapper_exit_code=2,
    )
    runner._mark_codex_invocation_failed(
        state_path,
        invocation_id=str(second["invocation_id"]),
        error_code="CodexBuilderChildExitError",
        accounting_result=wrapper_failure.accounting_result,
    )
    log_path = (
        tmp_path
        / "logs"
        / "codex-builder-child-authority-preflight-retry-invocation-0002.log"
    )
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(runner.AUTHORITY_GUARD_PREFLIGHT_ERROR)
    log_path.chmod(0o644)

    third = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )

    assert third["invocation_index"] == 3
    assert third["resume_thread_id"] == thread_id
    assert third["preflight_retry_of_invocation_id"] == second[
        "invocation_id"
    ]
    assert third["rollout_token_limit"] == second["rollout_token_limit"]
    state = json.loads(state_path.read_bytes())
    failed = state["codex_invocations"][1]
    assert failed["status"] == "process_failed"
    assert failed["usage_accounting"]["unknown_usage_model_calls"] == 1
    reconciliation = failed["pre_provider_failure_reconciliation"]
    assert reconciliation["provider_process_started"] is False
    assert reconciliation["retry_eligible"] is True
    assert Path(reconciliation["evidence_path"]).is_file()


def test_indeterminate_provider_retry_requires_fresh_risk_authorization_and_keeps_usage_unknown(
    tmp_path: Path,
) -> None:
    state_path, receipt, _first, thread_id = (
        _prepare_first_invocation_fixture(
            tmp_path,
            seed="indeterminate-provider-retry",
        )
    )
    _persist_resumable_invocation(
        state_path,
        invocation_index=1,
        thread_id=thread_id,
        usage_accounting=_reported_usage(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=30,
        ),
        rollout_budget=_rollout_budget(),
    )
    second = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
    )
    paths = runner._codex_child_paths(
        tmp_path,
        "indeterminate-provider-retry",
        2,
    )
    result_digest = _write_private(paths["result"], b"provider-reset-result")
    transcript_digest = _write_private(
        paths["transcript"],
        b'{"type":"thread.started"}\n',
    )
    stderr_digest = _write_private(paths["stderr_log"], b"")
    paths["result"].parent.chmod(0o700)
    state = json.loads(state_path.read_bytes())
    failed = state["codex_invocations"][1]
    failed.update(
        {
            "status": (
                "process_only_ended_with_error_business_outcome_unknown"
            ),
            "process_execution_status": "exited_nonzero",
            "business_outcome": "unknown",
            "formal_archive_supported": True,
            "thread_id": thread_id,
            "result_digest": result_digest,
            "transcript_digest": transcript_digest,
            "stderr_digest": stderr_digest,
            "usage_accounting": {
                **runner._usage_accounting(None),
                "model_call_count": 1,
                "model_call_count_support": (
                    "inferred_from_codex_process_receipt"
                ),
                "unknown_usage_model_calls": 1,
            },
        }
    )
    runner.enterprise_runner._atomic_private_json(state_path, state)

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="fresh explicit authorization",
    ):
        runner._prepare_codex_invocation(
            state_path,
            receipt=receipt,
            resume=True,
        )

    third = runner._prepare_codex_invocation(
        state_path,
        receipt=receipt,
        resume=True,
        authorize_indeterminate_provider_retry=True,
    )

    assert third["invocation_index"] == 3
    assert third["resume_thread_id"] == thread_id
    assert third["preflight_retry_of_invocation_id"] == second[
        "invocation_id"
    ]
    persisted = json.loads(state_path.read_bytes())
    failed = persisted["codex_invocations"][1]
    assert failed["usage_accounting"]["unknown_usage_model_calls"] == 1
    authorization = failed["indeterminate_provider_retry_authorization"]
    assert authorization["provider_outcome"] == "indeterminate"
    assert authorization[
        "duplicate_charge_or_execution_risk_acknowledged"
    ] is True
    assert authorization["retry_eligible"] is True
    evidence_path = Path(authorization["evidence_path"])
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o400
    evidence = json.loads(evidence_path.read_bytes())
    assert evidence["usage_accounting_policy"] == (
        "preserve_unknown_never_coerce_to_zero"
    )


@pytest.mark.parametrize(
    ("usage_accounting", "message"),
    (
        pytest.param(
            None,
            "missing usage accounting",
            id="missing-accounting",
        ),
        pytest.param(
            {
                **runner._usage_accounting(None),
                "model_call_count": 1,
                "model_call_count_support": (
                    "inferred_from_codex_process_receipt"
                ),
                "unknown_usage_model_calls": 1,
            },
            "unknown or unreported usage",
            id="unknown-usage",
        ),
        pytest.param(
            _reported_usage(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=30,
            )
            | {
                "fields": {
                    **_reported_usage(
                        input_tokens=100,
                        output_tokens=20,
                        cache_read_input_tokens=30,
                    )["fields"],
                    "cache_read_input_tokens": {
                        "support": "not_reported"
                    },
                }
            },
            "missing cache_read_input_tokens usage",
            id="missing-required-field",
        ),
    ),
)
def test_resume_fails_closed_when_prior_usage_is_not_fully_reported(
    tmp_path: Path,
    usage_accounting: dict[str, object] | None,
    message: str,
) -> None:
    state_path, receipt, _first, thread_id = (
        _prepare_first_invocation_fixture(tmp_path, seed=str(uuid4()))
    )
    _persist_resumable_invocation(
        state_path,
        invocation_index=1,
        thread_id=thread_id,
        usage_accounting=usage_accounting,
        rollout_budget=_rollout_budget(),
    )

    with pytest.raises(runner.CodexBuilderRunnerError, match=message):
        runner._prepare_codex_invocation(
            state_path,
            receipt=receipt,
            resume=True,
        )

    assert len(json.loads(state_path.read_bytes())["codex_invocations"]) == 1


@pytest.mark.parametrize("weighted_usage", (999_999, 1_000_000))
def test_resume_rejects_less_than_two_remaining_cumulative_tokens(
    tmp_path: Path,
    weighted_usage: int,
) -> None:
    state_path, receipt, _first, thread_id = (
        _prepare_first_invocation_fixture(tmp_path, seed=str(uuid4()))
    )
    _persist_resumable_invocation(
        state_path,
        invocation_index=1,
        thread_id=thread_id,
        usage_accounting=_reported_usage(
            input_tokens=weighted_usage,
            output_tokens=0,
            cache_read_input_tokens=0,
        ),
        rollout_budget=_rollout_budget(),
    )

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="cumulative Codex rollout token budget is exhausted",
    ):
        runner._prepare_codex_invocation(
            state_path,
            receipt=receipt,
            resume=True,
        )


def test_dynamic_child_rollout_limit_receipt_is_bound_exactly() -> None:
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="rollout-budget receipt",
    ):
        runner._validated_codex_rollout_budget(
            _rollout_budget(999_909),
            expected_limit_tokens=999_910,
        )

    assert runner._validated_codex_rollout_budget(
        _rollout_budget(999_910),
        expected_limit_tokens=999_910,
    ) == _rollout_budget(999_910)


def test_verified_rollout_budget_persists_to_invocation_and_execution_state(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    session_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed="debug",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        channel_id=str(uuid4()),
        invocations=[],
    )
    invocation = runner._prepare_codex_invocation(
        state_path,
        receipt={
            "task_id": runner.TASK_ID,
            "revision": runner.REVISION,
            "application_id": application_id,
            "assignment_id": assignment_id,
            "session_id": session_id,
        },
        resume=False,
    )
    runner._mark_codex_invocation_running(
        state_path,
        invocation_id=str(invocation["invocation_id"]),
    )
    paths = runner._codex_child_paths(tmp_path, "debug", 1)
    _write_private(paths["result"], b"bound-result")
    runner._attach_codex_result(
        state_path,
        invocation_id=str(invocation["invocation_id"]),
        result={
            "thread_id": str(uuid4()),
            "process_execution_status": "completed",
            "child_wrapper_exit_code": 0,
            "exit_code": 0,
            "timed_out": False,
            "rollout_budget": _rollout_budget(),
            "usage_accounting": runner._usage_accounting(None),
            "formal_archive_supported": True,
        },
        paths=paths,
    )

    state = json.loads(state_path.read_bytes())
    assert (
        state["codex_invocations"][0]["rollout_budget"]
        == _rollout_budget()
    )
    assert state["codex_execution"]["rollout_budget"] == _rollout_budget()
    assert (
        state["codex_budget"]["rollout_budget_requirement"]
        == _rollout_budget()
    )
    assert state["codex_execution"]["cost_support"] == "unsupported"


def test_invocation_121_is_rejected_by_frozen_turn_budget(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    assignment_id = str(uuid4())
    session_id = str(uuid4())
    state_path = _owner_state(
        tmp_path,
        seed="303",
        project_id=runner.TASK_ID,
        project_revision=runner.REVISION,
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        channel_id=str(uuid4()),
        invocations=[
            {
                "invocation_id": str(uuid4()),
                "status": "process_only_completed_business_outcome_unknown",
            }
            for _ in range(120)
        ],
    )

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="invocation budget exhausted: 120 >= 120",
    ):
        runner._prepare_codex_invocation(
            state_path,
            receipt={
                "task_id": runner.TASK_ID,
                "revision": runner.REVISION,
                "application_id": application_id,
                "assignment_id": assignment_id,
                "session_id": session_id,
            },
            resume=True,
        )


def test_token_monitor_marks_missing_ledgers_unknown_and_model_breaker_off(
    tmp_path: Path,
) -> None:
    observed = 10.0
    snapshot, _ = runner._record_external_token_monitor_snapshot(
        tmp_path,
        previous=None,
        previous_at=observed,
        observed_at=observed,
    )
    latest = tmp_path / "monitoring" / "token-monitor.latest.json"
    history = tmp_path / "monitoring" / "token-monitor.jsonl"
    projection = json.loads(latest.read_bytes())

    assert projection["safety"]["model_egress_enabled"] is False
    assert projection["safety"]["evidence_complete"] is False
    assert projection["safety"]["safe_now"] is None
    assert projection["safety"]["background_consumption_observed"] is None
    assert snapshot["safety"]["missing_required_sources"]
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    assert stat.S_IMODE(history.stat().st_mode) == 0o600
