"""撤销了、过期了，还能不能用——这两道闸此前零测试。

变异验证（2026-08-30，全量 2287 条）：把这两句拒绝各自去掉，
**一条测试都没红**：

    if authorization.revoked:                 ← 去掉，2287 全绿
        raise ConnectorDenied("...revoked or exhausted")
    if self._parse_time(...expires_at) <= now:  ← 去掉，2287 全绿
        raise ConnectorDenied("...expired")

同一批扫的另外两个（范围比对、payload_hash 绑定）都被抓住了，
所以不是"这段没被跑到"，是**这两条判据没有任何人在盯**。

它们守的是什么：连接器授权是"准许对外部系统做一次真实写操作"的凭证。
撤销是出事之后唯一的止血手段（发现凭证泄漏、发现智能体跑偏，
去把它撤了）；过期是没人来止血时的兜底。两条都松掉的话，
一张签发过的授权就是永久有效的——**撤销按钮按下去没有反应，
而按的人以为有**。

这里直接跑 `_authorization_for_request_sync`：它在这条路上只用到
`self._parse_time`，所以不搭那座「建整个应用」的塔
（本周已经吃过一次搭塔的亏，见 put-the-fix-where-it-is-testable）。
用的是真的模型对象、真的判据，只有取行那一下是假的。

每条都配反向（还没过期、没撤销的要放行），否则"一律拒"也能全绿——
而那会让所有连接器写操作都不能用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_platform.connector_sdk import (
    ConnectorAuthorization,
    ConnectorDenied,
    ConnectorDomainPolicy,
    ConnectorExecutionRequest,
    ConnectorService,
)

PAYLOAD_HASH = "sha256-of-the-exact-payload"


def _at(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _authorization(**extra) -> ConnectorAuthorization:
    fields = {
        "id": "auth-1",
        "connector_id": "crm",
        "connector_version": 1,
        "tenant_id": "tenant-1",
        "actor_id": "actor-1",
        "profile_id": "profile-1",
        "operation_id": "update_case",
        "payload_hash": PAYLOAD_HASH,
        "policy_revision": 7,
        "expires_at": _at(600),
    }
    fields.update(extra)
    return ConnectorAuthorization(**fields)


def _request(authorization: ConnectorAuthorization) -> ConnectorExecutionRequest:
    """一个和授权完全对得上的请求——所以任何拒绝都只可能来自被测的那道闸。"""
    return ConnectorExecutionRequest(
        connector_id=authorization.connector_id,
        connector_version=authorization.connector_version,
        tenant_id=authorization.tenant_id,
        actor_id=authorization.actor_id,
        actor_roles=["operator"],
        profile_id=authorization.profile_id,
        operation_id=authorization.operation_id,
        payload={"case_id": "case-1"},
        idempotency_key="key-1",
        authorization_id=authorization.id,
    )


def _policy(authorization: ConnectorAuthorization) -> ConnectorDomainPolicy:
    return ConnectorDomainPolicy(
        connector_id=authorization.connector_id,
        connector_version=authorization.connector_version,
        tenant_id=authorization.tenant_id,
        domain="crm",
        allowed_profiles=[authorization.profile_id],
        allowed_operations=[authorization.operation_id],
        revision=authorization.policy_revision,
    )


class _Row(dict):
    pass


class _Conn:
    """只答一句 SELECT。存在与否由 record_json 是不是 None 决定。"""

    def __init__(self, record_json: str | None) -> None:
        self._row = _Row(record_json=record_json) if record_json is not None else None

    def execute(self, *_args) -> "_Conn":
        return self

    def fetchone(self):
        return self._row


def _check(authorization: ConnectorAuthorization,
           *, stored: ConnectorAuthorization | None = None,
           payload_hash: str = PAYLOAD_HASH) -> ConnectorAuthorization:
    """跑真判据。stored 缺省就是 authorization 本身。"""
    record = stored if stored is not None else authorization
    service = ConnectorService.__new__(ConnectorService)   # 这条路只用 _parse_time
    return ConnectorService._authorization_for_request_sync(
        service,
        _Conn(record.model_dump_json()),
        _request(authorization),
        _policy(authorization),
        payload_hash,
        record.operation_kind,
    )


class TestAValidAuthorizationStillWorks:
    """反向那一批放最前面：没有它，把两道闸写成"一律拒"也能全绿。"""

    def test_it_is_returned(self):
        authorization = _authorization()
        assert _check(authorization).id == "auth-1"

    def test_not_revoked_is_the_default(self):
        assert _authorization().revoked is False

    def test_an_authorization_expiring_later_is_fine(self):
        assert _check(_authorization(expires_at=_at(3600))).id == "auth-1"


class TestRevocationActuallyStopsIt:
    def test_a_revoked_authorization_is_refused(self):
        """撤销是出事后唯一的止血手段。这一句去掉，2287 条测试全绿。"""
        with pytest.raises(ConnectorDenied, match="revoked"):
            _check(_authorization(revoked=True))

    def test_revocation_beats_an_otherwise_perfect_request(self):
        """范围全对、还没过期——**只**因为被撤销而拒。
        不然这条可能是被别的闸拦下的，测的就不是撤销。"""
        authorization = _authorization(revoked=True, expires_at=_at(3600))
        with pytest.raises(ConnectorDenied) as denied:
            _check(authorization)
        assert "expired" not in str(denied.value)

    def test_the_reason_says_revoked_not_something_vague(self):
        with pytest.raises(ConnectorDenied, match="revoked or exhausted"):
            _check(_authorization(revoked=True))


class TestExpiryActuallyStopsIt:
    def test_an_expired_authorization_is_refused(self):
        with pytest.raises(ConnectorDenied, match="expired"):
            _check(_authorization(expires_at=_at(-1)))

    def test_long_expired_is_still_refused(self):
        with pytest.raises(ConnectorDenied, match="expired"):
            _check(_authorization(expires_at=_at(-86_400)))

    def test_the_boundary_is_closed(self, monkeypatch):
        """判据写的是 <=：到点那一刻就已经不能用了。

        这条第一次写成"取 now、把 expires_at 设成 now"——**它是绿的，
        但绿得没有意义**：判据跑到的时候真实时间已经走过去了，
        改成 `<` 照样绿。要真的钉住这个边界只能把时间钉住。
        """
        frozen = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        monkeypatch.setattr("agent_platform.connector_sdk.datetime", _Frozen)
        with pytest.raises(ConnectorDenied, match="expired"):
            _check(_authorization(expires_at=frozen.isoformat()))

    def test_one_microsecond_before_the_deadline_still_works(self, monkeypatch):
        """上一条的反向：闸不能宽到"到点前也拒"。"""
        frozen = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        monkeypatch.setattr("agent_platform.connector_sdk.datetime", _Frozen)
        later = (frozen + timedelta(microseconds=1)).isoformat()
        assert _check(_authorization(expires_at=later)).id == "auth-1"


class TestTheOtherTwoGatesInTheSameSpot:
    """这两条本来就有人盯（同一批变异被抓住了），一起写在这儿留个形状。"""

    def test_a_request_without_an_authorization_id_is_refused(self):
        authorization = _authorization()
        request = _request(authorization)
        request.authorization_id = None
        service = ConnectorService.__new__(ConnectorService)
        with pytest.raises(ConnectorDenied, match="preauthorization"):
            ConnectorService._authorization_for_request_sync(
                service, _Conn(authorization.model_dump_json()), request,
                _policy(authorization), PAYLOAD_HASH, authorization.operation_kind)

    def test_an_authorization_that_is_not_on_file_is_refused(self):
        authorization = _authorization()
        service = ConnectorService.__new__(ConnectorService)
        with pytest.raises(ConnectorDenied, match="does not exist"):
            ConnectorService._authorization_for_request_sync(
                service, _Conn(None), _request(authorization),
                _policy(authorization), PAYLOAD_HASH, authorization.operation_kind)

    def test_a_different_payload_is_refused(self):
        """授权绑的是**那一份载荷**：换了内容就不是同一次准许。"""
        with pytest.raises(ConnectorDenied, match="does not match"):
            _check(_authorization(), payload_hash="sha256-of-a-different-payload")
