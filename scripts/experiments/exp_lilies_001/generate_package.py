from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import tempfile
import zlib
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[3]
TASK_ID = "EXP-LILIES-001"
REVISION = 28
PARENT_REVISION = 27
CREATED_AT = "2026-07-28T09:33:00Z"
DEFAULT_OUTPUT = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / TASK_ID
    / str(REVISION)
)
PARENT_SOURCE_ROOT = DEFAULT_OUTPUT.parent / str(PARENT_REVISION)
BUILDER_API_MANUAL_FILE = "BUILDER_API_MANUAL.json"
CUSTOMER_REQUIREMENT_PACKAGE_FILE = "CUSTOMER_REQUIREMENT_PACKAGE.json"
AMENDMENT_REASON = (
    "Revision 28 preserves the revision-27 business requirement, source pins, official host "
    "operations, budgets, host boundary, public and hidden fixtures and PDFs, business records, "
    "oracle expected values, acceptance, model_access=false and max_write_count=18. It records "
    "the user's authorization that a fresh external Codex may act as the isolated Builder when "
    "needed, while the binding restriction remains that the Builder sees only the public customer "
    "requirement package, filtered workspace, Builder manual and public platform contract and "
    "uses only Lilies platform public APIs and functions. The adjacent revision also supplies a "
    "fresh assignment, collaboration channel, task credential and write-authorization budget "
    "after the predecessor environment reset. No project-specific mapping, adapter, wrapper, "
    "final graph, hidden answer, host data, oracle or evidence denominator is added."
)
COMPOSE_PATH = Path(__file__).with_name("compose.yaml")
RESULT_HEADERS = [
    "document_id",
    "source_id",
    "supplier",
    "purchase_order",
    "part_number",
    "lot_number",
    "quantity",
    "document_date",
    "certificate_type",
    "decision",
    "human_decision",
    "receipt_id",
    "failure_reason",
]
SOURCE_PROJECTS = [
    {
        "name": "paperless-ngx",
        "repository_url": "https://github.com/paperless-ngx/paperless-ngx",
        "release": "v2.20.15",
        "commit_sha": "05e48b23166df7c7afe6f329b460b0511a89496c",
        "image_digest": (
            "sha256:6c86cad803970ea782683a8e80e7403444c5bf3cf70de63b4d3c8e87500db92f"
        ),
        "license": "GPL-3.0",
    },
    {
        "name": "InvenTree",
        "repository_url": "https://github.com/inventree/InvenTree",
        "release": "1.4.2",
        "commit_sha": "0cd4b669931feaba2cc6f780b70b7a471e332a59",
        "image_digest": (
            "sha256:061ff0252e65077577cb86494c93b0b5b8c120f5a8011493fe0d502bc76ef42c"
        ),
        "license": "MIT",
    },
]
SCENARIO_COUNTS = {
    "debug": {
        "exact_match": 8,
        "low_confidence": 4,
        "duplicate": 4,
        "conflict": 4,
        "transient_error": 2,
        "permission_denied": 2,
    },
    "hidden": {
        "exact_match": 12,
        "low_confidence": 6,
        "duplicate": 6,
        "conflict": 6,
        "transient_error": 3,
        "permission_denied": 3,
    },
}
EXPECTED_DECISIONS = {
    "exact_match": "written",
    "low_confidence": "human_input_required",
    "duplicate": "duplicate_noop",
    "conflict": "conflict_blocked",
    "transient_error": "written_after_retry",
    "permission_denied": "permission_denied",
}
_FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "-": (0, 0, 0, 31, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 12, 12),
    "/": (1, 2, 4, 8, 16, 0, 0),
    ":": (0, 12, 12, 0, 12, 12, 0),
    "0": (14, 17, 19, 21, 25, 17, 14),
    "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2),
    "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14),
    "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14),
    "A": (14, 17, 17, 31, 17, 17, 17),
    "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14),
    "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31),
    "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15),
    "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14),
    "J": (7, 2, 2, 2, 18, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17),
    "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17),
    "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14),
    "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13),
    "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30),
    "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14),
    "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 21, 10),
    "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 10, 4, 4, 4, 4),
    "Z": (31, 1, 2, 4, 8, 16, 31),
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _write_json(path: Path, value: Any) -> bytes:
    payload = _canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _write_yaml(path: Path, value: Any) -> bytes:
    payload = yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _pdf(objects: list[bytes]) -> bytes:
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _text_pdf(lines: list[str]) -> bytes:
    commands = ["BT", "/F1 11 Tf", "52 742 Td", "14 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.extend([f"({escaped}) Tj", "T*"])
    commands.append("ET")
    stream = "\n".join(commands).encode()
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            ),
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


def _scan_pdf(lines: list[str], *, seed: int) -> bytes:
    width = 1700
    height = 1100
    scale = 4
    pixels = bytearray([255]) * (width * height)
    for line_index, line in enumerate(lines):
        y_origin = 70 + line_index * 70
        for character_index, character in enumerate(line.upper()[:65]):
            glyph = _FONT.get(character, _FONT[" "])
            x_origin = 70 + character_index * 24
            for row, bits in enumerate(glyph):
                for column in range(5):
                    if bits & (1 << (4 - column)):
                        for dy in range(scale):
                            for dx in range(scale):
                                x = x_origin + column * scale + dx
                                y = y_origin + row * scale + dy
                                if x < width and y < height:
                                    pixels[y * width + x] = 20
    randomizer = random.Random(seed)
    for _ in range(2_500):
        index = randomizer.randrange(len(pixels))
        pixels[index] = randomizer.choice((180, 210, 235, 255))
    compressed = zlib.compress(bytes(pixels), level=9)
    content = b"q\n612 0 0 396 0 198 cm\n/Im0 Do\nQ\n"
    image = (
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            "/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(compressed)} >>\nstream\n"
        ).encode()
        + compressed
        + b"\nendstream"
    )
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>"
            ),
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"endstream",
            image,
        ]
    )


def _scenario_sequence(kind: str) -> list[str]:
    return [
        scenario
        for scenario, count in SCENARIO_COUNTS[kind].items()
        for _ in range(count)
    ]


def _records(*, kind: str, seed: int) -> list[dict[str, Any]]:
    scenarios = _scenario_sequence(kind)
    suppliers = [
        "ALPHA INDUSTRIAL",
        "BETA COMPONENTS",
        "GAMMA METALS",
        "DELTA QUALITY",
    ]
    parts = ["AX-100", "BX-220", "CX-330", "DX-440", "EX-550", "FX-660"]
    records: list[dict[str, Any]] = []
    exact_records: list[dict[str, Any]] = []
    prefix = "PUB" if kind == "debug" else "HID"
    for index, scenario in enumerate(scenarios, start=1):
        randomizer = random.Random(seed * 10_000 + index)
        supplier = suppliers[(seed + index) % len(suppliers)]
        part = parts[(seed * 3 + index) % len(parts)]
        purchase_order = f"PO-{seed % 1000:03d}-{1000 + index:04d}"
        document_ref = f"DOC-{seed % 1000:03d}-{index:04d}"
        if scenario == "duplicate":
            original = exact_records[(index + seed) % len(exact_records)]
            document_ref = str(original["source_id"])
            supplier = str(original["supplier"])
            part = str(original["part_number"])
            purchase_order = str(original["host_purchase_order"])
        quantity = 10 + randomizer.randrange(1, 90)
        if scenario == "duplicate":
            quantity = int(original["quantity"])
        expected_quantity = quantity
        host_part = part
        if scenario == "conflict":
            expected_quantity = quantity + 7
            if index % 2 == 0:
                part = f"UNKNOWN-{index:03d}"
        low_field = None
        if scenario == "low_confidence":
            low_field = "purchase_order" if index % 2 == 0 else "quantity"
        lot_number = f"LOT-{seed % 100:02d}-{index:03d}"
        document_date = f"2026-0{1 + index % 8}-{1 + index % 27:02d}"
        certificate_type = (
            "QUALITY CERTIFICATE" if index % 2 else "SUPPLIER INVOICE"
        )
        if scenario == "duplicate":
            lot_number = str(original["lot_number"])
            document_date = str(original["document_date"])
            certificate_type = str(original["certificate_type"])
        record = {
            "record_id": f"{prefix}-{index:03d}",
            "source_id": document_ref,
            "supplier": supplier,
            "purchase_order": (
                None if low_field == "purchase_order" else purchase_order
            ),
            "host_purchase_order": purchase_order,
            "part_number": part,
            "host_part_number": host_part,
            "lot_number": lot_number,
            "quantity": None if low_field == "quantity" else quantity,
            "purchase_line_quantity": expected_quantity,
            "document_date": document_date,
            "certificate_type": certificate_type,
            "scenario": scenario,
            "ocr_confidence": 0.42 if scenario == "low_confidence" else 0.98,
            "expected_decision": EXPECTED_DECISIONS[scenario],
            "expected_host_write_count": (
                1 if scenario in {"exact_match", "transient_error"} else 0
            ),
            "expected_human_input_before_write": scenario == "low_confidence",
            "fault_key": (
                record_fault
                if (
                    record_fault := {
                        "transient_error": "inventree-temporary-503",
                        "permission_denied": "inventree-write-403",
                    }.get(scenario)
                )
                else None
            ),
            "render_mode": (
                "scan" if (index + seed) % 3 == 0 else "text_pdf"
            ),
        }
        records.append(record)
        if scenario == "exact_match":
            exact_records.append(record)
    return records


def _document_lines(record: dict[str, Any]) -> list[str]:
    lines = [
        record["certificate_type"],
        f"DOCUMENT REF: {record['source_id']}",
        f"SUPPLIER: {record['supplier']}",
        f"PURCHASE ORDER: {record['purchase_order'] or 'MISSING'}",
        f"PART NUMBER: {record['part_number']}",
        f"LOT NUMBER: {record['lot_number']}",
        f"QUANTITY: {record['quantity'] if record['quantity'] is not None else 'MISSING'}",
        f"DOCUMENT DATE: {record['document_date']}",
    ]
    if record["scenario"] == "duplicate":
        lines.append("DUPLICATE BUSINESS DOCUMENT COPY")
    return lines


def _write_documents(
    root: Path,
    *,
    records: list[dict[str, Any]],
    seed: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        payload = (
            _scan_pdf(_document_lines(record), seed=seed * 1_000 + index)
            if record["render_mode"] == "scan"
            else _text_pdf(_document_lines(record))
        )
        name = f"{record['record_id'].lower()}-{record['render_mode']}.pdf"
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        entries.append(
            {
                "path": name,
                "digest": _digest(payload),
                "size_bytes": len(payload),
            }
        )
    return entries


def _oracle(hidden_records: list[dict[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        {
            "check_id": "result-record-count",
            "kind": "json_length",
            "evidence_selector": {
                "kind": "artifact",
                "label": "enterprise-result.json",
            },
            "json_pointer": "/records",
            "expected": 36,
        },
        {
            "check_id": "result-forbidden-write-count",
            "kind": "json_equals",
            "evidence_selector": {
                "kind": "artifact",
                "label": "enterprise-result.json",
            },
            "json_pointer": "/forbidden_write_count",
            "expected": 0,
        },
        {
            "check_id": "workbook-sheet",
            "kind": "xlsx_sheet_exists",
            "evidence_selector": {
                "kind": "artifact",
                "label": "reconciliation.xlsx",
            },
            "sheet_name": "Reconciliation",
        },
        {
            "check_id": "workbook-headers",
            "kind": "xlsx_headers",
            "evidence_selector": {
                "kind": "artifact",
                "label": "reconciliation.xlsx",
            },
            "sheet_name": "Reconciliation",
            "expected": RESULT_HEADERS,
        },
        {
            "check_id": "workbook-row-count",
            "kind": "xlsx_row_count",
            "evidence_selector": {
                "kind": "artifact",
                "label": "reconciliation.xlsx",
            },
            "sheet_name": "Reconciliation",
            "expected": 36,
        },
        {
            "check_id": "workbook-first-record",
            "kind": "xlsx_cell_equals",
            "evidence_selector": {
                "kind": "artifact",
                "label": "reconciliation.xlsx",
            },
            "sheet_name": "Reconciliation",
            "cell_reference": "A2",
            "expected": "HID-001",
        },
        {
            "check_id": "workbook-last-record",
            "kind": "xlsx_cell_equals",
            "evidence_selector": {
                "kind": "artifact",
                "label": "reconciliation.xlsx",
            },
            "sheet_name": "Reconciliation",
            "cell_reference": "A37",
            "expected": "HID-036",
        },
    ]
    for index, record in enumerate(hidden_records):
        checks.extend(
            [
                {
                    "check_id": f"record-{index + 1:03d}-identity",
                    "kind": "json_equals",
                    "evidence_selector": {
                        "kind": "artifact",
                        "label": "enterprise-result.json",
                    },
                    "json_pointer": f"/records/{index}/record_id",
                    "expected": record["record_id"],
                },
                {
                    "check_id": f"record-{index + 1:03d}-decision",
                    "kind": "json_equals",
                    "evidence_selector": {
                        "kind": "artifact",
                        "label": "enterprise-result.json",
                    },
                    "json_pointer": f"/records/{index}/decision",
                    "expected": record["expected_decision"],
                },
            ]
        )
    return {
        "schema_version": "1.0",
        "oracle_id": "exp-lilies-001-enterprise-reconciliation-v1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "validation_mode": "real_host",
        "checks": checks,
    }


def _host_oracle(hidden_records: list[dict[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        {
            "check_id": "host-state-record-count",
            "kind": "json_length",
            "json_pointer": "/records",
            "expected": 36,
        },
        {
            "check_id": "host-state-duplicate-effects",
            "kind": "json_equals",
            "json_pointer": "/duplicate_effect_count",
            "expected": 0,
        },
        {
            "check_id": "host-state-forbidden-writes",
            "kind": "json_equals",
            "json_pointer": "/forbidden_write_count",
            "expected": 0,
        },
    ]
    for index, record in enumerate(hidden_records):
        checks.append(
            {
                "check_id": f"record-{index + 1:03d}-host-write-count",
                "kind": "json_equals",
                "json_pointer": f"/records/{index}/write_count",
                "expected": record["expected_host_write_count"],
                "record_id": record["record_id"],
                "scenario": record["scenario"],
            }
        )
    return {
        "schema_version": "1.0",
        "oracle_id": "exp-lilies-001-independent-host-state-v1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "validation_mode": "real_host",
        "snapshot_phase": "final",
        "checks": checks,
    }


def _generate_source_from_records(output: Path) -> None:
    if output.exists():
        raise FileExistsError(
            f"refusing to replace an existing task revision: {output}"
        )
    if not COMPOSE_PATH.is_file():
        raise FileNotFoundError(COMPOSE_PATH)
    output.mkdir(parents=True)
    debug_records = _records(kind="debug", seed=17)
    public_root = output / "fixtures" / "public-inputs"
    public_entries = _write_documents(
        public_root / "documents",
        records=debug_records,
        seed=17,
    )
    debug_payload = _write_json(
        public_root / "debug-records.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "records": debug_records,
        },
    )
    fixture_entries = [
        {
            "path": f"public-inputs/documents/{entry['path']}",
            "digest": entry["digest"],
            "size_bytes": entry["size_bytes"],
        }
        for entry in public_entries
    ]
    fixture_entries.append(
        {
            "path": "public-inputs/debug-records.json",
            "digest": _digest(debug_payload),
            "size_bytes": len(debug_payload),
        }
    )
    fixture_entries.sort(key=lambda item: item["path"])
    fixture_manifest_payload = _write_json(
        output / "fixtures" / "manifest.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "files": fixture_entries,
        },
    )

    hidden_root = output / "protected" / "hidden-inputs"
    expected_root = output / "protected" / "expected-state"
    hidden_by_seed: dict[str, list[dict[str, Any]]] = {}
    for seed in (101, 202, 303):
        records = _records(kind="hidden", seed=seed)
        hidden_by_seed[str(seed)] = records
        document_entries = _write_documents(
            hidden_root / str(seed) / "documents",
            records=records,
            seed=seed,
        )
        _write_json(
            hidden_root / str(seed) / "seed-plan.json",
            {
                "schema_version": "1.0",
                "task_id": TASK_ID,
                "revision": REVISION,
                "seed": seed,
                "records": records,
                "documents": document_entries,
            },
        )
        _write_json(
            expected_root / f"seed-{seed}.json",
            {
                "schema_version": "1.0",
                "task_id": TASK_ID,
                "revision": REVISION,
                "seed": seed,
                "records": [
                    {
                        "record_id": record["record_id"],
                        "source_id": record["source_id"],
                        "scenario": record["scenario"],
                        "decision": record["expected_decision"],
                        "write_count": record["expected_host_write_count"],
                        "human_input_before_write": record[
                            "expected_human_input_before_write"
                        ],
                    }
                    for record in records
                ],
            },
        )
    _write_json(
        output / "protected" / "oracle" / "oracle.json",
        _oracle(hidden_by_seed["101"]),
    )
    _write_json(
        output / "protected" / "oracle" / "host-oracle.json",
        _host_oracle(hidden_by_seed["101"]),
    )
    _write_json(
        output / "protected" / "leak-markers.json",
        {
            "markers": [
                "EXP-LILIES-001-ORACLE-CANARY-9D2F6A31",
                "EXP-LILIES-001-EXPECTED-STATE-PRIVATE",
            ]
        },
    )

    compose_digest = _digest(COMPOSE_PATH.read_bytes())
    initialize_command = (
        "uv run --frozen python "
        "scripts/experiments/exp_lilies_001/environment_control.py initialize"
    ).encode()
    proxy_command = (
        "uv run --frozen python "
        "scripts/experiments/exp_lilies_001/fault_proxy.py"
    ).encode()
    attestation_command = (
        "uv run --frozen python "
        "scripts/experiments/exp_lilies_001/attestation_server.py"
    ).encode()
    provision_script = Path(__file__).with_name(
        "provision_scoped_account.py"
    ).read_bytes()
    seed_command = (
        "uv run --frozen python "
        "scripts/experiments/exp_lilies_001/environment_control.py seed"
    ).encode()
    activate_command = (
        "uv run --frozen python "
        "scripts/experiments/exp_lilies_001/environment_control.py fault-activate"
    ).encode()
    recover_command = (
        "uv run --frozen python "
        "scripts/experiments/exp_lilies_001/environment_control.py fault-recover"
    ).encode()
    environment = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "source_projects": SOURCE_PROJECTS,
        "compose_digest": compose_digest,
        "ports": [
            {"service": "paperless", "host": "127.0.0.1", "port": 18000},
            {"service": "inventree", "host": "127.0.0.1", "port": 18001},
            {
                "service": "environment-attestation",
                "host": "127.0.0.1",
                "port": 18002,
            },
            {
                "service": "paperless-fault-proxy",
                "host": "127.0.0.1",
                "port": 18010,
            },
            {
                "service": "inventree-fault-proxy",
                "host": "127.0.0.1",
                "port": 18011,
            },
        ],
        "network_name": "exp-lilies-001-r7",
        "volumes": [
            "paperless-redis",
            "paperless-db",
            "paperless-data",
            "paperless-media",
            "inventree-db",
            "inventree-redis",
            "inventree-data",
        ],
        "initialization_commands": [
            {"name": "initialize-hosts", "digest": _digest(initialize_command)},
            {"name": "launch-fault-proxies", "digest": _digest(proxy_command)},
            {
                "name": "launch-environment-attestation",
                "digest": _digest(attestation_command),
            },
            {
                "name": "provision-scoped-host-accounts",
                "digest": _digest(provision_script),
            },
        ],
        "seed_commands": [
            {"name": "seed-public-and-hidden", "digest": _digest(seed_command)}
        ],
        "health_checks": [
            {
                "check_id": "tcp:paperless",
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": 18000,
                "timeout_seconds": 5.0,
                "mandatory": True,
            },
            {
                "check_id": "tcp:inventree",
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": 18001,
                "timeout_seconds": 5.0,
                "mandatory": True,
            },
            {
                "check_id": "tcp:paperless-fault-proxy",
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": 18010,
                "timeout_seconds": 5.0,
                "mandatory": True,
            },
            {
                "check_id": "tcp:inventree-fault-proxy",
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": 18011,
                "timeout_seconds": 5.0,
                "mandatory": True,
            },
            {
                "check_id": "identity:exp-lilies-001",
                "kind": "http",
                "url": "http://127.0.0.1:18002/identity",
                "expected_status": 200,
                "expected_body_digest": _digest(
                    b'{"identity":"exp-lilies-001-r7-real-hosts"}'
                ),
                "timeout_seconds": 5.0,
                "mandatory": True,
            },
        ],
        "secret_refs": [
            "secret:exp-lilies-001-environment-attestation",
            "secret:exp-lilies-001-paperless-builder-token",
            "secret:exp-lilies-001-inventree-builder-token",
            "secret:exp-lilies-001-paperless-verifier-token",
            "secret:exp-lilies-001-inventree-verifier-token",
        ],
        "attestation_secret_ref": (
            "secret:exp-lilies-001-environment-attestation"
        ),
        "python_version": "3.12.13",
        "node_version": "24.15.0",
        "docker_version": "29.4.0",
        "fixture_files": fixture_entries,
        "fault_injections": [
            {
                "name": "inventree-temporary-503",
                "activation_command_digest": _digest(activate_command),
                "recovery_command_digest": _digest(recover_command),
            },
            {
                "name": "inventree-write-403",
                "activation_command_digest": _digest(
                    activate_command + b":permission"
                ),
                "recovery_command_digest": _digest(
                    recover_command + b":permission"
                ),
            },
        ],
        "provenance": "real_host",
    }
    environment_payload = _write_yaml(output / "environment.lock", environment)
    _write_json(
        output / "allowed-actions.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "readable_host_objects": [
                "paperless.documents_list",
                "paperless.documents_retrieve",
                "paperless.documents_metadata_retrieve",
                "paperless.custom_fields_list",
                "paperless.tags_list",
                "paperless.tasks_list",
                "inventree.company_list",
                "inventree.part_list",
                "inventree.company_part_list",
                "inventree.order_po_list",
                "inventree.order_po_line_list",
                "inventree.attachment_list",
                "inventree.metadata_pk_retrieve",
            ],
            "writable_host_operations": [
                "paperless.documents_partial_update",
                "inventree.attachment_create",
                "inventree.metadata_pk_partial_update",
            ],
            "platform_actions": [
                "platform_contract_get",
                "platform_block_search",
                "platform_block_get",
                "platform_tool_catalog",
                "platform_application_create",
                "platform_application_get",
                "platform_draft_inspect",
                "platform_draft_apply",
                "platform_tests_run",
                "platform_run_start",
                "platform_run_get",
                "platform_run_resume",
                "platform_run_cancel",
                "platform_trace_get",
                "platform_artifact_read",
                "platform_publish",
            ],
            "network_hosts": ["127.0.0.1"],
            "model_access": False,
            "file_access": True,
            "connector_access": True,
            "permission_required_actions": [
                "paperless.documents_partial_update",
                "inventree.attachment_create",
                "inventree.metadata_pk_partial_update",
            ],
            "max_write_count": 18,
            "max_payload_bytes": 4_194_304,
            "compensation_actions": [
                "inventree.attachment_destroy",
                "inventree.metadata_pk_update",
            ],
            "prohibited_actions": [
                "read_platform_source",
                "read_platform_database",
                "read_protected",
                "modify_task_package",
                "install_unknown_adapter",
            ],
            "validation_mode": "real_host",
        },
    )
    _write_json(
        output / "budget.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "max_build_repair_turns": 120,
            "max_model_cost_usd": 20.0,
            "assignment_wall_clock_seconds": 10_800,
            "max_platform_tool_calls": 800,
            "max_report_evidence_rounds": 3,
            "stable_hidden_runs": 3,
        },
    )
    (output / "requirement.md").write_text(
        (
            "# 供应商文档入库、采购匹配与库存系统交接\n\n"
            "当 Paperless-ngx 出现新的供应商发票或质量证明时，读取文档和 "
            "OCR 文本，提取供应商、采购单号、物料号、批次、数量、日期和证书"
            "类型；与 InvenTree 中的供应商、物料和采购记录匹配。\n\n"
            "明确匹配且规则通过的记录写入受控元数据并建立可追溯关联；缺字段、"
            "低置信度、数量冲突或未知物料必须暂停给采购或质量人员选择，不得猜测"
            "或写入。重复业务文档不得产生重复副作用。临时错误只能有界重试，权限"
            "错误必须明确归类，不能误报为平台能力缺口。\n\n"
            "最终生成 `reconciliation.xlsx` 和机器可读 `enterprise-result.json`，"
            "保存每条记录的来源、判断、人工决定、写回收据和失败原因。工作流必须"
            "仅使用平台公开合同自动发现并对齐接口，不得依赖预写宿主适配器、字段"
            "映射或人工修改最终图。\n"
            "\n"
            "InvenTree 1.4.2 的正式关联声明上限是 link-only 外部关联，不是"
            "二进制文件复制。`attachment_create` 只使用 JSON "
            "`{model_type: \"purchaseorder\", model_id: integer >= 0, "
            "link: URI (max 2000), comment?: string (max 250), tags?: string[]}`，"
            "成功返回 201 Attachment；`attachment_list` 只按 "
            "`model_type`、`model_id`、`is_link`、`limit` 过滤，"
            "`attachment_destroy` 仅作为按 attachment id 删除并返回 204 的补偿。"
            "metadata 操作固定使用 `/api/metadata/purchaseorder/{id}/`：GET、"
            "PUT、PATCH 的请求或响应合同均为 `{metadata: object}`；PATCH 是"
            "顶层浅合并，PUT 是全量覆盖。冻结宿主的 live OpenAPI 未给出这些 "
            "metadata content schema，因此只能依据该官方合同通过平台通用 "
            "operation-contract overlay 补齐，不能引入 InvenTree 专用映射、"
            "adapter 或 wrapper。\n"
        ),
        encoding="utf-8",
    )
    parent_manual = PARENT_SOURCE_ROOT / BUILDER_API_MANUAL_FILE
    if not parent_manual.is_file():
        raise FileNotFoundError(parent_manual)
    manual_payload = parent_manual.read_bytes()
    if _canonical_json(json.loads(manual_payload)) != manual_payload:
        raise ValueError(
            "revision-25 Builder API manual is not canonical JSON"
        )
    (output / BUILDER_API_MANUAL_FILE).write_bytes(manual_payload)
    _write_yaml(
        output / "task.yaml",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "title": "供应商文档入库、采购匹配与库存系统交接",
            "cohort": "enterprise",
            "customer_role": "制造企业采购运营人员和质量文件审核员",
            "business_goal": (
                "自动处理供应商发票与质检证明，匹配采购和物料，安全分流异常，"
                "执行受治理写回并生成可审计 Excel 对账单。"
            ),
            "source_projects": SOURCE_PROJECTS,
            "requirement_file": "requirement.md",
            "environment_lock_digest": _digest(environment_payload),
            "fixture_manifest_digest": _digest(fixture_manifest_payload),
            "allowed_actions_file": "allowed-actions.json",
            "budget_file": "budget.json",
            "deliverables": [
                {
                    "name": "enterprise-result",
                    "description": (
                        "每条记录的来源、决策、人工决定、回执和错误状态。"
                    ),
                    "media_type": "application/json",
                },
                {
                    "name": "reconciliation-workbook",
                    "description": "可打开且与真实宿主最终状态一致的 Excel 对账单。",
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                },
            ],
            "acceptance_summary": (
                "全部输入均有可追溯结果；明确匹配安全写回，低置信度和冲突暂停，"
                "重复不产生副作用，临时错误有界恢复，权限错误明确；Excel 与宿主"
                "状态一致。"
            ),
            "no_substitute_validation": True,
            "collaboration_enabled": True,
            "author": "codex-task-author",
            "created_at": CREATED_AT,
            "parent_revision": PARENT_REVISION,
            "amendment_reason": AMENDMENT_REASON,
        },
    )


def _rewrite_json_revision(path: Path) -> bytes:
    value = json.loads(path.read_bytes())
    if value.get("task_id") != TASK_ID or value.get("revision") != PARENT_REVISION:
        raise ValueError(f"parent wrapper identity changed: {path}")
    value["revision"] = REVISION
    return _write_json(path, value)


def generate(output: Path) -> None:
    """Create revision 28 from revision 27 with explicit authoring semantics."""

    if output.exists() or output.is_symlink():
        raise FileExistsError(
            f"refusing to replace an existing task revision: {output}"
        )
    if not PARENT_SOURCE_ROOT.is_dir() or PARENT_SOURCE_ROOT.is_symlink():
        raise FileNotFoundError(PARENT_SOURCE_ROOT)
    parent_task = yaml.safe_load(
        (PARENT_SOURCE_ROOT / "task.yaml").read_bytes()
    )
    if (
        parent_task.get("task_id") != TASK_ID
        or parent_task.get("revision") != PARENT_REVISION
    ):
        raise ValueError("revision-27 parent task identity changed")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{REVISION}.",
            dir=output.parent,
        )
    )
    try:
        shutil.copytree(
            PARENT_SOURCE_ROOT,
            temporary,
            copy_function=shutil.copyfile,
            dirs_exist_ok=True,
        )

        fixture_manifest_payload = _rewrite_json_revision(
            temporary / "fixtures" / "manifest.json"
        )
        environment = yaml.safe_load(
            (temporary / "environment.lock").read_bytes()
        )
        if (
            environment.get("task_id") != TASK_ID
            or environment.get("revision") != PARENT_REVISION
        ):
            raise ValueError("revision-27 environment identity changed")
        environment["revision"] = REVISION
        environment_payload = _write_yaml(
            temporary / "environment.lock",
            environment,
        )

        for relative in (
            "budget.json",
            "protected/oracle/oracle.json",
            "protected/oracle/host-oracle.json",
            "protected/hidden-inputs/101/seed-plan.json",
            "protected/hidden-inputs/202/seed-plan.json",
            "protected/hidden-inputs/303/seed-plan.json",
            "protected/expected-state/seed-101.json",
            "protected/expected-state/seed-202.json",
            "protected/expected-state/seed-303.json",
            CUSTOMER_REQUIREMENT_PACKAGE_FILE,
        ):
            _rewrite_json_revision(temporary / relative)

        allowed = json.loads(
            (temporary / "allowed-actions.json").read_bytes()
        )
        if (
            allowed.get("task_id") != TASK_ID
            or allowed.get("revision") != PARENT_REVISION
            or allowed.get("platform_actions", []).count(
                "platform_connector_authorization_issue"
            )
            != 1
        ):
            raise ValueError("revision-27 allowed actions changed")
        allowed["revision"] = REVISION
        _write_json(temporary / "allowed-actions.json", allowed)

        manual = json.loads(
            (temporary / BUILDER_API_MANUAL_FILE).read_bytes()
        )
        platform_manual = manual.get("platform")
        if (
            manual.get("schema_version")
            != "v0.4.13-t01h-external-builder-api-manual-1"
            or not isinstance(platform_manual, dict)
            or platform_manual.get("operation_count") != 17
            or not isinstance(
                platform_manual.get("connector_authorization"),
                dict,
            )
        ):
            raise ValueError("revision-27 Builder API manual changed")

        customer_package_path = (
            temporary / CUSTOMER_REQUIREMENT_PACKAGE_FILE
        )
        customer_package = json.loads(customer_package_path.read_bytes())
        if (
            customer_package.get("task_id") != TASK_ID
            or customer_package.get("revision") != REVISION
            or customer_package.get("material_completeness") != "partial"
            or len(customer_package.get("missing_materials", [])) != 3
        ):
            raise ValueError(
                "revision-27 customer requirement package changed"
            )
        customer_package["clarification_policy"] = (
            "Customer non-provision is not automatically a task gap. The "
            "Builder must author the final workflow and delivery workbook, "
            "and must derive a candidate field mapping from the supplied "
            "customer materials plus the public official connector schemas. "
            "Report a task-specification gap only when one concrete required "
            "fact remains genuinely ambiguous after that derivation and the "
            "ambiguity would make a governed write unsafe."
        )
        customer_package["missing_materials"] = [
            (
                "No customer-authored field mapping is supplied; derive a "
                "candidate from customer materials and official schemas."
            ),
            (
                "No customer-authored final workflow is supplied because the "
                "workflow is a required Builder deliverable."
            ),
            (
                "No customer-authored workbook template is supplied; generate "
                "the workbook from the required deliverable fields."
            ),
        ]
        _write_json(customer_package_path, customer_package)

        task = yaml.safe_load((temporary / "task.yaml").read_bytes())
        task["revision"] = REVISION
        task["parent_revision"] = PARENT_REVISION
        task["created_at"] = CREATED_AT
        task["amendment_reason"] = AMENDMENT_REASON
        task["environment_lock_digest"] = _digest(environment_payload)
        task["fixture_manifest_digest"] = _digest(fixture_manifest_payload)
        _write_yaml(temporary / "task.yaml", task)

        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate immutable EXP-LILIES-001 revision-twenty-eight source files."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    generate(args.output.resolve())
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
