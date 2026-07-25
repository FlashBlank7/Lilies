from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import posixpath
import stat
import sys
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree

from .collaboration_models import (
    EvidenceKind,
    EvidenceRef,
    VerificationClaim,
    VerificationDifference,
    VerificationResultPayload,
    VerificationVerdict,
)
from .formal_verification_contracts import (
    ArchivedEvidenceIndex,
    OracleCheck,
    OracleContract,
)
from .task_packages import (
    ArchiveStatus,
    MAX_ARCHIVE_FILE_BYTES,
    TaskPackageError,
    TaskPackageManager,
    ValidationMode,
)


MAX_ORACLE_FILE_BYTES = 2 * 1024 * 1024
MAX_DIFFERENCE_TEXT = 500
MAX_XLSX_ENTRIES = 2_048
MAX_XLSX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_XLSX_XML_BYTES = 8 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 200
_OFFICE_RELATIONSHIP_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)


class IndependentVerificationError(RuntimeError):
    """Frozen evidence could not be independently verified."""


class IndependentVerificationRejected(IndependentVerificationError):
    """The request was not an authorized real-host verification."""


def _read_regular(path: Path, *, limit: int) -> bytes:
    if path.is_symlink():
        raise IndependentVerificationRejected("verification input is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise IndependentVerificationRejected(
            "verification input is not readable"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise IndependentVerificationRejected(
                "verification input is not an isolated regular file"
            )
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise IndependentVerificationRejected(
                    "verification input exceeds its size limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise IndependentVerificationRejected(
                "verification input changed while being read"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, value
    current = value
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return False, None
            current = current[int(token)]
        else:
            return False, None
    return True, current


def _observed_summary(
    value: Any,
    *,
    protected_markers: Sequence[str] = (),
) -> str:
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)[:MAX_DIFFERENCE_TEXT]
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value)
        if any(
            unicodedata.normalize("NFKC", marker) in normalized
            for marker in protected_markers
        ):
            return "redacted protected value"
        return value[:MAX_DIFFERENCE_TEXT]
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    kind = "array" if isinstance(value, list) else "object"
    size = len(value) if hasattr(value, "__len__") else 0
    return (
        f"{kind}(items={size},"
        f"digest=sha256:{hashlib.sha256(encoded).hexdigest()})"
    )


def _oracle_expectation_summary(check: OracleCheck) -> str:
    if check.kind == "file_exists":
        return "oracle requires an archived file"
    if check.kind == "file_sha256":
        return "oracle requires the sealed file content"
    if check.kind == "json_absent":
        return "oracle requires the selected value to be absent"
    if check.kind == "json_length":
        return "oracle requires a different collection length"
    if check.kind == "xlsx_sheet_exists":
        return "oracle requires the workbook sheet"
    if check.kind == "xlsx_headers":
        return "oracle requires the declared workbook columns"
    if check.kind == "xlsx_row_count":
        return "oracle requires a different workbook data-row count"
    if check.kind == "xlsx_cell_equals":
        return "oracle expected workbook value was not satisfied"
    return "oracle expected value was not satisfied"


def _public_check_id(check: OracleCheck, *, private_salt: str) -> str:
    payload = f"{private_salt}\0{check.check_id}".encode("utf-8")
    return f"oracle-check:{hashlib.sha256(payload).hexdigest()[:32]}"


def _evidence(
    public_check_id: str,
    payload: bytes,
    *,
    captured_at: datetime,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"archive-check:{public_check_id}",
        kind=EvidenceKind.archive,
        digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        media_type="application/octet-stream",
        label=f"Frozen archive evidence for {public_check_id}",
        captured_at=captured_at,
    )


def _strict_json_document(payload: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(
        payload,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _json_values_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _json_values_equal(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    if isinstance(actual, dict):
        return set(actual) == set(expected) and all(
            _json_values_equal(actual[key], expected[key])
            for key in actual
        )
    return bool(actual == expected)


def _xlsx_member_bytes(
    archive: zipfile.ZipFile,
    member: str,
) -> bytes:
    try:
        info = archive.getinfo(member)
    except KeyError as error:
        raise ValueError(f"missing XLSX member: {member}") from error
    if info.file_size > MAX_XLSX_XML_BYTES:
        raise ValueError("XLSX XML member exceeds its size limit")
    with archive.open(info, "r") as handle:
        payload = handle.read(MAX_XLSX_XML_BYTES + 1)
    if len(payload) > MAX_XLSX_XML_BYTES:
        raise ValueError("XLSX XML member exceeds its size limit")
    return payload


def _xlsx_archive(payload: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(BytesIO(payload), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("artifact is not a valid XLSX ZIP container") from error
    entries = archive.infolist()
    if len(entries) > MAX_XLSX_ENTRIES:
        archive.close()
        raise ValueError("XLSX contains too many ZIP entries")
    total = 0
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or entry.flag_bits & 0x1
        ):
            archive.close()
            raise ValueError("XLSX contains an unsafe ZIP entry")
        total += entry.file_size
        if total > MAX_XLSX_UNCOMPRESSED_BYTES:
            archive.close()
            raise ValueError("XLSX uncompressed content exceeds its size limit")
        if (
            entry.file_size > 0
            and (
                entry.compress_size == 0
                or entry.file_size
                > entry.compress_size * MAX_XLSX_COMPRESSION_RATIO
            )
        ):
            archive.close()
            raise ValueError("XLSX ZIP entry exceeds the compression-ratio limit")
    return archive


def _xlsx_xml(payload: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ValueError("XLSX contains invalid XML") from error


def _xlsx_sheet_members(
    archive: zipfile.ZipFile,
) -> dict[str, str]:
    workbook = _xlsx_xml(_xlsx_member_bytes(archive, "xl/workbook.xml"))
    relationships = _xlsx_xml(
        _xlsx_member_bytes(archive, "xl/_rels/workbook.xml.rels")
    )
    relationship_targets: dict[str, str] = {}
    for relationship in relationships.iter():
        if relationship.tag.rsplit("}", 1)[-1] != "Relationship":
            continue
        relation_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        if (
            not relation_id
            or not target
            or relationship.attrib.get("TargetMode") == "External"
        ):
            raise ValueError("XLSX workbook relationship is invalid")
        normalized = posixpath.normpath(posixpath.join("xl", target))
        if (
            normalized.startswith("../")
            or not normalized.startswith("xl/worksheets/")
        ):
            raise ValueError("XLSX worksheet relationship escapes its boundary")
        relationship_targets[relation_id] = normalized
    sheets: dict[str, str] = {}
    for sheet in workbook.iter():
        if sheet.tag.rsplit("}", 1)[-1] != "sheet":
            continue
        name = sheet.attrib.get("name")
        relation_id = sheet.attrib.get(_OFFICE_RELATIONSHIP_ID)
        if not name or not relation_id or relation_id not in relationship_targets:
            raise ValueError("XLSX workbook sheet relationship is incomplete")
        if name in sheets:
            raise ValueError("XLSX workbook contains duplicate sheet names")
        sheets[name] = relationship_targets[relation_id]
    if not sheets:
        raise ValueError("XLSX workbook contains no sheets")
    return sheets


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xlsx_xml(_xlsx_member_bytes(archive, "xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root:
        if item.tag.rsplit("}", 1)[-1] != "si":
            continue
        values.append(
            "".join(
                node.text or ""
                for node in item.iter()
                if node.tag.rsplit("}", 1)[-1] == "t"
            )
        )
    return values


def _xlsx_cell_value(
    cell: ElementTree.Element,
    *,
    shared_strings: Sequence[str],
) -> Any:
    if any(node.tag.rsplit("}", 1)[-1] == "f" for node in cell):
        raise ValueError("XLSX formula cells are not accepted as oracle evidence")
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter()
            if node.tag.rsplit("}", 1)[-1] == "t"
        )
    value_node = next(
        (
            node
            for node in cell
            if node.tag.rsplit("}", 1)[-1] == "v"
        ),
        None,
    )
    raw = "" if value_node is None or value_node.text is None else value_node.text
    if cell_type == "s":
        if not raw.isdigit() or int(raw) >= len(shared_strings):
            raise ValueError("XLSX shared-string reference is invalid")
        return shared_strings[int(raw)]
    if cell_type == "b":
        if raw not in {"0", "1"}:
            raise ValueError("XLSX boolean cell is invalid")
        return raw == "1"
    if cell_type in {"str", "e"}:
        return raw
    if raw == "":
        return ""
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError as error:
            raise ValueError("XLSX numeric cell is invalid") from error


def _xlsx_sheet_cells(
    payload: bytes,
    *,
    sheet_name: str,
) -> dict[str, Any]:
    archive = _xlsx_archive(payload)
    try:
        sheets = _xlsx_sheet_members(archive)
        if sheet_name not in sheets:
            raise KeyError(sheet_name)
        shared_strings = _xlsx_shared_strings(archive)
        sheet = _xlsx_xml(_xlsx_member_bytes(archive, sheets[sheet_name]))
        cells: dict[str, Any] = {}
        for cell in sheet.iter():
            if cell.tag.rsplit("}", 1)[-1] != "c":
                continue
            reference = cell.attrib.get("r")
            if (
                not reference
                or reference in cells
                or not reference[:1].isalpha()
            ):
                raise ValueError("XLSX cell reference is missing or duplicated")
            cells[reference] = _xlsx_cell_value(
                cell,
                shared_strings=shared_strings,
            )
        return cells
    finally:
        archive.close()


def _xlsx_column_number(reference: str) -> int:
    letters = reference.rstrip("0123456789")
    value = 0
    for character in letters:
        if not ("A" <= character <= "Z"):
            raise ValueError("XLSX cell reference is invalid")
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def _xlsx_row_number(reference: str) -> int:
    digits = reference[len(reference.rstrip("0123456789")) :]
    if not digits:
        raise ValueError("XLSX cell reference is invalid")
    return int(digits)


def _xlsx_headers_and_data_rows(
    cells: Mapping[str, Any],
) -> tuple[list[Any], int]:
    occupied_rows = sorted(
        {
            _xlsx_row_number(reference)
            for reference, value in cells.items()
            if value not in {"", None}
        }
    )
    if not occupied_rows:
        return [], 0
    header_row = occupied_rows[0]
    header_cells = {
        _xlsx_column_number(reference): value
        for reference, value in cells.items()
        if _xlsx_row_number(reference) == header_row
        and value not in {"", None}
    }
    if not header_cells:
        return [], 0
    headers = [
        header_cells.get(column, "")
        for column in range(1, max(header_cells) + 1)
    ]
    data_rows = sum(1 for row in occupied_rows if row > header_row)
    return headers, data_rows


def _evaluate_check(
    archive_root: Path,
    check: OracleCheck,
    *,
    archive_path: str,
    captured_at: datetime,
    public_check_id: str,
    manifest_digest: str | None,
    manifest_size: int | None,
    protected_markers: Sequence[str],
) -> tuple[VerificationDifference | None, EvidenceRef]:
    target = archive_root.joinpath(*PurePosixPath(archive_path).parts)
    root = archive_root.resolve()
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise IndependentVerificationRejected("oracle target escapes the archive")
    if manifest_digest is None or manifest_size is None:
        if target.exists() or target.is_symlink():
            raise IndependentVerificationRejected(
                "oracle target appeared outside the frozen archive manifest"
            )
        payload = b"missing"
        evidence = _evidence(
            public_check_id,
            payload,
            captured_at=captured_at,
        )
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual="file missing",
                evidence_refs=[evidence],
            ),
            evidence,
        )
    try:
        payload = _read_regular(target, limit=MAX_ARCHIVE_FILE_BYTES)
    except IndependentVerificationRejected:
        evidence = _evidence(
            public_check_id,
            b"missing",
            captured_at=captured_at,
        )
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual="target missing or unreadable",
                evidence_refs=[evidence],
            ),
            evidence,
        )
    actual_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if (
        len(payload) != manifest_size
        or not hmac.compare_digest(actual_digest, manifest_digest)
    ):
        raise IndependentVerificationRejected(
            "oracle target changed after archive replay"
        )
    evidence = _evidence(
        public_check_id,
        payload,
        captured_at=captured_at,
    )
    if check.kind == "file_exists":
        return None, evidence
    if check.kind == "file_sha256":
        if hmac.compare_digest(actual_digest, str(check.expected_digest)):
            return None, evidence
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual=actual_digest,
                evidence_refs=[evidence],
            ),
            evidence,
        )
    if check.kind.startswith("xlsx_"):
        assert check.sheet_name is not None
        try:
            cells = _xlsx_sheet_cells(
                payload,
                sheet_name=check.sheet_name,
            )
        except KeyError:
            return (
                VerificationDifference(
                    check_id=public_check_id,
                    expected=_oracle_expectation_summary(check),
                    actual="workbook sheet missing",
                    evidence_refs=[evidence],
                ),
                evidence,
            )
        except ValueError:
            return (
                VerificationDifference(
                    check_id=public_check_id,
                    expected="oracle requires a safe, valid XLSX workbook",
                    actual="invalid XLSX",
                    evidence_refs=[evidence],
                ),
                evidence,
            )
        if check.kind == "xlsx_sheet_exists":
            return None, evidence
        headers, row_count = _xlsx_headers_and_data_rows(cells)
        if check.kind == "xlsx_headers":
            if _json_values_equal(headers, check.expected):
                return None, evidence
            actual = _observed_summary(
                headers,
                protected_markers=protected_markers,
            )
        elif check.kind == "xlsx_row_count":
            if row_count == check.expected:
                return None, evidence
            actual = f"rows={row_count}"
        else:
            assert check.cell_reference is not None
            if check.cell_reference not in cells:
                actual = "cell missing"
            else:
                value = cells[check.cell_reference]
                if _json_values_equal(value, check.expected):
                    return None, evidence
                actual = _observed_summary(
                    value,
                    protected_markers=protected_markers,
                )
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual=actual,
                evidence_refs=[evidence],
            ),
            evidence,
        )
    try:
        document = _strict_json_document(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected="oracle requires valid JSON",
                actual="invalid JSON",
                evidence_refs=[evidence],
            ),
            evidence,
        )
    assert check.json_pointer is not None
    present, actual = _json_pointer(document, check.json_pointer)
    if check.kind == "json_absent":
        if not present:
            return None, evidence
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual="value present",
                evidence_refs=[evidence],
            ),
            evidence,
        )
    if not present:
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual="value missing",
                evidence_refs=[evidence],
            ),
            evidence,
        )
    if check.kind == "json_length":
        actual_length = len(actual) if isinstance(actual, (list, dict, str)) else None
        if actual_length == check.expected:
            return None, evidence
        return (
            VerificationDifference(
                check_id=public_check_id,
                expected=_oracle_expectation_summary(check),
                actual=f"length={actual_length}",
                evidence_refs=[evidence],
            ),
            evidence,
        )
    if _json_values_equal(actual, check.expected):
        return None, evidence
    return (
        VerificationDifference(
            check_id=public_check_id,
            expected=_oracle_expectation_summary(check),
            actual=_observed_summary(
                actual,
                protected_markers=protected_markers,
            ),
            evidence_refs=[evidence],
        ),
        evidence,
    )


def verify_frozen_claim(
    *,
    state_root: Path,
    task_id: str,
    revision: int,
    claim: VerificationClaim,
) -> VerificationResultPayload:
    """Run the hidden declarative oracle without mutating package or archive state."""

    if not isinstance(claim, VerificationClaim) or claim.schema_version != "1.1":
        raise IndependentVerificationRejected(
            "formal verification requires a server-owned frozen v1.1 claim"
        )
    manager = TaskPackageManager(state_root, read_only=True)
    try:
        _policy_source_root, verification_policy = (
            manager.load_verification_policy_bundle(
                str(claim.verification_process_digest)
            )
        )
        archive = manager.validate_claim_binding(
            task_id=task_id,
            revision=revision,
            claim=claim,
        )
        package = manager.load_frozen(
            task_id,
            revision,
            expected_public_digest=str(claim.task_package_digest),
        )
        archive_root, replay = manager.find_archive_by_digest(
            task_id,
            revision,
            str(claim.archive_manifest_digest),
        )
    except (TaskPackageError, ValueError) as error:
        raise IndependentVerificationRejected(
            "frozen verification bindings were rejected"
        ) from error
    if (
        archive.status is not ArchiveStatus.succeeded
        or replay.validation_mode is not ValidationMode.real_host
        or replay.environment_ready_digest != claim.environment_ready_digest
    ):
        raise IndependentVerificationRejected(
            "substitute, unhealthy, or invalid archives cannot be verified"
        )
    oracle_path = package.root / "protected" / "oracle" / "oracle.json"
    try:
        oracle_payload = _read_regular(oracle_path, limit=MAX_ORACLE_FILE_BYTES)
        oracle_entry = next(
            entry
            for entry in package.record.immutable_files
            if entry.path == "protected/oracle/oracle.json"
        )
        if (
            len(oracle_payload) != oracle_entry.size_bytes
            or not hmac.compare_digest(
                f"sha256:{hashlib.sha256(oracle_payload).hexdigest()}",
                oracle_entry.digest,
            )
        ):
            raise IndependentVerificationRejected(
                "hidden oracle differs from the frozen package record"
            )
        oracle = OracleContract.model_validate_json(oracle_payload)
    except IndependentVerificationRejected:
        raise
    except Exception as error:
        raise IndependentVerificationRejected(
            "hidden oracle contract is unavailable or invalid"
        ) from error
    if (
        oracle.task_id != task_id
        or oracle.revision != revision
        or oracle.validation_mode != ValidationMode.real_host.value
    ):
        raise IndependentVerificationRejected(
            "hidden oracle is not bound to this task revision"
        )
    oracle_digest = (
        "sha256:"
        + hashlib.sha256(
            package.record.sealed_package_digest.encode("utf-8")
            + b"\0"
            + oracle_payload
        ).hexdigest()
    )
    archive_entries = {entry.path: entry for entry in replay.files}
    index_manifest_entry = archive_entries.get("evidence-index.json")
    if index_manifest_entry is None:
        raise IndependentVerificationRejected(
            "frozen archive has no trusted business evidence index"
        )
    try:
        evidence_index_payload = _read_regular(
            archive_root / "evidence-index.json",
            limit=MAX_ORACLE_FILE_BYTES,
        )
        if (
            len(evidence_index_payload) != index_manifest_entry.size_bytes
            or not hmac.compare_digest(
                f"sha256:{hashlib.sha256(evidence_index_payload).hexdigest()}",
                index_manifest_entry.digest,
            )
        ):
            raise IndependentVerificationRejected(
                "business evidence index differs from the frozen archive"
            )
        evidence_index = ArchivedEvidenceIndex.model_validate_json(
            evidence_index_payload
        )
    except IndependentVerificationRejected:
        raise
    except Exception as error:
        raise IndependentVerificationRejected(
            "business evidence index is unavailable or invalid"
        ) from error
    if (
        evidence_index.task_id != task_id
        or evidence_index.revision != revision
        or evidence_index.run_id != replay.run_id
        or evidence_index.assignment_id != claim.assignment_id
        or evidence_index.application_id != claim.application_id
    ):
        raise IndependentVerificationRejected(
            "business evidence index belongs to another frozen claim"
        )
    indexed_by_key = {
        entry.evidence_key: entry for entry in evidence_index.entries
    }
    protected_markers = manager.protected_leak_markers(package)
    differences: list[VerificationDifference] = []
    evidence: list[EvidenceRef] = []
    business_evidence_checks = 0
    for check in oracle.checks:
        if check.evidence_selector is not None:
            indexed = indexed_by_key.get(
                check.evidence_selector.evidence_key
            )
            if (
                indexed is None
                or indexed.run_id not in claim.business_run_ids
            ):
                raise IndependentVerificationRejected(
                    "hidden oracle selector has no exact trusted business evidence"
                )
            archive_path = indexed.archive_path
            entry = archive_entries.get(archive_path)
            if (
                entry is None
                or not hmac.compare_digest(entry.digest, indexed.digest)
                or entry.size_bytes != indexed.size_bytes
            ):
                raise IndependentVerificationRejected(
                    "hidden oracle selector resolved outside the frozen evidence index"
                )
            business_evidence_checks += 1
        else:
            assert check.archive_path is not None
            archive_path = check.archive_path
            entry = archive_entries.get(archive_path)
        difference, item_evidence = _evaluate_check(
            archive_root,
            check,
            archive_path=archive_path,
            captured_at=replay.created_at,
            public_check_id=_public_check_id(
                check,
                private_salt=package.record.sealed_package_digest,
            ),
            manifest_digest=entry.digest if entry is not None else None,
            manifest_size=entry.size_bytes if entry is not None else None,
            protected_markers=protected_markers,
        )
        evidence.append(item_evidence)
        if difference is not None:
            differences.append(difference)
    if business_evidence_checks < 1:
        raise IndependentVerificationRejected(
            "independent verification requires trusted business evidence"
        )
    try:
        if not hmac.compare_digest(
            _read_regular(oracle_path, limit=MAX_ORACLE_FILE_BYTES),
            oracle_payload,
        ):
            raise IndependentVerificationRejected(
                "hidden oracle changed during verification"
            )
        manager.replay_registered_run(
            task_id,
            revision,
            replay.run_id,
            expected_manifest_digest=str(claim.archive_manifest_digest),
        )
    except (TaskPackageError, OSError) as error:
        raise IndependentVerificationRejected(
            "frozen verification inputs changed during evaluation"
        ) from error
    verdict = (
        VerificationVerdict.verification_failed
        if differences
        else VerificationVerdict.independently_verified
    )
    verification_id = uuid5(
        NAMESPACE_URL,
        (
            f"lilies:independent-verification:{claim.claim_id}:"
            f"{claim.frozen_context_digest}:"
            f"{hashlib.sha256(oracle_payload).hexdigest()}"
        ),
    )
    result = VerificationResultPayload(
        schema_version="1.1",
        verification_id=verification_id,
        verdict=verdict,
        oracle_digest=oracle_digest,
        differences=differences,
        evidence_refs=evidence,
        task_package_digest=claim.task_package_digest,
        environment_ready_digest=claim.environment_ready_digest,
        archive_manifest_digest=claim.archive_manifest_digest,
        frozen_context_digest=claim.frozen_context_digest,
        verification_process_digest=(
            verification_policy.verification_process_digest
        ),
        validation_mode="real_host",
    )
    result_payload = json.dumps(
        result.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if any(
        marker.encode("utf-8") in result_payload
        for marker in protected_markers
    ):
        raise IndependentVerificationRejected(
            "verification result would expose hidden oracle material"
        )
    return result


def _safe_output_path(value: Path, *, state_root: Path) -> Path:
    output = value.resolve(strict=False)
    protected_root = state_root.resolve()
    if output == protected_root or protected_root in output.parents:
        raise IndependentVerificationRejected(
            "verification output must be outside frozen input state"
        )
    if output.exists() or output.is_symlink():
        raise IndependentVerificationRejected(
            "verification output must be a new regular file"
        )
    return output


def _write_new_result(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise IndependentVerificationRejected(
            "verification result target already exists"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            view = memoryview(payload)
            while view:
                written = handle.write(view)
                if written is None or written <= 0:
                    raise OSError("verification result write made no progress")
                view = view[written:]
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lilies-verifier",
        description="Verify one frozen task archive without platform mutation authority.",
    )
    parser.add_argument("--version", action="version", version="Lilies Verifier 0.4.13")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--state-root", type=Path, required=True)
    verify.add_argument("--task-id", required=True)
    verify.add_argument("--revision", type=int, required=True)
    verify.add_argument("--claim-file", type=Path, required=True)
    verify.add_argument("--result-out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        claim_payload = _read_regular(
            args.claim_file,
            limit=MAX_ORACLE_FILE_BYTES,
        )
        claim = VerificationClaim.model_validate_json(claim_payload)
        result = verify_frozen_claim(
            state_root=args.state_root,
            task_id=args.task_id,
            revision=args.revision,
            claim=claim,
        )
        payload = json.dumps(
            result.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if args.result_out is None:
            sys.stdout.buffer.write(payload + b"\n")
        else:
            output = _safe_output_path(args.result_out, state_root=args.state_root)
            _write_new_result(output, payload)
            print(
                json.dumps(
                    {
                        "status": "verification_result_written",
                        "result_digest": (
                            f"sha256:{hashlib.sha256(payload).hexdigest()}"
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return 0
    except (
        IndependentVerificationError,
        TaskPackageError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "independent_verification_rejected",
                        "reason": type(error).__name__,
                    }
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
