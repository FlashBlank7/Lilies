from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "usability_customer_journey_audit_v0.3.0.json"


@dataclass(frozen=True)
class Persona:
    id: str
    behavior: str
    success_signal: str


@dataclass(frozen=True)
class MarkerCheck:
    id: str
    path: str
    markers: tuple[str, ...]


PERSONAS = (
    Persona(
        id="business_owner",
        behavior="opens the front door to understand what Lilies can deliver",
        success_signal="sees customer scenarios, product path, and create action before reading docs",
    ),
    Persona(
        id="implementation_consultant",
        behavior="opens an existing workflow to inspect draft structure and acceptance gates",
        success_signal="sees draft readiness, canvas explanation, and acceptance/test direction",
    ),
    Persona(
        id="operator",
        behavior="tries a draft or published version and needs failure evidence",
        success_signal="sees run guidance, permission state, trace, and monitor entry",
    ),
    Persona(
        id="technical_reviewer",
        behavior="checks whether generated automation is controllable and test-backed",
        success_signal="sees bug triage, acceptance evidence, policy, monitor, and audit surfaces",
    ),
)


MARKER_CHECKS = (
    MarkerCheck(
        id="home_customer_orientation_copy",
        path="platform/frontend/lib/i18n.ts",
        markers=(
            "customerScenariosTitle",
            "customerScenarios",
            "productStepsTitle",
            "productSteps",
            "emptyAppsNextAction",
        ),
    ),
    MarkerCheck(
        id="home_customer_orientation_ui",
        path="platform/frontend/app/page.tsx",
        markers=(
            "customer-section",
            "scenario-grid",
            "product-path",
            "emptyAppsNextAction",
        ),
    ),
    MarkerCheck(
        id="draft_canvas_comprehension_copy",
        path="platform/frontend/lib/i18n.ts",
        markers=(
            "draftReadinessTitle",
            "canvasGuideTitle",
            "canvasGuideCopy",
            "bugTriageTitle",
            "bugTriageItems",
        ),
    ),
    MarkerCheck(
        id="draft_canvas_comprehension_ui",
        path="platform/frontend/app/applications/[id]/page.tsx",
        markers=(
            "readinessCards",
            "canvas-guidance",
            "draft-readiness",
            "canvas-guide",
            "bug-triage-panel",
        ),
    ),
    MarkerCheck(
        id="responsive_usability_styles",
        path="platform/frontend/app/globals.css",
        markers=(
            ".customer-section",
            ".scenario-grid",
            ".draft-readiness",
            ".canvas-guide",
            ".bug-triage-panel",
            "@media(max-width:720px)",
        ),
    ),
)


JOURNEYS = (
    {
        "id": "frontdoor_to_create",
        "persona": "business_owner",
        "steps": ["read scenario", "read product path", "describe outcome", "start build"],
        "required_surfaces": ["customer-section", "create-card", "apps-section"],
    },
    {
        "id": "open_draft_to_understand_canvas",
        "persona": "implementation_consultant",
        "steps": ["open application", "read draft state", "inspect canvas guide", "select brick"],
        "required_surfaces": ["draft-readiness", "canvas-guide", "block-panel"],
    },
    {
        "id": "try_and_debug_run",
        "persona": "operator",
        "steps": ["open Try tab", "review JSON preview", "run draft", "inspect trace or monitor"],
        "required_surfaces": ["runTab", "runInputPreview", "traceTitle", "monitorTab"],
    },
    {
        "id": "review_for_publish_safety",
        "persona": "technical_reviewer",
        "steps": ["run acceptance", "review bug triage", "check monitor", "publish only after verified"],
        "required_surfaces": ["testTab", "bug-triage-panel", "monitorTab", "publishVersion"],
    },
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def missing_markers(text: str, markers: Iterable[str]) -> list[str]:
    return [marker for marker in markers if marker not in text]


def build_evidence() -> dict[str, object]:
    checks = []
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

    failed_checks = [check for check in checks if not check["passed"]]
    journey_surface_index = sorted({surface for journey in JOURNEYS for surface in journey["required_surfaces"]})

    return {
        "version": "v0.3.0",
        "stage": "product_usability_stabilization",
        "status": "passed" if not failed_checks else "failed",
        "customer_personas": [asdict(persona) for persona in PERSONAS],
        "journeys": list(JOURNEYS),
        "journey_surface_index": journey_surface_index,
        "checks": checks,
        "summary": {
            "persona_count": len(PERSONAS),
            "journey_count": len(JOURNEYS),
            "check_count": len(checks),
            "failed_check_count": len(failed_checks),
            "frontdoor_journey_present": any(journey["id"] == "frontdoor_to_create" for journey in JOURNEYS),
            "draft_canvas_journey_present": any(journey["id"] == "open_draft_to_understand_canvas" for journey in JOURNEYS),
        },
    }


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit v0.3.0 product usability customer journeys.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    evidence = build_evidence()
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
