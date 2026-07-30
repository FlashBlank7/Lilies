from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class ScenarioError(RuntimeError):
    pass


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ScenarioError(f"JSON pointer must start with '/': {pointer!r}")
    current = value
    for encoded_segment in pointer[1:].split("/"):
        segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError) as exc:
                raise ScenarioError(
                    f"JSON pointer segment is unavailable: {pointer!r}"
                ) from exc
        elif isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            raise ScenarioError(f"JSON pointer segment is unavailable: {pointer!r}")
    return current


def _strip_key_prefixes(value: Any, prefixes: tuple[str, ...]) -> Any:
    if isinstance(value, list):
        return [_strip_key_prefixes(item, prefixes) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_key_prefixes(item, prefixes)
            for key, item in value.items()
            if not any(key.startswith(prefix) for prefix in prefixes)
        }
    return value


def _resolve_workspace(root: Path, configured: str) -> Path:
    root = root.resolve()
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ScenarioError(
            f"workspace escapes configured workspace_root: {configured!r}"
        )
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _format_value(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        try:
            return value.format_map(variables)
        except KeyError as exc:
            raise ScenarioError(
                f"unknown configuration variable: {exc.args[0]}"
            ) from exc
    if isinstance(value, list):
        return [_format_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _format_value(item, variables) for key, item in value.items()}
    return value


def _http_json(
    *,
    base_url: str,
    token: str,
    method: str,
    path: str,
    body: Any | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ScenarioError(
            f"{method} {path} returned HTTP {exc.code}: {detail[:1000]}"
        ) from exc
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{method} {path} did not return JSON") from exc


def _run_commands(
    commands: list[Any],
    *,
    cwd: Path,
    phase: str,
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for index, configured_command in enumerate(commands, start=1):
        if isinstance(configured_command, dict):
            command = configured_command.get("argv")
            accepted_returncodes = configured_command.get(
                "accepted_returncodes",
                [0],
            )
        else:
            command = configured_command
            accepted_returncodes = [0]
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ScenarioError(f"{phase} command {index} must be non-empty argv")
        if (
            not isinstance(accepted_returncodes, list)
            or not accepted_returncodes
            or not all(isinstance(item, int) for item in accepted_returncodes)
        ):
            raise ScenarioError(
                f"{phase} command {index} accepted_returncodes must be integers"
            )
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        receipt = {
            "index": index,
            "argv": command,
            "returncode": completed.returncode,
            "accepted_returncodes": accepted_returncodes,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        receipts.append(receipt)
        if completed.returncode not in accepted_returncodes:
            stderr = completed.stderr.strip()
            raise ScenarioError(
                f"{phase} command {index} failed with exit "
                f"{completed.returncode}: {stderr[-2000:]}"
            )
    return receipts


def _binding_body(binding: dict[str, Any], secret_ref: str) -> dict[str, Any]:
    required = (
        "connector_id",
        "connector_version",
        "tenant_id",
        "external_tenant_id",
        "profile_id",
        "application_ids",
        "allowed_operations",
        "subjects",
    )
    missing = [field for field in required if field not in binding]
    if missing:
        raise ScenarioError(f"connector binding is missing fields: {missing}")
    revision = int(binding.get("revision", 0))
    return {
        "binding": {
            **{field: binding[field] for field in required},
            "secret_ref": secret_ref,
            "enabled": bool(binding.get("enabled", True)),
            "revision": revision + 1,
        },
        "expected_revision": revision,
    }


def _rotate_credentials(
    *,
    rotations: list[dict[str, Any]],
    base_url: str,
    token: str,
    variables: dict[str, str],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    binding_cache: dict[str, list[dict[str, Any]]] = {}
    for rotation in rotations:
        connector_id = str(rotation["connector_id"])
        tenant_id = str(rotation["tenant_id"])
        value_file = Path(str(rotation["value_file"]))
        value_state = json.loads(value_file.read_text(encoding="utf-8"))
        value = _json_pointer(value_state, str(rotation["value_pointer"]))
        if not isinstance(value, str) or not value:
            raise ScenarioError(
                f"credential value for {connector_id!r} must be a non-empty string"
            )
        secret_name = str(rotation["secret_name"])
        _http_json(
            base_url=base_url,
            token=token,
            method="POST",
            path="/api/v1/platform/secrets",
            body={
                "owner_id": tenant_id,
                "name": secret_name,
                "value": value,
                "description": str(rotation.get("description", "")),
            },
        )
        if tenant_id not in binding_cache:
            encoded = urllib.parse.urlencode({"tenant_id": tenant_id})
            bindings = _http_json(
                base_url=base_url,
                token=token,
                method="GET",
                path=f"/api/v1/connectors/bindings?{encoded}",
            )
            if not isinstance(bindings, list):
                raise ScenarioError("connector binding list response is invalid")
            binding_cache[tenant_id] = bindings
        matches = [
            binding
            for binding in binding_cache[tenant_id]
            if binding.get("connector_id") == connector_id
        ]
        if len(matches) != 1:
            raise ScenarioError(
                f"expected one binding for {connector_id!r}/{tenant_id!r}, "
                f"found {len(matches)}"
            )
        secret_ref = f"secret://{tenant_id}/{secret_name}"
        updated = _http_json(
            base_url=base_url,
            token=token,
            method="PUT",
            path="/api/v1/connectors/bindings",
            body=_binding_body(matches[0], secret_ref),
        )
        if not isinstance(updated, dict):
            raise ScenarioError("connector binding update response is invalid")
        matches[0] = updated
        receipts.append(
            {
                "connector_id": connector_id,
                "tenant_id": tenant_id,
                "binding_revision": updated.get("revision"),
                "secret_ref": secret_ref,
            }
        )
    return receipts


def _artifact_metadata(outputs: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if (
                isinstance(value.get("filename"), str)
                and isinstance(value.get("media_type"), str)
            ):
                artifacts.append(
                    {
                        key: value.get(key)
                        for key in (
                            "filename",
                            "media_type",
                            "size_bytes",
                            "sha256",
                            "relative_path",
                        )
                    }
                )
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(outputs)
    return artifacts


def _run_workflow(
    *,
    case: dict[str, Any],
    base_url: str,
    token: str,
    application_id: str,
    workspace: Path,
) -> dict[str, Any]:
    input_state = json.loads(Path(case["input_file"]).read_text(encoding="utf-8"))
    selected = _json_pointer(input_state, str(case.get("input_pointer", "")))
    prefixes = tuple(str(item) for item in case.get("strip_key_prefixes", []))
    selected = _strip_key_prefixes(selected, prefixes)
    input_name = str(case.get("input_name", "records"))
    request_body = {
        "inputs": {input_name: selected},
        "use_draft": bool(case.get("use_draft", False)),
        "workspace_path": str(workspace),
    }
    run = _http_json(
        base_url=base_url,
        token=token,
        method="POST",
        path=f"/api/v1/applications/{application_id}/runs",
        body=request_body,
    )
    if not isinstance(run, dict):
        raise ScenarioError("workflow run response is invalid")
    run_id = run.get("id") or run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ScenarioError("workflow run response did not include a run id")

    poll_interval = float(case.get("poll_interval_seconds", 1.0))
    timeout_seconds = float(case.get("timeout_seconds", 1200.0))
    max_resumes = int(case.get("max_resumes", 100))
    resume_values = case.get("resume_values")
    started = time.monotonic()
    resume_count = 0
    while True:
        current = _http_json(
            base_url=base_url,
            token=token,
            method="GET",
            path=f"/api/v1/runs/{run_id}",
        )
        if not isinstance(current, dict):
            raise ScenarioError("workflow run detail response is invalid")
        status = current.get("status")
        if status == "paused":
            if not isinstance(resume_values, dict):
                raise ScenarioError("workflow paused without configured resume_values")
            if resume_count >= max_resumes:
                raise ScenarioError(
                    f"workflow exceeded max_resumes={max_resumes}"
                )
            _http_json(
                base_url=base_url,
                token=token,
                method="POST",
                path=f"/api/v1/runs/{run_id}/resume",
                body={"values": resume_values},
            )
            resume_count += 1
        elif status not in {"queued", "running"}:
            artifacts = _artifact_metadata(current.get("outputs"))
            required_media_types = {
                str(item) for item in case.get("required_artifact_media_types", [])
            }
            present_media_types = {
                str(item["media_type"])
                for item in artifacts
                if item.get("media_type")
            }
            missing_media_types = sorted(required_media_types - present_media_types)
            return {
                "run_id": run_id,
                "status": status,
                "error": current.get("error"),
                "duration_seconds": round(time.monotonic() - started, 3),
                "resume_count": resume_count,
                "artifacts": artifacts,
                "missing_artifact_media_types": missing_media_types,
                "output_keys": sorted(
                    current.get("outputs", {}).keys()
                    if isinstance(current.get("outputs"), dict)
                    else []
                ),
            }
        if time.monotonic() - started > timeout_seconds:
            raise ScenarioError(
                f"workflow run {run_id} exceeded timeout_seconds={timeout_seconds}"
            )
        time.sleep(max(0.05, poll_interval))


def _verification_summary(
    verification: dict[str, Any],
) -> dict[str, Any]:
    result_file = Path(str(verification["result_file"]))
    result = json.loads(result_file.read_text(encoding="utf-8"))
    fields = verification.get("result_pointers", {})
    if not isinstance(fields, dict):
        raise ScenarioError("verification.result_pointers must be an object")
    return {
        str(name): _json_pointer(result, str(pointer))
        for name, pointer in fields.items()
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run_case(config: dict[str, Any], case_id: str) -> dict[str, Any]:
    if config.get("schema_version") != "1.0":
        raise ScenarioError("unsupported scenario configuration schema_version")
    cases = config.get("cases")
    if not isinstance(cases, list):
        raise ScenarioError("configuration cases must be an array")
    matches = [case for case in cases if str(case.get("id")) == case_id]
    if len(matches) != 1:
        raise ScenarioError(f"expected one case with id={case_id!r}")
    raw_case = matches[0]
    if not isinstance(raw_case, dict):
        raise ScenarioError("scenario case must be an object")

    config_path = Path(str(config["_config_path"])).resolve()
    variables = {
        key: str(value) for key, value in config.get("variables", {}).items()
    }
    variables.update(
        {
            "case_id": case_id,
            "config_dir": str(config_path.parent),
            "scenario_run_id": uuid.uuid4().hex,
        }
    )
    case = _format_value(raw_case, variables)
    platform = _format_value(config.get("platform", {}), variables)
    if not isinstance(platform, dict):
        raise ScenarioError("platform configuration must be an object")
    base_url = str(platform["base_url"])
    token_state = json.loads(
        Path(str(platform["token_file"])).read_text(encoding="utf-8")
    )
    token = _json_pointer(token_state, str(platform["token_pointer"]))
    if not isinstance(token, str) or not token:
        raise ScenarioError("platform token must be a non-empty string")
    application_id = str(platform["application_id"])
    repository_root = Path(
        str(config.get("command_cwd", config_path.parent))
    ).resolve()
    workspace = _resolve_workspace(
        Path(str(config["workspace_root"])),
        str(case["workspace_path"]),
    )
    started = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "case_id": case_id,
        "application_id": application_id,
        "workspace_path": str(workspace),
        "status": "running",
    }
    try:
        report["setup"] = _run_commands(
            case.get("setup_commands", []),
            cwd=repository_root,
            phase="setup",
        )
        report["credential_rotations"] = _rotate_credentials(
            rotations=case.get("credential_rotations", []),
            base_url=base_url,
            token=token,
            variables=variables,
        )
        workflow = _run_workflow(
            case=case,
            base_url=base_url,
            token=token,
            application_id=application_id,
            workspace=workspace,
        )
        report["workflow"] = workflow
        report["post_run"] = _run_commands(
            case.get("post_run_commands", []),
            cwd=repository_root,
            phase="post_run",
        )
        verification = case.get("verification")
        if verification is not None:
            if not isinstance(verification, dict):
                raise ScenarioError("verification must be an object")
            report["verification_command"] = _run_commands(
                verification.get("commands", []),
                cwd=repository_root,
                phase="verification",
            )
            report["verification"] = _verification_summary(verification)
        workflow_ok = (
            workflow["status"] == "succeeded"
            and not workflow["missing_artifact_media_types"]
        )
        report["status"] = "completed" if workflow_ok else "failed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report_path = Path(str(case["report_file"]))
    report["report_file"] = str(report_path)
    _atomic_json(report_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one declaration-driven external workflow scenario through "
            "Lilies public APIs."
        )
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--case", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ScenarioError("scenario configuration must be an object")
    config["_config_path"] = str(args.config)
    report = run_case(config, str(args.case))
    summary = {
        "case_id": report["case_id"],
        "status": report["status"],
        "workflow_status": report.get("workflow", {}).get("status"),
        "resume_count": report.get("workflow", {}).get("resume_count"),
        "artifact_count": len(report.get("workflow", {}).get("artifacts", [])),
        "verification": report.get("verification"),
        "report_file": report["report_file"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
