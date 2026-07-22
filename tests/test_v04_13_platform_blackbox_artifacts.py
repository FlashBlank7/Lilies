from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_platform.platform_blackbox_artifacts import (
    ArtifactBinding,
    ArtifactReadRequest,
    ArtifactRegistrationRequest,
    MAX_ARTIFACT_CHUNK_BYTES,
    PlatformBlackboxArtifactConflict,
    PlatformBlackboxArtifactIntegrityError,
    PlatformBlackboxArtifactPathUnsafe,
    PlatformBlackboxArtifactRangeInvalid,
    PlatformBlackboxArtifactScopeDenied,
    PlatformBlackboxArtifactStore,
    PlatformBlackboxArtifactTooLarge,
)


def binding() -> ArtifactBinding:
    return ArtifactBinding(
        assignment_id=uuid4(),
        session_id=uuid4(),
        application_id=uuid4(),
        run_id=f"run-{uuid4()}",
    )


def registration(
    artifact_binding: ArtifactBinding,
    path: str,
    *,
    media_type: str = "text/plain",
    expected_sha256: str | None = None,
) -> ArtifactRegistrationRequest:
    return ArtifactRegistrationRequest(
        binding=artifact_binding,
        relative_path=path,
        media_type=media_type,
        expected_sha256=expected_sha256,
    )


def test_strict_models_reject_paths_media_types_and_secret_fields() -> None:
    artifact_binding = binding()
    for unsafe in (
        "/etc/passwd",
        "C:/Windows/system.ini",
        "../outside",
        "dir/../outside",
        "dir\\outside",
        "./file",
    ):
        with pytest.raises(ValidationError):
            registration(artifact_binding, unsafe)
    with pytest.raises(ValidationError):
        registration(artifact_binding, "result.txt", media_type="text/plain\r\nsecret:yes")
    with pytest.raises(ValidationError):
        ArtifactRegistrationRequest.model_validate(
            {
                **registration(artifact_binding, "result.txt").model_dump(mode="json"),
                "secret": "must-not-be-stored",
            }
        )


@pytest.mark.asyncio
async def test_register_utf8_read_and_restart_without_storing_content(tmp_path) -> None:
    db_path = tmp_path / "agent_platform.db"
    root = tmp_path / "assignment" / "run" / "artifacts"
    root.mkdir(parents=True)
    content = "企业验收结果\n36/36\n"
    (root / "reports").mkdir()
    (root / "reports" / "result.txt").write_text(content, encoding="utf-8")
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(db_path)
    assert await store.initialize() == {"schema_version": 1}

    registered = await store.register_artifact(
        registration(artifact_binding, "reports/result.txt"),
        artifact_root=root,
    )
    assert registered.replayed is False
    assert registered.artifact.root_path == root.resolve()
    assert registered.artifact.relative_path == "reports/result.txt"
    assert registered.artifact.size_bytes == len(content.encode())
    assert registered.artifact.sha256.startswith("sha256:")

    replay = await store.register_artifact(
        registration(artifact_binding, "reports/result.txt"),
        artifact_root=root,
    )
    assert replay.replayed is True
    assert replay.artifact.artifact_id == registered.artifact.artifact_id

    restarted = PlatformBlackboxArtifactStore(db_path)
    await restarted.initialize()
    result = await restarted.read_artifact(
        ArtifactReadRequest(
            artifact_id=registered.artifact.artifact_id,
            binding=artifact_binding,
        ),
        artifact_root=root,
    )
    assert result.encoding == "utf8"
    assert result.content == content
    assert result.sha256 == registered.artifact.sha256
    assert result.offset_bytes == 0
    assert result.chunk_size_bytes == len(content.encode())
    assert result.next_offset_bytes is None
    assert result.complete is True

    with sqlite3.connect(db_path) as connection:
        database_dump = "\n".join(connection.iterdump())
    assert content not in database_dump


@pytest.mark.asyncio
async def test_binary_artifact_projects_base64(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    raw = b"\x00\xff\x10binary\x80"
    (root / "evidence.bin").write_bytes(raw)
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(tmp_path / "agent_platform.db")
    await store.initialize()
    registered = await store.register_artifact(
        registration(
            artifact_binding,
            "evidence.bin",
            media_type="application/octet-stream",
        ),
        artifact_root=root,
    )

    result = await store.read_artifact(
        ArtifactReadRequest(
            artifact_id=registered.artifact.artifact_id,
            binding=artifact_binding,
        ),
        artifact_root=root,
    )
    assert result.encoding == "base64"
    assert result.content == base64.b64encode(raw).decode("ascii")
    assert result.chunk_size_bytes == len(raw)
    assert result.complete is True


@pytest.mark.asyncio
async def test_large_binary_and_control_content_round_trips_in_digest_verified_chunks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    raw = b"\x00control\n\x1b" + bytes(range(256)) * 600
    (root / "large-evidence.bin").write_bytes(raw)
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(tmp_path / "agent_platform.db")
    await store.initialize()
    registered = await store.register_artifact(
        registration(
            artifact_binding,
            "large-evidence.bin",
            media_type="application/octet-stream",
        ),
        artifact_root=root,
    )

    reconstructed = bytearray()
    offset = 0
    results = []
    while True:
        result = await store.read_artifact(
            ArtifactReadRequest(
                artifact_id=registered.artifact.artifact_id,
                binding=artifact_binding,
                offset_bytes=offset,
            ),
            artifact_root=root,
        )
        # The wire is valid JSON even when the underlying bytes contain NUL,
        # escape, and arbitrary binary values.
        serialized = json.dumps(result.model_dump(mode="json"))
        projected = json.loads(serialized)
        chunk = (
            projected["content"].encode("utf-8")
            if projected["encoding"] == "utf8"
            else base64.b64decode(projected["content"])
        )
        assert projected["size_bytes"] == len(raw)
        assert projected["sha256"] == registered.artifact.sha256
        assert projected["offset_bytes"] == offset
        assert projected["chunk_size_bytes"] == len(chunk) <= MAX_ARTIFACT_CHUNK_BYTES
        reconstructed.extend(chunk)
        results.append(result)
        if result.complete:
            assert result.next_offset_bytes is None
            break
        assert result.next_offset_bytes == offset + len(chunk)
        offset = result.next_offset_bytes

    assert bytes(reconstructed) == raw
    replay = await store.read_artifact(
        ArtifactReadRequest(
            artifact_id=registered.artifact.artifact_id,
            binding=artifact_binding,
            offset_bytes=results[0].offset_bytes,
        ),
        artifact_root=root,
    )
    assert replay == results[0]
    with pytest.raises(PlatformBlackboxArtifactRangeInvalid):
        await store.read_artifact(
            ArtifactReadRequest(
                artifact_id=registered.artifact.artifact_id,
                binding=artifact_binding,
                offset_bytes=len(raw) + 1,
            ),
            artifact_root=root,
        )


@pytest.mark.asyncio
async def test_symlink_escape_and_wrong_server_root_are_rejected(tmp_path) -> None:
    root = tmp_path / "artifacts"
    other_root = tmp_path / "other-artifacts"
    root.mkdir()
    other_root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(tmp_path / "agent_platform.db")
    await store.initialize()

    with pytest.raises(PlatformBlackboxArtifactPathUnsafe):
        await store.register_artifact(
            registration(artifact_binding, "escape.txt"),
            artifact_root=root,
        )

    (root / "safe.txt").write_text("safe", encoding="utf-8")
    registered = await store.register_artifact(
        registration(artifact_binding, "safe.txt"),
        artifact_root=root,
    )
    with pytest.raises(PlatformBlackboxArtifactScopeDenied):
        await store.read_artifact(
            ArtifactReadRequest(
                artifact_id=registered.artifact.artifact_id,
                binding=artifact_binding,
            ),
            artifact_root=other_root,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["assignment_id", "session_id", "application_id", "run_id"])
async def test_cross_binding_reads_are_denied(tmp_path, changed_field: str) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "result.json").write_text("{}", encoding="utf-8")
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(tmp_path / "agent_platform.db")
    await store.initialize()
    registered = await store.register_artifact(
        registration(artifact_binding, "result.json", media_type="application/json"),
        artifact_root=root,
    )
    replacement = f"run-{uuid4()}" if changed_field == "run_id" else uuid4()
    wrong_binding = artifact_binding.model_copy(update={changed_field: replacement})

    with pytest.raises(PlatformBlackboxArtifactScopeDenied):
        await store.read_artifact(
            ArtifactReadRequest(
                artifact_id=registered.artifact.artifact_id,
                binding=wrong_binding,
            ),
            artifact_root=root,
        )


@pytest.mark.asyncio
async def test_digest_tamper_expected_digest_oversize_and_registry_conflict(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    artifact_file = root / "result.txt"
    artifact_file.write_text("12345678", encoding="utf-8")
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(
        tmp_path / "agent_platform.db",
        hard_max_bytes=8,
    )
    await store.initialize()
    with pytest.raises(PlatformBlackboxArtifactIntegrityError):
        await store.register_artifact(
            registration(
                artifact_binding,
                "result.txt",
                expected_sha256="sha256:" + "0" * 64,
            ),
            artifact_root=root,
        )
    registered = await store.register_artifact(
        registration(artifact_binding, "result.txt"),
        artifact_root=root,
    )
    with pytest.raises(PlatformBlackboxArtifactConflict):
        await store.register_artifact(
            registration(artifact_binding, "result.txt", media_type="text/csv"),
            artifact_root=root,
        )
    first_chunk = await store.read_artifact(
        ArtifactReadRequest(
            artifact_id=registered.artifact.artifact_id,
            binding=artifact_binding,
            max_bytes=4,
        ),
        artifact_root=root,
    )
    assert first_chunk.content == "1234"
    assert first_chunk.chunk_size_bytes == 4
    assert first_chunk.next_offset_bytes == 4
    assert first_chunk.complete is False
    with pytest.raises(PlatformBlackboxArtifactRangeInvalid):
        await store.read_artifact(
            ArtifactReadRequest(
                artifact_id=registered.artifact.artifact_id,
                binding=artifact_binding,
                offset_bytes=9,
            ),
            artifact_root=root,
        )

    artifact_file.write_text("abcdefgh", encoding="utf-8")
    with pytest.raises(PlatformBlackboxArtifactIntegrityError):
        await store.read_artifact(
            ArtifactReadRequest(
                artifact_id=registered.artifact.artifact_id,
                binding=artifact_binding,
            ),
            artifact_root=root,
        )
    (root / "large.bin").write_bytes(b"x" * 9)
    with pytest.raises(PlatformBlackboxArtifactTooLarge):
        await store.register_artifact(
            registration(
                artifact_binding,
                "large.bin",
                media_type="application/octet-stream",
            ),
            artifact_root=root,
        )


@pytest.mark.asyncio
async def test_registry_mapping_is_database_immutable(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "result.txt").write_text("immutable", encoding="utf-8")
    artifact_binding = binding()
    store = PlatformBlackboxArtifactStore(tmp_path / "agent_platform.db")
    await store.initialize()
    registered = await store.register_artifact(
        registration(artifact_binding, "result.txt"),
        artifact_root=root,
    )

    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="registry is immutable"):
            connection.execute(
                "UPDATE platform_blackbox_artifacts SET sha256=? WHERE artifact_id=?",
                ("sha256:" + "f" * 64, str(registered.artifact.artifact_id)),
            )
