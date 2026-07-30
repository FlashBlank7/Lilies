#!/usr/bin/env python3
"""Run one Builder-hidden EXP-LILIES-005 Seed and emit aggregates only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import random
import select
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree


TASK_ID = "EXP-LILIES-005"
BUDGET_NAME = "My Finances"
ACTUAL_SERVER_URL = "http://127.0.0.1:18050"
PROGRAM_SERVER_URL = "http://host.docker.internal:18050"
XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: float = 120,
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} returned HTTP {error.code}: {detail}"
        ) from error
    return json.loads(payload) if payload else None


def platform_json(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    return http_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
    )


def rotate_actual_password(
    *,
    container: str,
    base_url: str,
    platform_token: str,
    application_id: str,
    additional_application_ids: list[str] | None = None,
) -> str:
    password = secrets.token_urlsafe(32)

    def type_password(master_fd: int) -> None:
        for character in f"{password}\r":
            os.write(master_fd, character.encode())
            time.sleep(0.01)

    master_fd, slave_fd = pty.openpty()
    reset = subprocess.Popen(
        [
            "docker",
            "exec",
            "-it",
            container,
            "node",
            "/app/src/scripts/reset-password.js",
        ],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    transcript = bytearray()
    password_submissions = 0
    reset_succeeded = False
    deadline = time.monotonic() + 30
    try:
        while reset.poll() is None and time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if not readable:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            transcript.extend(chunk)
            prompt = bytes(transcript).lower()
            if b"currently logged in" in prompt:
                reset_succeeded = True
                break
            if (
                password_submissions == 0
                and b"press enter" in prompt
            ):
                type_password(master_fd)
                password_submissions = 1
                transcript.clear()
            elif (
                password_submissions == 1
                and (
                    b"confirm" in prompt
                    or b"again" in prompt
                    or b"press enter" in prompt
                )
            ):
                type_password(master_fd)
                password_submissions = 2
                transcript.clear()
        if reset_succeeded and reset.poll() is None:
            reset.terminate()
            reset.wait(timeout=5)
        elif reset.poll() is None:
            reset.terminate()
            reset.wait(timeout=5)
            tail = (
                bytes(transcript)
                .decode("utf-8", errors="replace")
                .replace(password, "[REDACTED]")[-240:]
            )
            raise RuntimeError(
                "Actual password rotation timed out "
                f"after {password_submissions} submissions: {tail!r}"
            )
    finally:
        os.close(master_fd)
    if (
        not reset_succeeded
        and (reset.returncode != 0 or password_submissions == 0)
    ):
        raise RuntimeError("Actual password rotation failed")
    combined = bytes(transcript).decode("utf-8", errors="replace").casefold()
    if "unexpected error" in combined:
        raise RuntimeError("Actual password rotation reported an error")
    for owner_id in dict.fromkeys(
        [application_id, *(additional_application_ids or [])]
    ):
        platform_json(
            "POST",
            base_url,
            platform_token,
            "/api/v1/platform/secrets",
            {
                "owner_id": owner_id,
                "name": "actual-password",
                "value": password,
                "description": (
                    "Rotated for sealed EXP-LILIES-005 acceptance."
                ),
            },
        )
    return password


class ActualCli:
    def __init__(
        self,
        *,
        executable: Path,
        password: str,
        data_dir: Path,
    ) -> None:
        self.executable = executable
        self.password = password
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sync_id: str | None = None

    def run(self, *arguments: str, stdin: Any | None = None) -> Any:
        environment = {
            **os.environ,
            "ACTUAL_SERVER_URL": ACTUAL_SERVER_URL,
            "ACTUAL_PASSWORD": self.password,
        }
        if self.sync_id is not None:
            environment["ACTUAL_SYNC_ID"] = self.sync_id
        encoded_stdin = None
        if stdin is not None:
            encoded_stdin = json.dumps(
                stdin,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        completed = subprocess.run(
            [
                str(self.executable),
                "--data-dir",
                str(self.data_dir),
                "--format",
                "json",
                *arguments,
            ],
            input=encoded_stdin,
            text=True,
            capture_output=True,
            env=environment,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Actual CLI command failed without exposing protected output"
            )
        stdout = completed.stdout.strip()
        return json.loads(stdout) if stdout else None


def discover_budget(cli: ActualCli) -> str:
    budgets = cli.run("budgets", "list")
    matches: dict[str, dict[str, Any]] = {}
    for budget in budgets:
        if budget.get("name") != BUDGET_NAME:
            continue
        sync_id = str(budget["groupId"])
        prior = matches.get(sync_id)
        if prior is None or budget.get("state") == "remote":
            matches[sync_id] = budget
    if len(matches) != 1:
        raise RuntimeError("sealed verifier could not uniquely discover budget")
    return next(iter(matches))


def source_digest(seed: int, category: str) -> str:
    return hashlib.sha256(f"{seed}:{category}".encode()).hexdigest()


def receipt(
    *,
    seed: int,
    receipt_id: str,
    date: str,
    amount: int,
    merchant: str,
    category: str,
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "receipt_imported_id": f"receipt:{receipt_id}",
        "date": date,
        "amount_cents": amount,
        "ledger_amount_cents": -amount,
        "merchant_raw": merchant.title(),
        "merchant_normalized": merchant,
        "category_hint": category,
        "source_sha256": source_digest(seed, receipt_id),
    }


def build_seed(
    seed: int,
    run_tag: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    day = 2 + seed % 8

    def date(offset: int) -> str:
        return f"2026-07-{day + offset:02d}"

    def cents(low: int, high: int) -> int:
        return rng.randrange(low, high)

    prefix = f"S{seed}-{run_tag[-6:]}"
    identifiers = {
        name: f"{prefix}-{name}"
        for name in (
            "matched",
            "ambiguous",
            "conflict",
            "marker",
            "identity",
            "new-approved",
            "new-rejected",
        )
    }
    amounts = {
        name: cents(1200 + index * 100, 8800 + index * 100)
        for index, name in enumerate(identifiers)
    }
    merchants = {
        "matched": f"OFFICE NORTH {seed}",
        "ambiguous": f"CAFE CENTRAL {seed}",
        "conflict": f"RAIL REGIONAL {seed}",
        "marker": f"CLOUD SERVICE {seed}",
        "identity": f"MARKET SOUTH {seed}",
        "new-approved": "SOFTWARE MART",
        "new-rejected": f"UNKNOWN VENDOR {seed}",
    }
    categories = {
        "matched": "Office Supplies",
        "ambiguous": "Meals",
        "conflict": "Travel",
        "marker": "Software",
        "identity": "Meals",
        "new-approved": "Software",
        "new-rejected": "Uncategorized Review",
    }
    receipts = [
        receipt(
            seed=seed,
            receipt_id=identifiers[name],
            date=date(index),
            amount=amounts[name],
            merchant=merchants[name],
            category=categories[name],
        )
        for index, name in enumerate(identifiers)
    ]
    receipts.insert(6, dict(receipts[0]))
    seeded_transactions = [
        {
            "date": "2026-07-01",
            "amount": 300000,
            "payee_name": f"Owner Capital {seed}",
            "imported_payee": f"OWNER CAPITAL {seed}",
            "imported_id": f"bank:{prefix}:capital",
            "notes": "controlled opening balance",
            "cleared": True,
        },
        {
            "date": date(0),
            "amount": -amounts["matched"],
            "payee_name": merchants["matched"].title(),
            "imported_payee": merchants["matched"],
            "imported_id": f"bank:{prefix}:matched",
            "notes": "awaiting receipt",
            "cleared": True,
        },
        {
            "date": date(1),
            "amount": -amounts["ambiguous"],
            "payee_name": merchants["ambiguous"].title(),
            "imported_payee": merchants["ambiguous"],
            "imported_id": f"bank:{prefix}:ambiguous-a",
            "notes": "candidate a",
            "cleared": True,
        },
        {
            "date": date(1),
            "amount": -amounts["ambiguous"],
            "payee_name": merchants["ambiguous"].title(),
            "imported_payee": merchants["ambiguous"],
            "imported_id": f"bank:{prefix}:ambiguous-b",
            "notes": "candidate b",
            "cleared": True,
        },
        {
            "date": date(2),
            "amount": -(amounts["conflict"] + 111),
            "payee_name": merchants["conflict"].title(),
            "imported_payee": merchants["conflict"],
            "imported_id": f"bank:{prefix}:conflict",
            "notes": "bank amount retained",
            "cleared": True,
        },
        {
            "date": date(3),
            "amount": -amounts["marker"],
            "payee_name": merchants["marker"].title(),
            "imported_payee": merchants["marker"],
            "imported_id": f"bank:{prefix}:marker",
            "notes": f"receipt:{identifiers['marker']}",
            "cleared": True,
        },
        {
            "date": date(4),
            "amount": -amounts["identity"],
            "payee_name": merchants["identity"].title(),
            "imported_payee": merchants["identity"],
            "imported_id": f"receipt:{identifiers['identity']}",
            "notes": "stable identity without receipt note",
            "cleared": True,
        },
    ]
    decisions = [
        {
            "receipt_id": identifiers["matched"],
            "approved": True,
            "reason": "sealed approval",
        },
        {
            "receipt_id": identifiers["ambiguous"],
            "approved": True,
            "selected_imported_id": f"bank:{prefix}:ambiguous-b",
            "reason": "sealed candidate selection",
        },
        {
            "receipt_id": identifiers["new-approved"],
            "approved": True,
            "reason": "sealed approval",
        },
        {
            "receipt_id": identifiers["new-rejected"],
            "approved": False,
            "reason": "sealed rejection",
        },
    ]
    return receipts, seeded_transactions, decisions


def run_workflow(
    *,
    base_url: str,
    token: str,
    application_id: str,
    version: int,
    inputs: dict[str, Any],
    workspace_path: str,
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    created = platform_json(
        "POST",
        base_url,
        token,
        f"/api/v1/applications/{application_id}/runs",
        {
            "inputs": inputs,
            "version": version,
            "workspace_path": workspace_path,
        },
    )
    run_id = str(created["run_id"])
    resumes = 0
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        run = platform_json(
            "GET",
            base_url,
            token,
            f"/api/v1/runs/{run_id}",
        )
        if run["status"] == "paused":
            platform_json(
                "POST",
                base_url,
                token,
                f"/api/v1/runs/{run_id}/resume",
                {"values": {"decisions": decisions}},
            )
            resumes += 1
        elif run["status"] not in {"queued", "running"}:
            run["run_id"] = run_id
            return run, resumes
        time.sleep(0.1)
    raise TimeoutError("sealed workflow run timed out")


def cells_by_reference(xlsx_path: Path, sheet_number: int) -> dict[str, str | None]:
    with zipfile.ZipFile(xlsx_path) as archive:
        root = ElementTree.fromstring(
            archive.read(f"xl/worksheets/sheet{sheet_number}.xml")
        )
    return {
        str(cell.attrib["r"]): cell.attrib.get("t")
        for cell in root.findall(".//main:c", XLSX_NS)
    }


def validate_xlsx(xlsx_path: Path, row_count: int) -> bool:
    try:
        with zipfile.ZipFile(xlsx_path) as archive:
            names = set(archive.namelist())
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            formulas = any(
                ElementTree.fromstring(archive.read(name)).find(
                    ".//main:f", XLSX_NS
                )
                is not None
                for name in names
                if name.startswith("xl/worksheets/sheet")
                and name.endswith(".xml")
            )
        sheets = workbook.findall(".//main:sheet", XLSX_NS)
        reconciliation = cells_by_reference(xlsx_path, 1)
        summary = cells_by_reference(xlsx_path, 2)
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError):
        return False
    typed_rows = all(
        reconciliation.get(f"B{row}") is None
        and reconciliation.get(f"C{row}") is None
        and reconciliation.get(f"D{row}") is None
        and reconciliation.get(f"F{row}") == "b"
        and reconciliation.get(f"G{row}") == "b"
        for row in range(2, row_count + 2)
    )
    return (
        len(sheets) == 2
        and len(reconciliation) >= row_count * 7
        and len(summary) >= 10
        and typed_rows
        and not formulas
    )


def transaction_identity(transaction: dict[str, Any]) -> tuple[Any, ...]:
    return (
        transaction.get("id"),
        transaction.get("date"),
        transaction.get("amount"),
        transaction.get("imported_id"),
        transaction.get("notes"),
        transaction.get("category"),
    )


def find_receipt(
    receipts: list[dict[str, Any]],
    suffix: str,
) -> dict[str, Any]:
    matches = {
        str(item.get("receipt_id")): item
        for item in receipts
        if str(item.get("receipt_id", "")).endswith(suffix)
    }
    if len(matches) != 1:
        raise RuntimeError("sealed receipt class was not unique")
    return next(iter(matches.values()))


def host_business_outcomes(
    *,
    transactions: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> bool:
    category_ids = {
        str(item.get("name")): item.get("id") for item in categories
    }
    expectations = (
        ("matched", "Office Supplies"),
        ("ambiguous", "Meals"),
        ("new-approved", "Software"),
    )
    for suffix, category_name in expectations:
        target = find_receipt(receipts, suffix)
        marker = str(target["receipt_imported_id"])
        marked = [
            item
            for item in transactions
            if item.get("notes") == marker
        ]
        if (
            len(marked) != 1
            or marked[0].get("category") != category_ids.get(category_name)
        ):
            return False
        if suffix == "ambiguous":
            selected = next(
                (
                    item.get("selected_imported_id")
                    for item in decisions
                    if item.get("receipt_id") == target["receipt_id"]
                ),
                None,
            )
            if marked[0].get("imported_id") != selected:
                return False
    rejected = find_receipt(receipts, "new-rejected")
    if any(
        item.get("imported_id") == rejected["receipt_imported_id"]
        or item.get("notes") == rejected["receipt_imported_id"]
        for item in transactions
    ):
        return False
    conflict = find_receipt(receipts, "conflict")
    if any(
        item.get("notes") == conflict["receipt_imported_id"]
        for item in transactions
    ):
        return False
    identity = find_receipt(receipts, "identity")
    identity_matches = [
        item
        for item in transactions
        if item.get("imported_id") == identity["receipt_imported_id"]
    ]
    return (
        len(identity_matches) == 1
        and identity_matches[0].get("notes")
        != identity["receipt_imported_id"]
    )


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def balance_value(value: Any) -> int:
    if isinstance(value, bool):
        raise RuntimeError("Actual balance response was not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        for key in ("balance", "data", "value"):
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
    raise RuntimeError("Actual balance response was not an integer")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--platform-base", default="http://127.0.0.1:8017")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument(
        "--additional-secret-owner",
        action="append",
        default=[],
    )
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument(
        "--actual-container",
        default="exp-lilies-005-actual-r1",
    )
    parser.add_argument("--actual-cli", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--workspace-path", default="exp005")
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args()

    run_tag = f"s{args.seed}-{uuid4().hex[:12]}"
    password = rotate_actual_password(
        container=args.actual_container,
        base_url=args.platform_base,
        platform_token=args.platform_token,
        application_id=args.application_id,
        additional_application_ids=args.additional_secret_owner,
    )
    cli = ActualCli(
        executable=args.actual_cli,
        password=password,
        data_dir=args.workspace_root / f".sealed-verifier-{run_tag}",
    )
    sync_id = discover_budget(cli)
    cli.sync_id = sync_id
    cli.run("budgets", "download", sync_id)

    account_name = f"Protected Checking {run_tag}"
    cli.run(
        "accounts",
        "create",
        "--name",
        account_name,
        "--balance",
        "0",
    )
    accounts = cli.run("accounts", "list")
    account_matches = [
        account for account in accounts if account.get("name") == account_name
    ]
    if len(account_matches) != 1:
        raise RuntimeError("sealed account provisioning was not unique")
    account_id = str(account_matches[0]["id"])

    receipts, seeded_transactions, decisions = build_seed(args.seed, run_tag)
    cli.run(
        "transactions",
        "add",
        "--account",
        account_id,
        "--file",
        "-",
        stdin=seeded_transactions,
    )
    baseline_transactions = cli.run(
        "transactions",
        "list",
        "--account",
        account_id,
        "--start",
        "2026-07-01",
        "--end",
        "2026-07-31",
    )
    opening_balance = balance_value(
        cli.run("accounts", "balance", account_id)
    )
    inputs = {
        "batch_id": f"sealed-{run_tag}",
        "budget_name": BUDGET_NAME,
        "account_name": account_name,
        "date_range": {"start": "2026-07-01", "end": "2026-07-31"},
        "receipts": receipts,
    }
    seed_fingerprint = canonical_digest(
        {
            "seed": args.seed,
            "input_count": len(receipts),
            "business_classes": [
                "matched",
                "ambiguous",
                "amount_conflict",
                "already_marker",
                "already_identity",
                "duplicate_input",
                "new_approved",
                "new_rejected",
            ],
        }
    )
    run, resumes = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        version=args.version,
        inputs=inputs,
        workspace_path=args.workspace_path,
        decisions=decisions,
    )
    result = (run.get("outputs") or {}).get("result") or {}
    rows = result.get("rows") if isinstance(result, dict) else []
    summary = result.get("summary") if isinstance(result, dict) else {}
    rows = rows if isinstance(rows, list) else []
    summary = summary if isinstance(summary, dict) else {}

    after_cli = ActualCli(
        executable=args.actual_cli,
        password=password,
        data_dir=args.workspace_root / f".sealed-readback-{run_tag}-after",
    )
    after_cli.sync_id = discover_budget(after_cli)
    after_cli.run("budgets", "download", after_cli.sync_id)
    after_transactions = after_cli.run(
        "transactions",
        "list",
        "--account",
        account_id,
        "--start",
        "2026-07-01",
        "--end",
        "2026-07-31",
    )
    closing_balance = balance_value(
        after_cli.run("accounts", "balance", account_id)
    )
    categories = after_cli.run("categories", "list")
    artifact = (run.get("outputs") or {}).get("workbook_artifact") or {}
    xlsx_path = args.workspace_root / args.workspace_path / str(
        artifact.get("relative_path", "")
    )
    json_artifact = (run.get("outputs") or {}).get("json_artifact") or {}
    json_path = args.workspace_root / args.workspace_path / str(
        json_artifact.get("relative_path", "")
    )
    try:
        artifact_json = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        artifact_json = None

    expected_counts = {
        "matched": 1,
        "ambiguous": 1,
        "already_reconciled": 2,
        "amount_conflict": 1,
        "new_transaction": 1,
        "duplicate_input": 1,
        "rejected": 1,
    }
    observed_counts = status_counts(rows)
    new_effects = {
        transaction_identity(item)
        for item in after_transactions
    } - {
        transaction_identity(item)
        for item in baseline_transactions
    }
    first_checks = {
        "terminal_status": run.get("status") == "succeeded",
        "single_human_pause": resumes == 1,
        "business_classification": observed_counts == expected_counts,
        "write_count": summary.get("write_count") == 3,
        "host_effect_count": len(new_effects) == 3,
        "host_business_outcomes": host_business_outcomes(
            transactions=after_transactions,
            categories=categories,
            receipts=receipts,
            decisions=decisions,
        ),
        "host_balance": (
            closing_balance
            == opening_balance
            + next(
                item["ledger_amount_cents"]
                for item in receipts
                if item["receipt_id"].endswith("new-approved")
            )
        ),
        "workflow_balance_invariant": (
            summary.get("opening_balance_cents") == opening_balance
            and summary.get("closing_balance_cents") == closing_balance
            and summary.get("balance_invariant_passed") is True
        ),
        "artifact_json": (
            isinstance(artifact_json, dict)
            and artifact_json.get("batch_id") == inputs["batch_id"]
            and len(artifact_json.get("rows", [])) == len(receipts)
        ),
        "artifact_xlsx": validate_xlsx(xlsx_path, len(receipts)),
    }

    replay, replay_resumes = run_workflow(
        base_url=args.platform_base,
        token=args.platform_token,
        application_id=args.application_id,
        version=args.version,
        inputs=inputs,
        workspace_path=args.workspace_path,
        decisions=decisions,
    )
    replay_result = (replay.get("outputs") or {}).get("result") or {}
    replay_summary = (
        replay_result.get("summary")
        if isinstance(replay_result, dict)
        else {}
    )
    replay_cli = ActualCli(
        executable=args.actual_cli,
        password=password,
        data_dir=args.workspace_root / f".sealed-readback-{run_tag}-replay",
    )
    replay_cli.sync_id = discover_budget(replay_cli)
    replay_cli.run("budgets", "download", replay_cli.sync_id)
    replay_transactions = replay_cli.run(
        "transactions",
        "list",
        "--account",
        account_id,
        "--start",
        "2026-07-01",
        "--end",
        "2026-07-31",
    )
    replay_balance = balance_value(
        replay_cli.run("accounts", "balance", account_id)
    )
    replay_checks = {
        "replay_terminal_status": replay.get("status") == "succeeded",
        "replay_single_human_pause": replay_resumes == 1,
        "replay_zero_reported_write": (
            isinstance(replay_summary, dict)
            and replay_summary.get("write_count") == 0
        ),
        "replay_zero_host_effect": {
            transaction_identity(item) for item in replay_transactions
        }
        == {
            transaction_identity(item) for item in after_transactions
        },
        "replay_balance_unchanged": replay_balance == closing_balance,
    }
    checks = {**first_checks, **replay_checks}
    trace = platform_json(
        "GET",
        args.platform_base,
        args.platform_token,
        f"/v1/streams/{run['run_id']}",
    )
    tool_started = sum(
        str(event.get("type", "")).endswith(".tool.started")
        for event in trace
    )
    report = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "seed": args.seed,
        "seed_fingerprint": seed_fingerprint,
        "published_version": args.version,
        "run_id": run["run_id"],
        "replay_run_id": replay["run_id"],
        "input_count": len(receipts),
        "business_class_count": 8,
        "human_pause_count": resumes + replay_resumes,
        "artifact_count": sum(
            isinstance((run.get("outputs") or {}).get(name), dict)
            for name in ("json_artifact", "workbook_artifact")
        ),
        "baseline_transaction_count": len(baseline_transactions),
        "unique_successful_effect_count": len(new_effects),
        "tool_node_start_count": tool_started,
        "check_count": len(checks),
        "passed_check_count": sum(checks.values()),
        "failed_check_counts": {
            key: int(not passed) for key, passed in checks.items()
        },
        "passed": all(checks.values()),
        "workflow_changed": False,
        "claim_ceiling": (
            "controlled-local Actual 26.7.0 reconciliation correctness; "
            "not production accounting certification or multi-user reliability"
        ),
    }
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
