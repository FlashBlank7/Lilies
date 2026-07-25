from __future__ import annotations

import json
import stat
from argparse import Namespace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.collaboration_models import ReportSubmitRequest
from scripts import run_v04_13_codex_builder as runner
from scripts import run_v04_13_codex_builder_child as child


DIGEST = "sha256:" + "a" * 64


def test_dry_run_keeps_daemon_formal_build_and_models_out(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        runner.main(
            [
                "--state-root",
                str(tmp_path),
                "plan",
                "--seed",
                "debug",
            ]
        )
        == 0
    )

    plan = json.loads(capsys.readouterr().out)
    assert plan["processes"] == [
        "controlled_host_boundary",
        "in_process_platform",
        "isolated_codex_child_when_explicitly_authorized",
    ]
    assert plan["platform_model_egress_enabled"] is False
    assert plan["builder_actor"] == "codex"
    assert plan["human_monitoring_required"] is False
    assert plan["collaboration_policy"] == "auto_forward"
    assert plan["permission_auto_expansion_enabled"] is False
    assert plan["rollout_budget_requirement"] == {
        "enforcement": "codex_cli_rollout_budget",
        "limit_tokens": 1_000_000,
        "maximum_allowed_limit_tokens": 1_000_000,
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
    assert plan["cumulative_rollout_budget_enforcement"] == {
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
    }
    assert plan["subscription_cost_support"] == (
        "unsupported_no_realtime_usd_meter"
    )
    assert plan["realtime_cost_limit_usd"] is None
    assert plan["repair_cycle_required_order"] == list(runner.REPAIR_PHASES)
    assert any("daemon" in item for item in plan["forbidden_routes"])
    assert any("formal-build" in item for item in plan["forbidden_routes"])
    assert any("model" in item for item in plan["forbidden_routes"])


def test_platform_settings_scrub_inherited_model_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-enter-settings")
    secrets_state = {
        "platform_api_token": "platform-" + "p" * 40,
        "platform_envelope_key": "envelope-" + "e" * 40,
        "collaboration_developer_token": "developer-" + "d" * 40,
        "collaboration_verifier_token": "verifier-" + "v" * 40,
        "formal_hidden_seed_key": "hidden-" + "h" * 40,
        "collaborative_development_signing_key": "signing-" + "s" * 40,
    }

    settings = runner._platform_settings(
        tmp_path,
        secrets_state,
        platform_port=19000,
    )

    assert settings.model_egress_enabled is False
    assert settings.deepseek_api_key is None
    assert settings.lilies_local_agent_enabled is True
    assert settings.lilies_collaboration_enabled is True
    assert settings.lilies_autonomous_collaboration_enabled is True
    assert settings.lilies_local_builder_default is False


def test_host_setup_reuses_only_controlled_environment_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_environment_command(
        _state_root: Path,
        *arguments: str,
        environment: object,
    ) -> None:
        assert environment == {"SAFE": "1"}
        observed.append(arguments)

    monkeypatch.setattr(
        runner.enterprise_runner,
        "_environment_command",
        fake_environment_command,
    )

    runner._prepare_host_environment(
        tmp_path,
        seed="101",
        inherited_environment={"SAFE": "1"},
    )

    assert observed == [
        ("config",),
        ("reset", "--confirm-task-id", runner.TASK_ID),
        ("up",),
        ("initialize",),
        ("seed", "--seed", "101"),
        ("snapshot", "--seed", "101", "--phase", "baseline"),
    ]


def test_host_resume_only_starts_existing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_environment_command(
        _state_root: Path,
        *arguments: str,
        environment: object,
    ) -> None:
        assert environment == {"SAFE": "1"}
        observed.append(arguments)

    monkeypatch.setattr(
        runner.enterprise_runner,
        "_environment_command",
        fake_environment_command,
    )

    runner._resume_host_environment(
        tmp_path,
        inherited_environment={"SAFE": "1"},
    )

    assert observed == [("up",)]


@pytest.mark.asyncio
async def test_existing_handoff_fails_before_environment_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = runner._handoff_path(tmp_path, "debug")
    handoff.parent.mkdir(parents=True)
    handoff.write_text("already prepared", encoding="utf-8")
    handoff.chmod(0o600)

    def forbidden_secrets(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("preflight conflict must happen before environment state")

    monkeypatch.setattr(runner.enterprise_runner, "_runner_secrets", forbidden_secrets)
    with pytest.raises(runner.CodexBuilderRunnerError, match="already exists"):
        await runner._serve_and_bootstrap(
            Namespace(
                state_root=tmp_path,
                seed="debug",
                skip_environment_prepare=False,
                codex_timeout_seconds=60,
                keep_platform_after_codex=False,
                launch_codex=False,
            )
        )


def test_bootstrap_request_is_isolated_idempotent_and_private(tmp_path: Path) -> None:
    application_id = str(uuid4())

    first = runner._bootstrap_request(
        state_root=tmp_path,
        seed="202",
        application_id=application_id,
    )
    replay = runner._bootstrap_request(
        state_root=tmp_path,
        seed="202",
        application_id=application_id,
    )

    assert replay == first
    assert first.builder_actor == "codex"
    assert str(first.application_id) == application_id
    assert first.assignment_id != first.build_id
    assert first.build_id != first.session_id
    assert first.handoff_path.is_absolute()
    assert first.handoff_path.parent.name == "handoffs"


def test_owner_projection_and_lifecycle_drop_secret_fields(tmp_path: Path) -> None:
    receipt = {
        "schema_version": "1.0",
        "builder_actor": "codex",
        "assignment_id": str(uuid4()),
        "application_id": str(uuid4()),
        "build_id": str(uuid4()),
        "session_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "task_credential_ref": "platform-task-credential:opaque",
        "collaboration_credential_ref": "collaboration_opaque",
        "workspace_manifest_digest": DIGEST,
        "workspace_policy_digest": DIGEST,
        "handoff_path": str(tmp_path / "handoff.json"),
        "handoff_digest": DIGEST,
        "access_token": "must-not-persist",
        "nested_secret": {"token": "must-not-persist"},
    }

    projected = runner._safe_bootstrap_projection(receipt)
    runner._record_lifecycle(
        tmp_path,
        "debug",
        "external_builder_bootstrapped",
        **receipt,
    )

    assert "access_token" not in projected
    assert "nested_secret" not in projected
    assert projected["workspace_manifest_digest"] == DIGEST
    event_path = runner._lifecycle_path(tmp_path, "debug")
    payload = event_path.read_text(encoding="utf-8")
    assert "must-not-persist" not in payload
    assert stat.S_IMODE(event_path.stat().st_mode) == 0o600


def test_bootstrap_state_initializes_empty_three_phase_repair_ledger(
    tmp_path: Path,
) -> None:
    receipt = {
        "schema_version": "1.0",
        "builder_actor": "codex",
        "assignment_id": str(uuid4()),
        "application_id": str(uuid4()),
        "build_id": str(uuid4()),
        "session_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "handoff_path": str(tmp_path / "handoffs" / "codex.json"),
        "handoff_digest": DIGEST,
        "formal_archive_supported": False,
    }
    state_path = runner._write_bootstrap_state(
        tmp_path,
        seed="101",
        receipt=receipt,
        owner_urls={"application_studio_url": "http://127.0.0.1/app"},
        package={
            "public_summary_digest": DIGEST,
            "sealed_package_digest": DIGEST,
        },
    )

    state = json.loads(state_path.read_bytes())
    ledger_path = Path(state["repair_cycle_ledger"])
    ledger = json.loads(ledger_path.read_bytes())
    assert ledger["required_order"] == list(runner.REPAIR_PHASES)
    assert ledger["cycles"] == []
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600


def test_repair_cycle_enforces_report_enablement_same_project_rerun(
    tmp_path: Path,
) -> None:
    report_session = str(uuid4())
    enablement_session = str(uuid4())
    rerun_session = str(uuid4())

    first = runner._record_repair_phase(
        tmp_path,
        seed="303",
        cycle_id="cycle-001",
        project_id="project-04",
        phase="builder_report",
        session_id=report_session,
        record_ref="reports/cycle-001.json",
        record_digest=DIGEST,
        outcome="capability_gap",
    )
    second = runner._record_repair_phase(
        tmp_path,
        seed="303",
        cycle_id="cycle-001",
        project_id="project-04",
        phase="development_enablement",
        session_id=enablement_session,
        record_ref="enablements/cycle-001.json",
        record_digest=DIGEST,
        outcome="implemented_and_verified",
    )
    third = runner._record_repair_phase(
        tmp_path,
        seed="303",
        cycle_id="cycle-001",
        project_id="project-04",
        phase="same_project_rerun",
        session_id=rerun_session,
        record_ref="reruns/cycle-001.json",
        record_digest=DIGEST,
        outcome="passed",
    )

    assert first["next_phase"] == "development_enablement"
    assert second["next_phase"] == "same_project_rerun"
    assert third["complete"] is True
    ledger_path = runner._repair_ledger_path(tmp_path, "303")
    ledger = json.loads(ledger_path.read_bytes())
    assert [item["phase"] for item in ledger["cycles"][0]["segments"]] == list(runner.REPAIR_PHASES)
    assert {item["project_id"] for item in ledger["cycles"][0]["segments"]} == {"project-04"}
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600


def test_repair_cycle_rejects_out_of_order_or_cross_project_records(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="begin with a Builder report",
    ):
        runner._record_repair_phase(
            tmp_path,
            seed="debug",
            cycle_id="cycle-002",
            project_id="project-02",
            phase="development_enablement",
            session_id=str(uuid4()),
            record_ref="enablement.json",
            record_digest=DIGEST,
            outcome="implemented",
        )

    runner._record_repair_phase(
        tmp_path,
        seed="debug",
        cycle_id="cycle-002",
        project_id="project-02",
        phase="builder_report",
        session_id=str(uuid4()),
        record_ref="report.json",
        record_digest=DIGEST,
        outcome="capability_gap",
    )
    with pytest.raises(runner.CodexBuilderRunnerError, match="cannot change project"):
        runner._record_repair_phase(
            tmp_path,
            seed="debug",
            cycle_id="cycle-002",
            project_id="project-03",
            phase="development_enablement",
            session_id=str(uuid4()),
            record_ref="enablement.json",
            record_digest=DIGEST,
            outcome="implemented",
        )


@pytest.mark.asyncio
async def test_harness_usage_is_attributed_to_external_codex_builder() -> None:
    calls: list[tuple[str, object]] = []

    class Harness:
        async def start_task(self, task_id: str, **kwargs: object) -> object:
            calls.append(("start", {"task_id": task_id, **kwargs}))
            return {}

        async def record_usage(
            self,
            task_id: str,
            usage_type: str,
            **kwargs: object,
        ) -> object:
            calls.append(
                (
                    "usage",
                    {"task_id": task_id, "usage_type": usage_type, **kwargs},
                )
            )
            return {}

        async def record_model_usage(
            self,
            task_id: str,
            usage: object,
            **kwargs: object,
        ) -> object:
            calls.append(
                (
                    "model_usage",
                    {"task_id": task_id, "usage": usage, **kwargs},
                )
            )
            return {}

        async def finish_task(self, task_id: str, **kwargs: object) -> object:
            calls.append(("finish", {"task_id": task_id, **kwargs}))
            return {}

    services = Namespace(harness=Harness())
    receipt = {
        "assignment_id": str(uuid4()),
        "application_id": str(uuid4()),
        "session_id": str(uuid4()),
    }
    task_id = await runner._start_codex_harness_task(
        services,
        receipt=receipt,
        seed="101",
        model="gpt-test",
    )
    await runner._finish_codex_harness_task(
        services,
        task_id=task_id,
        receipt=receipt,
        model="gpt-test",
        result={
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 5,
                "output_tokens": 20,
                "reasoning_output_tokens": 7,
            },
            "transcript_digest": DIGEST,
        },
        succeeded=True,
    )

    assert [name for name, _ in calls] == [
        "start",
        "usage",
        "model_usage",
        "finish",
    ]
    start = calls[0][1]
    assert isinstance(start, dict)
    assert start["kind"] == "builder_build"
    assert start["metadata"]["phase"] == "external_codex_builder"
    model_usage = calls[2][1]
    assert isinstance(model_usage, dict)
    usage = model_usage["usage"]
    assert usage.input_tokens == 100
    assert usage.cache_read_input_tokens == 40
    assert usage.cost_source == "unsupported"
    assert model_usage["provider"] == "openai-codex-cli"


def test_child_public_manual_and_transcript_exclude_reasoning_and_bearers(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "handoff.json"
    task_token = "lpt_" + "t" * 80
    collaboration_token = "collaboration-" + "c" * 48
    transcript = b"\n".join(
        [
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "thread-public",
                }
            ).encode(),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "reasoning",
                        "text": "private chain of thought",
                    },
                }
            ).encode(),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": f"used {task_token} and {collaboration_token}",
                    },
                }
            ).encode(),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 9,
                        "cached_input_tokens": 3,
                        "output_tokens": 2,
                    },
                }
            ).encode(),
        ]
    )

    safe = child._safe_codex_transcript(
        transcript,
        redactions=(task_token, collaboration_token),
        handoff_path=handoff,
    )
    manual = child._public_api_manual()

    assert b"private chain of thought" not in safe
    assert task_token.encode() not in safe
    assert collaboration_token.encode() not in safe
    assert b"<redacted-authority>" in safe
    assert child._codex_usage(transcript) == {
        "input_tokens": 9,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 0,
        "output_tokens": 2,
        "reasoning_output_tokens": 0,
    }
    assert manual["platform"]["operation_count"] == 16
    assert (
        manual["collaboration"]["complete_platform_capability_gap"]
        .casefold()
        .find("completeness_issues=[]")
        >= 0
    )
    assert manual["authority"]["formal_archive_supported"] is False
    assert manual["authority"]["approval_mode_required"] == "auto_forward"
    assert manual["collaboration"]["endpoints"]["revise_report"].endswith("/revisions")
    template = json.loads(json.dumps(manual["collaboration"]["platform_capability_gap_template"]))
    template["idempotency_key"] = "external.codex.report.0001"
    template["report"]["report_id"] = str(uuid4())
    template["report"]["requirement_digest"] = DIGEST
    template["report"]["platform_contract_digest"] = DIGEST
    template["report"]["manuals_checked"][0]["digest"] = DIGEST
    attempted = template["report"]["attempted_routes"][0]
    attempted["attempt_id"] = str(uuid4())
    attempted["input_digest"] = DIGEST
    attempted["attempted_at"] = "2026-07-26T00:00:00Z"
    attempted["evidence_refs"][0]["digest"] = DIGEST
    attempted["evidence_refs"][0]["captured_at"] = "2026-07-26T00:00:00Z"
    template["report"]["evidence_refs"][0]["digest"] = DIGEST
    template["report"]["evidence_refs"][0]["captured_at"] = "2026-07-26T00:00:00Z"
    validated = ReportSubmitRequest.model_validate(template)
    assert validated.report.completeness_issues() == ()


def test_child_outer_seatbelt_is_the_only_runtime_enforcement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = tmp_path / "sandbox-exec"
    executable = tmp_path / "codex"
    workspace = tmp_path / "public-workspace"
    runtime = tmp_path / "runtime"
    handoff = tmp_path / "handoff.json"
    for path in (sandbox, executable, handoff):
        path.write_text("fixture", encoding="utf-8")
        path.chmod(0o700)
    workspace.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(child, "MACOS_SANDBOX", sandbox)

    command = child._sandboxed_arguments(
        executable=executable,
        codex_arguments=(
            "exec",
            "--sandbox",
            "danger-full-access",
            "-",
        ),
        public_workspace=workspace,
        handoff_path=handoff,
        runtime_root=runtime,
        provider_proxy_port=19001,
        platform_port=19002,
    )

    profile = command[2]
    write_section = profile.split("(allow file-write*", 1)[1].split(")", 1)[0]
    assert str(runtime) in write_section
    assert str(workspace) not in write_section
    assert "protected" not in profile
    assert "oracle" not in profile
    assert "platform-data" not in profile
    assert '(remote ip "localhost:19001")' in profile
    assert '(remote ip "localhost:19002")' in profile
    assert command[-3:] == (
        "--sandbox",
        "danger-full-access",
        "-",
    )
