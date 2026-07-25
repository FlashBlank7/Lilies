from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_platform.collaboration_qualification import (
    QualificationSurfaceResult,
    canonical_digest,
    qualification_source_revision,
)
from scripts.run_v04_13_live_development_handoff import (
    _AllowlistedConnectProxy,
    _codex_jsonl_summary,
    _communicate_codex_with_cancellation,
    _prepare_isolated_codex_identity,
    _proposal_is_exact_arithmetic_repair,
    _provider_proxy_observations_stay_fenced,
    _normalized_live_review_verdict,
    _sandboxed_codex_argv,
)


ROOT = Path(__file__).resolve().parents[1]
BUGGY_SOURCE = (
    'def add(left: int, right: int) -> int:\n'
    '    """Return the sum of two integers."""\n'
    "    return left - right\n"
)


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _assert_bound(payload: dict) -> None:
    unsigned = {
        key: value for key, value in payload.items() if key != "evidence_digest"
    }
    assert payload["evidence_digest"] == canonical_digest(unsigned)


def test_codex_provider_boundary_denies_unknown_host_and_binds_usage(
    tmp_path: Path,
) -> None:
    with _AllowlistedConnectProxy(("allowed.example",)) as proxy:
        proxy_port = proxy.port
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
            client.sendall(
                b"CONNECT denied.example:443 HTTP/1.1\r\n"
                b"Host: denied.example:443\r\n\r\n"
            )
            response = client.recv(4_096)
        observations = proxy.observations()
    assert b"403 Forbidden" in response
    assert observations == [
        {
            "method": "CONNECT",
            "host": "denied.example",
            "port": 443,
            "allowed": False,
            "client_to_provider_bytes": 0,
            "provider_to_client_bytes": 0,
        }
    ]
    assert _provider_proxy_observations_stay_fenced(
        [
            {
                "host": "chatgpt.com",
                "allowed": True,
                "upstream_connected": True,
                "client_to_provider_bytes": 10,
                "provider_to_client_bytes": 20,
            },
            observations[0],
        ]
    )
    assert not _provider_proxy_observations_stay_fenced(
        [
            {
                "host": "denied.example",
                "allowed": False,
                "upstream_connected": True,
                "client_to_provider_bytes": 1,
                "provider_to_client_bytes": 0,
            }
        ]
    )

    stdout = b"\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": '{"old_string":"a","new_string":"b","summary":"ok"}',
                    },
                }
            ).encode(),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 2,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 4,
                        "reasoning_output_tokens": 0,
                    },
                }
            ).encode(),
        )
    )
    message, summary = _codex_jsonl_summary(stdout)
    assert message.startswith('{"old_string"')
    assert summary["command_count"] == 0
    assert summary["file_or_external_tool_events"] == 0
    assert summary["usage"]["input_tokens"] == 10
    assert summary["usage"]["output_tokens"] == 4
    assert _proposal_is_exact_arithmetic_repair(
        {
            "old_string": "    return left - right\n",
            "new_string": "    return left + right\n",
        },
        source_text=BUGGY_SOURCE,
    )
    assert _proposal_is_exact_arithmetic_repair(
        {
            "old_string": BUGGY_SOURCE,
            "new_string": BUGGY_SOURCE.replace(
                "    return left - right\n",
                "    return left + right\n",
            ),
        },
        source_text=BUGGY_SOURCE,
    )
    assert _proposal_is_exact_arithmetic_repair(
        {
            "old_string": BUGGY_SOURCE,
            "new_string": BUGGY_SOURCE.replace(
                "    return left - right\n",
                "    return left + right\n",
            ),
        },
        source_text=BUGGY_SOURCE.rstrip("\n"),
    )
    assert not _proposal_is_exact_arithmetic_repair(
        {
            "old_string": BUGGY_SOURCE,
            "new_string": BUGGY_SOURCE.replace(
                '"""Return the sum of two integers."""',
                '"""Changed unrelated text."""',
            ).replace(
                "    return left - right\n",
                "    return left + right\n",
            ),
        },
        source_text=BUGGY_SOURCE,
    )
    assert _normalized_live_review_verdict("PASS — all frozen checks passed.") == (
        "accepted"
    )
    assert _normalized_live_review_verdict("Rework: one check failed.") == "rework"
    assert _normalized_live_review_verdict("passcode") is None

    runtime_root = tmp_path / "codex-runtime"
    runtime_root.mkdir()
    sandboxed = _sandboxed_codex_argv(
        executable=Path("/usr/bin/true"),
        argv=("/usr/bin/true",),
        runtime_root=runtime_root,
        proxy_port=proxy_port,
    )
    profile = sandboxed[2]
    assert f'localhost:{proxy_port}"' in profile
    assert "(allow process-fork" not in profile
    assert str(tmp_path / "role-workspace") not in profile
    if sys.platform == "darwin":
        executed = subprocess.run(
            sandboxed,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert executed.returncode == 0, executed.stderr


def test_codex_identity_requires_private_chatgpt_subscription_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_source = tmp_path / "source-codex-home"
    codex_source.mkdir(mode=0o700)
    source_auth = codex_source / "auth.json"
    source_auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "never-expose-this-token"},
            }
        ),
        encoding="utf-8",
    )
    source_auth.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(codex_source))
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)

    codex_home, user_home, metadata = _prepare_isolated_codex_identity(
        runtime_root
    )

    assert codex_home == runtime_root / "codex-home"
    assert user_home == runtime_root / "user-home"
    assert metadata == {
        "auth_mode": "chatgpt",
        "api_key_present": False,
        "tokens_present": True,
        "billing_mode": "chatgpt_subscription",
        "credential_identity": "codex-cli-subscription",
    }
    assert "never-expose-this-token" not in json.dumps(metadata)
    isolated_auth = codex_home / "auth.json"
    assert stat.S_IMODE(isolated_auth.stat().st_mode) == 0o600
    assert json.loads(isolated_auth.read_text(encoding="utf-8"))["tokens"][
        "access_token"
    ] == "never-expose-this-token"


@pytest.mark.parametrize(
    "payload",
    (
        {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": "sk-forbidden",
            "tokens": {},
        },
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": "sk-forbidden",
            "tokens": {"access_token": "token"},
        },
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {},
        },
    ),
)
def test_codex_identity_rejects_api_key_or_missing_subscription_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    codex_source = tmp_path / "source-codex-home"
    codex_source.mkdir(mode=0o700)
    source_auth = codex_source / "auth.json"
    source_auth.write_text(json.dumps(payload), encoding="utf-8")
    source_auth.chmod(0o600)
    monkeypatch.setenv("CODEX_HOME", str(codex_source))
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="ChatGPT subscription"):
        _prepare_isolated_codex_identity(runtime_root)


def test_codex_identity_rejects_symlink_and_non_private_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_source = tmp_path / "source-codex-home"
    codex_source.mkdir(mode=0o700)
    real_auth = tmp_path / "real-auth.json"
    real_auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "token"},
            }
        ),
        encoding="utf-8",
    )
    real_auth.chmod(0o600)
    (codex_source / "auth.json").symlink_to(real_auth)
    monkeypatch.setenv("CODEX_HOME", str(codex_source))
    runtime_root = tmp_path / "symlink-runtime"
    runtime_root.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="non-symlink regular file"):
        _prepare_isolated_codex_identity(runtime_root)

    (codex_source / "auth.json").unlink()
    (codex_source / "auth.json").write_bytes(real_auth.read_bytes())
    (codex_source / "auth.json").chmod(0o640)
    mode_runtime = tmp_path / "mode-runtime"
    mode_runtime.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="owner-only"):
        _prepare_isolated_codex_identity(mode_runtime)


def test_codex_jsonl_rejects_every_non_message_or_reasoning_item() -> None:
    for item_type in (
        "web_search",
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "unknown_future_tool",
    ):
        stdout = b"\n".join(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": item_type, "status": "completed"},
                    }
                ).encode(),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ).encode(),
            )
        )
        with pytest.raises(RuntimeError, match=item_type):
            _codex_jsonl_summary(stdout)


def test_codex_communication_cancellation_kills_the_process_group(
    tmp_path: Path,
) -> None:
    late_marker = tmp_path / "late-child-side-effect.txt"
    child_source = (
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(0.6)\n"
        f"Path({str(late_marker)!r}).write_text('escaped', encoding='utf-8')\n"
    )
    parent_source = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_source!r}])\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        (sys.executable, "-c", parent_source),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    cancellation = threading.Event()
    timer = threading.Timer(0.1, cancellation.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="cancelled assignment boundary"):
            _communicate_codex_with_cancellation(
                process,
                request=b"",
                timeout_seconds=10,
                cancel_event=cancellation,
            )
    finally:
        timer.cancel()
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)
    assert time.monotonic() - started < 2
    time.sleep(0.7)
    assert not late_marker.exists()


def test_codex_seatbelt_allows_only_the_bound_loopback_proxy_port(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        return

    allowed_listener = socket.socket()
    denied_listener = socket.socket()
    try:
        allowed_listener.bind(("127.0.0.1", 0))
        allowed_listener.listen()
        denied_listener.bind(("127.0.0.1", 0))
        denied_listener.listen()
        allowed_port = int(allowed_listener.getsockname()[1])
        denied_port = int(denied_listener.getsockname()[1])

        def probe(port: int) -> subprocess.CompletedProcess[str]:
            argv = ("/usr/bin/nc", "-z", "127.0.0.1", str(port))
            sandboxed = _sandboxed_codex_argv(
                executable=Path(argv[0]),
                argv=argv,
                runtime_root=tmp_path,
                proxy_port=allowed_port,
            )
            return subprocess.run(
                sandboxed,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        assert probe(allowed_port).returncode == 0
        assert probe(denied_port).returncode != 0
    finally:
        allowed_listener.close()
        denied_listener.close()


def test_generator_runs_real_manual_autonomous_api_and_cli_paths(
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "evidence"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_v04_13_development_qualification.py"),
            "--output-dir",
            str(output_directory),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr

    reusable = _load(
        output_directory / "reusable-collaborative-development.json"
    )
    durable = _load(
        output_directory / "durable-autonomous-dispatch-history.json"
    )
    surface_payload = _load(
        output_directory / "standalone-development-api-cli.json"
    )
    surface = QualificationSurfaceResult.model_validate(surface_payload)
    expected_source_revision = qualification_source_revision(ROOT)

    _assert_bound(reusable)
    _assert_bound(durable)
    _assert_bound(durable["record"])
    assert reusable["kind"] == "reusable_collaborative_development"
    assert reusable["source_revision"] == expected_source_revision
    assert reusable["status"] == "passed"
    assert reusable["enterprise_denominator"] is False
    assert reusable["roles"] == ["lilies", "codex"]
    assert reusable["executed_lifecycle"] == [
        "work_item",
        "result",
        "rework",
        "independent_lilies_review",
        "accept",
        "close",
        "stop",
        "archive",
    ]

    manual = reusable["manual"]
    autonomous = reusable["autonomous"]
    assert manual["mode"] == "manual_dispatch"
    assert manual["manual_waited_before_dispatch"] is True
    assert manual["manual_waited_for_review"] is True
    assert manual["manual_waited_after_rework"] is True
    assert autonomous["mode"] == "autonomous"
    assert autonomous["manual_waited_before_dispatch"] is False
    assert autonomous["manual_waited_after_rework"] is False
    for record in (manual, autonomous):
        assert record["review_verdicts"] == ["rework", "accepted"]
        assert [item["passed"] for item in record["results"]] == [False, True]
        assert len(record["independent_review_snapshots"]) == 2
        assert record["restart_store_history_equal"] is True
        assert record["restart_tool_usage_equal"] is True
        assert record["restart_dispatch_history_equal"] is True
        assert record["original_grants_unchanged"] is True
        assert record["source_repository_unchanged"] is True
        assert record["final_assignment_status"] == "archived"
        assert record["final_work_item_status"] == "closed"
        assert {
            item["status"] for item in record["dispatch_history"]
        } == {"delivered"}
        assert "assignment.archived" in {
            item["event_type"] for item in record["store_event_history"]
        }
        usage = record["tool_usage_history"]
        assert len(usage) == 7
        assert sum(item["tool_calls"] for item in usage) == 7
        assert sum(item["commands"] for item in usage) == 6
        assert all(item["status"] == "completed" for item in usage)
        assert {
            item["consumer_id"]
            for item in usage
            if item["consumer_type"] == "result"
        } == {item["result_id"] for item in record["results"]}
        assert {
            item["consumer_id"]
            for item in usage
            if item["consumer_type"] == "review"
        } == set(record["review_ids"])
    assert len(manual["dispatch_history"]) == 2
    assert len(autonomous["dispatch_history"]) == 4

    assert durable["kind"] == "durable_autonomous_dispatch_history"
    assert durable["source_revision"] == expected_source_revision
    assert durable["status"] == "passed"
    assert durable["enterprise_denominator"] is False
    assert durable["record"]["restart_history_equal"] is True
    assert durable["record"]["restart_store_history_equal"] is True
    assert durable["record"]["restart_tool_usage_equal"] is True
    assert durable["record"]["original_grants_unchanged"] is True
    assert durable["record"]["final_assignment_status"] == "archived"
    assert durable["record"]["final_work_item_status"] == "closed"
    assert durable["record"]["source_revision"] == expected_source_revision
    assert durable["record"]["history_digest"] == canonical_digest(
        durable["record"]["history"]
    )
    assert durable["record"]["store_history_digest"] == canonical_digest(
        durable["record"]["store_event_history"]
    )
    assert durable["record"]["tool_usage_digest"] == canonical_digest(
        durable["record"]["tool_usage_history"]
    )
    assert [item["destination_role"] for item in durable["record"]["history"]] == [
        "codex",
        "lilies",
        "codex",
        "lilies",
    ]

    assert surface.status == "passed"
    assert surface.digest is not None
    assert len(surface.observations) == 1
    surface_trace = surface.observations[0]
    assert surface.digest == canonical_digest(surface.observations)
    assert surface_trace["server"]["health_http_status"] == 200
    assert surface_trace["server"]["workflow_platform_required"] is False
    assert surface_trace["final_assignment_status"] == "archived"
    assert surface_trace["final_work_item_status"] == "closed"
    assert surface_trace["executed_lifecycle"] == reusable["executed_lifecycle"]
    assert surface_trace["review_verdicts"] == ["rework", "accepted"]
    assert surface_trace["result_test_passes"] == [False, True]
    assert surface_trace["state_transition_transport"] == (
        "independent_cli_processes_over_loopback_http"
    )
    assert surface_trace["state_transition_service_substitution"] is False
    assert surface_trace["role_evidence_generation"] == (
        "production_workspace_tools_in_qualification_orchestrator"
    )
    assert surface_trace["cli_process_count"] == len(
        surface_trace["cli_operations"]
    )
    assert surface_trace["source_repository_unchanged"] is True
    assert len(surface_trace["result_handoffs"]) == 2
    assert len(
        {handoff["result_id"] for handoff in surface_trace["result_handoffs"]}
    ) == 2
    assert len(
        {
            handoff["review_snapshot"]["receipt_id"]
            for handoff in surface_trace["result_handoffs"]
        }
    ) == 2
    for handoff, verdict in zip(
        surface_trace["result_handoffs"],
        ("rework", "accepted"),
        strict=True,
    ):
        assert handoff["read_by_lilies_cli"] is True
        assert handoff["review_prepare_replayed"] is True
        assert handoff["verdict"] == verdict
        assert handoff["review_snapshot"]["result_id"] == handoff["result_id"]
        assert handoff["review_snapshot"]["promotion_state"] == (
            "review_snapshot_only"
        )
        assert handoff["review_snapshot"]["source_repository_unchanged"] is True
    assert {
        item["command"] for item in surface_trace["cli_operations"]
    } >= {
        "create",
        "work-create",
        "dispatch",
        "lease",
        "start",
        "result",
        "result-show",
        "review-prepare",
        "review",
        "close",
        "events",
        "stop",
        "archive",
        "status",
    }
    assert sum(
        item["command"] == "result"
        for item in surface_trace["cli_operations"]
    ) == 2
    assert sum(
        item["command"] == "result-show"
        for item in surface_trace["cli_operations"]
    ) == 2
    assert sum(
        item["command"] == "review-prepare"
        for item in surface_trace["cli_operations"]
    ) == 4
    assert sum(
        item["command"] == "review"
        for item in surface_trace["cli_operations"]
    ) == 2
    assert {
        item["http_status"]
        for item in surface_trace["direct_api_operations"]
    } == {200}
    assert reusable["standalone_api_cli_digest"] == canonical_digest(
        [reusable["standalone_api_cli"]]
    )
    assert reusable["standalone_api_cli_digest"] == surface.digest
