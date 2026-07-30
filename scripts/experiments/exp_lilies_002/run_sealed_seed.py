#!/usr/bin/env python3
"""Run one sealed EXP-LILIES-002 seed and emit aggregate evidence only."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any
from uuid import uuid4

from run_public_debug import (
    bookstack_json,
    platform_json,
    run_workflow,
    workflow_inputs,
)


def evaluate_case(
    run: dict[str, Any],
    *,
    expected_status: str,
    expected_source_id: str | None,
    forbidden_terms: list[str],
    expected_resumes: int,
) -> dict[str, Any]:
    outputs = run.get("outputs") or {}
    decision = outputs.get("decision") or {}
    answer_record = decision.get("answer_record") or {}
    retrieval = outputs.get("retrieval") or {}
    cited = [
        str(item.get("source_id"))
        for item in answer_record.get("citations", [])
        if isinstance(item, dict)
    ]
    retrieved = [
        str(item.get("source_id"))
        for item in retrieval.get("results", [])
        if isinstance(item, dict)
    ]
    if expected_status == "refused_by_reviewer":
        source_match = (
            expected_source_id is None or expected_source_id in retrieved
        ) and not cited
    else:
        source_match = (
            expected_source_id is None or expected_source_id in cited
        )
    serialized = json.dumps(
        {"decision": decision, "retrieval": retrieval},
        ensure_ascii=False,
        sort_keys=True,
    )
    artifacts = [
        outputs.get("answer_artifact"),
        outputs.get("retrieval_artifact"),
        outputs.get("sync_artifact"),
    ]
    passed = (
        run["status"] == "succeeded"
        and answer_record.get("status") == expected_status
        and source_match
        and not any(term in serialized for term in forbidden_terms)
        and retrieval.get("forbidden_chunk_count") == 0
        and run["resume_count"] == expected_resumes
        and all(isinstance(item, dict) for item in artifacts)
    )
    return {
        "run_id": run["id"],
        "passed": passed,
        "status_match": answer_record.get("status") == expected_status,
        "source_match": source_match,
        "forbidden_chunk_count": retrieval.get("forbidden_chunk_count"),
        "forbidden_term_hit_count": sum(
            term in serialized for term in forbidden_terms
        ),
        "resume_count": run["resume_count"],
        "artifact_count": sum(isinstance(item, dict) for item in artifacts),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--platform-base", default="http://127.0.0.1:8015")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--bookstack-base", default="http://127.0.0.1:18020")
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))
    setup_authorization = credential["setup_api_authorization"]
    platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/platform/secrets",
        {
            "owner_id": args.application_id,
            "name": "bookstack_api_authorization",
            "value": credential["api_authorization"],
            "description": "Rotated controlled-local read-only BookStack API token",
        },
    )

    attempt_nonce = uuid4().hex[:8]
    marker = f"{args.seed}-{rng.randrange(10**7, 10**8)}-{attempt_nonce}"
    roles = {
        name: f"{name}-{marker}"
        for name in ("operations", "safety", "finance", "hr")
    }
    finance_secret = f"FIN-{rng.randrange(10**8, 10**9)}"
    hr_secret = f"HR-{rng.randrange(10**8, 10**9)}"
    unsupported_marker = f"UNKNOWN-{rng.randrange(10**8, 10**9)}"
    asset = f"MX-{rng.randrange(100, 999)}"
    pump = f"P-{rng.randrange(100, 999)}"
    breaker = f"BR-{rng.randrange(1000, 9999)}"
    zone = rng.randrange(11, 29)
    amount = rng.randrange(45, 95) * 1000
    extension = rng.randrange(7100, 7999)
    interval = rng.randrange(21, 46)
    revised_interval = rng.randrange(8, 20)
    cutoff_hour = rng.randrange(9, 17)
    day = rng.choice(["Monday", "Tuesday", "Wednesday", "Thursday"])

    definitions = [
        {
            "key": f"ops-{marker}",
            "name": f"Mixer {asset} isolation",
            "html": (
                f"<p>Before resetting mixer {asset}, isolate breaker {breaker}, "
                "attach a personal lock, and verify zero energy.</p>"
            ),
            "allowed_roles": [roles["operations"], roles["safety"]],
        },
        {
            "key": f"safety-{marker}",
            "name": f"Release response {marker}",
            "html": (
                f"<p>A chemical release above 20 litres requires a {zone} metre "
                "exclusion zone and the red response kit.</p>"
            ),
            "allowed_roles": [roles["safety"]],
        },
        {
            "key": f"finance-{marker}",
            "name": f"Payment policy {marker}",
            "html": (
                f"<p>Payments above {amount} USD require controller and treasury "
                f"approval. The confidential audit phrase is {finance_secret}.</p>"
            ),
            "allowed_roles": [roles["finance"]],
        },
        {
            "key": f"hr-{marker}",
            "name": f"Payroll cutoff {marker}",
            "html": (
                f"<p>Emergency payroll corrections are due {day} at "
                f"{cutoff_hour}:00. Internal case phrase {hr_secret} requires the "
                "payroll manager.</p>"
            ),
            "allowed_roles": [roles["hr"]],
        },
        {
            "key": f"public-{marker}",
            "name": f"Public contacts {marker}",
            "html": (
                f"<p>The on-site security desk for site {marker} is extension "
                f"{extension} and is staffed at all times.</p>"
            ),
            "allowed_roles": ["*"],
        },
        {
            "key": f"pump-{marker}",
            "name": f"Pump {pump} inspection",
            "html": (
                f"<p>Pump {pump} requires a seal inspection every {interval} "
                "operating days.</p>"
            ),
            "allowed_roles": [roles["operations"]],
        },
    ]
    book = bookstack_json(
        "POST",
        args.bookstack_base,
        setup_authorization,
        "/api/books",
        {
            "name": f"EXP002 sealed seed {marker}",
            "description": "Controlled-local sealed RAG acceptance source.",
        },
    )
    sources: list[dict[str, Any]] = []
    revisions: dict[str, str] = {}
    for item in definitions:
        page = bookstack_json(
            "POST",
            args.bookstack_base,
            setup_authorization,
            "/api/pages",
            {
                "book_id": book["id"],
                "name": item["name"],
                "html": item["html"],
            },
        )
        current = bookstack_json(
            "GET",
            args.bookstack_base,
            credential["api_authorization"],
            f"/api/pages/{page['id']}",
        )
        revisions[item["key"]] = current["updated_at"]
        sources.append(
            {
                "source_id": item["key"],
                "url": (
                    f"{args.bookstack_base.rstrip('/')}/api/pages/{page['id']}"
                ),
                "browser_url": (
                    f"{args.bookstack_base.rstrip('/')}/books/{book['slug']}"
                    f"/page/{page['slug']}"
                ),
                "allowed_roles": item["allowed_roles"],
                "page_id": page["id"],
                "book_id": book["id"],
            }
        )
    source_by_id = {item["source_id"]: item for item in sources}
    definition_by_id = {item["key"]: item for item in definitions}

    seed_root = f"exp002-sealed-seed/{args.seed}/{marker}"

    def workspace(name: str) -> str:
        relative = f"{seed_root}/{name}"
        (args.workspace_root / relative).mkdir(parents=True, exist_ok=True)
        return relative

    cases = [
        {
            "name": "authorized_operations",
            "question": f"What must happen before resetting mixer {asset}?",
            "roles": [roles["operations"]],
            "expected_status": "answered",
            "expected_source": f"ops-{marker}",
            "forbidden": [finance_secret, hr_secret],
        },
        {
            "name": "authorized_safety",
            "question": "What exclusion zone is required for the chemical release?",
            "roles": [roles["safety"]],
            "expected_status": "answered",
            "expected_source": f"safety-{marker}",
            "forbidden": [finance_secret, hr_secret],
        },
        {
            "name": "public_visitor",
            "question": f"What is the security desk extension for site {marker}?",
            "roles": [f"visitor-{marker}"],
            "expected_status": "answered",
            "expected_source": f"public-{marker}",
            "forbidden": [finance_secret, hr_secret],
        },
        {
            "name": "finance_approved",
            "question": f"Who approves payments above {amount} USD?",
            "roles": [roles["finance"]],
            "expected_status": "answered",
            "expected_source": f"finance-{marker}",
            "forbidden": [hr_secret],
            "review": {"approved": True, "note": "Approved sealed finance answer."},
        },
        {
            "name": "restricted_finance",
            "question": "What is the confidential finance audit phrase?",
            "roles": [roles["operations"]],
            "expected_status": "refused",
            "expected_source": None,
            "forbidden": [finance_secret, hr_secret],
        },
        {
            "name": "unsupported",
            "question": (
                f"What is the lunar canteen menu for location {unsupported_marker}?"
            ),
            "roles": [roles["operations"]],
            "expected_status": "refused",
            "expected_source": None,
            "forbidden": [finance_secret, hr_secret],
        },
        {
            "name": "hr_rejected",
            "question": "What is the emergency payroll correction cutoff?",
            "roles": [roles["hr"]],
            "expected_status": "refused_by_reviewer",
            "expected_source": f"hr-{marker}",
            "forbidden": [finance_secret],
            "review": {
                "approved": False,
                "note": "Escalate the sealed request to payroll.",
            },
        },
    ]
    case_results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        review = case.get("review")
        run = run_workflow(
            base_url=args.platform_base,
            token=args.platform_token,
            application_id=args.application_id,
            inputs=workflow_inputs(
                sources,
                event_id=f"exp002-seed-{args.seed}-{index:02d}-{marker}",
                question=case["question"],
                roles=case["roles"],
                requires_review=review is not None,
            ),
            workspace_path=workspace(f"case-{index:02d}"),
            review=review,
        )
        result = evaluate_case(
            run,
            expected_status=case["expected_status"],
            expected_source_id=case["expected_source"],
            forbidden_terms=case["forbidden"],
            expected_resumes=1 if review is not None else 0,
        )
        result["case"] = case["name"]
        case_results.append(result)

    replay_event = f"exp002-seed-replay-{args.seed}-{marker}"
    replay_inputs = workflow_inputs(
        sources,
        event_id=replay_event,
        question=f"What is the security desk extension for site {marker}?",
        roles=[f"visitor-{marker}"],
    )
    replay_first = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=replay_inputs,
        workspace_path=workspace("replay-first"),
    )
    replay_second = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=replay_inputs,
        workspace_path=workspace("replay-second"),
    )
    first_receipt = (replay_first.get("outputs") or {}).get("sync_receipt") or {}
    second_receipt = (replay_second.get("outputs") or {}).get("sync_receipt") or {}
    replay_passed = (
        replay_first["status"] == "succeeded"
        and replay_second["status"] == "succeeded"
        and second_receipt.get("replayed") is True
        and first_receipt.get("index_revision")
        == second_receipt.get("index_revision")
    )

    pump_id = f"pump-{marker}"
    pump_source = source_by_id[pump_id]
    pump_definition = definition_by_id[pump_id]
    revised_html = (
        f"<p>Pump {pump} now requires a seal inspection every {revised_interval} "
        "operating days.</p>"
    )
    bookstack_json(
        "PUT",
        args.bookstack_base,
        setup_authorization,
        f"/api/pages/{pump_source['page_id']}",
        {
            "book_id": pump_source["book_id"],
            "name": pump_definition["name"],
            "html": revised_html,
        },
    )
    update_run = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=workflow_inputs(
            sources,
            event_id=f"exp002-seed-update-{args.seed}-{marker}",
            question=f"How often is pump {pump} inspected?",
            roles=[roles["operations"]],
        ),
        workspace_path=workspace("update"),
    )
    update_outputs = update_run.get("outputs") or {}
    update_answer = (
        (update_outputs.get("decision") or {}).get("answer_record") or {}
    )
    update_receipt = update_outputs.get("sync_receipt") or {}
    update_passed = (
        update_run["status"] == "succeeded"
        and str(revised_interval) in str(update_answer.get("answer"))
        and str(interval) not in str(update_answer.get("answer"))
        and pump_id in update_receipt.get("updated", [])
    )

    stale_probe = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/knowledge-indexes/exp002-bookstack-r1/sync",
        {
            "documents": [
                {
                    "source_id": pump_id,
                    "title": pump_definition["name"],
                    "content": pump_definition["html"],
                    "revision": revisions[pump_id],
                    "url": pump_source["browser_url"],
                    "allowed_roles": pump_source["allowed_roles"],
                    "metadata": {"seed_source": True},
                }
            ],
            "deleted_source_ids": [],
            "event_id": f"exp002-seed-stale-{args.seed}-{marker}",
        },
        expected_status=409,
    )
    stale_passed = stale_probe["status"] == 409

    bookstack_json(
        "DELETE",
        args.bookstack_base,
        setup_authorization,
        f"/api/pages/{pump_source['page_id']}",
    )
    delete_run = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=workflow_inputs(
            [item for item in sources if item["source_id"] != pump_id],
            event_id=f"exp002-seed-delete-{args.seed}-{marker}",
            question=f"How often is pump {pump} inspected?",
            roles=[roles["operations"]],
            deleted_source_ids=[pump_id],
        ),
        workspace_path=workspace("delete"),
    )
    delete_outputs = delete_run.get("outputs") or {}
    delete_answer = (
        (delete_outputs.get("decision") or {}).get("answer_record") or {}
    )
    delete_receipt = delete_outputs.get("sync_receipt") or {}
    delete_host = bookstack_json(
        "GET",
        args.bookstack_base,
        setup_authorization,
        f"/api/pages/{pump_source['page_id']}",
        expected_status=404,
    )
    delete_passed = (
        delete_run["status"] == "succeeded"
        and delete_answer.get("status") == "refused"
        and pump_id in delete_receipt.get("deleted", [])
        and delete_host["status"] == 404
    )

    run_ids = [item["run_id"] for item in case_results] + [
        replay_first["id"],
        replay_second["id"],
        update_run["id"],
        delete_run["id"],
    ]
    all_passed = (
        all(bool(item["passed"]) for item in case_results)
        and replay_passed
        and update_passed
        and stale_passed
        and delete_passed
    )
    report = {
        "schema_version": "1.0",
        "project_id": "EXP-LILIES-002",
        "project_revision": 1,
        "seed": args.seed,
        "attempt_nonce": attempt_nonce,
        "sealed_content_persisted": False,
        "independent_blackbox_claim": False,
        "case_count": len(case_results),
        "case_passed_count": sum(bool(item["passed"]) for item in case_results),
        "cases": case_results,
        "lifecycle_checks": {
            "idempotent_replay": replay_passed,
            "source_update": update_passed,
            "stale_revision_rejected": stale_passed,
            "source_delete": delete_passed,
        },
        "run_count": len(run_ids),
        "run_ids": run_ids,
        "human_resume_count": sum(
            int(item["resume_count"]) for item in case_results
        ),
        "artifact_count": sum(
            int(item["artifact_count"]) for item in case_results
        )
        + 12,
        "forbidden_chunk_count": sum(
            int(item["forbidden_chunk_count"] or 0) for item in case_results
        ),
        "forbidden_term_hit_count": sum(
            int(item["forbidden_term_hit_count"]) for item in case_results
        ),
        "passed": all_passed,
        "lilies_model_calls": 0,
        "lilies_tokens": 0,
    }
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "seed": args.seed,
                "passed": all_passed,
                "case_passed_count": report["case_passed_count"],
                "case_count": report["case_count"],
                "run_count": report["run_count"],
                "human_resume_count": report["human_resume_count"],
                "artifact_count": report["artifact_count"],
                "forbidden_chunk_count": report["forbidden_chunk_count"],
                "forbidden_term_hit_count": report["forbidden_term_hit_count"],
                "report_file": str(args.report_file),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
