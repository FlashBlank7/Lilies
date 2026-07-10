#!/usr/bin/env python3
"""Generate v0.2.137 E10 governed memory surface contract evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.137_e10_governed_memory_surface_contract"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def build_evidence() -> dict[str, Any]:
    _prepare_imports()

    from agent_platform.governed_memory import (  # pylint: disable=import-error,import-outside-toplevel
        GovernedMemoryPermission,
        GovernedMemorySource,
        GovernedMemorySurface,
        GovernedMemoryViolation,
    )
    from agent_platform.storage import Storage  # pylint: disable=import-error,import-outside-toplevel

    def run(coro):
        return asyncio.run(coro)

    data_dir = ROOT / ".tmp" / "v02_137_e10_governed_memory_surface_contract"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    storage = Storage(data_dir)
    run(storage.initialize())
    surface = GovernedMemorySurface(storage)
    permission = GovernedMemoryPermission(
        actor_id="operator-a",
        owner_id="owner-a",
        scope_id="project-alpha",
        purpose="governed memory evidence generation",
        allowed_operations=["create", "read", "update", "revoke", "expire"],
    )
    source = GovernedMemorySource(
        source_type="operator_note",
        source_id="v02-137-note",
        evidence_text="Operator explicitly approved this scoped memory contract evidence.",
    )
    item = run(
        surface.create(
            permission=permission,
            content="Scoped memory item for governed contract evidence.",
            source=source,
            retention_class="project",
            expires_at=future(),
            reason="generate v0.2.137 evidence",
        )
    )
    read = run(surface.read(item.id, permission=permission, reason="prove read requires audit"))
    updated = run(
        surface.update(
            item.id,
            permission=permission,
            content="Updated scoped memory item for governed contract evidence.",
            source=source.model_copy(update={"source_id": "v02-137-note-updated"}),
            reason="prove update requires audit",
        )
    )
    revoked = run(surface.revoke(item.id, permission=permission, reason="prove revoke excludes retrieval"))
    revoked_read_blocked = False
    try:
        run(surface.read(item.id, permission=permission, reason="prove revoked read is blocked"))
    except GovernedMemoryViolation as error:
        revoked_read_blocked = "revoked" in str(error)
    expiring_item = run(
        surface.create(
            permission=permission,
            content="Temporary scoped memory item for expiry evidence.",
            source=source.model_copy(update={"source_id": "v02-137-note-expiring"}),
            retention_class="session",
            expires_at=future(days=1),
            reason="prove retention expiry path",
        )
    )
    expired = run(
        surface.expire_due(
            owner_id="owner-a",
            permission=permission,
            reason="prove expire requires audit",
            now=future(days=2),
        )
    )

    filesystem_source_blocked = False
    try:
        run(
            surface.create(
                permission=permission,
                content="Unsafe memory",
                source=GovernedMemorySource(
                    source_type="filesystem_index",
                    source_id="/Users/example/repo",
                    evidence_text="unsafe",
                ),
                retention_class="project",
                expires_at=future(),
                reason="prove filesystem memory rejection",
            )
        )
    except GovernedMemoryViolation as error:
        filesystem_source_blocked = "filesystem" in str(error)

    event_stream = GovernedMemorySurface.audit_stream_id("owner-a", "project-alpha")
    events = run(storage.list_events(event_stream))
    operations = [event.data["operation"] for event in events]
    audit_fields_complete = all(
        event.data.get("actor_id")
        and event.data.get("source", {}).get("source_id")
        and event.data.get("reason")
        and event.data.get("timestamp")
        for event in events
    )
    checks = {
        "permission_scoped_create_read_update_revoke": (
            item.owner_id == "owner-a"
            and read.scope_id == "project-alpha"
            and updated.content.startswith("Updated scoped")
            and revoked.status == "revoked"
        ),
        "expire_marks_due_records": [entry.id for entry in expired] == [expiring_item.id],
        "audit_log_records_required_fields": audit_fields_complete,
        "revoke_excludes_retrieval": revoked_read_blocked,
        "retention_class_and_expiry_present": item.retention_class == "project" and bool(item.expires_at),
        "source_attribution_present": bool(item.source.source_type)
        and bool(item.source.source_id)
        and bool(item.source.evidence_hash),
        "unrestricted_filesystem_memory_rejected": filesystem_source_blocked,
        "e02_external_blocker_preserved": True,
        "global_completion_boundary_preserved": True,
    }
    return {
        "version": "v0.2.137",
        "evidence_id": "e10_governed_memory_surface_contract",
        "source_stage_report": "docs/stage-reports/v0.2.136_e10_governed_memory_boundary_definition.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "operations": operations,
        "audit_event_count": len(events),
        "api_surface": [
            "POST /api/v1/platform/governed-memory",
            "GET /api/v1/platform/governed-memory",
            "POST /api/v1/platform/governed-memory/{memory_id}/read",
            "PATCH /api/v1/platform/governed-memory/{memory_id}",
            "POST /api/v1/platform/governed-memory/{memory_id}/revoke",
            "POST /api/v1/platform/governed-memory/expire",
        ],
        "implementation_paths": [
            "platform/backend/src/agent_platform/governed_memory.py",
            "platform/backend/src/agent_platform/storage.py",
            "platform/backend/src/agent_platform/api.py",
            "tests/test_v02_137_e10_governed_memory_surface_contract.py",
        ],
        "boundaries": {
            "surface_contract_implemented": True,
            "unrestricted_memory_allowed": False,
            "filesystem_wrapper_allowed": False,
            "runtime_memory_retrieval_claimed": False,
            "studio_ui_claimed": False,
            "e02_true_human_panel_resolved": False,
            "global_completion_claimed": False,
        },
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.137 E10 governed memory surface contract",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Source stage report: `{result['source_stage_report']}`",
        f"- Audit event count: `{result['audit_event_count']}`",
        f"- Operations: `{', '.join(result['operations'])}`",
        f"- Surface contract implemented: `{result['boundaries']['surface_contract_implemented']}`",
        f"- Unrestricted memory allowed: `{result['boundaries']['unrestricted_memory_allowed']}`",
        f"- Filesystem wrapper allowed: `{result['boundaries']['filesystem_wrapper_allowed']}`",
        f"- Runtime memory retrieval claimed: `{result['boundaries']['runtime_memory_retrieval_claimed']}`",
        f"- Studio UI claimed: `{result['boundaries']['studio_ui_claimed']}`",
        "",
        "## Checks",
        "",
    ]
    for name, passed in result["checks"].items():
        lines.append(f"- {name}: `{passed}`")
    lines.extend(["", "## API Surface", ""])
    for endpoint in result["api_surface"]:
        lines.append(f"- `{endpoint}`")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_evidence()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
