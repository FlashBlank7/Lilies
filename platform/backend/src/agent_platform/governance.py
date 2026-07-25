from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Literal, cast

from pydantic import BaseModel

from .capability_contracts import EvidenceEnvironment, VerificationStatus
from .capability_evidence import (
    CapabilityEvidenceCreateRequest,
    CapabilityEvidenceRegistry,
    EvidenceArtifact,
    EvidenceGap,
)
from .connector_sdk import ConnectorService
from .durable_jobs import DurableJobRecord, DurableJobStatus, DurableJobStore
from .models import utc_now
from .platform_harness import PlatformHarness, PlatformTaskRecord
from .storage import Storage
from .template_store import TemplateStore
from .workflow_storage import WorkflowStorage


SupportState = Literal["reported", "estimated", "unsupported", "not_recorded"]


class GovernanceTaskFilters(BaseModel):
    task_id: str | None = None
    kind: str | None = None
    status: str | None = None
    owner_id: str | None = None
    application_id: str | None = None
    workflow_id: str | None = None
    model: str | None = None
    parent_task_id: str | None = None
    created_from: str | None = None
    created_to: str | None = None
    query: str = ""


class GovernanceTaskPage(BaseModel):
    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int
    has_more: bool
    filters: GovernanceTaskFilters
    support: dict[str, SupportState]


class GovernanceService:
    """Cross-application governance projections over durable platform facts."""

    def __init__(
        self,
        *,
        storage: Storage,
        harness: PlatformHarness,
        workflow_store: WorkflowStorage,
        templates: TemplateStore,
        durable_jobs: DurableJobStore,
        connectors: ConnectorService,
    ) -> None:
        self.storage = storage
        self.harness = harness
        self.workflow_store = workflow_store
        self.templates = templates
        self.durable_jobs = durable_jobs
        self.connectors = connectors

    async def tasks(
        self,
        filters: GovernanceTaskFilters,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> GovernanceTaskPage:
        items = await self._filtered_task_items(filters)
        normalized_offset = max(0, offset)
        normalized_limit = max(1, min(limit, 200))
        selected = items[normalized_offset : normalized_offset + normalized_limit]
        return GovernanceTaskPage(
            items=selected,
            total=len(items),
            offset=normalized_offset,
            limit=normalized_limit,
            has_more=normalized_offset + normalized_limit < len(items),
            filters=filters,
            support={
                "task_status": "reported",
                "duration": "reported",
                "queue_delay": self._support_for_values(items, "queue_delay_seconds"),
                "application": self._support_for_values(items, "application_id"),
                "workflow": self._support_for_values(items, "workflow_id"),
                "model": self._support_for_values(items, "model"),
            },
        )

    async def overview(self, filters: GovernanceTaskFilters) -> dict[str, Any]:
        items = await self._filtered_task_items(filters)
        counts = Counter(item["status"] for item in items)
        durations = [
            float(item["duration_seconds"])
            for item in items
            if isinstance(item.get("duration_seconds"), (int, float))
        ]
        queue_delays = [
            float(item["queue_delay_seconds"])
            for item in items
            if isinstance(item.get("queue_delay_seconds"), (int, float))
        ]
        workers = await self.harness.list_worker_heartbeats(limit=500)
        alerts = await self.alerts(filters)
        durable_items = await self._durable_job_items(filters, limit=200)
        durable_counts = Counter(item["status"] for item in durable_items)
        return {
            "generated_at": utc_now(),
            "task_counts": {
                "total": len(items),
                "active": counts["running"],
                "queued": counts["queued"],
                "paused": counts["paused"],
                "succeeded": counts["succeeded"],
                "failed": counts["failed"],
                "cancelled": counts["cancelled"],
            },
            "duration_seconds": {
                "p50": self._percentile(durations, 50),
                "p95": self._percentile(durations, 95),
                "support": "reported" if durations else "not_recorded",
            },
            "queue_delay_seconds": {
                "p50": self._percentile(queue_delays, 50),
                "p95": self._percentile(queue_delays, 95),
                "support": "reported" if queue_delays else "not_recorded",
            },
            "workers": {
                "total": len(workers),
                "active": sum(item.liveness == "active" for item in workers),
                "stale": sum(item.liveness == "stale" for item in workers),
            },
            "durable_jobs": {
                "observed": len(durable_items),
                "observation_limit": 200,
                "active": durable_counts["running"],
                "queued": durable_counts["queued"],
                "retry_wait": durable_counts["retry_wait"],
                "paused": durable_counts["paused"],
                "succeeded": durable_counts["succeeded"],
                "failed": durable_counts["failed"],
                "cancelled": durable_counts["cancelled"],
            },
            "recent_failures": [item for item in items if item["status"] == "failed"][:10],
            "alerts": alerts["items"][:10],
            "filters": filters.model_dump(mode="json"),
            "claim_boundary": (
                "Local durable task and integration evidence; no production SLO or paging claim."
            ),
        }

    async def durable_job_operations(
        self,
        *,
        application_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        valid_statuses: set[DurableJobStatus] = {
            "queued",
            "running",
            "retry_wait",
            "paused",
            "succeeded",
            "failed",
            "cancelled",
        }
        if status and status not in valid_statuses:
            raise ValueError(f"unsupported durable job status: {status}")
        jobs = await self.durable_jobs.list(
            application_id,
            statuses={cast(DurableJobStatus, status)} if status else None,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
        names = await self._application_names()
        items = [self._durable_job_item(job, names) for job in jobs]
        counts = Counter(item["status"] for item in items)
        return {
            "items": items,
            "observed": len(items),
            "observation_limit": max(1, min(limit, 200)),
            "offset": max(0, offset),
            "counts": dict(counts),
            "support": {
                "lifecycle": "reported",
                "attempts": "reported",
                "lease_fencing": "reported",
                "checkpoints": "reported",
                "collection_receipts": "reported",
                "production_slo": "unsupported",
                "external_paging": "unsupported",
            },
            "claim_boundary": (
                "Bounded local durable-job operations and controlled collection evidence; "
                "not a production reliability or arbitrary-site access claim."
            ),
        }

    async def connector_operations(
        self,
        *,
        connector_id: str | None = None,
        tenant_id: str | None = None,
        operation_id: str | None = None,
        status: str | None = None,
        emergency_stop: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(limit, 200))
        normalized_offset = max(0, offset)
        executions = await self.connectors.list_executions(
            connector_id=connector_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            status=status,
            limit=normalized_limit,
            offset=normalized_offset,
        )
        policies = [
            item
            for item in await self.connectors.list_policies()
            if (not connector_id or item.connector_id == connector_id)
            and (not tenant_id or item.tenant_id == tenant_id)
            and (emergency_stop is None or item.emergency_stop is emergency_stop)
        ]
        bindings = await self.connectors.list_bindings(
            connector_id,
            tenant_id=tenant_id,
        )
        exercises = await self.connectors.list_exercises(
            connector_id=connector_id,
            tenant_id=tenant_id,
        )
        manifests = [
            item
            for item in await self.connectors.list_manifests()
            if not connector_id or item.connector_id == connector_id
        ]
        counts = Counter(item.status for item in executions)
        return {
            "items": [item.public_receipt() for item in executions],
            "offset": normalized_offset,
            "limit": normalized_limit,
            "has_more": len(executions) == normalized_limit,
            "counts": dict(counts),
            "manifests": [
                {
                    "connector_id": item.connector_id,
                    "version": item.version,
                    "title": item.title,
                    "domain": item.domain,
                    "operations": [operation.id for operation in item.operations],
                    "profiles": [
                        {
                            "id": profile.id,
                            "environment": profile.environment,
                            "available": profile.available,
                            "claim_ceiling": profile.claim_ceiling,
                        }
                        for profile in item.deployment_profiles
                    ],
                }
                for item in manifests
            ],
            "bindings": [
                {
                    "connector_id": item.connector_id,
                    "connector_version": item.connector_version,
                    "tenant_id": item.tenant_id,
                    "profile_id": item.profile_id,
                    "application_count": len(item.application_ids),
                    "allowed_operations": item.allowed_operations,
                    "subject_count": len(item.subjects),
                    "enabled": item.enabled,
                    "revision": item.revision,
                }
                for item in bindings
            ],
            "policies": [
                {
                    "connector_id": item.connector_id,
                    "connector_version": item.connector_version,
                    "tenant_id": item.tenant_id,
                    "domain": item.domain,
                    "allowed_profiles": item.allowed_profiles,
                    "allowed_operations": item.allowed_operations,
                    "mutation_preauthorization_required": (
                        item.mutation_preauthorization_required
                    ),
                    "emergency_stop": item.emergency_stop,
                    "emergency_reason": item.emergency_reason,
                    "revision": item.revision,
                }
                for item in policies
            ],
            "exercises": [item.model_dump(mode="json") for item in exercises[:100]],
            "support": {
                "tenant_identity": "reported" if bindings else "not_recorded",
                "schema_contract": "reported" if manifests else "not_recorded",
                "policy": "reported" if policies else "not_recorded",
                "idempotency": "reported",
                "writeback_receipt": "reported" if executions else "not_recorded",
                "callback": "reported" if any(item.callback_status for item in executions) else "not_recorded",
                "compensation_exercise": (
                    "reported"
                    if any(item.kind == "compensation" for item in exercises)
                    else "not_recorded"
                ),
                "production_slo": "unsupported",
            },
            "claim_boundary": (
                "Tenant-safe Connector metadata and controlled-test evidence only; raw secrets are "
                "never projected here, and production readiness is unsupported."
            ),
        }

    async def usage(
        self,
        filters: GovernanceTaskFilters,
        *,
        provider: str | None = None,
        interval: Literal["hour", "day"] = "hour",
        limit: int = 1000,
    ) -> dict[str, Any]:
        records = await self._task_records()
        all_samples: list[dict[str, Any]] = []
        for task in records:
            item = self._task_item(task, {})
            if not self._task_matches(item, filters, ignore_time=True):
                continue
            for usage in task.usage:
                if usage.usage_type != "model_usage":
                    continue
                sample = {"created_at": usage.created_at, **usage.metadata}
                if provider and sample.get("provider") != provider:
                    continue
                if not self._within_time(sample["created_at"], filters.created_from, filters.created_to):
                    continue
                all_samples.append(sample)
        all_samples.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        normalized_limit = max(1, min(limit, 5000))
        returned_samples = all_samples[:normalized_limit]
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
            "cost_usd",
        )
        support = {field: self._usage_support(all_samples, field) for field in fields}
        cache_read_support = support["cache_read_input_tokens"]
        cache_creation_support = support["cache_creation_input_tokens"]
        if not all_samples:
            support["cached_input_tokens"] = "not_recorded"
        elif cache_read_support == cache_creation_support == "reported":
            support["cached_input_tokens"] = "reported"
        elif cache_read_support == cache_creation_support == "estimated":
            support["cached_input_tokens"] = "estimated"
        else:
            support["cached_input_tokens"] = "unsupported"
        totals: dict[str, int | float | None] = {}
        for field in fields:
            values = [sample.get(field) for sample in all_samples]
            numeric = [value for value in values if isinstance(value, (int, float))]
            totals[field] = sum(numeric) if numeric else None
        cached_values = [
            int(sample.get("cache_read_input_tokens") or 0)
            + int(sample.get("cache_creation_input_tokens") or 0)
            for sample in all_samples
            if isinstance(sample.get("cache_read_input_tokens"), (int, float))
            or isinstance(sample.get("cache_creation_input_tokens"), (int, float))
        ]
        totals["cached_input_tokens"] = sum(cached_values) if cached_values else None
        buckets: dict[str, dict[str, Any]] = {}
        for sample in all_samples:
            parsed = self._parse_time(str(sample.get("created_at", "")))
            if parsed is None:
                continue
            key = (
                parsed.replace(minute=0, second=0, microsecond=0).isoformat()
                if interval == "hour"
                else parsed.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            )
            bucket = buckets.setdefault(
                key,
                {
                    "start": key,
                    "calls": 0,
                    "cached_input_tokens": 0,
                    **{field: 0 for field in fields},
                },
            )
            bucket["calls"] += 1
            for field in fields:
                value = sample.get(field)
                if isinstance(value, (int, float)):
                    bucket[field] += value
            bucket["cached_input_tokens"] += int(sample.get("cache_read_input_tokens") or 0)
            bucket["cached_input_tokens"] += int(sample.get("cache_creation_input_tokens") or 0)
        dimensions: dict[str, list[dict[str, Any]]] = {}
        for dimension in (
            "phase",
            "task_kind",
            "actor",
            "node_id",
            "model",
            "provider",
            "application_id",
            "workflow_id",
            "owner_id",
        ):
            grouped: dict[str, dict[str, Any]] = {}
            for sample in all_samples:
                key = str(sample.get(dimension) or "not_recorded")
                row = grouped.setdefault(
                    key,
                    {
                        dimension: key,
                        "calls": 0,
                        "tokens": 0,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                        "cost_usd": 0.0,
                    },
                )
                row["calls"] += 1
                row["tokens"] += sum(
                    int(sample.get(field) or 0)
                    for field in ("input_tokens", "output_tokens")
                )
                row["cached_input_tokens"] += int(
                    sample.get("cache_read_input_tokens") or 0
                ) + int(sample.get("cache_creation_input_tokens") or 0)
                row["reasoning_tokens"] += int(sample.get("reasoning_tokens") or 0)
                if isinstance(sample.get("cost_usd"), (int, float)):
                    row["cost_usd"] += float(sample["cost_usd"])
            dimensions[dimension] = sorted(
                grouped.values(),
                key=lambda item: (item["cost_usd"], item["tokens"], item["calls"]),
                reverse=True,
            )
        budgets = self._latest_budgets(all_samples)
        return {
            "generated_at": utc_now(),
            "samples": returned_samples,
            "sample_count": len(all_samples),
            "returned_sample_count": len(returned_samples),
            "has_more": len(returned_samples) < len(all_samples),
            "totals": totals,
            "support": support,
            "series": [buckets[key] for key in sorted(buckets)],
            "interval": interval,
            "dimensions": dimensions,
            "budgets": budgets,
            "filters": {**filters.model_dump(mode="json"), "provider": provider},
            "cost_boundary": (
                "provider_reported is authoritative; estimated_configured_price is an explicit estimate."
            ),
            "token_boundary": (
                "model_call counts are excluded from token totals and cannot satisfy token-limit claims."
            ),
        }

    async def reliability(self, filters: GovernanceTaskFilters) -> dict[str, Any]:
        items = await self._filtered_task_items(filters)
        task_map = {item["id"]: item for item in items}
        counters = Counter()
        examples: dict[str, list[str]] = defaultdict(list)
        for task_id, item in task_map.items():
            metadata = item.get("metadata") or {}
            lease = metadata.get("worker_lease") if isinstance(metadata, dict) else {}
            if item["status"] == "cancelled":
                counters["cancelled"] += 1
                examples["cancelled"].append(task_id)
            if metadata.get("stale_reconciled"):
                counters["stale_reconciled"] += 1
                examples["stale_reconciled"].append(task_id)
            if isinstance(lease, dict) and lease.get("expired"):
                counters["lease_expired"] += 1
                examples["lease_expired"].append(task_id)
            if metadata.get("origin") == "resume":
                counters["resumed"] += 1
                examples["resumed"].append(task_id)
            events = await self.storage.list_events(task_id)
            retry_count = sum("retry" in event.type for event in events)
            timeout_count = sum("timeout" in event.type for event in events)
            if retry_count:
                counters["retries"] += retry_count
                examples["retries"].append(task_id)
            if timeout_count or "timeout" in str(item.get("error", "")).casefold():
                counters["timeouts"] += max(1, timeout_count)
                examples["timeouts"].append(task_id)
        scheduler_events = await self.storage.list_events("scheduler")
        counters["schedule_deduplicated"] = sum(
            "dedup" in event.type or event.data.get("deduplicated") is True
            for event in scheduler_events
        )
        workers = await self.harness.list_worker_heartbeats(limit=500)
        queue = await self.harness.queue_semantics_snapshot(limit=500)
        task_counts = Counter(item["status"] for item in items)
        queue.update({
            "scope": "filtered_governance_tasks",
            "task_counts": {
                status: task_counts.get(status, 0)
                for status in (
                    "queued",
                    "running",
                    "paused",
                    "succeeded",
                    "failed",
                    "cancelled",
                )
            },
            "active_task_count": task_counts.get("queued", 0) + task_counts.get("running", 0),
        })
        return {
            "generated_at": utc_now(),
            "metrics": dict(counters),
            "examples": {key: value[:20] for key, value in examples.items()},
            "workers": [item.model_dump(mode="json") for item in workers],
            "queue": queue,
            "support": {
                "retry": "reported",
                "timeout": "reported",
                "cancellation": "reported",
                "stale_reconciliation": "reported",
                "lease_expiry": "reported",
                "resume": "reported",
                "schedule_deduplication": (
                    "reported" if scheduler_events else "not_recorded"
                ),
                "worker_heartbeat": "reported" if workers else "not_recorded",
            },
            "filters": filters.model_dump(mode="json"),
        }

    async def trace(self, task_id: str) -> dict[str, Any]:
        records = await self._task_records()
        by_id = {item.id: item for item in records}
        if task_id not in by_id:
            raise KeyError(f"platform task not found: {task_id}")
        children: dict[str, list[str]] = defaultdict(list)
        for record in records:
            if record.parent_task_id:
                children[record.parent_task_id].append(record.id)

        ancestors: list[str] = []
        seen: set[str] = set()
        current = by_id[task_id]
        while current.parent_task_id and current.parent_task_id in by_id:
            if current.parent_task_id in seen:
                break
            seen.add(current.parent_task_id)
            ancestors.append(current.parent_task_id)
            current = by_id[current.parent_task_id]
        root_id = ancestors[-1] if ancestors else task_id

        def build(node_id: str, lineage: set[str]) -> dict[str, Any]:
            record = by_id[node_id]
            next_lineage = {*lineage, node_id}
            return {
                **self._task_item(record, {}),
                "children": [
                    build(child_id, next_lineage)
                    for child_id in sorted(children.get(node_id, []))
                    if child_id not in next_lineage
                ],
            }

        related_ids: list[str] = []

        def collect(node_id: str) -> None:
            if node_id in related_ids:
                return
            related_ids.append(node_id)
            for child_id in children.get(node_id, []):
                collect(child_id)

        collect(root_id)
        spans: list[dict[str, Any]] = []
        for related_id in related_ids:
            record = by_id[related_id]
            for usage in record.usage:
                spans.append({
                    "task_id": related_id,
                    "span_type": usage.usage_type,
                    "created_at": usage.created_at,
                    "metadata": usage.metadata,
                })
            for event in await self.storage.list_events(related_id):
                spans.append({
                    "task_id": related_id,
                    "span_type": "event",
                    "event_type": event.type,
                    "created_at": event.created_at,
                    "metadata": event.data,
                })
        spans.sort(key=lambda item: str(item.get("created_at", "")))
        requested = by_id[task_id]
        durable_job_id = str(requested.metadata.get("durable_job_id") or "")
        durable_trace: dict[str, Any] | None = None
        if durable_job_id:
            try:
                job = await self.durable_jobs.get(durable_job_id)
                attempts = await self.durable_jobs.list_attempts(durable_job_id)
                events = await self.durable_jobs.list_events(durable_job_id, limit=1000)
                receipts = await self.durable_jobs.list_receipts(durable_job_id, limit=1000)
                durable_trace = {
                    "job": job.model_dump(mode="json"),
                    "attempts": [item.model_dump(mode="json") for item in attempts],
                    "events": [item.model_dump(mode="json") for item in events],
                    "receipts": [item.model_dump(mode="json") for item in receipts],
                }
            except KeyError:
                durable_trace = {
                    "job_id": durable_job_id,
                    "missing": True,
                    "reason": "linked durable job record was not found",
                }
        connector_records = []
        seen_connector_executions: set[str] = set()
        for related_id in related_ids:
            for record in await self.connectors.list_executions(
                run_id=related_id,
                limit=200,
            ):
                if record.id in seen_connector_executions:
                    continue
                seen_connector_executions.add(record.id)
                connector_records.append(record)
        connector_events: list[dict[str, Any]] = []
        for record in connector_records:
            connector_events.extend(
                await self.connectors.list_events(execution_id=record.id, limit=1000)
            )
        return {
            "requested_task_id": task_id,
            "root_task_id": root_id,
            "ancestors": ancestors,
            "tree": build(root_id, set()),
            "spans": spans,
            "durable_job": durable_trace,
            "connector": {
                "executions": [item.public_receipt() for item in connector_records],
                "events": connector_events,
                "claim_boundary": (
                    "Tenant-safe Connector receipts and audit events; raw request payloads and secrets "
                    "are excluded from this trace projection."
                ),
            },
            "support": {
                "parent_child": "reported",
                "task_usage": "reported",
                "task_events": "reported",
                "durable_job_link": "reported" if durable_job_id else "not_recorded",
                "connector_link": "reported" if connector_records else "not_recorded",
                "distributed_trace_context": "unsupported",
            },
        }

    async def policy(self) -> dict[str, Any]:
        events = await self.storage.list_events("platform-governance")
        return {
            "controls": self.harness.policy_controls(),
            "audit": [
                {
                    "id": event.id,
                    "type": event.type,
                    "created_at": event.created_at,
                    "data": event.data,
                }
                for event in reversed(events[-100:])
            ],
            "support": {
                "current_controls": "reported",
                "change_audit": "reported" if events else "not_recorded",
                "restart_persistence": "unsupported",
            },
        }

    async def capability_evidence(self) -> dict[str, Any]:
        records = self.templates.evidence.list()
        items: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            errors = self.templates.evidence.integrity_errors(record)
            item = {
                **record.model_dump(mode="json"),
                "artifact_categories": record.artifact_categories,
                "integrity": "intact" if not errors else "invalid",
                "integrity_errors": errors,
                "claim_source": "module" if record.module_id else "platform",
            }
            items.append(item)
            grouped[record.capability_id].append(item)
        capabilities = []
        rank = {
            "unsupported": -1,
            "blocked_by_environment": -1,
            "design_only": 0,
            "static_verified": 1,
            "component_verified": 2,
            "integration_verified": 3,
            "live_verified": 4,
            "production_observed": 5,
        }
        for capability_id, claims in sorted(grouped.items()):
            intact = [item for item in claims if item["integrity"] == "intact"]
            strongest = (
                max(
                    intact,
                    key=lambda item: rank.get(str(item.get("verification_status")), -2),
                )
                if intact
                else None
            )
            capabilities.append({
                "capability_id": capability_id,
                "strongest_status": (
                    strongest["verification_status"] if strongest else "unverified"
                ),
                "evidence_level": strongest["evidence_level"] if strongest else "H0",
                "claim_count": len(claims),
                "artifact_categories": sorted({
                    category
                    for claim in intact
                    for category in claim["artifact_categories"]
                }),
                "known_gaps": [gap for claim in claims for gap in claim.get("gaps", [])],
                "integrity": "intact" if intact else "invalid",
            })
        intact_items = [item for item in items if item["integrity"] == "intact"]
        return {
            "capabilities": capabilities,
            "records": sorted(items, key=lambda item: (item["capability_id"], item["created_at"])),
            "support": {
                category: self._category_support(intact_items, category)
                for category in (
                    "implementation",
                    "default",
                    "api",
                    "test",
                    "integration",
                    "live",
                    "telemetry",
                )
            } | {
                "production_completeness": "unsupported",
            },
            "claim_boundary": (
                "Only intact registered artifacts contribute to the strongest local claim."
            ),
        }

    async def alerts(self, filters: GovernanceTaskFilters) -> dict[str, Any]:
        tasks = await self._filtered_task_items(filters)
        durable_jobs = await self._durable_job_items(filters, limit=200)
        workers = await self.harness.list_worker_heartbeats(limit=500)
        items: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for task in tasks:
            if task["status"] == "failed":
                items.append(self._alert("task_failed", "high", task, task.get("error") or "Task failed"))
            if task["status"] == "queued":
                created = self._parse_time(task["created_at"])
                if created and now - created > timedelta(minutes=5):
                    items.append(self._alert("queue_delay", "medium", task, "Queued for more than five minutes"))
            usage_samples = [
                {"created_at": usage.get("created_at"), **usage.get("metadata", {})}
                for usage in task.get("usage", [])
                if usage.get("usage_type") == "model_usage"
                and isinstance(usage.get("metadata"), dict)
            ]
            if any(
                observation.get("exhausted") is True
                for observation in self._latest_budgets(usage_samples)
            ):
                items.append(
                    self._alert(
                        "budget_exhausted",
                        "high",
                        task,
                        "Configured model budget is exhausted",
                    )
                )
        for worker in workers:
            if worker.liveness == "stale":
                items.append({
                    "id": f"worker_stale:{worker.worker_id}",
                    "detector": "worker_stale",
                    "severity": "high",
                    "status": "open",
                    "source_timestamp": worker.last_seen_at,
                    "worker_id": worker.worker_id,
                    "message": "Worker heartbeat is stale",
                    "source": "platform_worker_heartbeat",
                })
        for job in durable_jobs:
            if job.get("alert"):
                alert = job["alert"]
                items.append({
                    "id": f"durable_job_alert:{job['id']}",
                    "detector": str(alert.get("code") or "durable_job_alert"),
                    "severity": "high" if alert.get("severity") == "error" else "medium",
                    "status": "open",
                    "source_timestamp": alert.get("created_at") or job["updated_at"],
                    "job_id": job["id"],
                    "task_id": job.get("platform_task_id"),
                    "application_id": job.get("application_id"),
                    "message": str(alert.get("message") or job.get("error") or "Durable job alert"),
                    "source": "durable_job_store",
                })
            elif job["status"] == "failed":
                items.append({
                    "id": f"durable_job_failed:{job['id']}",
                    "detector": "durable_job_failed",
                    "severity": "high",
                    "status": "open",
                    "source_timestamp": job["updated_at"],
                    "job_id": job["id"],
                    "task_id": job.get("platform_task_id"),
                    "application_id": job.get("application_id"),
                    "message": job.get("error") or "Durable job failed",
                    "source": "durable_job_store",
                })
            if job.get("lease_expired"):
                items.append({
                    "id": f"durable_job_lease_expired:{job['id']}",
                    "detector": "durable_job_lease_expired",
                    "severity": "high",
                    "status": "open",
                    "source_timestamp": job.get("lease_expires_at"),
                    "job_id": job["id"],
                    "task_id": job.get("platform_task_id"),
                    "application_id": job.get("application_id"),
                    "message": "Durable job lease expired before terminal evidence was reconciled",
                    "source": "durable_job_store",
                })
        items.sort(key=lambda item: str(item.get("source_timestamp", "")), reverse=True)
        return {
            "items": items,
            "total": len(items),
            "support": {
                "local_observation": "reported",
                "durable_job_observation": "reported",
                "production_incident": "unsupported",
                "paging_delivery": "unsupported",
            },
        }

    async def record_policy_audit(self, result: dict[str, Any]) -> None:
        await self.storage.append_event(
            "platform-governance",
            "platform_harness.policy_controls.updated",
            result,
        )

    async def _filtered_task_items(
        self,
        filters: GovernanceTaskFilters,
    ) -> list[dict[str, Any]]:
        records = await self._task_records()
        names = await self._application_names()
        items = [self._task_item(record, names) for record in records]
        items = [item for item in items if self._task_matches(item, filters)]
        items.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return items

    async def _durable_job_items(
        self,
        filters: GovernanceTaskFilters,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if filters.task_id or filters.kind or filters.owner_id or filters.model or filters.parent_task_id:
            return []
        jobs = await self.durable_jobs.list(filters.application_id, limit=limit)
        names = await self._application_names()
        items = [self._durable_job_item(job, names) for job in jobs]
        if filters.status:
            items = [item for item in items if item["status"] == filters.status]
        if filters.created_from or filters.created_to:
            items = [
                item
                for item in items
                if self._within_time(
                    item["created_at"], filters.created_from, filters.created_to
                )
            ]
        query = filters.query.strip().casefold()
        if query:
            items = [
                item
                for item in items
                if query
                in " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "id",
                        "application_id",
                        "application_name",
                        "status",
                        "trigger_kind",
                        "error",
                    )
                ).casefold()
            ]
        return items

    @staticmethod
    def _durable_job_item(
        record: DurableJobRecord,
        names: dict[str, str],
    ) -> dict[str, Any]:
        lease_expires = GovernanceService._parse_time(record.lease_expires_at or "")
        return {
            **record.model_dump(mode="json"),
            "application_name": names.get(record.application_id),
            "workflow_id": record.application_id,
            "kind": "durable_job",
            "receipt_count": int(record.checkpoint.get("receipt_count", 0)),
            "lease_expired": bool(
                record.status == "running"
                and lease_expires
                and lease_expires <= datetime.now(timezone.utc)
            ),
        }

    async def _task_records(self) -> list[PlatformTaskRecord]:
        return [
            PlatformTaskRecord.model_validate(item)
            for item in await self.storage.scan_platform_tasks()
        ]

    async def _application_names(self) -> dict[str, str]:
        return {
            str(item["id"]): str(item.get("name") or item["id"])
            for item in await self.workflow_store.list_applications()
        }

    def _task_item(self, record: PlatformTaskRecord, names: dict[str, str]) -> dict[str, Any]:
        application_id = str(
            record.metadata.get("application_id")
            or PlatformHarness._application_id_for_task(record)
            or ""
        )
        workflow_id = str(record.metadata.get("workflow_id") or application_id or "")
        model = str(record.metadata.get("model") or "")
        if not model:
            model = next(
                (
                    str(usage.metadata.get("model"))
                    for usage in record.usage
                    if usage.usage_type == "model_usage" and usage.metadata.get("model")
                ),
                "",
            )
        created = self._parse_time(record.created_at)
        end = (
            datetime.now(timezone.utc)
            if record.status in {"queued", "running", "paused"}
            else self._parse_time(record.finished_at or record.updated_at)
        )
        duration = (end - created).total_seconds() if created and end else None
        queue_delay = self._queue_delay(record)
        return {
            **record.model_dump(mode="json"),
            "application_id": application_id or None,
            "application_name": names.get(application_id) if application_id else None,
            "workflow_id": workflow_id or None,
            "model": model or None,
            "duration_seconds": duration,
            "queue_delay_seconds": queue_delay,
        }

    @staticmethod
    def _task_matches(
        item: dict[str, Any],
        filters: GovernanceTaskFilters,
        *,
        ignore_time: bool = False,
    ) -> bool:
        exact = {
            "id": filters.task_id,
            "kind": filters.kind,
            "status": filters.status,
            "owner_id": filters.owner_id,
            "application_id": filters.application_id,
            "workflow_id": filters.workflow_id,
            "model": filters.model,
            "parent_task_id": filters.parent_task_id,
        }
        if any(value is not None and str(item.get(key) or "") != value for key, value in exact.items()):
            return False
        if not ignore_time and not GovernanceService._within_time(
            str(item.get("created_at", "")), filters.created_from, filters.created_to
        ):
            return False
        query = filters.query.strip().casefold()
        if query:
            haystack = " ".join(
                str(item.get(key) or "")
                for key in (
                    "id",
                    "kind",
                    "status",
                    "owner_id",
                    "resource_id",
                    "application_id",
                    "application_name",
                    "workflow_id",
                    "model",
                    "error",
                )
            ).casefold()
            if query not in haystack:
                return False
        return True

    @staticmethod
    def _queue_delay(record: PlatformTaskRecord) -> float | None:
        lease = record.metadata.get("worker_lease")
        if not isinstance(lease, dict) or not lease.get("queue_claimed"):
            return None
        claimed = GovernanceService._parse_time(
            str(lease.get("queue_claimed_at") or lease.get("updated_at", ""))
        )
        created = GovernanceService._parse_time(record.created_at)
        return max(0.0, (claimed - created).total_seconds()) if claimed and created else None

    @staticmethod
    def _usage_support(samples: list[dict[str, Any]], field: str) -> SupportState:
        if not samples:
            return "not_recorded"
        states = [
            str(sample.get("support", {}).get(field, "not_recorded"))
            for sample in samples
            if isinstance(sample.get("support"), dict)
        ]
        if "reported" in states:
            return "reported"
        if "estimated" in states:
            return "estimated"
        if "unsupported" in states or "not_reported" in states:
            return "unsupported"
        return "not_recorded"

    @staticmethod
    def _latest_budgets(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in samples:
            task_id = str(sample.get("task_id") or "")
            budget = sample.get("budget")
            if not task_id or not isinstance(budget, dict):
                continue
            grouped[task_id].append(sample)

        observations: list[dict[str, Any]] = []
        for task_id, task_samples in grouped.items():
            latest = max(
                task_samples,
                key=lambda item: str(item.get("created_at") or ""),
            )
            latest_budget = latest["budget"]
            raw_limit = latest_budget.get("limit_usd")
            limit = float(raw_limit) if isinstance(raw_limit, (int, float)) else None
            costs = [sample.get("cost_usd") for sample in task_samples]
            known_costs = [float(value) for value in costs if isinstance(value, (int, float))]
            complete_cost = bool(costs) and len(known_costs) == len(costs)
            spent = sum(known_costs) if complete_cost else None
            if limit is None:
                support = "not_configured"
            elif not known_costs:
                support = "cost_unsupported"
            elif not complete_cost:
                support = "cost_partial"
            else:
                support = "reported_or_estimated"
            observations.append({
                "task_id": task_id,
                "owner_id": latest.get("owner_id"),
                "application_id": latest.get("application_id"),
                "model": latest.get("model"),
                "created_at": latest.get("created_at"),
                "limit_usd": limit,
                "spent_usd": spent,
                "remaining_usd": limit - spent if limit is not None and spent is not None else None,
                "exhausted": spent >= limit if limit is not None and spent is not None else None,
                "support": support,
                "sample_count": len(task_samples),
            })
        observations.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return observations

    @staticmethod
    def _percentile(values: list[float], percentile: int) -> float | None:
        if not values:
            return None
        if percentile == 50:
            return median(values)
        ordered = sorted(values)
        index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
        return ordered[index]

    @staticmethod
    def _support_for_values(items: list[dict[str, Any]], field: str) -> SupportState:
        if not items:
            return "not_recorded"
        return "reported" if any(item.get(field) is not None for item in items) else "not_recorded"

    @staticmethod
    def _within_time(value: str, start: str | None, end: str | None) -> bool:
        parsed = GovernanceService._parse_time(value)
        if parsed is None:
            return start is None and end is None
        parsed_start = GovernanceService._parse_time(start or "")
        parsed_end = GovernanceService._parse_time(end or "")
        return not ((parsed_start and parsed < parsed_start) or (parsed_end and parsed > parsed_end))

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _category_support(items: list[dict[str, Any]], category: str) -> SupportState:
        return (
            "reported"
            if any(category in item.get("artifact_categories", []) for item in items)
            else "not_recorded"
        )

    @staticmethod
    def _alert(detector: str, severity: str, task: dict[str, Any], message: str) -> dict[str, Any]:
        return {
            "id": f"{detector}:{task['id']}",
            "detector": detector,
            "severity": severity,
            "status": "open",
            "source_timestamp": task.get("updated_at"),
            "task_id": task["id"],
            "application_id": task.get("application_id"),
            "owner_id": task.get("owner_id"),
            "message": message,
            "source": "platform_harness_task",
        }


PLATFORM_EVIDENCE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "platform.task_durability",
        "claim": "Platform tasks, transitions, usage, leases, and terminal state survive process restart.",
        "scope": "Local SQLite-backed Platform Harness integration.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/platform_harness.py"),
            ("implementation", "platform/backend/src/agent_platform/storage.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("test", "tests/test_storage.py"),
            ("integration", "tests/test_workflow.py"),
        ),
    },
    {
        "capability_id": "platform.queue_worker_governance",
        "claim": "Queue leasing, worker heartbeat, stale reconciliation, and worker process controls are queryable platform mechanisms.",
        "scope": "Local and subprocess integration; no distributed production SLO claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/platform_harness.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("test", "tests/test_v02_112_e08_distributed_heartbeat_registry.py"),
            ("integration", "tests/test_v02_128_e08_distributed_queue_semantics.py"),
        ),
    },
    {
        "capability_id": "platform.policy_controls",
        "claim": "Network, secret, cancellation, lease, and resource policies are enforced and operator-visible.",
        "scope": "Local Platform Harness controls; policy edits are not persisted across restart.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/platform_harness.py"),
            ("default", "platform/backend/src/agent_platform/config.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("test", "tests/test_v02_96_e08_editable_policy_controls_api.py"),
            ("integration", "tests/test_v02_68_e08_cancellation_budget_behavior.py"),
        ),
        "gap": (
            "restart_persistence",
            "Runtime policy edits are process-local.",
            "A restart restores configured defaults rather than the latest operator edit.",
        ),
    },
    {
        "capability_id": "platform.model_usage_telemetry",
        "claim": "Provider-reported model tokens and explicitly estimated cost are durably attributable to platform tasks and budgets.",
        "scope": "Local integration telemetry with explicit unsupported fields and no billing-authority claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/models.py"),
            ("implementation", "platform/backend/src/agent_platform/platform_harness.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("test", "tests/test_v04_07_governance_console.py"),
            ("integration", "tests/test_v04_07_governance_console.py"),
        ),
        "gap": (
            "provider_field_support",
            "Some providers do not report reasoning, cache, or billed cost fields.",
            "Unsupported values remain null and cannot support a stronger claim.",
        ),
    },
    {
        "capability_id": "product.three_interface_boundary",
        "claim": "Customer Runtime, Engineer Studio, and Governance Console expose distinct routes and disclosure boundaries.",
        "scope": "Local product integration verified by source, build, and browser tests.",
        "paths": (
            ("implementation", "platform/frontend/app/runtime/[id]/page.tsx"),
            ("implementation", "platform/frontend/app/applications/[id]/page.tsx"),
            ("implementation", "platform/frontend/app/governance/page.tsx"),
            ("test", "tests/test_v04_07_governance_console.py"),
            ("integration", "tests/test_v04_07_governance_console.py"),
        ),
    },
    {
        "capability_id": "platform.evaluation_harness_profiles",
        "claim": "H0-H5 evaluation profiles generate capability-scoped plans, execute only eligible work, persist exact outcomes, and cap claims at the weakest profile, environment, contract, and case evidence.",
        "scope": "Local H3 integration across authenticated APIs, durable records, Platform Harness tasks, and Engineer Studio; no configured H4 target or customer production H5 claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/evaluation_harness.py"),
            ("implementation", "platform/backend/src/agent_platform/storage.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("implementation", "platform/frontend/app/applications/[id]/evaluation-harness-panel.tsx"),
            ("test", "tests/test_v04_08_evaluation_harness.py"),
            ("integration", "tests/test_v04_08_evaluation_harness.py"),
        ),
        "gap": (
            "live_and_production_evidence",
            "No eligible configured H4 target or customer production H5 telemetry is part of the local evidence package.",
            "The platform capability claim remains local H3 even though H4 and H5 profiles are selectable and explicitly blocked by default.",
        ),
    },
    {
        "capability_id": "platform.durable_job_substrate",
        "claim": "Scheduled workflow jobs have idempotent enqueue, persisted attempts, leases with fencing, checkpoints, retry, cancellation, resume, restart reconciliation, and operator-visible records.",
        "scope": "Local SQLite-backed H3 integration with a scheduler-local worker; no production SLO or distributed failover claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/durable_jobs.py"),
            ("implementation", "platform/backend/src/agent_platform/scheduler.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("test", "tests/test_v04_09_durable_daily_collection.py"),
            ("integration", "tests/test_v04_09_durable_daily_collection.py"),
        ),
        "gap": (
            "production_reliability",
            "The durable-job substrate has no production SLO, distributed worker failover, or external paging evidence.",
            "Claims remain capped at controlled local H3 integration.",
        ),
    },
    {
        "capability_id": "platform.controlled_web_collection",
        "claim": "A durable workflow can collect declared allowlisted HTTP sources, enforce robots and size boundaries, preserve provenance receipts, detect content changes, and render a cited digest.",
        "scope": "Controlled local HTTP fixtures and declared sources only; no arbitrary-site permission or external notification claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/web_collection.py"),
            ("implementation", "platform/backend/src/agent_platform/blocks.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("test", "tests/test_v04_09_durable_daily_collection.py"),
            ("integration", "tests/test_v04_09_durable_daily_collection.py"),
        ),
        "gap": (
            "external_access_and_delivery",
            "Customer credentials, arbitrary websites, production anti-abuse controls, and external notification delivery are not established.",
            "Source access stays deny-by-default and delivery remains inside Lilies Customer Runtime.",
        ),
    },
    {
        "capability_id": "platform.connector_embedding_sdk",
        "claim": "A versioned Connector contract can map a signed external tenant and subject into an editable Lilies workflow and execute schema-validated operations through a controlled test deployment profile.",
        "scope": "Signed ingress and controlled mock/test-tenant H3 integration; no customer-live identity, private deployment, or production observation claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/connector_sdk.py"),
            ("implementation", "platform/backend/src/agent_platform/blocks.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("implementation", "platform/frontend/app/connector-operations-panel.tsx"),
            ("test", "tests/test_v04_10_connector_embedding.py"),
            ("integration", "tests/test_v04_10_connector_embedding.py"),
        ),
        "gap": (
            "customer_live_and_production_environment",
            "No eligible customer-live H4 target, private customer deployment, or H5 production telemetry is attached.",
            "The strongest claim remains controlled test-tenant H3 integration.",
        ),
    },
    {
        "capability_id": "platform.governed_connector_writeback",
        "claim": "Connector mutations are guarded by tenant roles, exact-payload preauthorization, policy revisions, emergency stop, idempotent durable receipts, ordered callbacks, and explicit compensation.",
        "scope": "Controlled customer HTTP fixture and local governance H3 evidence; no production writeback reliability, SLO, or disaster-recovery claim.",
        "paths": (
            ("implementation", "platform/backend/src/agent_platform/connector_sdk.py"),
            ("implementation", "platform/backend/src/agent_platform/governance.py"),
            ("api", "platform/backend/src/agent_platform/api.py"),
            ("implementation", "platform/frontend/app/governance/page.tsx"),
            ("test", "tests/test_v04_10_connector_embedding.py"),
            ("integration", "tests/test_v04_10_connector_embedding.py"),
        ),
        "gap": (
            "production_writeback_assurance",
            "No customer production load, availability, callback-delivery, or recovery observation is present.",
            "Receipts and exercises prove bounded control behavior only, not a production SLO.",
        ),
    },
)


def ensure_platform_capability_evidence(
    registry: CapabilityEvidenceRegistry,
    *,
    evidence_root: Path,
) -> list[str]:
    registered: list[str] = []
    for spec in PLATFORM_EVIDENCE_SPECS:
        artifacts = [
            EvidenceArtifact(category=category, path=path, method="direct")
            for category, path in spec["paths"]
            if (evidence_root / path).is_file()
        ]
        missing = [path for _, path in spec["paths"] if not (evidence_root / path).is_file()]
        gaps = []
        if spec.get("gap"):
            field, reason, impact = spec["gap"]
            gaps.append(EvidenceGap(field=field, reason=reason, impact=impact))
        if missing:
            gaps.append(EvidenceGap(
                field="packaged_artifacts",
                reason="Evidence artifacts are absent from this runtime package.",
                impact="The local claim is capped at the strongest remaining artifact set.",
                recheck_trigger="Run from a source checkout containing the listed artifacts.",
            ))
        requested = (
            VerificationStatus.integration_verified
            if {item.category for item in artifacts}.issuperset({"implementation", "api", "test", "integration"})
            else VerificationStatus.component_verified
            if {item.category for item in artifacts}.issuperset({"implementation", "test"})
            else VerificationStatus.static_verified
            if any(item.category == "implementation" for item in artifacts)
            else VerificationStatus.design_only
        )
        record = registry.register(CapabilityEvidenceCreateRequest(
            capability_id=spec["capability_id"],
            claim=spec["claim"],
            claim_scope=spec["scope"],
            requested_status=requested,
            environment=EvidenceEnvironment.sandbox,
            artifacts=artifacts,
            gaps=gaps,
        ))
        registered.append(record.record_id)
    return registered
