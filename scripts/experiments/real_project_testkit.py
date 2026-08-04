#!/usr/bin/env python3
"""Small public-API test kit for repeatable real-project workflow acceptance."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID


MAX_HTTP_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RESUME_COUNT = 1_000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class WorkflowStartError(RuntimeError):
    """A rejected creation response with a safe cancellation/evidence receipt."""

    def __init__(self, message: str, run_receipt: dict[str, Any]) -> None:
        super().__init__(message)
        self.run_receipt = run_receipt


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def normalize_loopback_base_url(value: str) -> str:
    """Return one exact HTTP loopback origin or reject it."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("platform base URL must be an exact loopback HTTP origin")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("platform base URL must be an exact loopback HTTP origin") from error
    host = parsed.hostname
    if (
        parsed.scheme != "http"
        or host not in LOOPBACK_HOSTS
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.netloc != f"{host}:{port}"
    ):
        raise ValueError("platform base URL must be an exact loopback HTTP origin")
    return f"http://{host}:{port}"


def _no_proxy_no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


def _normalized_schema_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "_", normalized).strip("_")


_CONSERVATIVE_STRING_OPTIONS = frozenset(
    {
        "reject",
        "deny",
        "hold",
        "skip",
        "manual_review",
        "hold_for_review",
        "cancel",
        "stop",
        "拒绝",
        "否决",
        "暂存",
        "跳过",
        "人工复核",
        "暂存待复核",
        "取消",
        "停止",
    }
)
_POSITIVE_BOOLEAN_FIELDS = frozenset(
    {
        "approve",
        "approved",
        "is_approved",
        "approval_granted",
        "accept",
        "accepted",
        "is_accepted",
        "allow_write",
        "write_allowed",
        "approve_write",
        "write_approved",
        "should_write",
        "confirm_write",
        "write_confirmed",
        "批准",
        "同意",
        "同意写入",
        "批准写入",
    }
)
_NEUTRAL_FREE_TEXT_FIELDS = frozenset(
    {
        "comment",
        "comments",
        "note",
        "notes",
        "reason",
        "review_comment",
        "review_note",
        "hold_reason",
        "备注",
        "说明",
        "原因",
    }
)


def _is_conservative_string_option(option: str) -> bool:
    return _normalized_schema_token(option) in _CONSERVATIVE_STRING_OPTIONS


def _boolean_conservative_value(name: str) -> bool:
    if _normalized_schema_token(name) not in _POSITIVE_BOOLEAN_FIELDS:
        raise RuntimeError("paused Human Input boolean semantics are ambiguous")
    return False


def _public_json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _canonical_run_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        canonical = str(UUID(value))
    except ValueError:
        return None
    return canonical


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: float = 60,
    expose_error_detail: bool = False,
    max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
) -> Any:
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes <= 0
    ):
        raise ValueError("max_response_bytes must be a positive integer")
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
        with _no_proxy_no_redirect_opener().open(request, timeout=timeout) as response:
            payload = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as error:
        detail = ""
        if expose_error_detail:
            detail = error.read(4_097).decode("utf-8", errors="replace")[:4_096]
        raise RuntimeError(
            f"{method} {urllib.parse.urlsplit(url)._replace(query='', fragment='').geturl()} "
            f"returned HTTP {error.code}" + (f": {detail}" if detail else "")
        ) from error
    if len(payload) > max_response_bytes:
        raise RuntimeError("HTTP response exceeded the configured byte limit")
    try:
        return json.loads(payload) if payload else None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("HTTP response was not valid JSON") from error


def platform_json(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
    *,
    expose_error_detail: bool = False,
) -> Any:
    normalized_base_url = normalize_loopback_base_url(base_url)
    if not isinstance(path, str):
        raise ValueError("platform API path must stay on the loopback origin")
    parsed_path = urllib.parse.urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.fragment
    ):
        raise ValueError("platform API path must stay on the loopback origin")
    return http_json(
        method,
        f"{normalized_base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        body=body,
        expose_error_detail=expose_error_detail,
    )


def conservative_human_resume_values(run: Mapping[str, Any]) -> dict[str, Any]:
    """Return a schema-derived hold/reject response without inspecting case data."""

    state = run.get("state")
    if not isinstance(state, Mapping):
        raise RuntimeError("paused run omitted its public state")
    waiting_node_id = state.get("waiting_node_id")
    snapshot = state.get("snapshot")
    if not isinstance(waiting_node_id, str) or not isinstance(snapshot, Mapping):
        raise RuntimeError("paused run omitted its waiting-node identity")
    workflow = snapshot.get("workflow")
    if not isinstance(workflow, Mapping):
        raise RuntimeError("paused run omitted its public workflow schema")

    block_node_id = waiting_node_id.rsplit(".", 1)[-1]
    pending: list[Mapping[str, Any]] = [workflow]
    matches: list[Mapping[str, Any]] = []
    while pending:
        current = pending.pop()
        nodes = current.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            if node.get("id") == block_node_id and node.get("type") == "human_input":
                matches.append(node)
            config = node.get("config")
            nested = config.get("workflow") if isinstance(config, Mapping) else None
            if isinstance(nested, Mapping):
                pending.append(nested)
    if len(matches) != 1:
        raise RuntimeError("paused Human Input schema is not uniquely identifiable")
    config = matches[0].get("config")
    fields = config.get("fields") if isinstance(config, Mapping) else None
    if not isinstance(fields, list) or not fields:
        raise RuntimeError("paused Human Input schema has no fields")

    values: dict[str, Any] = {}
    for field in fields:
        if not isinstance(field, Mapping):
            raise RuntimeError("paused Human Input field schema is invalid")
        name = field.get("name")
        field_type = field.get("type", "string")
        required = field.get("required", True)
        options = field.get("options", [])
        if not isinstance(name, str) or not name or not isinstance(required, bool):
            raise RuntimeError("paused Human Input field identity is invalid")
        if not required:
            continue
        if field_type == "boolean":
            values[name] = _boolean_conservative_value(name)
        elif field_type in {"number", "array", "object"}:
            raise RuntimeError(
                "paused Human Input collection or number has no platform-owned safe value"
            )
        elif field_type == "string":
            if not isinstance(options, list) or any(
                not isinstance(option, str) or not option for option in options
            ):
                raise RuntimeError("paused Human Input options are invalid")
            preferred = next(
                (option for option in options if _is_conservative_string_option(option)),
                None,
            )
            if options and preferred is None:
                raise RuntimeError("paused Human Input has no conservative string option")
            if not options and _normalized_schema_token(name) not in _NEUTRAL_FREE_TEXT_FIELDS:
                raise RuntimeError(
                    "paused Human Input free text role is not conservatively recognized"
                )
            values[name] = preferred or "held_for_manual_review"
        else:
            raise RuntimeError(f"unsupported Human Input field type: {field_type}")
    return values


def run_workflow(
    *,
    base_url: str,
    token: str,
    application_id: str,
    version: int,
    inputs: dict[str, Any],
    workspace_path: str = ".",
    resume_values: dict[str, Any] | None = None,
    resume_resolver: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    max_resume_count: int = DEFAULT_MAX_RESUME_COUNT,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    if (
        isinstance(max_resume_count, bool)
        or not isinstance(max_resume_count, int)
        or max_resume_count < 0
    ):
        raise ValueError("max_resume_count must be a non-negative integer")
    if resume_values is not None and resume_resolver is not None:
        raise ValueError("provide resume_values or resume_resolver, not both")
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
    raw_run_id = created.get("run_id") if isinstance(created, Mapping) else None
    run_id = _canonical_run_id(raw_run_id)
    observed_version = created.get("version") if isinstance(created, Mapping) else None
    public_version = (
        observed_version
        if isinstance(observed_version, int) and not isinstance(observed_version, bool)
        else None
    )
    raw_status = created.get("status") if isinstance(created, Mapping) else None
    public_status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in {"queued", "running"}
        else "unknown"
    )
    creation_receipt = {
        "response_type": _public_json_type(created),
        "run_id": run_id,
        "observed_version": public_version,
        "created_status": public_status,
        "cancel_attempted": False,
        "cancel_result": "not_required" if run_id is not None else "unsafe_run_identity",
    }
    rejection: str | None = None
    if not isinstance(created, Mapping) or run_id is None:
        rejection = "platform did not return a safe canonical run identity"
    elif public_version != version:
        rejection = "platform did not start the exact requested version"
    elif public_status == "unknown":
        rejection = "platform returned a malformed run creation response"
    if rejection is not None:
        if run_id is not None:
            creation_receipt["cancel_attempted"] = True
            creation_receipt["cancel_result"] = "failed"
            try:
                cancelled = platform_json(
                    "POST",
                    base_url,
                    token,
                    f"/api/v1/runs/{run_id}/cancel",
                )
                if (
                    isinstance(cancelled, Mapping)
                    and cancelled.get("run_id") == run_id
                    and cancelled.get("status") in {"cancelling", "cancelled"}
                ):
                    creation_receipt["cancel_result"] = str(cancelled["status"])
            except BaseException:
                pass
        raise WorkflowStartError(rejection, creation_receipt)
    assert run_id is not None
    resume_count = 0
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = platform_json("GET", base_url, token, f"/api/v1/runs/{run_id}")
        if not isinstance(run, Mapping):
            raise RuntimeError("platform returned an invalid run document")
        status = run.get("status")
        if not isinstance(status, str) or not status:
            raise RuntimeError("platform run document omitted its status")
        if status == "paused":
            if resume_count >= max_resume_count:
                raise RuntimeError(f"run {run_id} exceeded its bounded acceptance pauses")
            if resume_resolver is not None:
                current_resume_values = resume_resolver(run)
            else:
                current_resume_values = resume_values
            if current_resume_values is None:
                raise RuntimeError(f"run {run_id} paused without an acceptance response")
            if not isinstance(current_resume_values, dict):
                raise RuntimeError("acceptance response must be a JSON object")
            platform_json(
                "POST",
                base_url,
                token,
                f"/api/v1/runs/{run_id}/resume",
                {"values": current_resume_values},
            )
            resume_count += 1
        elif status not in {"queued", "running"}:
            result = dict(run)
            result["resume_count"] = resume_count
            return result
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_seconds}s")


def wait_run(
    *,
    base_url: str,
    token: str,
    run_id: str,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = platform_json("GET", base_url, token, f"/api/v1/runs/{run_id}")
        if run["status"] not in {"queued", "running", "paused"}:
            return run
        time.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_seconds}s")


def wait_timer(
    *,
    base_url: str,
    token: str,
    timer_key: str,
    statuses: set[str],
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    encoded_key = urllib.parse.quote(timer_key, safe="")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        timer = platform_json(
            "GET",
            base_url,
            token,
            f"/api/v1/event-timers/{encoded_key}",
        )
        if timer.get("status") in statuses:
            return timer
        time.sleep(0.05)
    raise TimeoutError(
        f"timer {timer_key} did not reach {sorted(statuses)} within {timeout_seconds}s"
    )


def run_trace(
    *,
    base_url: str,
    token: str,
    run_id: str,
) -> list[dict[str, Any]]:
    return platform_json("GET", base_url, token, f"/v1/streams/{run_id}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
