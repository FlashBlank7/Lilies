from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_platform.task_packages import (
    BUILDER_API_MANUAL_FILE,
    TaskPackageError,
    TaskPackageManager,
    TaskPackageSecurityError,
)
from scripts.experiments.exp_lilies_001.generate_package import generate


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "EXP-LILIES-001" / "20"
    generate(source)
    return source


def test_revision_twenty_freezes_the_builder_manual_as_public_input(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    manager = TaskPackageManager(tmp_path / "state")
    repository_packages = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "experiments"
        / "lilies-collaboration"
        / "EXP-LILIES-001"
    )
    for revision in range(1, 20):
        manager.freeze_revision(repository_packages / str(revision))

    package = manager.freeze_revision(source)
    entries = {entry.path: entry for entry in package.record.immutable_files}

    assert BUILDER_API_MANUAL_FILE in entries
    assert not BUILDER_API_MANUAL_FILE.startswith("protected/")
    manual = json.loads((source / BUILDER_API_MANUAL_FILE).read_bytes())
    assert (
        manual["schema_version"]
        == "v0.4.13-t01h-external-builder-api-manual-1"
    )


def test_builder_manual_rejects_live_task_or_collaboration_credentials(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    path = source / BUILDER_API_MANUAL_FILE
    manual = json.loads(path.read_bytes())
    manual["leaked"] = (
        "lpt_0123456789abcdef0123456789abcdef_"
        "abcdefghijklmnopqrstuvwxyzABCDEFGH12345678"
    )
    path.write_text(
        json.dumps(
            manual,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        TaskPackageSecurityError,
        match="contains a live credential",
    ):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_builder_manual_requires_canonical_strict_json(tmp_path: Path) -> None:
    source = _source(tmp_path)
    path = source / BUILDER_API_MANUAL_FILE
    manual = json.loads(path.read_bytes())
    path.write_text(json.dumps(manual, indent=2), encoding="utf-8")

    with pytest.raises(
        TaskPackageError,
        match="canonical versioned projection",
    ):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)
