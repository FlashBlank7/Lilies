from __future__ import annotations

import pytest

from agent_platform.forbidden_assistance_scanner import (
    SCANNER_VERSION,
    generic_policy_digest,
    registered_scanner_versions,
    scan_forbidden_assistance,
    scanner_process_digest,
)


def test_retained_t01f_scanner_has_golden_policy_and_process_identity() -> None:
    assert SCANNER_VERSION == "t01f-generic-1"
    assert registered_scanner_versions() == ("t01f-generic-1",)
    assert (
        generic_policy_digest(SCANNER_VERSION)
        == "sha256:c2444d6d7c415c1886b652962ea81c6e06444dbe0b02e9735a98f1fc0407c2e0"
    )
    assert (
        scanner_process_digest(SCANNER_VERSION)
        == "sha256:fa316227832fc084764b64ca02ec00a35b033be67bb7f5c95b78a71d732d793f"
    )


def test_scanner_dispatch_rejects_an_unretained_archive_version() -> None:
    with pytest.raises(
        ValueError,
        match="scanner is unavailable",
    ):
        scan_forbidden_assistance(
            scanner_version="t01f-generic-unretained",
        )
