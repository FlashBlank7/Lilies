from __future__ import annotations

import asyncio
import hmac
import inspect
import re
import secrets
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import SecretStr

from .capability_generality_gate import CapabilityGeneralityViolation
from .collaboration_models import (
    ChannelStatus,
    CollaborationChannel,
    CollaborationReport,
    DeveloperLease,
    DeveloperSourcePromotionRequest,
)
from .collaboration_service import CollaborationService, IssuedCollaborationChannel
from .formal_assignment_broker import (
    FormalAssignmentBroker,
    FormalDeveloperWorkspace,
    PrepareFormalAssignmentRequest,
    PreparedFormalAssignment,
)
from .formal_source_provenance import FormalSourceProvenanceCoordinator
from .lilies_models import (
    AssignmentMode,
    BuildAssignment,
    CollaborationAccess,
    CollaborationScope,
    PlatformAccess,
    PlatformScope,
)
from .platform_blackbox_auth import PlatformBlackboxScope
from .platform_harness import PlatformHarness, PlatformHarnessViolation
from .task_packages import AllowedActionsPolicy


ContractDigestProvider = Callable[
    [
        tuple[PlatformBlackboxScope, ...],
        tuple[UUID, ...],
        AllowedActionsPolicy,
    ],
    Awaitable[str] | str,
]
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class FormalAssignmentRuntimeError(RuntimeError):
    """A trusted production provider could not prepare formal authority."""


class PlatformFormalAssignmentRuntime(FormalAssignmentBroker):
    """Async production boundary around the sealed, synchronous broker.

    Package validation and filesystem materialization stay in the broker's
    worker thread. Contract rendering and PlatformHarness secret resolution
    are scheduled back onto the platform event loop so their loop-bound stores
    are never accessed from a foreign loop.
    """

    def __init__(
        self,
        *,
        task_state_root: Path,
        broker_state_root: Path,
        public_workspace_root: Path,
        platform_base_url: str,
        contract_digest_provider: ContractDigestProvider,
        harness: PlatformHarness,
        collaboration: CollaborationService,
        developer_source_root: Path | None = None,
        developer_workspace_root: Path | None = None,
        source_provenance: FormalSourceProvenanceCoordinator | None = None,
        supplemental_public_materials: Mapping[str, Path] | None = None,
        environment_secret_owner: str = "formal-environment",
        provider_timeout_seconds: float = 30.0,
    ) -> None:
        normalized_base_url = platform_base_url.rstrip("/")
        if not normalized_base_url:
            raise ValueError("formal platform base URL cannot be empty")
        if provider_timeout_seconds <= 0:
            raise ValueError("formal provider timeout must be positive")
        self._platform_base_url = normalized_base_url
        self._contract_digest_provider = contract_digest_provider
        self._harness = harness
        self._collaboration = collaboration
        self._source_provenance = source_provenance
        if not environment_secret_owner or "/" in environment_secret_owner:
            raise ValueError("formal environment secret owner is invalid")
        self._environment_secret_owner = environment_secret_owner
        self._provider_timeout_seconds = provider_timeout_seconds
        self._platform_loop: asyncio.AbstractEventLoop | None = None
        self._loop_guard = threading.Lock()
        self._credential_locks: dict[UUID, asyncio.Lock] = {}
        super().__init__(
            task_state_root=task_state_root,
            broker_state_root=broker_state_root,
            public_workspace_root=public_workspace_root,
            platform_access_provider=self._platform_access,
            collaboration_access_provider=self._collaboration_access,
            environment_secret_resolver=self._environment_secret,
            developer_source_root=developer_source_root,
            developer_workspace_root=developer_workspace_root,
            developer_projection_provider=(
                source_provenance.freeze_workspace_projection
                if source_provenance is not None
                else None
            ),
            supplemental_public_materials=supplemental_public_materials,
        )

    async def prepare_async(
        self,
        request: PrepareFormalAssignmentRequest,
    ) -> PreparedFormalAssignment:
        loop = asyncio.get_running_loop()
        with self._loop_guard:
            self._platform_loop = loop
        prepared = await asyncio.to_thread(super().prepare, request)
        return PreparedFormalAssignment.model_validate(prepared)

    async def developer_workspace_for_channel(
        self,
        channel: CollaborationChannel,
    ) -> FormalDeveloperWorkspace:
        """Resolve the exact private source snapshot behind a developer lease."""

        resolved = await asyncio.to_thread(
            self.resolve_developer_workspace,
            assignment_id=channel.assignment_id,
            session_id=channel.lilies_session_id,
        )
        if (
            resolved.task_id != channel.task_id
            or resolved.task_revision != channel.task_revision
            or resolved.assignment_id != channel.assignment_id
        ):
            raise FormalAssignmentRuntimeError(
                "formal developer workspace differs from its collaboration channel"
            )
        return resolved

    async def promote_developer_workspace(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        lease: DeveloperLease,
        request: DeveloperSourcePromotionRequest,
    ) -> Any:
        """Create and activate the exact delta from the lease-bound workspace."""

        coordinator = self._source_provenance
        if coordinator is None:
            raise FormalAssignmentRuntimeError(
                "formal developer source promotion is unavailable"
            )
        workspace = await self.developer_workspace_for_channel(channel)
        source_manifest_digest = workspace.source_manifest_digest
        if (
            source_manifest_digest is None
            or not hmac.compare_digest(
                workspace.workspace.manifest_digest,
                request.workspace_manifest_digest,
            )
            or not hmac.compare_digest(
                source_manifest_digest,
                request.source_manifest_digest,
            )
            or report.channel_id != channel.channel_id
            or lease.report_id != report.report_id
        ):
            raise FormalAssignmentRuntimeError(
                "developer promotion request differs from its private workspace lease"
            )
        try:
            receipt = await asyncio.to_thread(
                coordinator.promote_workspace_delta,
                assignment_id=channel.assignment_id,
                channel_id=channel.channel_id,
                report_id=report.report_id,
                report_revision=report.revision,
                lease_id=lease.lease_id,
                lease_owner_id=lease.owner_id,
                response_id=request.response_id,
                idempotency_key=request.idempotency_key,
                workspace=Path(workspace.workspace.path),
                workspace_manifest_digest=request.workspace_manifest_digest,
                source_manifest_digest=request.source_manifest_digest,
                created_at=datetime.now(timezone.utc),
            )
            effective = await asyncio.to_thread(
                coordinator.promoted_response_is_effective,
                assignment_id=channel.assignment_id,
                channel_id=channel.channel_id,
                report_id=report.report_id,
                report_revision=report.revision,
                response_id=request.response_id,
                commit_sha=receipt.commit_sha,
            )
            result = receipt.model_dump(mode="json")
            result["effective"] = effective
            result["reload_confirmed"] = (
                receipt.reload_status != "not_required" and effective
            )
            if result["reload_confirmed"]:
                result["reload_status"] = "confirmed"
            return result
        except CapabilityGeneralityViolation:
            raise
        except Exception as error:
            raise FormalAssignmentRuntimeError(
                "formal developer workspace delta could not be promoted"
            ) from error

    def _on_platform_loop(self, value: Awaitable[Any] | Any) -> Any:
        if not inspect.isawaitable(value):
            return value
        with self._loop_guard:
            loop = self._platform_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            if inspect.iscoroutine(value):
                value.close()
            raise FormalAssignmentRuntimeError("formal provider has no active platform event loop")
        future = asyncio.run_coroutine_threadsafe(value, loop)
        try:
            return future.result(timeout=self._provider_timeout_seconds)
        except Exception as error:
            future.cancel()
            raise FormalAssignmentRuntimeError(
                "formal platform provider did not complete"
            ) from error

    def _platform_access(
        self,
        request: PrepareFormalAssignmentRequest,
        required_scopes: tuple[PlatformScope, ...],
        allowed_actions: AllowedActionsPolicy,
    ) -> PlatformAccess:
        try:
            blackbox_scopes = tuple(PlatformBlackboxScope(scope.value) for scope in required_scopes)
        except ValueError as error:
            raise FormalAssignmentRuntimeError(
                "formal package requested an unpublished platform scope"
            ) from error
        contract_digest = self._on_platform_loop(
            self._contract_digest_provider(
                blackbox_scopes,
                (request.application_id,),
                allowed_actions,
            )
        )
        if not isinstance(contract_digest, str) or not _DIGEST_PATTERN.fullmatch(contract_digest):
            raise FormalAssignmentRuntimeError(
                "formal contract provider returned an invalid digest"
            )
        credential_id = uuid5(
            NAMESPACE_URL,
            f"lilies:platform-task-credential:{request.assignment_id}",
        )
        return PlatformAccess(
            base_url=self._platform_base_url,
            contract_url="/api/v1/lilies/platform-contract",
            contract_digest=contract_digest,
            credential_ref=f"platform-task-credential:{credential_id}",
            scopes=list(required_scopes),
            application_ids=[request.application_id],
        )

    @staticmethod
    def _channel_id(request: PrepareFormalAssignmentRequest) -> UUID:
        return PlatformFormalAssignmentRuntime._channel_id_for(
            task_id=request.task_id,
            revision=request.revision,
            assignment_id=request.assignment_id,
        )

    @staticmethod
    def _channel_id_for(
        *,
        task_id: str,
        revision: int,
        assignment_id: UUID,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"lilies:collaboration:{task_id}:{revision}:{assignment_id}",
        )

    @staticmethod
    def _channel_idempotency_key(assignment_id: UUID) -> str:
        return f"formal.channel.activate.{assignment_id.hex}"

    @classmethod
    def _collaboration_credential_ref(
        cls,
        *,
        channel_id: UUID,
        assignment_id: UUID,
    ) -> str:
        credential_id = uuid5(
            NAMESPACE_URL,
            "lilies:collaboration-credential:"
            f"{channel_id}:{cls._channel_idempotency_key(assignment_id)}",
        )
        return f"collaboration_{credential_id.hex}"

    @classmethod
    def _collaboration_access(
        cls,
        request: PrepareFormalAssignmentRequest,
        expires_at: Any,
    ) -> CollaborationAccess:
        channel_id = cls._channel_id(request)
        return CollaborationAccess(
            channel_id=channel_id,
            credential_ref=cls._collaboration_credential_ref(
                channel_id=channel_id,
                assignment_id=request.assignment_id,
            ),
            scopes=list(CollaborationScope),
            expires_at=expires_at,
        )

    def _split_secret_reference(self, secret_ref: str) -> tuple[str, str]:
        prefix = "secret:"
        name = secret_ref.removeprefix(prefix)
        if (
            not secret_ref.startswith(prefix)
            or secret_ref.startswith("secret://")
            or not name
            or "/" in name
        ):
            raise FormalAssignmentRuntimeError(
                "formal environment attestation requires a PlatformHarness secret ref"
            )
        return self._environment_secret_owner, name

    async def _resolve_harness_secret_ref(
        self,
        *,
        owner_id: str,
        name: str,
    ) -> str:
        resolved = await self._harness.inject_secret_references(
            owner_id=owner_id,
            payload={"$secret": f"secret://{owner_id}/{name}"},
        )
        if not isinstance(resolved, str) or not resolved:
            raise PlatformHarnessViolation(
                "formal PlatformHarness secret did not resolve to a string"
            )
        return resolved

    def _environment_secret(self, secret_ref: str) -> bytes:
        owner_id, name = self._split_secret_reference(secret_ref)
        resolved = self._on_platform_loop(
            self._resolve_harness_secret_ref(
                owner_id=owner_id,
                name=name,
            )
        )
        if not isinstance(resolved, str):
            raise FormalAssignmentRuntimeError(
                "formal environment attestation secret has an invalid type"
            )
        return resolved.encode("utf-8")

    @staticmethod
    def _secret_owner(assignment_id: UUID) -> str:
        return f"local-lilies-assignment:{assignment_id}"

    async def _durable_collaboration_bearer(
        self,
        assignment_id: UUID,
    ) -> str:
        owner_id = self._secret_owner(assignment_id)
        name = "formal-collaboration-token"
        existing = {
            str(item.get("name")): item
            for item in await self._harness.list_secrets(owner_id=owner_id)
        }
        if name in existing:
            return await self._resolve_harness_secret_ref(
                owner_id=owner_id,
                name=name,
            )
        bearer = f"lcc_{assignment_id.hex}_{secrets.token_urlsafe(48)}"
        saved = await self._harness.save_secret(
            owner_id=owner_id,
            name=name,
            value=bearer,
            description=("Crash-safe formal collaboration bearer for Local Lilies"),
        )
        if saved.get("encrypted") is not True:
            await self._harness.delete_secret(owner_id=owner_id, name=name)
            raise FormalAssignmentRuntimeError(
                "formal collaboration bearer requires encrypted PlatformHarness storage"
            )
        return bearer

    async def collaboration_credential_secret(
        self,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> SecretStr:
        if (
            assignment.mode is not AssignmentMode.formal_experiment
            or assignment.task_package is None
            or assignment.collaboration is None
        ):
            raise FormalAssignmentRuntimeError(
                "formal collaboration provider requires a complete formal assignment"
            )
        try:
            prepared = await asyncio.to_thread(
                self.resolve_prepared_assignment,
                assignment_id=assignment.assignment_id,
                session_id=session_id,
            )
        except Exception as error:
            raise FormalAssignmentRuntimeError(
                "formal collaboration session is not broker-authorized"
            ) from error
        if prepared.assignment != assignment:
            raise FormalAssignmentRuntimeError(
                "formal collaboration assignment differs from the broker receipt"
            )
        access = assignment.collaboration
        expected_channel_id = self._channel_id_for(
            task_id=assignment.task_package.task_id,
            revision=assignment.task_package.revision,
            assignment_id=assignment.assignment_id,
        )
        expected_ref = self._collaboration_credential_ref(
            channel_id=expected_channel_id,
            assignment_id=assignment.assignment_id,
        )
        if (
            assignment.target.application_id is None
            or access.channel_id != expected_channel_id
            or access.credential_ref != expected_ref
            or access.scopes != list(CollaborationScope)
            or access.expires_at != assignment.constraints.deadline_at
            or assignment.constraints.max_report_evidence_rounds is None
        ):
            raise FormalAssignmentRuntimeError(
                "formal collaboration authority changed its frozen binding"
            )
        lock = self._credential_locks.setdefault(
            assignment.assignment_id,
            asyncio.Lock(),
        )
        async with lock:
            bearer = await self._durable_collaboration_bearer(assignment.assignment_id)
            try:
                issued = await self._collaboration.create_formal_channel(
                    assignment_mode=assignment.mode,
                    task_id=assignment.task_package.task_id,
                    task_revision=assignment.task_package.revision,
                    assignment_id=assignment.assignment_id,
                    lilies_session_id=session_id,
                    application_ids=assignment.platform.application_ids,
                    collaboration_enabled=True,
                    user_notified=True,
                    expires_at=access.expires_at,
                    retention_until=access.expires_at + timedelta(days=30),
                    idempotency_key=self._channel_idempotency_key(assignment.assignment_id),
                    max_report_evidence_rounds=(
                        assignment.constraints.max_report_evidence_rounds
                    ),
                    prepared_access_token=SecretStr(bearer),
                )
            except Exception as error:
                try:
                    await self.close_collaboration_authority(
                        assignment,
                        session_id,
                    )
                except Exception:
                    pass
                raise FormalAssignmentRuntimeError(
                    "formal collaboration activation failed closed"
                ) from error
        self._validate_issued_channel(
            issued,
            assignment=assignment,
            session_id=session_id,
            expected_channel_id=expected_channel_id,
            expected_ref=expected_ref,
            bearer=bearer,
        )
        return SecretStr(bearer)

    async def close_collaboration_authority(
        self,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> CollaborationChannel:
        """Close exact formal authority before deleting its durable bearer."""

        if (
            assignment.mode is not AssignmentMode.formal_experiment
            or assignment.task_package is None
            or assignment.collaboration is None
        ):
            raise FormalAssignmentRuntimeError(
                "formal collaboration close requires a complete formal assignment"
            )
        try:
            prepared = await asyncio.to_thread(
                self.resolve_prepared_assignment,
                assignment_id=assignment.assignment_id,
                session_id=session_id,
            )
        except Exception as error:
            raise FormalAssignmentRuntimeError(
                "formal collaboration close is not broker-authorized"
            ) from error
        if prepared.assignment != assignment:
            raise FormalAssignmentRuntimeError(
                "formal collaboration close differs from the broker receipt"
            )
        task_ref = assignment.task_package
        access = assignment.collaboration
        expected_channel_id = self._channel_id_for(
            task_id=task_ref.task_id,
            revision=task_ref.revision,
            assignment_id=assignment.assignment_id,
        )
        if (
            assignment.target.application_id is None
            or assignment.platform.application_ids != [assignment.target.application_id]
            or access.channel_id != expected_channel_id
        ):
            raise FormalAssignmentRuntimeError(
                "formal collaboration close changed its frozen binding"
            )
        try:
            closed = CollaborationChannel.model_validate(
                await self._collaboration.close_formal_assignment_channel(
                    assignment_mode=assignment.mode,
                    task_id=task_ref.task_id,
                    task_revision=task_ref.revision,
                    assignment_id=assignment.assignment_id,
                    lilies_session_id=session_id,
                    application_ids=assignment.platform.application_ids,
                )
            )
        except Exception as error:
            raise FormalAssignmentRuntimeError(
                "formal collaboration authority did not close"
            ) from error
        if (
            closed.channel_id != expected_channel_id
            or closed.assignment_id != assignment.assignment_id
            or closed.lilies_session_id != session_id
            or closed.application_ids != assignment.platform.application_ids
            or closed.status is not ChannelStatus.closed
            or closed.closed_at is None
        ):
            raise FormalAssignmentRuntimeError(
                "formal collaboration close receipt changed its frozen binding"
            )
        await self._harness.delete_secret(
            owner_id=self._secret_owner(assignment.assignment_id),
            name="formal-collaboration-token",
        )
        return closed

    @staticmethod
    def _validate_issued_channel(
        issued: IssuedCollaborationChannel,
        *,
        assignment: BuildAssignment,
        session_id: UUID,
        expected_channel_id: UUID,
        expected_ref: str,
        bearer: str,
    ) -> None:
        channel = CollaborationChannel.model_validate(issued.channel)
        if (
            channel.channel_id != expected_channel_id
            or channel.assignment_id != assignment.assignment_id
            or channel.lilies_session_id != session_id
            or channel.task_id != assignment.task_package.task_id
            or channel.task_revision != assignment.task_package.revision
            or channel.application_ids != assignment.platform.application_ids
            or (
                channel.max_report_evidence_rounds
                != assignment.constraints.max_report_evidence_rounds
            )
            or issued.credential_ref != expected_ref
            or not hmac.compare_digest(
                issued.access_token.get_secret_value(),
                bearer,
            )
        ):
            raise FormalAssignmentRuntimeError(
                "formal collaboration activation returned another authority"
            )
