#!/usr/bin/env python3
"""Regenerate only mechanical source/artifact manifests in oracle-lock.json."""

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HOST_ROOTS = [
    "README.md",
    "config",
    "numeric-reference",
    "scripts",
    "t01m-host-oracle",
    "t01m_host",
    "tests",
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def manifest(paths: list[Path]) -> tuple[list[dict[str, str]], str]:
    entries = [
        {"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)}
        for path in sorted(paths, key=lambda item: item.as_posix())
    ]
    return entries, hashlib.sha256(canonical(entries)).hexdigest()


def host_paths() -> list[Path]:
    output = []
    for text in HOST_ROOTS:
        root = ROOT / text
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if path.is_symlink():
                raise RuntimeError(f"locked host path is a symlink: {path}")
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            ):
                output.append(path)
    return output


def source_paths() -> list[Path]:
    paths = [ROOT / "AndroidManifest.xml"]
    paths.extend(path for path in (ROOT / "src").rglob("*") if path.is_file())
    if any(path.is_symlink() for path in paths):
        raise RuntimeError("driver source manifest cannot contain a symlink")
    return paths


def atomic_write(path: Path, raw: bytes) -> None:
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--built-at", required=True)
    parser.add_argument("--certificate-fingerprint-sha256", required=True)
    args = parser.parse_args()
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})",
        args.built_at,
    ):
        raise RuntimeError("--built-at must be a second-precision ISO-8601 timestamp")
    fingerprint = args.certificate_fingerprint_sha256.replace(":", "").upper()
    if not re.fullmatch(r"[0-9A-F]{64}", fingerprint):
        raise RuntimeError("certificate fingerprint must be SHA-256 hex")
    key_text = os.environ.get("T01M_SIGNING_KEY_PATH")
    cert_text = os.environ.get("T01M_SIGNING_CERT_PATH")
    if not key_text or not cert_text:
        raise RuntimeError("external signing key/certificate environment is required")
    key = Path(key_text).resolve()
    cert = Path(cert_text).resolve()
    if (
        key.is_relative_to(ROOT)
        or cert.is_relative_to(ROOT)
        or not key.is_file()
        or not cert.is_file()
    ):
        raise RuntimeError("signing material must be regular external files")

    lock_path = ROOT / "oracle-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    source_entries, source_digest = manifest(source_paths())
    host_entries, host_digest = manifest(host_paths())
    apk = ROOT / "dist/t01m-external-oracle.apk"
    with zipfile.ZipFile(apk) as archive:
        if archive.namelist().count("classes.dex") != 1:
            raise RuntimeError("driver APK must contain exactly one classes.dex")
        dex_digest = hashlib.sha256(archive.read("classes.dex")).hexdigest()

    from sys import path as module_path

    module_path.insert(0, str(ROOT))
    from t01m_host.config import load_accessibility_contract, load_flow
    from t01m_host.event_binding import load_reduced_motion_contract

    flow = load_flow()
    accessibility = load_accessibility_contract()
    reduced = load_reduced_motion_contract()
    bindings = lock["frozen_contract_bindings"]
    bindings["project_brief_path_identity"] = "T01M_PROJECT_BRIEF_PATH"
    bindings["acceptance_oracle_path_identity"] = "T01M_ACCEPTANCE_ORACLE_PATH"
    bindings.pop("project_brief_path", None)
    bindings.pop("acceptance_oracle_path", None)
    lock["built_at"] = args.built_at
    lock["status"] = (
        "frozen_source_binary_host_numeric_reference_contract_and_rebuild_bindings"
    )
    lock["schema_version"] = 3
    lock["driver_id"] = "T01M-EXTERNAL-ANDROID-ORACLE-DRIVER-v4"
    lock["host_controller_id"] = "T01M-FROZEN-ANDROID-HOST-CONTROLLER-v2"
    lock["provenance"].pop("uncertain_concurrent_draft", None)
    lock["provenance"]["application_repository_path_identity"] = (
        "explicit --repository argument; never discovered"
    )
    lock["provenance"].pop("application_repository", None)
    lock["provenance"]["driver_v4_change_scope"] = (
        "complete A01-A10 fail-closed host orchestration, full Unicode and "
        "collection checks, accessibility and motion capture, and "
        "environment-identified reproducible driver build; no application "
        "source or implementation semantics"
    )
    lock["source"].update(
        {
            "manifest_sha256": digest(ROOT / "AndroidManifest.xml"),
            "readme_sha256": digest(ROOT / "README.md"),
            "java_sha256": digest(
                ROOT
                / "src/dev/lilies/t01m/oracle/T01MOracleInstrumentation.java"
            ),
            "canonical_manifest_sha256": source_digest,
            "recursive_path_sha256": source_entries,
        }
    )
    lock["host_controller"].update(
        {
            "manifest_roots": HOST_ROOTS,
            "canonical_manifest_sha256": host_digest,
            "recursive_path_sha256": host_entries,
            "flow_step_count": len(flow["steps"]),
            "accessibility_screen_count": len(accessibility["screens"]),
            "accessibility_font_scales": accessibility["font_scales"],
            "reduced_motion_transition_count": len(
                reduced["transition_targets"]
            ),
        }
    )
    lock["host_controller"].pop("a08_screen_count", None)
    lock["host_controller"].pop("a08_font_scales", None)
    lock["signing"].update(
        {
            "private_key_sha256": digest(key),
            "certificate_sha256": digest(cert),
            "certificate_fingerprint_sha256": fingerprint,
            "private_key_storage": "external; T01M_SIGNING_KEY_PATH",
            "certificate_storage": "external; T01M_SIGNING_CERT_PATH",
        }
    )
    lock["artifact"].update(
        {
            "bytes": apk.stat().st_size,
            "sha256": digest(apk),
            "source_to_dex_binding": {
                "source_manifest_sha256": source_digest,
                "classes_dex_sha256": dex_digest,
                "apk_sha256": digest(apk),
            },
        }
    )
    lock["artifact"]["reproducible_rebuild"] = {
        "independent_build_count": 2,
        "byte_identical": True,
        "script_identity": "scripts/rebuild_driver.py",
    }
    lock["security_boundary"].update(
        {
            "target_private_files_read": True,
            "target_private_read_scope": (
                "A07 host-only run-as path/size/SHA-256 inventory; raw bytes "
                "are transiently hashed and never persisted in evidence"
            ),
            "target_private_file_contents_persisted": False,
        }
    )
    lock["remaining_claim_ceiling"] = (
        "Oracle source/APK, complete frozen host routes, locked JBR numeric "
        "reference, deterministic dual rebuild, static and fixture tests are "
        "verified. No target repository or target APK was accessed or executed "
        "during this repair, so target-dependent A01-A10 runtime verdicts remain "
        "unclaimed until their explicit commands run against supplied evidence."
    )
    atomic_write(lock_path, canonical(lock))
    print(
        json.dumps(
            {
                "oracle_lock_sha256": digest(lock_path),
                "source_manifest_sha256": source_digest,
                "host_manifest_sha256": host_digest,
                "apk_sha256": digest(apk),
                "result": "pass",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
