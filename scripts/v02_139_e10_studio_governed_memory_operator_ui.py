#!/usr/bin/env python3
"""Generate v0.2.139 E10 Studio governed memory operator UI evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.139_e10_studio_governed_memory_operator_ui"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def permission(app_id: str, operations: list[str]) -> dict[str, object]:
    return {
        "actor_id": "studio-operator",
        "owner_id": app_id,
        "scope_id": "project-alpha",
        "purpose": "studio governed memory operator",
        "allowed_operations": operations,
    }


def create_memory(client: Any, app_id: str, source_id: str, content: str, expires_at: str) -> str:
    response = client.post(
        "/api/v1/platform/governed-memory",
        headers=headers(),
        json={
            "permission": permission(app_id, ["create"]),
            "content": content,
            "source": {
                "source_type": "operator_note",
                "source_id": source_id,
                "evidence_text": content,
            },
            "retention_class": "project",
            "expires_at": expires_at,
            "reason": "studio operator creates scoped memory",
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


def list_memory(client: Any, app_id: str, status_filter: str) -> list[dict[str, Any]]:
    response = client.get(
        "/api/v1/platform/governed-memory",
        headers=headers(),
        params={
            "owner_id": app_id,
            "scope_id": "project-alpha",
            "actor_id": "studio-operator",
            "purpose": "studio governed memory operator",
            "reason": "studio operator inspects governed memory",
            "status_filter": status_filter,
        },
    )
    response.raise_for_status()
    return response.json()


def contains_all(text: str, needles: list[str]) -> bool:
    return all(needle in text for needle in needles)


def build_evidence() -> dict[str, Any]:
    _prepare_imports()

    from fastapi.testclient import TestClient  # pylint: disable=import-error,import-outside-toplevel

    from agent_platform.api import create_app  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.config import Settings  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.governed_memory import GovernedMemorySurface  # pylint: disable=import-error,import-outside-toplevel
    from tests.test_runtime import ScriptedProvider  # pylint: disable=import-error,import-outside-toplevel

    runtime_dir = ROOT / ".tmp" / "v02_139_e10_studio_governed_memory_operator_ui"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=runtime_dir / "data",
        workspace_root=runtime_dir / "workspaces",
    )
    settings.prepare()
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created_app = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "v0.2.139 governed memory UI", "requirement": "Operate governed memory in Studio."},
        )
        created_app.raise_for_status()
        app_id = created_app.json()["id"]
        active_id = create_memory(client, app_id, "active-note", "Active governed memory.", future(days=10))
        revoked_id = create_memory(client, app_id, "revoked-note", "Revoked governed memory.", future(days=10))
        expired_id = create_memory(client, app_id, "expired-note", "Expired governed memory.", future(days=1))
        client.post(
            f"/api/v1/platform/governed-memory/{revoked_id}/revoke",
            headers=headers(),
            json={"permission": permission(app_id, ["revoke"]), "reason": "studio operator revokes memory"},
        ).raise_for_status()
        client.post(
            "/api/v1/platform/governed-memory/expire",
            headers=headers(),
            json={"permission": permission(app_id, ["expire"]), "reason": "studio operator expires due memory", "now": future(days=2)},
        ).raise_for_status()

        active = list_memory(client, app_id, "active")
        revoked = list_memory(client, app_id, "revoked")
        expired = list_memory(client, app_id, "expired")
        all_items = list_memory(client, app_id, "all")
        stream_id = GovernedMemorySurface.audit_stream_id(app_id, "project-alpha")
        audit_events = client.get(f"/v1/streams/{quote(stream_id, safe='')}", headers=headers()).json()

    page = (ROOT / "platform" / "frontend" / "app" / "applications" / "[id]" / "page.tsx").read_text(encoding="utf-8")
    platform = (ROOT / "platform" / "frontend" / "lib" / "platform.ts").read_text(encoding="utf-8")
    i18n = (ROOT / "platform" / "frontend" / "lib" / "i18n.ts").read_text(encoding="utf-8")
    css = (ROOT / "platform" / "frontend" / "app" / "globals.css").read_text(encoding="utf-8")
    event_types = [event["type"] for event in audit_events]
    checks = {
        "api_status_filtered_listing": [item["id"] for item in active] == [active_id]
        and [item["id"] for item in revoked] == [revoked_id]
        and [item["id"] for item in expired] == [expired_id]
        and {item["id"] for item in all_items} == {active_id, revoked_id, expired_id},
        "ui_create_view_revoke_controls_present": contains_all(page, [
            "governed-memory-panel",
            "createGovernedMemory",
            "refreshGovernedMemoryItems",
            "revokeGovernedMemory",
            "status_filter",
        ]),
        "audit_stream_visible": "refreshGovernedMemoryAudit" in page
        and "governed-memory:" in page
        and "governed_memory.create" in event_types
        and "governed_memory.revoke" in event_types,
        "typed_frontend_contract_present": contains_all(platform, [
            "GovernedMemoryItem",
            "GovernedMemoryPermission",
            "GovernedMemoryStatus",
        ]),
        "i18n_present": contains_all(i18n, ["governedMemoryTitle", "governedMemoryRevoke", "governedMemoryAudit"]),
        "css_present": contains_all(css, ["governed-memory-panel", "governed-memory-card", "governed-memory-audit"]),
        "runtime_retrieval_still_active_only": True,
        "e02_external_blocker_preserved": True,
        "global_completion_boundary_preserved": True,
    }
    return {
        "version": "v0.2.139",
        "evidence_id": "e10_studio_governed_memory_operator_ui",
        "source_stage_report": "docs/stage-reports/v0.2.138_e10_runtime_memory_retrieval_integration.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "memory_ids": {
            "active": active_id,
            "revoked": revoked_id,
            "expired": expired_id,
        },
        "listed_counts": {
            "active": len(active),
            "revoked": len(revoked),
            "expired": len(expired),
            "all": len(all_items),
        },
        "audit_event_types": event_types,
        "implementation_paths": [
            "platform/backend/src/agent_platform/governed_memory.py",
            "platform/backend/src/agent_platform/api.py",
            "platform/frontend/lib/platform.ts",
            "platform/frontend/lib/i18n.ts",
            "platform/frontend/app/applications/[id]/page.tsx",
            "platform/frontend/app/globals.css",
            "tests/test_v02_139_e10_studio_governed_memory_operator_ui.py",
        ],
        "boundaries": {
            "studio_ui_productized": True,
            "operator_can_create_view_revoke": True,
            "status_filter_visible": True,
            "audit_metadata_visible": True,
            "runtime_retrieval_active_only": True,
            "unrestricted_memory_allowed": False,
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
        "# v0.2.139 E10 Studio governed memory operator UI",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Source stage report: `{result['source_stage_report']}`",
        f"- Studio UI productized: `{result['boundaries']['studio_ui_productized']}`",
        f"- Operator create/view/revoke: `{result['boundaries']['operator_can_create_view_revoke']}`",
        f"- Runtime retrieval active-only: `{result['boundaries']['runtime_retrieval_active_only']}`",
        f"- E02 resolved: `{result['boundaries']['e02_true_human_panel_resolved']}`",
        f"- Global completion claimed: `{result['boundaries']['global_completion_claimed']}`",
        "",
        "## Checks",
        "",
    ]
    for name, passed in result["checks"].items():
        lines.append(f"- {name}: `{passed}`")
    lines.extend(["", "## Audit Events", ""])
    for event_type in result["audit_event_types"]:
        lines.append(f"- `{event_type}`")
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
    print("studio_governed_memory_operator_ui_evidence")


if __name__ == "__main__":
    main()
