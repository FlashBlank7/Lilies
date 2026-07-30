import hashlib
import json
import os
import re
import unicodedata
import tempfile
from pathlib import Path
from typing import Any


class OracleError(RuntimeError):
    """A deterministic oracle failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def write_new_or_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


_SANITIZERS = [
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"https?://[^\s\"']+"), "<url>"),
    (re.compile(r"(?i)(token|secret|password|authorization)=\S+"), r"\1=<redacted>"),
]


def sanitize_logcat(raw: str, serial: str) -> str:
    text = raw.replace(serial, "<device-serial>") if serial else raw
    for pattern, replacement in _SANITIZERS:
        text = pattern.sub(replacement, text)
    return text


def normalized_relative_path(root: Path, path: Path) -> str:
    if path.is_symlink():
        raise OracleError(f"symbolic links are forbidden in evidence: {path}")
    relative = path.relative_to(root).as_posix()
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
        or unicodedata.normalize("NFC", relative) != relative
    ):
        raise OracleError(f"non-normalized evidence path: {relative!r}")
    return relative


def require_hex_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise OracleError(f"{label} must be a lowercase SHA-256")
    return value
