"""Role-scoped input boundary for collaborative-development agent handlers.

Trusted orchestration may load a complete ``DevelopmentAssignment`` to enforce
leases, persistence, and workspace-broker invariants.  A role handler must
never receive that owner view: it gets one deep-copied role projection plus
only the lifecycle records required for its current handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event as ThreadEvent

from .collaborative_development_models import (
    DevelopmentAssignment,
    DevelopmentAssignmentProjection,
    DevelopmentLease,
    DevelopmentResult,
    DevelopmentWorkItem,
    DispatchOutboxItem,
    WorkspaceGrant,
)
from .development_workspace_broker import DevelopmentReviewSnapshotReceipt


@dataclass(frozen=True, slots=True)
class RoleBoundDispatchContext:
    """The complete and only context visible to one embedded role handler."""

    outbox: DispatchOutboxItem
    assignment: DevelopmentAssignmentProjection
    work_item: DevelopmentWorkItem
    lease: DevelopmentLease | None = None
    source_result: DevelopmentResult | None = None
    review_snapshot: DevelopmentReviewSnapshotReceipt | None = None
    cancel_event: ThreadEvent | None = None

    @property
    def workspace_grant(self) -> WorkspaceGrant:
        return self.assignment.workspace_grant

    @classmethod
    def from_assignment(
        cls,
        *,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        lease: DevelopmentLease | None = None,
        source_result: DevelopmentResult | None = None,
        review_snapshot: DevelopmentReviewSnapshotReceipt | None = None,
        cancel_event: ThreadEvent | None = None,
        workspace_grant: WorkspaceGrant | None = None,
    ) -> RoleBoundDispatchContext:
        """Build a deep-copied single-role view from trusted assignment state."""

        projection = DevelopmentAssignmentProjection.from_assignment(
            assignment,
            outbox.destination_role,
        ).model_copy(deep=True)
        if workspace_grant is not None:
            if workspace_grant.agent_role != outbox.destination_role:
                raise ValueError("role handler workspace grant has the wrong role")
            persisted_grant = projection.workspace_grant
            if (
                workspace_grant.baseline_commit
                != persisted_grant.baseline_commit
                or workspace_grant.grant_revision
                != persisted_grant.grant_revision
                or not set(workspace_grant.allowed_paths).issubset(
                    persisted_grant.allowed_paths
                )
                or not set(workspace_grant.allowed_argv).issubset(
                    persisted_grant.allowed_argv
                )
                or not set(workspace_grant.allowed_hosts).issubset(
                    persisted_grant.allowed_hosts
                )
                or not set(workspace_grant.allowed_side_effects).issubset(
                    persisted_grant.allowed_side_effects
                )
                or not set(workspace_grant.secret_refs).issubset(
                    persisted_grant.secret_refs
                )
            ):
                raise ValueError(
                    "role handler workspace grant is not identical or narrower"
                )
            if review_snapshot is None and workspace_grant != persisted_grant:
                raise ValueError(
                    "only a bound review snapshot may replace the persisted workspace"
                )
            projection = projection.model_copy(
                update={"workspace_grant": workspace_grant.model_copy(deep=True)},
                deep=True,
            )
        context = cls(
            outbox=outbox.model_copy(deep=True),
            assignment=projection,
            work_item=work_item.model_copy(deep=True),
            lease=lease.model_copy(deep=True) if lease is not None else None,
            source_result=(
                source_result.model_copy(deep=True)
                if source_result is not None
                else None
            ),
            review_snapshot=(
                review_snapshot.model_copy(deep=True)
                if review_snapshot is not None
                else None
            ),
            cancel_event=cancel_event,
        )
        context._validate_bindings()
        return context

    def _validate_bindings(self) -> None:
        assignment_id = self.assignment.assignment_id
        work_item_id = self.work_item.work_item_id
        destination_role = self.outbox.destination_role
        if (
            self.outbox.assignment_id != assignment_id
            or self.work_item.assignment_id != assignment_id
            or self.outbox.work_item_id != work_item_id
        ):
            raise ValueError("role handler context has cross-assignment bindings")
        if (
            self.assignment.agent_role.agent_role != destination_role
            or self.workspace_grant.agent_role != destination_role
        ):
            raise ValueError("role handler context has cross-role authority")
        if self.workspace_grant.baseline_commit != self.assignment.baseline_commit:
            raise ValueError("role handler context changed the frozen baseline")
        if self.lease is not None and (
            self.lease.assignment_id != assignment_id
            or self.lease.work_item_id != work_item_id
            or self.lease.owner_role != destination_role
        ):
            raise ValueError("role handler context has a cross-role lease")
        if self.source_result is not None and (
            self.source_result.assignment_id != assignment_id
            or self.source_result.work_item_id != work_item_id
        ):
            raise ValueError("role handler context has a cross-assignment result")
        if self.review_snapshot is not None:
            if self.source_result is None:
                raise ValueError("review snapshot requires its bound source result")
            if (
                self.review_snapshot.assignment_id != assignment_id
                or self.review_snapshot.work_item_id != work_item_id
                or self.review_snapshot.result_id != self.source_result.result_id
                or self.review_snapshot.reviewer_role != destination_role
            ):
                raise ValueError("role handler context has a cross-role review snapshot")
            if (
                self.workspace_grant.workspace_id
                != self.review_snapshot.review_snapshot_id
                or self.workspace_grant.workspace_root
                != self.review_snapshot.review_workspace_root
            ):
                raise ValueError(
                    "role handler review grant differs from its frozen snapshot"
                )


__all__ = ["RoleBoundDispatchContext"]
