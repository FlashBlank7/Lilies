from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .lilies_models import Digest, OpaqueReference


SafeRelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=1_000),
]
StableEvidenceLabel = Annotated[
    str,
    StringConstraints(min_length=1, max_length=500),
]


def safe_relative_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError("verification target must be a POSIX relative path")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("verification target must remain inside the archive")
    return "/".join(parts)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class OracleEvidenceSelector(_FrozenContract):
    kind: Literal["artifact", "host_receipt"]
    label: StableEvidenceLabel
    operation: OpaqueReference | None = None

    @model_validator(mode="after")
    def operation_matches_kind(self) -> "OracleEvidenceSelector":
        if self.kind == "artifact" and self.operation is not None:
            raise ValueError("artifact selectors cannot name a host-write operation")
        if self.kind == "host_receipt" and self.operation is None:
            raise ValueError("host-receipt selectors require an operation")
        return self

    @property
    def evidence_key(self) -> str:
        if self.kind == "artifact":
            return f"artifact:{self.label}"
        return f"host_receipt:{self.operation}:{self.label}"


class OracleCheck(_FrozenContract):
    check_id: OpaqueReference
    kind: Literal[
        "file_exists",
        "file_sha256",
        "json_equals",
        "json_absent",
        "json_length",
        "xlsx_sheet_exists",
        "xlsx_headers",
        "xlsx_row_count",
        "xlsx_cell_equals",
    ]
    archive_path: SafeRelativePath | None = None
    evidence_selector: OracleEvidenceSelector | None = None
    json_pointer: str | None = Field(default=None, max_length=1_000)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=160)
    cell_reference: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$",
    )
    expected: Any = None
    expected_digest: Digest | None = None

    @field_validator("archive_path")
    @classmethod
    def archive_path_is_safe(cls, value: str | None) -> str | None:
        return safe_relative_path(value) if value is not None else None

    @field_validator("json_pointer")
    @classmethod
    def pointer_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != "" and not value.startswith("/"):
            raise ValueError("JSON pointer must be empty or begin with slash")
        if len(value.split("/")) > 100:
            raise ValueError("JSON pointer is too deep")
        return value

    @model_validator(mode="after")
    def target_and_expected_value_match_kind(self) -> "OracleCheck":
        if (self.archive_path is None) == (self.evidence_selector is None):
            raise ValueError(
                "oracle checks require exactly one archive path or evidence selector"
            )
        if self.kind.startswith("xlsx_"):
            if (
                self.sheet_name is None
                or self.json_pointer is not None
                or self.expected_digest is not None
            ):
                raise ValueError(
                    "XLSX checks require a sheet name and no JSON pointer or digest"
                )
            if self.kind == "xlsx_sheet_exists":
                if self.cell_reference is not None or self.expected is not None:
                    raise ValueError(
                        "xlsx_sheet_exists accepts only a sheet name"
                    )
            elif self.kind == "xlsx_headers":
                if (
                    self.cell_reference is not None
                    or not isinstance(self.expected, list)
                    or not self.expected
                    or any(
                        not isinstance(item, str) or not item
                        for item in self.expected
                    )
                ):
                    raise ValueError(
                        "xlsx_headers requires a non-empty string list"
                    )
            elif self.kind == "xlsx_row_count":
                if (
                    self.cell_reference is not None
                    or isinstance(self.expected, bool)
                    or not isinstance(self.expected, int)
                    or self.expected < 0
                ):
                    raise ValueError(
                        "xlsx_row_count requires a non-negative integer"
                    )
            elif self.cell_reference is None:
                raise ValueError(
                    "xlsx_cell_equals requires a cell reference"
                )
            return self
        if self.sheet_name is not None or self.cell_reference is not None:
            raise ValueError("non-XLSX checks cannot name a sheet or cell")
        if self.kind == "file_exists":
            if (
                self.json_pointer is not None
                or self.expected is not None
                or self.expected_digest is not None
            ):
                raise ValueError("file_exists accepts no expected value")
        elif self.kind == "file_sha256":
            if (
                self.expected_digest is None
                or self.json_pointer is not None
                or self.expected is not None
            ):
                raise ValueError("file_sha256 requires only expected_digest")
        elif self.kind == "json_absent":
            if (
                self.json_pointer is None
                or self.expected is not None
                or self.expected_digest is not None
            ):
                raise ValueError("json_absent requires only json_pointer")
        elif self.kind == "json_length":
            if (
                self.json_pointer is None
                or isinstance(self.expected, bool)
                or not isinstance(self.expected, int)
                or self.expected < 0
                or self.expected_digest is not None
            ):
                raise ValueError("json_length requires a non-negative integer")
        elif self.json_pointer is None or self.expected_digest is not None:
            raise ValueError("json_equals requires a JSON pointer and expected value")
        return self


class OracleContract(_FrozenContract):
    schema_version: Literal["1.0"]
    oracle_id: OpaqueReference
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    validation_mode: Literal["real_host"]
    checks: list[OracleCheck] = Field(min_length=1, max_length=500)

    @field_validator("checks")
    @classmethod
    def checks_are_unique_and_business_grounded(
        cls,
        value: list[OracleCheck],
    ) -> list[OracleCheck]:
        check_ids = [check.check_id for check in value]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("oracle check IDs must be unique")
        if not any(check.evidence_selector is not None for check in value):
            raise ValueError(
                "formal oracle requires a business artifact or host-receipt selector"
            )
        return value


class ArchivedEvidenceIndexEntry(_FrozenContract):
    schema_version: Literal["1.0"] = "1.0"
    evidence_key: str = Field(min_length=3, max_length=1_100)
    kind: Literal["artifact", "host_receipt"]
    label: StableEvidenceLabel
    operation: OpaqueReference | None = None
    provenance_source: Literal[
        "platform_artifact_scan",
        "platform_host_write",
    ]
    run_id: OpaqueReference
    archive_path: SafeRelativePath
    digest: Digest
    size_bytes: int = Field(ge=0, le=128 * 1024 * 1024)
    media_type: str = Field(min_length=1, max_length=255)

    @field_validator("archive_path")
    @classmethod
    def archive_path_is_safe(cls, value: str) -> str:
        return safe_relative_path(value)

    @model_validator(mode="after")
    def provenance_and_key_match_kind(self) -> "ArchivedEvidenceIndexEntry":
        selector = OracleEvidenceSelector(
            kind=self.kind,
            label=self.label,
            operation=self.operation,
        )
        expected_source = (
            "platform_artifact_scan"
            if self.kind == "artifact"
            else "platform_host_write"
        )
        if self.provenance_source != expected_source:
            raise ValueError("evidence provenance does not match its kind")
        if self.evidence_key != selector.evidence_key:
            raise ValueError("evidence key does not match its stable selector")
        expected_prefix = "artifacts/" if self.kind == "artifact" else "host-receipts/"
        if not self.archive_path.startswith(expected_prefix):
            raise ValueError("evidence archive path does not match its kind")
        return self


class ArchivedEvidenceIndex(_FrozenContract):
    schema_version: Literal["1.0"]
    complete: Literal[True] = True
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    application_id: UUID
    entry_count: int = Field(ge=0, le=2_000)
    entries: list[ArchivedEvidenceIndexEntry] = Field(max_length=2_000)

    @field_validator("entries")
    @classmethod
    def entries_are_exactly_addressable(
        cls,
        value: list[ArchivedEvidenceIndexEntry],
    ) -> list[ArchivedEvidenceIndexEntry]:
        keys = [entry.evidence_key for entry in value]
        paths = [entry.archive_path for entry in value]
        if len(keys) != len(set(keys)):
            raise ValueError("stable evidence selectors must resolve exactly once")
        if len(paths) != len(set(paths)):
            raise ValueError("evidence archive paths must be unique")
        return value

    @model_validator(mode="after")
    def entry_count_matches(self) -> "ArchivedEvidenceIndex":
        if self.entry_count != len(self.entries):
            raise ValueError("evidence index entry count does not match its entries")
        return self
