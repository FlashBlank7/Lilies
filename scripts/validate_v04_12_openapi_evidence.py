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
    assert (
        sum(
            aggregate["contract_case_denominator"][key]
            for key in ("passed", "failed", "unsupported", "blocked_by_environment", "not_run")
        )
        == cases
    )
    for item in hosts:
        delivery = item["delivery"]
        generated_cases = delivery["generated_contract_cases"]
        assert generated_cases["positive"] == delivery["generated_operations"]
        assert generated_cases["negative"] == delivery["generated_operations"]
        assert delivery["upstream_revalidation_ms"] >= 0
        assert item["forbidden_assistance_scan"]["status"] == "pass"
        assert item["forbidden_assistance_scan"]["forbidden_term_hits"] == []

    history = load(EVIDENCE / "inventree_live_contract_attempt_1_failure.json")
    live = load(EVIDENCE / "inventree_live_contract.json")
    selected_methods = set(live["selected_operations"].values())
    assert "GET" in selected_methods
    assert selected_methods - {"GET"}
    assert live["contract_run"]["status"] == "passed"
    assert live["contract_run"]["failed"] == 0
    assert all(item["status"] == "passed" for item in live["contract_run"]["results"])
    write_operations = {
        operation_id
        for operation_id, method in live["selected_operations"].items()
        if method != "GET"
    }
    write_results = [
        item
        for item in live["contract_run"]["results"]
        if item["case"]["kind"] == "positive" and item["case"]["operation_id"] in write_operations
    ]
    assert write_results
    assert all(len(item["response_evidence"]["sha256"]) == 64 for item in write_results)
    assert any(item["response_evidence"].get("identity") for item in write_results)
    assert all(len(item["executed_input_evidence"]["sha256"]) == 64 for item in write_results)
    side_effect = load(EVIDENCE / "inventree_live_side_effect.json")
    assert side_effect["status"] == "passed"
    assert side_effect["operation_id"] in write_operations
    assert side_effect["expected"] == {
        key: side_effect["actual"][key] for key in side_effect["expected"]
    }
    assert side_effect["actual"]["pk"] == write_results[0]["response_evidence"]["identity"]["pk"]
    executed_body = write_results[0]["executed_input_evidence"]["body_preview"]["body"]
    assert side_effect["expected"]["company"] == executed_body["company"]
    assert side_effect["expected"]["name"] == executed_body["name"]
    assert side_effect["credential_value_recorded"] is False
    assert live["credential_value_recorded"] is False
    observed_cases: dict[str, str] = {}
    for artifact in (history, live):
        for result in artifact["contract_run"]["results"]:
            case_id = result["case"]["id"]
            previous = observed_cases.get(case_id)
            assert previous is None or previous == result["status"]
            observed_cases[case_id] = result["status"]
    assert len(observed_cases) == 6
    assert sum(status == "passed" for status in observed_cases.values()) == 5
    assert sum(status == "failed" for status in observed_cases.values()) == 1
    assert aggregate["contract_case_denominator"]["passed"] == 5
    assert aggregate["contract_case_denominator"]["failed"] == 1
    assert aggregate["contract_case_denominator"]["not_run"] == cases - len(observed_cases)
    assert aggregate["execution_attempt_history"]["result_records"] == 8
    assert aggregate["human_and_model_cost"]["human_rescue_count"] == 1
    assert aggregate["host_results"]["inventree"]["live_contract_status"] == "passed"
    assert aggregate["host_results"]["inventree"]["historical_failed_cases"] == 1
    history_failures = [
        item for item in history["contract_run"]["results"] if item["status"] == "failed"
    ]
    assert history["contract_run"]["status"] == "failed"
    assert history["contract_run"]["failed"] == len(history_failures) == 1
    assert history["contract_run"]["passed"] == 3
    retained = aggregate["host_results"]["inventree"]["retained_failure"]
    assert retained["artifact"] == "inventree_live_contract_attempt_1_failure.json"
    assert retained["case_id"] == history_failures[0]["case"]["id"]
    assert retained["operation_id"] == history_failures[0]["case"]["operation_id"]
    assert retained["actual"] == history_failures[0]["actual"]
    envelope = load(EVIDENCE / "response_envelope_contract.json")
    assert {item["expected_result"] for item in envelope["cases"]} == {"passed", "failed"}
    for item in envelope["cases"]:
        canonical = json.dumps(
            item["raw_body"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert hashlib.sha256(canonical).hexdigest() == item["canonical_sha256"]
        assert len(canonical) == item["canonical_bytes"]
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
