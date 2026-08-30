"""一次任务最多写 N 次——这个计数器守的是**对外部系统的真实写操作**。

connector_sdk 自己的注释写着这道闸为什么在服务端而不在运行器里：
"separate runs, processes, and restarts share one exact N-write ceiling"。
转账、下单、发消息都走这条路，多写一次就是真的多做一次。

变异验证（2026-08-30，全量 2712 条）：六个变异里五个有人盯着
（写满了放行、边界翻一格、计数不加、策略变了照写、单次载荷上限），
漏一个——**首次登记时 `max_write_count < 1` 的拒绝去掉，一条都没红**。
后果是"授权 0 次写"被登记成一条 write_count=1 的预算行：
一个**明确不准写**的授权，换来了一次真实写操作。

这条路上只用到 self 的三个无状态方法（canonical_json / payload_hash /
_assignment_budget_policy），所以直接跑真判据，
只有那张预算表是现建的内存库——不搭"起整个连接器服务"的塔。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_platform.connector_sdk import (
    ConnectorDenied,
    ConnectorExecutionRequest,
    ConnectorService,
)

BUDGET_TABLE = """
CREATE TABLE connector_assignment_budgets (
  assignment_id TEXT PRIMARY KEY,
  policy_digest TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  max_write_count INTEGER NOT NULL,
  max_payload_bytes INTEGER NOT NULL,
  write_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(BUDGET_TABLE)
    yield connection
    connection.close()


def _request(**extra) -> ConnectorExecutionRequest:
    fields = {
        "connector_id": "crm",
        "connector_version": 1,
        "tenant_id": "tenant-1",
        "actor_id": "actor-1",
        "actor_roles": ["operator"],
        "profile_id": "profile-1",
        "operation_id": "update_case",
        "payload": {"case_id": "case-1"},
        "idempotency_key": "key-1",
        "assignment_id": "assignment-1",
        "assignment_max_write_count": 1,
        "assignment_max_payload_bytes": 10_000,
        "allowed_network_hosts": ["crm.example.com"],
        "allowed_compensation_operations": [],
    }
    fields.update(extra)
    return ConnectorExecutionRequest(**fields)


def _spend(conn, request, payload=None) -> None:
    service = ConnectorService.__new__(ConnectorService)   # 这条路只用无状态方法
    ConnectorService._consume_assignment_budget_sync(
        service, conn, request, payload if payload is not None else {"a": 1})


def _written(conn, assignment_id="assignment-1") -> int:
    row = conn.execute(
        "SELECT write_count FROM connector_assignment_budgets WHERE assignment_id=?",
        (assignment_id,)).fetchone()
    return -1 if row is None else int(row["write_count"])


class TestAZeroWriteAssignmentCannotWrite:
    """**这一族就是漏网的那个。**"""

    def test_it_is_refused(self, conn):
        with pytest.raises(ConnectorDenied, match="exhausted"):
            _spend(conn, _request(assignment_max_write_count=0))

    def test_nothing_is_recorded_when_it_is_refused(self, conn):
        """拒了就不能留下预算行——留下的话下一次调用会当成"已经登记过"。"""
        with pytest.raises(ConnectorDenied):
            _spend(conn, _request(assignment_max_write_count=0))
        assert _written(conn) == -1, "被拒了却还是登记了一行"

    def test_a_negative_budget_never_reaches_this_gate(self):
        """负数由**模型层**挡（ge=0），根本走不到这个函数。

        写这条是因为我本来打算在这儿断言"负数也拒"——跑出来是
        pydantic 的 ValidationError。闸在哪儿就在哪儿测，
        不然这条会变成一句关于别人职责的空话。
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _request(assignment_max_write_count=-1)


class TestTheCeilingHoldsAcrossCalls:
    """反向那一批：闸不能宽到"一次都不让写"——那样连接器整个不能用。"""

    def test_the_first_write_of_a_one_write_budget_goes_through(self, conn):
        _spend(conn, _request(assignment_max_write_count=1))
        assert _written(conn) == 1

    def test_the_second_write_is_refused(self, conn):
        request = _request(assignment_max_write_count=1)
        _spend(conn, request)
        with pytest.raises(ConnectorDenied, match="exhausted"):
            _spend(conn, request)
        assert _written(conn) == 1, "被拒的那次不该把计数推上去"

    def test_a_three_write_budget_allows_exactly_three(self, conn):
        request = _request(assignment_max_write_count=3)
        for _ in range(3):
            _spend(conn, request)
        assert _written(conn) == 3
        with pytest.raises(ConnectorDenied, match="exhausted"):
            _spend(conn, request)


class TestTheOtherTwoGuardsInTheSameSpot:
    """这两条本来就有人盯着（同批变异被抓住），一起写在这儿留个形状。"""

    def test_an_oversized_payload_is_refused(self, conn):
        with pytest.raises(ConnectorDenied, match="byte limit"):
            _spend(conn, _request(assignment_max_payload_bytes=5),
                   payload={"big": "x" * 500})

    def test_changing_the_policy_mid_assignment_is_refused(self, conn):
        """同一个 assignment 换一套预算接着用——那等于自己给自己加额度。"""
        _spend(conn, _request(assignment_max_write_count=3))
        with pytest.raises(ConnectorDenied, match="policy changed"):
            _spend(conn, _request(assignment_max_write_count=9))

    def test_an_incomplete_budget_is_refused(self, conn):
        with pytest.raises(ConnectorDenied, match="incomplete"):
            _spend(conn, _request(assignment_max_write_count=None))

    def test_no_assignment_means_this_gate_does_not_apply(self, conn):
        """没有 assignment_id 就不是受管任务，这道闸不该拦它。"""
        _spend(conn, _request(assignment_id=""))
        assert _written(conn) == -1
