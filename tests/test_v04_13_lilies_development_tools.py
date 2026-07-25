from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaborative_development_models import (
    AgentRole,
    SideEffect,
    WorkspaceGrant,
)
from agent_platform.lilies_development_tools import (
    AutonomousHandoffAuthorityRequest,
    DevelopmentToolAuthority,
    DevelopmentToolDenied,
    DevelopmentToolName,
    DevelopmentWorkspaceTools,
    GitDiffRequest,
    GitStatusRequest,
    ProcessRunRequest,
    WorkspacePatchRequest,
    WorkspaceReadRequest,
    WorkspaceSearchRequest,
    WorkspaceWriteRequest,
    derive_autonomous_handoff_authority,
)


def _grant(
    root: Path,
    *,
    role: AgentRole = AgentRole.lilies,
    allowed_paths: tuple[str, ...] = ("source",),
    allowed_argv: tuple[tuple[str, ...], ...] = (),
    effects: tuple[SideEffect, ...] = (),
) -> WorkspaceGrant:
    return WorkspaceGrant(
        workspace_id=uuid4(),
        agent_role=role,
        workspace_root=str(root),
        baseline_commit="a" * 40,
        allowed_paths=allowed_paths,
        allowed_argv=allowed_argv,
        allowed_side_effects=effects,
        created_at=datetime.now(timezone.utc),
    )


def _authority(
    root: Path,
    *,
    role: AgentRole = AgentRole.lilies,
    tools: tuple[DevelopmentToolName, ...],
    allowed_paths: tuple[str, ...] = ("source",),
    allowed_argv: tuple[tuple[str, ...], ...] = (),
    effects: tuple[SideEffect, ...] = (),
    max_timeout_seconds: float = 5,
    max_output_bytes: int = 10_000,
) -> DevelopmentToolAuthority:
    return DevelopmentToolAuthority(
        actor_role=role,
        workspace_grant=_grant(
            root,
            role=role,
            allowed_paths=allowed_paths,
            allowed_argv=allowed_argv,
            effects=effects,
        ),
        enabled_tools=tools,
        max_timeout_seconds=max_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def test_authority_is_strict_role_scoped_and_side_effect_bound(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "source").mkdir(parents=True)
    grant = _grant(root, role=AgentRole.codex)

    with pytest.raises(ValidationError, match="actor role"):
        DevelopmentToolAuthority(
            actor_role=AgentRole.lilies,
            workspace_grant=grant,
            enabled_tools=(DevelopmentToolName.workspace_read,),
        )
    with pytest.raises(ValidationError, match="workspace_write authority"):
        DevelopmentToolAuthority(
            actor_role=AgentRole.codex,
            workspace_grant=grant,
            enabled_tools=(DevelopmentToolName.workspace_write,),
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorkspaceReadRequest.model_validate(
            {"path": "source/main.py", "unexpected": True}
        )


@pytest.mark.asyncio
async def test_search_and_read_are_bounded_to_granted_non_symlink_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    (source / "main.py").write_text("first\nNeedle here\nlast\n", encoding="utf-8")
    (outside / "secret.txt").write_text("do not leak", encoding="utf-8")
    (source / "escape").symlink_to(outside, target_is_directory=True)
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(
                DevelopmentToolName.workspace_search,
                DevelopmentToolName.workspace_read,
            ),
        )
    )

    result = await tools.workspace_search(
        WorkspaceSearchRequest(
            path="source",
            file_pattern="*.py",
            text="needle",
        )
    )
    assert [(item.path, item.line, item.column) for item in result.matches] == [
        ("source/main.py", 2, 1)
    ]
    read = await tools.workspace_read(WorkspaceReadRequest(path="source/main.py"))
    assert read.content == "first\nNeedle here\nlast"

    with pytest.raises(ValidationError, match="stay inside"):
        WorkspaceReadRequest(path="../outside/secret.txt")
    with pytest.raises(DevelopmentToolDenied, match="symlink"):
        await tools.workspace_read(
            WorkspaceReadRequest(path="source/escape/secret.txt")
        )


@pytest.mark.asyncio
async def test_write_and_patch_follow_explicit_mutation_authority(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "source").mkdir(parents=True)
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(
                DevelopmentToolName.workspace_write,
                DevelopmentToolName.workspace_patch,
                DevelopmentToolName.workspace_read,
            ),
            effects=(SideEffect.workspace_write,),
        )
    )

    written = await tools.workspace_write(
        WorkspaceWriteRequest(path="source/pkg/value.txt", content="old\n")
    )
    assert written.bytes_written == 4
    patched = await tools.workspace_patch(
        WorkspacePatchRequest(
            path="source/pkg/value.txt",
            old_string="old",
            new_string="new",
        )
    )
    assert patched.replacements == 1
    assert (root / "source/pkg/value.txt").read_text(encoding="utf-8") == "new\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "source/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DevelopmentToolDenied, match="symlink"):
        await tools.workspace_write(
            WorkspaceWriteRequest(path="source/link/escaped.txt", content="denied")
        )
    assert not (outside / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_process_run_uses_exact_argv_clean_environment_and_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    source.mkdir(parents=True)
    script = source / "inspect.py"
    script.write_text(
        "import os, sys\n"
        "print(os.environ.get('SHOULD_NOT_LEAK', '<missing>'))\n"
        "print(os.environ['LILIES_NETWORK_ACCESS'])\n"
        "print(sys.argv[1])\n",
        encoding="utf-8",
    )
    argv = (sys.executable, "inspect.py", "value;touch shell-was-used")
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.process_run,),
            allowed_argv=(argv,),
            effects=(SideEffect.process_execute,),
        )
    )

    result = await tools.process_run(
        ProcessRunRequest(argv=argv, cwd="source", timeout_seconds=2)
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "<missing>",
        "denied",
        "value;touch shell-was-used",
    ]
    assert result.inherited_environment == "none"
    assert result.network_access == "not_granted"
    assert result.shell_used is False
    assert not (source / "shell-was-used").exists()

    with pytest.raises(DevelopmentToolDenied, match="exact command allowlist"):
        await tools.process_run(
            ProcessRunRequest(
                argv=(sys.executable, "inspect.py", "different"),
                cwd="source",
            )
        )


@pytest.mark.asyncio
async def test_process_sandbox_blocks_internal_authority_expansion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    private = root / "private"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    private.mkdir()
    outside.mkdir()
    (private / "secret.txt").write_text("workspace secret", encoding="utf-8")
    outside_target = outside / "escaped.txt"
    script = source / "escape.py"
    script.write_text(
        "import socket, subprocess\n"
        "from pathlib import Path\n"
        "Path('inside.txt').write_text('allowed', encoding='utf-8')\n"
        "checks = {\n"
        f"  'outside_write': lambda: Path({str(outside_target)!r}).write_text('no'),\n"
        "  'ungranted_read': lambda: Path('../private/secret.txt').read_text(),\n"
        "  'network': lambda: socket.create_connection(('127.0.0.1', 9), 0.1),\n"
        "  'child': lambda: subprocess.run(['/usr/bin/true'], check=True),\n"
        "}\n"
        "for name, operation in checks.items():\n"
        "    try:\n"
        "        operation()\n"
        "    except Exception as error:\n"
        "        print(f'{name}:{type(error).__name__}')\n"
        "    else:\n"
        "        print(f'{name}:UNEXPECTEDLY_ALLOWED')\n",
        encoding="utf-8",
    )
    argv = (sys.executable, "escape.py")
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.process_run,),
            allowed_argv=(argv,),
            effects=(
                SideEffect.process_execute,
                SideEffect.workspace_write,
            ),
        )
    )

    result = await tools.process_run(ProcessRunRequest(argv=argv, cwd="source"))

    assert result.exit_code == 0, result.stderr
    assert (source / "inside.txt").read_text(encoding="utf-8") == "allowed"
    assert not outside_target.exists()
    assert "UNEXPECTEDLY_ALLOWED" not in result.stdout
    assert {line.split(":", 1)[0] for line in result.stdout.splitlines()} == {
        "outside_write",
        "ungranted_read",
        "network",
        "child",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv, expected",
    [
        (("bash", "-lc", "printf unsafe"), "forbidden"),
        (("curl", "https://example.com"), "forbidden"),
        ((sys.executable, "-c", "print('unsafe')"), "inline interpreter"),
    ],
)
async def test_forbidden_argv_overrides_explicit_allowlist(
    tmp_path: Path,
    argv: tuple[str, ...],
    expected: str,
) -> None:
    root = tmp_path / "workspace"
    (root / "source").mkdir(parents=True)
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.process_run,),
            allowed_argv=(argv,),
            effects=(SideEffect.process_execute,),
        )
    )

    with pytest.raises(DevelopmentToolDenied, match=expected):
        await tools.process_run(ProcessRunRequest(argv=argv, cwd="source"))


@pytest.mark.asyncio
async def test_process_timeout_and_output_capture_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    source.mkdir(parents=True)
    (source / "output.py").write_text("print('x' * 10000)\n", encoding="utf-8")
    (source / "sleep.py").write_text(
        "import time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    output_argv = (sys.executable, "output.py")
    sleep_argv = (sys.executable, "sleep.py")
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.process_run,),
            allowed_argv=(output_argv, sleep_argv),
            effects=(SideEffect.process_execute,),
            max_timeout_seconds=2,
            max_output_bytes=128,
        )
    )

    output = await tools.process_run(
        ProcessRunRequest(
            argv=output_argv,
            cwd="source",
            timeout_seconds=1,
            max_output_bytes=64,
        )
    )
    assert len(output.stdout.encode()) == 64
    assert output.stdout_bytes > 64
    assert output.stdout_truncated

    timed_out = await tools.process_run(
        ProcessRunRequest(
            argv=sleep_argv,
            cwd="source",
            timeout_seconds=0.05,
            max_output_bytes=64,
        )
    )
    assert timed_out.timed_out
    assert timed_out.exit_code is None


@pytest.mark.asyncio
async def test_process_cwd_cannot_escape_through_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    (source / "escape").symlink_to(outside, target_is_directory=True)
    argv = (sys.executable, "--version")
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.process_run,),
            allowed_argv=(argv,),
            effects=(SideEffect.process_execute,),
        )
    )

    with pytest.raises(DevelopmentToolDenied, match="symlink"):
        await tools.process_run(
            ProcessRunRequest(argv=argv, cwd="source/escape")
        )


@pytest.mark.asyncio
async def test_process_arguments_cannot_escape_or_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    outside_script = outside / "outside.py"
    outside_script.write_text("print('outside')\n", encoding="utf-8")
    (source / "linked.py").symlink_to(outside_script)
    traversal_argv = (sys.executable, "../outside.py")
    symlink_argv = (sys.executable, "linked.py")
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.process_run,),
            allowed_argv=(traversal_argv, symlink_argv),
            effects=(SideEffect.process_execute,),
        )
    )

    with pytest.raises(DevelopmentToolDenied, match="escape"):
        await tools.process_run(
            ProcessRunRequest(argv=traversal_argv, cwd="source")
        )
    with pytest.raises(DevelopmentToolDenied, match="symlink"):
        await tools.process_run(
            ProcessRunRequest(argv=symlink_argv, cwd="source")
        )


@pytest.mark.asyncio
async def test_git_status_and_diff_are_dedicated_read_only_tools(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Test"],
        check=True,
    )
    tracked = source / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "baseline"], check=True)
    tracked.write_text("after\n", encoding="utf-8")

    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.git_status, DevelopmentToolName.git_diff),
        )
    )
    before_head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    status = await tools.git_status(GitStatusRequest(cwd="source"))
    diff = await tools.git_diff(
        GitDiffRequest(cwd="source", paths=("tracked.txt",))
    )
    after_head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
    ).strip()

    assert status.exit_code == 0
    assert "tracked.txt" in status.stdout
    assert "-before" in diff.stdout
    assert "+after" in diff.stdout
    assert before_head == after_head


@pytest.mark.asyncio
async def test_git_tools_never_disclose_paths_outside_the_role_grant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    private = root / "private"
    source.mkdir(parents=True)
    private.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
    )
    (source / "visible.txt").write_text("before\n", encoding="utf-8")
    (private / "secret-name.txt").write_text(
        "TOP-SECRET-BEFORE\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
    (source / "visible.txt").write_text("after\n", encoding="utf-8")
    (private / "secret-name.txt").write_text(
        "TOP-SECRET-AFTER\n",
        encoding="utf-8",
    )

    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(
                DevelopmentToolName.git_status,
                DevelopmentToolName.git_diff,
            ),
        )
    )
    status = await tools.git_status(GitStatusRequest(cwd="source"))
    # An empty request is projected to the grant, never to the whole clone.
    diff = await tools.git_diff(GitDiffRequest(cwd="source"))

    assert status.exit_code == 0, status.stderr
    assert diff.exit_code == 0, diff.stderr
    assert "source/visible.txt" in status.stdout
    assert "private" not in status.stdout
    assert "secret-name" not in status.stdout
    assert "visible.txt" in diff.stdout
    assert "private" not in diff.stdout
    assert "TOP-SECRET" not in diff.stdout
    assert status.argv[-2:] == ("--", "source")
    assert diff.argv[-2:] == ("--", "source")


@pytest.mark.asyncio
async def test_git_tools_reject_metadata_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "source"
    outside_git = tmp_path / "outside-git"
    source.mkdir(parents=True)
    outside_git.mkdir()
    (source / ".git").symlink_to(outside_git, target_is_directory=True)
    tools = DevelopmentWorkspaceTools(
        _authority(
            root,
            tools=(DevelopmentToolName.git_status,),
        )
    )

    with pytest.raises(DevelopmentToolDenied, match="metadata must not be a symlink"):
        await tools.git_status(GitStatusRequest(cwd="source"))


def test_autonomous_handoff_is_identical_or_strictly_narrower(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "source").mkdir(parents=True)
    argv = (sys.executable, "test.py")
    parent = _authority(
        root,
        tools=(
            DevelopmentToolName.workspace_read,
            DevelopmentToolName.workspace_write,
            DevelopmentToolName.process_run,
        ),
        allowed_paths=("source", "work"),
        allowed_argv=(argv,),
        effects=(SideEffect.workspace_write, SideEffect.process_execute),
        max_timeout_seconds=10,
        max_output_bytes=10_000,
    )

    identical = derive_autonomous_handoff_authority(parent)
    assert identical.workspace_grant == parent.workspace_grant
    assert identical.enabled_tools == parent.enabled_tools
    assert identical.max_timeout_seconds == parent.max_timeout_seconds
    assert identical.max_output_bytes == parent.max_output_bytes
    assert identical.autonomous_handoff

    narrower = derive_autonomous_handoff_authority(
        parent,
        AutonomousHandoffAuthorityRequest(
            enabled_tools=(DevelopmentToolName.workspace_read,),
            allowed_paths=("source",),
            max_timeout_seconds=5,
            max_output_bytes=1_000,
        ),
    )
    assert narrower.workspace_grant.allowed_paths == ("source",)
    assert narrower.workspace_grant.allowed_argv == ()
    assert narrower.workspace_grant.allowed_side_effects == ()
    assert parent.workspace_grant.allowed_paths == ("source", "work")

    with pytest.raises(DevelopmentToolDenied, match="add workspace paths"):
        derive_autonomous_handoff_authority(
            parent,
            AutonomousHandoffAuthorityRequest(
                enabled_tools=(DevelopmentToolName.workspace_read,),
                allowed_paths=("source", "extra"),
                max_timeout_seconds=5,
                max_output_bytes=1_000,
            ),
        )
    with pytest.raises(DevelopmentToolDenied, match="increase timeout"):
        derive_autonomous_handoff_authority(
            parent,
            AutonomousHandoffAuthorityRequest(
                enabled_tools=(DevelopmentToolName.workspace_read,),
                allowed_paths=("source",),
                max_timeout_seconds=11,
                max_output_bytes=1_000,
            ),
        )
    with pytest.raises(DevelopmentToolDenied, match="add argv"):
        derive_autonomous_handoff_authority(
            parent,
            AutonomousHandoffAuthorityRequest(
                enabled_tools=(DevelopmentToolName.process_run,),
                allowed_paths=("source",),
                allowed_argv=((sys.executable, "other.py"),),
                allowed_side_effects=(SideEffect.process_execute,),
                max_timeout_seconds=5,
                max_output_bytes=1_000,
            ),
        )
    with pytest.raises(DevelopmentToolDenied, match="add side effects"):
        derive_autonomous_handoff_authority(
            parent,
            AutonomousHandoffAuthorityRequest(
                enabled_tools=(DevelopmentToolName.workspace_read,),
                allowed_paths=("source",),
                allowed_side_effects=(SideEffect.external_mutation,),
                max_timeout_seconds=5,
                max_output_bytes=1_000,
            ),
        )


def test_workspace_root_itself_cannot_be_a_symlink(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    (real_root / "source").mkdir(parents=True)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    authority = _authority(
        linked_root,
        tools=(DevelopmentToolName.workspace_read,),
    )

    with pytest.raises(DevelopmentToolDenied, match="non-symlink"):
        DevelopmentWorkspaceTools(authority)
