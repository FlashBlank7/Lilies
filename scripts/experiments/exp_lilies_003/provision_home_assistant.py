#!/usr/bin/env python3
"""Provision a controlled Home Assistant owner and a long-lived API token."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import websockets


def json_request(
    method: str,
    url: str,
    *,
    body: Any | None = None,
    token: str | None = None,
) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def token_request(url: str, form: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError("Home Assistant token response is not an object")
    return payload


async def create_long_lived_token(
    websocket_url: str,
    access_token: str,
) -> str:
    async with websockets.connect(websocket_url, open_timeout=30) as socket:
        required = json.loads(await socket.recv())
        if required.get("type") != "auth_required":
            raise RuntimeError(f"unexpected websocket greeting: {required}")
        await socket.send(
            json.dumps({"type": "auth", "access_token": access_token})
        )
        accepted = json.loads(await socket.recv())
        if accepted.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant authentication failed: {accepted}")
        await socket.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "auth/long_lived_access_token",
                    "client_name": "EXP-LILIES-003 workflow",
                    "lifespan": 30,
                }
            )
        )
        result = json.loads(await socket.recv())
        if result.get("success") is not True or not result.get("result"):
            raise RuntimeError(f"long-lived token request failed: {result}")
        return str(result["result"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:18030",
    )
    parser.add_argument(
        "--websocket-url",
        default="ws://127.0.0.1:18030/api/websocket",
    )
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()

    onboarding = json_request("GET", f"{args.base_url}/api/onboarding")
    user_step = next(
        item for item in onboarding if item.get("step") == "user"
    )
    if user_step.get("done"):
        raise RuntimeError(
            "Home Assistant is already onboarded; use a fresh controlled config volume"
        )
    username = f"exp003-{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(24)
    client_id = "http://localhost:18030/"
    user = json_request(
        "POST",
        f"{args.base_url}/api/onboarding/users",
        body={
            "client_id": client_id,
            "name": "EXP-LILIES-003 Owner",
            "username": username,
            "password": password,
            "language": "en",
        },
    )
    tokens = token_request(
        f"{args.base_url}/auth/token",
        {
            "grant_type": "authorization_code",
            "code": str(user["auth_code"]),
            "client_id": client_id,
        },
    )
    access_token = str(tokens["access_token"])
    workflow_token = asyncio.run(
        create_long_lived_token(args.websocket_url, access_token)
    )
    credential = {
        "schema_version": "1.0",
        "base_url": args.base_url,
        "websocket_url": args.websocket_url,
        "username": username,
        "password": password,
        "workflow_token": workflow_token,
    }
    args.credential_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        args.credential_file,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(credential, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                "status": "provisioned",
                "credential_file": str(args.credential_file),
                "credential_mode": oct(
                    args.credential_file.stat().st_mode & 0o777
                ),
                "home_assistant_version": json_request(
                    "GET",
                    f"{args.base_url}/api/config",
                    token=workflow_token,
                ).get("version"),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
