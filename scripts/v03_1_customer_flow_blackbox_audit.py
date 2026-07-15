#!/usr/bin/env python3
"""Audit v0.3.1 customer requirement intake and black-box flow evidence."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.1" / "customer_flow_blackbox_audit_v0.3.1.json"


@dataclass(frozen=True)
class CustomerRequirementFixture:
    id: str
    persona: str
    need: str
    requirement: str
    expected_outcome: str
    acceptance_signal: str


@dataclass(frozen=True)
class MarkerCheck:
    id: str
    path: str
    markers: tuple[str, ...]


CUSTOMER_FIXTURES = (
    CustomerRequirementFixture(
        id="business_owner",
        persona="business_owner",
        need="evaluate whether Lilies turns cross-team customer requests into a trackable workflow",
        requirement=(
            "Receive a customer message, classify it as sales inquiry, delivery status, billing issue, "
            "or complaint escalation, then generate next handling step, suggested owner, missing information, "
            "and a customer-success-ready summary."
        ),
        expected_outcome="routing result, owner suggestion, missing information, and customer-readable summary",
        acceptance_signal="covers sales, delivery, billing, and complaint inputs",
    ),
    CustomerRequirementFixture(
        id="implementation_consultant",
        persona="implementation_consultant",
        need="turn client requirements into an acceptance-ready plan",
        requirement=(
            "Read a client requirement paragraph, extract business goal, roles, key data, risks, and open "
            "questions, then generate a workflow draft brief, acceptance case list, and pre-delivery checklist."
        ),
        expected_outcome="structured plan, open questions, and acceptance checklist",
        acceptance_signal="includes goal, roles, risks, and at least three acceptance cases",
    ),
    CustomerRequirementFixture(
        id="operator",
        persona="operator",
        need="run a stable exception handling process and understand failure ownership",
        requirement=(
            "Receive a failed order, support ticket, or system alert, judge severity and impact, request missing "
            "fields when needed, and generate handling steps, notification targets, trace, and postmortem note."
        ),
        expected_outcome="severity, handling steps, notification targets, and postmortem note",
        acceptance_signal="covers missing information, actionable, and escalation cases",
    ),
    CustomerRequirementFixture(
        id="technical_reviewer",
        persona="technical_reviewer",
        need="review generated automation before trusting it",
        requirement=(
            "Read a workflow description and check start inputs, core processing nodes, tool calls, error handling, "
            "acceptance cases, and release risks, then output publish decision, blockers, missing tests, and policy boundaries."
        ),
        expected_outcome="publish decision, blockers, testing gaps, and policy boundary notes",
        acceptance_signal="rejects workflows missing acceptance cases or tool evidence",
    ),
)


MARKER_CHECKS = (
    MarkerCheck(
        id="customer_fixture_copy",
        path="platform/frontend/lib/i18n.ts",
        markers=(
            "customerIntakeTitle",
            "customerIntakeHelp",
            "scenarioUseButton",
            "selectedScenarioLabel",
            "customerExamples",
            "business_owner",
            "implementation_consultant",
            "operator",
            "technical_reviewer",
        ),
    ),
    MarkerCheck(
        id="customer_fixture_ui",
        path="platform/frontend/app/page.tsx",
        markers=(
            "selectedExampleId",
            "selectedCustomerExample",
            "applyCustomerExample",
            "customer-intake-panel",
            "example-grid",
            "example-card",
            "data-customer-example",
        ),
    ),
    MarkerCheck(
        id="customer_fixture_styles",
        path="platform/frontend/app/globals.css",
        markers=(
            ".customer-intake-panel",
            ".customer-intake-head",
            ".example-grid",
            ".example-card",
            ".scenario-chip",
        ),
    ),
)


BUG_LEDGER = (
    {
        "id": "P0-frontdoor-customer-start-is-blank",
        "severity": "P0",
        "status": "fixed",
        "reproduction": "A first-time non-technical user opens the home page and does not know what requirement to write.",
        "fix": "Add customer requirement examples that fill the existing creation textarea.",
        "verification": "customer_fixture_ui and live_frontend_home checks verify the intake surface.",
    },
    {
        "id": "P1-persona-examples-not-executable",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Customer personas are explanatory cards only and cannot become testable inputs.",
        "fix": "Convert personas into requirement fixtures with expected outcomes and acceptance signals.",
        "verification": "fixture_quality check requires four concrete fixtures with outcome and acceptance signal.",
    },
    {
        "id": "P1-no-owned-customer-flow-harness",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Regression detection depends on ad hoc curl or manual observation.",
        "fix": "Add this versioned customer-flow audit with optional live service checks and JSON evidence.",
        "verification": "focused pytest and script execution must pass before stage archive.",
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


def fixture_evidence() -> dict[str, object]:
    ids = [fixture.id for fixture in CUSTOMER_FIXTURES]
    requirement_lengths = {fixture.id: len(fixture.requirement) for fixture in CUSTOMER_FIXTURES}
    weak_fixtures = [
        fixture.id
        for fixture in CUSTOMER_FIXTURES
        if len(fixture.requirement) < 120 or not fixture.expected_outcome or not fixture.acceptance_signal
    ]
    required = {"business_owner", "implementation_consultant", "operator", "technical_reviewer"}
    return {
        "id": "fixture_quality",
        "passed": not weak_fixtures and required.issubset(set(ids)) and len(set(ids)) == len(ids),
        "persona_count": len(set(ids)),
        "required_personas_present": sorted(required.intersection(ids)),
        "missing_personas": sorted(required.difference(ids)),
        "weak_fixtures": weak_fixtures,
        "requirement_lengths": requirement_lengths,
    }


def bug_ledger_evidence() -> dict[str, object]:
    blocking = [
        item
        for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {
        "id": "p0_p1_bug_ledger",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def fetch_url(url: str, timeout_seconds: float = 5.0) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Lilies-v0.3.1-audit"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {"ok": True, "status_code": response.getcode(), "body": body[:50000], "error": ""}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"ok": False, "status_code": 0, "body": "", "error": str(error)}


def live_evidence(frontend_url: str, backend_url: str) -> list[dict[str, object]]:
    frontend = fetch_url(frontend_url.rstrip("/") + "/")
    frontend_body = str(frontend["body"])
    backend = fetch_url(backend_url.rstrip("/") + "/health")
    backend_body = str(backend["body"])
    return [
        {
            "id": "live_frontend_home",
            "url": frontend_url.rstrip("/") + "/",
            "passed": bool(frontend["ok"]) and "customer-intake-panel" in frontend_body and "data-customer-example" in frontend_body,
            "status_code": frontend["status_code"],
            "required_markers": ["customer-intake-panel", "data-customer-example"],
            "missing_markers": [
                marker for marker in ["customer-intake-panel", "data-customer-example"] if marker not in frontend_body
            ],
            "error": frontend["error"],
        },
        {
            "id": "live_backend_health",
            "url": backend_url.rstrip("/") + "/health",
            "passed": bool(backend["ok"]) and '"status":"ok"' in backend_body.replace(" ", ""),
            "status_code": backend["status_code"],
            "required_markers": ['"status":"ok"'],
            "missing_markers": [] if '"status":"ok"' in backend_body.replace(" ", "") else ['"status":"ok"'],
            "error": backend["error"],
        },
    ]


def build_evidence(*, live: bool = False, frontend_url: str = "http://127.0.0.1:3000", backend_url: str = "http://127.0.0.1:8001") -> dict[str, object]:
    source_checks = marker_evidence()
    fixture_check = fixture_evidence()
    bug_check = bug_ledger_evidence()
    live_checks = live_evidence(frontend_url, backend_url) if live else []
    checks = [fixture_check, bug_check, *source_checks, *live_checks]
    failed = [check for check in checks if not check["passed"]]
    return {
        "version": "v0.3.1",
        "stage": "customer_requirement_intake_and_blackbox_flow",
        "status": "passed" if not failed else "failed",
        "customer_requirement_fixtures": [asdict(fixture) for fixture in CUSTOMER_FIXTURES],
        "bug_ledger": list(BUG_LEDGER),
        "live_checks_enabled": live,
        "checks": checks,
        "summary": {
            "fixture_count": len(CUSTOMER_FIXTURES),
            "bug_count": len(BUG_LEDGER),
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_check["blocking_bug_count"],
        },
    }


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit v0.3.1 customer intake and black-box flow.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true", help="Check local frontend and backend HTTP endpoints.")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()

    evidence = build_evidence(live=args.live, frontend_url=args.frontend_url, backend_url=args.backend_url)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
