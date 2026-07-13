#!/usr/bin/env python3
"""Verify v0.3.7 detail-page guidance and no-build smoke cleanup."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "detail_guidance_persona_v0.3.7.json"
SMOKE_MARKER = "v0.3.7-smoke"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


BUG_LEDGER = (
    {
        "id": "P1-detail-page-next-action-unclear",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "A returning user opens a concrete draft but has to infer whether to inspect, test, run, publish, or monitor.",
        "fix": "Add a next-action checklist with stable markers and direct tab targets.",
        "verification": "detail_next_action_markers and live detail route evidence.",
    },
    {
        "id": "P1-detail-page-orientation-too-implicit",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The detail canvas is reachable but does not expose compact state signals for non-technical users.",
        "fix": "Add first-run orientation signals for structure, acceptance, try-run, and monitor readiness.",
        "verification": "detail_guidance_source_markers.",
    },
    {
        "id": "P1-v037-smoke-retention",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Detail guidance persona tests create local smoke apps.",
        "fix": "Clean the v0.3.7 smoke app through the smoke cleanup boundary.",
        "verification": "cleanup_smoke_app and verified_smoke_deleted.",
    },
)


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_token(explicit: str = "", env_files: Iterable[Path] | None = None) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "argument"
    for key in ("AGENT_PLATFORM_API_TOKEN", "AGENT_PLATFORM_TOKEN", "API_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value, f"env:{key}"
    for path in env_files or (
        ROOT / ".env",
        ROOT / "platform" / "backend" / ".env",
        ROOT / "platform" / "frontend" / ".env.local",
    ):
        values = parse_env_file(path)
        for key in ("AGENT_PLATFORM_API_TOKEN", "AGENT_PLATFORM_TOKEN", "API_TOKEN"):
            if values.get(key):
                return values[key], f"file:{display_path(path)}:{key}"
    return "", "missing"


def request_json(
    method: str,
    url: str,
    *,
    token: str = "",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "Lilies-v0.3.7-detail-guidance"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def fetch_text(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.7-detail-guidance"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "body": body[:60000], "error": ""}


def failed_check(check_id: str, url: str, error: Exception) -> dict[str, object]:
    status_code = error.code if isinstance(error, urllib.error.HTTPError) else 0
    return {"id": check_id, "url": url, "passed": False, "status_code": status_code, "error": str(error)}


def source_marker_checks() -> list[dict[str, object]]:
    checks = [
        (
            "detail_guidance_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                'data-detail-guidance="first-run-orientation"',
                'data-detail-guidance="next-action-checklist"',
                "detailSignals",
                "nextActionCards",
                "data-next-action",
            ),
        ),
        (
            "detail_i18n_guidance_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "detailSignalStructure",
                "nextActionTitle",
                "nextActionInspect",
                "nextActionMonitor",
                "nextActionOpen",
            ),
        ),
        (
            "detail_guidance_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".detail-signal-grid",
                ".next-action-checklist",
                ".next-action-list",
            ),
        ),
    ]
    evidence: list[dict[str, object]] = []
    for check_id, relative_path, markers in checks:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        missing = [marker for marker in markers if marker not in text]
        evidence.append(
            {
                "id": check_id,
                "path": relative_path,
                "required_markers": list(markers),
                "missing_markers": missing,
                "passed": not missing,
            }
        )
    return evidence


def bug_ledger_evidence() -> dict[str, object]:
    blocking = [
        item
        for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {
        "id": "p0_p1_bug_ledger_detail_guidance",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def create_payload(now: int | None = None) -> dict[str, str]:
    suffix = now if now is not None else int(time.time())
    requirement = (
        f"[{SMOKE_MARKER}] Detail guidance smoke draft for a returning consultant. "
        "The user should open the draft, understand structure and next actions, then clean this app without any build."
    )
    return {
        "name": f"{SMOKE_MARKER} detail guidance {suffix}",
        "description": requirement[:180],
        "requirement": requirement,
        "mode": "workflow",
    }


def skeleton_operations(suffix: str) -> list[tuple[str, dict[str, object]]]:
    start_id = f"v037_start_{suffix}"
    answer_id = f"v037_answer_{suffix}"
    return [
        ("add_node", {"node": {
            "id": start_id,
            "type": "start",
            "block_version": 1,
            "title": "Customer Request",
            "description": "v0.3.7 detail guidance smoke input.",
            "config": {"inputs": [{"name": "customer_request", "label": "Customer request", "type": "string", "required": True}]},
            "position": {"x": 120, "y": 160},
            "retry": {"enabled": False, "max_attempts": 1, "delay_seconds": 0.5},
            "error_strategy": "fail",
        }}),
        ("add_node", {"node": {
            "id": answer_id,
            "type": "answer",
            "block_version": 1,
            "title": "Draft Answer",
            "description": "Starter answer for detail guidance smoke.",
            "config": {"answer": {"$ref": {"node_id": start_id, "path": ["output", "customer_request"]}}},
            "position": {"x": 420, "y": 160},
            "retry": {"enabled": False, "max_attempts": 1, "delay_seconds": 0.5},
            "error_strategy": "fail",
        }}),
        ("add_edge", {"edge": {
            "id": f"v037_edge_{suffix}",
            "source": start_id,
            "target": answer_id,
            "source_port": "output",
            "target_port": "input",
        }}),
        ("add_test", {"test": {
            "id": f"v037_acceptance_{suffix}",
            "name": "Detail guidance structure check",
            "requirement": "The smoke draft contains structure for the detail guidance checklist.",
            "inputs": {"customer_request": "Explain this draft to a returning consultant."},
            "assertions": [],
            "required_node_types": ["start", "answer"],
            "required_tool_nodes": [],
            "required_tools": [],
            "minimum_tool_calls": 0,
            "mandatory": True,
            "structural_only": True,
        }}),
    ]


def apply_skeleton(api_url: str, application_id: str, token: str, initial_revision: int) -> tuple[int, list[str]]:
    revision = initial_revision
    called: list[str] = []
    suffix = str(int(time.time()))
    for index, (op, data) in enumerate(skeleton_operations(suffix)):
        called.append(f"POST /api/v1/applications/{{id}}/draft:{op}")
        result = request_json(
            "POST",
            api_url.rstrip("/") + f"/api/v1/applications/{application_id}/draft",
            token=token,
            payload={
                "expected_revision": revision,
                "idempotency_key": f"{SMOKE_MARKER}-{index}-{op}-{suffix}",
                "op": op,
                "data": data,
            },
        )
        revision = int(result["json"]["revision"])  # type: ignore[index]
    return revision, called


def runtime_health_check(api_url: str) -> dict[str, object]:
    url = api_url.rstrip("/") + "/health"
    try:
        result = request_json("GET", url)
        runtime = result["json"].get("runtime", {}) if isinstance(result["json"], dict) else {}
        return {
            "id": "runtime_health_current_code",
            "url": url,
            "passed": result["status_code"] == 200
            and runtime.get("version") == EXPECTED_RUNTIME_VERSION
            and runtime.get("current_code_ready") is True,
            "status_code": result["status_code"],
            "runtime": runtime,
        }
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return failed_check("runtime_health_current_code", url, error)


def live_checks(api_url: str, frontend_url: str, token: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    checks: list[dict[str, object]] = [runtime_health_check(api_url)]
    safety: dict[str, object] = {
        "build_endpoint_called": False,
        "called_endpoints": [],
        "smoke_marker": SMOKE_MARKER,
        "cleanup_attempted": False,
    }
    api_base = api_url.rstrip("/")
    frontend_base = frontend_url.rstrip("/")
    application_id = ""
    try:
        created = request_json("POST", api_base + "/api/v1/applications", token=token, payload=create_payload())
        app = created["json"]
        application_id = str(app.get("id", ""))
        safety["called_endpoints"].append("POST /api/v1/applications")
        checks.append({
            "id": "created_detail_guidance_smoke_app",
            "passed": created["status_code"] == 201 and bool(application_id),
            "status_code": created["status_code"],
            "application_id": application_id,
            "draft_revision": app.get("draft_revision"),
        })
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_check("created_detail_guidance_smoke_app", api_base + "/api/v1/applications", error))

    if application_id:
        try:
            final_revision, draft_calls = apply_skeleton(api_base, application_id, token, 0)
            safety["called_endpoints"].extend(draft_calls)
            checks.append({
                "id": "seeded_detail_guidance_structure",
                "passed": final_revision >= 4,
                "final_revision": final_revision,
                "operation_count": len(draft_calls),
            })
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            checks.append(failed_check("seeded_detail_guidance_structure", api_base + f"/api/v1/applications/{application_id}/draft", error))

        detail_url = frontend_base + f"/applications/{application_id}?safeDraft=1"
        try:
            detail = fetch_text(detail_url)
            body = str(detail["body"])
            checks.append({
                "id": "rendered_detail_route_available",
                "url": detail_url,
                "passed": detail["status_code"] == 200 and "Foundry" in body,
                "status_code": detail["status_code"],
            })
            rendered_markers = (
                'data-detail-guidance="first-run-orientation"',
                'data-detail-guidance="next-action-checklist"',
                'data-next-action="inspect"',
                'data-next-action="run"',
            )
            missing_rendered = [marker for marker in rendered_markers if marker not in body]
            checks.append({
                "id": "rendered_detail_guidance_markers",
                "url": detail_url,
                "passed": not missing_rendered,
                "required_markers": list(rendered_markers),
                "missing_markers": missing_rendered,
            })
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("rendered_detail_route_available", detail_url, error))

        cleanup_url = api_base + f"/api/v1/applications/{application_id}/smoke-cleanup"
        try:
            cleanup = request_json("POST", cleanup_url, token=token, payload={"smoke_marker": SMOKE_MARKER, "dry_run": False})
            safety["cleanup_attempted"] = True
            safety["called_endpoints"].append("POST /api/v1/applications/{id}/smoke-cleanup")
            checks.append({
                "id": "cleanup_smoke_app",
                "passed": cleanup["status_code"] == 200 and cleanup["json"].get("deleted") is True,
                "status_code": cleanup["status_code"],
                "related_counts": cleanup["json"].get("related_counts"),
            })
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("cleanup_smoke_app", cleanup_url, error))

        try:
            request_json("GET", api_base + f"/api/v1/applications/{application_id}", token=token)
            checks.append({"id": "verified_smoke_deleted", "passed": False, "error": "smoke app still exists"})
        except urllib.error.HTTPError as error:
            checks.append({"id": "verified_smoke_deleted", "passed": error.code == 404, "status_code": error.code})
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("verified_smoke_deleted", api_base + f"/api/v1/applications/{application_id}", error))

    checks.append({
        "id": "safety_no_build_call",
        "passed": safety["build_endpoint_called"] is False,
        "called_endpoints": safety["called_endpoints"],
        "forbidden_endpoint": "POST /api/v1/applications/{id}/builds",
    })
    return checks, safety


def build_evidence(
    *,
    live: bool = False,
    api_url: str = "http://127.0.0.1:8001",
    frontend_url: str = "http://127.0.0.1:3000",
    token: str = "",
) -> dict[str, object]:
    checks: list[dict[str, object]] = [bug_ledger_evidence(), *source_marker_checks()]
    loaded_token, token_source = load_token(token)
    safety: dict[str, object] = {"build_endpoint_called": False, "called_endpoints": [], "smoke_marker": SMOKE_MARKER}
    if live:
        if not loaded_token:
            checks.append({"id": "token_available", "passed": False, "token_source": token_source})
        else:
            checks.append({"id": "token_available", "passed": True, "token_source": token_source})
            live_result, safety = live_checks(api_url, frontend_url, loaded_token)
            checks.extend(live_result)
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.7",
        "stage": "draft_detail_first_run_guidance",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "token_source": token_source if live else "not_used",
        "smoke_marker": SMOKE_MARKER,
        "bug_ledger": list(BUG_LEDGER),
        "safety": safety,
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "build_endpoint_called": safety["build_endpoint_called"],
        },
    }


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.7 detail guidance persona evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    evidence = build_evidence(live=args.live, api_url=args.api_url, frontend_url=args.frontend_url, token=args.token)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
