from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest

from agent_platform.collaboration_models import (
    VerificationClaim,
    VerificationVerdict,
    frozen_claim_context_digest,
)
from agent_platform.independent_verifier_broker import (
    IndependentVerifierBrokerError,
    _claim_state_read_roots,
    _dependency_site_root,
    _sandbox_profile,
    _verifier_dependency_roots,
    _verifier_environment,
    _verifier_read_roots,
    _verifier_source_root,
    run_independent_verifier_subprocess,
)
from agent_platform.task_packages import (
    TaskPackageManager,
    VerificationRuntimeDependency,
)
from tests.test_v04_13_independent_verification import (
    ASSIGNMENT_ID,
    LEAK_MARKER,
    REVISION,
    TASK_ID,
    VerificationCase,
    _build_case,
    _digest_bytes,
    _json_bytes,
)


CHANNEL_ID = UUID("40000000-0000-4000-8000-000000000004")
FORGED_ASSIGNMENT_ID = UUID("50000000-0000-4000-8000-000000000005")


def _full_claim(
    case: VerificationCase,
    *,
    assignment_id: UUID = ASSIGNMENT_ID,
) -> VerificationClaim:
    return VerificationClaim.model_validate(
        {
            **case.claim.model_dump(mode="json", exclude_none=True),
            "channel_id": str(CHANNEL_ID),
            "assignment_id": str(assignment_id),
            "claim_revision": 1,
            "status": "frozen",
            "created_at": "2026-07-24T00:00:00Z",
        }
    )


def _state_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
            _digest_bytes(path.read_bytes()),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _require_macos_sandbox() -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.skip("macOS sandbox-exec is unavailable on this host")


@pytest.mark.parametrize(
    ("module_path", "site_root"),
    [
        (
            ".venv/lib/python3.12/site-packages/pydantic/__init__.py",
            ".venv/lib/python3.12/site-packages",
        ),
        (
            (
                "uv-cache/archive-v0/runtime-overlay/lib/python3.12/"
                "site-packages/typing_extensions.py"
            ),
            "uv-cache/archive-v0/runtime-overlay/lib/python3.12/site-packages",
        ),
    ],
)
def test_dependency_site_root_accepts_venv_and_uv_overlay_layouts(
    tmp_path: Path,
    module_path: str,
    site_root: str,
) -> None:
    location = tmp_path / module_path
    location.parent.mkdir(parents=True)
    location.write_text("# trusted runtime dependency", encoding="utf-8")
    module = ModuleType("verifier_runtime_dependency")
    module.__file__ = str(location)

    assert _dependency_site_root(module) == (tmp_path / site_root)


def test_dependency_site_root_rejects_arbitrary_parent_import_path(
    tmp_path: Path,
) -> None:
    location = tmp_path / "injected-python-path" / "typing_extensions.py"
    location.parent.mkdir()
    location.write_text("# untrusted parent import", encoding="utf-8")
    module = ModuleType("verifier_runtime_dependency")
    module.__file__ = str(location)

    with pytest.raises(
        IndependentVerifierBrokerError,
        match="outside a site-packages root",
    ):
        _dependency_site_root(module)


def test_verifier_environment_uses_only_loaded_dependency_site_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected_root = tmp_path / "injected-python-path"
    output_root = tmp_path / "output"
    source_root = tmp_path / "frozen-source"
    injected_root.mkdir()
    output_root.mkdir()
    source_root.mkdir()
    monkeypatch.syspath_prepend(str(injected_root))

    dependency_roots = _verifier_dependency_roots()
    environment_paths = {
        Path(path).resolve()
        for path in _verifier_environment(
            output_root,
            source_root=source_root,
        )["PYTHONPATH"].split(os.pathsep)
    }

    assert environment_paths == {
        source_root.resolve(),
        *dependency_roots,
    }
    assert injected_root.resolve() not in environment_paths
    assert all(
        root.name in {"site-packages", "dist-packages"}
        for root in dependency_roots
    )


def test_uv_ephemeral_site_root_is_not_implicitly_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    source_root = tmp_path / "frozen-source"
    ephemeral_site = (
        tmp_path / "uv-build" / "runtime" / "lib" / "python3.12" / "site-packages"
    )
    for root in (
        state_root,
        input_root,
        output_root,
        source_root,
        ephemeral_site,
    ):
        root.mkdir(parents=True)
    (ephemeral_site / "sitecustomize.py").write_text(
        "# must not enter the verifier import boundary",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_platform.independent_verifier_broker.sysconfig.get_paths",
        lambda: {
            "purelib": str(ephemeral_site),
            "platlib": str(ephemeral_site),
        },
    )

    environment_paths = {
        Path(path).resolve()
        for path in _verifier_environment(
            output_root,
            source_root=source_root,
        )["PYTHONPATH"].split(os.pathsep)
    }
    read_roots = {
        path.resolve()
        for path in _verifier_read_roots(
            state_read_roots=[state_root],
            input_root=input_root,
            source_root=source_root,
        )
    }

    assert ephemeral_site.resolve() not in environment_paths
    assert ephemeral_site.resolve() not in read_roots


def _run_broker(
    case: VerificationCase,
    *,
    claim: VerificationClaim,
    broker_root: Path,
) -> Any:
    return run_independent_verifier_subprocess(
        state_root=case.state_root,
        task_id=TASK_ID,
        revision=REVISION,
        claim=claim,
        broker_root=broker_root,
        timeout_seconds=30,
    )


def test_broker_runs_real_sandbox_and_returns_result_bound_to_full_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    claim = _full_claim(case)
    source_root = _verifier_source_root(
        state_root=case.state_root,
        process_digest=str(claim.verification_process_digest),
    )
    broker_root = tmp_path / "broker"
    completed_processes: list[subprocess.CompletedProcess[str]] = []
    run_kwargs: list[dict[str, Any]] = []
    original_run = subprocess.run

    def capture_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_kwargs.append(dict(kwargs))
        completed = original_run(*args, **kwargs)
        completed_processes.append(completed)
        return completed

    monkeypatch.setattr(
        "agent_platform.independent_verifier_broker.subprocess.run",
        capture_run,
    )

    result = _run_broker(case, claim=claim, broker_root=broker_root)

    assert result.verdict is VerificationVerdict.independently_verified
    assert result.task_package_digest == claim.task_package_digest
    assert result.environment_ready_digest == claim.environment_ready_digest
    assert result.archive_manifest_digest == claim.archive_manifest_digest
    assert result.frozen_context_digest == claim.frozen_context_digest
    assert result.validation_mode == claim.validation_mode == "real_host"
    assert len(completed_processes) == 1
    completed = completed_processes[0]
    command = completed.args
    assert command[0] == shutil.which("sandbox-exec")
    assert command[1] == "-p"
    assert "(deny default)" in command[2]
    assert "(deny network*)" in command[2]
    assert command[4:7] == [
        "-S",
        "-m",
        "agent_platform.independent_verifier",
    ]
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert set(run_kwargs[0]["env"]) == {
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "TMPDIR",
    }
    assert run_kwargs[0]["cwd"] == source_root
    assert run_kwargs[0]["cwd"] != (
        Path(__file__).resolve().parents[1]
        / "platform"
        / "backend"
        / "src"
    )

    receipt = json.loads(completed.stdout)
    expected_payload = _json_bytes(result.model_dump(mode="json", exclude_none=True))
    assert receipt == {
        "status": "verification_result_written",
        "result_digest": _digest_bytes(expected_payload),
    }
    exposed = completed.stdout + completed.stderr
    assert LEAK_MARKER not in exposed
    assert "protected/oracle" not in exposed


def test_broker_sandbox_leaves_frozen_state_byte_and_mode_identical(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    claim = _full_claim(case)
    broker_root = tmp_path / "broker"
    before = _state_snapshot(case.state_root)

    _run_broker(case, claim=claim, broker_root=broker_root)

    assert _state_snapshot(case.state_root) == before
    assert broker_root.is_dir()
    assert list(broker_root.iterdir()) == []


def test_claim_sandbox_cannot_read_another_task_oracle(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    claim = _full_claim(case)
    source_root = _verifier_source_root(
        state_root=case.state_root,
        process_digest=str(claim.verification_process_digest),
    )
    other_oracle = (
        case.state_root / "packages" / "OTHER-TASK" / "1" / "protected" / "oracle" / "oracle.json"
    )
    other_oracle.parent.mkdir(parents=True)
    other_oracle.write_text('{"hidden":"other-task"}', encoding="utf-8")
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    interpreter = Path(sys.executable).resolve()
    state_read_roots = _claim_state_read_roots(
        state_root=case.state_root,
        task_id=TASK_ID,
        revision=REVISION,
        claim=claim,
    )
    profile = _sandbox_profile(
        read_roots=_verifier_read_roots(
            state_read_roots=state_read_roots,
            input_root=input_root,
            source_root=source_root,
        ),
        output_root=output_root,
        executable=interpreter,
    )

    completed = subprocess.run(
        [
            shutil.which("sandbox-exec"),
            "-p",
            profile,
            str(interpreter),
            "-c",
            (f"from pathlib import Path;Path({str(other_oracle)!r}).read_bytes()"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_verifier_environment(
            output_root,
            source_root=source_root,
        ),
        cwd=source_root,
    )

    assert completed.returncode != 0
    assert "Operation not permitted" in completed.stderr


def test_broker_rejects_full_claim_with_forged_server_owned_assignment(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    forged = _full_claim(case, assignment_id=FORGED_ASSIGNMENT_ID)

    with pytest.raises(
        IndependentVerifierBrokerError,
        match="broker rejected the frozen claim boundary",
    ):
        _run_broker(
            case,
            claim=forged,
            broker_root=tmp_path / "broker",
        )


def test_broker_ignores_active_shadow_with_production_only_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_macos_sandbox()
    case = _build_case(
        tmp_path,
        actual_status="needs-human-review",
        oracle_expected_status="completed",
    )
    claim = _full_claim(case)
    shadow_root = tmp_path / "active-repo-shadow"
    shadow_package = shadow_root / "agent_platform"
    shadow_package.mkdir(parents=True)
    (shadow_package / "__init__.py").write_text("", encoding="utf-8")
    (shadow_package / "independent_verifier.py").write_text(
        "\n".join(
            (
                "import os",
                "from pathlib import Path",
                "if os.environ.get('PYTEST_CURRENT_TEST'):",
                "    raise SystemExit(91)",
                "# Production-only attack: forge success when tests are absent.",
                "result = Path(os.environ['FORGED_RESULT_PATH'])",
                "result.write_text('{\"verdict\":\"independently_verified\"}')",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(shadow_root))

    result = _run_broker(
        case,
        claim=claim,
        broker_root=tmp_path / "broker",
    )

    assert result.verdict is VerificationVerdict.verification_failed
    assert result.differences
    source_root = _verifier_source_root(
        state_root=case.state_root,
        process_digest=str(claim.verification_process_digest),
    )
    assert shadow_root.resolve() not in {
        Path(item).resolve()
        for item in _verifier_environment(
            tmp_path / "broker-output",
            source_root=source_root,
        )["PYTHONPATH"].split(os.pathsep)
    }


def test_broker_rejects_frozen_bundle_byte_drift(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    claim = _full_claim(case)
    manager = TaskPackageManager(case.state_root, read_only=True)
    source_root, _manifest = manager.load_verification_policy_bundle(
        str(claim.verification_process_digest)
    )
    verifier_source = (
        source_root / "agent_platform" / "independent_verifier.py"
    )
    original = verifier_source.read_bytes()
    os.chmod(verifier_source, 0o600)
    verifier_source.write_bytes(
        original + b"\n# unauthorized bundle drift\n"
    )

    with pytest.raises(
        IndependentVerifierBrokerError,
        match="broker rejected the frozen claim boundary",
    ):
        _run_broker(
            case,
            claim=claim,
            broker_root=tmp_path / "broker",
        )


def test_broker_rejects_same_version_runtime_dependency_byte_drift_before_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_case(tmp_path)
    claim = _full_claim(case)
    manager = TaskPackageManager(case.state_root, read_only=True)
    dependencies = manager._verification_runtime_dependencies()
    forged = [
        VerificationRuntimeDependency(
            **{
                **item.model_dump(mode="json"),
                "installed_files_digest": (
                    "sha256:" + "f" * 64
                    if index == 0
                    else item.installed_files_digest
                ),
            }
        )
        for index, item in enumerate(dependencies)
    ]
    subprocess_calls: list[object] = []
    monkeypatch.setattr(
        "agent_platform.independent_verifier_broker.shutil.which",
        lambda _executable: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        TaskPackageManager,
        "_verification_runtime_dependencies",
        staticmethod(lambda: forged),
    )
    monkeypatch.setattr(
        "agent_platform.independent_verifier_broker.subprocess.run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    with pytest.raises(
        IndependentVerifierBrokerError,
        match="broker rejected the frozen claim boundary",
    ):
        _run_broker(
            case,
            claim=claim,
            broker_root=tmp_path / "broker",
        )

    assert subprocess_calls == []


def test_broker_rejects_rehashed_claim_with_unregistered_policy_digest(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    payload = _full_claim(case).model_dump(
        mode="json",
        exclude_none=True,
    )
    payload["verification_process_digest"] = "sha256:" + "f" * 64
    payload["frozen_context_digest"] = frozen_claim_context_digest(payload)
    forged = VerificationClaim.model_validate(payload)

    with pytest.raises(
        IndependentVerifierBrokerError,
        match="broker rejected the frozen claim boundary",
    ):
        _run_broker(
            case,
            claim=forged,
            broker_root=tmp_path / "broker",
        )


def test_broker_rejects_output_receipt_that_does_not_bind_result_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_macos_sandbox()
    case = _build_case(tmp_path)
    claim = _full_claim(case)
    original_run = subprocess.run

    def forge_receipt(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        completed = original_run(*args, **kwargs)
        assert completed.returncode == 0, completed.stderr
        forged_stdout = json.dumps(
            {
                "status": "verification_result_written",
                "result_digest": ("sha256:" + hashlib.sha256(b"forged-result").hexdigest()),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return subprocess.CompletedProcess(
            args=completed.args,
            returncode=completed.returncode,
            stdout=forged_stdout + "\n",
            stderr=completed.stderr,
        )

    monkeypatch.setattr(
        "agent_platform.independent_verifier_broker.subprocess.run",
        forge_receipt,
    )

    with pytest.raises(
        IndependentVerifierBrokerError,
        match="receipt did not match",
    ):
        _run_broker(
            case,
            claim=claim,
            broker_root=tmp_path / "broker",
        )


@pytest.mark.parametrize(
    "probe",
    [
        "outside-read",
        "state-write",
        "input-write",
        "network",
        "child-exec",
    ],
)
def test_broker_sandbox_profile_denies_every_non_output_capability(
    tmp_path: Path,
    probe: str,
) -> None:
    _require_macos_sandbox()
    state_root = tmp_path / "state"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    for root in (state_root, input_root, output_root):
        root.mkdir()
    source_root = tmp_path / "frozen-source"
    source_root.mkdir()
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("outside verifier boundary", encoding="utf-8")
    (state_root / "state.txt").write_text("frozen", encoding="utf-8")
    (input_root / "claim.json").write_text("{}", encoding="utf-8")
    interpreter = Path(sys.executable).resolve()
    code = {
        "outside-read": (f"from pathlib import Path;Path({str(secret)!r}).read_bytes()"),
        "state-write": (
            f"from pathlib import Path;Path({str(state_root / 'forged.txt')!r}).write_text('x')"
        ),
        "input-write": (
            f"from pathlib import Path;Path({str(input_root / 'claim.json')!r}).write_text('x')"
        ),
        "network": ("import socket;socket.create_connection(('127.0.0.1',9),0.2)"),
        "child-exec": ("import subprocess;subprocess.run(['/bin/echo','forbidden'],check=True)"),
    }[probe]
    profile = _sandbox_profile(
        read_roots=_verifier_read_roots(
            state_read_roots=[state_root],
            input_root=input_root,
            source_root=source_root,
        ),
        output_root=output_root,
        executable=interpreter,
    )

    completed = subprocess.run(
        [shutil.which("sandbox-exec"), "-p", profile, str(interpreter), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=_verifier_environment(
            output_root,
            source_root=source_root,
        ),
        cwd=source_root,
    )

    assert completed.returncode != 0
    assert "Operation not permitted" in completed.stderr
    assert not (state_root / "forged.txt").exists()
    assert (input_root / "claim.json").read_text(encoding="utf-8") == "{}"
    assert secret.read_text(encoding="utf-8") == "outside verifier boundary"


def test_parent_sys_path_cannot_expand_the_verifier_read_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_macos_sandbox()
    injected_root = tmp_path / "injected-python-path"
    state_root = tmp_path / "state"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    for root in (injected_root, state_root, input_root, output_root):
        root.mkdir()
    source_root = tmp_path / "frozen-source"
    source_root.mkdir()
    secret = injected_root / "parent-process-secret.txt"
    secret.write_text("must remain outside verifier", encoding="utf-8")
    monkeypatch.syspath_prepend(str(injected_root))
    interpreter = Path(sys.executable).resolve()
    profile = _sandbox_profile(
        read_roots=_verifier_read_roots(
            state_read_roots=[state_root],
            input_root=input_root,
            source_root=source_root,
        ),
        output_root=output_root,
        executable=interpreter,
    )

    assert str(injected_root.resolve()) not in profile
    assert '(subpath "/")' not in profile
    completed = subprocess.run(
        [
            shutil.which("sandbox-exec"),
            "-p",
            profile,
            str(interpreter),
            "-c",
            (f"from pathlib import Path;Path({str(secret)!r}).read_bytes()"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_verifier_environment(
            output_root,
            source_root=source_root,
        ),
        cwd=source_root,
    )

    assert completed.returncode != 0
    assert "Operation not permitted" in completed.stderr
    assert secret.read_text(encoding="utf-8") == "must remain outside verifier"


def test_broker_sandbox_profile_allows_only_declared_output_sink(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    state_root = tmp_path / "state"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    for root in (state_root, input_root, output_root):
        root.mkdir()
    source_root = tmp_path / "frozen-source"
    source_root.mkdir()
    interpreter = Path(sys.executable).resolve()
    output = output_root / "result.json"
    profile = _sandbox_profile(
        read_roots=_verifier_read_roots(
            state_read_roots=[state_root],
            input_root=input_root,
            source_root=source_root,
        ),
        output_root=output_root,
        executable=interpreter,
    )
    completed = subprocess.run(
        [
            shutil.which("sandbox-exec"),
            "-p",
            profile,
            str(interpreter),
            "-c",
            (f"from pathlib import Path;Path({str(output)!r}).write_text('trusted-output')"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_verifier_environment(
            output_root,
            source_root=source_root,
        ),
        cwd=source_root,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert output.read_text(encoding="utf-8") == "trusted-output"
