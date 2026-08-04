from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
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
REVISION = 28
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
COMPOSE_PROJECT_NAME = "exp-lilies-001-r7"
ENVIRONMENT_OWNER_VOLUME = f"{COMPOSE_PROJECT_NAME}-environment-owner"
ENVIRONMENT_OWNER_SCHEMA_VERSION = "1.0"
ENVIRONMENT_OWNER_LABEL_PREFIX = "io.lilies.experiment.environment-owner"
EXPECTED_COMPOSE_SERVICES = frozenset(
    {
        "paperless-broker",
        "paperless-db",
        "paperless",
        "inventree-db",
        "inventree-cache",
        "inventree",
        "inventree-worker",
    }
)
EXPECTED_COMPOSE_VOLUME_COUNT = 7
COMPOSE_SECRET_BINDINGS = {
    "paperless-db": {
        "POSTGRES_PASSWORD": "paperless_db_password",
    },
    "paperless": {
        "PAPERLESS_DBPASS": "paperless_db_password",
        "PAPERLESS_SECRET_KEY": "paperless_secret_key",
        "PAPERLESS_ADMIN_PASSWORD": "paperless_admin_password",
    },
    "inventree-db": {
        "POSTGRES_PASSWORD": "inventree_db_password",
    },
    "inventree": {
        "INVENTREE_DB_PASSWORD": "inventree_db_password",
        "INVENTREE_SECRET_KEY": "inventree_secret_key",
        "INVENTREE_ADMIN_PASSWORD": "inventree_admin_password",
    },
    "inventree-worker": {
        "INVENTREE_DB_PASSWORD": "inventree_db_password",
        "INVENTREE_SECRET_KEY": "inventree_secret_key",
    },
}
WORKFLOW_INPUT_FIELDS = (
    "record_id",
    "source_id",
    "supplier",
    "purchase_order",
    "part_number",
    "lot_number",
    "quantity",
    "document_date",
    "certificate_type",
    "ocr_confidence",
)


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


def _write_new_private_json_within_state_root(
    state_root: Path,
    path: Path,
    value: Any,
) -> None:
    """Create one private file below state_root without following symlinks."""

    try:
        resolved_state_root = state_root.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError, OSError) as error:
        raise EnvironmentControlError("state root is unavailable") from error
    if not resolved_state_root.is_dir():
        raise EnvironmentControlError("state root is not a directory")

    absolute_path = Path(os.path.abspath(os.fspath(path)))
    try:
        relative_path = absolute_path.relative_to(resolved_state_root)
    except ValueError as error:
        raise EnvironmentControlError(
            "workflow input output must be located within the state root"
        ) from error
    if not relative_path.parts:
        raise EnvironmentControlError("workflow input output must name a file")

    payload = _canonical_json(value)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory_descriptors: list[int] = []
    output_descriptor: int | None = None
    try:
        directory_descriptor = os.open(resolved_state_root, directory_flags)
        directory_descriptors.append(directory_descriptor)
        for component in relative_path.parts[:-1]:
            directory_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            directory_descriptors.append(directory_descriptor)
        try:
            output_descriptor = os.open(
                relative_path.name,
                file_flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError as error:
            raise EnvironmentControlError(
                "workflow input output must not already exist or be a symlink"
            ) from error
        try:
            os.fchmod(output_descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(output_descriptor, remaining)
                if written <= 0:
                    raise OSError("private workflow input write made no progress")
                remaining = remaining[written:]
            os.fsync(output_descriptor)
        except BaseException:
            os.close(output_descriptor)
            output_descriptor = None
            try:
                os.unlink(relative_path.name, dir_fd=directory_descriptor)
            except OSError:
                pass
            raise
    except EnvironmentControlError:
        raise
    except OSError as error:
        raise EnvironmentControlError(
            "workflow input output parent must be an existing non-symlink "
            "directory within the state root"
        ) from error
    finally:
        if output_descriptor is not None:
            os.close(output_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


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
    owner_labels = _environment_owner_labels(
        state_root,
        secret_state=values,
    )
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
            "EXP_LILIES_ENVIRONMENT_OWNER_SCHEMA": owner_labels[
                f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.schema-version"
            ],
            "EXP_LILIES_ENVIRONMENT_OWNER_TASK": owner_labels[
                f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.task-id"
            ],
            "EXP_LILIES_ENVIRONMENT_OWNER_PROJECT": owner_labels[
                f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.compose-project"
            ],
            "EXP_LILIES_ENVIRONMENT_OWNER_BINDING": owner_labels[
                f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.state-root-binding"
            ],
        }
    )
    return environment


def _environment_owner_labels(
    state_root: Path,
    *,
    secret_state: dict[str, str] | None = None,
    legacy_adopted: bool | None = None,
) -> dict[str, str]:
    values = (
        _secret_state(state_root, create=False)
        if secret_state is None
        else secret_state
    )
    binding_message = (
        TASK_ID.encode("utf-8")
        + b"\0"
        + COMPOSE_PROJECT_NAME.encode("utf-8")
        + b"\0"
        + str(state_root.resolve()).encode("utf-8")
    )
    owner_binding = hmac.new(
        values["attestation_secret"].encode("utf-8"),
        binding_message,
        hashlib.sha256,
    ).hexdigest()
    labels = {
        f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.schema-version": (
            ENVIRONMENT_OWNER_SCHEMA_VERSION
        ),
        f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.task-id": TASK_ID,
        f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.compose-project": (
            COMPOSE_PROJECT_NAME
        ),
        f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.state-root-binding": (
            owner_binding
        ),
    }
    if legacy_adopted is not None:
        labels[f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.legacy-adopted"] = (
            "true" if legacy_adopted else "false"
        )
    return labels


def _docker_output(arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ["docker", *arguments],
        cwd=COMPOSE_PATH.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise EnvironmentControlError(
            f"docker {' '.join(arguments[:2])} failed with "
            f"status {completed.returncode}"
        )
    return completed.stdout


def _docker_json(arguments: Sequence[str]) -> Any:
    output = _docker_output(arguments)
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise EnvironmentControlError(
            f"docker {' '.join(arguments[:2])} returned invalid JSON"
        ) from error


def _docker_lines(arguments: Sequence[str]) -> list[str]:
    return [
        line.strip()
        for line in _docker_output(arguments).splitlines()
        if line.strip()
    ]


def _environment_owner_volume() -> dict[str, Any] | None:
    volume_names = _docker_lines(
        [
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"name={ENVIRONMENT_OWNER_VOLUME}",
        ]
    )
    if ENVIRONMENT_OWNER_VOLUME not in volume_names:
        return None
    payload = _docker_json(
        ["volume", "inspect", ENVIRONMENT_OWNER_VOLUME]
    )
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise EnvironmentControlError(
            "Docker environment owner volume inspection is invalid"
        )
    return payload[0]


def _assert_environment_owner(
    state_root: Path,
    owner_volume: dict[str, Any],
) -> bool:
    labels = owner_volume.get("Labels")
    expected = _environment_owner_labels(state_root)
    legacy_label = (
        labels.get(f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.legacy-adopted")
        if isinstance(labels, dict)
        else None
    )
    actual_binding = (
        labels.get(f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.state-root-binding")
        if isinstance(labels, dict)
        else None
    )
    expected_binding = expected.pop(
        f"{ENVIRONMENT_OWNER_LABEL_PREFIX}.state-root-binding"
    )
    if (
        owner_volume.get("Name") != ENVIRONMENT_OWNER_VOLUME
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in expected.items())
        or not isinstance(actual_binding, str)
        or not hmac.compare_digest(actual_binding, expected_binding)
        or legacy_label not in {"true", "false"}
    ):
        raise EnvironmentControlError(
            "the fixed Docker experiment environment is owned by another "
            "state root"
        )
    return legacy_label == "true"


def _compose_project_containers() -> list[dict[str, Any]]:
    container_ids = _docker_lines(
        [
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT_NAME}",
        ]
    )
    if not container_ids:
        return []
    payload = _docker_json(["container", "inspect", *container_ids])
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        raise EnvironmentControlError(
            "Docker experiment container inspection is invalid"
        )
    return payload


def _compose_project_volumes() -> list[str]:
    return _docker_lines(
        [
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT_NAME}",
        ]
    )


def _container_environment(container: dict[str, Any]) -> dict[str, str]:
    configuration = container.get("Config")
    values = configuration.get("Env") if isinstance(configuration, dict) else None
    if not isinstance(values, list) or any(
        not isinstance(item, str) for item in values
    ):
        raise EnvironmentControlError(
            "Docker experiment container environment is invalid"
        )
    environment: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator:
            continue
        if name in environment:
            raise EnvironmentControlError(
                "Docker experiment container has duplicate environment keys"
            )
        environment[name] = value
    return environment


def _compose_containers_by_service(
    containers: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_service: dict[str, dict[str, Any]] = {}
    for container in containers:
        configuration = container.get("Config")
        labels = (
            configuration.get("Labels")
            if isinstance(configuration, dict)
            else None
        )
        service = (
            labels.get("com.docker.compose.service")
            if isinstance(labels, dict)
            and labels.get("com.docker.compose.project")
            == COMPOSE_PROJECT_NAME
            else None
        )
        if not isinstance(service, str) or service in by_service:
            raise EnvironmentControlError(
                "existing Docker experiment container identity is ambiguous"
            )
        by_service[service] = container
    if set(by_service) != EXPECTED_COMPOSE_SERVICES:
        raise EnvironmentControlError(
            "existing Docker experiment container set is incomplete or "
            "unexpected"
        )
    return by_service


def _validate_legacy_compose_adoption(
    state_root: Path,
    *,
    containers: Sequence[dict[str, Any]],
    project_volumes: Sequence[str],
) -> None:
    if not containers:
        if project_volumes:
            raise EnvironmentControlError(
                "existing Docker experiment data volumes have no container "
                "identity; refusing ambiguous state-root adoption"
            )
        return

    by_service = _compose_containers_by_service(containers)
    secret_state = _secret_state(state_root, create=False)
    for service, bindings in COMPOSE_SECRET_BINDINGS.items():
        environment = _container_environment(by_service[service])
        if any(
            not hmac.compare_digest(
                environment.get(environment_name, ""),
                secret_state[secret_name],
            )
            for environment_name, secret_name in bindings.items()
        ):
            raise EnvironmentControlError(
                "existing Docker experiment containers do not match this "
                "state root"
            )


def _validate_owned_compose_resources(
    state_root: Path,
    *,
    owner_legacy_adopted: bool,
    containers: Sequence[dict[str, Any]],
    project_volumes: Sequence[str],
) -> None:
    if not containers:
        # The durable owner registry is the identity witness while an exact
        # owner has intentionally stopped containers but retained data.
        return

    by_service = _compose_containers_by_service(containers)
    expected_labels = _environment_owner_labels(state_root)
    label_modes: set[str] = set()
    for container in by_service.values():
        configuration = container.get("Config")
        labels = (
            configuration.get("Labels")
            if isinstance(configuration, dict)
            else None
        )
        if not isinstance(labels, dict):
            raise EnvironmentControlError(
                "Docker experiment container labels are invalid"
            )
        owner_labels = {
            str(name): str(value)
            for name, value in labels.items()
            if isinstance(name, str)
            and name.startswith(ENVIRONMENT_OWNER_LABEL_PREFIX + ".")
        }
        if not owner_labels:
            label_modes.add("legacy")
        elif all(owner_labels.get(key) == value for key, value in expected_labels.items()):
            label_modes.add("bound")
        else:
            raise EnvironmentControlError(
                "Docker experiment resource owner binding is invalid"
            )

    if label_modes == {"legacy"} and owner_legacy_adopted:
        _validate_legacy_compose_adoption(
            state_root,
            containers=containers,
            project_volumes=project_volumes,
        )
        return
    if label_modes != {"bound"}:
        raise EnvironmentControlError(
            "Docker experiment mixes legacy and owner-bound resources"
        )
    if len(project_volumes) != EXPECTED_COMPOSE_VOLUME_COUNT:
        raise EnvironmentControlError(
            "Docker experiment data volume set is incomplete or unexpected"
        )
    volume_payload = _docker_json(["volume", "inspect", *project_volumes])
    if (
        not isinstance(volume_payload, list)
        or len(volume_payload) != EXPECTED_COMPOSE_VOLUME_COUNT
        or any(not isinstance(item, dict) for item in volume_payload)
    ):
        raise EnvironmentControlError(
            "Docker experiment data volume inspection is invalid"
        )
    for volume in volume_payload:
        labels = volume.get("Labels")
        if not isinstance(labels, dict) or any(
            labels.get(key) != value
            for key, value in expected_labels.items()
        ):
            raise EnvironmentControlError(
                "Docker experiment data volume owner binding is invalid"
            )


def _claim_environment_owner(state_root: Path) -> None:
    owner_volume = _environment_owner_volume()
    if owner_volume is not None:
        owner_legacy_adopted = _assert_environment_owner(
            state_root,
            owner_volume,
        )
        _validate_owned_compose_resources(
            state_root,
            owner_legacy_adopted=owner_legacy_adopted,
            containers=_compose_project_containers(),
            project_volumes=_compose_project_volumes(),
        )
        return

    containers = _compose_project_containers()
    project_volumes = _compose_project_volumes()
    _validate_legacy_compose_adoption(
        state_root,
        containers=containers,
        project_volumes=project_volumes,
    )
    labels = _environment_owner_labels(
        state_root,
        # The frozen Compose contract predates resource-level owner labels, so
        # every current resource set remains in exact in-memory secret-
        # verification mode. The separate registry is still the global CAS.
        legacy_adopted=True,
    )
    create_arguments = ["volume", "create"]
    for name, value in sorted(labels.items()):
        create_arguments.extend(["--label", f"{name}={value}"])
    create_arguments.append(ENVIRONMENT_OWNER_VOLUME)
    _docker_output(create_arguments)

    # Docker volume creation is atomic by name. A competing state root can
    # race the preflight, but only one label set wins; every contender must
    # inspect the durable result before touching Compose resources.
    owner_volume = _environment_owner_volume()
    if owner_volume is None:
        raise EnvironmentControlError(
            "Docker environment owner volume was not created"
        )
    _assert_environment_owner(state_root, owner_volume)


def _release_environment_owner(state_root: Path) -> None:
    owner_volume = _environment_owner_volume()
    if owner_volume is None:
        raise EnvironmentControlError(
            "Docker environment owner volume is missing; ownership cannot "
            "be verified and nothing was released"
        )
    _assert_environment_owner(state_root, owner_volume)

    containers = _compose_project_containers()
    project_volumes = _compose_project_volumes()
    if containers or project_volumes:
        remaining: list[str] = []
        if containers:
            remaining.append(f"{len(containers)} container(s)")
        if project_volumes:
            remaining.append(f"{len(project_volumes)} data volume(s)")
        raise EnvironmentControlError(
            "cannot release the Docker environment owner while Compose "
            f"project resources remain: {', '.join(remaining)}"
        )

    _docker_output(["volume", "rm", ENVIRONMENT_OWNER_VOLUME])
    if _environment_owner_volume() is not None:
        raise EnvironmentControlError(
            "Docker environment owner volume deletion could not be verified"
        )


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
    # A listening socket is not an application-readiness signal while these
    # frozen images are still running first-boot migrations.  In particular,
    # provisioning scoped users before Django's post_migrate hooks finish can
    # observe an empty permission inventory.  Wait for both official API roots
    # before requesting credentials or mutating seed configuration.
    # Paperless redirects `/api/` to its HTML login page, which is not a JSON
    # readiness contract.  Its official OpenAPI endpoint is public and only
    # becomes available after application initialization has completed.
    _json_request_with_retry(
        "http://127.0.0.1:18000/api/schema/",
        timeout_seconds=1_200,
    )
    _json_request_with_retry(
        "http://127.0.0.1:18001/api/",
        timeout_seconds=1_200,
    )
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


def _write_workflow_input(
    state_root: Path,
    package_root: Path,
    *,
    seed: str,
    output: Path,
) -> None:
    records, _document_root = _load_seed_plan(package_root, seed=seed)
    projected_records: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        missing_fields = [
            field for field in WORKFLOW_INPUT_FIELDS if field not in record
        ]
        if missing_fields:
            raise EnvironmentControlError(
                "seed record "
                f"{index} is missing required workflow input fields"
            )
        projected_records.append(
            {field: record[field] for field in WORKFLOW_INPUT_FIELDS}
        )
    _write_new_private_json_within_state_root(
        state_root,
        output,
        {
            "records": projected_records,
            "run_label": "formal",
        },
    )


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
    release = subparsers.add_parser("release")
    release.add_argument("--confirm-task-id", required=True)
    subparsers.add_parser("initialize")
    subparsers.add_parser("serve")
    seed = subparsers.add_parser("seed")
    seed.add_argument("--seed", required=True)
    workflow_input = subparsers.add_parser("workflow-input")
    workflow_input.add_argument("--seed", required=True)
    workflow_input.add_argument("--output", type=Path, required=True)
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
    if args.command == "release":
        if args.confirm_task_id != TASK_ID:
            raise EnvironmentControlError(
                f"release requires --confirm-task-id {TASK_ID}"
            )
        _release_environment_owner(state_root)
        return 0
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command in {"config", "up"}:
        _secret_state(state_root, create=True)
    _claim_environment_owner(state_root)
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
    elif args.command == "workflow-input":
        _write_workflow_input(
            state_root,
            package_root,
            seed=args.seed,
            output=args.output,
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
