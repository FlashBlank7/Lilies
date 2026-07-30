#!/usr/bin/env python3
"""Provision the controlled BookStack host without exposing host credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


HTTP_BASE = "http://127.0.0.1:18020"
ADMIN_EMAIL = "admin@admin.com"
ADMIN_PASSWORD = "password"
ROLE_NAME = "EXP-LILIES-002 Read Only API"
READER_EMAIL = "exp-lilies-002-reader@local.invalid"
TOKEN_PREFIX = "EXP-LILIES-002"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


class BookStackBrowser:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    def get(self, path: str) -> tuple[str, str]:
        with self.opener.open(f"{self.base_url}{path}", timeout=30) as response:
            return response.read().decode("utf-8"), response.geturl()

    def post(self, path: str, fields: dict[str, str]) -> tuple[str, str]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=urlencode(fields).encode("utf-8"),
            method="POST",
        )
        with self.opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8"), response.geturl()

    @staticmethod
    def csrf(html: str) -> str:
        match = re.search(r'name="_token" value="([^"]+)"', html)
        if match is None:
            raise RuntimeError("BookStack form did not contain a CSRF token")
        return match.group(1)

    def login_admin(self) -> None:
        html, _ = self.get("/login")
        _, url = self.post(
            "/login",
            {
                "_token": self.csrf(html),
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
            },
        )
        if url.endswith("/login"):
            raise RuntimeError("BookStack administrator login failed")

    def _settings_links(self, path: str, entity: str) -> list[int]:
        html, _ = self.get(path)
        values = re.findall(
            rf'href="{re.escape(self.base_url)}/settings/{entity}/(\d+)"',
            html,
        )
        return sorted({int(item) for item in values})

    @staticmethod
    def _input_value(html: str, name: str) -> str | None:
        patterns = (
            rf'<input[^>]+name="{re.escape(name)}"[^>]+value="([^"]*)"',
            rf'<input[^>]+value="([^"]*)"[^>]+name="{re.escape(name)}"',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match is not None:
                return match.group(1)
        return None

    def ensure_role(self) -> int:
        for role_id in self._settings_links("/settings/roles", "roles"):
            html, _ = self.get(f"/settings/roles/{role_id}")
            if self._input_value(html, "display_name") == ROLE_NAME:
                return role_id
        html, _ = self.get("/settings/roles/new")
        _, url = self.post(
            "/settings/roles/new",
            {
                "_token": self.csrf(html),
                "display_name": ROLE_NAME,
                "description": (
                    "Controlled-local service role with API access and read-only "
                    "visibility of books, chapters, pages, and revisions."
                ),
                "permissions[access-api]": "true",
                "permissions[bookshelf-view-all]": "true",
                "permissions[book-view-all]": "true",
                "permissions[chapter-view-all]": "true",
                "permissions[page-view-all]": "true",
                "permissions[revision-view-all]": "true",
            },
        )
        match = re.search(r"/settings/roles/(\d+)$", url)
        if match is not None:
            return int(match.group(1))
        for role_id in self._settings_links("/settings/roles", "roles"):
            detail, _ = self.get(f"/settings/roles/{role_id}")
            if self._input_value(detail, "display_name") == ROLE_NAME:
                return role_id
        raise RuntimeError(f"BookStack role creation did not produce a role: {url}")

    def ensure_reader(self, role_id: int) -> int:
        for user_id in self._settings_links("/settings/users", "users"):
            html, _ = self.get(f"/settings/users/{user_id}")
            if self._input_value(html, "email") == READER_EMAIL:
                return user_id
        password = secrets.token_urlsafe(32)
        html, _ = self.get("/settings/users/create")
        _, url = self.post(
            "/settings/users/create",
            {
                "_token": self.csrf(html),
                "name": "EXP-LILIES-002 knowledge reader",
                "email": READER_EMAIL,
                "password": password,
                "password-confirm": password,
                f"roles[{role_id}]": str(role_id),
                "language": "en",
            },
        )
        match = re.search(r"/settings/users/(\d+)$", url)
        if match is not None:
            return int(match.group(1))
        for user_id in self._settings_links("/settings/users", "users"):
            detail, _ = self.get(f"/settings/users/{user_id}")
            if self._input_value(detail, "email") == READER_EMAIL:
                return user_id
        raise RuntimeError(f"BookStack user creation did not produce a user: {url}")

    def _token_ids(self, user_id: int) -> list[int]:
        html, _ = self.get(f"/settings/users/{user_id}")
        values = re.findall(
            rf'href="{re.escape(self.base_url)}/api-tokens/{user_id}/(\d+)\?context=settings"',
            html,
        )
        return sorted({int(item) for item in values})

    def delete_experiment_tokens(self, user_id: int) -> int:
        deleted = 0
        for token_id in self._token_ids(user_id):
            detail, _ = self.get(f"/api-tokens/{user_id}/{token_id}?context=settings")
            if not (self._input_value(detail, "name") or "").startswith(TOKEN_PREFIX):
                continue
            confirmation, _ = self.get(f"/api-tokens/{user_id}/{token_id}/delete")
            self.post(
                f"/api-tokens/{user_id}/{token_id}",
                {
                    "_token": self.csrf(confirmation),
                    "_method": "delete",
                },
            )
            deleted += 1
        return deleted

    def create_token(self, user_id: int, name: str) -> str:
        html, _ = self.get(f"/api-tokens/{user_id}/create?context=settings")
        expires_at = (date.today() + timedelta(days=7)).isoformat()
        detail, url = self.post(
            f"/api-tokens/{user_id}/create",
            {
                "_token": self.csrf(html),
                "name": name,
                "expires_at": expires_at,
            },
        )
        match = re.search(rf"/api-tokens/{user_id}/(\d+)$", url)
        token_id = self._input_value(detail, "token_id")
        readonly_values = re.findall(
            r'<input[^>]+readonly="readonly"[^>]+value="([^"]+)"[^>]*>',
            detail,
        )
        token_secret = readonly_values[-1] if len(readonly_values) >= 2 else None
        if match is None or not token_id or not token_secret:
            raise RuntimeError("BookStack did not return a one-time API token secret")
        return f"Token {token_id}:{token_secret}"


def api_json(
    method: str,
    base_url: str,
    authorization: str,
    path: str,
    body: Any | None = None,
    *,
    expected_error: int | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Authorization": authorization,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = _canonical_json(body)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if expected_error is not None and error.code == expected_error:
            return {"status": error.code}
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"BookStack API {method} {path} returned HTTP {error.code}: {detail}"
        ) from error
    if expected_error is not None:
        raise RuntimeError(f"BookStack API {method} {path} unexpectedly succeeded")
    return json.loads(payload) if payload else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=HTTP_BASE)
    parser.add_argument("--fixtures-file", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--sources-file", type=Path, required=True)
    args = parser.parse_args()

    fixtures = json.loads(args.fixtures_file.read_text(encoding="utf-8"))
    browser = BookStackBrowser(args.base_url)
    browser.login_admin()
    role_id = browser.ensure_role()
    reader_id = browser.ensure_reader(role_id)
    deleted_tokens = browser.delete_experiment_tokens(1)
    deleted_tokens += browser.delete_experiment_tokens(reader_id)
    setup_authorization = browser.create_token(
        1,
        f"{TOKEN_PREFIX} fixture administrator",
    )
    reader_authorization = browser.create_token(
        reader_id,
        f"{TOKEN_PREFIX} workflow reader",
    )

    book = api_json(
        "POST",
        args.base_url,
        setup_authorization,
        "/api/books",
        {
            "name": f"EXP-LILIES-002 controlled knowledge {secrets.token_hex(4)}",
            "description": (
                "Controlled-local source book for access-controlled RAG workflow "
                "verification."
            ),
        },
    )
    book_id = int(book["id"])
    sources: list[dict[str, Any]] = []
    for fixture in fixtures:
        page = api_json(
            "POST",
            args.base_url,
            setup_authorization,
            "/api/pages",
            {
                "book_id": book_id,
                "name": fixture["name"],
                "html": fixture["html"],
                "tags": fixture.get("tags", []),
            },
        )
        page_id = int(page["id"])
        current = api_json(
            "GET",
            args.base_url,
            reader_authorization,
            f"/api/pages/{page_id}",
        )
        sources.append(
            {
                "source_id": fixture["source_key"],
                "url": f"{args.base_url.rstrip('/')}/api/pages/{page_id}",
                "browser_url": f"{args.base_url.rstrip('/')}/books/{book['slug']}/page/{page['slug']}",
                "allowed_roles": fixture["allowed_roles"],
                "page_id": page_id,
                "book_id": book_id,
                "original_updated_at": current["updated_at"],
            }
        )

    read_only_probe = api_json(
        "POST",
        args.base_url,
        reader_authorization,
        "/api/books",
        {"name": "THIS MUST NOT BE CREATED"},
        expected_error=403,
    )
    credential_payload = {
        "api_authorization": reader_authorization,
        "setup_api_authorization": setup_authorization,
        "reader_user_id": reader_id,
        "reader_role_id": role_id,
        "read_only_write_probe_status": read_only_probe["status"],
    }
    args.credential_file.write_bytes(_canonical_json(credential_payload) + b"\n")
    args.credential_file.chmod(0o600)
    args.sources_file.write_bytes(_canonical_json(sources) + b"\n")
    args.sources_file.chmod(0o600)

    result = {
        "status": "ready",
        "book_id": book_id,
        "page_count": len(sources),
        "reader_role": ROLE_NAME,
        "reader_write_probe_status": read_only_probe["status"],
        "rotated_experiment_token_count": deleted_tokens,
        "sources_digest": _sha256(sources),
        "credentials_persisted_mode": "0600",
        "credential_values_exposed": False,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
