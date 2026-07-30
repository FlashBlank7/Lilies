#!/usr/bin/env python3
"""Run EXP-LILIES-002 public business acceptance through platform APIs."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    expected_status: int | None = None,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        if expected_status is not None and error.code == expected_status:
            return {"status": error.code}
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} returned HTTP {error.code}: {detail}"
        ) from error
    if expected_status is not None and status != expected_status:
        raise RuntimeError(
            f"{method} {url} returned HTTP {status}, expected {expected_status}"
        )
    return json.loads(payload) if payload else None


def platform_json(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
    *,
    expected_status: int | None = None,
) -> Any:
    return http_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
        expected_status=expected_status,
    )


def bookstack_json(
    method: str,
    base_url: str,
    authorization: str,
    path: str,
    body: Any | None = None,
    *,
    expected_status: int | None = None,
) -> Any:
    return http_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": authorization},
        body=body,
        expected_status=expected_status,
    )


def run_workflow(
    *,
    base_url: str,
    token: str,
    application_id: str,
    inputs: dict[str, Any],
    workspace_path: str,
    review: dict[str, Any] | None = None,
    use_draft: bool = True,
) -> dict[str, Any]:
    created = platform_json(
        "POST",
        base_url,
        token,
        f"/api/v1/applications/{application_id}/runs",
        {
            "inputs": inputs,
            "use_draft": use_draft,
            "workspace_path": workspace_path,
        },
    )
    run_id = str(created["run_id"])
    resumes = 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = platform_json("GET", base_url, token, f"/api/v1/runs/{run_id}")
        if run["status"] == "paused":
            if review is None:
                raise RuntimeError(f"run {run_id} paused without a review response")
            platform_json(
                "POST",
                base_url,
                token,
                f"/api/v1/runs/{run_id}/resume",
                {"values": review},
            )
            resumes += 1
        elif run["status"] not in {"queued", "running"}:
            run["resume_count"] = resumes
            return run
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not complete")


def case_result(case: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    outputs = run.get("outputs") or {}
    state_outputs = run["state"]["outputs"]
    decision = outputs.get("decision") or {}
    answer_record = decision.get("answer_record") or {}
    retrieval = outputs.get("retrieval") or {}
    citations = answer_record.get("citations") or []
    serialized = json.dumps(
        {"decision": decision, "retrieval": retrieval},
        ensure_ascii=False,
        sort_keys=True,
    )
    forbidden_hits = [
        term for term in case.get("forbidden_terms", []) if term in serialized
    ]
    expected_source = case.get("expected_source_key")
    cited_sources = [
        str(item.get("source_id"))
        for item in citations
        if isinstance(item, dict)
    ]
    retrieved_sources = [
        str(item.get("source_id"))
        for item in retrieval.get("results", [])
        if isinstance(item, dict)
    ]
    artifacts = {
        name: outputs.get(name)
        for name in (
            "answer_artifact",
            "retrieval_artifact",
            "sync_artifact",
        )
    }
    expected_resumes = 1 if case.get("requires_review") else 0
    if answer_record.get("status") == "refused_by_reviewer":
        source_ok = (
            expected_source is None or expected_source in retrieved_sources
        ) and not cited_sources
    else:
        source_ok = expected_source is None or expected_source in cited_sources
    fetched = state_outputs.get("read_sources", {}).get("items", [])
    passed = (
        run["status"] == "succeeded"
        and answer_record.get("status") == case["expected_status"]
        and source_ok
        and retrieval.get("forbidden_chunk_count") == 0
        and not forbidden_hits
        and run["resume_count"] == expected_resumes
        and all(isinstance(value, dict) for value in artifacts.values())
        and len(fetched) == len(run["state"]["inputs"]["sources"])
    )
    return {
        "business_case": case["business_case"],
        "run_id": run["id"],
        "run_status": run["status"],
        "passed": passed,
        "expected_status": case["expected_status"],
        "actual_status": answer_record.get("status"),
        "expected_source": expected_source,
        "cited_sources": cited_sources,
        "retrieved_sources": retrieved_sources,
        "answer": answer_record.get("answer"),
        "citation_revisions": [
            item.get("revision") for item in citations if isinstance(item, dict)
        ],
        "authorized_document_count": (
            retrieval.get("acl_decision") or {}
        ).get("authorized_documents"),
        "filtered_document_count": len(
            (retrieval.get("acl_decision") or {}).get(
                "filtered_source_ids",
                [],
            )
        ),
        "retrieved_count": retrieval.get("retrieved_count"),
        "forbidden_chunk_count": retrieval.get("forbidden_chunk_count"),
        "forbidden_hits": forbidden_hits,
        "resume_count": run["resume_count"],
        "source_read_count": len(fetched),
        "sync": {
            key: (outputs.get("sync_receipt") or {}).get(key)
            for key in (
                "event_id",
                "inserted",
                "updated",
                "deleted",
                "unchanged",
                "index_revision",
                "changed",
                "replayed",
            )
        },
        "artifacts": artifacts,
    }


def workflow_inputs(
    sources: list[dict[str, Any]],
    *,
    event_id: str,
    question: str,
    roles: list[str],
    requires_review: bool = False,
    deleted_source_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "sources": sources,
        "deleted_source_ids": deleted_source_ids or [],
        "sync_event_id": event_id,
        "question": question,
        "principal_roles": roles,
        "requires_review": requires_review,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8015")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--bookstack-base", default="http://127.0.0.1:18020")
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--sources-file", type=Path, required=True)
    parser.add_argument("--fixtures-file", type=Path, required=True)
    parser.add_argument("--cases-file", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    args = parser.parse_args()

    credential = json.loads(args.credential_file.read_text(encoding="utf-8"))
    sources = json.loads(args.sources_file.read_text(encoding="utf-8"))
    fixtures = json.loads(args.fixtures_file.read_text(encoding="utf-8"))
    cases = json.loads(args.cases_file.read_text(encoding="utf-8"))
    fixture_by_key = {item["source_key"]: item for item in fixtures}
    source_by_key = {item["source_id"]: item for item in sources}
    run_tag = uuid4().hex[:10]
    results: list[dict[str, Any]] = []
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

    def workspace(name: str) -> str:
        relative = f"exp002-public-debug/{run_tag}/{name}"
        (args.workspace_root / relative).mkdir(parents=True, exist_ok=True)
        return relative

    for index, case in enumerate(cases, start=1):
        inputs = workflow_inputs(
            sources,
            event_id=f"exp002-debug-{run_tag}-{index:02d}",
            question=case["question"],
            roles=case["principal_roles"],
            requires_review=bool(case.get("requires_review")),
        )
        run = run_workflow(
            base_url=args.platform_base,
            token=args.platform_token,
            application_id=args.application_id,
            inputs=inputs,
            workspace_path=workspace(f"{index:02d}"),
            review=case.get("review"),
        )
        results.append(case_result(case, run))

    replay_event = f"exp002-debug-replay-{run_tag}"
    replay_inputs = workflow_inputs(
        sources,
        event_id=replay_event,
        question="Which extension reaches the on-site security desk?",
        roles=["visitor"],
    )
    first_replay = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=replay_inputs,
        workspace_path=workspace("replay-first"),
    )
    second_replay = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=replay_inputs,
        workspace_path=workspace("replay-second"),
    )
    first_receipt = (first_replay.get("outputs") or {}).get("sync_receipt") or {}
    second_receipt = (second_replay.get("outputs") or {}).get("sync_receipt") or {}
    replay_check = {
        "first_run_id": first_replay["id"],
        "second_run_id": second_replay["id"],
        "first_index_revision": first_receipt.get("index_revision"),
        "second_index_revision": second_receipt.get("index_revision"),
        "second_replayed": second_receipt.get("replayed"),
        "passed": (
            first_replay["status"] == "succeeded"
            and second_replay["status"] == "succeeded"
            and second_receipt.get("replayed") is True
            and first_receipt.get("index_revision")
            == second_receipt.get("index_revision")
        ),
    }

    pump_source = source_by_key["ops-pump-inspection"]
    pump_fixture = fixture_by_key["ops-pump-inspection"]
    updated_html = (
        "<p>Pump P-08 now requires a seal inspection every 14 operating days. "
        "Record the vibration reading before opening the inspection ticket.</p>"
    )
    bookstack_json(
        "PUT",
        args.bookstack_base,
        credential["setup_api_authorization"],
        f"/api/pages/{pump_source['page_id']}",
        {
            "book_id": pump_source["book_id"],
            "name": pump_fixture["name"],
            "html": updated_html,
            "tags": pump_fixture.get("tags", []),
        },
    )
    update_case = {
        "business_case": "source_update_replaces_old_revision",
        "question": "How often is Pump P-08 inspected?",
        "principal_roles": ["operations"],
        "expected_status": "answered",
        "expected_source_key": "ops-pump-inspection",
        "forbidden_terms": ["30 operating days", "SILVER-CEDAR", "HUSH-ORCHID"],
        "requires_review": False,
    }
    update_run = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=workflow_inputs(
            sources,
            event_id=f"exp002-debug-update-{run_tag}",
            question=update_case["question"],
            roles=update_case["principal_roles"],
        ),
        workspace_path=workspace("update"),
    )
    update_result = case_result(update_case, update_run)
    update_result["passed"] = bool(
        update_result["passed"]
        and "14 operating days" in str(update_result["answer"])
        and pump_source["original_updated_at"]
        not in update_result["citation_revisions"]
        and "ops-pump-inspection" in update_result["sync"]["updated"]
    )

    stale_document = {
        "source_id": "ops-pump-inspection",
        "title": pump_fixture["name"],
        "content": pump_fixture["html"],
        "revision": pump_source["original_updated_at"],
        "url": pump_source["browser_url"],
        "allowed_roles": pump_source["allowed_roles"],
        "metadata": {
            "bookstack_page_id": pump_source["page_id"],
            "source_system": "BookStack",
        },
    }
    stale_probe = platform_json(
        "POST",
        args.platform_base,
        args.platform_token,
        "/api/v1/knowledge-indexes/exp002-bookstack-r1/sync",
        {
            "documents": [stale_document],
            "deleted_source_ids": [],
            "event_id": f"exp002-stale-{run_tag}",
        },
        expected_status=409,
    )
    stale_check = {
        "status": stale_probe["status"],
        "passed": stale_probe["status"] == 409,
    }

    bookstack_json(
        "DELETE",
        args.bookstack_base,
        credential["setup_api_authorization"],
        f"/api/pages/{pump_source['page_id']}",
    )
    remaining_sources = [
        item for item in sources if item["source_id"] != "ops-pump-inspection"
    ]
    delete_case = {
        "business_case": "source_delete_removes_old_content",
        "question": "How often is Pump P-08 inspected?",
        "principal_roles": ["operations"],
        "expected_status": "refused",
        "forbidden_terms": [
            "14 operating days",
            "30 operating days",
            "SILVER-CEDAR",
            "HUSH-ORCHID",
        ],
        "requires_review": False,
    }
    delete_run = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        inputs=workflow_inputs(
            remaining_sources,
            event_id=f"exp002-debug-delete-{run_tag}",
            question=delete_case["question"],
            roles=delete_case["principal_roles"],
            deleted_source_ids=["ops-pump-inspection"],
        ),
        workspace_path=workspace("delete"),
    )
    delete_result = case_result(delete_case, delete_run)
    host_delete_probe = bookstack_json(
        "GET",
        args.bookstack_base,
        credential["setup_api_authorization"],
        f"/api/pages/{pump_source['page_id']}",
        expected_status=404,
    )
    delete_result["host_page_status"] = host_delete_probe["status"]
    delete_result["passed"] = bool(
        delete_result["passed"]
        and host_delete_probe["status"] == 404
        and "ops-pump-inspection" in delete_result["sync"]["deleted"]
    )

    all_checks = (
        [bool(item["passed"]) for item in results]
        + [
            bool(replay_check["passed"]),
            bool(update_result["passed"]),
            bool(stale_check["passed"]),
            bool(delete_result["passed"]),
        ]
    )
    report = {
        "schema_version": "1.0",
        "project_id": "EXP-LILIES-002",
        "revision": 1,
        "application_id": args.application_id,
        "run_tag": run_tag,
        "public_case_count": len(results),
        "public_case_passed": sum(bool(item["passed"]) for item in results),
        "cases": results,
        "idempotent_replay": replay_check,
        "source_update": update_result,
        "stale_revision_rejection": stale_check,
        "source_delete": delete_result,
        "passed": all(all_checks),
        "lilies_model_calls": 0,
        "lilies_tokens": 0,
        "credential_values_in_report": False,
    }
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "public_case_passed": report["public_case_passed"],
                "public_case_count": report["public_case_count"],
                "idempotent_replay_passed": replay_check["passed"],
                "update_passed": update_result["passed"],
                "stale_rejection_passed": stale_check["passed"],
                "delete_passed": delete_result["passed"],
                "report_file": str(args.report_file),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
