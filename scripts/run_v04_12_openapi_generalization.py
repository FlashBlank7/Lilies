#!/usr/bin/env python3
"""Measure one OpenAPI-to-Connector delivery without host-specific assistance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.openapi_connector import (  # noqa: E402
    OpenAPIConnectorGenerationRequest,
    OpenAPIConnectorGenerator,
    OpenAPIMaterialLoader,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the generic OpenAPI Connector generation experiment."
    )
    parser.add_argument("--name", required=True, help="Evidence label, not a behavior switch")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--connector-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allowed-host", action="append", required=True)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-command", required=True)
    parser.add_argument(
        "--forbidden-term",
        action="append",
        default=[],
        help="Host name that must not appear in platform or experiment source",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def operation_kind(method: str) -> str:
    return "read" if method.casefold() == "get" else "write"


def generated_case_counts(generation: Any) -> dict[str, int]:
    positive = len(generation.manifest.operations)
    negative = len(generation.manifest.operations)
    return {"positive": positive, "negative": negative, "total": positive + negative}


def forbidden_assistance_scan(forbidden_terms: list[str]) -> dict[str, Any]:
    source_files = [
        path
        for root in (ROOT / "platform", ROOT / "scripts")
        for path in root.glob("**/*")
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".js", ".mjs"}
        and not {"node_modules", ".next", ".tmp"}.intersection(path.parts)
    ]
    authored_contract_tokens = (
        "Connector" + "Manifest(",
        "Connector" + "Operation(",
        "ConnectorObject" + "Schema(",
    )
    experiment_files = [
        ROOT / "scripts" / "run_v04_12_openapi_generalization.py",
        ROOT / "scripts" / "run_v04_12_openapi_live_contract.py",
    ]
    authored_hits = [
        {"file": str(path.relative_to(ROOT)), "token": token}
        for path in experiment_files
        for token in authored_contract_tokens
        if token in path.read_text(encoding="utf-8")
    ]
    host_branch_hits = [
        str(path.relative_to(ROOT))
        for path in experiment_files
        if re.search(
            r"\b(if|elif|match)\b[^\n]{0,160}\bargs\.name\b",
            path.read_text(encoding="utf-8"),
        )
    ]
    forbidden_term_hits: list[dict[str, str]] = []
    for path in source_files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        compact = " ".join(source.casefold().split())
        for term in forbidden_terms:
            if term.casefold() in compact:
                forbidden_term_hits.append(
                    {"file": str(path.relative_to(ROOT)), "term": term}
                )
    return {
        "status": (
            "pass"
            if not authored_hits and not host_branch_hits and not forbidden_term_hits
            else "fail"
        ),
        "authored_contract_tokens": authored_hits,
        "host_name_branch_files": host_branch_hits,
        "forbidden_term_hits": forbidden_term_hits,
        "forbidden_terms": forbidden_terms,
        "scanned_file_count": len(source_files),
        "method": "static scan of platform and all experiment-script source",
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    raw = args.spec.read_bytes()
    request = OpenAPIConnectorGenerationRequest.model_validate(
        {
            "connector_id": args.connector_id,
            "version": 1,
            "domain": args.domain,
            "document": raw.decode("utf-8"),
            "deployment": {
                "profile_id": "frozen-experiment",
                "environment": "test",
                "base_url": args.base_url,
                "allowed_hosts": args.allowed_host,
                "available": True,
                "claim_ceiling": "H3",
            },
        }
    )
    loader = OpenAPIMaterialLoader()
    parse_started = time.perf_counter()
    document, provenance, gaps = await loader.load(request)
    parse_wall_ms = (time.perf_counter() - parse_started) * 1000
    generation = OpenAPIConnectorGenerator().generate(
        document,
        request,
        provenance,
        gaps,
        parse_ms=parse_wall_ms,
    )
    revalidation_started = time.perf_counter()
    revalidated = OpenAPIConnectorGenerator().generate(
        document,
        request,
        provenance,
        gaps,
        parse_ms=parse_wall_ms,
    )
    upstream_revalidation_ms = (time.perf_counter() - revalidation_started) * 1000
    if (
        revalidated.discovered_operation_count != generation.discovered_operation_count
        or revalidated.generated_operation_count != generation.generated_operation_count
    ):
        raise RuntimeError("same-source revalidation changed the generated operation denominator")
    operations = generation.manifest.operations
    operation_mix = Counter(operation_kind(item.method) for item in operations)
    gap_counts = Counter(item.code for item in generation.gaps)
    total_fields = generation.total_field_count
    generated_operations = generation.generated_operation_count
    discovered_operations = generation.discovered_operation_count
    output: dict[str, Any] = {
        "schema_version": "v0.4.12-openapi-generalization-1",
        "experiment": args.name,
        "claim": "static OpenAPI intake, Connector generation, and generated contract-case coverage",
        "claim_ceiling": "H3 generation evidence; live contract evidence is reported separately",
        "source": {
            "repository": args.source_repository,
            "commit": args.source_commit,
            "spec_path": str(args.spec),
            "spec_sha256": hashlib.sha256(raw).hexdigest(),
            "spec_bytes": len(raw),
            "freeze_command": args.source_command,
            "openapi_version": provenance.openapi_version,
            "document_version": provenance.document_version,
            "title": provenance.title,
        },
        "delivery": {
            "discovered_operations": discovered_operations,
            "generated_operations": generated_operations,
            "unsupported_operations": discovered_operations - generated_operations,
            "supported_operation_rate": (
                generated_operations / discovered_operations if discovered_operations else 0
            ),
            "generated_operation_mix": dict(sorted(operation_mix.items())),
            "mapped_fields_in_generated_operations": generation.mapped_field_count,
            "total_fields_in_generated_operations": total_fields,
            "generated_field_mapping_rate": (
                generation.mapped_field_count / total_fields if total_fields else 1
            ),
            "generated_contract_cases": generated_case_counts(generation),
            "parse_ms": round(generation.parse_ms, 3),
            "generate_ms": round(generation.generate_ms, 3),
            "end_to_end_generation_ms": round((time.perf_counter() - started) * 1000, 3),
            "generation_attempts": 1,
            "repair_attempts": 0,
            "contract_validation_attempts": 0,
            "test_ms": None,
            "time_to_first_valid_contract_ms": None,
            "upstream_revalidation_ms": round(upstream_revalidation_ms, 3),
            "model_calls": 0,
            "model_cost_usd": 0,
            "human_authored_adapter_count": 0,
            "human_authored_mapping_count": 0,
            "human_rescue_count": 0,
        },
        "capability_gaps": {
            "total": len(generation.gaps),
            "by_code": {f"IF-{number:02d}": gap_counts.get(f"IF-{number:02d}", 0) for number in range(1, 15)},
            "items": [item.model_dump(mode="json") for item in generation.gaps],
        },
        "live_contracts": {
            "status": "not_run",
            "reason": "this command measures generation only; a controlled host run must append live evidence",
            "read": {"status": "not_run"},
            "write": {"status": "not_run"},
        },
        "forbidden_assistance_scan": forbidden_assistance_scan(args.forbidden_term),
        "reproduce": {
            "command": " ".join(sys.argv),
            "python": sys.version.split()[0],
        },
    }
    return output


def main() -> int:
    args = parse_args()
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "delivery": result["delivery"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
