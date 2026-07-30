from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent_platform.formal_source_provenance import (
    DeveloperSourceActivationFence,
    DeveloperSourceReloadConfirmation,
    _digest,
    _reload_confirmation_follows_activation,
)
from agent_platform.kernel_boot_identity import (
    DARWIN_BOOT_SESSION_SCHEME,
    LEGACY_DARWIN_BOOT_TIME_SCHEME,
    PROCESS_MONOTONIC_ORDER_BASIS,
    legacy_darwin_boot_digest,
)


LEGACY_BOOT_STARTED = datetime(
    2026,
    7,
    22,
    9,
    50,
    52,
    781_734,
    tzinfo=timezone.utc,
)
ACTIVATED_AT = datetime(
    2026,
    7,
    26,
    10,
    3,
    2,
    795_980,
    tzinfo=timezone.utc,
)
PROCESS_BOOT_STARTED = LEGACY_BOOT_STARTED.replace(microsecond=0)
PROCESS_LOADED_AT = datetime(
    2026,
    7,
    26,
    10,
    12,
    tzinfo=timezone.utc,
)
LEGACY_MONOTONIC_NS = 302_429_367_464_458
PROCESS_MONOTONIC_NS = 303_407_307_311_208
STABLE_BOOT_DIGEST = (
    "sha256:1e0043ecd04e0d7506007dfb70aeef5d8b2c9ed8132d80234dd36a7c162dd0e9"
)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _legacy_fence() -> DeveloperSourceActivationFence:
    legacy_digest = legacy_darwin_boot_digest(LEGACY_BOOT_STARTED)
    assert legacy_digest is not None
    payload = {
        "schema_version": "1.0",
        "assignment_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_revision": 5,
        "response_id": str(uuid4()),
        "receipt_digest": "sha256:" + "1" * 64,
        "intent_digest": "sha256:" + "2" * 64,
        "branch_ref": "refs/heads/usabilityEnhence",
        "commit_sha": "3" * 40,
        "tree_sha": "4" * 40,
        "activation_process_instance_id": str(uuid4()),
        "activation_boot_id": legacy_digest,
        "activation_boot_started_at": _iso(LEGACY_BOOT_STARTED),
        "activation_monotonic_ns": LEGACY_MONOTONIC_NS,
        "activated_at": _iso(ACTIVATED_AT),
    }
    return DeveloperSourceActivationFence(
        **payload,
        fence_digest=_digest(payload),
    )


def _confirmation_payload(
    fence: DeveloperSourceActivationFence,
) -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "assignment_id": str(fence.assignment_id),
        "channel_id": str(fence.channel_id),
        "report_id": str(fence.report_id),
        "report_revision": fence.report_revision,
        "response_id": str(fence.response_id),
        "receipt_digest": fence.receipt_digest,
        "intent_digest": fence.intent_digest,
        "branch_ref": fence.branch_ref,
        "hidden_ref": (
            f"refs/lilies/formal/{fence.assignment_id}/{fence.response_id}"
        ),
        "commit_sha": fence.commit_sha,
        "tree_sha": fence.tree_sha,
        "changed_paths": [
            "platform/backend/src/agent_platform/openapi_connector.py"
        ],
        "status": "confirmed",
        "activation_process_instance_id": str(
            fence.activation_process_instance_id
        ),
        "confirming_process_instance_id": str(uuid4()),
        "activation_fence_digest": fence.fence_digest,
        "activation_boot_id": fence.activation_boot_id,
        "activation_boot_scheme": LEGACY_DARWIN_BOOT_TIME_SCHEME,
        "activation_boot_started_at": _iso(
            fence.activation_boot_started_at
        ),
        "activation_monotonic_ns": fence.activation_monotonic_ns,
        "process_generation_boot_id": STABLE_BOOT_DIGEST,
        "process_generation_boot_scheme": DARWIN_BOOT_SESSION_SCHEME,
        "process_generation_boot_started_at": _iso(
            PROCESS_BOOT_STARTED
        ),
        "process_generation_monotonic_ns": PROCESS_MONOTONIC_NS,
        "process_generation_loaded_at": _iso(PROCESS_LOADED_AT),
        "generation_identity_match": "legacy-darwin-boottime",
        "generation_order_basis": PROCESS_MONOTONIC_ORDER_BASIS,
        "confirmed_at": _iso(
            PROCESS_LOADED_AT + timedelta(seconds=1)
        ),
    }


def test_real_darwin_microsecond_drift_uses_narrow_legacy_bridge() -> None:
    fence = _legacy_fence()
    payload = _confirmation_payload(fence)
    confirmation = DeveloperSourceReloadConfirmation(
        **payload,
        confirmation_digest=_digest(payload),
    )

    assert _reload_confirmation_follows_activation(
        fence=fence,
        confirmation=confirmation,
    )


def test_legacy_bridge_relation_tampering_is_rejected_after_redigest() -> None:
    fence = _legacy_fence()
    payload = _confirmation_payload(fence)
    payload["generation_identity_match"] = "later-stable-kernel-boot"
    confirmation = DeveloperSourceReloadConfirmation(
        **payload,
        confirmation_digest=_digest(payload),
    )

    assert not _reload_confirmation_follows_activation(
        fence=fence,
        confirmation=confirmation,
    )


def test_legacy_fence_serialization_remains_byte_compatible() -> None:
    fence = _legacy_fence()
    serialized = fence.model_dump(mode="json", exclude_none=True)

    assert fence.schema_version == "1.0"
    assert "activation_boot_scheme" not in serialized
    without_digest = dict(serialized)
    without_digest.pop("fence_digest")
    assert fence.fence_digest == _digest(without_digest)
