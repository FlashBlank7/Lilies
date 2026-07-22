from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxOperation,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from tests.test_runtime import ScriptedProvider


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="internal-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )


def test_http_application_create_atomically_allows_one_callback_and_one_application(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        services = client.app.state.services
        assignment_id = uuid4()
        session_id = uuid4()
        issued = client.portal.call(
            services.platform_blackbox_auth.issue_credential,
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=session_id,
                scopes=list(PlatformBlackboxScope),
                application_ids=[],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        token = issued.access_token.get_secret_value()
        base_headers = {
            "Authorization": f"Bearer {token}",
            "X-Lilies-Assignment-ID": str(assignment_id),
            "X-Lilies-Session-ID": str(session_id),
            "X-Lilies-Contract-Digest": "sha256:" + "0" * 64,
            "X-Lilies-Tool-Call-ID": "concurrent-contract-tool",
            "X-Lilies-Idempotency-Key": "concurrent-contract-key-0001",
        }
        contract_response = client.get(
            "/api/v1/lilies/platform-contract",
            headers=base_headers,
        )
        assert contract_response.status_code == 200, contract_response.text
        contract_digest = contract_response.json()["data"]["contract_digest"]

        async def exercise_race() -> tuple[list[httpx.Response], int, list, list]:
            first_callback_entered = asyncio.Event()
            release_first_callback = asyncio.Event()
            callback_calls = 0
            original_create = services.workflow_store.create_application

            async def held_create(request):
                nonlocal callback_calls
                callback_calls += 1
                if callback_calls == 1:
                    first_callback_entered.set()
                    await release_first_callback.wait()
                return await original_create(request)

            def headers(key: str) -> dict[str, str]:
                return {
                    **base_headers,
                    "X-Lilies-Contract-Digest": contract_digest,
                    "X-Lilies-Tool-Call-ID": f"tool-{key}",
                    "X-Lilies-Idempotency-Key": key,
                }

            transport = httpx.ASGITransport(app=client.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http_client:
                with patch.object(
                    services.workflow_store,
                    "create_application",
                    new=held_create,
                ):
                    first_task = asyncio.create_task(
                        http_client.post(
                            "/api/v1/lilies/applications",
                            headers=headers("concurrent-create-key-0001"),
                            json={"name": "First concurrent application"},
                        )
                    )
                    await asyncio.wait_for(first_callback_entered.wait(), timeout=2)
                    second_response = await http_client.post(
                        "/api/v1/lilies/applications",
                        headers=headers("concurrent-create-key-0002"),
                        json={"name": "Second concurrent application"},
                    )
                    release_first_callback.set()
                    first_response = await asyncio.wait_for(first_task, timeout=2)

            applications = await services.workflow_store.list_applications()
            credential = await services.platform_blackbox_auth.authenticate_credential(token)
            audit = await services.platform_blackbox_auth.list_audit(
                assignment_id=assignment_id
            )
            return [first_response, second_response], callback_calls, applications, [
                credential,
                audit,
            ]

        responses, callback_calls, applications, auth_evidence = client.portal.call(
            exercise_race
        )
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [201, 404]
        denied = next(response for response in responses if response.status_code == 404)
        assert denied.json()["error"]["code"] == "not_found"
        assert callback_calls == 1
        assert len(applications) == 1

        credential, audit = auth_evidence
        assert [str(value) for value in credential.application_ids] == [
            applications[0]["id"]
        ]
        create_audit = [
            item
            for item in audit
            if item.operation is PlatformBlackboxOperation.application_create
        ]
        assert [item.outcome for item in create_audit].count("authorized") == 1
        assert [item.outcome for item in create_audit].count("completed") == 1
        assert [item.outcome for item in create_audit].count("denied") == 1
        denial = next(item for item in create_audit if item.outcome == "denied")
        assert denial.reason_code == "application_denied"
