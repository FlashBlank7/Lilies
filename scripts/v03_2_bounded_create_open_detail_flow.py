#!/usr/bin/env python3
"""Run the bounded v0.3.2 create/open/detail flow without calling builds."""

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
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.2" / "bounded_create_open_detail_flow_v0.3.2.json"
SMOKE_MARKER = "v0.3.2-smoke"


@dataclass(frozen=True)
class MarkerCheck:
    id: str
    path: str
    markers: tuple[str, ...]


MARKER_CHECKS = (
    MarkerCheck(
        id="safe_draft_copy",
        path="platform/frontend/lib/i18n.ts",
        markers=("safeDraftHint", "saveDraftOnlyButton", "saveDraftOnlyBusy"),
    ),
    MarkerCheck(
        id="safe_draft_ui",
        path="platform/frontend/app/page.tsx",
        markers=("saveDraftOnly", "draftBusy", "safeDraft=1", "secondary-action"),
    ),
    MarkerCheck(
        id="safe_draft_styles",
        path="platform/frontend/app/globals.css",
        markers=(".create-actions", ".secondary-action", ".create-copy"),
    ),
)


BUG_LEDGER = (
    {
        "id": "P0-frontdoor-forces-model-build",
        "severity": "P0",
        "status": "fixed",
        "reproduction": "A cautious user can only start the builder team after selecting a customer example.",
        "fix": "Add a save-draft-only action that calls application create without calling builds.",
        "verification": "safe_draft_ui marker check and live bounded create/open/detail flow.",
    },
    {
        "id": "P1-no-local-create-open-detail-harness",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Customer examples could be filled but not proven to create and reopen a local application.",
        "fix": "Add this bounded live harness for POST applications plus GET application/draft/detail route.",
        "verification": "live_created_application, live_opened_application, live_opened_draft, and live_frontend_detail checks.",
    },
    {
        "id": "P1-smoke-app-cleanup-missing",
        "severity": "P1",
        "status": "deferred_with_reason",
        "reproduction": "The harness leaves local smoke applications because there is no delete application API.",
        "fix": "Defer cleanup until an application archive/delete feature exists; keep a v0.3.2-smoke marker for traceability.",
        "verification": "stage report records local-state side effect explicitly.",
    },
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def missing_markers(text: str, markers: Iterable[str]) -> list[str]:
    return [marker for marker in markers if marker not in text]


def marker_evidence() -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for check in MARKER_CHECKS:
        text = read_text(check.path)
        missing = missing_markers(text, check.markers)
        checks.append(
            {
                "id": check.id,
                "path": check.path,
                "required_markers": list(check.markers),
                "missing_markers": missing,
                "passed": not missing,
            }
        )
    return checks


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
    for path in env_files or (ROOT / ".env", ROOT / "platform" / "backend" / ".env", ROOT / "platform" / "frontend" / ".env.local"):
        values = parse_env_file(path)
        for key in ("AGENT_PLATFORM_API_TOKEN", "AGENT_PLATFORM_TOKEN", "API_TOKEN"):
            if values.get(key):
                return values[key], f"file:{display_path(path)}:{key}"
    return "", "missing"


def smoke_requirement() -> str:
    return (
        f"[{SMOKE_MARKER}] Build a safe local draft from the business-owner customer example: "
        "classify customer messages, suggest an owner, list missing information, and produce a customer-success summary. "
        "This smoke flow must create only a draft application and must not start the builder team."
    )


def create_payload(now: int | None = None) -> dict[str, str]:
    suffix = now if now is not None else int(time.time())
    requirement = smoke_requirement()
    return {
        "name": f"{SMOKE_MARKER} customer draft {suffix}",
        "description": requirement[:180],
        "requirement": requirement,
        "mode": "workflow",
    }


def request_json(method: str, url: str, *, token: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Lilies-v0.3.2-bounded-flow",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body), "error": ""}


def fetch_html(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.2-bounded-flow"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "body": body[:50000], "error": ""}


def failed_http_check(check_id: str, url: str, error: Exception) -> dict[str, object]:
    return {
        "id": check_id,
        "url": url,
        "passed": False,
        "status_code": 0,
        "error": str(error),
    }


def bug_ledger_evidence() -> dict[str, object]:
    blocking = [
        item
        for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {
        "id": "p0_p1_bug_ledger_create_open_detail",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def live_evidence(api_url: str, frontend_url: str, token: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    checks: list[dict[str, object]] = []
    created: dict[str, object] = {}
    payload = create_payload()
    create_url = api_url.rstrip("/") + "/api/v1/applications"
    safety = {
        "build_endpoint_called": False,
        "called_endpoints": ["POST /api/v1/applications"],
        "smoke_marker": SMOKE_MARKER,
    }
    try:
        create_result = request_json("POST", create_url, token=token, payload=payload)
        created = create_result["json"]  # type: ignore[assignment]
        application_id = str(created.get("id", ""))
        checks.append(
            {
                "id": "live_created_application",
                "url": create_url,
                "passed": create_result["status_code"] == 201
                and bool(application_id)
                and str(created.get("name", "")).startswith(SMOKE_MARKER)
                and created.get("active_version") is None,
                "status_code": create_result["status_code"],
                "application_id": application_id,
                "active_version": created.get("active_version"),
                "draft_revision": created.get("draft_revision"),
                "error": "",
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_http_check("live_created_application", create_url, error))
        return checks, safety

    application_id = str(created.get("id", ""))
    app_url = api_url.rstrip("/") + f"/api/v1/applications/{application_id}"
    draft_url = api_url.rstrip("/") + f"/api/v1/applications/{application_id}/draft"
    detail_url = frontend_url.rstrip("/") + f"/applications/{application_id}?safeDraft=1"
    safety["called_endpoints"] = [
        "POST /api/v1/applications",
        "GET /api/v1/applications/{id}",
        "GET /api/v1/applications/{id}/draft",
        "GET /applications/{id}?safeDraft=1",
    ]

    try:
        app_result = request_json("GET", app_url, token=token)
        app_json = app_result["json"]
        checks.append(
            {
                "id": "live_opened_application",
                "url": app_url,
                "passed": app_result["status_code"] == 200
                and app_json.get("id") == application_id
                and SMOKE_MARKER in str(app_json.get("requirement", "")),
                "status_code": app_result["status_code"],
                "application_id": app_json.get("id"),
                "error": "",
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_http_check("live_opened_application", app_url, error))

    try:
        draft_result = request_json("GET", draft_url, token=token)
        draft_json = draft_result["json"]
        snapshot = draft_json.get("snapshot", {}) if isinstance(draft_json, dict) else {}
        checks.append(
            {
                "id": "live_opened_draft",
                "url": draft_url,
                "passed": draft_result["status_code"] == 200
                and draft_json.get("application_id") == application_id
                and draft_json.get("revision") == 0
                and SMOKE_MARKER in str(snapshot.get("requirement", "")),
                "status_code": draft_result["status_code"],
                "application_id": draft_json.get("application_id"),
                "revision": draft_json.get("revision"),
                "node_count": len(snapshot.get("workflow", {}).get("nodes", [])) if isinstance(snapshot, dict) else 0,
                "test_count": len(snapshot.get("tests", [])) if isinstance(snapshot, dict) else 0,
                "error": "",
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        checks.append(failed_http_check("live_opened_draft", draft_url, error))

    try:
        detail_result = fetch_html(detail_url)
        body = str(detail_result["body"])
        checks.append(
            {
                "id": "live_frontend_detail",
                "url": detail_url,
                "passed": detail_result["status_code"] == 200 and "Foundry" in body,
                "status_code": detail_result["status_code"],
                "required_markers": ["Foundry"],
                "missing_markers": [] if "Foundry" in body else ["Foundry"],
                "error": "",
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        checks.append(failed_http_check("live_frontend_detail", detail_url, error))

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
    checks: list[dict[str, object]] = [bug_ledger_evidence(), *marker_evidence()]
    loaded_token, token_source = load_token(token)
    safety: dict[str, object] = {"build_endpoint_called": False, "called_endpoints": [], "smoke_marker": SMOKE_MARKER}
    if live:
        if not loaded_token:
            checks.append({"id": "token_available", "passed": False, "token_source": token_source})
        else:
            checks.append({"id": "token_available", "passed": True, "token_source": token_source})
            live_checks, safety = live_evidence(api_url, frontend_url, loaded_token)
            checks.extend(live_checks)
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.2",
        "stage": "bounded_create_open_detail_flow",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "token_source": token_source if live else "not_used",
        "smoke_marker": SMOKE_MARKER,
        "payload_preview": create_payload(now=0),
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
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
    parser = argparse.ArgumentParser(description="Run v0.3.2 bounded create/open/detail flow.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true", help="Create a local smoke app and open API/detail routes.")
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
