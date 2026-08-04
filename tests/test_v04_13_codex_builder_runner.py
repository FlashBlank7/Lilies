from __future__ import annotations

import hashlib
import hmac
import json
import stat
from argparse import Namespace
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from agent_platform.collaboration_models import ReportSubmitRequest
from scripts import run_v04_13_codex_builder as runner
from scripts import run_v04_13_codex_builder_child as child


DIGEST = "sha256:" + "a" * 64


def _resumable_environment(
    root: Path,
    *,
    seed: str = "debug",
    revision: int = runner.REVISION,
) -> Path:
    environment = root / "environment"
    environment.mkdir(parents=True, mode=0o700)
    values = {
        f"seed-receipts-{seed}.json": {
            "schema_version": "1.0",
            "task_id": runner.TASK_ID,
            "revision": revision,
            "seed": seed,
            "record_count": 1,
            "records": [{"record_id": "PUB-001"}],
        },
        f"host-snapshot-{seed}-baseline.json": {
            "schema_version": "1.0",
            "task_id": runner.TASK_ID,
            "revision": revision,
            "seed": seed,
            "phase": "baseline",
            "record_count": 1,
            "records": [{"record_id": "PUB-001"}],
        },
        "secrets.json": {
            "schema_version": "1.0",
            "task_id": runner.TASK_ID,
            "attestation_secret": "attestation",
        },
        "credentials.json": {
            "schema_version": "1.0",
            "task_id": runner.TASK_ID,
            "paperless_builder_token": "paperless-builder",
            "inventree_builder_token": "inventree-builder",
            "paperless_verifier_token": "paperless-verifier",
            "inventree_verifier_token": "inventree-verifier",
        },
    }
    for name, value in values.items():
        runner.enterprise_runner._atomic_private_json(
            environment / name,
            value,
        )
    return environment


def _public_package_revisions(root: Path) -> Path:
    for revision in (runner.REVISION - 1, runner.REVISION):
        public_inputs = (
            root
            / str(revision)
            / "fixtures"
            / "public-inputs"
        )
        public_inputs.mkdir(parents=True)
        (public_inputs / "records.json").write_text(
            '{"records":["PUB-001"]}',
            encoding="utf-8",
        )
    return root / str(runner.REVISION)


def _bound_handoff_state(
    tmp_path: Path,
    *,
    revision: int,
) -> tuple[Path, dict]:
    handoff = runner._revision_handoff_path(
        tmp_path,
        "debug",
        revision,
    )
    handoff.parent.mkdir(parents=True)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat()
    identities = {
        "run_id": f"formal-run:{uuid4()}",
        "assignment_id": str(uuid4()),
        "application_id": str(uuid4()),
        "build_id": str(uuid4()),
        "session_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "environment_instance_id": (
            f"{runner.TASK_ID.lower()}:r{revision}:seed-debug"
        ),
        "channel_id": str(uuid4()),
    }
    task_credential_ref = f"platform-task-credential:{uuid4()}"
    collaboration_credential_ref = f"collaboration:{uuid4()}"
    payload = {
        "schema_version": "1.0",
        "builder_actor": "codex",
        "formal_archive_supported": True,
        "task": {
            "task_id": runner.TASK_ID,
            "revision": revision,
            "run_id": identities["run_id"],
        },
        "assignment": {
            "assignment_id": identities["assignment_id"],
            "application_id": identities["application_id"],
            "build_id": identities["build_id"],
            "session_id": identities["session_id"],
            "connection_id": identities["connection_id"],
            "environment_instance_id": identities[
                "environment_instance_id"
            ],
            "bundle_digest": DIGEST,
        },
        "workspace": {
            "path": str(tmp_path / "workspace"),
            "manifest_digest": DIGEST,
            "policy_digest": DIGEST,
        },
        "platform": {
            "base_url": "http://127.0.0.1:18121",
            "contract_url": "/api/v1/lilies/platform-contract",
            "contract_digest": DIGEST,
            "credential_ref": task_credential_ref,
            "access_token": "private-task-authority",
            "expires_at": expires_at,
        },
        "collaboration": {
            "base_url": "http://127.0.0.1:18121",
            "channel_id": identities["channel_id"],
            "credential_ref": collaboration_credential_ref,
            "access_token": "private-collaboration-authority",
            "expires_at": expires_at,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    handoff.write_bytes(encoded)
    handoff.chmod(0o600)
    state = {
        "task_id": runner.TASK_ID,
        "revision": revision,
        "seed": "debug",
        "builder_actor": "codex",
        "bootstrap": {
            "schema_version": "1.0",
            "builder_actor": "codex",
            "task_id": runner.TASK_ID,
            "revision": revision,
            **identities,
            "task_credential_ref": task_credential_ref,
            "collaboration_credential_ref": (
                collaboration_credential_ref
            ),
            "contract_digest": DIGEST,
            "assignment_bundle_digest": DIGEST,
            "workspace_manifest_digest": DIGEST,
            "workspace_policy_digest": DIGEST,
            "expires_at": expires_at,
            "handoff_path": str(handoff),
            "handoff_digest": (
                f"sha256:{hashlib.sha256(encoded).hexdigest()}"
            ),
            "formal_archive_supported": True,
        },
    }
    return handoff, state


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
    assert plan["external_codex_model_launch"] == (
        "requires both fresh --launch-codex and "
        "--authorize-external-codex-token-spend flags, and is impossible "
        "while the state-root EXTERNAL_CODEX_SPEND_DISABLED sentinel exists"
    )
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
        "enforcement": ("runner_persisted_reported_usage_remaining_budget"),
        "cumulative_limit_tokens": 1_000_000,
        "weighted_token_formula": ("output_tokens+max(0,input_tokens-cache_read_input_tokens)"),
        "token_weights": {
            "sampling": 1.0,
            "prefill": 1.0,
        },
        "prior_usage_requirement": "reported_only_fail_closed",
        "cumulative_reported_weighted_tokens": 0,
        "remaining_tokens": 1_000_000,
        "next_invocation_cli_limit_tokens": 1_000_000,
    }
    assert plan["subscription_cost_support"] == ("unsupported_no_realtime_usd_meter")
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
    assert settings.lilies_formal_public_guidance_path == (
        runner.PUBLIC_BUILDER_GUIDANCE
    )
    assert settings.lilies_formal_public_guidance_path.is_file()


def test_public_builder_guidance_is_generic_and_covers_operating_rules() -> None:
    guidance = runner.PUBLIC_BUILDER_GUIDANCE.read_text(encoding="utf-8")
    required = (
        "workspace.path",
        ".lilies-mount-manifest.json",
        "byte size, and SHA-256 digest",
        "connector descriptors",
        "node, branch, port, reference, and terminal path",
        "`[REDACTED]`",
        "independent, order-safe, and safe to run concurrently",
        "restore a measured zero baseline",
        "persist after compensation or deletion",
        "Reserve enough mutation capacity",
        "exact canonical mutation identity",
        "An exact replay reuses the same key",
        "different payload uses a different deterministic key",
        "conflicts separately from permission denials",
        "Excel serial date",
        "`attempt_count` is greater than one",
        "Stop on a new error category",
        "Do not repeat speculative edits",
    )
    assert all(item in guidance for item in required)
    forbidden = (
        "EXP-LILIES",
        "Paperless",
        "InvenTree",
        "ThingsBoard",
        "http://",
        "https://",
        "/api/",
        "/Users/",
        "/private/",
        "oracle",
        "protected",
        "expected-vs-actual",
        "final graph",
        ".git",
        "source code",
        "platform database",
    )
    assert not any(item.casefold() in guidance.casefold() for item in forbidden)


def test_managed_attestation_rejects_an_exited_boundary() -> None:
    class ExitedProcess:
        @staticmethod
        def poll() -> int:
            return 1

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="managed environment boundary exited",
    ):
        runner._wait_for_managed_attestation(
            ExitedProcess(),
            attestation_secret="s" * 48,
            timeout_seconds=1,
        )


def test_managed_attestation_requires_signed_stable_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 48

    class RunningProcess:
        @staticmethod
        def poll() -> None:
            return None

    class Response:
        status = 200

        def __init__(self, signature: str) -> None:
            self.headers = {
                "X-Lilies-Environment-Attestation": signature,
            }

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{"identity":"exp-lilies-001-r7-real-hosts"}'

    def signed_response(request: object, *, timeout: int) -> Response:
        assert timeout == 2
        challenge = request.get_header(  # type: ignore[attr-defined]
            "X-lilies-attestation-challenge"
        )
        signature = (
            "sha256:"
            + hmac.new(
                secret.encode("utf-8"),
                challenge.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        return Response(signature)

    monkeypatch.setattr(runner, "urlopen", signed_response)

    runner._wait_for_managed_attestation(
        RunningProcess(),
        attestation_secret=secret,
        timeout_seconds=2,
    )


def test_host_setup_reuses_only_controlled_environment_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_environment_command(
        environment_state_root: Path,
        *arguments: str,
        inherited_environment: object,
    ) -> None:
        assert environment_state_root == tmp_path
        assert inherited_environment == {"SAFE": "1"}
        observed.append(arguments)

    monkeypatch.setattr(
        runner,
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
        environment_state_root: Path,
        *arguments: str,
        inherited_environment: object,
    ) -> None:
        assert environment_state_root == tmp_path
        assert inherited_environment == {"SAFE": "1"}
        observed.append(arguments)

    monkeypatch.setattr(
        runner,
        "_environment_command",
        fake_environment_command,
    )

    runner._resume_host_environment(
        tmp_path,
        inherited_environment={"SAFE": "1"},
    )

    assert observed == [("up",)]


def test_resumable_environment_requires_current_bound_identity(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(tmp_path)

    runner._validate_resumable_environment(
        environment,
        seed="debug",
    )

    drifted = _resumable_environment(
        tmp_path / "drifted",
        revision=runner.REVISION - 1,
    )
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="identity is invalid",
    ):
        runner._validate_resumable_environment(
            drifted,
            seed="debug",
        )


def test_environment_revision_adoption_is_metadata_only_and_replayable(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(
        tmp_path / "host",
        revision=runner.REVISION - 1,
    )
    current_package = _public_package_revisions(tmp_path / "packages")
    receipt_path = environment / "seed-receipts-debug.json"
    baseline_path = environment / "host-snapshot-debug-baseline.json"
    original_receipt = receipt_path.read_bytes()
    original_baseline = baseline_path.read_bytes()

    adoption = runner._adopt_environment_revision(
        environment,
        seed="debug",
        current_package_root=current_package,
    )
    replay = runner._adopt_environment_revision(
        environment,
        seed="debug",
        current_package_root=current_package,
    )

    assert replay == adoption
    assert adoption["predecessor_revision"] == runner.REVISION - 1
    assert adoption["successor_revision"] == runner.REVISION
    assert adoption["host_mutation_operations"] == []
    assert adoption["predecessor_public_inputs_tree_digest"] == (
        adoption["successor_public_inputs_tree_digest"]
    )
    assert json.loads(receipt_path.read_bytes())["revision"] == runner.REVISION
    assert json.loads(baseline_path.read_bytes())["revision"] == runner.REVISION
    transition = (
        environment
        / runner.ENVIRONMENT_REVISION_HISTORY_DIRNAME
        / (
            f"r{runner.REVISION - 1}-to-r{runner.REVISION}"
            "-seed-debug"
        )
    )
    assert (transition / receipt_path.name).read_bytes() == original_receipt
    assert (transition / baseline_path.name).read_bytes() == original_baseline
    assert stat.S_IMODE(transition.stat().st_mode) == 0o500
    assert stat.S_IMODE((transition / "adoption.json").stat().st_mode) == 0o400
    assert stat.S_IMODE(
        (transition / receipt_path.name).stat().st_mode
    ) == 0o400


def test_environment_revision_adoption_repairs_a_partial_projection(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(
        tmp_path / "host",
        revision=runner.REVISION - 1,
    )
    current_package = _public_package_revisions(tmp_path / "packages")
    runner._adopt_environment_revision(
        environment,
        seed="debug",
        current_package_root=current_package,
    )
    transition = (
        environment
        / runner.ENVIRONMENT_REVISION_HISTORY_DIRNAME
        / (
            f"r{runner.REVISION - 1}-to-r{runner.REVISION}"
            "-seed-debug"
        )
    )
    baseline_path = environment / "host-snapshot-debug-baseline.json"
    baseline_path.write_bytes(
        (transition / baseline_path.name).read_bytes()
    )
    baseline_path.chmod(0o600)

    replay = runner._adopt_environment_revision(
        environment,
        seed="debug",
        current_package_root=current_package,
    )

    assert replay["successor_revision"] == runner.REVISION
    assert json.loads(baseline_path.read_bytes())["revision"] == runner.REVISION


def test_environment_revision_adoption_rejects_public_input_drift_before_writes(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(
        tmp_path / "host",
        revision=runner.REVISION - 1,
    )
    current_package = _public_package_revisions(tmp_path / "packages")
    current_input = (
        current_package / "fixtures" / "public-inputs" / "records.json"
    )
    current_input.write_text('{"records":["CHANGED"]}', encoding="utf-8")
    original_receipt = (
        environment / "seed-receipts-debug.json"
    ).read_bytes()

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="public input tree differs",
    ):
        runner._adopt_environment_revision(
            environment,
            seed="debug",
            current_package_root=current_package,
        )

    assert (
        environment / "seed-receipts-debug.json"
    ).read_bytes() == original_receipt
    assert not (
        environment / runner.ENVIRONMENT_REVISION_HISTORY_DIRNAME
    ).exists()


def test_environment_revision_adoption_rejects_symlinked_identity(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(
        tmp_path / "host",
        revision=runner.REVISION - 1,
    )
    current_package = _public_package_revisions(tmp_path / "packages")
    receipt_path = environment / "seed-receipts-debug.json"
    outside = tmp_path / "outside-receipt.json"
    receipt_path.replace(outside)
    receipt_path.symlink_to(outside)

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="must not be a symlink",
    ):
        runner._adopt_environment_revision(
            environment,
            seed="debug",
            current_package_root=current_package,
        )


def test_environment_control_owner_is_exact_replay_and_cross_root_safe(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(tmp_path / "host")
    platform = (tmp_path / "platform").resolve()
    platform.mkdir(mode=0o700)

    runner._claim_environment_control(
        environment,
        platform_state_root=platform,
        seed="debug",
    )
    runner._claim_environment_control(
        environment,
        platform_state_root=platform,
        seed="debug",
    )

    marker = environment / runner.ENVIRONMENT_CONTROL_OWNER_FILENAME
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="another platform state root",
    ):
        runner._claim_environment_control(
            environment,
            platform_state_root=(tmp_path / "other").resolve(),
            seed="debug",
        )


def test_environment_state_root_rejects_alias_and_wrong_directory(
    tmp_path: Path,
) -> None:
    environment = _resumable_environment(tmp_path / "host")
    assert (
        runner._environment_state_root(
            (tmp_path / "platform").resolve(),
            environment,
        )
        == environment.resolve()
    )
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="controlled environment directory",
    ):
        runner._environment_state_root(
            (tmp_path / "platform").resolve(),
            tmp_path / "host",
        )

    alias = tmp_path / "environment"
    alias.symlink_to(environment, target_is_directory=True)
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="must not be a symlink",
    ):
        runner._environment_state_root(
            (tmp_path / "platform").resolve(),
            alias,
        )


def test_external_environment_can_initialize_a_fresh_platform_state() -> None:
    environment = Path("/private/example/environment")

    assert runner._create_runner_secrets_for_bootstrap(
        skip_environment_prepare=False,
        configured_environment_state_root=None,
        handoff_exists=False,
    )
    assert runner._create_runner_secrets_for_bootstrap(
        skip_environment_prepare=True,
        configured_environment_state_root=environment,
        handoff_exists=False,
    )
    assert not runner._create_runner_secrets_for_bootstrap(
        skip_environment_prepare=True,
        configured_environment_state_root=environment,
        handoff_exists=True,
    )
    assert not runner._create_runner_secrets_for_bootstrap(
        skip_environment_prepare=True,
        configured_environment_state_root=None,
        handoff_exists=False,
    )


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


@pytest.mark.asyncio
async def test_external_codex_launch_requires_fresh_token_spend_authorization(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="authorize-external-codex-token-spend",
    ):
        await runner._serve_and_bootstrap(
            Namespace(
                state_root=tmp_path,
                seed="debug",
                skip_environment_prepare=True,
                codex_timeout_seconds=60,
                keep_platform_after_codex=False,
                launch_codex=True,
                resume_codex=True,
                authorize_external_codex_token_spend=False,
            )
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_active_predecessor_retirement_flag_requires_revision_advance(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "not-created"
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="requires --advance-project-revision",
    ):
        await runner._serve_and_bootstrap(
            Namespace(
                state_root=state_root,
                seed="debug",
                skip_environment_prepare=True,
                codex_timeout_seconds=60,
                keep_platform_after_codex=False,
                launch_codex=False,
                retire_active_predecessor_authority=True,
                advance_project_revision=False,
            )
        )

    assert not state_root.exists()


@pytest.mark.asyncio
async def test_state_root_bootstrap_disable_sentinel_blocks_before_any_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / runner.BUILDER_BOOTSTRAP_DISABLED_FILENAME
    sentinel.write_text("state=disabled\n", encoding="utf-8")
    sentinel.chmod(0o600)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("bootstrap sentinel must fail before any action")

    monkeypatch.setattr(
        runner,
        "_existing_bootstrap_context",
        forbidden,
    )
    monkeypatch.setattr(
        runner.enterprise_runner,
        "_runner_secrets",
        forbidden,
    )
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="Builder bootstrap is disabled.*no command-line override",
    ):
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

    assert sentinel.read_text(encoding="utf-8") == "state=disabled\n"
    assert list(tmp_path.iterdir()) == [sentinel]


def test_state_root_bootstrap_disable_sentinel_does_not_block_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = tmp_path / runner.BUILDER_BOOTSTRAP_DISABLED_FILENAME
    sentinel.write_text("state=disabled\n", encoding="utf-8")
    sentinel.chmod(0o600)
    args = runner.build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "plan",
            "--seed",
            "debug",
        ]
    )

    assert runner._dry_run_plan(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["task_id"] == runner.TASK_ID
    assert plan["revision"] == runner.REVISION
    assert sentinel.exists()


def test_owner_setup_only_is_explicit_bootstrap_mode(
    tmp_path: Path,
) -> None:
    args = runner.build_parser().parse_args(
        [
            "--state-root",
            str(tmp_path),
            "bootstrap",
            "--seed",
            "debug",
            "--owner-setup-only",
        ]
    )

    assert args.owner_setup_only is True
    assert args.launch_codex is False


def test_owner_setup_state_reuses_only_the_exact_empty_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_id = str(uuid4())
    application = {
        "id": application_id,
        "name": runner._expected_application_name("debug"),
    }
    package = {
        "public_summary_digest": DIGEST,
        "sealed_package_digest": "sha256:" + "b" * 64,
    }
    path = runner._write_owner_setup_state(
        tmp_path,
        seed="debug",
        application=application,
        package=package,
    )
    monkeypatch.setattr(
        runner.enterprise_runner,
        "_request_json",
        lambda *_args, **_kwargs: dict(application),
    )

    reused = runner._load_owner_setup_application(
        tmp_path,
        seed="debug",
        package=package,
        platform_url="http://127.0.0.1:18125",
        platform_token="private-owner-token",
    )

    assert reused == application
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private-owner-token" not in path.read_text(encoding="utf-8")
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="does not match the frozen task",
    ):
        runner._load_owner_setup_application(
            tmp_path,
            seed="debug",
            package={
                **package,
                "sealed_package_digest": "sha256:" + "c" * 64,
            },
            platform_url="http://127.0.0.1:18125",
            platform_token="private-owner-token",
        )


@pytest.mark.asyncio
async def test_token_spend_authorization_cannot_arm_an_idle_runner(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="requires --launch-codex",
    ):
        await runner._serve_and_bootstrap(
            Namespace(
                state_root=tmp_path,
                seed="debug",
                skip_environment_prepare=True,
                codex_timeout_seconds=60,
                keep_platform_after_codex=False,
                launch_codex=False,
                resume_codex=False,
                authorize_external_codex_token_spend=True,
            )
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_state_root_spend_disable_sentinel_has_no_cli_override(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "EXTERNAL_CODEX_SPEND_DISABLED"
    sentinel.write_text("state=disabled\n", encoding="utf-8")
    sentinel.chmod(0o600)

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="there is no command-line override",
    ):
        await runner._serve_and_bootstrap(
            Namespace(
                state_root=tmp_path,
                seed="debug",
                skip_environment_prepare=True,
                codex_timeout_seconds=60,
                keep_platform_after_codex=True,
                launch_codex=True,
                resume_codex=True,
                authorize_external_codex_token_spend=True,
            )
        )

    assert sentinel.read_text(encoding="utf-8") == "state=disabled\n"
    assert list(tmp_path.iterdir()) == [sentinel]


@pytest.mark.asyncio
async def test_spend_disable_sentinel_is_rechecked_at_child_launch(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / runner.EXTERNAL_CODEX_SPEND_DISABLED_FILENAME
    sentinel.write_text("state=disabled\n", encoding="utf-8")
    sentinel.chmod(0o600)

    with (
        ExitStack() as stack,
        pytest.raises(
            runner.CodexBuilderRunnerError,
            match="there is no command-line override",
        ),
    ):
        await runner._launch_codex_child(
            stack,
            state_root=tmp_path,
            seed="debug",
            handoff_path=tmp_path / "handoff.json",
            model="gpt-5",
            timeout_seconds=60,
            inherited_environment={},
        )

    assert list(tmp_path.iterdir()) == [sentinel]


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


def test_task_token_derivation_is_stable_private_and_credential_bound() -> None:
    first_id = uuid4()
    second_id = uuid4()
    factory = runner._deterministic_task_token_factory("k" * 48)
    replay = runner._deterministic_task_token_factory("k" * 48)
    rotated = runner._deterministic_task_token_factory("r" * 48)

    first = factory(first_id)

    assert first == replay(first_id)
    assert first != factory(second_id)
    assert first != rotated(first_id)
    assert first.startswith(f"lpt_{first_id.hex}_")
    assert "k" * 32 not in first


def test_bootstrap_request_accepts_a_distinct_revision_handoff_path(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    target = runner._revision_handoff_path(
        tmp_path,
        "202",
        runner.REVISION,
    )

    request = runner._bootstrap_request(
        state_root=tmp_path,
        seed="202",
        application_id=application_id,
        handoff_path=target,
    )

    assert request.handoff_path == target
    assert request.handoff_path != runner._handoff_path(tmp_path, "202")
    assert f"r{runner.REVISION}" in request.handoff_path.name


def test_existing_bootstrap_context_accepts_private_revision_handoff(
    tmp_path: Path,
) -> None:
    handoff, state = _bound_handoff_state(
        tmp_path,
        revision=runner.REVISION,
    )
    state_path = tmp_path / "codex-builder-seed-debug.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o600)

    context = runner._existing_bootstrap_context(tmp_path, "debug")

    assert context is not None
    assert context[0] == state_path
    assert context[3] == handoff


def test_existing_bootstrap_context_rejects_handoff_digest_drift(
    tmp_path: Path,
) -> None:
    handoff, state = _bound_handoff_state(
        tmp_path,
        revision=runner.REVISION,
    )
    state_path = tmp_path / "codex-builder-seed-debug.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_path.chmod(0o600)
    handoff.write_bytes(handoff.read_bytes() + b"\n")

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="digest does not match owner state",
    ):
        runner._existing_bootstrap_context(tmp_path, "debug")


def test_existing_bootstrap_context_rejects_symlinked_handoff_parent(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-handoff-root"
    outside.mkdir()
    (tmp_path / "handoffs").symlink_to(outside, target_is_directory=True)
    handoff = outside / (
        f"codex-builder-r{runner.REVISION}-seed-debug.json"
    )
    handoff.write_text("private authority", encoding="utf-8")
    handoff.chmod(0o600)
    state_path = tmp_path / "codex-builder-seed-debug.json"
    state_path.write_text(
        json.dumps(
            {
                "task_id": runner.TASK_ID,
                "revision": runner.REVISION,
                "seed": "debug",
                "bootstrap": {
                    "revision": runner.REVISION,
                    "handoff_path": str(
                        tmp_path / "handoffs" / handoff.name
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="handoff boundary is unsafe",
    ):
        runner._existing_bootstrap_context(tmp_path, "debug")


def test_existing_bootstrap_context_rejects_handoff_outside_state_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-handoff.json"
    outside.write_text("private authority", encoding="utf-8")
    outside.chmod(0o600)
    state_path = tmp_path / "codex-builder-seed-debug.json"
    state_path.write_text(
        json.dumps(
            {
                "task_id": runner.TASK_ID,
                "revision": runner.REVISION,
                "seed": "debug",
                "bootstrap": {
                    "revision": runner.REVISION,
                    "handoff_path": str(outside),
                },
            }
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="handoff boundary is unsafe",
    ):
        runner._existing_bootstrap_context(tmp_path, "debug")


def test_revision_owner_state_finalization_is_exactly_replayable() -> None:
    receipt = {
        "revision": runner.REVISION,
        "assignment_id": str(uuid4()),
        "handoff_digest": DIGEST,
    }
    predecessor = {
        "revision": runner.REVISION - 1,
        "status": "retired_revision_history_snapshot",
        "environment_revision_adoption": {
            "adoption_receipt_digest": DIGEST,
        },
        "authority_retirement": {
            "task_credential_ref": "platform-task-credential:history",
            "collaboration_channel_id": str(uuid4()),
        },
    }
    package = {
        "public_summary_digest": DIGEST,
        "sealed_package_digest": "sha256:" + "b" * 64,
    }
    initial = {
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION - 1,
        "bootstrap": {"revision": runner.REVISION - 1},
        "codex_execution": {"invocations": 6},
    }

    finalized, advanced = runner._finalize_revision_owner_state(
        initial,
        safe_receipt=receipt,
        package=package,
        advanced_from=predecessor,
    )
    replay, replayed = runner._finalize_revision_owner_state(
        finalized,
        safe_receipt=receipt,
        package=package,
        advanced_from=predecessor,
    )

    assert advanced is True
    assert replayed is False
    assert replay == finalized
    assert finalized["revision"] == runner.REVISION
    assert finalized["bootstrap"] == receipt
    assert finalized["project_revision_history"] == [predecessor]
    assert finalized["project_revision_history"][0][
        "environment_revision_adoption"
    ]["adoption_receipt_digest"] == DIGEST
    assert "authority_retirement" in finalized["project_revision_history"][0]
    assert finalized["last_execution_revision"] == runner.REVISION - 1


def test_revision_owner_state_replay_rejects_another_handoff() -> None:
    receipt = {
        "revision": runner.REVISION,
        "assignment_id": str(uuid4()),
        "handoff_digest": DIGEST,
    }
    finalized = {
        "revision": runner.REVISION,
        "bootstrap": dict(receipt),
    }
    conflict = {
        **receipt,
        "assignment_id": str(uuid4()),
    }

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="conflicts with the replayed handoff",
    ):
        runner._finalize_revision_owner_state(
            finalized,
            safe_receipt=conflict,
            package={},
            advanced_from={"revision": runner.REVISION - 1},
        )


def _predecessor_authority_fixture() -> tuple[dict, Namespace, list[str]]:
    assignment_id = uuid4()
    session_id = uuid4()
    application_id = uuid4()
    predecessor_revision = runner.REVISION - 1
    channel_id = uuid5(
        NAMESPACE_URL,
        "lilies:collaboration:"
        f"{runner.TASK_ID}:{predecessor_revision}:{assignment_id}",
    )
    activation_key = f"formal.channel.activate.{assignment_id.hex}"
    collaboration_credential_id = uuid5(
        NAMESPACE_URL,
        "lilies:collaboration-credential:"
        f"{channel_id}:{activation_key}",
    )
    task_credential_ref = f"platform-task-credential:{uuid4()}"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    calls: list[str] = []
    task_record = Namespace(
        credential_ref=task_credential_ref,
        assignment_id=assignment_id,
        session_id=session_id,
        application_ids=[application_id],
        expires_at=expires_at,
        revoked_at=None,
    )
    channel = {
        "task_id": runner.TASK_ID,
        "task_revision": predecessor_revision,
        "channel_id": str(channel_id),
        "assignment_id": str(assignment_id),
        "lilies_session_id": str(session_id),
        "application_ids": [str(application_id)],
        "status": "active",
        "closed_at": None,
    }

    class Auth:
        async def get_credential(self, credential_ref: str) -> Namespace:
            calls.append("task.get")
            assert credential_ref == task_credential_ref
            return task_record

        async def revoke_credential(
            self,
            credential_ref: str,
            *,
            reason: str,
        ) -> Namespace:
            calls.append("task.revoke")
            assert credential_ref == task_credential_ref
            assert reason == runner.PREDECESSOR_RETIREMENT_REASON
            if task_record.revoked_at is None:
                task_record.revoked_at = datetime.now(timezone.utc)
            return task_record

    class Store:
        async def get_channel(self, target: str) -> dict:
            calls.append("channel.get")
            assert target == str(channel_id)
            return dict(channel)

        async def revoke_credential(
            self,
            target: UUID,
            reason: str,
        ) -> dict:
            calls.append("collaboration.revoke")
            assert target == collaboration_credential_id
            assert reason == runner.PREDECESSOR_RETIREMENT_REASON
            return {
                "credential_id": str(collaboration_credential_id),
                "channel_id": str(channel_id),
                "assignment_id": str(assignment_id),
                "lilies_session_id": str(session_id),
                "revoked_at": "2026-07-26T00:00:00+00:00",
            }

        async def close_formal_channel_boundary(
            self,
            **kwargs: object,
        ) -> dict:
            calls.append("channel.close")
            assert kwargs["task_revision"] == predecessor_revision
            channel["status"] = "closed"
            channel["closed_at"] = "2026-07-26T00:00:01+00:00"
            return dict(channel)

    bootstrap = {
        "revision": predecessor_revision,
        "assignment_id": str(assignment_id),
        "session_id": str(session_id),
        "application_id": str(application_id),
        "channel_id": str(channel_id),
        "task_credential_ref": task_credential_ref,
        "collaboration_credential_ref": (
            f"collaboration_{collaboration_credential_id.hex}"
        ),
        "expires_at": expires_at.isoformat(),
    }
    services = Namespace(
        platform_blackbox_auth=Auth(),
        collaboration=Namespace(store=Store()),
    )
    return bootstrap, services, calls


@pytest.mark.asyncio
async def test_active_predecessor_authority_requires_explicit_retirement() -> None:
    bootstrap, services, calls = _predecessor_authority_fixture()

    with pytest.raises(
        runner.CodexBuilderRunnerError,
        match="retire-active-predecessor-authority",
    ):
        await runner._retire_predecessor_authority(
            services,
            bootstrap=bootstrap,
            allow_active=False,
        )

    assert calls == ["task.get", "channel.get"]


@pytest.mark.asyncio
async def test_predecessor_authority_retirement_is_exact_and_ordered() -> None:
    bootstrap, services, calls = _predecessor_authority_fixture()

    receipt = await runner._retire_predecessor_authority(
        services,
        bootstrap=bootstrap,
        allow_active=True,
    )

    assert calls == [
        "task.get",
        "channel.get",
        "task.revoke",
        "collaboration.revoke",
        "channel.close",
    ]
    assert receipt["predecessor_revision"] == runner.REVISION - 1
    assert receipt["successor_revision"] == runner.REVISION
    assert receipt["task_credential_ref"] == bootstrap[
        "task_credential_ref"
    ]
    assert receipt["collaboration_channel_id"] == bootstrap["channel_id"]
    assert receipt["active_predecessor_retirement_authorized"] is True


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
        "formal_archive_supported": True,
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
    stale_task_token = "lpt_" + "s" * 64
    stale_collaboration_token = "lcc_" + "r" * 72
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
                        "text": (
                            f"used {task_token} and {collaboration_token}; "
                            f"stale {stale_task_token} and "
                            f"Bearer {stale_collaboration_token}"
                        ),
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
    assert stale_task_token.encode() not in safe
    assert stale_collaboration_token.encode() not in safe
    assert b"<redacted-authority>" in safe
    assert child._codex_usage(transcript) == {
        "input_tokens": 9,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 0,
        "output_tokens": 2,
        "reasoning_output_tokens": 0,
    }
    assert manual["platform"]["operation_count"] == 17
    assert (
        manual["collaboration"]["complete_platform_capability_gap"]
        .casefold()
        .find("completeness_issues=[]")
        >= 0
    )
    assert manual["authority"]["formal_archive_supported"] is True
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
