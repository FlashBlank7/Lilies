from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .models import utc_now
from .storage import Storage


MemoryOperation = Literal["create", "read", "update", "revoke", "expire"]
MemoryStatus = Literal["active", "revoked", "expired"]
RetentionClass = Literal["session", "project", "user_renewable"]


class GovernedMemoryViolation(RuntimeError):
    pass


class GovernedMemoryPermission(BaseModel):
    actor_id: str = Field(min_length=1, max_length=160)
    owner_id: str = Field(min_length=1, max_length=160)
    scope_id: str = Field(min_length=1, max_length=240)
    purpose: str = Field(min_length=1, max_length=1000)
    allowed_operations: list[MemoryOperation] = Field(default_factory=list)
    expires_at: str | None = None

    @field_validator("allowed_operations")
    @classmethod
    def require_operations(cls, value: list[MemoryOperation]) -> list[MemoryOperation]:
        unique = list(dict.fromkeys(value))
        if not unique:
            raise ValueError("permission must include at least one allowed operation")
        return unique


class GovernedMemorySource(BaseModel):
    source_type: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=500)
    captured_at: str = Field(default_factory=utc_now)
    evidence_text: str = Field(default="", max_length=20_000)
    evidence_hash: str = Field(default="", max_length=160)


class GovernedMemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: f"mem_{uuid4().hex}")
    owner_id: str
    scope_id: str
    content: str = Field(min_length=1, max_length=20_000)
    source: GovernedMemorySource
    retention_class: RetentionClass
    expires_at: str
    status: MemoryStatus = "active"
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    revoked_at: str | None = None
    revoked_reason: str = ""


class GovernedMemorySurface:
    """Permission-scoped, auditable memory surface.

    The surface is deliberately narrower than general assistant memory: every
    operation requires a scope-bound permission and writes an append-only audit
    event through the existing Storage event stream.
    """

    BANNED_SOURCE_TYPES = {"filesystem", "filesystem_index", "background_activity", "arbitrary_file"}

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def create(
        self,
        *,
        permission: GovernedMemoryPermission,
        content: str,
        source: GovernedMemorySource,
        retention_class: RetentionClass,
        reason: str,
        expires_at: str | None = None,
    ) -> GovernedMemoryItem:
        self._authorize(permission, "create", owner_id=permission.owner_id, scope_id=permission.scope_id, reason=reason)
        self._validate_source(source)
        resolved_expires_at = expires_at or self._default_expires_at(retention_class)
        self._validate_retention(retention_class, resolved_expires_at)
        item = GovernedMemoryItem(
            owner_id=permission.owner_id,
            scope_id=permission.scope_id,
            content=content,
            source=self._source_with_hash(source),
            retention_class=retention_class,
            expires_at=resolved_expires_at,
        )
        saved = await self.storage.save_governed_memory_item(item.model_dump(mode="json"))
        item = GovernedMemoryItem.model_validate(saved)
        await self._audit("create", item=item, permission=permission, reason=reason)
        return item

    async def read(
        self,
        memory_id: str,
        *,
        permission: GovernedMemoryPermission,
        reason: str,
    ) -> GovernedMemoryItem:
        item = await self._load(memory_id)
        self._authorize(permission, "read", owner_id=item.owner_id, scope_id=item.scope_id, reason=reason)
        self._ensure_retrievable(item)
        await self._audit("read", item=item, permission=permission, reason=reason)
        return item

    async def update(
        self,
        memory_id: str,
        *,
        permission: GovernedMemoryPermission,
        content: str,
        source: GovernedMemorySource,
        reason: str,
    ) -> GovernedMemoryItem:
        item = await self._load(memory_id)
        self._authorize(permission, "update", owner_id=item.owner_id, scope_id=item.scope_id, reason=reason)
        self._ensure_retrievable(item)
        self._validate_source(source)
        item.content = content
        item.source = self._source_with_hash(source)
        item.updated_at = utc_now()
        saved = await self.storage.save_governed_memory_item(item.model_dump(mode="json"))
        item = GovernedMemoryItem.model_validate(saved)
        await self._audit("update", item=item, permission=permission, reason=reason)
        return item

    async def revoke(
        self,
        memory_id: str,
        *,
        permission: GovernedMemoryPermission,
        reason: str,
    ) -> GovernedMemoryItem:
        item = await self._load(memory_id)
        self._authorize(permission, "revoke", owner_id=item.owner_id, scope_id=item.scope_id, reason=reason)
        if item.status != "revoked":
            now = utc_now()
            item.status = "revoked"
            item.revoked_at = now
            item.revoked_reason = reason
            item.updated_at = now
            saved = await self.storage.save_governed_memory_item(item.model_dump(mode="json"))
            item = GovernedMemoryItem.model_validate(saved)
        await self._audit("revoke", item=item, permission=permission, reason=reason)
        return item

    async def expire_due(
        self,
        *,
        owner_id: str,
        permission: GovernedMemoryPermission,
        reason: str,
        now: str | None = None,
    ) -> list[GovernedMemoryItem]:
        self._authorize(permission, "expire", owner_id=owner_id, scope_id=permission.scope_id, reason=reason)
        now_dt = self._parse_time(now or utc_now())
        active = await self.storage.list_governed_memory_items(
            owner_id=owner_id,
            scope_id=permission.scope_id,
            statuses={"active"},
            limit=500,
        )
        expired: list[GovernedMemoryItem] = []
        for raw in active:
            item = GovernedMemoryItem.model_validate(raw)
            if self._parse_time(item.expires_at) <= now_dt:
                item.status = "expired"
                item.updated_at = utc_now()
                saved = await self.storage.save_governed_memory_item(item.model_dump(mode="json"))
                item = GovernedMemoryItem.model_validate(saved)
                await self._audit("expire", item=item, permission=permission, reason=reason)
                expired.append(item)
        return expired

    async def list_active(
        self,
        *,
        owner_id: str,
        scope_id: str,
        permission: GovernedMemoryPermission,
        reason: str,
        limit: int = 100,
    ) -> list[GovernedMemoryItem]:
        self._authorize(permission, "read", owner_id=owner_id, scope_id=scope_id, reason=reason)
        raw_items = await self.storage.list_governed_memory_items(
            owner_id=owner_id,
            scope_id=scope_id,
            statuses={"active"},
            limit=limit,
        )
        items = [GovernedMemoryItem.model_validate(raw) for raw in raw_items]
        return [item for item in items if self._parse_time(item.expires_at) > self._parse_time(utc_now())]

    async def list_for_operator(
        self,
        *,
        owner_id: str,
        scope_id: str,
        permission: GovernedMemoryPermission,
        reason: str,
        status_filter: MemoryStatus | Literal["all"] = "active",
        limit: int = 100,
    ) -> list[GovernedMemoryItem]:
        self._authorize(permission, "read", owner_id=owner_id, scope_id=scope_id, reason=reason)
        statuses: set[str] | None = None
        if status_filter != "all":
            statuses = {status_filter}
        raw_items = await self.storage.list_governed_memory_items(
            owner_id=owner_id,
            scope_id=scope_id,
            statuses=statuses,
            limit=limit,
        )
        items = [GovernedMemoryItem.model_validate(raw) for raw in raw_items]
        if status_filter == "active":
            now = self._parse_time(utc_now())
            return [item for item in items if self._parse_time(item.expires_at) > now]
        return items

    async def _load(self, memory_id: str) -> GovernedMemoryItem:
        try:
            raw = await self.storage.get_governed_memory_item(memory_id)
        except KeyError as error:
            raise GovernedMemoryViolation(f"governed memory item not found: {memory_id}") from error
        return GovernedMemoryItem.model_validate(raw)

    def _authorize(
        self,
        permission: GovernedMemoryPermission,
        operation: MemoryOperation,
        *,
        owner_id: str,
        scope_id: str,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise GovernedMemoryViolation("governed memory operation requires a reason")
        if permission.owner_id != owner_id or permission.scope_id != scope_id:
            raise GovernedMemoryViolation("permission scope does not match governed memory scope")
        if operation not in permission.allowed_operations:
            raise GovernedMemoryViolation(f"permission does not allow governed memory operation: {operation}")
        if permission.expires_at and self._parse_time(permission.expires_at) <= self._parse_time(utc_now()):
            raise GovernedMemoryViolation("governed memory permission has expired")

    def _ensure_retrievable(self, item: GovernedMemoryItem) -> None:
        if item.status == "revoked":
            raise GovernedMemoryViolation("governed memory item is revoked")
        if item.status == "expired" or self._parse_time(item.expires_at) <= self._parse_time(utc_now()):
            raise GovernedMemoryViolation("governed memory item is expired")

    def _validate_source(self, source: GovernedMemorySource) -> None:
        source_type = source.source_type.strip().lower()
        if source_type in self.BANNED_SOURCE_TYPES:
            raise GovernedMemoryViolation("unrestricted filesystem or background memory source is not allowed")
        if source.source_id in {"*", "**", "/"}:
            raise GovernedMemoryViolation("wildcard filesystem memory source is not allowed")
        path = PurePosixPath(source.source_id)
        if path.is_absolute() or ".." in path.parts:
            raise GovernedMemoryViolation("arbitrary filesystem path memory source is not allowed")

    def _validate_retention(self, retention_class: RetentionClass, expires_at: str) -> None:
        if retention_class not in {"session", "project", "user_renewable"}:
            raise GovernedMemoryViolation(f"unsupported retention class: {retention_class}")
        if self._parse_time(expires_at) <= self._parse_time(utc_now()):
            raise GovernedMemoryViolation("governed memory expires_at must be in the future")

    def _default_expires_at(self, retention_class: RetentionClass) -> str:
        # 认不出来的类别在这里就拦住，别让它变成一个裸 KeyError。
        #
        # 顺序问题：create 里是先算默认期限、再 _validate_retention，
        # 于是"unsupported retention class"那句话**永远走不到**——
        # 没给 expires_at 的调用会先在这个字典查找上炸出 KeyError('forever')。
        # 一道够不着的闸比没有闸更坏：它看着像在把关。
        # （HTTP 那条路有 Literal 挡着，所以这是内部调用方才踩得到的坑。）
        days = {"session": 1, "project": 30, "user_renewable": 90}.get(retention_class)
        if days is None:
            raise GovernedMemoryViolation(
                f"unsupported retention class: {retention_class}")
        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    def _source_with_hash(self, source: GovernedMemorySource) -> GovernedMemorySource:
        if source.evidence_hash:
            return source
        digest_input = source.evidence_text or source.source_id
        return source.model_copy(update={"evidence_hash": hashlib.sha256(digest_input.encode("utf-8")).hexdigest()})

    async def _audit(
        self,
        operation: MemoryOperation,
        *,
        item: GovernedMemoryItem,
        permission: GovernedMemoryPermission,
        reason: str,
    ) -> None:
        await self.storage.append_event(
            self.audit_stream_id(item.owner_id, item.scope_id),
            f"governed_memory.{operation}",
            {
                "version": "v0.2.137",
                "operation": operation,
                "actor_id": permission.actor_id,
                "owner_id": item.owner_id,
                "scope_id": item.scope_id,
                "memory_id": item.id,
                "source": item.source.model_dump(mode="json"),
                "reason": reason,
                "timestamp": utc_now(),
                "status": item.status,
            },
        )

    @staticmethod
    def audit_stream_id(owner_id: str, scope_id: str) -> str:
        return f"governed-memory:{owner_id}:{scope_id}"

    def _parse_time(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
