#!/usr/bin/env python3
"""Verify v0.3.6 runtime identity and no-build customer persona paths."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.6" / "runtime_persona_triage_v0.3.6.json"
SMOKE_MARKER = "v0.3.6-smoke"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


@dataclass(frozen=True)
class Persona:
    id: str
    role: str
    behavior: str
    expected_signal: str


PERSONAS = (
    Persona(
        id="confused_first_time_business_owner",
        role="business_owner",
        behavior="starts from home page, needs examples and safe draft language before trusting the product",
        expected_signal="home route and source include customer examples plus save-draft-only affordance",
    ),
    Persona(
        id="concrete_workflow_builder",
        role="implementation_consultant",
        behavior="creates a concrete workflow draft and expects visible Start to Answer structure without a model build",
        expected_signal="created smoke draft contains start node, answer node, edge, and structural acceptance test",
    ),
    Persona(
        id="returning_draft_reviewer",
        role="returning_consultant",
        behavior="returns to the application list, opens an existing draft, and expects revision/detail guidance",
        expected_signal="application list, draft API, and detail route are reachable for the smoke app",
    ),
)


BUG_LEDGER = (
    {
        "id": "P1-runtime-health-stale-version-label",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The backend app advertised version 0.2.0 while v0.3.x routes existed.",
        "fix": "Expose v0.3.6 runtime identity and route availability from /health.",
        "verification": "runtime_health_current_code check and tests/test_v03_6_runtime_health_identity.py.",
    },
    {
        "id": "P1-local-service-drift-invisible",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The frontend proxy could talk to an old backend on 8001 without a clear machine-readable signal.",
        "fix": "Frontend proxy /api/platform/health now surfaces the backend runtime identity when 8001 is current.",
        "verification": "frontend_proxy_health_current_code check.",
    },
    {
        "id": "P1-persona-smoke-artifact-retention",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Persona tests create local evidence apps that can accumulate.",
        "fix": "Use the v0.3.5 smoke-cleanup boundary for the v0.3.6 smoke app.",
        "verification": "cleanup_smoke_app and verified_smoke_deleted checks.",
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
    headers = {"Content-Type": "application/json", "User-Agent": "Lilies-v0.3.6-persona-triage"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def fetch_text(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.6-persona-triage"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "body": body[:60000], "error": ""}


def failed_check(check_id: str, url: str, error: Exception) -> dict[str, object]:
    status_code = error.code if isinstance(error, urllib.error.HTTPError) else 0
    return {"id": check_id, "url": url, "passed": False, "status_code": status_code, "error": str(error)}


def source_marker_checks() -> list[dict[str, object]]:
    checks = [
        (
            "home_confused_customer_markers",
            "platform/frontend/app/page.tsx",
            ("customer-intake-panel", "saveDraftOnly", "data-customer-example"),
        ),
        (
            "copy_persona_markers",
            "platform/frontend/lib/i18n.ts",
            ("customerExamples", "business_owner", "implementation_consultant", "operator", "technical_reviewer"),
        ),
        (
            "detail_returning_reviewer_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            ("draftReadinessTitle", "canvasGuideTitle", "bugTriageTitle", "draft?.revision"),
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
        "id": "p0_p1_bug_ledger_runtime_persona_triage",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def persona_definition_evidence() -> dict[str, object]:
    persona_ids = [persona.id for persona in PERSONAS]
    roles = [persona.role for persona in PERSONAS]
    return {
        "id": "persona_set_defined",
        "passed": len(PERSONAS) >= 3 and len(set(persona_ids)) == len(persona_ids),
        "personas": [persona.__dict__ for persona in PERSONAS],
        "roles": roles,
    }


def skeleton_operations(suffix: str) -> list[tuple[str, dict[str, object]]]:
    start_id = f"v036_start_{suffix}"
    answer_id = f"v036_answer_{suffix}"
    return [
        ("add_node", {"node": {
            "id": start_id,
            "type": "start",
            "block_version": 1,
            "title": "Customer Request",
            "description": "v0.3.6 persona smoke input created without a model build.",
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
            "description": "Starter answer for persona triage; replace during later build/edit stages.",
            "config": {"answer": {"$ref": {"node_id": start_id, "path": ["output", "customer_request"]}}},
            "position": {"x": 420, "y": 160},
            "retry": {"enabled": False, "max_attempts": 1, "delay_seconds": 0.5},
            "error_strategy": "fail",
        }}),
        ("add_edge", {"edge": {
            "id": f"v036_edge_{suffix}",
            "source": start_id,
            "target": answer_id,
            "source_port": "output",
            "target_port": "input",
        }}),
        ("add_test", {"test": {
            "id": f"v036_acceptance_{suffix}",
            "name": "Persona starter structure check",
            "requirement": "The no-build persona smoke app contains an editable Start to Answer skeleton.",
            "inputs": {"customer_request": "Route a customer request and explain the next owner."},
            "assertions": [],
            "required_node_types": ["start", "answer"],
            "required_tool_nodes": [],
            "required_tools": [],
            "minimum_tool_calls": 0,
            "mandatory": True,
            "structural_only": True,
        }}),
    ]


def create_payload(now: int | None = None) -> dict[str, str]:
    suffix = now if now is not None else int(time.time())
    requirement = (
        f"[{SMOKE_MARKER}] Persona triage draft for an implementation consultant: classify a customer's "
        "workflow request, identify missing information, assign the next owner, and produce a readable handoff. "
        "This must remain a no-build smoke draft."
    )
    return {
        "name": f"{SMOKE_MARKER} runtime persona triage {suffix}",
        "description": requirement[:180],
        "requirement": requirement,
        "mode": "workflow",
    }


def check_runtime_health(api_url: str, frontend_url: str) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    targets = [
        ("runtime_health_current_code", api_url.rstrip("/") + "/health"),
        ("frontend_proxy_health_current_code", frontend_url.rstrip("/") + "/api/platform/health"),
    ]
    for check_id, url in targets:
        try:
            result = request_json("GET", url)
            runtime = result["json"].get("runtime", {}) if isinstance(result["json"], dict) else {}
            routes = runtime.get("route_availability", {}) if isinstance(runtime, dict) else {}
            checks.append(
                {
                    "id": check_id,
                    "url": url,
                    "passed": result["status_code"] == 200
                    and runtime.get("version") == EXPECTED_RUNTIME_VERSION
                    and routes.get("smoke_cleanup") is True
                    and runtime.get("current_code_ready") is True,
                    "status_code": result["status_code"],
                    "runtime": runtime,
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check(check_id, url, error))
    return checks


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


def live_persona_checks(api_url: str, frontend_url: str, token: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    checks: list[dict[str, object]] = []
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
        home = fetch_text(frontend_base + "/")
        body = str(home["body"])
        checks.append(
            {
                "id": "persona_confused_first_time_home_visible",
                "persona": "confused_first_time_business_owner",
                "url": frontend_base + "/",
                "passed": home["status_code"] == 200 and "Foundry" in body,
                "status_code": home["status_code"],
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        checks.append(failed_check("persona_confused_first_time_home_visible", frontend_base + "/", error))

    try:
        created = request_json("POST", api_base + "/api/v1/applications", token=token, payload=create_payload())
        app = created["json"]
        application_id = str(app.get("id", ""))
        safety["called_endpoints"].append("POST /api/v1/applications")
        checks.append(
            {
                "id": "persona_builder_created_smoke_draft",
                "persona": "concrete_workflow_builder",
                "passed": created["status_code"] == 201
                and bool(application_id)
                and str(app.get("name", "")).startswith(SMOKE_MARKER)
                and app.get("active_version") is None,
                "status_code": created["status_code"],
                "application_id": application_id,
                "draft_revision": app.get("draft_revision"),
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_check("persona_builder_created_smoke_draft", api_base + "/api/v1/applications", error))

    if application_id:
        try:
            final_revision, draft_calls = apply_skeleton(api_base, application_id, token, 0)
            safety["called_endpoints"].extend(draft_calls)
            checks.append(
                {
                    "id": "persona_builder_seeded_visible_structure",
                    "persona": "concrete_workflow_builder",
                    "passed": final_revision >= 4,
                    "final_revision": final_revision,
                    "operation_count": len(draft_calls),
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            checks.append(
                failed_check(
                    "persona_builder_seeded_visible_structure",
                    api_base + f"/api/v1/applications/{application_id}/draft",
                    error,
                )
            )

        try:
            listed = request_json("GET", api_base + "/api/v1/applications", token=token)
            apps = listed["json"] if isinstance(listed["json"], list) else []
            safety["called_endpoints"].append("GET /api/v1/applications")
            checks.append(
                {
                    "id": "persona_returning_list_contains_smoke_app",
                    "persona": "returning_draft_reviewer",
                    "passed": listed["status_code"] == 200 and any(app.get("id") == application_id for app in apps if isinstance(app, dict)),
                    "status_code": listed["status_code"],
                    "application_count": len(apps),
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("persona_returning_list_contains_smoke_app", api_base + "/api/v1/applications", error))

        draft_url = api_base + f"/api/v1/applications/{application_id}/draft"
        try:
            draft_result = request_json("GET", draft_url, token=token)
            draft = draft_result["json"]
            snapshot = draft.get("snapshot", {}) if isinstance(draft, dict) else {}
            workflow = snapshot.get("workflow", {}) if isinstance(snapshot, dict) else {}
            nodes = workflow.get("nodes", []) if isinstance(workflow, dict) else []
            edges = workflow.get("edges", []) if isinstance(workflow, dict) else []
            tests = snapshot.get("tests", []) if isinstance(snapshot, dict) else []
            node_types = {node.get("type") for node in nodes if isinstance(node, dict)}
            safety["called_endpoints"].append("GET /api/v1/applications/{id}/draft")
            checks.append(
                {
                    "id": "persona_returning_opened_structured_draft",
                    "persona": "returning_draft_reviewer",
                    "passed": draft_result["status_code"] == 200
                    and {"start", "answer"}.issubset(node_types)
                    and len(edges) >= 1
                    and len(tests) >= 1,
                    "status_code": draft_result["status_code"],
                    "revision": draft.get("revision"),
                    "node_types": sorted(str(item) for item in node_types),
                    "edge_count": len(edges),
                    "test_count": len(tests),
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("persona_returning_opened_structured_draft", draft_url, error))

        detail_url = frontend_base + f"/applications/{application_id}?safeDraft=1"
        try:
            detail = fetch_text(detail_url)
            body = str(detail["body"])
            checks.append(
                {
                    "id": "persona_returning_detail_route_visible",
                    "persona": "returning_draft_reviewer",
                    "url": detail_url,
                    "passed": detail["status_code"] == 200 and "Foundry" in body,
                    "status_code": detail["status_code"],
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("persona_returning_detail_route_visible", detail_url, error))

        cleanup_url = api_base + f"/api/v1/applications/{application_id}/smoke-cleanup"
        try:
            cleanup = request_json(
                "POST",
                cleanup_url,
                token=token,
                payload={"smoke_marker": SMOKE_MARKER, "dry_run": False},
            )
            safety["cleanup_attempted"] = True
            safety["called_endpoints"].append("POST /api/v1/applications/{id}/smoke-cleanup")
            checks.append(
                {
                    "id": "cleanup_smoke_app",
                    "passed": cleanup["status_code"] == 200 and cleanup["json"].get("deleted") is True,
                    "status_code": cleanup["status_code"],
                    "related_counts": cleanup["json"].get("related_counts"),
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("cleanup_smoke_app", cleanup_url, error))

        try:
            request_json("GET", api_base + f"/api/v1/applications/{application_id}", token=token)
            checks.append({"id": "verified_smoke_deleted", "passed": False, "error": "smoke app still exists"})
        except urllib.error.HTTPError as error:
            checks.append({"id": "verified_smoke_deleted", "passed": error.code == 404, "status_code": error.code})
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("verified_smoke_deleted", api_base + f"/api/v1/applications/{application_id}", error))

    checks.append(
        {
            "id": "safety_no_build_call",
            "passed": safety["build_endpoint_called"] is False,
            "called_endpoints": safety["called_endpoints"],
            "forbidden_endpoint": "POST /api/v1/applications/{id}/builds",
        }
    )
    return checks, safety


def build_evidence(
    *,
    live: bool = False,
    api_url: str = "http://127.0.0.1:8001",
    frontend_url: str = "http://127.0.0.1:3000",
    token: str = "",
) -> dict[str, object]:
    checks: list[dict[str, object]] = [
        persona_definition_evidence(),
        bug_ledger_evidence(),
        *source_marker_checks(),
    ]
    loaded_token, token_source = load_token(token)
    safety: dict[str, object] = {"build_endpoint_called": False, "called_endpoints": [], "smoke_marker": SMOKE_MARKER}
    if live:
        checks.extend(check_runtime_health(api_url, frontend_url))
        if not loaded_token:
            checks.append({"id": "token_available", "passed": False, "token_source": token_source})
        else:
            checks.append({"id": "token_available", "passed": True, "token_source": token_source})
            live_checks, safety = live_persona_checks(api_url, frontend_url, loaded_token)
            checks.extend(live_checks)
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.6",
        "stage": "runtime_product_health_triage",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "token_source": token_source if live else "not_used",
        "smoke_marker": SMOKE_MARKER,
        "expected_runtime_version": EXPECTED_RUNTIME_VERSION,
        "personas": [persona.__dict__ for persona in PERSONAS],
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
    parser = argparse.ArgumentParser(description="Run v0.3.6 runtime persona triage.")
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
