"""Role-separated service for reusable Lilies/Codex software development."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from .collaborative_development_authority import (
    AuthorityDecision,
    CollaborativeDevelopmentAuthorityStore,
    DevelopmentAuthorityDecisionConflict,
    DevelopmentAuthorityDecisionError,
    DevelopmentAuthorityRequestNotFound,
    DevelopmentAuthorizationRequestView,
)
from .collaborative_development_auth import DevelopmentPrincipal
from .collaborative_development_models import (
    AgentRole,
    ApprovalMode,
    AssignmentStatus,
    DevelopmentAssignment,
    DevelopmentAssignmentProjection,
    DevelopmentBudget,
    DevelopmentLease,
    DevelopmentResult,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    DispatchOutboxItem,
    ExecutionMode,
    LiliesReview,
    ReaderCursor,
    WorkItemStatus,
    WorkspaceGrant,
    utc_now,
)
from .collaborative_development_dispatcher import canonical_digest
from .collaborative_development_storage import (
    CollaborativeDevelopmentAuthorizationError,
    CollaborativeDevelopmentBudgetExceeded,
    CollaborativeDevelopmentConflict as StoreConflict,
    CollaborativeDevelopmentInvalidState,
    CollaborativeDevelopmentNotFound as StoreNotFound,
    CollaborativeDevelopmentStorageError,
    CollaborativeDevelopmentStore,
)
from .development_workspace_broker import (
    DevelopmentReviewSnapshotReceipt,
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceError,
    PreparedDevelopmentWorkspaces,
)


class CollaborativeDevelopmentError(RuntimeError):
    code = "collaborative_development_error"
    status_code = 500

    def public_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
        }


class CollaborativeDevelopmentDisabled(CollaborativeDevelopmentError):
    code = "collaborative_development_not_found"
    status_code = 404


class AutonomousCollaborationDisabled(CollaborativeDevelopmentError):
    code = "autonomous_collaboration_disabled"
    status_code = 409


class CollaborativeDevelopmentUnauthorized(CollaborativeDevelopmentError):
    code = "collaborative_development_not_found"
    status_code = 404


class CollaborativeDevelopmentNotFound(CollaborativeDevelopmentError):
    code = "collaborative_development_not_found"
    status_code = 404


class CollaborativeDevelopmentConflict(CollaborativeDevelopmentError):
    code = "collaborative_development_conflict"
    status_code = 409


class CollaborativeDevelopmentLimitExceeded(CollaborativeDevelopmentError):
    code = "collaborative_development_limit_exceeded"
    status_code = 422


class CollaborativeDevelopmentService:
    """Enforce assignment role boundaries above the transactional store."""

    def __init__(
        self,
        *,
        store: CollaborativeDevelopmentStore,
        enabled: bool,
        autonomous_enabled: bool = False,
        authority_store: CollaborativeDevelopmentAuthorityStore | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.autonomous_enabled = autonomous_enabled
        self.authority_store = authority_store or CollaborativeDevelopmentAuthorityStore(
            store.database_path.with_name(
                f"{store.database_path.stem}-dispatch{store.database_path.suffix}"
            )
        )

    async def initialize(self) -> None:
        await self.store.initialize()
        await self.authority_store.initialize()

    def require_enabled(self) -> None:
        if not self.enabled:
            raise CollaborativeDevelopmentDisabled("resource was not found")

    def require_autonomous_enabled(self) -> None:
        self.require_enabled()
        if not self.autonomous_enabled:
            raise AutonomousCollaborationDisabled(
                "autonomous collaboration is disabled; use manual_dispatch"
            )

    async def validate_principal(self, principal: DevelopmentPrincipal) -> None:
        """Revoke deterministic role credentials at the assignment boundary."""

        self.require_enabled()
        role = principal.agent_role
        if role is None:
            return
        if principal.assignment_id is None:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        try:
            assignment = await self.store.get_assignment(principal.assignment_id)
        except Exception as error:
            raise self._translate(error) from error
        if (
            assignment.status != AssignmentStatus.active
            or utc_now() >= assignment.deadline
            or role not in {grant.agent_role for grant in assignment.agent_roles}
        ):
            raise CollaborativeDevelopmentUnauthorized("resource was not found")

    @staticmethod
    def _require_user(principal: DevelopmentPrincipal) -> None:
        if principal.actor_role != "user" or principal.assignment_id is not None:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")

    @staticmethod
    def _require_assignment_binding(
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> None:
        if principal.assignment_id is not None and principal.assignment_id != assignment_id:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")

    @staticmethod
    def _task_roles(
        assignment: DevelopmentAssignment,
        role: AgentRole,
    ) -> set[DevelopmentTaskRole]:
        for grant in assignment.agent_roles:
            if grant.agent_role == role:
                return set(grant.task_roles)
        return set()

    @staticmethod
    def _workspace_grant(
        assignment: DevelopmentAssignment,
        role: AgentRole,
    ) -> WorkspaceGrant:
        for grant in assignment.workspace_grants:
            if grant.agent_role == role:
                return grant
        raise CollaborativeDevelopmentUnauthorized("resource was not found")

    @staticmethod
    def _authority_values_match(
        current: tuple[Any, ...],
        requested: tuple[Any, ...],
        replacement: tuple[Any, ...],
    ) -> bool:
        expected = set(current)
        expected.update(requested)
        return len(replacement) == len(expected) and set(replacement) == expected

    @classmethod
    def _validate_requested_grant_revision(
        cls,
        *,
        assignment: DevelopmentAssignment,
        request: DevelopmentAuthorizationRequestView,
        replacement_grant: WorkspaceGrant,
        replacement_budget: DevelopmentBudget | None,
        expected_assignment_revision: int,
    ) -> WorkspaceGrant:
        if request.status != "pending":
            raise CollaborativeDevelopmentConflict(
                "authority request already has a durable decision"
            )
        if assignment.revision != expected_assignment_revision:
            raise CollaborativeDevelopmentConflict("assignment revision compare-and-set failed")
        current = cls._workspace_grant(assignment, request.destination_role)
        if canonical_digest(current) != request.existing_grant_digest:
            raise CollaborativeDevelopmentConflict(
                "authority request no longer matches the frozen grant"
            )
        stable_identity = (
            replacement_grant.workspace_id == current.workspace_id,
            replacement_grant.agent_role == current.agent_role,
            replacement_grant.workspace_root == current.workspace_root,
            replacement_grant.baseline_commit == current.baseline_commit,
            replacement_grant.created_at == current.created_at,
        )
        if not all(stable_identity):
            raise CollaborativeDevelopmentConflict(
                "grant revision cannot change workspace identity or baseline"
            )
        if replacement_grant.grant_revision != current.grant_revision + 1:
            raise CollaborativeDevelopmentConflict(
                "replacement grant revision must advance exactly once"
            )
        requested = request.requested_authority
        dimensions_match = (
            cls._authority_values_match(
                current.allowed_paths,
                requested.paths,
                replacement_grant.allowed_paths,
            ),
            cls._authority_values_match(
                current.allowed_argv,
                requested.argv,
                replacement_grant.allowed_argv,
            ),
            cls._authority_values_match(
                current.allowed_hosts,
                requested.hosts,
                replacement_grant.allowed_hosts,
            ),
            cls._authority_values_match(
                current.allowed_side_effects,
                requested.side_effects,
                replacement_grant.allowed_side_effects,
            ),
            cls._authority_values_match(
                current.secret_refs,
                requested.secret_refs,
                replacement_grant.secret_refs,
            ),
        )
        if not all(dimensions_match):
            raise CollaborativeDevelopmentConflict(
                "replacement grant must add exactly the approved requested authority"
            )
        if requested.budget is None:
            if replacement_budget is not None:
                raise CollaborativeDevelopmentConflict(
                    "replacement budget was not part of the authority request"
                )
        elif replacement_budget != requested.budget:
            raise CollaborativeDevelopmentConflict(
                "replacement budget must exactly match the requested budget"
            )
        return current

    @staticmethod
    def _verify_workspace(
        grant: WorkspaceGrant,
        *,
        require_clean: bool,
    ) -> tuple[Path, Path]:
        root = Path(grant.workspace_root)
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise CollaborativeDevelopmentConflict(
                "granted development workspace is unavailable"
            ) from error
        if str(resolved) != grant.workspace_root or root.is_symlink():
            raise CollaborativeDevelopmentConflict(
                "granted development workspace must be a resolved non-symlink path"
            )
        try:
            head = subprocess.run(
                [
                    "git",
                    "-C",
                    str(resolved),
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            common = subprocess.run(
                ["git", "-C", str(resolved), "rev-parse", "--git-common-dir"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            dirty = subprocess.run(
                [
                    "git",
                    "-C",
                    str(resolved),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CollaborativeDevelopmentConflict(
                "granted development workspace is not inspectable"
            ) from error
        if head.returncode != 0 or head.stdout.strip() != grant.baseline_commit:
            raise CollaborativeDevelopmentConflict(
                "granted development workspace does not match the frozen baseline"
            )
        if common.returncode != 0 or not common.stdout.strip():
            raise CollaborativeDevelopmentConflict(
                "granted development workspace has no inspectable Git common directory"
            )
        if dirty.returncode != 0:
            raise CollaborativeDevelopmentConflict(
                "granted development workspace status is not inspectable"
            )
        if require_clean and dirty.stdout:
            raise CollaborativeDevelopmentConflict(
                "granted development workspace must be clean at assignment creation"
            )
        common_path = Path(common.stdout.strip())
        if not common_path.is_absolute():
            common_path = resolved / common_path
        try:
            common_resolved = common_path.resolve(strict=True)
        except OSError as error:
            raise CollaborativeDevelopmentConflict(
                "granted development workspace Git common directory is unavailable"
            ) from error
        return resolved, common_resolved

    @classmethod
    def _attested_workspaces(
        cls,
        assignment: DevelopmentAssignment,
        *,
        require_clean: bool,
    ) -> tuple[DevelopmentWorkspaceBroker, PreparedDevelopmentWorkspaces]:
        inspected = {
            grant.agent_role: cls._verify_workspace(
                grant,
                require_clean=require_clean,
            )
            for grant in assignment.workspace_grants
        }
        roots = [root for root, _common in inspected.values()]
        if any(
            left in right.parents or right in left.parents
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise CollaborativeDevelopmentConflict(
                "role workspace roots must not contain one another"
            )
        common_dirs = [common for _root, common in inspected.values()]
        if len(set(common_dirs)) != len(common_dirs):
            raise CollaborativeDevelopmentConflict(
                "role workspaces must not share a Git worktree common directory"
            )
        assignment_roots = {root.parent for root in roots}
        if len(assignment_roots) != 1:
            raise CollaborativeDevelopmentConflict(
                "role workspaces are not part of one broker-prepared assignment"
            )
        assignment_root = next(iter(assignment_roots))
        broker = DevelopmentWorkspaceBroker(assignment_root.parent)
        try:
            prepared = broker.load_prepared(assignment.assignment_id)
        except DevelopmentWorkspaceError as error:
            raise CollaborativeDevelopmentConflict(
                "role workspaces lack a valid broker attestation"
            ) from error
        prepared_grants = {grant.agent_role: grant for grant in prepared.grants}
        assignment_grants = {grant.agent_role: grant for grant in assignment.workspace_grants}
        if (
            prepared.baseline_commit != assignment.baseline_commit
            or prepared_grants != assignment_grants
        ):
            raise CollaborativeDevelopmentConflict(
                "assignment authority differs from its broker-attested manifest"
            )
        return broker, prepared

    @classmethod
    def _authority_revision_broker(
        cls,
        *,
        assignment: DevelopmentAssignment,
        request: DevelopmentAuthorizationRequestView,
        replacement_grant: WorkspaceGrant,
    ) -> tuple[DevelopmentWorkspaceBroker, PreparedDevelopmentWorkspaces]:
        inspected = {
            grant.agent_role: cls._verify_workspace(
                grant,
                require_clean=False,
            )
            for grant in assignment.workspace_grants
        }
        roots = [root for root, _common in inspected.values()]
        if any(
            left in right.parents or right in left.parents
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise CollaborativeDevelopmentConflict(
                "role workspace roots must not contain one another"
            )
        common_dirs = [common for _root, common in inspected.values()]
        if len(set(common_dirs)) != len(common_dirs):
            raise CollaborativeDevelopmentConflict(
                "role workspaces must not share a Git worktree common directory"
            )
        assignment_roots = {root.parent for root in roots}
        if len(assignment_roots) != 1:
            raise CollaborativeDevelopmentConflict(
                "role workspaces are not part of one broker-prepared assignment"
            )
        assignment_root = next(iter(assignment_roots))
        broker = DevelopmentWorkspaceBroker(assignment_root.parent)
        try:
            prepared = broker.load_prepared(assignment.assignment_id)
        except DevelopmentWorkspaceError as error:
            raise CollaborativeDevelopmentConflict(
                "role workspaces lack a valid broker attestation"
            ) from error
        if prepared.baseline_commit != assignment.baseline_commit:
            raise CollaborativeDevelopmentConflict(
                "workspace manifest baseline differs from the assignment"
            )
        prepared_by_role = {grant.agent_role: grant for grant in prepared.grants}
        assignment_by_role = {grant.agent_role: grant for grant in assignment.workspace_grants}
        for role, assignment_grant in assignment_by_role.items():
            prepared_grant = prepared_by_role.get(role)
            if prepared_grant is None:
                raise CollaborativeDevelopmentConflict(
                    "workspace manifest lacks an assignment role"
                )
            if role == request.destination_role:
                allowed_digests = {
                    request.existing_grant_digest,
                    canonical_digest(replacement_grant),
                }
                if canonical_digest(prepared_grant) not in allowed_digests:
                    raise CollaborativeDevelopmentConflict(
                        "workspace manifest has a competing grant revision"
                    )
            elif prepared_grant != assignment_grant:
                raise CollaborativeDevelopmentConflict("another role's workspace authority changed")
        return broker, prepared

    @staticmethod
    def _translate(error: Exception) -> CollaborativeDevelopmentError:
        if isinstance(error, CollaborativeDevelopmentError):
            return error
        if isinstance(error, StoreNotFound):
            return CollaborativeDevelopmentNotFound("resource was not found")
        if isinstance(
            error,
            (
                StoreConflict,
                CollaborativeDevelopmentInvalidState,
                CollaborativeDevelopmentAuthorizationError,
            ),
        ):
            return CollaborativeDevelopmentConflict(str(error))
        if isinstance(error, CollaborativeDevelopmentBudgetExceeded):
            return CollaborativeDevelopmentLimitExceeded(str(error))
        if isinstance(error, CollaborativeDevelopmentStorageError):
            return CollaborativeDevelopmentError(
                "collaborative development storage operation failed"
            )
        raise error

    async def _assignment(
        self,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> DevelopmentAssignment:
        self.require_enabled()
        self._require_assignment_binding(principal, assignment_id)
        try:
            return await self.store.get_assignment(assignment_id)
        except Exception as error:
            raise self._translate(error) from error

    async def create_assignment(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment: DevelopmentAssignment,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        self.require_enabled()
        self._require_user(principal)
        if assignment.execution_mode == ExecutionMode.autonomous:
            self.require_autonomous_enabled()
        try:
            await self.store.get_assignment(assignment.assignment_id)
        except StoreNotFound:
            self._attested_workspaces(assignment, require_clean=True)
        except Exception as error:
            raise self._translate(error) from error
        try:
            return await self.store.create_assignment(
                assignment,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def get_assignment(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> DevelopmentAssignment | DevelopmentAssignmentProjection:
        assignment = await self._assignment(principal, assignment_id)
        role = principal.agent_role
        if role is None:
            self._require_user(principal)
            return assignment
        try:
            return DevelopmentAssignmentProjection.from_assignment(assignment, role)
        except ValueError as error:
            raise CollaborativeDevelopmentUnauthorized("resource was not found") from error

    async def set_execution_mode(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        mode: ExecutionMode,
        expected_revision: int,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        self.require_enabled()
        self._require_user(principal)
        if mode == ExecutionMode.autonomous:
            self.require_autonomous_enabled()
        try:
            return await self.store.set_execution_mode(
                assignment_id,
                mode,
                expected_revision=expected_revision,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def set_approval_mode(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        mode: ApprovalMode,
        expected_revision: int,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        self.require_enabled()
        self._require_user(principal)
        try:
            return await self.store.set_approval_mode(
                assignment_id,
                mode,
                expected_revision=expected_revision,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def create_work_item(
        self,
        *,
        principal: DevelopmentPrincipal,
        item: DevelopmentWorkItem,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        assignment = await self._assignment(principal, item.assignment_id)
        if principal.actor_role != "user":
            role = principal.agent_role
            if role is None or DevelopmentTaskRole.coordinator not in self._task_roles(
                assignment, role
            ):
                raise CollaborativeDevelopmentUnauthorized("resource was not found")
        try:
            return await self.store.create_work_item(
                item,
                actor_role=principal.actor_role,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def list_work_items(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> list[DevelopmentWorkItem]:
        await self._assignment(principal, assignment_id)
        try:
            return await self.store.list_work_items(assignment_id)
        except Exception as error:
            raise self._translate(error) from error

    @staticmethod
    def _review_requeue_dispatch_behavior(mode: ExecutionMode) -> str:
        if mode == ExecutionMode.autonomous:
            return "eligible_for_next_autonomous_worker_poll"
        return "eligible_when_operator_runs_dispatch_worker"

    async def list_review_reconciliations(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> dict[str, Any]:
        """Expose unknown review outcomes only to the assignment owner."""

        self.require_enabled()
        self._require_user(principal)
        assignment = await self._assignment(principal, assignment_id)
        try:
            reconciliations = await self.store.list_review_reconciliations(
                assignment_id
            )
        except Exception as error:
            raise self._translate(error) from error
        return {
            "assignment_id": assignment_id,
            "execution_mode": assignment.execution_mode,
            "dispatch_behavior": self._review_requeue_dispatch_behavior(
                assignment.execution_mode
            ),
            "automatic_unknown_outcome_replay": False,
            "reconciliations": reconciliations,
            "enterprise_denominator": False,
        }

    async def requeue_review_reconciliation(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        outbox_id: UUID,
        expected_work_item_revision: int,
        expected_failed_attempt: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Owner-authorized, idempotent recovery for one uncertain review."""

        self.require_enabled()
        self._require_user(principal)
        assignment = await self._assignment(principal, assignment_id)
        try:
            outbox: DispatchOutboxItem = (
                await self.store.requeue_review_reconciliation(
                    assignment_id,
                    outbox_id=outbox_id,
                    expected_work_item_revision=expected_work_item_revision,
                    expected_failed_attempt=expected_failed_attempt,
                    reason=reason,
                    actor_id=principal.actor_id,
                    idempotency_key=idempotency_key,
                )
            )
        except Exception as error:
            raise self._translate(error) from error
        return {
            "assignment_id": assignment_id,
            "execution_mode": assignment.execution_mode,
            "dispatch_behavior": self._review_requeue_dispatch_behavior(
                assignment.execution_mode
            ),
            "automatic_unknown_outcome_replay": False,
            "grant_changed": False,
            "budget_reset": False,
            "requeued_outbox": outbox,
            "enterprise_denominator": False,
        }

    async def dispatch_work_item(
        self,
        *,
        principal: DevelopmentPrincipal,
        work_item_id: UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        self.require_enabled()
        self._require_user(principal)
        try:
            return await self.store.dispatch_work_item(
                work_item_id,
                expected_revision=expected_revision,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def acquire_lease(
        self,
        *,
        principal: DevelopmentPrincipal,
        work_item_id: UUID,
        expected_revision: int,
        ttl_seconds: int,
        idempotency_key: str,
    ) -> DevelopmentLease:
        self.require_enabled()
        role = principal.agent_role
        if role is None or principal.assignment_id is None:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        item = await self.store.get_work_item(work_item_id)
        self._require_assignment_binding(principal, item.assignment_id)
        try:
            return await self.store.acquire_lease(
                work_item_id,
                owner_role=role,
                owner_id=principal.actor_id,
                expected_revision=expected_revision,
                ttl_seconds=ttl_seconds,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def start_work(
        self,
        *,
        principal: DevelopmentPrincipal,
        lease_id: UUID,
        expected_work_item_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        self.require_enabled()
        role = principal.agent_role
        if role is None or principal.assignment_id is None:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        try:
            lease = await self.store.get_lease(lease_id)
            self._require_assignment_binding(principal, lease.assignment_id)
            if lease.owner_role != role:
                raise CollaborativeDevelopmentUnauthorized("resource was not found")
            item = await self.store.start_work(
                lease_id,
                owner_id=principal.actor_id,
                expected_work_item_revision=expected_work_item_revision,
                idempotency_key=idempotency_key,
            )
            return item
        except Exception as error:
            raise self._translate(error) from error

    async def submit_result(
        self,
        *,
        principal: DevelopmentPrincipal,
        result: DevelopmentResult,
        expected_work_item_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        self.require_enabled()
        if principal.agent_role != result.agent_role:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        self._require_assignment_binding(principal, result.assignment_id)
        try:
            return await self.store.submit_result(
                result,
                owner_id=principal.actor_id,
                expected_work_item_revision=expected_work_item_revision,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def abort_work(
        self,
        *,
        principal: DevelopmentPrincipal,
        lease_id: UUID,
        expected_work_item_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        self.require_enabled()
        role = principal.agent_role
        if role is None or principal.assignment_id is None:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        try:
            lease = await self.store.get_lease(lease_id)
            self._require_assignment_binding(principal, lease.assignment_id)
            if lease.owner_role != role:
                raise CollaborativeDevelopmentUnauthorized("resource was not found")
            return await self.store.abort_work(
                lease_id,
                owner_id=principal.actor_id,
                expected_work_item_revision=expected_work_item_revision,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def get_result(
        self,
        *,
        principal: DevelopmentPrincipal,
        result_id: UUID,
    ) -> DevelopmentResult:
        self.require_enabled()
        try:
            result = await self.store.get_result(result_id)
            self._require_assignment_binding(principal, result.assignment_id)
            if principal.agent_role not in {AgentRole.lilies, result.agent_role}:
                raise CollaborativeDevelopmentUnauthorized("resource was not found")
            return result
        except Exception as error:
            raise self._translate(error) from error

    async def prepare_review_snapshot(
        self,
        *,
        principal: DevelopmentPrincipal,
        result_id: UUID,
        idempotency_key: str,
    ) -> DevelopmentReviewSnapshotReceipt:
        """Prepare one immutable, role-bound result for independent review.

        Result reads remain side-effect free.  This explicit command is
        outcome-idempotent because the broker persists and verifies one
        deterministic receipt per frozen result.
        """

        self.require_enabled()
        if (
            principal.agent_role != AgentRole.lilies
            or principal.assignment_id is None
        ):
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        if not 8 <= len(idempotency_key) <= 240 or any(
            ord(character) < 32 for character in idempotency_key
        ):
            raise CollaborativeDevelopmentConflict(
                "review snapshot idempotency key is invalid"
            )
        try:
            result = await self.get_result(
                principal=principal,
                result_id=result_id,
            )
            item = await self.store.get_work_item(result.work_item_id)
            if (
                item.assignment_id != result.assignment_id
                or item.work_item_id != result.work_item_id
                or item.status != WorkItemStatus.ready_for_lilies_review
            ):
                raise CollaborativeDevelopmentConflict(
                    "result is not currently ready for Lilies review"
                )
            assignment = await self.store.get_assignment(result.assignment_id)
            broker, prepared = self._attested_workspaces(
                assignment,
                require_clean=False,
            )
            try:
                return await asyncio.to_thread(
                    broker.materialize_review_snapshot,
                    prepared=prepared,
                    result=result,
                )
            except DevelopmentWorkspaceError as error:
                raise CollaborativeDevelopmentConflict(
                    "review snapshot could not be prepared from the frozen result"
                ) from error
        except Exception as error:
            raise self._translate(error) from error

    async def submit_review(
        self,
        *,
        principal: DevelopmentPrincipal,
        review: LiliesReview,
        expected_work_item_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        self.require_enabled()
        if principal.agent_role != AgentRole.lilies:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        self._require_assignment_binding(principal, review.assignment_id)
        try:
            assignment = await self.store.get_assignment(review.assignment_id)
            result = await self.store.get_result(review.result_id)
            if review.verdict.value == "accepted":
                broker, prepared = self._attested_workspaces(
                    assignment,
                    require_clean=False,
                )
                receipt = await asyncio.to_thread(
                    broker.materialize_review_snapshot,
                    prepared=prepared,
                    result=result,
                )
                if (
                    result.diff_digest not in review.evidence_refs
                    or receipt.receipt_digest not in review.evidence_refs
                ):
                    raise CollaborativeDevelopmentConflict(
                        "accepted review lacks its trusted result and snapshot bindings"
                    )
            return await self.store.submit_review(
                review,
                reviewer_id=principal.actor_id,
                expected_work_item_revision=expected_work_item_revision,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def close_work_item(
        self,
        *,
        principal: DevelopmentPrincipal,
        work_item_id: UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        self.require_enabled()
        self._require_user(principal)
        try:
            return await self.store.close_work_item(
                work_item_id,
                expected_revision=expected_revision,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def stop_assignment(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        self.require_enabled()
        self._require_user(principal)
        try:
            return await self.store.stop_assignment(
                assignment_id,
                expected_revision=expected_revision,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def archive_assignment(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        self.require_enabled()
        self._require_user(principal)
        try:
            return await self.store.archive_assignment(
                assignment_id,
                expected_revision=expected_revision,
                actor_id=principal.actor_id,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def list_authority_requests(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        status: str = "pending",
    ) -> list[DevelopmentAuthorizationRequestView]:
        self.require_enabled()
        self._require_user(principal)
        await self._assignment(principal, assignment_id)
        if status not in {"pending", "approved", "rejected", "all"}:
            raise CollaborativeDevelopmentConflict("authority request status filter is invalid")
        try:
            return await self.authority_store.list_requests(
                assignment_id,
                status=cast(
                    Literal["pending", "approved", "rejected", "all"],
                    status,
                ),
            )
        except DevelopmentAuthorityDecisionError as error:
            raise CollaborativeDevelopmentError("authority requests could not be read") from error

    async def approve_authority_request(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        request_id: UUID,
        reason: str,
        idempotency_key: str,
        expected_assignment_revision: int,
        replacement_grant: WorkspaceGrant,
        replacement_budget: DevelopmentBudget | None,
    ) -> DevelopmentAuthorizationRequestView:
        self.require_enabled()
        self._require_user(principal)
        assignment = await self._assignment(principal, assignment_id)
        if assignment.status != AssignmentStatus.active or utc_now() >= assignment.deadline:
            raise CollaborativeDevelopmentConflict(
                "authority requests cannot be decided after assignment stop or expiry"
            )
        try:
            request = await self.authority_store.get_request(
                assignment_id,
                request_id,
            )
            if request.status == "rejected":
                raise CollaborativeDevelopmentConflict("authority request was already rejected")
            if request.status == "pending":
                self._validate_requested_grant_revision(
                    assignment=assignment,
                    request=request,
                    replacement_grant=replacement_grant,
                    replacement_budget=replacement_budget,
                    expected_assignment_revision=expected_assignment_revision,
                )
            else:
                if (
                    request.replacement_grant != replacement_grant
                    or request.replacement_budget != replacement_budget
                    or request.expected_assignment_revision != expected_assignment_revision
                ):
                    raise CollaborativeDevelopmentConflict(
                        "approved authority request cannot change its replacement"
                    )
                current = self._workspace_grant(
                    assignment,
                    request.destination_role,
                )
                if (
                    current != request.replacement_grant
                    and canonical_digest(current) != request.existing_grant_digest
                ):
                    raise CollaborativeDevelopmentConflict(
                        "assignment has a competing workspace grant revision"
                    )
            reserved = await self.authority_store.decide(
                assignment_id=assignment_id,
                request_id=request_id,
                decision=AuthorityDecision.approved,
                actor_id=principal.actor_id,
                reason=reason,
                idempotency_key=idempotency_key,
                replacement_grant=replacement_grant,
                replacement_budget=replacement_budget,
                expected_assignment_revision=expected_assignment_revision,
            )
            broker, prepared = self._authority_revision_broker(
                assignment=assignment,
                request=reserved,
                replacement_grant=replacement_grant,
            )
            updated_assignment = await self.store.apply_workspace_grant_revision(
                assignment_id,
                outbox_id=reserved.outbox_id,
                replacement_grant=replacement_grant,
                replacement_budget=replacement_budget,
                expected_assignment_revision=expected_assignment_revision,
                expected_grant_digest=reserved.existing_grant_digest,
                actor_id=principal.actor_id,
                idempotency_key=f"authority:{request_id}:apply-grant",
            )
            try:
                await asyncio.to_thread(
                    broker.revise_prepared_grant,
                    prepared=prepared,
                    expected_manifest_digest=prepared.manifest_digest,
                    replacement_grant=replacement_grant,
                )
            except DevelopmentWorkspaceError as error:
                raise CollaborativeDevelopmentConflict(
                    "approved grant could not update its broker attestation"
                ) from error
            await self.store.resume_authorization_outbox(
                assignment_id,
                outbox_id=reserved.outbox_id,
                expected_grant_digest=canonical_digest(replacement_grant),
                actor_id=principal.actor_id,
                idempotency_key=f"authority:{request_id}:resume-outbox",
            )
            applied = await self.authority_store.mark_applied(
                assignment_id=assignment_id,
                request_id=request_id,
            )
            if (
                updated_assignment.revision != expected_assignment_revision + 1
                or not applied.grant_changed
                or not applied.execution_resumed
            ):
                raise CollaborativeDevelopmentError(
                    "approved authority revision did not reach its applied state"
                )
            return applied
        except DevelopmentAuthorityRequestNotFound as error:
            raise CollaborativeDevelopmentNotFound("resource was not found") from error
        except DevelopmentAuthorityDecisionConflict as error:
            raise CollaborativeDevelopmentConflict(str(error)) from error
        except DevelopmentAuthorityDecisionError as error:
            raise CollaborativeDevelopmentError(
                "authority decision could not be persisted"
            ) from error
        except Exception as error:
            raise self._translate(error) from error

    async def reject_authority_request(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        request_id: UUID,
        reason: str,
        idempotency_key: str,
    ) -> DevelopmentAuthorizationRequestView:
        self.require_enabled()
        self._require_user(principal)
        assignment = await self._assignment(principal, assignment_id)
        if assignment.status != AssignmentStatus.active or utc_now() >= assignment.deadline:
            raise CollaborativeDevelopmentConflict(
                "authority requests cannot be decided after assignment stop or expiry"
            )
        try:
            return await self.authority_store.decide(
                assignment_id=assignment_id,
                request_id=request_id,
                decision=AuthorityDecision.rejected,
                actor_id=principal.actor_id,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        except DevelopmentAuthorityRequestNotFound as error:
            raise CollaborativeDevelopmentNotFound("resource was not found") from error
        except DevelopmentAuthorityDecisionConflict as error:
            raise CollaborativeDevelopmentConflict(str(error)) from error
        except DevelopmentAuthorityDecisionError as error:
            raise CollaborativeDevelopmentError(
                "authority decision could not be persisted"
            ) from error

    async def read_events(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        after: int,
        limit: int,
    ) -> list[Any]:
        await self._assignment(principal, assignment_id)
        try:
            return await self.store.read_events(
                assignment_id,
                after=after,
                limit=limit,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def ack_events(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
        ack_seq: int,
        expected_cursor_revision: int,
        idempotency_key: str,
    ) -> ReaderCursor:
        await self._assignment(principal, assignment_id)
        try:
            return await self.store.ack_events(
                assignment_id,
                reader_role=principal.actor_role,
                reader_id=principal.actor_id,
                ack_seq=ack_seq,
                expected_cursor_revision=expected_cursor_revision,
                idempotency_key=idempotency_key,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def reader_cursor(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> ReaderCursor | None:
        await self._assignment(principal, assignment_id)
        try:
            return await self.store.get_reader_cursor(
                assignment_id,
                reader_role=principal.actor_role,
                reader_id=principal.actor_id,
            )
        except Exception as error:
            raise self._translate(error) from error

    async def workspace_authority(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> WorkspaceGrant:
        assignment = await self._assignment(principal, assignment_id)
        role = principal.agent_role
        if role is None:
            raise CollaborativeDevelopmentUnauthorized("resource was not found")
        return self._workspace_grant(assignment, role)

    async def status_summary(
        self,
        *,
        principal: DevelopmentPrincipal,
        assignment_id: UUID,
    ) -> dict[str, Any]:
        assignment = await self._assignment(principal, assignment_id)
        items = await self.list_work_items(
            principal=principal,
            assignment_id=assignment_id,
        )
        counts = {
            status.value: sum(item.status == status for item in items) for status in WorkItemStatus
        }
        if principal.agent_role is None:
            self._require_user(principal)
            assignment_view: DevelopmentAssignment | DevelopmentAssignmentProjection = assignment
        else:
            assignment_view = DevelopmentAssignmentProjection.from_assignment(
                assignment,
                principal.agent_role,
            )
        return {
            "assignment": assignment_view,
            "work_item_counts": counts,
            "enterprise_denominator": False,
        }


__all__ = [
    "AutonomousCollaborationDisabled",
    "CollaborativeDevelopmentConflict",
    "CollaborativeDevelopmentDisabled",
    "CollaborativeDevelopmentError",
    "CollaborativeDevelopmentLimitExceeded",
    "CollaborativeDevelopmentNotFound",
    "CollaborativeDevelopmentService",
    "CollaborativeDevelopmentUnauthorized",
]
