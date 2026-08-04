from __future__ import annotations

import json
import stat
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.experiments import real_project_testkit as kit


RUN_ID = "3a0d93c8-6755-41f9-bf91-91847ce1e3e8"


def _paused_run(*, waiting_node_id: str = "review") -> dict[str, Any]:
    return {
        "status": "paused",
        "state": {
            "waiting_node_id": waiting_node_id,
            "snapshot": {
                "workflow": {
                    "nodes": [
                        {
                            "id": "review",
                            "type": "human_input",
                            "config": {
                                "fields": [
                                    {
                                        "name": "approved",
                                        "label": "Approve",
                                        "type": "boolean",
                                        "required": True,
                                        "options": [],
                                    },
                                    {
                                        "name": "decision",
                                        "label": "Decision",
                                        "type": "string",
                                        "required": True,
                                        "options": ["approve", "hold_for_review"],
                                    },
                                    {
                                        "name": "comment",
                                        "label": "Comment",
                                        "type": "string",
                                        "required": True,
                                        "options": [],
                                    },
                                    {
                                        "name": "optional_note",
                                        "label": "Optional",
                                        "type": "string",
                                        "required": False,
                                        "options": [],
                                    },
                                ]
                            },
                        }
                    ],
                    "edges": [],
                }
            },
        },
    }


def test_conservative_human_resume_values_uses_only_public_schema() -> None:
    values = kit.conservative_human_resume_values(_paused_run())

    assert values == {
        "approved": False,
        "decision": "hold_for_review",
        "comment": "held_for_manual_review",
    }


def test_conservative_human_resume_values_finds_scoped_nested_node() -> None:
    run = _paused_run(waiting_node_id="records[2].review")
    values = kit.conservative_human_resume_values(run)

    assert values["approved"] is False


def test_conservative_human_resume_values_refuses_approve_only_choice() -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[1]["options"] = ["approve"]

    with pytest.raises(RuntimeError, match="no conservative string option"):
        kit.conservative_human_resume_values(run)


def test_conservative_human_resume_values_does_not_match_no_as_substring() -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[1]["options"] = ["notify_then_approve"]

    with pytest.raises(RuntimeError, match="no conservative string option"):
        kit.conservative_human_resume_values(run)


def test_conservative_human_resume_values_rejects_approve_after_review() -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[1]["options"] = ["approve_after_review"]

    with pytest.raises(RuntimeError, match="no conservative string option"):
        kit.conservative_human_resume_values(run)


@pytest.mark.parametrize(
    "name,expected",
    [("approved", False), ("write_allowed", False), ("同意写入", False)],
)
def test_conservative_boolean_uses_field_semantics(name: str, expected: bool) -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[:] = [
        {
            "name": name,
            "label": "ignored",
            "type": "boolean",
            "required": True,
            "options": [],
        }
    ]
    assert kit.conservative_human_resume_values(run) == {name: expected}


@pytest.mark.parametrize(
    "name",
    [
        "enabled",
        "rejected",
        "hold",
        "reject_write",
        "reject_disabled",
        "do_not_approve",
        "不拒绝",
    ],
)
def test_conservative_boolean_rejects_ambiguous_semantics(name: str) -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[:] = [
        {
            "name": name,
            "label": "ignored",
            "type": "boolean",
            "required": True,
            "options": [],
        }
    ]
    with pytest.raises(RuntimeError, match="boolean semantics are ambiguous"):
        kit.conservative_human_resume_values(run)


@pytest.mark.parametrize(
    "option",
    [
        "do_not_reject",
        "doNotReject",
        "cannot_reject",
        "不拒绝",
        "never_hold",
        "proceed",
        "create_record",
        "review_then_write",
    ],
)
def test_conservative_string_rejects_double_negation_and_mutation_markers(
    option: str,
) -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[:] = [
        {
            "name": "decision",
            "label": "Decision",
            "type": "string",
            "required": True,
            "options": [option],
        }
    ]
    with pytest.raises(RuntimeError, match="no conservative string option"):
        kit.conservative_human_resume_values(run)


@pytest.mark.parametrize("field_type", ["number", "array", "object"])
def test_typed_human_fields_ignore_builder_supplied_safe_default(field_type: str) -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    field = {
        "name": "safe_value",
        "label": "Safe value",
        "type": field_type,
        "required": True,
        "options": [],
    }
    fields[:] = [field]
    with pytest.raises(RuntimeError, match="no platform-owned safe value"):
        kit.conservative_human_resume_values(run)

    field["safe_default"] = True
    field["default"] = {"number": 0, "array": [], "object": {}}[field_type]
    with pytest.raises(RuntimeError, match="no platform-owned safe value"):
        kit.conservative_human_resume_values(run)


def test_required_free_text_is_limited_to_exact_neutral_roles() -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[:] = [
        {
            "name": "decision_reasoning",
            "label": "Decision",
            "type": "string",
            "required": True,
            "options": [],
        }
    ]
    with pytest.raises(RuntimeError, match="free text role"):
        kit.conservative_human_resume_values(run)


@pytest.mark.parametrize(
    "option",
    ["reject=false", "avoid_reject", "rejection_not_required", "manual_review_later"],
)
def test_string_options_are_exact_allowlist_not_substring_matches(option: str) -> None:
    run = _paused_run()
    fields = run["state"]["snapshot"]["workflow"]["nodes"][0]["config"]["fields"]
    fields[:] = [
        {
            "name": "decision",
            "label": "Decision",
            "type": "string",
            "required": True,
            "options": [option],
        }
    ]
    with pytest.raises(RuntimeError, match="no conservative string option"):
        kit.conservative_human_resume_values(run)


def test_run_workflow_handles_multiple_pauses_and_binds_exact_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[Mapping[str, Any]] = [
        {"run_id": RUN_ID, "status": "queued", "version": 7},
        _paused_run(),
        {"run_id": RUN_ID, "status": "running"},
        _paused_run(),
        {"run_id": RUN_ID, "status": "succeeded", "outputs": {"ok": True}},
    ]
    calls: list[tuple[str, str, Any]] = []

    def fake_platform_json(
        method: str,
        _base_url: str,
        _token: str,
        path: str,
        body: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        calls.append((method, path, body))
        if method == "POST" and path.endswith("/resume"):
            return {"status": "running"}
        return dict(responses.pop(0))

    monkeypatch.setattr(kit, "platform_json", fake_platform_json)
    monkeypatch.setattr(kit.time, "sleep", lambda _seconds: None)

    result = kit.run_workflow(
        base_url="http://127.0.0.1:18100",
        token="not-logged",
        application_id="app-1",
        version=7,
        inputs={},
        resume_resolver=kit.conservative_human_resume_values,
        max_resume_count=2,
    )

    assert result["status"] == "succeeded"
    assert result["resume_count"] == 2
    resume_calls = [call for call in calls if call[1].endswith("/resume")]
    assert [call[2]["values"]["approved"] for call in resume_calls] == [False, False]


def test_run_workflow_rejects_version_drift_before_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_platform_json(
        method: str,
        _base_url: str,
        _token: str,
        path: str,
        _body: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((method, path))
        if path.endswith("/runs"):
            return {"run_id": RUN_ID, "status": "queued", "version": 8}
        return {"run_id": RUN_ID, "status": "cancelling"}

    monkeypatch.setattr(kit, "platform_json", fake_platform_json)

    with pytest.raises(kit.WorkflowStartError, match="exact requested version") as captured:
        kit.run_workflow(
            base_url="http://127.0.0.1:18100",
            token="not-logged",
            application_id="app-1",
            version=7,
            inputs={},
        )
    assert calls == [
        ("POST", "/api/v1/applications/app-1/runs"),
        ("POST", f"/api/v1/runs/{RUN_ID}/cancel"),
    ]
    assert captured.value.run_receipt == {
        "response_type": "object",
        "run_id": RUN_ID,
        "observed_version": 8,
        "created_status": "queued",
        "cancel_attempted": True,
        "cancel_result": "cancelling",
    }


@pytest.mark.parametrize(
    "raw_run_id",
    (
        RUN_ID.upper(),
        RUN_ID.replace("-", ""),
        "{" + RUN_ID + "}",
    ),
    ids=("uppercase", "hex_without_hyphens", "braced"),
)
def test_run_workflow_canonicalizes_uuid_variants_before_orphan_cancel(
    raw_run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_platform_json(
        method: str,
        _base_url: str,
        _token: str,
        path: str,
        _body: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((method, path))
        if path.endswith("/runs"):
            return {"run_id": raw_run_id, "status": "queued", "version": 8}
        return {"run_id": RUN_ID, "status": "cancelling"}

    monkeypatch.setattr(kit, "platform_json", fake_platform_json)

    with pytest.raises(kit.WorkflowStartError, match="exact requested version") as captured:
        kit.run_workflow(
            base_url="http://127.0.0.1:18100",
            token="not-logged",
            application_id="app-1",
            version=7,
            inputs={},
        )

    assert calls == [
        ("POST", "/api/v1/applications/app-1/runs"),
        ("POST", f"/api/v1/runs/{RUN_ID}/cancel"),
    ]
    assert captured.value.run_receipt["run_id"] == RUN_ID
    assert captured.value.run_receipt["cancel_attempted"] is True
    assert captured.value.run_receipt["cancel_result"] == "cancelling"
    assert raw_run_id not in json.dumps(captured.value.run_receipt)


def test_run_workflow_never_cancels_malicious_non_uuid_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malicious = "../../runs/Bearer-PROTECTED/cancel?next=evil.invalid"
    calls: list[tuple[str, str]] = []

    def fake_platform_json(
        method: str,
        _base_url: str,
        _token: str,
        path: str,
        _body: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((method, path))
        return {"run_id": malicious, "status": "queued", "version": 7}

    monkeypatch.setattr(kit, "platform_json", fake_platform_json)

    with pytest.raises(kit.WorkflowStartError, match="safe canonical run identity") as captured:
        kit.run_workflow(
            base_url="http://127.0.0.1:18100",
            token="not-logged",
            application_id="app-1",
            version=7,
            inputs={},
        )

    assert calls == [("POST", "/api/v1/applications/app-1/runs")]
    assert captured.value.run_receipt["run_id"] is None
    assert captured.value.run_receipt["cancel_attempted"] is False
    assert captured.value.run_receipt["cancel_result"] == "unsafe_run_identity"
    assert malicious not in json.dumps(captured.value.run_receipt)


def test_run_workflow_cancels_safe_run_from_malformed_creation_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_platform_json(
        method: str,
        _base_url: str,
        _token: str,
        path: str,
        _body: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((method, path))
        if path.endswith("/runs"):
            return {"run_id": RUN_ID, "version": 7, "status": {"bad": "shape"}}
        return {"run_id": RUN_ID, "status": "cancelled"}

    monkeypatch.setattr(kit, "platform_json", fake_platform_json)
    with pytest.raises(kit.WorkflowStartError, match="malformed run creation") as captured:
        kit.run_workflow(
            base_url="http://127.0.0.1:18100",
            token="not-logged",
            application_id="app-1",
            version=7,
            inputs={},
        )
    assert calls[-1] == ("POST", f"/api/v1/runs/{RUN_ID}/cancel")
    assert captured.value.run_receipt["run_id"] == RUN_ID
    assert captured.value.run_receipt["created_status"] == "unknown"
    assert captured.value.run_receipt["cancel_attempted"] is True
    assert captured.value.run_receipt["cancel_result"] == "cancelled"


def test_run_workflow_fails_closed_at_pause_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = {"run_id": RUN_ID, "status": "queued", "version": 7}
    responses = iter((created, _paused_run(), _paused_run()))

    def fake_platform_json(*args: Any, **_kwargs: Any) -> Any:
        if args[0] == "POST" and str(args[3]).endswith("/resume"):
            return {"status": "running"}
        return next(responses)

    monkeypatch.setattr(kit, "platform_json", fake_platform_json)
    monkeypatch.setattr(kit.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="bounded acceptance pauses"):
        kit.run_workflow(
            base_url="http://127.0.0.1:18100",
            token="not-logged",
            application_id="app-1",
            version=7,
            inputs={},
            resume_resolver=kit.conservative_human_resume_values,
            max_resume_count=1,
        )


def test_run_workflow_rejects_non_integer_pause_limit() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        kit.run_workflow(
            base_url="http://127.0.0.1:18100",
            token="not-logged",
            application_id="app-1",
            version=7,
            inputs={},
            max_resume_count=1.5,  # type: ignore[arg-type]
        )


def test_http_json_rejects_invalid_response_limit() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        kit.http_json("GET", "http://127.0.0.1:18100", max_response_bytes=0)


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:18100",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:18100/path",
        "http://127.0.0.1:18100?next=http://evil.invalid",
        "http://127.0.0.1:18100#fragment",
        "http://user@127.0.0.1:18100",
        "http://127.0.0.1.evil.invalid:18100",
        "http://2130706433:18100",
        "http://[::1]:18100",
        " http://127.0.0.1:18100",
    ],
)
def test_loopback_base_url_rejects_origin_tricks(value: str) -> None:
    with pytest.raises(ValueError, match="exact loopback"):
        kit.normalize_loopback_base_url(value)


def test_loopback_base_url_accepts_only_canonical_origins() -> None:
    assert kit.normalize_loopback_base_url("http://127.0.0.1:18100/") == "http://127.0.0.1:18100"
    assert kit.normalize_loopback_base_url("http://localhost:18100") == "http://localhost:18100"


def test_http_json_uses_no_proxy_and_no_redirect_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"ok":true}'

    class Opener:
        def open(self, request: urllib.request.Request, *, timeout: float) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def fake_build_opener(*handlers: Any) -> Opener:
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:9999")
    monkeypatch.setattr(kit.urllib.request, "build_opener", fake_build_opener)
    result = kit.http_json(
        "GET",
        "http://127.0.0.1:18100/health",
        headers={"Authorization": "Bearer private"},
    )
    assert result == {"ok": True}
    handlers = captured["handlers"]
    proxy_handlers = [item for item in handlers if isinstance(item, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    redirect_handlers = [item for item in handlers if isinstance(item, kit._NoRedirectHandler)]
    assert len(redirect_handlers) == 1
    assert (
        redirect_handlers[0].redirect_request(
            urllib.request.Request("http://127.0.0.1:18100"),
            None,
            302,
            "Found",
            {},
            "http://attacker.invalid",
        )
        is None
    )


@pytest.mark.parametrize("path", ["//evil.invalid/x", "https://evil.invalid/x", "/ok#x"])
def test_platform_json_rejects_cross_origin_paths(path: str) -> None:
    with pytest.raises(ValueError, match="stay on the loopback origin"):
        kit.platform_json(
            "GET",
            "http://127.0.0.1:18100",
            "private",
            path,
        )


def test_write_report_is_private_and_valid_json(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "report.json"
    kit.write_report(output, {"status": "passed"})

    assert json.loads(output.read_text()) == {"status": "passed"}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
