"""受管记忆的十二道闸：一条测试都没有过。

governed_memory 321 行，被 api 和 workflow_runtime 用着（两个带令牌的接口），
此前零测试。它的整个存在理由就是"记忆不能随便读写"：
每次操作都要一份限定范围的授权、都要写一条审计、都有留存期限。
十二处 `raise GovernedMemoryViolation` 就是这些规矩的全部落点。

闸的测试要从坏那一侧进。这个文件逐条走一遍：
授权范围对不上、操作不在许可里、许可过期、没写理由、
记忆被撤销/过期、来源是文件系统或通配、留存期已过。
每条都配一条正向，否则"什么都拒"也能全绿。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_platform.governed_memory import (
    GovernedMemoryPermission,
    GovernedMemorySource,
    GovernedMemorySurface,
    GovernedMemoryViolation,
)
from agent_platform.storage import Storage

OWNER, SCOPE = "owner-a", "scope-a"


def _when(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _permission(**extra) -> GovernedMemoryPermission:
    fields = {
        "actor_id": "actor-1", "owner_id": OWNER, "scope_id": SCOPE,
        "purpose": "记住业主说过的口径",
        "allowed_operations": ["create", "read", "update", "revoke"],
    }
    fields.update(extra)
    return GovernedMemoryPermission(**fields)


def _source(**extra) -> GovernedMemorySource:
    fields = {"source_type": "owner_message", "source_id": "msg/42",
              "evidence_text": "业主说：只统计出现两次以上的词"}
    fields.update(extra)
    return GovernedMemorySource(**fields)


@pytest.fixture
def surface(tmp_path: Path) -> GovernedMemorySurface:
    storage = Storage(tmp_path / "d")
    asyncio.run(storage.initialize())
    return GovernedMemorySurface(storage=storage)


def _create(surface, *, permission=None, source=None, reason="业主明确要求记住",
            retention_class="project", **extra):
    return asyncio.run(surface.create(
        permission=permission or _permission(),
        content="只统计出现两次以上的词",
        source=source or _source(),
        retention_class=retention_class,
        reason=reason,
        **extra,
    ))


class TestTheHappyPathWorks:
    """先钉正向。少了它，下面每条"被拒"都可能是因为整个面根本不工作。"""

    def test_a_scoped_permission_can_write_and_read_back(self, surface):
        item = _create(surface)
        assert item.content == "只统计出现两次以上的词"
        back = asyncio.run(surface.read(item.id, permission=_permission(),
                                        reason="回答业主的问题要用"))
        assert back.id == item.id
        assert back.status == "active"

    def test_every_operation_leaves_an_audit_trail(self, surface):
        """审计是这个面的另一半承诺——没有审计，授权检查也就没人能复核。"""
        item = _create(surface)
        asyncio.run(surface.read(item.id, permission=_permission(), reason="查一下"))
        events = asyncio.run(surface.storage.list_events(
            GovernedMemorySurface.audit_stream_id(item.owner_id, item.scope_id)))
        kinds = [e.type for e in events]
        assert kinds, "一条审计都没写"
        assert any("create" in k for k in kinds) and any("read" in k for k in kinds), kinds


class TestThePermissionMustActuallyMatch:
    def test_another_owners_permission_is_refused(self, surface):
        item = _create(surface)
        with pytest.raises(GovernedMemoryViolation, match="scope"):
            asyncio.run(surface.read(item.id, permission=_permission(owner_id="owner-b"),
                                     reason="我就看看"))

    def test_another_scopes_permission_is_refused(self, surface):
        item = _create(surface)
        with pytest.raises(GovernedMemoryViolation, match="scope"):
            asyncio.run(surface.read(item.id, permission=_permission(scope_id="scope-b"),
                                     reason="我就看看"))

    def test_an_operation_outside_the_permission_is_refused(self, surface):
        """只给了读，就不能写——许可是按操作列的。"""
        item = _create(surface)
        read_only = _permission(allowed_operations=["read"])
        with pytest.raises(GovernedMemoryViolation, match="does not allow"):
            asyncio.run(surface.update(item.id, permission=read_only,
                                       content="偷改的内容", source=_source(),
                                       reason="改一下"))

    def test_an_expired_permission_is_refused(self, surface):
        item = _create(surface)
        stale = _permission(expires_at=_when(-1))
        with pytest.raises(GovernedMemoryViolation, match="expired"):
            asyncio.run(surface.read(item.id, permission=stale, reason="看看"))

    def test_a_permission_that_has_not_expired_still_works(self, surface):
        """反向：给了未来的期限不能被当成过期。"""
        item = _create(surface, permission=_permission(expires_at=_when(3)))
        assert asyncio.run(surface.read(item.id, permission=_permission(expires_at=_when(3)),
                                        reason="看看")).id == item.id

    @pytest.mark.parametrize("reason", ["", "   ", "\n"])
    def test_an_operation_without_a_reason_is_refused(self, surface, reason):
        """理由是审计的核心内容——空理由的审计等于没有审计。"""
        with pytest.raises(GovernedMemoryViolation, match="reason"):
            _create(surface, reason=reason)


class TestARevokedOrExpiredItemCannotBeRead:
    def test_a_revoked_item_is_refused(self, surface):
        item = _create(surface)
        asyncio.run(surface.revoke(item.id, permission=_permission(),
                                   reason="业主改主意了"))
        with pytest.raises(GovernedMemoryViolation, match="revoked"):
            asyncio.run(surface.read(item.id, permission=_permission(), reason="再看看"))

    def test_an_item_past_its_expiry_is_refused(self, surface):
        """到期就读不到——留存期限得真的管事，不能只是个字段。"""
        item = _create(surface, expires_at=_when(1))
        stored = asyncio.run(surface.storage.get_governed_memory_item(item.id))
        stored["expires_at"] = _when(-1)
        asyncio.run(surface.storage.save_governed_memory_item(stored))
        with pytest.raises(GovernedMemoryViolation, match="expired"):
            asyncio.run(surface.read(item.id, permission=_permission(), reason="看看"))

    def test_an_unexpired_item_reads_fine(self, surface):
        item = _create(surface, expires_at=_when(30))
        assert asyncio.run(surface.read(item.id, permission=_permission(),
                                        reason="看看")).id == item.id


class TestTheMemorySourceIsRestricted:
    """这个面刻意比通用记忆窄：不许把"整个文件系统"当记忆来源。"""

    @pytest.mark.parametrize("source_type", [
        "filesystem", "filesystem_index", "background_activity", "arbitrary_file",
        "FileSystem", "  filesystem  ",
    ])
    def test_a_banned_source_type_is_refused(self, surface, source_type):
        """大小写和空白不能绕过去——实现里 strip().lower() 了，钉住它。"""
        with pytest.raises(GovernedMemoryViolation, match="not allowed"):
            _create(surface, source=_source(source_type=source_type))

    @pytest.mark.parametrize("source_id", ["*", "**", "/"])
    def test_a_wildcard_source_is_refused(self, surface, source_id):
        with pytest.raises(GovernedMemoryViolation, match="wildcard"):
            _create(surface, source=_source(source_id=source_id))

    @pytest.mark.parametrize("source_id", [
        "/etc/passwd", "/home/zhaoyang/.env", "../../secrets", "a/../../b",
    ])
    def test_an_absolute_or_climbing_path_is_refused(self, surface, source_id):
        with pytest.raises(GovernedMemoryViolation, match="arbitrary filesystem path"):
            _create(surface, source=_source(source_id=source_id))

    def test_an_ordinary_source_is_accepted(self, surface):
        """反向那一条：正常来源不能被误伤，否则这个面没法用。"""
        assert _create(surface, source=_source(source_type="owner_message",
                                               source_id="conversation/7")).id


class TestRetentionMustBeSane:
    def test_an_expiry_in_the_past_is_refused(self, surface):
        with pytest.raises(GovernedMemoryViolation, match="must be in the future"):
            _create(surface, expires_at=_when(-1))

    @pytest.mark.parametrize("retention_class, at_least_days", [
        ("session", 1), ("project", 30), ("user_renewable", 90),
    ])
    def test_the_default_expiry_matches_the_retention_class(
            self, surface, retention_class, at_least_days):
        """默认期限要跟留存类别对上——三档写成一样的话，分档就没有意义。

        比的是绝对天数，不是"从常量推出来的"：常量被改时那种断言照样绿。
        """
        item = _create(surface, retention_class=retention_class)
        days = (datetime.fromisoformat(item.expires_at)
                - datetime.now(timezone.utc)).total_seconds() / 86400
        assert at_least_days - 0.1 <= days <= at_least_days + 0.1, days

    def test_an_unknown_retention_class_is_refused(self, surface):
        """认不出来的类别要说人话，不能扔一个裸 KeyError。

        原来 create 是先算默认期限、再 _validate_retention，于是那句
        "unsupported retention class" **永远走不到**：没给 expires_at 的调用
        先在字典查找上炸出 KeyError('forever')。够不着的闸比没有闸更坏。
        （HTTP 那条路有 Literal 挡着，这是内部调用方才踩得到的。）
        """
        with pytest.raises(GovernedMemoryViolation, match="unsupported retention class"):
            _create(surface, retention_class="forever")

    def test_it_is_refused_even_when_an_expiry_is_given(self, surface):
        """带了期限那条路走的是 _validate_retention，也得拒。"""
        with pytest.raises(GovernedMemoryViolation, match="unsupported retention class"):
            _create(surface, retention_class="forever", expires_at=_when(5))
