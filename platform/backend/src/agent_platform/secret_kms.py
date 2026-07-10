from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any, Protocol


class SecretKMSProvider(Protocol):
    @property
    def provider_id(self) -> str:
        ...

    @property
    def primary_key_id(self) -> str:
        ...

    def wrap_data_key(self, data_key: bytes, *, key_id: str | None = None) -> dict[str, Any]:
        ...

    def unwrap_data_key(self, wrapped_data_key: dict[str, Any]) -> bytes:
        ...

    def status(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LocalSecretKMSProvider:
    provider_id: str
    primary_key_id: str
    wrapping_keys: dict[str, str]

    def __post_init__(self) -> None:
        if not self.provider_id or "/" in self.provider_id or ":" in self.provider_id:
            raise ValueError("secret KMS provider id must not be empty or contain / or :")
        if not self.primary_key_id:
            raise ValueError("secret KMS primary key id must not be empty")
        if self.primary_key_id not in self.wrapping_keys:
            raise ValueError(f"secret KMS primary key is not configured: {self.primary_key_id}")

    def wrap_data_key(self, data_key: bytes, *, key_id: str | None = None) -> dict[str, Any]:
        selected_key_id = key_id or self.primary_key_id
        wrapping_key = self.wrapping_keys.get(selected_key_id)
        if not wrapping_key:
            raise ValueError(f"secret KMS key is not configured: {selected_key_id}")
        nonce = os.urandom(16)
        wrapping_material = self._derive_wrapping_material(wrapping_key, selected_key_id)
        wrapped = self._xor_bytes(data_key, self._keystream(wrapping_material, nonce, len(data_key)))
        payload = {
            "algorithm": "local-hmac-sha256-xor-keywrap",
            "key_id": selected_key_id,
            "nonce": self._b64(nonce),
            "provider_id": self.provider_id,
            "wrapped": self._b64(wrapped),
            "version": 1,
        }
        payload["tag"] = self._b64(hmac.new(wrapping_material, self._stable_payload(payload), hashlib.sha256).digest())
        return payload

    def unwrap_data_key(self, wrapped_data_key: dict[str, Any]) -> bytes:
        provider_id = str(wrapped_data_key.get("provider_id") or "")
        if provider_id != self.provider_id:
            raise ValueError(f"secret KMS provider mismatch: {provider_id}")
        key_id = str(wrapped_data_key.get("key_id") or "")
        wrapping_key = self.wrapping_keys.get(key_id)
        if not wrapping_key:
            raise ValueError(f"secret KMS key is not configured: {key_id}")
        try:
            tag = self._unb64(str(wrapped_data_key["tag"]))
            nonce = self._unb64(str(wrapped_data_key["nonce"]))
            wrapped = self._unb64(str(wrapped_data_key["wrapped"]))
        except Exception as error:
            raise ValueError("secret KMS wrapped data key is invalid") from error
        wrapping_material = self._derive_wrapping_material(wrapping_key, key_id)
        expected = hmac.new(wrapping_material, self._stable_payload({k: v for k, v in wrapped_data_key.items() if k != "tag"}), hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValueError("secret KMS wrapped data key authentication failed")
        return self._xor_bytes(wrapped, self._keystream(wrapping_material, nonce, len(wrapped)))

    def status(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": "local",
            "configured": True,
            "primary_key_id": self.primary_key_id,
            "keyring_size": len(self.wrapping_keys),
            "rotation_aware": bool(self.wrapping_keys),
            "wrap_supported": True,
            "unwrap_supported": True,
        }

    def _derive_wrapping_material(self, wrapping_key: str, key_id: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            wrapping_key.encode("utf-8"),
            f"{self.provider_id}:{key_id}".encode("utf-8"),
            120_000,
            dklen=32,
        )

    def _keystream(self, key: bytes, nonce: bytes, length: int) -> bytes:
        chunks: list[bytes] = []
        counter = 0
        produced = 0
        while produced < length:
            chunks.append(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
            produced += len(chunks[-1])
            counter += 1
        return b"".join(chunks)[:length]

    def _stable_payload(self, payload: dict[str, Any]) -> bytes:
        parts = [
            str(payload.get("algorithm", "")),
            str(payload.get("key_id", "")),
            str(payload.get("nonce", "")),
            str(payload.get("provider_id", "")),
            str(payload.get("version", "")),
            str(payload.get("wrapped", "")),
        ]
        return "\n".join(parts).encode("utf-8")

    def _b64(self, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _unb64(self, value: str) -> bytes:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))

    def _xor_bytes(self, left: bytes, right: bytes) -> bytes:
        return bytes(a ^ b for a, b in zip(left, right, strict=True))


def build_secret_kms_provider(
    *,
    provider: str,
    provider_id: str,
    key_id: str,
    key: str,
    previous_keys: dict[str, str] | None = None,
) -> SecretKMSProvider | None:
    provider_name = provider.strip().casefold()
    if provider_name in {"", "none", "disabled"}:
        return None
    if provider_name != "local":
        raise ValueError(f"unsupported secret KMS provider: {provider}")
    normalized_key_id = key_id.strip() or "primary"
    keys = {str(k): str(v) for k, v in (previous_keys or {}).items() if v}
    if key:
        keys[normalized_key_id] = key
    return LocalSecretKMSProvider(
        provider_id=provider_id.strip() or "local-kms",
        primary_key_id=normalized_key_id,
        wrapping_keys=keys,
    )
