#!/usr/bin/env python3
"""Validate v0.4.12 evidence denominators and browser artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "v0.4.12"


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"evidence root must be an object: {path}")
    return value


def main() -> int:
    hosts = [
        load(EVIDENCE / f"{name}_openapi_generalization.json")
        for name in ("paperless", "inventree", "chatwoot")
    ]
    aggregate = load(EVIDENCE / "openapi_generalization_aggregate.json")
    discovered = sum(item["delivery"]["discovered_operations"] for item in hosts)
    generated = sum(item["delivery"]["generated_operations"] for item in hosts)
    unsupported = sum(item["delivery"]["unsupported_operations"] for item in hosts)
    cases = sum(item["delivery"]["generated_contract_cases"]["total"] for item in hosts)
    assert (discovered, generated, unsupported, cases) == (955, 530, 425, 1060)
    assert aggregate["generation_denominator"]["generated_contract_cases"] == cases
    assert aggregate["contract_case_denominator"]["total_generated"] == cases
    assert sum(
        aggregate["contract_case_denominator"][key]
        for key in ("passed", "failed", "unsupported", "blocked_by_environment", "not_run")
    ) == cases
    for item in hosts:
        delivery = item["delivery"]
        generated_cases = delivery["generated_contract_cases"]
        assert generated_cases["positive"] == delivery["generated_operations"]
        assert generated_cases["negative"] == delivery["generated_operations"]
        assert delivery["upstream_revalidation_ms"] >= 0
        assert item["forbidden_assistance_scan"]["status"] == "pass"
        assert item["forbidden_assistance_scan"]["forbidden_term_hits"] == []

    live = load(EVIDENCE / "inventree_live_contract.json")
    assert live["contract_run"]["status"] == "failed"
    assert live["contract_run"]["passed"] == 3
    assert live["contract_run"]["failed"] == 1
    assert live["credential_value_recorded"] is False
    for name in ("paperless", "chatwoot"):
        blocked = load(EVIDENCE / f"{name}_live_contract_environment.json")
        assert blocked["status"] == "blocked_by_environment"
        assert blocked["eligible_host"] is False
        assert blocked["fabricated_pass"] is False

    browser_root = EVIDENCE / "browser"
    browser = load(browser_root / "browser-evidence.json")
    assert browser["pass_disclosure"]["status"] == "passed"
    assert browser["pass_disclosure"]["registerEnabled"] is True
    assert browser["failure_disclosure"]["status"] == "failed"
    assert browser["failure_disclosure"]["registerEnabled"] is False
    assert browser["console_errors"] == []
    assert browser["failed_requests"] == []
    assert browser["layout"]["document_overflow"] is False
    assert browser["layout"]["surface_overflow"] is False
    for screenshot in browser["screenshots"]:
        path = browser_root / screenshot["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == screenshot["sha256"]
    print("v0.4.12 OpenAPI evidence validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
