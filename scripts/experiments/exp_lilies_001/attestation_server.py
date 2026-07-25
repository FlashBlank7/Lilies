from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


IDENTITY = b'{"identity":"exp-lilies-001-r4-real-hosts"}'
PAPERLESS_VERSION = "2.20.15"
INVENTREE_VERSION = "1.4.2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AttestationError(RuntimeError):
    """The exact real-host identity could not be authenticated."""


@dataclass(frozen=True)
class HostConfiguration:
    paperless_url: str
    paperless_token: str
    inventree_url: str
    inventree_token: str
    attestation_secret: bytes


def _load_configuration() -> HostConfiguration:
    secret_value = os.environ.get("EXP_LILIES_ATTESTATION_SECRET", "")
    attestation_secret = secret_value.encode("utf-8")
    if len(attestation_secret) < 32:
        raise AttestationError(
            "environment attestation secret must contain at least 32 bytes"
        )
    configuration = HostConfiguration(
        paperless_url=os.environ.get(
            "EXP_LILIES_PAPERLESS_URL",
            "http://127.0.0.1:18000",
        ).rstrip("/"),
        paperless_token=os.environ.get(
            "EXP_LILIES_PAPERLESS_VERIFIER_TOKEN",
            "",
        ),
        inventree_url=os.environ.get(
            "EXP_LILIES_INVENTREE_URL",
            "http://127.0.0.1:18001",
        ).rstrip("/"),
        inventree_token=os.environ.get(
            "EXP_LILIES_INVENTREE_VERIFIER_TOKEN",
            "",
        ),
        attestation_secret=attestation_secret,
    )
    if not configuration.paperless_token or not configuration.inventree_token:
        raise AttestationError("read-only verifier tokens are required")
    return configuration


def _read_json(
    url: str,
    *,
    authorization: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": authorization,
            "User-Agent": "Lilies-EXP-LILIES-001-Attestation/1.0",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise AttestationError("host identity response exceeds its limit")
            if int(response.status) != 200:
                raise AttestationError("host identity endpoint did not return 200")
            headers = {
                str(name).casefold(): str(value)
                for name, value in response.headers.items()
            }
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        raise AttestationError("host identity request failed") from error
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AttestationError("host identity response is not JSON") from error
    if not isinstance(value, dict):
        raise AttestationError("host identity response must be a JSON object")
    return value, headers


def _find_version(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {
                "version",
                "server_version",
                "serverversion",
                "inventree_version",
            } and isinstance(item, str):
                return item.lstrip("v")
        for item in value.values():
            if found := _find_version(item):
                return found
    elif isinstance(value, list):
        for item in value:
            if found := _find_version(item):
                return found
    return None


def verify_real_hosts(configuration: HostConfiguration) -> None:
    _paperless, paperless_headers = _read_json(
        f"{configuration.paperless_url}/api/documents/?page_size=1",
        authorization=f"Token {configuration.paperless_token}",
    )
    paperless_version = paperless_headers.get("x-version", "").lstrip("v")
    if paperless_version != PAPERLESS_VERSION:
        raise AttestationError("Paperless host version does not match the lock")

    inventree, _inventree_headers = _read_json(
        f"{configuration.inventree_url}/api/",
        authorization=f"Token {configuration.inventree_token}",
    )
    inventree_version = _find_version(inventree)
    if inventree_version != INVENTREE_VERSION:
        raise AttestationError("InvenTree host version does not match the lock")


class _Handler(BaseHTTPRequestHandler):
    server_version = "LiliesEnvironmentAttestation/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/identity":
            self.send_error(404)
            return
        challenge = self.headers.get("X-Lilies-Attestation-Challenge", "")
        if not challenge.startswith("sha256:") or len(challenge) != 71:
            self.send_error(400)
            return
        configuration: HostConfiguration = self.server.configuration  # type: ignore[attr-defined]
        try:
            verify_real_hosts(configuration)
        except AttestationError:
            payload = b'{"ready":false}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        signature = (
            "sha256:"
            + hmac.new(
                configuration.attestation_secret,
                challenge.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(IDENTITY)))
        self.send_header("X-Lilies-Environment-Attestation", signature)
        self.end_headers()
        self.wfile.write(IDENTITY)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authenticate the frozen EXP-LILIES-001 real hosts."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18002)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configuration = _load_configuration()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.configuration = configuration  # type: ignore[attr-defined]
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
