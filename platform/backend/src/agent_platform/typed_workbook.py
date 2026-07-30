from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
MAX_WORKBOOK_SHEETS = 16
MAX_WORKBOOK_COLUMNS = 128
MAX_WORKBOOK_ROWS_PER_SHEET = 5_000
MAX_WORKBOOK_CELLS = 100_000
MAX_WORKBOOK_STRING_CHARS = 8_192
MAX_WORKBOOK_TEXT_BYTES = 4_000_000
# Keep generated files inside the task-scoped public artifact registry ceiling.
MAX_WORKBOOK_BYTES = 2_000_000

_INVALID_XML_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_INVALID_SHEET_CHARACTER = re.compile(r"[\x00-\x1f\\/*?:\[\]]")
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,91}\.xlsx$")
_COLUMN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_EXCEL_EPOCH_DATE = date(1899, 12, 30)
_EXCEL_EPOCH_DATETIME = datetime(1899, 12, 30)


class WorkflowReferenceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str = Field(min_length=1, max_length=160)
    path: list[str] = Field(default_factory=list, max_length=32)
    optional: bool = False


class WorkflowValueReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reference: WorkflowReferenceTarget = Field(alias="$ref")
    optional: bool = False


class WorkbookColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(min_length=1, max_length=64)
    header: str = Field(min_length=1, max_length=255)
    value_type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
    ] = Field(default="string", alias="type")
    nullable: bool = False

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _COLUMN_KEY.fullmatch(value):
            raise ValueError(
                "column key must start with a letter or underscore and contain "
                "only letters, digits, underscores, dots, or hyphens"
            )
        return value

    @field_validator("header")
    @classmethod
    def validate_header(cls, value: str) -> str:
        _validate_xml_text(value, field="column header")
        return value


class WorkbookSheet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=31)
    columns: list[WorkbookColumn] = Field(
        min_length=1,
        max_length=MAX_WORKBOOK_COLUMNS,
    )
    rows: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_WORKBOOK_ROWS_PER_SHEET,
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("sheet name cannot have leading or trailing whitespace")
        if value.startswith("'") or value.endswith("'"):
            raise ValueError("sheet name cannot start or end with an apostrophe")
        if _INVALID_SHEET_CHARACTER.search(value):
            raise ValueError("sheet name contains an Excel-reserved character")
        _validate_xml_text(value, field="sheet name")
        return value

    @model_validator(mode="after")
    def validate_columns_and_rows(self) -> WorkbookSheet:
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("sheet contains duplicate column keys")
        allowed = set(keys)
        for index, row in enumerate(self.rows, start=1):
            unknown = sorted(set(row) - allowed)
            if unknown:
                raise ValueError(
                    f"row {index} contains undeclared columns: {unknown}"
                )
            for column in self.columns:
                if column.key not in row and not column.nullable:
                    raise ValueError(
                        f"row {index} is missing non-nullable column {column.key!r}"
                    )
        return self


class WorkbookSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sheets: list[WorkbookSheet] = Field(
        min_length=1,
        max_length=MAX_WORKBOOK_SHEETS,
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> WorkbookSpec:
        names = [sheet.name.casefold() for sheet in self.sheets]
        if len(names) != len(set(names)):
            raise ValueError("workbook contains duplicate case-insensitive sheet names")
        cell_count = sum(
            len(sheet.columns) * (len(sheet.rows) + 1) for sheet in self.sheets
        )
        if cell_count > MAX_WORKBOOK_CELLS:
            raise ValueError(
                f"workbook exceeds the {MAX_WORKBOOK_CELLS} cell limit"
            )
        return self


class WorkbookLineageSource(BaseModel):
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

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        _validate_xml_text(value, field="lineage reference")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("lineage sha256 must use the sha256:<64 lowercase hex> form")
        return value


class TypedWorkbookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    spec: WorkbookSpec | WorkflowValueReference
    filename: str = Field(default="workbook.xlsx", min_length=6, max_length=96)
    formula_policy: Literal["reject", "literal"] = "reject"
    lineage: list[WorkbookLineageSource] | WorkflowValueReference = Field(
        default_factory=list
    )

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if not _SAFE_FILENAME.fullmatch(value):
            raise ValueError(
                "filename must be a plain ASCII .xlsx basename without path separators"
            )
        if ".." in value:
            raise ValueError("filename cannot contain a parent-directory segment")
        return value


def write_typed_workbook_artifact(
    *,
    workspace: Path,
    spec: Any,
    filename: str,
    formula_policy: Literal["reject", "literal"],
    lineage: Any,
    run_id: str,
    node_id: str,
    application_id: str,
) -> dict[str, Any]:
    """Validate, render, and persist one deterministic task-owned XLSX artifact."""

    validated = WorkbookSpec.model_validate(spec)
    sources = _validate_lineage(lineage)
    canonical_spec = _canonical_json(
        {
            "formula_policy": formula_policy,
            "spec": validated.model_dump(mode="python", by_alias=True),
        }
    )
    spec_digest = _digest(canonical_spec)
    payload = render_typed_workbook(
        validated,
        formula_policy=formula_policy,
    )
    if len(payload) > MAX_WORKBOOK_BYTES:
        raise ValueError(
            f"rendered workbook exceeds the {MAX_WORKBOOK_BYTES} byte limit"
        )

    root = _safe_workspace(workspace)
    artifacts = _safe_artifact_directory(root)
    target = artifacts / filename
    replayed = _write_once(target, payload)
    digest = _digest(payload)
    public_lineage = {
        "generator": {
            "block_type": "typed_workbook",
            "block_version": 1,
        },
        "application_id": application_id,
        "run_id": run_id,
        "node_id": node_id,
        "workbook_spec_sha256": spec_digest,
        "sources": [source.model_dump(mode="json") for source in sources],
    }
    return {
        "relative_path": f"artifacts/{filename}",
        "filename": filename,
        "media_type": XLSX_MEDIA_TYPE,
        "size_bytes": len(payload),
        "sha256": digest,
        "lineage": public_lineage,
        "replayed": replayed,
    }


def render_typed_workbook(
    spec: WorkbookSpec | dict[str, Any],
    *,
    formula_policy: Literal["reject", "literal"] = "reject",
) -> bytes:
    """Return a deterministic, formula-free Office Open XML workbook."""

    workbook = (
        spec if isinstance(spec, WorkbookSpec) else WorkbookSpec.model_validate(spec)
    )
    normalized = _validated_cells(workbook, formula_policy=formula_policy)
    members: list[tuple[str, bytes]] = [
        ("[Content_Types].xml", _content_types_xml(len(workbook.sheets))),
        ("_rels/.rels", _package_relationships_xml()),
        ("xl/workbook.xml", _workbook_xml(workbook)),
        (
            "xl/_rels/workbook.xml.rels",
            _workbook_relationships_xml(len(workbook.sheets)),
        ),
        ("xl/styles.xml", _styles_xml()),
    ]
    for index, (sheet, rows) in enumerate(
        zip(workbook.sheets, normalized, strict=True),
        start=1,
    ):
        members.append(
            (f"xl/worksheets/sheet{index}.xml", _worksheet_xml(sheet, rows))
        )

    output = BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(
                info,
                payload,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    result = output.getvalue()
    if len(result) > MAX_WORKBOOK_BYTES:
        raise ValueError(
            f"rendered workbook exceeds the {MAX_WORKBOOK_BYTES} byte limit"
        )
    return result


def _validate_lineage(value: Any) -> list[WorkbookLineageSource]:
    if not isinstance(value, list):
        raise TypeError("lineage must resolve to an array")
    if len(value) > 100:
        raise ValueError("lineage cannot contain more than 100 sources")
    return [WorkbookLineageSource.model_validate(item) for item in value]


def _validated_cells(
    workbook: WorkbookSpec,
    *,
    formula_policy: Literal["reject", "literal"],
) -> list[list[list[tuple[str, Any] | None]]]:
    all_sheets: list[list[list[tuple[str, Any] | None]]] = []
    text_bytes = 0
    for sheet in workbook.sheets:
        text_bytes += len(sheet.name.encode("utf-8"))
        text_bytes += sum(
            len(column.header.encode("utf-8")) for column in sheet.columns
        )
        for column in sheet.columns:
            _validate_formula_text(
                column.header,
                formula_policy=formula_policy,
                location=f"{sheet.name}!{column.key} header",
            )
        rows: list[list[tuple[str, Any] | None]] = []
        for row_number, row in enumerate(sheet.rows, start=2):
            cells: list[tuple[str, Any] | None] = []
            for column in sheet.columns:
                value = row.get(column.key)
                if value is None:
                    if not column.nullable:
                        raise ValueError(
                            f"{sheet.name}!{column.key} row {row_number} cannot be null"
                        )
                    cells.append(None)
                    continue
                normalized = _normalize_cell(
                    value,
                    value_type=column.value_type,
                    formula_policy=formula_policy,
                    location=f"{sheet.name}!{column.key} row {row_number}",
                )
                if normalized[0] == "string":
                    text_bytes += len(normalized[1].encode("utf-8"))
                    if text_bytes > MAX_WORKBOOK_TEXT_BYTES:
                        raise ValueError(
                            "workbook text exceeds the bounded UTF-8 byte limit"
                        )
                cells.append(normalized)
            rows.append(cells)
        all_sheets.append(rows)
    return all_sheets


def _normalize_cell(
    value: Any,
    *,
    value_type: str,
    formula_policy: Literal["reject", "literal"],
    location: str,
) -> tuple[str, Any]:
    if value_type == "string":
        if not isinstance(value, str):
            raise TypeError(f"{location} must be a string")
        if len(value) > MAX_WORKBOOK_STRING_CHARS:
            raise ValueError(
                f"{location} exceeds the {MAX_WORKBOOK_STRING_CHARS} character limit"
            )
        _validate_xml_text(value, field=location)
        _validate_formula_text(
            value,
            formula_policy=formula_policy,
            location=location,
        )
        return ("string", value)
    if value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{location} must be an integer")
        if abs(value) >= 10**15:
            raise ValueError(
                f"{location} exceeds Excel's exact numeric precision; store it as text"
            )
        return ("number", str(value))
    if value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise TypeError(f"{location} must be a finite number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{location} must be a finite number")
        try:
            decimal = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError(f"{location} must be a finite number") from error
        if not decimal.is_finite():
            raise ValueError(f"{location} must be a finite number")
        digits = len(decimal.as_tuple().digits)
        if digits > 15:
            raise ValueError(
                f"{location} exceeds Excel's 15-digit numeric precision"
            )
        if decimal and (decimal.adjusted() > 307 or decimal.adjusted() < -307):
            raise ValueError(f"{location} is outside Excel's numeric range")
        return ("number", _decimal_text(decimal))
    if value_type == "boolean":
        if not isinstance(value, bool):
            raise TypeError(f"{location} must be a boolean")
        return ("boolean", value)
    if value_type == "date":
        parsed = _parse_date(value, location=location)
        return ("date", str((parsed - _EXCEL_EPOCH_DATE).days))
    if value_type == "datetime":
        parsed = _parse_datetime(value, location=location)
        delta = parsed - _EXCEL_EPOCH_DATETIME
        serial = (
            Decimal(delta.days)
            + Decimal(delta.seconds) / Decimal(86_400)
            + Decimal(delta.microseconds) / Decimal(86_400_000_000)
        )
        return ("datetime", _decimal_text(serial))
    raise ValueError(f"{location} uses unsupported value type {value_type!r}")


def _parse_date(value: Any, *, location: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{location} must be a date without a time")
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{location} must be an ISO 8601 date") from error
    else:
        raise TypeError(f"{location} must be an ISO 8601 date")
    if parsed < date(1900, 1, 1):
        raise ValueError(f"{location} predates the supported Excel date range")
    return parsed


def _parse_datetime(value: Any, *, location: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as error:
            raise ValueError(f"{location} must be an ISO 8601 datetime") from error
    else:
        raise TypeError(f"{location} must be an ISO 8601 datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if parsed < datetime(1900, 1, 1):
        raise ValueError(f"{location} predates the supported Excel date range")
    return parsed


def _worksheet_xml(
    sheet: WorkbookSheet,
    rows: list[list[tuple[str, Any] | None]],
) -> bytes:
    header_cells = [
        _inline_string_cell(_cell_reference(index, 1), column.header, style=3)
        for index, column in enumerate(sheet.columns, start=1)
    ]
    row_xml = [f'<row r="1">{"".join(header_cells)}</row>']
    for row_number, row in enumerate(rows, start=2):
        cells: list[str] = []
        for column_number, normalized in enumerate(row, start=1):
            if normalized is None:
                continue
            kind, value = normalized
            reference = _cell_reference(column_number, row_number)
            if kind == "string":
                cells.append(_inline_string_cell(reference, value))
            elif kind == "boolean":
                cells.append(
                    f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>'
                )
            elif kind == "date":
                cells.append(f'<c r="{reference}" s="1"><v>{value}</v></c>')
            elif kind == "datetime":
                cells.append(f'<c r="{reference}" s="2"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    last = _cell_reference(len(sheet.columns), len(rows) + 1)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )
    return xml.encode("utf-8")


def _inline_string_cell(reference: str, value: str, *, style: int | None = None) -> str:
    style_attribute = f' s="{style}"' if style is not None else ""
    return (
        f'<c r="{reference}" t="inlineStr"{style_attribute}>'
        f'<is><t xml:space="preserve">{escape(value)}</t></is></c>'
    )


def _content_types_xml(sheet_count: int) -> bytes:
    worksheet_overrides = "".join(
        (
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'
        )
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.styles+xml"/>'
        f"{worksheet_overrides}</Types>"
    ).encode("utf-8")


def _package_relationships_xml() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ).encode("utf-8")


def _workbook_xml(workbook: WorkbookSpec) -> bytes:
    sheets = "".join(
        (
            f"<sheet name={quoteattr(sheet.name)} sheetId=\"{index}\" "
            f'r:id="rId{index}"/>'
        )
        for index, sheet in enumerate(workbook.sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets>"
        '<calcPr calcId="0" calcMode="manual" fullCalcOnLoad="0"/>'
        "</workbook>"
    ).encode("utf-8")


def _workbook_relationships_xml(sheet_count: int) -> bytes:
    sheet_relationships = "".join(
        (
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        )
        for index in range(1, sheet_count + 1)
    )
    style_id = sheet_count + 1
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_relationships}"
        f'<Relationship Id="rId{style_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    ).encode("utf-8")


def _styles_xml() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
        '<numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm:ss"/>'
        "</numFmts>"
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    ).encode("utf-8")


def _cell_reference(column: int, row: int) -> str:
    letters = ""
    current = column
    while current:
        current, remainder = divmod(current - 1, 26)
        letters = f"{chr(65 + remainder)}{letters}"
    return f"{letters}{row}"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _validate_xml_text(value: str, *, field: str) -> None:
    if _INVALID_XML_CHARACTER.search(value):
        raise ValueError(f"{field} contains a character that XML cannot represent")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} contains invalid Unicode") from error


def _validate_formula_text(
    value: str,
    *,
    formula_policy: Literal["reject", "literal"],
    location: str,
) -> None:
    if (
        formula_policy == "reject"
        and value.lstrip(" \t\r\n").startswith(_FORMULA_PREFIXES)
    ):
        raise ValueError(
            f"{location} looks like a spreadsheet formula; use literal policy "
            "only when the value must remain text"
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    raise TypeError(f"value is not canonical JSON: {type(value).__name__}")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _safe_workspace(workspace: Path) -> Path:
    candidate = Path(workspace)
    if candidate.is_symlink():
        raise ValueError("workflow workspace cannot be a symbolic link")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError("workflow workspace is not an available directory")
    return resolved


def _safe_artifact_directory(workspace: Path) -> Path:
    artifacts = workspace / "artifacts"
    if artifacts.is_symlink():
        raise ValueError("artifact directory cannot be a symbolic link")
    if artifacts.exists() and not artifacts.is_dir():
        raise ValueError("artifact directory path is not a directory")
    artifacts.mkdir(mode=0o700, exist_ok=True)
    resolved = artifacts.resolve()
    if resolved.parent != workspace or resolved != artifacts:
        raise ValueError("artifact directory escapes the workflow workspace")
    return resolved


def _write_once(target: Path, payload: bytes) -> bool:
    if target.is_symlink():
        raise ValueError("artifact target cannot be a symbolic link")
    if target.exists():
        if not target.is_file():
            raise ValueError("artifact target is not a regular file")
        if _file_matches(target, payload):
            return True
        raise FileExistsError(
            "artifact target already exists with different content"
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(target, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if target.is_symlink() or not target.is_file():
            raise ValueError("artifact target became unsafe during creation") from None
        if _file_matches(target, payload):
            return True
        raise FileExistsError(
            "artifact target already exists with different content"
        ) from None
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            target.unlink(missing_ok=True)
        raise
    os.chmod(target, 0o600)
    return False


def _file_matches(target: Path, payload: bytes) -> bool:
    if target.stat().st_size != len(payload):
        return False
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == hashlib.sha256(payload).hexdigest()
