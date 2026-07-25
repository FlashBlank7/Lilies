from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.task_packages import (
    BUILDER_API_MANUAL_FILE,
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
)
from scripts import run_v04_13_codex_builder_child as child


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _handoff(
    *,
    workspace: Path,
    manifest_digest: str,
    policy_digest: str,
    base_url: str = "http://127.0.0.1:19090",
    revision: int = 20,
) -> dict[str, object]:
    assignment_id = str(uuid4())
    build_id = str(uuid4())
    return {
        "schema_version": "1.0",
        "builder_actor": "codex",
        "formal_archive_supported": False,
        "task": {
            "task_id": "EXP-LILIES-001",
            "revision": revision,
            "run_id": f"formal-run:{build_id}",
        },
        "assignment": {
            "application_id": str(uuid4()),
            "assignment_id": assignment_id,
            "build_id": build_id,
            "session_id": str(uuid4()),
            "environment_instance_id": (
                f"exp-lilies-001:r{revision}:seed-debug"
            ),
        },
        "workspace": {
            "path": str(workspace),
            "manifest_digest": manifest_digest,
            "policy_digest": policy_digest,
        },
        "platform": {
            "base_url": base_url,
            "contract_url": "/api/contract",
            "contract_digest": DIGEST,
            "access_token": "lpt_" + "t" * 80,
        },
        "collaboration": {
            "base_url": base_url,
            "channel_id": str(uuid4()),
            "access_token": "collaboration-" + "c" * 48,
        },
    }


def _workspace_fixture(
    tmp_path: Path,
    *,
    base_url: str = "http://127.0.0.1:19090",
) -> tuple[Path, dict[str, object]]:
    workspace = tmp_path / "public-workspace"
    workspace.mkdir(mode=0o700, parents=True)
    (workspace / "work").mkdir(mode=0o700)
    (workspace / "artifacts").mkdir(mode=0o700)
    manual = _canonical(child._public_api_manual())
    requirement = b"Build the frozen public workflow through the platform API.\n"
    public_files = {
        BUILDER_API_MANUAL_FILE: manual,
        "requirement.md": requirement,
    }
    entries: list[dict[str, object]] = []
    for relative, payload in public_files.items():
        target = workspace / relative
        target.write_bytes(payload)
        target.chmod(0o400)
        entries.append(
            {
                "digest": child._digest(payload),
                "logical_source": f"task-package:{relative}",
                "read_only": True,
                "size_bytes": len(payload),
                "target_path": relative,
            }
        )
    assignment_id = str(uuid4())
    build_id = str(uuid4())
    manifest = {
        "schema_version": "1.0",
        "task_id": "EXP-LILIES-001",
        "revision": 20,
        "role": "lilies",
        "run_id": f"formal-run:{build_id}",
        "assignment_id": assignment_id,
        "public_summary_digest": DIGEST,
        "environment_ready_digest": DIGEST,
        "environment_instance_id": "exp-lilies-001:r20:seed-debug",
        "created_at": "2026-07-26T00:00:00Z",
        "denied_segments": sorted(child._REQUIRED_DENIED_SEGMENTS),
        "writable_prefixes": ["work", "artifacts"],
        "entries": entries,
    }
    policy = {
        "schema_version": "1.0",
        "denied_segments": sorted(
            {
                *child._REQUIRED_DENIED_SEGMENTS,
                WORKSPACE_MANIFEST_FILE,
                WORKSPACE_POLICY_FILE,
            }
        ),
        "writable_prefixes": ["work", "artifacts"],
    }
    manifest_payload = _canonical(manifest)
    policy_payload = _canonical(policy)
    (workspace / WORKSPACE_MANIFEST_FILE).write_bytes(manifest_payload)
    (workspace / WORKSPACE_POLICY_FILE).write_bytes(policy_payload)
    (workspace / WORKSPACE_MANIFEST_FILE).chmod(0o400)
    (workspace / WORKSPACE_POLICY_FILE).chmod(0o400)
    workspace.chmod(0o500)
    handoff = _handoff(
        workspace=workspace,
        manifest_digest=child._digest(manifest_payload),
        policy_digest=child._digest(policy_payload),
        base_url=base_url,
    )
    handoff["task"]["run_id"] = manifest["run_id"]  # type: ignore[index]
    handoff["assignment"]["assignment_id"] = assignment_id  # type: ignore[index]
    handoff["assignment"]["build_id"] = build_id  # type: ignore[index]
    return workspace, handoff


def _write_private_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical(value))
    path.chmod(0o600)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_pid_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.02)
    return not _pid_exists(pid)


def test_private_handoff_uses_nofollow_descriptor_for_every_path_component(
    tmp_path: Path,
) -> None:
    target = tmp_path / "handoff.json"
    _write_private_json(target, {"schema_version": "1.0"})
    assert child._read_private_handoff(target)["schema_version"] == "1.0"

    final_symlink = tmp_path / "final-link.json"
    final_symlink.symlink_to(target)
    with pytest.raises(child.CodexBuilderChildError, match="unsafe"):
        child._read_private_handoff(final_symlink)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    nested = real_parent / "nested.json"
    _write_private_json(nested, {"schema_version": "1.0"})
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(child.CodexBuilderChildError, match="unsafe"):
        child._read_private_handoff(linked_parent / "nested.json")


def test_workspace_preflight_rechecks_every_manifest_file_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    workspace, handoff = _workspace_fixture(tmp_path)
    public_workspace, _, _, _ = child._validate_handoff(handoff)
    verified = child._verify_public_workspace(
        handoff=handoff,
        public_workspace=public_workspace,
    )
    assert verified.manual_path == workspace / BUILDER_API_MANUAL_FILE
    assert verified.manual_digest == child._digest(
        _canonical(child._public_api_manual())
    )

    workspace.chmod(0o700)
    requirement = workspace / "requirement.md"
    requirement.unlink()
    requirement.symlink_to(ROOT / "pyproject.toml")
    workspace.chmod(0o500)
    with pytest.raises(child.CodexBuilderChildError):
        child._verify_public_workspace(
            handoff=handoff,
            public_workspace=public_workspace,
        )


def test_workspace_preflight_rejects_digest_drift_and_undeclared_files(
    tmp_path: Path,
) -> None:
    workspace, handoff = _workspace_fixture(tmp_path)
    public_workspace, _, _, _ = child._validate_handoff(handoff)
    workspace.chmod(0o700)
    requirement = workspace / "requirement.md"
    requirement.chmod(0o600)
    requirement.write_bytes(b"drift")
    requirement.chmod(0o400)
    workspace.chmod(0o500)
    with pytest.raises(child.CodexBuilderChildError, match="bytes"):
        child._verify_public_workspace(
            handoff=handoff,
            public_workspace=public_workspace,
        )

    workspace, handoff = _workspace_fixture(tmp_path / "second")
    public_workspace, _, _, _ = child._validate_handoff(handoff)
    workspace.chmod(0o700)
    undeclared = workspace / "owner-note.txt"
    undeclared.write_text("must not enter Builder context", encoding="utf-8")
    undeclared.chmod(0o400)
    workspace.chmod(0o500)
    with pytest.raises(child.CodexBuilderChildError, match="undeclared"):
        child._verify_public_workspace(
            handoff=handoff,
            public_workspace=public_workspace,
        )


def test_environment_and_profile_do_not_inherit_certificate_or_executable_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malicious_cert_root = tmp_path / "developer-secrets"
    malicious_cert_root.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(malicious_cert_root))
    monkeypatch.setenv("SSL_CERT_FILE", str(malicious_cert_root / "token.pem"))
    runtime = tmp_path / "runtime"
    codex_home = runtime / "codex-home"
    user_home = runtime / "user-home"
    temporary = runtime / "tmp"
    for path in (runtime, codex_home, user_home, temporary):
        path.mkdir(exist_ok=True)
    environment, keys = child._clean_external_builder_environment(
        codex_home=codex_home,
        user_home=user_home,
        temporary_directory=temporary,
        proxy_port=19001,
    )
    assert "SSL_CERT_DIR" not in environment
    assert "SSL_CERT_FILE" not in environment
    assert "SSL_CERT_DIR" not in keys
    assert "SSL_CERT_FILE" not in keys

    sandbox = tmp_path / "sandbox-exec"
    sandbox.write_text("fixture", encoding="utf-8")
    sandbox.chmod(0o700)
    executable_parent = tmp_path / "codex-extension-private"
    executable_parent.mkdir()
    executable = executable_parent / "codex"
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o700)
    workspace = tmp_path / "workspace"
    handoff = tmp_path / "handoff.json"
    workspace.mkdir()
    handoff.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(child, "MACOS_SANDBOX", sandbox)

    command = child._sandboxed_arguments(
        executable=executable,
        codex_arguments=("exec", "-"),
        public_workspace=workspace,
        handoff_path=handoff,
        runtime_root=runtime,
        provider_proxy_port=19001,
        platform_port=19002,
    )
    profile = command[2]
    read_section = profile.split("(allow file-read*", 1)[1].split(
        "(allow file-write*", 1
    )[0]
    assert f'(subpath "{executable_parent}")' not in read_section
    assert f'(subpath "{malicious_cert_root}")' not in profile


def test_usage_output_marks_each_missing_field_not_reported() -> None:
    transcript = (
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 2,
                    "cached_input_tokens": None,
                },
            }
        ).encode()
        + b"\n"
    )
    usage, support = child._codex_usage_details(transcript)
    assert usage == {"input_tokens": 11, "output_tokens": 2}
    assert support == {
        "input_tokens": "reported",
        "cached_input_tokens": "not_reported",
        "cache_write_input_tokens": "not_reported",
        "output_tokens": "reported",
        "reasoning_output_tokens": "not_reported",
    }


def test_resume_reuses_exact_isolated_thread_and_never_uses_ephemeral(
    tmp_path: Path,
) -> None:
    thread_id = str(uuid4())
    runtime = tmp_path / "runtime"
    codex_home = runtime / "codex-home"
    user_home = runtime / "user-home"
    temporary = runtime / "tmp"
    sessions = codex_home / "sessions" / "2026" / "07" / "26"
    for path, mode in (
        (runtime, 0o700),
        (codex_home, 0o700),
        (user_home, 0o700),
        (temporary, 0o700),
        (codex_home / "sessions", 0o700),
        (codex_home / "sessions" / "2026", 0o700),
        (codex_home / "sessions" / "2026" / "07", 0o700),
        (sessions, 0o700),
    ):
        path.mkdir(exist_ok=True)
        path.chmod(mode)
    auth = codex_home / "auth.json"
    auth.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    auth.chmod(0o600)
    session = sessions / f"rollout-2026-07-26T00-00-00-{thread_id}.jsonl"
    session.write_text("{}\n", encoding="utf-8")
    session.chmod(0o600)

    resumed_codex_home, resumed_user_home, billing = (
        child._resume_runtime_identity(runtime, thread_id=thread_id)
    )
    assert resumed_codex_home == codex_home
    assert resumed_user_home == user_home
    assert billing["billing_mode"] == "chatgpt_subscription"
    state_path, state_digest = child._resume_state_binding(
        sessions_root=codex_home / "sessions",
        thread_id=thread_id,
    )
    assert state_path == session
    assert state_digest == child._digest(b"{}\n")

    first = child._codex_execution_arguments(
        public_workspace=tmp_path / "workspace",
        model="gpt-test",
        resume_thread_id=None,
    )
    resumed = child._codex_execution_arguments(
        public_workspace=tmp_path / "workspace",
        model="gpt-test",
        resume_thread_id=thread_id,
    )
    assert "--ephemeral" not in first
    assert "--ephemeral" not in resumed
    assert resumed[resumed.index("exec") + 1] == "resume"
    assert resumed[-2:] == (thread_id, "-")


def test_every_initial_and_resumed_codex_argv_enforces_budget_and_single_agent(
    tmp_path: Path,
) -> None:
    binding = child.InvocationBinding.create(
        runtime_root=tmp_path / "runtime",
        public_workspace=tmp_path / "workspace",
    )
    initial = child._codex_execution_arguments(
        public_workspace=tmp_path / "workspace",
        model="gpt-test",
        resume_thread_id=None,
        rollout_token_limit=500_000,
        invocation_binding=binding,
    )
    resumed = child._codex_execution_arguments(
        public_workspace=tmp_path / "workspace",
        model="gpt-test",
        resume_thread_id=str(uuid4()),
        rollout_token_limit=500_000,
        invocation_binding=binding,
    )
    for arguments in (initial, resumed):
        joined = " ".join(arguments)
        assert "features.rollout_budget.enabled=true" in arguments
        assert "features.rollout_budget.limit_tokens=500000" in arguments
        assert (
            "features.rollout_budget.sampling_token_weight=1.0"
            in arguments
        )
        assert (
            "features.rollout_budget.prefill_token_weight=1.0"
            in arguments
        )
        assert (
            "features.rollout_budget.reminder_at_remaining_tokens=[250000,50000]"
            in arguments
        )
        assert "features.collab=false" in arguments
        assert "features.multi_agent=false" in arguments
        assert "features.multi_agent_v2=false" in arguments
        assert binding.matches_command(joined)
        assert "features.rollout_budget=true" not in arguments

    with pytest.raises(child.CodexBuilderChildError, match="rollout token limit"):
        child._codex_execution_arguments(
            public_workspace=tmp_path / "workspace",
            model="gpt-test",
            resume_thread_id=None,
            rollout_token_limit=child.MAX_ROLLOUT_TOKEN_LIMIT + 1,
        )
    with pytest.raises(child.CodexBuilderChildError, match="rollout token limit"):
        child._codex_execution_arguments(
            public_workspace=tmp_path / "workspace",
            model="gpt-test",
            resume_thread_id=None,
            rollout_token_limit=1,
        )


def test_codex_feature_preflight_fails_closed_when_budget_is_not_enabled(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/bin/sh\n"
        "printf 'multi_agent stable false\\n'\n"
        "printf 'multi_agent_v2 stable false\\n'\n"
        "printf 'rollout_budget under-development false\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    with pytest.raises(child.CodexBuilderChildError, match="cannot prove"):
        child._verify_codex_security_features(
            executable=executable,
            environment={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            rollout_token_limit=100_000,
        )


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_is_forwarded_and_entire_codex_process_group_is_reaped(
    tmp_path: Path,
    signum: int,
) -> None:
    pid_file = tmp_path / f"processes-{signum}.json"
    inner = (
        "import json,os,subprocess,sys,time;"
        "desc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        "open(sys.argv[1],'w').write(json.dumps([os.getpid(),desc.pid]));"
        "time.sleep(120)"
    )
    helper = f"""
import subprocess
import sys
from scripts import run_v04_13_codex_builder_child as child
p = subprocess.Popen(
    [sys.executable, "-c", {inner!r}, sys.argv[1]],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
)
print("ready", flush=True)
try:
    child._communicate_isolated_process(
        p,
        input_bytes=b"",
        timeout_seconds=60,
    )
except child._ForwardedTermination as error:
    raise SystemExit(128 + error.signum)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    outer = subprocess.Popen(
        [sys.executable, "-c", helper, str(pid_file)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert outer.stdout is not None
    assert outer.stdout.readline().strip() == "ready"
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    process_ids = json.loads(pid_file.read_text(encoding="utf-8"))
    outer.send_signal(signum)
    _, stderr = outer.communicate(timeout=15)
    assert outer.returncode == 128 + signum, stderr
    assert all(_wait_pid_gone(pid) for pid in process_ids)


def test_timeout_kills_descendants_before_returning(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout-processes.json"
    program = (
        "import json,os,subprocess,sys,time;"
        "desc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        "open(sys.argv[1],'w').write(json.dumps([os.getpid(),desc.pid]));"
        "time.sleep(120)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(pid_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr, timed_out = child._communicate_isolated_process(
        process,
        input_bytes=b"",
        timeout_seconds=1,
    )
    assert timed_out is True
    assert stdout == b""
    assert stderr == b""
    process_ids = json.loads(pid_file.read_text(encoding="utf-8"))
    assert all(_wait_pid_gone(pid) for pid in process_ids)


def test_tracker_kills_descendant_that_escapes_original_process_group(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    binding = child.InvocationBinding.create(
        runtime_root=runtime,
        public_workspace=workspace,
    )
    pid_file = tmp_path / "detached-pid"
    detached_program = (
        "import os,sys,time;"
        "open(sys.argv[1],'w').write(str(os.getpid()));"
        "time.sleep(120)"
    )
    root_program = (
        "import subprocess,sys,time;"
        "subprocess.Popen("
        "[sys.executable,'-c',sys.argv[2],sys.argv[1]],"
        "start_new_session=True);"
        "time.sleep(0.5)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            root_program,
            str(pid_file),
            detached_program,
            *binding.codex_config_arguments()[1::2],
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr, timed_out = child._communicate_isolated_process(
        process,
        input_bytes=b"",
        timeout_seconds=5,
        invocation_binding=binding,
    )
    assert timed_out is False
    assert stdout == b""
    assert stderr == b""
    detached_pid = int(pid_file.read_text(encoding="utf-8"))
    assert _wait_pid_gone(detached_pid)


def test_tracker_refuses_to_signal_reused_or_changed_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = child.InvocationBinding(
        invocation_id="t01h-" + "a" * 32,
        runtime_digest="b" * 64,
        workspace_digest="c" * 64,
    )
    expected = child._ProcessIdentity(
        pid=987_654,
        parent_pid=1,
        process_group_id=987_654,
        started_at="Sun Jul 26 08:00:00 2026",
        command_digest="d" * 64,
        command="/bin/sleep 120",
    )
    replacement = child._ProcessIdentity(
        pid=expected.pid,
        parent_pid=1,
        process_group_id=expected.pid,
        started_at="Sun Jul 26 08:00:01 2026",
        command_digest="e" * 64,
        command="/usr/bin/yes",
    )
    tracker = child._InvocationProcessTracker(
        root_pid=expected.pid,
        binding=binding,
    )
    monkeypatch.setattr(
        child,
        "_process_identity_snapshot",
        lambda: {replacement.pid: replacement},
    )
    monkeypatch.setattr(
        child.os,
        "kill",
        lambda *_args: pytest.fail("replacement PID must never be signalled"),
    )
    assert tracker._signal_current(expected, signal.SIGTERM) is False


def test_provider_proxy_port_is_closed_after_context_exit() -> None:
    with child._AllowlistedConnectProxy(("example.com",)) as proxy:
        port = proxy.port
        connection = socket.create_connection(("127.0.0.1", port), timeout=1)
        connection.close()
    probe = socket.socket()
    probe.settimeout(0.2)
    try:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        probe.close()


@pytest.mark.skipif(
    sys.platform != "darwin" or not child.MACOS_SANDBOX.is_file(),
    reason="real macOS sandbox-exec is unavailable",
)
def test_real_macos_seatbelt_probe_denies_private_surfaces_and_allows_public_api(
    tmp_path: Path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if (
                self.path != "/api/contract"
                or not self.headers.get("Authorization", "").startswith("Bearer lpt_")
            ):
                self.send_response(403)
                self.end_headers()
                return
            body = b'{"schema_version":"1.0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        workspace, handoff = _workspace_fixture(tmp_path, base_url=base_url)
        public_workspace, _, platform_port, _ = child._validate_handoff(handoff)
        verified = child._verify_public_workspace(
            handoff=handoff,
            public_workspace=public_workspace,
        )
        runtime = tmp_path / "runtime"
        user_home = runtime / "user-home"
        temporary = runtime / "tmp"
        for directory in (runtime, user_home, temporary):
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        handoff_path = runtime / child.RUNTIME_HANDOFF_FILE
        _write_private_json(handoff_path, handoff)

        private_root = tmp_path / "private-surfaces"
        forbidden_paths = (
            private_root / "repo" / "pyproject.toml",
            private_root / "platform-data" / "agent_platform.db",
            private_root / "task-package" / "protected" / "hidden.json",
            private_root / "task-package" / "protected" / "oracle" / "oracle.json",
        )
        for path in forbidden_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private", encoding="utf-8")
            path.chmod(0o600)

        evidence = child._run_seatbelt_negative_probe(
            public_workspace=workspace,
            public_probe_path=verified.public_probe_path,
            handoff_path=handoff_path,
            handoff=handoff,
            runtime_root=runtime,
            user_home=user_home,
            provider_proxy_port=server.server_port,
            platform_port=platform_port,
            forbidden_paths=forbidden_paths,
        )
        assert evidence == {
            "schema_version": "v0.4.13-t01h-seatbelt-probe-1",
            "api_status": 200,
            "forbidden_read_count": 4,
            "public_workspace_read": True,
        }
        assert stat.S_IMODE(
            (runtime / child.SEATBELT_PROBE_FILE).stat().st_mode
        ) == 0o600
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
