from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import agent_platform.record_pipeline as record_pipeline
from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.record_pipeline import (
    JSON_MEDIA_TYPE,
    ConflictCheck,
    MatchCondition,
    RegexExtractConfig,
    deduplicate_records,
    extract_regex_fields,
    match_record,
    normalize_record_collection,
    validate_bounded_json_schema,
    validate_json_value,
    write_typed_json_artifact,
)
from agent_platform.workflow_models import DraftOperation
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import _issue, _request, _settings


def _customer_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "customer_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20,
                "pattern": r"^CUS-[0-9]+$",
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
            },
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
                "uniqueItems": True,
            },
        },
        "required": ["customer_id", "priority", "channels"],
        "additionalProperties": False,
    }


def _regex_config() -> RegexExtractConfig:
    return RegexExtractConfig.model_validate(
        {
            "text": "Ticket: CS-104\nMinutes: 45\nApproved: yes\nDate: 2026-07-25",
            "fields": [
                {
                    "name": "ticket",
                    "pattern": r"^Ticket:\s*([A-Z0-9-]+)$",
                    "group": 1,
                    "type": "string",
                    "required": True,
                    "flags": ["multiline", "ascii"],
                },
                {
                    "name": "minutes",
                    "pattern": r"^Minutes:\s*([0-9]+)$",
                    "group": 1,
                    "type": "integer",
                    "required": True,
                    "flags": ["multiline", "ascii"],
                },
                {
                    "name": "approved",
                    "pattern": r"^Approved:\s*([A-Za-z]+)$",
                    "group": 1,
                    "type": "boolean",
                    "required": True,
                    "flags": ["multiline", "ignorecase", "ascii"],
                },
                {
                    "name": "observed_on",
                    "pattern": r"^Date:\s*([0-9-]+)$",
                    "group": 1,
                    "type": "date",
                    "required": True,
                    "flags": ["multiline", "ascii"],
                },
            ],
        }
    )


def _condition(
    name: str,
    source_path: list[str],
    candidate_path: list[str],
    comparator: str,
    weight: float,
    *,
    required: bool = False,
) -> MatchCondition:
    return MatchCondition.model_validate(
        {
            "name": name,
            "source_path": source_path,
            "candidate_path": candidate_path,
            "comparator": comparator,
            "weight": weight,
            "required": required,
        }
    )


def _conflict(name: str, path: list[str]) -> ConflictCheck:
    return ConflictCheck.model_validate(
        {
            "name": name,
            "source_path": path,
            "candidate_path": path,
            "comparator": "exact",
        }
    )


def _match(
    source: dict,
    candidates: list[dict],
    *,
    ambiguity_threshold: float = 0.0,
) -> dict:
    return match_record(
        source,
        candidates,
        conditions=[
            _condition(
                "email",
                ["email"],
                ["email"],
                "casefold",
                3.0,
                required=True,
            ),
            _condition(
                "balance",
                ["balance"],
                ["balance"],
                "numeric",
                1.0,
            ),
        ],
        conflict_checks=[_conflict("region", ["region"])],
        min_score=0.75,
        ambiguity_threshold=ambiguity_threshold,
        result_limit=20,
    )


def _create_application(client: TestClient) -> str:
    response = client.post(
        "/api/v1/applications",
        headers={"Authorization": "Bearer internal-test-token"},
        json={
            "name": "Generic record workflow",
            "requirement": "Validate, extract, align, and persist bounded customer data.",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _apply_operations(
    client: TestClient,
    application_id: str,
    operations: list[tuple[str, dict]],
) -> None:
    revision = 0
    for index, (operation, data) in enumerate(operations):
        result = client.portal.call(
            partial(
                client.app.state.services.applications.apply_operation,
                application_id,
                DraftOperation(
                    expected_revision=revision,
                    idempotency_key=f"record-pipeline-draft-{index:04d}",
                    op=operation,
                    data=data,
                ),
            )
        )
        revision = int(result["revision"])


def test_bounded_json_schema_returns_stable_machine_readable_errors() -> None:
    valid = validate_json_value(
        {
            "customer_id": "CUS-42",
            "priority": 3,
            "channels": ["email", "phone"],
        },
        _customer_schema(),
    )
    assert valid == {
        "valid": True,
        "errors": [],
        "value": {
            "customer_id": "CUS-42",
            "priority": 3,
            "channels": ["email", "phone"],
        },
    }

    invalid_value = {
        "customer_id": "wrong",
        "priority": 7,
        "channels": ["email", "email"],
        "undeclared": True,
    }
    first = validate_json_value(invalid_value, _customer_schema())
    second = validate_json_value(invalid_value, _customer_schema())
    assert first == second
    assert first["valid"] is False
    assert [item["keyword"] for item in first["errors"]] == [
        "uniqueItems",
        "pattern",
        "maximum",
        "additionalProperties",
    ]
    assert first["value"] == invalid_value


def test_bounded_json_schema_rejects_remote_or_unbounded_features() -> None:
    with pytest.raises(ValueError, match="unsupported schema keywords"):
        validate_bounded_json_schema({"$ref": "https://example.invalid/schema.json"})
    with pytest.raises(ValueError, match="platform limit"):
        validate_bounded_json_schema(
            {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5_001,
            }
        )
    with pytest.raises(ValueError, match="quantified regex groups"):
        validate_bounded_json_schema(
            {
                "type": "string",
                "pattern": r"(a+)+$",
            }
        )
    with pytest.raises(TypeError, match="must be a JSON number"):
        validate_bounded_json_schema(
            {
                "type": "number",
                "minimum": "5",
            }
        )


def test_json_schema_uses_mathematical_numeric_equality() -> None:
    assert validate_json_value(
        1.0,
        {"type": "integer", "const": 1},
    )["valid"] is True
    duplicate_numbers = validate_json_value(
        [1, 1.0],
        {
            "type": "array",
            "uniqueItems": True,
        },
    )
    assert duplicate_numbers["valid"] is False
    assert duplicate_numbers["errors"][0]["keyword"] == "uniqueItems"
    with pytest.raises(ValueError, match="duplicate JSON values"):
        validate_bounded_json_schema({"enum": [1, 1.0]})
    first_large = 10**99 + 1
    second_large = 10**99 + 2
    assert validate_json_value(
        second_large,
        {"const": first_large},
    )["valid"] is False
    assert validate_json_value(
        [first_large, second_large],
        {"type": "array", "uniqueItems": True},
    )["valid"] is True


def test_regex_extract_is_strongly_typed_and_has_bounded_evidence() -> None:
    config = _regex_config()
    result = extract_regex_fields(config.text, config.fields)
    assert result["fields"] == {
        "ticket": "CS-104",
        "minutes": 45,
        "approved": True,
        "observed_on": "2026-07-25",
    }
    assert result["confidence"] == 1.0
    assert result["missing"] == []
    assert result["errors"] == []
    assert all(item["pattern_sha256"].startswith("sha256:") for item in result["evidence"])
    assert all("raw_sha256" in item for item in result["evidence"])

    with pytest.raises(ValidationError, match="regex alternation is not supported"):
        RegexExtractConfig.model_validate(
            {
                "text": "aaaa",
                "fields": [
                    {
                        "name": "unsafe",
                        "pattern": r"(a|aa)+",
                        "group": 1,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="unbounded wildcard"):
        RegexExtractConfig.model_validate(
            {
                "text": "anything",
                "fields": [
                    {
                        "name": "unsafe",
                        "pattern": r"(.*)",
                        "group": 1,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="overlapping variable repeats"):
        RegexExtractConfig.model_validate(
            {
                "text": "aaaa",
                "fields": [
                    {
                        "name": "unsafe",
                        "pattern": r"^a*a*a*b",
                        "group": 0,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="Unicode escapes"):
        RegexExtractConfig.model_validate(
            {
                "text": "aaaa",
                "fields": [
                    {
                        "name": "unsafe",
                        "pattern": r"^a+\x61+X",
                        "group": 0,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="leading literal closing bracket"):
        RegexExtractConfig.model_validate(
            {
                "text": "aaaa",
                "fields": [
                    {
                        "name": "unsafe",
                        "pattern": r"^[]a]+[a]+X",
                        "group": 0,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="overlapping variable repeats"):
        RegexExtractConfig.model_validate(
            {
                "text": "AAAA",
                "fields": [
                    {
                        "name": "unsafe",
                        "pattern": "^([A-Z]+)İ+X",
                        "group": 1,
                        "flags": ["ignorecase"],
                    }
                ],
            }
        )
    overflow = RegexExtractConfig.model_validate(
        {
            "text": "Value: 1e999",
            "fields": [
                {
                    "name": "value",
                    "pattern": r"Value:\s*([0-9e+]+)",
                    "group": 1,
                    "type": "number",
                    "flags": ["ascii"],
                }
            ],
        }
    )
    overflow_result = extract_regex_fields(overflow.text, overflow.fields)
    assert overflow_result["fields"]["value"] is None
    assert overflow_result["errors"][0]["code"] == "type_coercion_failed"


def test_regex_extract_fails_closed_when_process_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _regex_config()
    monkeypatch.setattr(
        record_pipeline,
        "REGEX_EXECUTION_TIMEOUT_SECONDS",
        0.000001,
    )
    result = extract_regex_fields(config.text, config.fields)
    assert result["confidence"] == 0.0
    assert result["errors"][0]["code"] == "regex_execution_timeout"
    assert result["missing"] == [
        "ticket",
        "minutes",
        "approved",
        "observed_on",
    ]
    with pytest.raises(RuntimeError, match="pattern execution failed closed"):
        validate_json_value(
            "CUS-42",
            {
                "type": "string",
                "pattern": r"^CUS-[0-9]+$",
            },
        )


def test_record_deduplicate_preserves_first_seen_order_and_receipts() -> None:
    records = [
        {"tenant": "north", "external_key": "A", "value": 1},
        {"tenant": "south", "external_key": "B", "value": 2},
        {"tenant": "north", "external_key": "A", "value": 9},
    ]
    result = deduplicate_records(
        records,
        [["tenant"], ["external_key"]],
    )
    assert result["unique"] == records[:2]
    assert result["duplicates"] == [
        {
            "index": 2,
            "first_index": 0,
            "record": records[2],
            "key_sha256": result["receipts"][2]["key_sha256"],
        }
    ]
    assert [item["status"] for item in result["receipts"]] == [
        "unique",
        "unique",
        "duplicate",
    ]
    assert result == deduplicate_records(records, [["tenant"], ["external_key"]])

    with pytest.raises(ValueError, match="missing configured key paths"):
        deduplicate_records([{"tenant": "north"}], [["external_key"]])
    kept = deduplicate_records(
        [{"tenant": "north"}, {"tenant": "north"}],
        [["external_key"]],
        missing_key_policy="keep",
    )
    assert len(kept["unique"]) == 2
    assert kept["duplicates"] == []


def test_record_collection_normalizes_arrays_envelopes_and_single_objects() -> None:
    records = [{"id": "A"}, {"id": "B"}]
    direct = normalize_record_collection(
        records,
        [["results"], ["items"]],
    )
    assert direct == {
        "records": records,
        "count": 2,
        "empty": False,
        "source_shape": "array",
        "selected_path": [],
    }

    enveloped = normalize_record_collection(
        {"meta": {"page": 1}, "payload": {"items": records}},
        [["results"], ["payload", "items"]],
        single_object_policy="error",
    )
    assert enveloped["records"] == records
    assert enveloped["source_shape"] == "envelope"
    assert enveloped["selected_path"] == ["payload", "items"]

    wrapped = normalize_record_collection(
        {"id": "A"},
        [["results"]],
    )
    assert wrapped["records"] == [{"id": "A"}]
    assert wrapped["source_shape"] == "object"

    with pytest.raises(ValueError, match="contains none"):
        normalize_record_collection(
            {"id": "A"},
            [["results"]],
            single_object_policy="error",
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        normalize_record_collection(
            {"results": []},
            [["results"]],
            empty_policy="error",
        )


def test_record_match_has_all_four_statuses_and_stable_candidate_evidence() -> None:
    source = {
        "email": "Customer@Example.COM",
        "balance": "10.00",
        "region": "north",
    }
    matching = {
        "account_id": "A-1",
        "email": "customer@example.com",
        "balance": 10,
        "region": "north",
    }
    matched = _match(source, [matching])
    assert matched["status"] == "matched"
    assert matched["match"] == {
        "index": 0,
        "candidate": matching,
        "score": 1.0,
    }

    ambiguous = _match(
        source,
        [
            matching,
            {**matching, "account_id": "A-2"},
        ],
    )
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["match"] is None
    assert [item["index"] for item in ambiguous["candidates"]] == [0, 1]

    conflict = _match(source, [{**matching, "region": "south"}])
    assert conflict["status"] == "conflict"
    assert conflict["candidates"][0]["conflicts"][0]["name"] == "region"

    not_found = _match(
        source,
        [
            {
                "account_id": "A-3",
                "email": "different@example.com",
                "balance": 99,
                "region": "north",
            }
        ],
    )
    assert not_found["status"] == "not_found"
    assert not_found["evidence"]["qualified_count"] == 0


def test_record_match_required_mismatch_and_decimal_threshold_are_fail_closed() -> None:
    required_mismatch = match_record(
        {"strong_id": "expected", "weak": "same"},
        [{"strong_id": "different", "weak": "same"}],
        conditions=[
            _condition(
                "strong_id",
                ["strong_id"],
                ["strong_id"],
                "exact",
                1.0,
                required=True,
            ),
            _condition("weak", ["weak"], ["weak"], "exact", 99.0),
        ],
        conflict_checks=[],
        min_score=0.9,
        ambiguity_threshold=0.0,
        result_limit=20,
    )
    assert required_mismatch["status"] == "not_found"
    assert required_mismatch["candidates"][0]["disqualified"] is True

    below_threshold = match_record(
        {"first": "same", "second": "source"},
        [{"first": "same", "second": "candidate"}],
        conditions=[
            _condition("first", ["first"], ["first"], "exact", 2.0),
            _condition("second", ["second"], ["second"], "exact", 1.0),
        ],
        conflict_checks=[],
        min_score=0.6666668,
        ambiguity_threshold=0.0,
        result_limit=20,
    )
    assert below_threshold["candidates"][0]["score"] == 0.666667
    assert below_threshold["status"] == "not_found"
    assert "_score" not in below_threshold["candidates"][0]


def test_typed_json_artifact_is_canonical_safe_and_replayable(tmp_path: Path) -> None:
    workspace = tmp_path / "run"
    workspace.mkdir()
    value = {
        "records": [
            {"customer_id": "CUS-2", "priority": 2},
            {"customer_id": "CUS-1", "priority": 1},
        ],
        "accepted": True,
    }
    lineage = [
        {
            "source_type": "workflow_input",
            "reference": "customer_records",
            "sha256": f"sha256:{'1' * 64}",
        }
    ]
    first = write_typed_json_artifact(
        workspace=workspace,
        value=value,
        filename="customer-records.json",
        lineage=lineage,
        run_id="run-1",
        node_id="artifact-1",
        application_id="application-1",
    )
    replay = write_typed_json_artifact(
        workspace=workspace,
        value=value,
        filename="customer-records.json",
        lineage=lineage,
        run_id="run-1",
        node_id="artifact-1",
        application_id="application-1",
    )
    assert replay == {**first, "replayed": True}
    assert first["media_type"] == JSON_MEDIA_TYPE
    assert first["lineage"]["generator"] == {
        "block_type": "typed_json_artifact",
        "block_version": 1,
    }
    payload = (workspace / "artifacts" / "customer-records.json").read_bytes()
    assert payload == (
        b'{"accepted":true,"records":[{"customer_id":"CUS-2","priority":2},'
        b'{"customer_id":"CUS-1","priority":1}]}\n'
    )

    with pytest.raises(FileExistsError, match="different content"):
        write_typed_json_artifact(
            workspace=workspace,
            value={**value, "accepted": False},
            filename="customer-records.json",
            lineage=[],
            run_id="run-1",
            node_id="artifact-1",
            application_id="application-1",
        )
    with pytest.raises(ValueError, match="plain ASCII .json basename"):
        write_typed_json_artifact(
            workspace=workspace,
            value=value,
            filename="../escape.json",
            lineage=[],
            run_id="run-1",
            node_id="artifact-1",
            application_id="application-1",
        )

    unsafe = tmp_path / "unsafe"
    outside = tmp_path / "outside"
    unsafe.mkdir()
    outside.mkdir()
    (unsafe / "artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot be a symbolic link"):
        write_typed_json_artifact(
            workspace=unsafe,
            value=value,
            filename="safe.json",
            lineage=[],
            run_id="run-2",
            node_id="artifact-2",
            application_id="application-2",
        )
    assert list(outside.iterdir()) == []


def test_public_manual_and_runtime_expose_all_generic_record_blocks(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_application(client)
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        _apply_operations(
            client,
            application_id,
            [
                (
                    "add_node",
                    {
                        "node": {
                            "id": "start",
                            "type": "start",
                            "title": "Input",
                            "config": {
                                "inputs": [
                                    {"name": "payload", "type": "object"},
                                    {"name": "text", "type": "string"},
                                    {"name": "records", "type": "array"},
                                    {"name": "response", "type": "object"},
                                    {"name": "source", "type": "object"},
                                    {"name": "candidates", "type": "array"},
                                    {"name": "artifact_value", "type": "object"},
                                ]
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "validate",
                            "type": "json_schema_validate",
                            "title": "Validate",
                            "config": {
                                "value": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["payload"],
                                    }
                                },
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "request_id": {"type": "string"},
                                        "priority": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 5,
                                        },
                                    },
                                    "required": ["request_id", "priority"],
                                    "additionalProperties": False,
                                },
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "extract",
                            "type": "regex_extract",
                            "title": "Extract",
                            "config": {
                                "text": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["text"],
                                    }
                                },
                                "fields": [
                                    {
                                        "name": "ticket",
                                        "pattern": r"Ticket:\s*([A-Z0-9-]+)",
                                        "group": 1,
                                        "type": "string",
                                        "flags": ["ascii"],
                                    },
                                    {
                                        "name": "minutes",
                                        "pattern": r"Minutes:\s*([0-9]+)",
                                        "group": 1,
                                        "type": "integer",
                                        "flags": ["ascii"],
                                    },
                                ],
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "normalize",
                            "type": "record_collection_normalize",
                            "title": "Normalize response",
                            "config": {
                                "value": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["response"],
                                    }
                                },
                                "record_paths": [["payload", "items"], ["results"]],
                                "single_object_policy": "error",
                                "empty_policy": "allow",
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "deduplicate",
                            "type": "record_deduplicate",
                            "title": "Deduplicate",
                            "config": {
                                "records": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["records"],
                                    }
                                },
                                "key_paths": [["tenant"], ["external_key"]],
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "match",
                            "type": "record_match",
                            "title": "Match",
                            "config": {
                                "source": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["source"],
                                    }
                                },
                                "candidates": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["candidates"],
                                    }
                                },
                                "conditions": [
                                    {
                                        "name": "email",
                                        "source_path": ["email"],
                                        "candidate_path": ["email"],
                                        "comparator": "casefold",
                                        "weight": 1.0,
                                        "required": True,
                                    }
                                ],
                                "conflict_checks": [
                                    {
                                        "name": "region",
                                        "source_path": ["region"],
                                        "candidate_path": ["region"],
                                        "comparator": "exact",
                                    }
                                ],
                                "min_score": 1.0,
                                "ambiguity_threshold": 0.0,
                                "result_limit": 10,
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "artifact",
                            "type": "typed_json_artifact",
                            "title": "Artifact",
                            "config": {
                                "value": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["artifact_value"],
                                    }
                                },
                                "filename": "customer-output.json",
                                "lineage": [
                                    {
                                        "source_type": "workflow_input",
                                        "reference": "artifact_value",
                                    }
                                ],
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "end",
                            "type": "end",
                            "title": "End",
                            "config": {
                                "outputs": {
                                    "validation": {
                                        "$ref": {
                                            "node_id": "validate",
                                            "path": ["output"],
                                        }
                                    },
                                    "extraction": {
                                        "$ref": {
                                            "node_id": "extract",
                                            "path": ["output"],
                                        }
                                    },
                                    "deduplication": {
                                        "$ref": {
                                            "node_id": "deduplicate",
                                            "path": ["output"],
                                        }
                                    },
                                    "normalized": {
                                        "$ref": {
                                            "node_id": "normalize",
                                            "path": ["records"],
                                        }
                                    },
                                    "matching": {
                                        "$ref": {
                                            "node_id": "match",
                                            "path": ["output"],
                                        }
                                    },
                                    "selected_account_id": {
                                        "$ref": {
                                            "node_id": "match",
                                            "path": ["match", "candidate", "account_id"],
                                        }
                                    },
                                    "artifact": {
                                        "$ref": {
                                            "node_id": "artifact",
                                            "path": ["artifact"],
                                        }
                                    },
                                }
                            },
                        }
                    },
                ),
                *[
                    (
                        "add_edge",
                        {
                            "edge": {
                                "id": f"start-{node_id}",
                                "source": "start",
                                "target": node_id,
                                "source_port": "output",
                                "target_port": "input",
                            }
                        },
                    )
                    for node_id in (
                        "validate",
                        "extract",
                        "deduplicate",
                        "normalize",
                        "match",
                        "artifact",
                    )
                ],
                *[
                    (
                        "add_edge",
                        {
                            "edge": {
                                "id": f"{node_id}-end",
                                "source": node_id,
                                "target": "end",
                                "source_port": "output",
                                "target_port": "input",
                            }
                        },
                    )
                    for node_id in (
                        "validate",
                        "extract",
                        "deduplicate",
                        "normalize",
                        "match",
                        "artifact",
                    )
                ],
            ],
        )

        block_types = (
            "json_schema_validate",
            "regex_extract",
            "record_deduplicate",
            "record_collection_normalize",
            "record_match",
            "typed_json_artifact",
        )
        for index, block_type in enumerate(block_types):
            manual = _request(
                client,
                "GET",
                f"/api/v1/lilies/blocks/{block_type}",
                headers,
                key=f"record-block-manual-{index:04d}",
            )
            assert manual.status_code == 200, manual.text
            assert manual.json()["data"]["definition"]["type"] == block_type
            assert manual.json()["data"]["manual"]["examples"]
            if block_type == "record_match":
                match_port = next(
                    port
                    for port in manual.json()["data"]["definition"]["output_ports"]
                    if port["name"] == "match"
                )
                assert "match.candidate" in match_port["description"]
                assert "record is not an output alias" in " ".join(
                    manual.json()["data"]["manual"]["composability_constraints"]
                )

        artifact_value = {
            "dataset": "customer-support",
            "records": [{"ticket": "CS-104", "minutes": 45}],
        }
        started = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="record-pipeline-run-0001",
            json={
                "inputs": {
                    "payload": {
                        "request_id": "REQ-1",
                        "priority": 3,
                    },
                    "text": "Ticket: CS-104\nMinutes: 45",
                    "records": [
                        {
                            "tenant": "north",
                            "external_key": "A",
                            "value": 1,
                        },
                        {
                            "tenant": "north",
                            "external_key": "A",
                            "value": 9,
                        },
                    ],
                    "response": {
                        "payload": {
                            "items": [
                                {"account_id": "A-1"},
                                {"account_id": "A-2"},
                            ]
                        }
                    },
                    "source": {
                        "email": "Customer@Example.com",
                        "region": "north",
                    },
                    "candidates": [
                        {
                            "account_id": "A-1",
                            "email": "customer@example.com",
                            "region": "north",
                        }
                    ],
                    "artifact_value": artifact_value,
                },
                "use_draft": True,
            },
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["data"]["run_id"]
        for index in range(100):
            result = _request(
                client,
                "GET",
                f"/api/v1/lilies/runs/{run_id}",
                headers,
                key=f"record-pipeline-run-poll-{index:04d}",
            )
            assert result.status_code == 200, result.text
            if result.json()["data"]["status"] in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                break
        data = result.json()["data"]
        assert data["status"] == "succeeded", result.text
        assert data["outputs"]["validation"]["valid"] is True
        assert data["outputs"]["extraction"]["fields"] == {
            "ticket": "CS-104",
            "minutes": 45,
        }
        assert len(data["outputs"]["deduplication"]["unique"]) == 1
        assert data["outputs"]["normalized"] == [
            {"account_id": "A-1"},
            {"account_id": "A-2"},
        ]
        assert data["outputs"]["matching"]["status"] == "matched"
        assert data["outputs"]["selected_account_id"] == "A-1"
        descriptor = data["outputs"]["artifact"]
        assert descriptor["media_type"] == JSON_MEDIA_TYPE
        assert len(data["artifacts"]) == 1
        registered = data["artifacts"][0]
        assert registered["sha256"] == descriptor["sha256"]

        downloaded = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{registered['artifact_id']}",
            headers,
            key="record-pipeline-artifact-read-0001",
        )
        assert downloaded.status_code == 200, downloaded.text
        artifact_data = downloaded.json()["data"]
        assert artifact_data["encoding"] == "utf8"
        payload = artifact_data["content"].encode("utf-8")
        assert json.loads(payload) == artifact_value


def test_registry_is_generic_and_contains_a_non_source_business_example() -> None:
    registry = build_block_registry()
    block_types = (
        "json_schema_validate",
        "regex_extract",
        "record_deduplicate",
        "record_collection_normalize",
        "record_match",
        "typed_json_artifact",
    )
    manuals = [registry.manual(block_type) for block_type in block_types]
    serialized = json.dumps(manuals, ensure_ascii=False).casefold()
    assert "customer service request" in serialized
    assert '"path": ["match", "candidate"]' in serialized
    assert "record is not an output alias" in serialized
    assert "connector_id" not in serialized
    assert all(registry.get(item).block_kind == "business_workflow" for item in block_types)
