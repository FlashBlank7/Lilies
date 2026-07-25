from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[3]
TASK_ID = "EXP-LILIES-001"
REVISION = 4
COMPOSE_PATH = Path(__file__).with_name("compose.yaml")
ACCOUNT_PROVISION_SCRIPT = Path(__file__).with_name(
    "provision_scoped_account.py"
)
DEFAULT_PACKAGE_ROOT = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / TASK_ID
    / str(REVISION)
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class EnvironmentControlError(RuntimeError):
    """The exact experiment environment could not be prepared safely."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _atomic_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EnvironmentControlError(f"private state file is unavailable: {path.name}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise EnvironmentControlError(
            f"private state file must have mode 0600: {path.name}"
        )
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise EnvironmentControlError("private state must be a JSON object")
    return value


def _secret_state(state_root: Path, *, create: bool) -> dict[str, str]:
    path = state_root / "secrets.json"
    if path.exists():
        value = _read_private_json(path)
        if (
            value.get("schema_version") != "1.0"
            or value.get("task_id") != TASK_ID
            or any(
                not isinstance(value.get(name), str) or len(value[name]) < 32
                for name in (
                    "paperless_db_password",
                    "paperless_secret_key",
                    "paperless_admin_password",
                    "inventree_db_password",
                    "inventree_secret_key",
                    "inventree_admin_password",
                    "paperless_builder_password",
                    "paperless_verifier_password",
                    "inventree_builder_password",
                    "inventree_verifier_password",
                    "attestation_secret",
                )
            )
        ):
            raise EnvironmentControlError("experiment secret state is invalid")
        return {str(key): str(item) for key, item in value.items()}
    if not create:
        raise EnvironmentControlError("experiment secrets have not been created")
    value = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "paperless_db_password": secrets.token_urlsafe(36),
        "paperless_secret_key": secrets.token_urlsafe(48),
        "paperless_admin_password": secrets.token_urlsafe(36),
        "inventree_db_password": secrets.token_urlsafe(36),
        "inventree_secret_key": secrets.token_urlsafe(48),
        "inventree_admin_password": secrets.token_urlsafe(36),
        "paperless_builder_password": secrets.token_urlsafe(36),
        "paperless_verifier_password": secrets.token_urlsafe(36),
        "inventree_builder_password": secrets.token_urlsafe(36),
        "inventree_verifier_password": secrets.token_urlsafe(36),
        # The platform secret boundary stores text and resolves it as UTF-8
        # bytes for HMAC verification. Keep the task-author and platform views
        # byte-identical instead of decoding an unrelated base64 value in the
        # attestation process.
        "attestation_secret": secrets.token_urlsafe(48),
    }
    _atomic_private_json(path, value)
    return {str(key): str(item) for key, item in value.items()}


def _compose_environment(state_root: Path, *, create: bool) -> dict[str, str]:
    values = _secret_state(state_root, create=create)
    environment = os.environ.copy()
    environment.update(
        {
            "EXP_LILIES_PAPERLESS_DB_PASSWORD": values[
                "paperless_db_password"
            ],
            "EXP_LILIES_PAPERLESS_SECRET_KEY": values[
                "paperless_secret_key"
            ],
            "EXP_LILIES_PAPERLESS_ADMIN_PASSWORD": values[
                "paperless_admin_password"
            ],
            "EXP_LILIES_INVENTREE_DB_PASSWORD": values[
                "inventree_db_password"
            ],
            "EXP_LILIES_INVENTREE_SECRET_KEY": values[
                "inventree_secret_key"
            ],
            "EXP_LILIES_INVENTREE_ADMIN_PASSWORD": values[
                "inventree_admin_password"
            ],
        }
    )
    return environment


def _compose(
    state_root: Path,
    arguments: Sequence[str],
    *,
    create_secrets: bool,
) -> None:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_PATH),
            *arguments,
        ],
        cwd=COMPOSE_PATH.parent,
        env=_compose_environment(
            state_root,
            create=create_secrets,
        ),
        check=False,
    )
    if completed.returncode != 0:
        raise EnvironmentControlError(
            f"docker compose failed with status {completed.returncode}"
        )


def _provision_scoped_account(
    state_root: Path,
    *,
    service: str,
    host: str,
    role: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    environment = _compose_environment(state_root, create=False)
    manage_script = (
        "manage.py"
        if service == "paperless"
        else "src/backend/InvenTree/manage.py"
        if service == "inventree"
        else None
    )
    if manage_script is None:
        raise EnvironmentControlError(
            "scoped account provisioning requires a frozen host service"
        )
    environment.update(
        {
            "EXP_LILIES_ACCOUNT_HOST": host,
            "EXP_LILIES_ACCOUNT_ROLE": role,
            "EXP_LILIES_ACCOUNT_USERNAME": username,
            "EXP_LILIES_ACCOUNT_PASSWORD": password,
        }
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_PATH),
            "exec",
            "--no-TTY",
            "--env",
            "EXP_LILIES_ACCOUNT_HOST",
            "--env",
            "EXP_LILIES_ACCOUNT_ROLE",
            "--env",
            "EXP_LILIES_ACCOUNT_USERNAME",
            "--env",
            "EXP_LILIES_ACCOUNT_PASSWORD",
            service,
            "python",
            manage_script,
            "shell",
        ],
        cwd=COMPOSE_PATH.parent,
        env=environment,
        input=ACCOUNT_PROVISION_SCRIPT.read_bytes(),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise EnvironmentControlError(
            f"{host} {role} account provisioning failed"
        )
    for line in reversed(completed.stdout.decode("utf-8").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == "1.0"
            and value.get("host") == host
            and value.get("role") == role
            and isinstance(value.get("token"), str)
        ):
            return value
    raise EnvironmentControlError(
        f"{host} {role} account provisioning returned no result"
    )


def _json_request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    basic_auth: tuple[str, str] | None = None,
    value: Any = None,
    form: dict[str, str] | None = None,
) -> Any:
    if value is not None and form is not None:
        raise ValueError("request cannot contain both JSON and form data")
    if token is not None and basic_auth is not None:
        raise ValueError("request cannot contain both token and basic authentication")
    data: bytes | None = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "Lilies-EXP-LILIES-001-TaskAuthor/1.0",
    }
    if token:
        headers["Authorization"] = f"Token {token}"
    elif basic_auth is not None:
        encoded = base64.b64encode(
            f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    if value is not None:
        data = _canonical_json(value)
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise EnvironmentControlError("host response exceeds its limit")
    except HTTPError as error:
        detail = error.read(4_096).decode("utf-8", errors="replace")
        raise EnvironmentControlError(
            f"host request failed with status {error.code}: {detail[:500]}"
        ) from error
    except (URLError, OSError, TimeoutError) as error:
        raise EnvironmentControlError("host request failed") from error
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnvironmentControlError("host response is not JSON") from error


def _token_value(value: Any) -> str:
    if not isinstance(value, dict):
        raise EnvironmentControlError("token response is not a JSON object")
    for key in ("token", "key"):
        token = value.get(key)
        if isinstance(token, str) and len(token) >= 16:
            return token
    raise EnvironmentControlError("token response does not contain a token")


def _json_request_with_retry(
    url: str,
    *,
    timeout_seconds: float = 300,
    **kwargs: Any,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error: EnvironmentControlError | None = None
    while time.monotonic() < deadline:
        try:
            return _json_request(url, **kwargs)
        except EnvironmentControlError as error:
            last_error = error
            time.sleep(1)
    raise EnvironmentControlError("host did not become ready before timeout") from last_error


def _primary_key(value: Any, *, label: str) -> int:
    if isinstance(value, dict):
        for key in ("pk", "id"):
            item = value.get(key)
            if isinstance(item, int) and item > 0:
                return item
    raise EnvironmentControlError(f"{label} response does not contain a primary key")


def _multipart_document_upload(
    *,
    token: str,
    title: str,
    created: str,
    path: Path,
) -> str:
    if path.is_symlink() or not path.is_file():
        raise EnvironmentControlError("document fixture is not a regular file")
    document = path.read_bytes()
    if len(document) > MAX_RESPONSE_BYTES:
        raise EnvironmentControlError("document fixture exceeds its upload limit")
    boundary = f"lilies-{secrets.token_hex(24)}"
    chunks: list[bytes] = []
    for name, value in (("title", title), ("created", created)):
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    chunks.extend(
        [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="document"; '
                f'filename="{path.name}"\r\n'
                "Content-Type: application/pdf\r\n\r\n"
            ).encode(),
            document,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    payload = b"".join(chunks)
    request = Request(
        "http://127.0.0.1:18000/api/documents/post_document/",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Token {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Lilies-EXP-LILIES-001-TaskAuthor/1.0",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            response_payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_payload) > MAX_RESPONSE_BYTES:
                raise EnvironmentControlError("upload response exceeds its limit")
    except HTTPError as error:
        detail = error.read(4_096).decode("utf-8", errors="replace")
        raise EnvironmentControlError(
            f"Paperless upload failed with status {error.code}: {detail[:500]}"
        ) from error
    except (URLError, OSError, TimeoutError) as error:
        raise EnvironmentControlError("Paperless upload failed") from error
    try:
        value = json.loads(response_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnvironmentControlError("Paperless upload response is not JSON") from error
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("task_id", "id"):
            task_id = value.get(key)
            if isinstance(task_id, str) and task_id:
                return task_id
    raise EnvironmentControlError("Paperless upload response has no task identity")


def _paperless_document_id(
    *,
    token: str,
    task_id: str,
    timeout_seconds: float = 300,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = _json_request(
            "http://127.0.0.1:18000/api/tasks/?" + urlencode({"task_id": task_id}),
            token=token,
        )
        if isinstance(value, dict):
            records = value.get("results")
            if records is None and value.get("task_id") == task_id:
                records = [value]
        else:
            records = value
        if not isinstance(records, list):
            raise EnvironmentControlError("Paperless task response is invalid")
        matching = [
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("task_id") or record.get("id") or "") == task_id
        ]
        if len(matching) > 1:
            raise EnvironmentControlError("Paperless task identity is ambiguous")
        if matching:
            record = matching[0]
            status_value = str(
                record.get("status") or record.get("task_status") or ""
            ).upper()
            if status_value in {"FAILURE", "FAILED", "REVOKED"}:
                raise EnvironmentControlError("Paperless document consumption failed")
            document_id = (
                record.get("related_document")
                or record.get("document_id")
                or record.get("document")
            )
            if isinstance(document_id, int) and document_id > 0:
                return document_id
            if (
                isinstance(document_id, str)
                and document_id.isdecimal()
                and int(document_id) > 0
            ):
                return int(document_id)
        time.sleep(1)
    raise EnvironmentControlError("Paperless document consumption timed out")


def _grant_paperless_document_access(
    *,
    token: str,
    document_id: int,
    builder_user_id: int,
    verifier_user_id: int,
) -> None:
    _json_request(
        f"http://127.0.0.1:18000/api/documents/{document_id}/",
        method="PATCH",
        token=token,
        value={
            "set_permissions": {
                "view": {
                    "users": [builder_user_id, verifier_user_id],
                    "groups": [],
                },
                "change": {
                    "users": [builder_user_id],
                    "groups": [],
                },
            }
        },
    )


def _initialize(state_root: Path) -> None:
    secrets_state = _secret_state(state_root, create=False)
    paperless_token = _token_value(
        _json_request_with_retry(
            "http://127.0.0.1:18000/api/token/",
            method="POST",
            form={
                "username": "exp_lilies_admin",
                "password": secrets_state["paperless_admin_password"],
            },
        )
    )
    inventree_token = _token_value(
        _json_request_with_retry(
            "http://127.0.0.1:18001/api/user/me/token/"
            "?name=EXP-LILIES-001-task-author",
            basic_auth=(
                "exp_lilies_admin",
                secrets_state["inventree_admin_password"],
            ),
        )
    )
    _json_request(
        (
            "http://127.0.0.1:18001/api/settings/global/"
            "PURCHASEORDER_REFERENCE_PATTERN/"
        ),
        method="PATCH",
        token=inventree_token,
        value={"value": "{?:PO-017}-{ref:04d}"},
    )
    scoped = {
        ("paperless", "builder"): _provision_scoped_account(
            state_root,
            service="paperless",
            host="paperless",
            role="builder",
            username="exp_lilies_builder",
            password=secrets_state["paperless_builder_password"],
        ),
        ("paperless", "verifier"): _provision_scoped_account(
            state_root,
            service="paperless",
            host="paperless",
            role="verifier",
            username="exp_lilies_verifier",
            password=secrets_state["paperless_verifier_password"],
        ),
        ("inventree", "builder"): _provision_scoped_account(
            state_root,
            service="inventree",
            host="inventree",
            role="builder",
            username="exp_lilies_builder",
            password=secrets_state["inventree_builder_password"],
        ),
        ("inventree", "verifier"): _provision_scoped_account(
            state_root,
            service="inventree",
            host="inventree",
            role="verifier",
            username="exp_lilies_verifier",
            password=secrets_state["inventree_verifier_password"],
        ),
    }
    _atomic_private_json(
        state_root / "credentials.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "paperless_admin_token": paperless_token,
            "inventree_admin_token": inventree_token,
            "paperless_builder_token": scoped[
                ("paperless", "builder")
            ]["token"],
            "paperless_verifier_token": scoped[
                ("paperless", "verifier")
            ]["token"],
            "inventree_builder_token": scoped[
                ("inventree", "builder")
            ]["token"],
            "inventree_verifier_token": scoped[
                ("inventree", "verifier")
            ]["token"],
            "paperless_builder_user_id": scoped[
                ("paperless", "builder")
            ]["user_id"],
            "paperless_verifier_user_id": scoped[
                ("paperless", "verifier")
            ]["user_id"],
            "inventree_builder_user_id": scoped[
                ("inventree", "builder")
            ]["user_id"],
            "inventree_verifier_user_id": scoped[
                ("inventree", "verifier")
            ]["user_id"],
            "builder_credentials_status": "scoped",
            "verifier_credentials_status": "read_only",
            "permission_inventory": {
                f"{host}:{role}": result["permission_codenames"]
                for (host, role), result in scoped.items()
            },
        },
    )


def _load_seed_plan(
    package_root: Path,
    *,
    seed: str,
) -> tuple[list[dict[str, Any]], Path]:
    if seed == "debug":
        path = package_root / "fixtures" / "public-inputs" / "debug-records.json"
        document_root = package_root / "fixtures" / "public-inputs" / "documents"
    elif seed in {"101", "202", "303"}:
        root = package_root / "protected" / "hidden-inputs" / seed
        path = root / "seed-plan.json"
        document_root = root / "documents"
    else:
        raise EnvironmentControlError("seed must be debug, 101, 202, or 303")
    value = json.loads(path.read_bytes())
    records = value.get("records") if isinstance(value, dict) else None
    if (
        not isinstance(records, list)
        or not records
        or any(not isinstance(record, dict) for record in records)
    ):
        raise EnvironmentControlError("seed plan has an invalid record inventory")
    return records, document_root


def _seed(
    state_root: Path,
    package_root: Path,
    *,
    seed: str,
) -> None:
    credentials = _read_private_json(state_root / "credentials.json")
    paperless_token = credentials.get("paperless_admin_token")
    inventree_token = credentials.get("inventree_admin_token")
    if not isinstance(paperless_token, str) or not isinstance(
        inventree_token,
        str,
    ):
        raise EnvironmentControlError("task-author host credentials are unavailable")
    records, document_root = _load_seed_plan(package_root, seed=seed)
    paperless_builder_user_id = credentials.get("paperless_builder_user_id")
    paperless_verifier_user_id = credentials.get("paperless_verifier_user_id")
    if (
        not isinstance(paperless_builder_user_id, int)
        or paperless_builder_user_id <= 0
        or not isinstance(paperless_verifier_user_id, int)
        or paperless_verifier_user_id <= 0
    ):
        raise EnvironmentControlError("Paperless scoped user identities are unavailable")

    supplier_ids: dict[str, int] = {}
    for supplier in sorted({str(record["supplier"]) for record in records}):
        created = _json_request(
            "http://127.0.0.1:18001/api/company/",
            method="POST",
            token=inventree_token,
            value={
                "name": supplier,
                "description": f"{TASK_ID} frozen supplier",
                "is_supplier": True,
                "active": True,
            },
        )
        supplier_ids[supplier] = _primary_key(created, label="InvenTree company")

    part_ids: dict[str, int] = {}
    for part_number in sorted(
        {str(record["host_part_number"]) for record in records}
    ):
        created = _json_request(
            "http://127.0.0.1:18001/api/part/",
            method="POST",
            token=inventree_token,
            value={
                "name": f"Experiment part {part_number}",
                "description": f"{TASK_ID} frozen part",
                "IPN": part_number,
                "active": True,
                "purchaseable": True,
            },
        )
        part_ids[part_number] = _primary_key(created, label="InvenTree part")

    supplier_part_ids: dict[tuple[str, str], int] = {}
    supplier_part_pairs = sorted(
        {
            (
                str(record["supplier"]),
                str(record["host_part_number"]),
            )
            for record in records
        }
    )
    for supplier, part_number in supplier_part_pairs:
        created = _json_request(
            "http://127.0.0.1:18001/api/company/part/",
            method="POST",
            token=inventree_token,
            value={
                "supplier": supplier_ids[supplier],
                "part": part_ids[part_number],
                "SKU": f"{supplier_ids[supplier]}-{part_number}",
                "description": f"{TASK_ID} frozen supplier part",
                "active": True,
            },
        )
        supplier_part_ids[(supplier, part_number)] = _primary_key(
            created,
            label="InvenTree supplier part",
        )

    purchase_orders: dict[str, int] = {}
    purchase_lines: dict[str, int] = {}
    for record in records:
        reference = str(record["host_purchase_order"])
        if reference in purchase_orders:
            continue
        order = _json_request(
            "http://127.0.0.1:18001/api/order/po/",
            method="POST",
            token=inventree_token,
            value={
                "reference": reference,
                "supplier": supplier_ids[str(record["supplier"])],
                "description": f"{TASK_ID} frozen purchase order",
            },
        )
        order_id = _primary_key(order, label="InvenTree purchase order")
        purchase_orders[reference] = order_id
        line = _json_request(
            "http://127.0.0.1:18001/api/order/po-line/",
            method="POST",
            token=inventree_token,
            value={
                "order": order_id,
                "part": supplier_part_ids[
                    (
                        str(record["supplier"]),
                        str(record["host_part_number"]),
                    )
                ],
                "quantity": record["purchase_line_quantity"],
                "reference": str(record["source_id"]),
                "merge_items": False,
            },
        )
        purchase_lines[reference] = _primary_key(
            line,
            label="InvenTree purchase order line",
        )

    receipts: list[dict[str, Any]] = []
    for record in records:
        matching = sorted(
            document_root.glob(f"{str(record['record_id']).lower()}-*.pdf")
        )
        if len(matching) != 1:
            raise EnvironmentControlError(
                f"record {record['record_id']} has no unique PDF fixture"
            )
        task_id = _multipart_document_upload(
            token=paperless_token,
            title=f"{TASK_ID} {record['record_id']} {record['source_id']}",
            created=str(record["document_date"]),
            path=matching[0],
        )
        document_id = _paperless_document_id(
            token=paperless_token,
            task_id=task_id,
        )
        _grant_paperless_document_access(
            token=paperless_token,
            document_id=document_id,
            builder_user_id=paperless_builder_user_id,
            verifier_user_id=paperless_verifier_user_id,
        )
        reference = str(record["host_purchase_order"])
        receipts.append(
            {
                "record_id": str(record["record_id"]),
                "source_id": str(record["source_id"]),
                "paperless_task_id": task_id,
                "paperless_document_id": document_id,
                "inventree_company_id": supplier_ids[str(record["supplier"])],
                "inventree_part_id": part_ids[
                    str(record["host_part_number"])
                ],
                "inventree_supplier_part_id": supplier_part_ids[
                    (
                        str(record["supplier"]),
                        str(record["host_part_number"]),
                    )
                ],
                "inventree_purchase_order_id": purchase_orders[reference],
                "inventree_purchase_order_line_id": purchase_lines[reference],
            }
        )
    _atomic_private_json(
        state_root / f"seed-receipts-{seed}.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "seed": seed,
            "record_count": len(receipts),
            "records": receipts,
        },
    )
    _fault_state(
        state_root,
        package_root,
        seed=seed,
        active=True,
    )


def _serve_boundary_services(state_root: Path) -> None:
    secrets_state = _secret_state(state_root, create=False)
    credentials = _read_private_json(state_root / "credentials.json")
    required_tokens = {
        name: credentials.get(name)
        for name in (
            "paperless_verifier_token",
            "inventree_verifier_token",
        )
    }
    if any(
        not isinstance(value, str) or not value
        for value in required_tokens.values()
    ):
        raise EnvironmentControlError(
            "scoped verifier credentials are unavailable"
        )
    environment = os.environ.copy()
    environment.update(
        {
            "EXP_LILIES_ATTESTATION_SECRET": secrets_state[
                "attestation_secret"
            ],
            "EXP_LILIES_PAPERLESS_VERIFIER_TOKEN": str(
                required_tokens["paperless_verifier_token"]
            ),
            "EXP_LILIES_INVENTREE_VERIFIER_TOKEN": str(
                required_tokens["inventree_verifier_token"]
            ),
        }
    )
    fault_state_path = state_root / "fault-state.json"
    commands = [
        [
            sys.executable,
            str(Path(__file__).with_name("fault_proxy.py")),
            "--port",
            "18010",
            "--upstream",
            "http://127.0.0.1:18000",
            "--state-path",
            str(fault_state_path),
        ],
        [
            sys.executable,
            str(Path(__file__).with_name("fault_proxy.py")),
            "--port",
            "18011",
            "--upstream",
            "http://127.0.0.1:18001",
            "--state-path",
            str(fault_state_path),
        ],
        [
            sys.executable,
            str(Path(__file__).with_name("attestation_server.py")),
        ],
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
        )
        for command in commands
    ]
    try:
        while True:
            for process in processes:
                status = process.poll()
                if status is not None:
                    raise EnvironmentControlError(
                        f"boundary service exited with status {status}"
                    )
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def _fault_state(
    state_root: Path,
    package_root: Path,
    *,
    seed: str,
    active: bool,
) -> None:
    if seed == "debug":
        plan_path = package_root / "fixtures" / "public-inputs" / "debug-records.json"
    elif seed in {"101", "202", "303"}:
        plan_path = (
            package_root
            / "protected"
            / "hidden-inputs"
            / seed
            / "seed-plan.json"
        )
    else:
        raise EnvironmentControlError("seed must be debug, 101, 202, or 303")
    value = json.loads(plan_path.read_bytes())
    records = value.get("records") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise EnvironmentControlError("seed plan is invalid")
    transient = sorted(
        str(record["source_id"])
        for record in records
        if isinstance(record, dict)
        and record.get("scenario") == "transient_error"
    )
    permission = sorted(
        str(record["source_id"])
        for record in records
        if isinstance(record, dict)
        and record.get("scenario") == "permission_denied"
    )
    all_source_ids = sorted(
        {
            str(record["source_id"])
            for record in records
            if isinstance(record, dict)
            and isinstance(record.get("source_id"), str)
            and record["source_id"]
        }
    )
    _atomic_private_json(
        state_root / "fault-state.json",
        {
            "schema_version": "1.0",
            "active": active,
            "seed": seed,
            "transient_source_ids": transient,
            "permission_source_ids": permission,
            "consumed_transient_source_ids": [],
            "all_source_ids": all_source_ids,
            "request_log": [],
        },
    )


def _snapshot_host_state(
    state_root: Path,
    package_root: Path,
    *,
    seed: str,
    phase: str,
) -> Path:
    if phase not in {"baseline", "final"}:
        raise EnvironmentControlError("snapshot phase must be baseline or final")
    credentials = _read_private_json(state_root / "credentials.json")
    receipts = _read_private_json(state_root / f"seed-receipts-{seed}.json")
    records, _document_root = _load_seed_plan(package_root, seed=seed)
    receipt_records = receipts.get("records")
    if not isinstance(receipt_records, list):
        raise EnvironmentControlError("seed receipts are invalid")
    receipts_by_record = {
        str(record.get("record_id")): record
        for record in receipt_records
        if isinstance(record, dict)
    }
    if len(receipts_by_record) != len(records):
        raise EnvironmentControlError("seed receipts do not cover the full denominator")
    paperless_token = credentials.get("paperless_verifier_token")
    inventree_token = credentials.get("inventree_verifier_token")
    if not isinstance(paperless_token, str) or not isinstance(
        inventree_token,
        str,
    ):
        raise EnvironmentControlError("read-only snapshot credentials are unavailable")
    fault_state = _read_private_json(state_root / "fault-state.json")
    request_log = fault_state.get("request_log")
    if not isinstance(request_log, list) or any(
        not isinstance(item, dict) for item in request_log
    ):
        raise EnvironmentControlError("fault proxy request log is unavailable")
    snapshot_records: list[dict[str, Any]] = []
    duplicate_effect_count = 0
    forbidden_write_count = 0
    for record in records:
        record_id = str(record["record_id"])
        receipt = receipts_by_record.get(record_id)
        if not isinstance(receipt, dict):
            raise EnvironmentControlError("seed receipt identity is unavailable")
        document_id = receipt.get("paperless_document_id")
        purchase_order_id = receipt.get("inventree_purchase_order_id")
        purchase_order_line_id = receipt.get("inventree_purchase_order_line_id")
        if any(
            not isinstance(item, int) or item <= 0
            for item in (
                document_id,
                purchase_order_id,
                purchase_order_line_id,
            )
        ):
            raise EnvironmentControlError("seed receipt contains an invalid host identity")
        source_id = str(record["source_id"])
        mutations = [
            item
            for item in request_log
            if source_id in item.get("source_ids", [])
        ]
        successful_mutations = sum(
            isinstance(item.get("status"), int)
            and 200 <= item["status"] < 300
            for item in mutations
        )
        scenario = str(record["scenario"])
        if scenario == "duplicate":
            duplicate_effect_count += successful_mutations
        if scenario not in {"exact_match", "transient_error"}:
            forbidden_write_count += successful_mutations
        snapshot_records.append(
            {
                "record_id": record_id,
                "source_id": source_id,
                "scenario": scenario,
                "write_count": successful_mutations,
                "paperless_document": _json_request(
                    f"http://127.0.0.1:18000/api/documents/{document_id}/"
                    "?full_perms=true",
                    token=paperless_token,
                ),
                "inventree_purchase_order": _json_request(
                    f"http://127.0.0.1:18001/api/order/po/{purchase_order_id}/",
                    token=inventree_token,
                ),
                "inventree_purchase_order_line": _json_request(
                    "http://127.0.0.1:18001/api/order/po-line/"
                    f"{purchase_order_line_id}/",
                    token=inventree_token,
                ),
                "proxy_mutations": mutations,
                "successful_proxy_mutations": successful_mutations,
                "injected_transient_failures": sum(
                    item.get("status") == 503 and item.get("injected") is True
                    for item in mutations
                ),
                "injected_permission_denials": sum(
                    item.get("status") == 403 and item.get("injected") is True
                    for item in mutations
                ),
            }
        )
    target = state_root / f"host-snapshot-{seed}-{phase}.json"
    _atomic_private_json(
        target,
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "seed": seed,
            "phase": phase,
            "record_count": len(snapshot_records),
            "records": snapshot_records,
            "request_log_count": len(request_log),
            "duplicate_effect_count": duplicate_effect_count,
            "forbidden_write_count": forbidden_write_count,
        },
    )
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control the frozen EXP-LILIES-001 real-host environment."
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "--package-root",
        type=Path,
        default=DEFAULT_PACKAGE_ROOT,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config")
    subparsers.add_parser("up")
    subparsers.add_parser("down")
    reset = subparsers.add_parser("reset")
    reset.add_argument("--confirm-task-id", required=True)
    subparsers.add_parser("initialize")
    subparsers.add_parser("serve")
    seed = subparsers.add_parser("seed")
    seed.add_argument("--seed", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--seed", required=True)
    snapshot.add_argument(
        "--phase",
        choices=("baseline", "final"),
        required=True,
    )
    for command in ("fault-activate", "fault-recover"):
        fault = subparsers.add_parser(command)
        fault.add_argument("--seed", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state_root = args.state_root.resolve()
    package_root = args.package_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "config":
        _compose(state_root, ["config", "--quiet"], create_secrets=True)
    elif args.command == "up":
        _compose(state_root, ["up", "--detach"], create_secrets=True)
    elif args.command == "down":
        _compose(state_root, ["down"], create_secrets=False)
    elif args.command == "reset":
        if args.confirm_task_id != TASK_ID:
            raise EnvironmentControlError(
                f"reset requires --confirm-task-id {TASK_ID}"
            )
        _compose(
            state_root,
            ["down", "--volumes", "--remove-orphans"],
            create_secrets=False,
        )
    elif args.command == "initialize":
        _initialize(state_root)
    elif args.command == "seed":
        _seed(
            state_root,
            package_root,
            seed=args.seed,
        )
    elif args.command == "snapshot":
        _snapshot_host_state(
            state_root,
            package_root,
            seed=args.seed,
            phase=args.phase,
        )
    elif args.command == "serve":
        _serve_boundary_services(state_root)
    elif args.command == "fault-activate":
        _fault_state(
            state_root,
            package_root,
            seed=args.seed,
            active=True,
        )
    elif args.command == "fault-recover":
        _fault_state(
            state_root,
            package_root,
            seed=args.seed,
            active=False,
        )
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvironmentControlError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
