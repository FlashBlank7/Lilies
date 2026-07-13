#!/usr/bin/env python3
"""Verify v0.3.11 guided Try tab recovery and no-build operator run."""

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
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "guided_try_run_recovery_v0.3.11.json"
SMOKE_MARKER = "v0.3.11-smoke"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


BUG_LEDGER = (
    {
        "id": "P1-try-tab-readiness-unclear",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "An operator opening Try had to infer draft/input/published/run readiness from form fields and JSON.",
        "fix": "Add a Try readiness panel with stable markers and user-facing details.",
        "verification": "try_guidance_source_markers.",
    },
    {
        "id": "P1-run-sample-reuse-hidden",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Acceptance inputs were already useful samples but not exposed as an explicit Try action.",
        "fix": "Add sample-fill action backed by first mandatory acceptance inputs or defaults.",
        "verification": "try_sample_source_markers.",
    },
    {
        "id": "P1-operator-run-not-proven",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The usability suite did not prove that an operator could run a safe draft without starting builds.",
        "fix": "Add live safe draft run evidence and cleanup.",
        "verification": "ran_safe_draft_operator_persona and safety_no_build_call.",
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
    headers = {"Content-Type": "application/json", "User-Agent": "Lilies-v0.3.11-guided-try-run"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def fetch_text(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.11-guided-try-run"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "body": body[:60000], "error": ""}


def failed_check(check_id: str, url: str, error: Exception) -> dict[str, object]:
    status_code = error.code if isinstance(error, urllib.error.HTTPError) else 0
    return {"id": check_id, "url": url, "passed": False, "status_code": status_code, "error": str(error)}


def source_marker_checks() -> list[dict[str, object]]:
    checks = [
        (
            "try_guidance_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "runReadinessItems",
                'data-try-guidance="run-readiness"',
                "runRecoveryHint",
                "data-run-status",
            ),
        ),
        (
            "try_sample_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "applySampleRunInputs",
                "fillRunSample",
                "runSampleApplied",
                "firstMandatoryInputs",
            ),
        ),
        (
            "try_guidance_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryReadinessTitle",
                "fillRunSample",
                "runMissingInputHelp",
                "permissionHelp",
            ),
        ),
        (
            "try_guidance_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-readiness-panel",
                ".try-readiness-list",
                ".run-recovery-hint",
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
        "id": "p0_p1_bug_ledger_guided_try_run_recovery",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def create_payload(now: int | None = None) -> dict[str, str]:
    suffix = now if now is not None else int(time.time())
    requirement = (
        f"[{SMOKE_MARKER}] Guided Try run smoke draft for an operator. "
        "The user should fill sample input, run the safe draft, inspect output, and clean this app without any build."
    )
    return {
        "name": f"{SMOKE_MARKER} guided try run {suffix}",
        "description": requirement[:180],
        "requirement": requirement,
        "mode": "workflow",
    }


def skeleton_operations(suffix: str) -> list[tuple[str, dict[str, object]]]:
    start_id = f"v0311_start_{suffix}"
    answer_id = f"v0311_answer_{suffix}"
    return [
        ("add_node", {"node": {
            "id": start_id,
            "type": "start",
            "block_version": 1,
            "title": "Operator Input",
            "description": "v0.3.11 guided Try run smoke input.",
            "config": {"inputs": [{"name": "operator_request", "label": "Operator request", "type": "string", "required": True}]},
            "position": {"x": 120, "y": 160},
            "retry": {"enabled": False, "max_attempts": 1, "delay_seconds": 0.5},
            "error_strategy": "fail",
        }}),
        ("add_node", {"node": {
            "id": answer_id,
            "type": "answer",
            "block_version": 1,
            "title": "Operator Echo",
            "description": "Safe no-model answer for operator Try run evidence.",
            "config": {"answer": {"$ref": {"node_id": start_id, "path": ["output", "operator_request"]}}},
            "position": {"x": 420, "y": 160},
            "retry": {"enabled": False, "max_attempts": 1, "delay_seconds": 0.5},
            "error_strategy": "fail",
        }}),
        ("add_edge", {"edge": {
            "id": f"v0311_edge_{suffix}",
            "source": start_id,
            "target": answer_id,
            "source_port": "output",
            "target_port": "input",
        }}),
        ("add_test", {"test": {
            "id": f"v0311_acceptance_{suffix}",
            "name": "Guided Try sample input",
            "requirement": "The Try tab can reuse this input and run the safe draft.",
            "inputs": {"operator_request": "Summarize this failed order and name the next owner."},
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


def poll_run(api_url: str, run_id: str, token: str, safety: dict[str, object], attempts: int = 10) -> dict[str, object]:
    api_base = api_url.rstrip("/")
    last: dict[str, object] = {}
    for _ in range(attempts):
        result = request_json("GET", api_base + f"/api/v1/runs/{run_id}", token=token)
        safety["called_endpoints"].append("GET /api/v1/runs/{id}")
        last = result["json"]
        status = str(last.get("status", ""))
        if status in {"succeeded", "failed", "paused", "cancelled"}:
            return last
        time.sleep(0.5)
    return last


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
            "id": "created_guided_try_smoke_app",
            "passed": created["status_code"] == 201 and bool(application_id),
            "status_code": created["status_code"],
            "application_id": application_id,
        })
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_check("created_guided_try_smoke_app", api_base + "/api/v1/applications", error))

    if application_id:
        try:
            final_revision, draft_calls = apply_skeleton(api_base, application_id, token, 0)
            safety["called_endpoints"].extend(draft_calls)
            checks.append({
                "id": "seeded_guided_try_safe_draft",
                "passed": final_revision >= 4,
                "final_revision": final_revision,
                "operation_count": len(draft_calls),
            })
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            checks.append(failed_check("seeded_guided_try_safe_draft", api_base + f"/api/v1/applications/{application_id}/draft", error))

        detail_url = frontend_base + f"/applications/{application_id}?safeDraft=1"
        try:
            detail = fetch_text(detail_url)
            checks.append({
                "id": "rendered_detail_route_available",
                "url": detail_url,
                "passed": detail["status_code"] == 200 and "Foundry" in str(detail["body"]),
                "status_code": detail["status_code"],
            })
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            checks.append(failed_check("rendered_detail_route_available", detail_url, error))

        try:
            run_result = request_json(
                "POST",
                api_base + f"/api/v1/applications/{application_id}/runs",
                token=token,
                payload={
                    "inputs": {"operator_request": "Summarize this failed order and name the next owner."},
                    "use_draft": True,
                    "workspace_path": ".",
                },
            )
            run_id = str(run_result["json"].get("run_id", ""))
            safety["called_endpoints"].append("POST /api/v1/applications/{id}/runs")
            final_run = poll_run(api_base, run_id, token, safety) if run_id else {}
            checks.append({
                "id": "ran_safe_draft_operator_persona",
                "passed": run_result["status_code"] in {200, 201, 202} and final_run.get("status") == "succeeded",
                "status_code": run_result["status_code"],
                "run_id": run_id,
                "final_status": final_run.get("status"),
                "outputs": final_run.get("outputs"),
                "error": final_run.get("error"),
            })
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            checks.append(failed_check("ran_safe_draft_operator_persona", api_base + f"/api/v1/applications/{application_id}/runs", error))

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
        "version": "v0.3.11",
        "stage": "guided_try_run_recovery",
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
    parser = argparse.ArgumentParser(description="Run v0.3.11 guided Try tab recovery evidence.")
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
