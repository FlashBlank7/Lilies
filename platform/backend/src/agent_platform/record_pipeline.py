from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import stat
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .typed_workbook import (
    WorkflowValueReference,
    _digest,
    _safe_artifact_directory,
    _safe_workspace,
)


JSON_MEDIA_TYPE = "application/json"
MAX_JSON_BYTES = 2_000_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000
MAX_CONTAINER_ITEMS = 5_000
MAX_STRING_CHARS = 262_144
MAX_SCHEMA_DEPTH = 12
MAX_SCHEMA_NODES = 256
MAX_SCHEMA_PROPERTIES = 128
MAX_SCHEMA_ENUM_VALUES = 128
MAX_SCHEMA_PATTERN_CHECKS = 5_000
MAX_VALIDATION_ERRORS = 100
MAX_REGEX_FIELDS = 64
MAX_REGEX_PATTERN_CHARS = 256
MAX_REGEX_TEXT_CHARS = 32_768
MAX_REGEX_GROUPS = 16
REGEX_EXECUTION_TIMEOUT_SECONDS = 2.0
MAX_RECORDS = 5_000
MAX_KEY_PATHS = 16
MAX_PATH_DEPTH = 16
MAX_MATCH_CONDITIONS = 32
MAX_MATCH_RESULTS = 100

_SAFE_JSON_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,91}\.json$")
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SAFE_GROUP_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_INTEGER_TEXT = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
_NUMBER_TEXT = re.compile(
    r"^[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|"
    r"\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
_REGEX_SENTINELS = frozenset(
    {"<unicode-space>", "<unicode-digit>", "<unicode-word>", "<unicode-other>"}
)
_REGEX_UNIVERSE = frozenset({chr(index) for index in range(128)}) | _REGEX_SENTINELS
_UNICODE_IGNORECASE_EQUIVALENTS = (
    frozenset({"I", "i", "İ", "ı"}),
    frozenset({"K", "k", "K"}),
    frozenset({"S", "s", "ſ"}),
)
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "$comment",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "enum",
        "const",
    }
)
_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_MISSING = object()


PathSegment = str | int
Comparator = Literal["exact", "casefold", "numeric"]


class JsonSchemaValidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: Any
    schema_: dict[str, Any] = Field(alias="schema")
    max_errors: int = Field(default=25, ge=1, le=MAX_VALIDATION_ERRORS)

    @field_validator("schema_")
    @classmethod
    def validate_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_bounded_json_schema(value)
        return value


class RegexExtractField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=64)
    pattern: str = Field(min_length=1, max_length=MAX_REGEX_PATTERN_CHARS)
    group: int | str = 1
    value_type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "json",
    ] = Field(default="string", alias="type")
    required: bool = True
    flags: list[Literal["ignorecase", "multiline", "ascii"]] = Field(
        default_factory=list,
        max_length=3,
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SAFE_NAME.fullmatch(value):
            raise ValueError("field name must be a safe identifier")
        return value

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        validate_safe_regex(value)
        return value

    @field_validator("group")
    @classmethod
    def validate_group(cls, value: int | str) -> int | str:
        if isinstance(value, bool):
            raise ValueError("regex group must be an integer or group name")
        if isinstance(value, int):
            if value < 0 or value > MAX_REGEX_GROUPS:
                raise ValueError(
                    f"regex group must be between 0 and {MAX_REGEX_GROUPS}"
                )
            return value
        if not _SAFE_GROUP_NAME.fullmatch(value):
            raise ValueError("regex group name must be a safe identifier")
        return value

    @field_validator("flags")
    @classmethod
    def validate_flags(
        cls,
        value: list[Literal["ignorecase", "multiline", "ascii"]],
    ) -> list[Literal["ignorecase", "multiline", "ascii"]]:
        if len(value) != len(set(value)):
            raise ValueError("regex flags cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_selected_group(self) -> RegexExtractField:
        flags = _regex_flags(self.flags)
        compiled = re.compile(self.pattern, flags)
        if compiled.groups > MAX_REGEX_GROUPS:
            raise ValueError(
                f"regex cannot contain more than {MAX_REGEX_GROUPS} capture groups"
            )
        if isinstance(self.group, int) and self.group > compiled.groups:
            raise ValueError(
                f"regex group {self.group} is not present in the pattern"
            )
        if isinstance(self.group, str) and self.group not in compiled.groupindex:
            raise ValueError(
                f"regex named group {self.group!r} is not present in the pattern"
            )
        return self


class RegexExtractConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: Any
    fields: list[RegexExtractField] = Field(
        min_length=1,
        max_length=MAX_REGEX_FIELDS,
    )

    @model_validator(mode="after")
    def validate_unique_fields(self) -> RegexExtractConfig:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("regex extraction field names must be unique")
        return self


class RecordDeduplicateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    records: Any
    key_paths: list[list[PathSegment]] = Field(
        min_length=1,
        max_length=MAX_KEY_PATHS,
    )
    missing_key_policy: Literal["error", "keep"] = "error"

    @field_validator("key_paths")
    @classmethod
    def validate_key_paths(
        cls,
        value: list[list[PathSegment]],
    ) -> list[list[PathSegment]]:
        _validate_paths(value, label="key paths")
        return value


class RecordCollectionNormalizeConfig(BaseModel):
    """Normalize common connector and tool response envelopes into object records."""

    model_config = ConfigDict(extra="forbid", strict=True)

    value: Any
    record_paths: list[list[PathSegment]] = Field(
        default_factory=lambda: [
            ["results"],
            ["items"],
            ["records"],
            ["data"],
        ],
        min_length=1,
        max_length=MAX_KEY_PATHS,
    )
    single_object_policy: Literal["wrap", "error"] = "wrap"
    empty_policy: Literal["allow", "error"] = "allow"

    @field_validator("record_paths")
    @classmethod
    def validate_record_paths(
        cls,
        value: list[list[PathSegment]],
    ) -> list[list[PathSegment]]:
        _validate_paths(value, label="record paths")
        return value


class MatchCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=64)
    source_path: list[PathSegment] = Field(
        min_length=1,
        max_length=MAX_PATH_DEPTH,
    )
    candidate_path: list[PathSegment] = Field(
        min_length=1,
        max_length=MAX_PATH_DEPTH,
    )
    comparator: Comparator = "exact"
    weight: float = Field(default=1.0, gt=0, le=100)
    required: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SAFE_NAME.fullmatch(value):
            raise ValueError("condition name must be a safe identifier")
        return value

    @field_validator("source_path", "candidate_path")
    @classmethod
    def validate_path(cls, value: list[PathSegment]) -> list[PathSegment]:
        _validate_paths([value], label="match path")
        return value

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("condition weight must be finite")
        return value


class ConflictCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=64)
    source_path: list[PathSegment] = Field(
        min_length=1,
        max_length=MAX_PATH_DEPTH,
    )
    candidate_path: list[PathSegment] = Field(
        min_length=1,
        max_length=MAX_PATH_DEPTH,
    )
    comparator: Comparator = "exact"
    allow_missing: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SAFE_NAME.fullmatch(value):
            raise ValueError("conflict check name must be a safe identifier")
        return value

    @field_validator("source_path", "candidate_path")
    @classmethod
    def validate_path(cls, value: list[PathSegment]) -> list[PathSegment]:
        _validate_paths([value], label="conflict path")
        return value


class RecordMatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Exactly one of source (single record) / sources (batch reconciliation).
    source: Any = None
    sources: Any = None
    consume_candidates: bool = True
    candidates: Any
    conditions: list[MatchCondition] = Field(
        min_length=1,
        max_length=MAX_MATCH_CONDITIONS,
    )
    conflict_checks: list[ConflictCheck] = Field(
        default_factory=list,
        max_length=MAX_MATCH_CONDITIONS,
    )
    min_score: float = Field(default=1.0, ge=0, le=1)
    ambiguity_threshold: float = Field(default=0.0, ge=0, le=1)
    result_limit: int = Field(default=20, ge=1, le=MAX_MATCH_RESULTS)

    @field_validator("min_score", "ambiguity_threshold")
    @classmethod
    def validate_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("match score boundaries must be finite")
        return value

    @model_validator(mode="after")
    def validate_unique_names(self) -> RecordMatchConfig:
        condition_names = [item.name for item in self.conditions]
        if len(condition_names) != len(set(condition_names)):
            raise ValueError("match condition names must be unique")
        conflict_names = [item.name for item in self.conflict_checks]
        if len(conflict_names) != len(set(conflict_names)):
            raise ValueError("conflict check names must be unique")
        if (self.source is None) == (self.sources is None):
            raise ValueError(
                "record_match requires exactly one of source (single record) "
                "or sources (batch list)"
            )
        return self


class ArtifactLineageSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_type: Literal[
        "workflow_input",
        "node_output",
        "connector_receipt",
        "external_resource",
        "generated",
    ]
    reference: str = Field(min_length=1, max_length=512)
    sha256: str | None = None

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError(
                "lineage sha256 must use the sha256:<64 lowercase hex> form"
            )
        return value


class TypedJsonArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: Any
    filename: str = Field(default="records.json", min_length=6, max_length=96)
    lineage: list[ArtifactLineageSource] | WorkflowValueReference = Field(
        default_factory=list
    )

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return _validate_json_filename(value)


def validate_bounded_json_schema(schema: dict[str, Any]) -> None:
    """Reject remote, executable, or unbounded JSON Schema features."""

    _validate_json_value(schema, label="schema")
    state = {"nodes": 0}

    def visit(current: Any, *, depth: int, path: str) -> None:
        if not isinstance(current, dict):
            raise TypeError(f"{path} must be a JSON Schema object")
        state["nodes"] += 1
        if state["nodes"] > MAX_SCHEMA_NODES:
            raise ValueError(
                f"schema cannot contain more than {MAX_SCHEMA_NODES} schema nodes"
            )
        if depth > MAX_SCHEMA_DEPTH:
            raise ValueError(
                f"schema cannot exceed {MAX_SCHEMA_DEPTH} nested schema levels"
            )
        unsupported = sorted(set(current) - _SUPPORTED_SCHEMA_KEYS)
        if unsupported:
            raise ValueError(f"{path} contains unsupported schema keywords: {unsupported}")
        for keyword in ("$schema", "$comment", "title", "description"):
            if keyword in current and not isinstance(current[keyword], str):
                raise TypeError(f"{path}.{keyword} must be a string")

        declared_types = current.get("type")
        if declared_types is not None:
            if isinstance(declared_types, str):
                values = [declared_types]
            elif isinstance(declared_types, list):
                values = declared_types
                if not values or any(not isinstance(item, str) for item in values):
                    raise TypeError(f"{path}.type must contain schema type strings")
                if len(values) != len(set(values)):
                    raise ValueError(f"{path}.type cannot contain duplicates")
            else:
                raise TypeError(f"{path}.type must be a schema type or array")
            unknown = sorted(set(values) - _SCHEMA_TYPES)
            if unknown:
                raise ValueError(f"{path}.type contains unsupported types: {unknown}")

        properties = current.get("properties")
        if properties is not None:
            if not isinstance(properties, dict):
                raise TypeError(f"{path}.properties must be an object")
            if len(properties) > MAX_SCHEMA_PROPERTIES:
                raise ValueError(
                    f"{path}.properties exceeds the {MAX_SCHEMA_PROPERTIES} property limit"
                )
            for name in sorted(properties):
                if not isinstance(name, str) or len(name) > 256:
                    raise ValueError(f"{path}.properties contains an invalid property name")
                visit(
                    properties[name],
                    depth=depth + 1,
                    path=f"{path}.properties[{name!r}]",
                )

        required = current.get("required")
        if required is not None:
            if (
                not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)
            ):
                raise TypeError(f"{path}.required must be an array of strings")
            if len(required) > MAX_SCHEMA_PROPERTIES:
                raise ValueError(f"{path}.required contains too many names")
            if len(required) != len(set(required)):
                raise ValueError(f"{path}.required cannot contain duplicates")
            if properties is not None and not set(required).issubset(properties):
                raise ValueError(
                    f"{path}.required must reference names declared in properties"
                )

        additional = current.get("additionalProperties")
        if additional is not None and not isinstance(additional, bool):
            raise TypeError(f"{path}.additionalProperties must be a boolean")

        items = current.get("items")
        if items is not None:
            visit(items, depth=depth + 1, path=f"{path}.items")

        _validate_nonnegative_integer_keyword(current, "minItems", path)
        _validate_nonnegative_integer_keyword(current, "maxItems", path)
        _validate_nonnegative_integer_keyword(current, "minLength", path)
        _validate_nonnegative_integer_keyword(current, "maxLength", path)
        if current.get("maxItems", 0) > MAX_CONTAINER_ITEMS:
            raise ValueError(
                f"{path}.maxItems exceeds the platform limit {MAX_CONTAINER_ITEMS}"
            )
        if current.get("maxLength", 0) > MAX_STRING_CHARS:
            raise ValueError(
                f"{path}.maxLength exceeds the platform limit {MAX_STRING_CHARS}"
            )
        _validate_min_max(current, "minItems", "maxItems", path)
        _validate_min_max(current, "minLength", "maxLength", path)
        if "uniqueItems" in current and not isinstance(current["uniqueItems"], bool):
            raise TypeError(f"{path}.uniqueItems must be a boolean")

        pattern = current.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise TypeError(f"{path}.pattern must be a string")
            validate_safe_regex(pattern)
            re.compile(pattern)

        for keyword in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        ):
            if keyword in current:
                if not _is_number(current[keyword]):
                    raise TypeError(f"{path}.{keyword} must be a JSON number")
                _decimal_number(current[keyword], label=f"{path}.{keyword}")
        _validate_numeric_bounds(current, path)

        if "enum" in current:
            enum = current["enum"]
            if not isinstance(enum, list) or not enum:
                raise TypeError(f"{path}.enum must be a non-empty array")
            if len(enum) > MAX_SCHEMA_ENUM_VALUES:
                raise ValueError(
                    f"{path}.enum exceeds the {MAX_SCHEMA_ENUM_VALUES} value limit"
                )
            canonical = [_schema_value_key(item) for item in enum]
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"{path}.enum cannot contain duplicate JSON values")

    visit(schema, depth=0, path="$")


def validate_json_value(
    value: Any,
    schema: dict[str, Any],
    *,
    max_errors: int = 25,
) -> dict[str, Any]:
    _validate_json_value(value, label="value")
    validate_bounded_json_schema(schema)
    pattern_results = _bounded_schema_pattern_results(value, schema)
    errors: list[dict[str, Any]] = []
    _validate_against_schema(
        value,
        schema,
        path=[],
        errors=errors,
        max_errors=max_errors,
        pattern_results=pattern_results,
    )
    return {
        "valid": not errors,
        "errors": errors,
        "value": value,
    }


def extract_regex_fields(
    text: Any,
    fields: list[RegexExtractField],
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise TypeError("regex_extract text must resolve to a string")
    if len(text) > MAX_REGEX_TEXT_CHARS:
        raise ValueError(
            f"regex_extract text exceeds the {MAX_REGEX_TEXT_CHARS} character limit"
        )
    try:
        text_bytes = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("regex_extract text contains invalid Unicode") from error
    if len(text_bytes) > MAX_REGEX_TEXT_CHARS * 4:
        raise ValueError("regex_extract text exceeds the bounded UTF-8 byte limit")

    matches, execution_error = _bounded_regex_matches(text, fields)
    if execution_error is not None:
        return {
            "fields": {field.name: None for field in fields},
            "confidence": 0.0,
            "missing": [field.name for field in fields if field.required],
            "errors": [
                {
                    "field": None,
                    "code": execution_error,
                    "message": (
                        "bounded regex execution did not complete within the "
                        "platform process boundary"
                    ),
                }
            ],
            "evidence": [
                {
                    "field": field.name,
                    "matched": False,
                    "required": field.required,
                    "pattern_sha256": _digest(field.pattern.encode("utf-8")),
                }
                for field in fields
            ],
        }

    extracted: dict[str, Any] = {}
    missing: list[str] = []
    errors: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    successful = 0
    for field, match in zip(fields, matches, strict=True):
        if match["status"] == "not_found":
            if field.required:
                missing.append(field.name)
            extracted[field.name] = None
            evidence.append(
                {
                    "field": field.name,
                    "matched": False,
                    "required": field.required,
                    "pattern_sha256": _digest(field.pattern.encode("utf-8")),
                }
            )
            continue
        if match["status"] == "error":
            errors.append(
                {
                    "field": field.name,
                    "code": "regex_execution_failed",
                    "message": "safe regex execution failed inside the process boundary",
                }
            )
            extracted[field.name] = None
            continue
        raw = match["raw"]
        span = match["span"]
        try:
            value = _coerce_extracted_value(raw, field.value_type)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append(
                {
                    "field": field.name,
                    "code": "type_coercion_failed",
                    "message": str(error),
                    "target_type": field.value_type,
                }
            )
            extracted[field.name] = None
            evidence.append(
                {
                    "field": field.name,
                    "matched": True,
                    "typed": False,
                    "required": field.required,
                    "span": span,
                    "raw_sha256": _digest(raw.encode("utf-8")),
                    "pattern_sha256": _digest(field.pattern.encode("utf-8")),
                }
            )
            continue
        successful += 1
        extracted[field.name] = value
        evidence.append(
            {
                "field": field.name,
                "matched": True,
                "typed": True,
                "required": field.required,
                "span": span,
                "raw_sha256": _digest(raw.encode("utf-8")),
                "pattern_sha256": _digest(field.pattern.encode("utf-8")),
            }
        )

    return {
        "fields": extracted,
        "confidence": round(successful / len(fields), 6),
        "missing": missing,
        "errors": errors,
        "evidence": evidence,
    }


def deduplicate_records(
    records: Any,
    key_paths: list[list[PathSegment]],
    *,
    missing_key_policy: Literal["error", "keep"] = "error",
) -> dict[str, Any]:
    validated = _validate_records(records, label="record_deduplicate records")
    _validate_paths(key_paths, label="key paths")
    unique: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    first_by_key: dict[bytes, int] = {}

    for index, record in enumerate(validated):
        values: list[Any] = []
        missing_paths: list[list[PathSegment]] = []
        for path in key_paths:
            value = _value_at_path(record, path)
            if value is _MISSING:
                missing_paths.append(path)
            else:
                values.append(value)
        if missing_paths and missing_key_policy == "error":
            raise ValueError(
                f"record {index} is missing configured key paths: {missing_paths}"
            )
        if missing_paths:
            key_payload = _canonical_json(
                {"missing_record_index": index, "paths": missing_paths}
            )
        else:
            key_payload = _canonical_json(values)
        key_digest = _digest(key_payload)
        first_index = first_by_key.get(key_payload)
        if first_index is None:
            first_by_key[key_payload] = index
            unique.append(record)
            receipts.append(
                {
                    "index": index,
                    "status": "unique",
                    "first_index": index,
                    "key_sha256": key_digest,
                    "missing_paths": missing_paths,
                }
            )
            continue
        duplicates.append(
            {
                "index": index,
                "first_index": first_index,
                "record": record,
                "key_sha256": key_digest,
            }
        )
        receipts.append(
            {
                "index": index,
                "status": "duplicate",
                "first_index": first_index,
                "key_sha256": key_digest,
                "missing_paths": missing_paths,
            }
        )

    return {
        "unique": unique,
        "duplicates": duplicates,
        "receipts": receipts,
    }


def normalize_record_collection(
    value: Any,
    record_paths: list[list[PathSegment]],
    *,
    single_object_policy: Literal["wrap", "error"] = "wrap",
    empty_policy: Literal["allow", "error"] = "allow",
) -> dict[str, Any]:
    """Return one stable object-array envelope from a bounded JSON response.

    Connectors commonly return either an array, a single object, or an object
    containing ``results``, ``items``, ``records``, or ``data``.  This
    primitive makes the envelope choice explicit and records which path was
    selected; it never contains provider-specific fields or mapping rules.
    """

    _validate_json_value(value, label="record_collection_normalize value")
    _validate_paths(record_paths, label="record paths")

    selected_path: list[PathSegment] = []
    source_shape: Literal["array", "envelope", "object"]
    candidate: Any
    if isinstance(value, list):
        source_shape = "array"
        candidate = value
    elif isinstance(value, dict):
        source_shape = "object"
        candidate = _MISSING
        for path in record_paths:
            resolved = _value_at_path(value, path)
            if resolved is not _MISSING:
                selected_path = list(path)
                candidate = resolved
                source_shape = "envelope"
                break
        if candidate is _MISSING:
            if single_object_policy == "error":
                raise ValueError(
                    "record collection object contains none of the configured "
                    f"record paths: {record_paths}"
                )
            candidate = [value]
    else:
        raise TypeError(
            "record_collection_normalize value must resolve to an array or object"
        )

    if isinstance(candidate, dict) and single_object_policy == "wrap":
        candidate = [candidate]
    records = _validate_records(
        candidate,
        label="record_collection_normalize records",
    )
    if not records and empty_policy == "error":
        raise ValueError("record collection cannot be empty")
    return {
        "records": records,
        "count": len(records),
        "empty": not records,
        "source_shape": source_shape,
        "selected_path": selected_path,
    }


def match_record(
    source: Any,
    candidates: Any,
    *,
    conditions: list[MatchCondition],
    conflict_checks: list[ConflictCheck],
    min_score: float,
    ambiguity_threshold: float,
    result_limit: int,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise TypeError("record_match source must resolve to an object")
    _validate_json_value(source, label="record_match source")
    validated_candidates = _validate_records(
        candidates,
        label="record_match candidates",
    )
    total_weight = sum(Decimal(str(item.weight)) for item in conditions)
    evaluated: list[dict[str, Any]] = []

    for index, candidate in enumerate(validated_candidates):
        score = Decimal(0)
        disqualified = False
        condition_evidence: list[dict[str, Any]] = []
        for condition in conditions:
            source_value = _value_at_path(source, condition.source_path)
            candidate_value = _value_at_path(candidate, condition.candidate_path)
            source_missing = source_value is _MISSING
            candidate_missing = candidate_value is _MISSING
            if condition.required and (source_missing or candidate_missing):
                disqualified = True
            matched = (
                False
                if source_missing or candidate_missing
                else _compare_values(
                    source_value,
                    candidate_value,
                    condition.comparator,
                )
            )
            if matched:
                score += Decimal(str(condition.weight))
            elif condition.required:
                disqualified = True
            condition_evidence.append(
                {
                    "name": condition.name,
                    "comparator": condition.comparator,
                    "matched": matched,
                    "required": condition.required,
                    "source_missing": source_missing,
                    "candidate_missing": candidate_missing,
                    "weight": condition.weight,
                }
            )

        conflicts: list[dict[str, Any]] = []
        for check in conflict_checks:
            source_value = _value_at_path(source, check.source_path)
            candidate_value = _value_at_path(candidate, check.candidate_path)
            source_missing = source_value is _MISSING
            candidate_missing = candidate_value is _MISSING
            if source_missing or candidate_missing:
                matches = check.allow_missing
                reason = "allowed_missing" if matches else "missing"
            else:
                matches = _compare_values(
                    source_value,
                    candidate_value,
                    check.comparator,
                )
                reason = "matched" if matches else "mismatch"
            if not matches:
                # 冲突要带上**两边的值和差额**，不能只报"哪一项不一致"。
                # 对账类需求普遍要求"列出是哪个字段不一致、差多少"（工业任务包
                # T1/T5 的验收原文），只给一个 name 时这句话在积木层无法实现——
                # 这是"任务在积木层是否可解"的效度问题，不是模型能力问题。
                detail: dict[str, Any] = {
                    "name": check.name,
                    "comparator": check.comparator,
                    "reason": reason,
                    "source_missing": source_missing,
                    "candidate_missing": candidate_missing,
                    "source_value": None if source_missing else source_value,
                    "candidate_value": None if candidate_missing else candidate_value,
                }
                if (
                    not source_missing
                    and not candidate_missing
                    and isinstance(source_value, (int, float))
                    and isinstance(candidate_value, (int, float))
                    and not isinstance(source_value, bool)
                    and not isinstance(candidate_value, bool)
                ):
                    detail["delta"] = _score_float(
                        Decimal(str(source_value)) - Decimal(str(candidate_value))
                    )
                conflicts.append(detail)

        normalized = score / total_weight
        evaluated.append(
            {
                "index": index,
                "candidate": candidate,
                "score": _score_float(normalized),
                "_score": normalized,
                "disqualified": disqualified,
                "conflicts": conflicts,
                "conditions": condition_evidence,
            }
        )

    evaluated.sort(key=lambda item: (-item["_score"], item["index"]))
    minimum = Decimal(str(min_score))
    ambiguity = Decimal(str(ambiguity_threshold))
    qualified = [
        item
        for item in evaluated
        if not item["disqualified"] and item["_score"] >= minimum
    ]
    clean = [item for item in qualified if not item["conflicts"]]
    conflicting = [item for item in qualified if item["conflicts"]]
    status: Literal["matched", "not_found", "ambiguous", "conflict"]
    selected: dict[str, Any] | None = None

    if not qualified:
        status = "not_found"
    elif not clean:
        status = "conflict"
    else:
        best = clean[0]
        near_conflict = any(
            item["_score"] >= best["_score"] - ambiguity
            for item in conflicting
        )
        if near_conflict:
            status = "conflict"
        elif (
            len(clean) > 1
            and clean[1]["_score"] >= best["_score"] - ambiguity
        ):
            status = "ambiguous"
        else:
            status = "matched"
            selected = {
                "index": best["index"],
                "candidate": best["candidate"],
                "score": best["score"],
            }

    public_candidates = [
        {key: value for key, value in item.items() if key != "_score"}
        for item in evaluated[:result_limit]
    ]
    return {
        "status": status,
        "match": selected,
        "candidates": public_candidates,
        "evidence": {
            "evaluated_count": len(evaluated),
            "returned_count": min(len(evaluated), result_limit),
            "truncated": len(evaluated) > result_limit,
            "total_weight": float(total_weight),
            "min_score": min_score,
            "ambiguity_threshold": ambiguity_threshold,
            "qualified_count": len(qualified),
            "clean_count": len(clean),
            "conflicting_count": len(conflicting),
        },
    }


MAX_BATCH_COMPARISONS = 250_000


def match_records(
    sources: Any,
    candidates: Any,
    *,
    conditions: list[MatchCondition],
    conflict_checks: list[ConflictCheck],
    min_score: float,
    ambiguity_threshold: float,
    result_limit: int,
    consume_candidates: bool = True,
) -> dict[str, Any]:
    """Batch reconciliation: match every source against a shared candidate pool.

    With ``consume_candidates`` (default) each candidate is matched at most
    once — the one-to-one shape of 对账. Sources are processed in input order,
    so results are deterministic for identical inputs.
    """

    validated_sources = _validate_records(sources, label="record_match sources")
    validated_candidates = _validate_records(
        candidates,
        label="record_match candidates",
    )
    if len(validated_sources) * len(validated_candidates) > MAX_BATCH_COMPARISONS:
        raise ValueError(
            "record_match batch is too large: "
            f"{len(validated_sources)}×{len(validated_candidates)} comparisons "
            f"exceed {MAX_BATCH_COMPARISONS}"
        )

    consumed: set[int] = set()
    results: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    unmatched_sources: list[dict[str, Any]] = []
    ambiguous_sources: list[dict[str, Any]] = []
    conflict_sources: list[dict[str, Any]] = []

    for source_index, source in enumerate(validated_sources):
        pool_indexes = [
            index
            for index in range(len(validated_candidates))
            if index not in consumed
        ]
        pool = [validated_candidates[index] for index in pool_indexes]
        outcome = match_record(
            source,
            pool,
            conditions=conditions,
            conflict_checks=conflict_checks,
            min_score=min_score,
            ambiguity_threshold=ambiguity_threshold,
            result_limit=result_limit,
        )
        selected = outcome["match"]
        if selected is not None:
            original_index = pool_indexes[selected["index"]]
            selected = {**selected, "index": original_index}
            if consume_candidates:
                consumed.add(original_index)
        entry = {
            "source_index": source_index,
            "source": source,
            "status": outcome["status"],
            "match": selected,
        }
        results.append(entry)
        if outcome["status"] == "matched":
            matched.append({
                "source_index": source_index,
                "source": source,
                "candidate_index": selected["index"],
                "candidate": selected["candidate"],
                "score": selected["score"],
            })
        elif outcome["status"] == "ambiguous":
            ambiguous_sources.append(entry)
        elif outcome["status"] == "conflict":
            # 把冲突的那个候选带上：批量模式此前只回 {source, status, match:null}，
            # 下游拿不到被比对的那条记录，"差多少"无从谈起。
            conflicting = next(
                (
                    item for item in outcome["candidates"]
                    if item.get("conflicts") and not item.get("disqualified")
                ),
                None,
            )
            if conflicting is not None:
                pool_position = conflicting["index"]
                entry["conflict_with"] = {
                    "candidate_index": pool_indexes[pool_position],
                    "candidate": conflicting["candidate"],
                    "score": conflicting["score"],
                    "conflicts": conflicting["conflicts"],
                }
            conflict_sources.append(entry)
        else:
            unmatched_sources.append(entry)

    matched_candidate_indexes = {item["candidate_index"] for item in matched}
    unmatched_candidates = [
        {"index": index, "candidate": candidate}
        for index, candidate in enumerate(validated_candidates)
        if index not in matched_candidate_indexes
    ]
    return {
        "results": results,
        "matched": matched,
        "unmatched_sources": unmatched_sources,
        "ambiguous_sources": ambiguous_sources,
        "conflict_sources": conflict_sources,
        "unmatched_candidates": unmatched_candidates,
        "summary": {
            "total_sources": len(validated_sources),
            "total_candidates": len(validated_candidates),
            "matched": len(matched),
            "unmatched_sources": len(unmatched_sources),
            "ambiguous": len(ambiguous_sources),
            "conflicts": len(conflict_sources),
            "unmatched_candidates": len(unmatched_candidates),
        },
    }


def write_typed_json_artifact(
    *,
    workspace: Path,
    value: Any,
    filename: str,
    lineage: Any,
    run_id: str,
    node_id: str,
    application_id: str,
) -> dict[str, Any]:
    """Persist one bounded canonical JSON value under the run artifact boundary."""

    _validate_json_value(value, label="JSON artifact value")
    safe_filename = _validate_json_filename(filename)
    payload = _canonical_json(value) + b"\n"
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError(
            f"JSON artifact exceeds the {MAX_JSON_BYTES} byte limit"
        )
    sources = _validate_lineage(lineage)
    root = _safe_workspace(workspace)
    _safe_artifact_directory(root)
    replayed = _write_json_once(root, safe_filename, payload)
    digest = _digest(payload)
    return {
        "relative_path": f"artifacts/{safe_filename}",
        "filename": safe_filename,
        "media_type": JSON_MEDIA_TYPE,
        "size_bytes": len(payload),
        "sha256": digest,
        "lineage": {
            "generator": {
                "block_type": "typed_json_artifact",
                "block_version": 1,
            },
            "application_id": application_id,
            "run_id": run_id,
            "node_id": node_id,
            "value_sha256": _digest(_canonical_json(value)),
            "sources": [source.model_dump(mode="json") for source in sources],
        },
        "replayed": replayed,
    }


def validate_safe_regex(pattern: str) -> None:
    """Accept a conservative regular-language subset with bounded backtracking."""

    if len(pattern) > MAX_REGEX_PATTERN_CHARS:
        raise ValueError(
            f"regex pattern exceeds the {MAX_REGEX_PATTERN_CHARS} character limit"
        )
    try:
        encoded = pattern.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("regex pattern contains invalid Unicode") from error
    if len(encoded) > MAX_REGEX_PATTERN_CHARS * 4:
        raise ValueError("regex pattern exceeds the bounded UTF-8 byte limit")

    in_class = False
    escaped = False
    group_stack: list[int] = []
    closed_group_at: set[int] = set()
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if escaped:
            if character.isdigit():
                raise ValueError("regex backreferences are not supported")
            if character in {"N", "U", "u", "x"}:
                raise ValueError(
                    "regex hexadecimal and named Unicode escapes are not supported"
                )
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if in_class:
            if character == "]":
                in_class = False
            index += 1
            continue
        if character == "[":
            content_start = index + 2 if pattern.startswith("[^", index) else index + 1
            if content_start < len(pattern) and pattern[content_start] == "]":
                raise ValueError(
                    "regex classes with a leading literal closing bracket are "
                    "not supported"
                )
            in_class = True
            index += 1
            continue
        if character == "|":
            index += 1
            continue
        if character == "(":
            if pattern.startswith("(?:", index):
                group_stack.append(index)
                index += 3
                continue
            if pattern.startswith("(?P<", index):
                end = pattern.find(">", index + 4)
                if end < 0 or not _SAFE_GROUP_NAME.fullmatch(
                    pattern[index + 4 : end]
                ):
                    raise ValueError("regex named capture group is invalid")
                group_stack.append(index)
                index = end + 1
                continue
            if pattern.startswith("(?", index):
                raise ValueError(
                    "regex lookarounds, inline flags, and special groups are not supported"
                )
            group_stack.append(index)
            index += 1
            continue
        if character == ")":
            if not group_stack:
                raise ValueError("regex contains an unmatched closing parenthesis")
            group_stack.pop()
            closed_group_at.add(index)
            index += 1
            continue
        if character in {"*", "+", "?"} or character == "{":
            previous = index - 1
            if previous < 0:
                raise ValueError("regex quantifier has no preceding atom")
            if previous in closed_group_at:
                raise ValueError("quantified regex groups are not supported")
            if pattern[previous] == "." and character in {"*", "+"}:
                raise ValueError("unbounded wildcard regex quantifiers are not supported")
            if character == "{":
                end = pattern.find("}", index + 1)
                if end < 0:
                    raise ValueError("regex bounded quantifier is not closed")
                body = pattern[index + 1 : end]
                parts = body.split(",")
                if len(parts) > 2 or any(
                    part and not part.isdigit() for part in parts
                ):
                    raise ValueError("regex bounded quantifier is invalid")
                if not parts[0] or (len(parts) == 2 and not parts[1]):
                    raise ValueError("open-ended regex quantifiers are not supported")
                lower = int(parts[0])
                upper = int(parts[-1])
                if lower > upper or upper > 1_000:
                    raise ValueError(
                        "regex bounded quantifier must have an upper bound at most 1000"
                    )
                index = end + 1
                continue
        index += 1
    if escaped:
        raise ValueError("regex cannot end with an incomplete escape")
    if in_class:
        raise ValueError("regex character class is not closed")
    if group_stack:
        raise ValueError("regex capture group is not closed")
    _reject_overlapping_variable_repeats(pattern)
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"invalid regex pattern: {error}") from error
    if compiled.groups > MAX_REGEX_GROUPS:
        raise ValueError(
            f"regex cannot contain more than {MAX_REGEX_GROUPS} capture groups"
        )


def _validate_against_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    path: list[PathSegment],
    errors: list[dict[str, Any]],
    max_errors: int,
    pattern_results: dict[tuple[PathSegment, ...], bool],
) -> None:
    if len(errors) >= max_errors:
        return

    if "enum" in schema and _schema_value_key(value) not in {
        _schema_value_key(item) for item in schema["enum"]
    }:
        _add_validation_error(
            errors,
            path,
            "enum",
            "value is not one of the allowed JSON values",
            max_errors,
        )
    if "const" in schema and _schema_value_key(value) != _schema_value_key(
        schema["const"]
    ):
        _add_validation_error(
            errors,
            path,
            "const",
            "value does not equal the required JSON value",
            max_errors,
        )

    declared = schema.get("type")
    if declared is not None:
        allowed = [declared] if isinstance(declared, str) else declared
        if not any(_matches_schema_type(value, item) for item in allowed):
            _add_validation_error(
                errors,
                path,
                "type",
                f"expected {' or '.join(allowed)}, got {_json_type(value)}",
                max_errors,
            )
            return

    if isinstance(value, dict):
        required = sorted(schema.get("required", []))
        for name in required:
            if name not in value:
                _add_validation_error(
                    errors,
                    [*path, name],
                    "required",
                    "required property is missing",
                    max_errors,
                )
        properties = schema.get("properties", {})
        for name in sorted(set(value) & set(properties)):
            _validate_against_schema(
                value[name],
                properties[name],
                path=[*path, name],
                errors=errors,
                max_errors=max_errors,
                pattern_results=pattern_results,
            )
        if schema.get("additionalProperties") is False:
            for name in sorted(set(value) - set(properties)):
                _add_validation_error(
                    errors,
                    [*path, name],
                    "additionalProperties",
                    "undeclared property is not allowed",
                    max_errors,
                )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            _add_validation_error(
                errors,
                path,
                "minItems",
                f"array contains fewer than {schema['minItems']} items",
                max_errors,
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _add_validation_error(
                errors,
                path,
                "maxItems",
                f"array contains more than {schema['maxItems']} items",
                max_errors,
            )
        if schema.get("uniqueItems"):
            seen: set[bytes] = set()
            for index, item in enumerate(value):
                canonical = _schema_value_key(item)
                if canonical in seen:
                    _add_validation_error(
                        errors,
                        [*path, index],
                        "uniqueItems",
                        "array item duplicates an earlier JSON value",
                        max_errors,
                    )
                seen.add(canonical)
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_against_schema(
                    item,
                    schema["items"],
                    path=[*path, index],
                    errors=errors,
                    max_errors=max_errors,
                    pattern_results=pattern_results,
                )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            _add_validation_error(
                errors,
                path,
                "minLength",
                f"string is shorter than {schema['minLength']} characters",
                max_errors,
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _add_validation_error(
                errors,
                path,
                "maxLength",
                f"string is longer than {schema['maxLength']} characters",
                max_errors,
            )
        if "pattern" in schema and not pattern_results[tuple(path)]:
            _add_validation_error(
                errors,
                path,
                "pattern",
                "string does not match the configured safe regex",
                max_errors,
            )

    if _is_number(value):
        numeric = Decimal(str(value))
        comparisons = (
            ("minimum", numeric < Decimal(str(schema.get("minimum", numeric)))),
            ("maximum", numeric > Decimal(str(schema.get("maximum", numeric)))),
            (
                "exclusiveMinimum",
                numeric <= Decimal(str(schema.get("exclusiveMinimum", numeric - 1))),
            ),
            (
                "exclusiveMaximum",
                numeric >= Decimal(str(schema.get("exclusiveMaximum", numeric + 1))),
            ),
        )
        for keyword, failed in comparisons:
            if keyword in schema and failed:
                _add_validation_error(
                    errors,
                    path,
                    keyword,
                    f"number violates {keyword}",
                    max_errors,
                )


def _add_validation_error(
    errors: list[dict[str, Any]],
    path: list[PathSegment],
    keyword: str,
    message: str,
    max_errors: int,
) -> None:
    if len(errors) >= max_errors:
        return
    errors.append(
        {
            "path": path,
            "keyword": keyword,
            "message": message,
        }
    )


def _matches_schema_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        )
    if expected == "number":
        return _is_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _validate_json_value(value: Any, *, label: str) -> None:
    nodes = 0
    stack: list[tuple[Any, int, str]] = [(value, 0, "$")]
    while stack:
        current, depth, path = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError(
                f"{label} cannot contain more than {MAX_JSON_NODES} JSON nodes"
            )
        if depth > MAX_JSON_DEPTH:
            raise ValueError(
                f"{label} cannot exceed {MAX_JSON_DEPTH} nested JSON levels"
            )
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, int):
            try:
                digits = len(str(abs(current)))
            except ValueError as error:
                raise ValueError(
                    f"{label} integer at {path} exceeds the conversion limit"
                ) from error
            if digits > 100:
                raise ValueError(f"{label} integer at {path} exceeds 100 digits")
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label} number at {path} must be finite")
            continue
        if isinstance(current, str):
            if len(current) > MAX_STRING_CHARS:
                raise ValueError(
                    f"{label} string at {path} exceeds {MAX_STRING_CHARS} characters"
                )
            try:
                current.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    f"{label} string at {path} contains invalid Unicode"
                ) from error
            continue
        if isinstance(current, list):
            if len(current) > MAX_CONTAINER_ITEMS:
                raise ValueError(
                    f"{label} array at {path} exceeds {MAX_CONTAINER_ITEMS} items"
                )
            stack.extend(
                (item, depth + 1, f"{path}[{index}]")
                for index, item in reversed(list(enumerate(current)))
            )
            continue
        if isinstance(current, dict):
            if len(current) > MAX_CONTAINER_ITEMS:
                raise ValueError(
                    f"{label} object at {path} exceeds {MAX_CONTAINER_ITEMS} properties"
                )
            for key in current:
                if not isinstance(key, str):
                    raise TypeError(f"{label} object at {path} has a non-string key")
                if len(key) > 256:
                    raise ValueError(
                        f"{label} object key at {path} exceeds 256 characters"
                    )
            stack.extend(
                (current[key], depth + 1, f"{path}.{key}")
                for key in reversed(sorted(current))
            )
            continue
        raise TypeError(
            f"{label} value at {path} is not JSON-compatible: "
            f"{type(current).__name__}"
        )
    payload = _canonical_json(value)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_JSON_BYTES} byte limit")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise TypeError("value is not canonical JSON") from error


def _schema_value_key(value: Any) -> bytes:
    """Return a JSON-Schema equality key where mathematically equal numbers match."""

    def normalize(current: Any) -> Any:
        if current is None:
            return ["null"]
        if isinstance(current, bool):
            return ["boolean", current]
        if _is_number(current):
            numeric = Decimal(str(current))
            if numeric == 0:
                text = "0"
            else:
                sign, raw_digits, exponent = numeric.as_tuple()
                digits = list(raw_digits)
                while len(digits) > 1 and digits[-1] == 0:
                    digits.pop()
                    exponent += 1
                text = f"{sign}:{''.join(str(item) for item in digits)}:{exponent}"
            return ["number", text]
        if isinstance(current, str):
            return ["string", current]
        if isinstance(current, list):
            return ["array", [normalize(item) for item in current]]
        if isinstance(current, dict):
            return [
                "object",
                [[key, normalize(current[key])] for key in sorted(current)],
            ]
        raise TypeError(
            f"value is not JSON-compatible: {type(current).__name__}"
        )

    return _canonical_json(normalize(value))


def _coerce_extracted_value(raw: str, value_type: str) -> Any:
    if len(raw) > MAX_STRING_CHARS:
        raise ValueError("captured value exceeds the bounded string limit")
    if value_type == "string":
        return raw
    stripped = raw.strip()
    if value_type == "integer":
        if len(stripped) > 100 or not _INTEGER_TEXT.fullmatch(stripped):
            raise ValueError("captured value is not a bounded base-10 integer")
        return int(stripped)
    if value_type == "number":
        if len(stripped) > 128 or not _NUMBER_TEXT.fullmatch(stripped):
            raise ValueError("captured value is not a bounded JSON number")
        number = _decimal_number(stripped, label="captured value")
        result = float(number)
        if not math.isfinite(result):
            raise ValueError("captured value is outside the finite number range")
        return result
    if value_type == "boolean":
        normalized = stripped.casefold()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
        raise ValueError("captured value is not a supported boolean literal")
    if value_type == "date":
        try:
            return date.fromisoformat(stripped).isoformat()
        except ValueError as error:
            raise ValueError("captured value is not an ISO 8601 date") from error
    if value_type == "datetime":
        raw_datetime = (
            f"{stripped[:-1]}+00:00"
            if stripped.endswith(("Z", "z"))
            else stripped
        )
        try:
            parsed = datetime.fromisoformat(raw_datetime)
        except ValueError as error:
            raise ValueError(
                "captured value is not an ISO 8601 datetime"
            ) from error
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()
    if value_type == "json":
        parsed = json.loads(stripped)
        _validate_json_value(parsed, label="captured JSON")
        return parsed
    raise ValueError(f"unsupported extraction type: {value_type}")


def _regex_flags(flags: list[str]) -> re.RegexFlag:
    value = re.NOFLAG
    mapping = {
        "ignorecase": re.IGNORECASE,
        "multiline": re.MULTILINE,
        "ascii": re.ASCII,
    }
    for flag in flags:
        value |= mapping[flag]
    return value


def _bounded_regex_matches(
    text: str,
    fields: list[RegexExtractField],
) -> tuple[list[dict[str, Any]], str | None]:
    payload = [
        {
            "text": text,
            "pattern": field.pattern,
            "flags": list(field.flags),
            "group": field.group,
            "capture": True,
        }
        for field in fields
    ]
    return _bounded_regex_requests(payload)


def _bounded_schema_pattern_results(
    value: Any,
    schema: dict[str, Any],
) -> dict[tuple[PathSegment, ...], bool]:
    checks: list[tuple[list[PathSegment], str, str]] = []

    def collect(
        current: Any,
        current_schema: dict[str, Any],
        path: list[PathSegment],
    ) -> None:
        if isinstance(current, str) and "pattern" in current_schema:
            checks.append((path, current_schema["pattern"], current))
            if len(checks) > MAX_SCHEMA_PATTERN_CHECKS:
                raise ValueError(
                    "JSON Schema validation exceeds the "
                    f"{MAX_SCHEMA_PATTERN_CHECKS} pattern-check limit"
                )
        if isinstance(current, dict):
            properties = current_schema.get("properties", {})
            for name in sorted(set(current) & set(properties)):
                collect(current[name], properties[name], [*path, name])
        if isinstance(current, list) and "items" in current_schema:
            for index, item in enumerate(current):
                collect(item, current_schema["items"], [*path, index])

    collect(value, schema, [])
    if not checks:
        return {}
    requests = [
        {
            "text": text,
            "pattern": pattern,
            "flags": [],
            "group": 0,
            "capture": False,
        }
        for _path, pattern, text in checks
    ]
    matches, execution_error = _bounded_regex_requests(requests)
    if execution_error is not None:
        raise RuntimeError(
            "bounded JSON Schema pattern execution failed closed: "
            f"{execution_error}"
        )
    results: dict[tuple[PathSegment, ...], bool] = {}
    for (path, _pattern, _text), match in zip(checks, matches, strict=True):
        if match["status"] == "error":
            raise RuntimeError(
                "bounded JSON Schema pattern execution failed closed"
            )
        results[tuple(path)] = match["status"] == "matched"
    return results


def _bounded_regex_requests(
    payload: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_regex_match_worker,
        args=(sender, payload),
        daemon=True,
    )
    started = False
    try:
        try:
            process.start()
            started = True
        except (OSError, RuntimeError):
            return [], "regex_execution_unavailable"
        finally:
            sender.close()
        if not receiver.poll(REGEX_EXECUTION_TIMEOUT_SECONDS):
            return [], "regex_execution_timeout"
        try:
            result = receiver.recv()
        except (EOFError, OSError):
            return [], "regex_execution_failed"
        if (
            not isinstance(result, list)
            or len(result) != len(payload)
            or any(not isinstance(item, dict) for item in result)
        ):
            return [], "regex_execution_failed"
        return result, None
    finally:
        receiver.close()
        if started:
            if process.is_alive():
                process.kill()
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)


def _regex_match_worker(
    connection: Any,
    fields: list[dict[str, Any]],
) -> None:
    result: list[dict[str, Any]] = []
    try:
        for field in fields:
            try:
                compiled = re.compile(
                    field["pattern"],
                    _regex_flags(field["flags"]),
                )
                matched = compiled.search(field["text"])
                if matched is None:
                    result.append({"status": "not_found"})
                    continue
                if not field["capture"]:
                    result.append({"status": "matched"})
                    continue
                group = field["group"]
                result.append(
                    {
                        "status": "matched",
                        "raw": matched.group(group),
                        "span": [
                            matched.start(group),
                            matched.end(group),
                        ],
                    }
                )
            except (IndexError, KeyError, TypeError, ValueError, re.error):
                result.append({"status": "error"})
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


def _validate_records(records: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise TypeError(f"{label} must resolve to an array")
    if len(records) > MAX_RECORDS:
        raise ValueError(f"{label} exceeds the {MAX_RECORDS} record limit")
    _validate_json_value(records, label=label)
    if any(not isinstance(record, dict) for record in records):
        raise TypeError(f"{label} must contain only objects")
    return records


def _validate_paths(paths: list[list[PathSegment]], *, label: str) -> None:
    seen: set[bytes] = set()
    for path in paths:
        if not path or len(path) > MAX_PATH_DEPTH:
            raise ValueError(
                f"{label} must contain paths of 1 to {MAX_PATH_DEPTH} segments"
            )
        for segment in path:
            if isinstance(segment, bool) or not isinstance(segment, (str, int)):
                raise TypeError(f"{label} path segments must be strings or integers")
            if isinstance(segment, str) and (
                not segment or len(segment) > 128
            ):
                raise ValueError(
                    f"{label} string path segments must contain 1 to 128 characters"
                )
            if isinstance(segment, int) and (
                segment < 0 or segment >= MAX_CONTAINER_ITEMS
            ):
                raise ValueError(
                    f"{label} integer path segments must be between 0 and "
                    f"{MAX_CONTAINER_ITEMS - 1}"
                )
        canonical = _canonical_json(path)
        if canonical in seen:
            raise ValueError(f"{label} cannot contain duplicate paths")
        seen.add(canonical)


def _value_at_path(value: Any, path: list[PathSegment]) -> Any:
    current = value
    for segment in path:
        if isinstance(current, dict) and isinstance(segment, str):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list) and isinstance(segment, int):
            if segment >= len(current):
                return _MISSING
            current = current[segment]
        else:
            return _MISSING
    return current


def _compare_values(left: Any, right: Any, comparator: Comparator) -> bool:
    if comparator == "exact":
        return _json_type(left) == _json_type(right) and _canonical_json(
            left
        ) == _canonical_json(right)
    if comparator == "casefold":
        return (
            isinstance(left, str)
            and isinstance(right, str)
            and left.casefold() == right.casefold()
        )
    if comparator == "numeric":
        try:
            return _match_decimal(left) == _match_decimal(right)
        except (TypeError, ValueError, InvalidOperation):
            return False
    raise ValueError(f"unsupported comparator: {comparator}")


def _match_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("booleans are not numeric match values")
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numeric match value must be finite")
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise TypeError("numeric match value must be a number or numeric string")
    if len(raw) > 128 or not _NUMBER_TEXT.fullmatch(raw):
        raise ValueError("numeric match value is not a bounded JSON number")
    return _decimal_number(raw, label="numeric match value")


def _decimal_number(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a number")
    if not isinstance(value, (int, float, str, Decimal)):
        raise TypeError(f"{label} must be a number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite number")
    return result


def _score_float(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001")))


def _validate_nonnegative_integer_keyword(
    schema: dict[str, Any],
    keyword: str,
    path: str,
) -> None:
    if keyword not in schema:
        return
    value = schema[keyword]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{path}.{keyword} must be a non-negative integer")


def _validate_min_max(
    schema: dict[str, Any],
    minimum: str,
    maximum: str,
    path: str,
) -> None:
    if (
        minimum in schema
        and maximum in schema
        and schema[minimum] > schema[maximum]
    ):
        raise ValueError(f"{path}.{minimum} cannot exceed {maximum}")


def _validate_numeric_bounds(schema: dict[str, Any], path: str) -> None:
    lower: list[tuple[str, Decimal, bool]] = []
    upper: list[tuple[str, Decimal, bool]] = []
    if "minimum" in schema:
        lower.append(("minimum", Decimal(str(schema["minimum"])), False))
    if "exclusiveMinimum" in schema:
        lower.append(
            (
                "exclusiveMinimum",
                Decimal(str(schema["exclusiveMinimum"])),
                True,
            )
        )
    if "maximum" in schema:
        upper.append(("maximum", Decimal(str(schema["maximum"])), False))
    if "exclusiveMaximum" in schema:
        upper.append(
            (
                "exclusiveMaximum",
                Decimal(str(schema["exclusiveMaximum"])),
                True,
            )
        )
    for lower_name, lower_value, lower_exclusive in lower:
        for upper_name, upper_value, upper_exclusive in upper:
            if lower_value > upper_value or (
                lower_value == upper_value and (lower_exclusive or upper_exclusive)
            ):
                raise ValueError(
                    f"{path}.{lower_name} conflicts with {upper_name}"
                )


def _validate_lineage(value: Any) -> list[ArtifactLineageSource]:
    if not isinstance(value, list):
        raise TypeError("lineage must resolve to an array")
    if len(value) > 100:
        raise ValueError("lineage cannot contain more than 100 sources")
    return [ArtifactLineageSource.model_validate(item) for item in value]


def _write_json_once(workspace: Path, filename: str, payload: bytes) -> bool:
    """Create through held directory descriptors so path components cannot be swapped."""

    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC

    workspace_descriptor = os.open(workspace, directory_flags)
    artifact_descriptor: int | None = None
    try:
        workspace_status = os.fstat(workspace_descriptor)
        if not stat.S_ISDIR(workspace_status.st_mode):
            raise ValueError("workflow workspace descriptor is not a directory")
        artifact_descriptor = os.open(
            "artifacts",
            directory_flags,
            dir_fd=workspace_descriptor,
        )
        artifact_status = os.fstat(artifact_descriptor)
        if not stat.S_ISDIR(artifact_status.st_mode):
            raise ValueError("artifact descriptor is not a directory")
        return _write_json_target_once(
            artifact_descriptor,
            filename,
            payload,
        )
    finally:
        if artifact_descriptor is not None:
            os.close(artifact_descriptor)
        os.close(workspace_descriptor)


def _write_json_target_once(
    directory_descriptor: int,
    filename: str,
    payload: bytes,
) -> bool:
    create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        create_flags |= os.O_NOFOLLOW
        read_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        create_flags |= os.O_CLOEXEC
        read_flags |= os.O_CLOEXEC

    try:
        target_descriptor = os.open(
            filename,
            create_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError:
        existing_descriptor = os.open(
            filename,
            read_flags,
            dir_fd=directory_descriptor,
        )
        try:
            status = os.fstat(existing_descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("artifact target is not a regular file")
            if stat.S_IMODE(status.st_mode) != 0o600:
                raise ValueError("artifact target does not have private file mode")
            if _descriptor_matches(existing_descriptor, payload):
                return True
        finally:
            os.close(existing_descriptor)
        raise FileExistsError(
            "artifact target already exists with different content"
        ) from None

    try:
        status = os.fstat(target_descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("artifact target descriptor is not a regular file")
        os.fchmod(target_descriptor, 0o600)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(target_descriptor, view[written:])
            if count <= 0:
                raise OSError("artifact target write made no progress")
            written += count
        os.fsync(target_descriptor)
        os.fsync(directory_descriptor)
        return False
    except Exception:
        try:
            os.unlink(filename, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(target_descriptor)


def _descriptor_matches(descriptor: int, payload: bytes) -> bool:
    status = os.fstat(descriptor)
    if status.st_size != len(payload):
        return False
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest() == hashlib.sha256(payload).hexdigest()


def _reject_overlapping_variable_repeats(pattern: str) -> None:
    """Reject repeat chains whose accepted characters can be repartitioned."""

    atoms: list[tuple[frozenset[str], int, int | None]] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            charset, is_zero_width = _escaped_regex_charset(pattern[index + 1])
            if not is_zero_width:
                atoms.append((charset, 1, 1))
            index += 2
            continue
        if character == "[":
            end = _regex_class_end(pattern, index)
            atoms.append((_regex_class_charset(pattern[index + 1 : end]), 1, 1))
            index = end + 1
            continue
        if character == "(":
            if pattern.startswith("(?:", index):
                index += 3
            elif pattern.startswith("(?P<", index):
                index = pattern.find(">", index + 4) + 1
            else:
                index += 1
            continue
        if character == "|":
            # Alternation branches are independent; a repeat chain never
            # spans them.
            atoms.clear()
            index += 1
            continue
        if character in {")", "^", "$"}:
            index += 1
            continue
        if character in {"*", "+", "?"} or character == "{":
            if not atoms:
                raise ValueError("regex quantifier has no preceding atom")
            charset, _, _ = atoms[-1]
            if character == "*":
                bounds = (0, None)
                index += 1
            elif character == "+":
                bounds = (1, None)
                index += 1
            elif character == "?":
                bounds = (0, 1)
                index += 1
            else:
                end = pattern.find("}", index + 1)
                parts = pattern[index + 1 : end].split(",")
                lower = int(parts[0])
                upper = int(parts[-1])
                bounds = (lower, upper)
                index = end + 1
            atoms[-1] = (charset, *bounds)
            continue
        atoms.append(
            (
                _REGEX_UNIVERSE
                if character == "."
                else _literal_regex_charset(character),
                1,
                1,
            )
        )
        index += 1

    variable_charsets: list[frozenset[str]] = []
    selective_prefix = pattern.startswith("^") or pattern.startswith(r"\A")
    saw_variable = False
    for charset, minimum, maximum in atoms:
        variable = maximum is None or minimum != maximum
        if variable:
            if not saw_variable and not selective_prefix:
                raise ValueError(
                    "a regex whose first variable repeat has no start anchor or "
                    "selective literal prefix is not supported"
                )
            saw_variable = True
            if any(not charset.isdisjoint(previous) for previous in variable_charsets):
                raise ValueError(
                    "regex contains overlapping variable repeats that can cause "
                    "unbounded backtracking"
                )
            variable_charsets.append(charset)
            continue
        if minimum == 0:
            continue
        if not saw_variable and len(charset) <= 2:
            selective_prefix = True
        if all(charset.isdisjoint(previous) for previous in variable_charsets):
            variable_charsets.clear()


def _escaped_regex_charset(character: str) -> tuple[frozenset[str], bool]:
    if character in {"A", "B", "b", "Z"}:
        return frozenset(), True
    if character == "s":
        return (
            frozenset({" ", "\t", "\n", "\r", "\f", "\v", "<unicode-space>"}),
            False,
        )
    if character == "S":
        whitespace, _ = _escaped_regex_charset("s")
        return _REGEX_UNIVERSE - whitespace, False
    if character == "d":
        return (
            frozenset("0123456789") | frozenset({"<unicode-digit>"}),
            False,
        )
    if character == "D":
        digits, _ = _escaped_regex_charset("d")
        return _REGEX_UNIVERSE - digits, False
    if character == "w":
        return (
            frozenset(
                "abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789_"
            )
            | frozenset({"<unicode-word>", "<unicode-digit>"}),
            False,
        )
    if character == "W":
        word, _ = _escaped_regex_charset("w")
        return _REGEX_UNIVERSE - word, False
    escaped_literals = {
        "a": "\a",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "f": "\f",
        "v": "\v",
    }
    return _literal_regex_charset(escaped_literals.get(character, character)), False


def _literal_regex_charset(character: str) -> frozenset[str]:
    values = {character}
    if character.isalpha():
        values.update({character.lower(), character.upper(), character.casefold()})
        for equivalents in _UNICODE_IGNORECASE_EQUIVALENTS:
            if not values.isdisjoint(equivalents):
                values.update(equivalents)
    if ord(character) < 128:
        return frozenset(values)
    if character.isspace():
        values.add("<unicode-space>")
    elif character.isdigit():
        values.update({"<unicode-digit>", "<unicode-word>"})
    elif character.isalnum() or character == "_":
        values.add("<unicode-word>")
    else:
        values.add("<unicode-other>")
    return frozenset(values)


def _regex_class_end(pattern: str, start: int) -> int:
    escaped = False
    for index in range(start + 1, len(pattern)):
        character = pattern[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "]":
            return index
    raise ValueError("regex character class is not closed")


def _regex_class_charset(content: str) -> frozenset[str]:
    negated = content.startswith("^")
    source = content[1:] if negated else content
    parts: list[frozenset[str]] = []
    index = 0
    while index < len(source):
        if source[index] == "\\":
            charset, zero_width = _escaped_regex_charset(source[index + 1])
            if zero_width:
                raise ValueError("zero-width escapes are not supported in regex classes")
            parts.append(charset)
            index += 2
            continue
        if (
            index + 2 < len(source)
            and source[index + 1] == "-"
            and source[index + 2] != "]"
        ):
            start = ord(source[index])
            end = ord(source[index + 2])
            if start > end or end - start > 256:
                raise ValueError("regex character class range is invalid or too wide")
            charset: set[str] = set()
            for codepoint in range(start, end + 1):
                charset.update(_literal_regex_charset(chr(codepoint)))
            parts.append(frozenset(charset))
            index += 3
            continue
        parts.append(_literal_regex_charset(source[index]))
        index += 1
    combined = frozenset().union(*parts)
    return _REGEX_UNIVERSE - combined if negated else combined


def _validate_json_filename(value: str) -> str:
    if not _SAFE_JSON_FILENAME.fullmatch(value) or ".." in value:
        raise ValueError(
            "filename must be a plain ASCII .json basename without path separators"
        )
    return value
