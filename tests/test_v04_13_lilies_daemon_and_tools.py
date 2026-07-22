from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_platform.lilies_config import LiliesSettings, x25519_public_key
from agent_platform.lilies_daemon import (
    DAEMON_INFO_FIELDS,
    read_daemon_info,
    remove_daemon_info,
    write_daemon_info,
)
from agent_platform.lilies_tools import (
    LiliesToolContext,
    LiliesToolError,
    WorkspaceReadInput,
    build_lilies_core_registry,
)


def test_lilies_settings_own_private_data_and_identity(tmp_path: Path) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies")
    settings.prepare()

    assert settings.data_dir == (tmp_path / "lilies").resolve()
    assert settings.resolved_workspace_root == settings.data_dir / "workspaces"
    assert settings.identity_key_file.stat().st_mode & 0o777 == 0o600
    private_bytes = settings.identity_key_file.read_bytes()
    public_bytes = x25519_public_key(private_bytes)
    assert settings.daemon_fingerprint() == f"sha256:{hashlib.sha256(public_bytes).hexdigest()}"
    assert settings.daemon_fingerprint() != (
        f"sha256:{hashlib.sha256(private_bytes).hexdigest()}"
    )


def test_x25519_public_key_matches_rfc_7748_vector() -> None:
    private_key = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645"
        "df4c2f87ebc0992ab177fba51db92c2a"
    )
    expected_public_key = bytes.fromhex(
        "8520f0098930a754748b7ddcb43ef75a0"
        "dbf3a0d26381af4eba4a98eaa9b4e6a"
    )
    assert x25519_public_key(private_key) == expected_public_key


def test_lilies_refuses_platform_data_directory_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    monkeypatch.setenv("DATA_DIR", str(shared))
    settings = LiliesSettings(data_dir=shared)

    with pytest.raises(ValueError, match="must not be the platform DATA_DIR"):
        settings.prepare()


def test_lilies_refuses_default_or_existing_platform_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATA_DIR", raising=False)

    with pytest.raises(ValueError, match="must not be the platform DATA_DIR"):
        LiliesSettings(data_dir=Path("data")).prepare()

    existing_platform_dir = tmp_path / "configured-platform"
    existing_platform_dir.mkdir()
    (existing_platform_dir / "agent_platform.db").touch()
    with pytest.raises(ValueError, match="must not be the platform DATA_DIR"):
        LiliesSettings(data_dir=existing_platform_dir).prepare()


def test_daemon_record_is_minimal_private_and_fingerprint_bound(tmp_path: Path) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies")
    settings.prepare()

    created = write_daemon_info(settings, pid=os.getpid())
    persisted = json.loads(settings.daemon_file.read_text(encoding="utf-8"))

    assert set(created) == DAEMON_INFO_FIELDS
    assert persisted == created
    assert settings.daemon_file.stat().st_mode & 0o777 == 0o600
    assert not any(
        forbidden in json.dumps(persisted).casefold()
        for forbidden in ("access_token", "authorization", "api_token", "secret")
    )
    assert read_daemon_info(settings) == created
    assert not remove_daemon_info(settings, expected_pid=os.getpid() + 1)
    assert remove_daemon_info(settings, expected_pid=os.getpid())


@pytest.mark.asyncio
async def test_local_tools_are_strict_scoped_and_permission_classified(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_lilies_core_registry()
    context = LiliesToolContext(session_id="session", workspace=workspace)

    write = registry.get("workspace_write")
    assert write.dangerous and write.mutating and write.side_effecting
    result = await write.execute({"path": "notes/result.txt", "content": "done"}, context)
    assert result.content == "wrote notes/result.txt"

    read = registry.get("workspace_read")
    result = await read.execute({"path": "notes/result.txt"}, context)
    assert result.content.endswith("done")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorkspaceReadInput.model_validate({"path": "notes/result.txt", "unknown": True})
    with pytest.raises(LiliesToolError, match="escapes"):
        await read.execute({"path": "../../outside.txt"}, context)


@pytest.mark.asyncio
async def test_local_tools_reject_workspace_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("not visible", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    registry = build_lilies_core_registry()

    with pytest.raises(LiliesToolError, match="escapes"):
        await registry.get("workspace_read").execute(
            {"path": "escape/secret.txt"},
            LiliesToolContext(session_id="session", workspace=workspace),
        )
