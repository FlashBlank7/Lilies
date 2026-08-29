"""密文信封的认证标签，得真能认出被改过的信封。

平台的密钥（连接器的 API token 之类）是加密存的，每份信封带一个
HMAC-SHA256 标签。标签存在的**唯一理由**就是"被人改过要认得出来"。

而这件事此前一条测试都没有。变异验证（2026-08-29，跑的是全量 1399 条）：
把 platform_harness 里两处、secret_kms 里一处的
`hmac.compare_digest(tag, expected)` 换成 `expected.startswith(tag)`，
**1399 条一条不红**。那个实现下标签形同虚设——
伪造者给一个 1 字节的标签，256 次就撞上了。

已有的用例覆盖了存取往返、密钥轮换、缺了旧密钥的情形，全是"正常路径"；
唯独没有人去改一改信封再看它认不认。**闸的测试要从坏那一侧进。**

（这里改的是数据库里存的那一行，模拟"能写库但没有密钥"的人。
不是在演示攻击，是在验证这道闸确实拦得住。）
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from agent_platform.platform_harness import (
    SECRET_ENVELOPE_V2_PREFIX,
    PlatformHarness,
    PlatformHarnessViolation,
)
from agent_platform.storage import Storage


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def harness(tmp_path: Path):
    storage = Storage(tmp_path / "storage")
    run(storage.initialize())
    made = PlatformHarness(
        storage=storage,
        secret_envelope_key="the-real-key",
        secret_envelope_key_id="kms-test",
    )
    run(made.save_secret(owner_id="owner-a", name="api_token",
                         value="sk-the-real-secret", description=""))
    made._storage_for_test = storage
    return made


def _read(harness) -> tuple[str, dict]:
    """把库里那一行拆成 (前缀, 信封字典)。"""
    raw = run(harness._storage_for_test.get_platform_secret(
        owner_id="owner-a", name="api_token"))["value"]
    assert raw.startswith(SECRET_ENVELOPE_V2_PREFIX), raw[:40]
    body = raw[len(SECRET_ENVELOPE_V2_PREFIX):]
    padded = body + "=" * (-len(body) % 4)
    return SECRET_ENVELOPE_V2_PREFIX, json.loads(base64.urlsafe_b64decode(padded))


def _put_back(harness, prefix: str, envelope: dict) -> None:
    blob = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stored = prefix + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    run(harness._storage_for_test.save_platform_secret(
        owner_id="owner-a", name="api_token", description="", value=stored))


def _open_it(harness):
    return run(harness.inject_secret_references(
        owner_id="owner-a", payload={"$secret": "api_token"}))


def test_an_untouched_envelope_still_opens(harness):
    """先钉住正向那一条。

    少了它，下面每条"被拒"都可能是因为整个信封根本打不开——
    那样测的就不是"认出了篡改"，而是"什么都打不开"。
    """
    assert _open_it(harness) == "sk-the-real-secret"


def test_changed_ciphertext_is_refused(harness):
    """把密文换掉、标签原样留着——标签就是为这个存在的。"""
    prefix, envelope = _read(harness)
    envelope["ciphertext"] = base64.urlsafe_b64encode(b"tampered!!").decode().rstrip("=")
    _put_back(harness, prefix, envelope)
    with pytest.raises(PlatformHarnessViolation, match="authentication failed"):
        _open_it(harness)


def test_a_truncated_tag_is_refused(harness):
    """只留标签的头几个字节。

    这一条正对着变异验证抓到的那个实现（`expected.startswith(tag)`）：
    那种写法下，短标签一律通过——伪造者给 1 个字节，256 次就撞上了。
    完整标签比对必须拒掉它。
    """
    prefix, envelope = _read(harness)
    raw_tag = base64.urlsafe_b64decode(
        envelope["tag"] + "=" * (-len(envelope["tag"]) % 4))
    for keep in (1, 4, 16):
        envelope["tag"] = base64.urlsafe_b64encode(
            raw_tag[:keep]).decode().rstrip("=")
        _put_back(harness, prefix, envelope)
        with pytest.raises(PlatformHarnessViolation, match="authentication failed"):
            _open_it(harness)


def test_an_empty_tag_is_refused(harness):
    """空标签。`startswith("")` 恒真——最省事的那种伪造。"""
    prefix, envelope = _read(harness)
    envelope["tag"] = ""
    _put_back(harness, prefix, envelope)
    with pytest.raises(PlatformHarnessViolation, match="authentication failed"):
        _open_it(harness)


def test_a_flipped_bit_in_the_tag_is_refused(harness):
    """标签最后一位改一改——长度对、内容差一点。"""
    prefix, envelope = _read(harness)
    raw_tag = bytearray(base64.urlsafe_b64decode(
        envelope["tag"] + "=" * (-len(envelope["tag"]) % 4)))
    raw_tag[-1] ^= 0x01
    envelope["tag"] = base64.urlsafe_b64encode(bytes(raw_tag)).decode().rstrip("=")
    _put_back(harness, prefix, envelope)
    with pytest.raises(PlatformHarnessViolation, match="authentication failed"):
        _open_it(harness)


def test_a_swapped_nonce_is_refused(harness):
    """密文没动，只把 nonce 换掉——标签覆盖的是整个信封，不只是密文。"""
    prefix, envelope = _read(harness)
    envelope["nonce"] = base64.urlsafe_b64encode(b"0" * 16).decode().rstrip("=")
    _put_back(harness, prefix, envelope)
    with pytest.raises(PlatformHarnessViolation, match="authentication failed"):
        _open_it(harness)


class TestTheWrappedDataKeyIsAuthenticatedToo:
    """信封里还套着一层：数据密钥本身也是被包起来的，也带标签。

    上面那批测的是外层信封（platform_harness）；这一层在 secret_kms 里，
    同一次变异验证里同样是"改成前缀比、1399 条一条不红"。
    两层是两条独立的路——**闸铺满所有出口**，测试也要铺满。
    """

    @staticmethod
    def _provider():
        from agent_platform.secret_kms import LocalSecretKMSProvider

        return LocalSecretKMSProvider(
            provider_id="local-kms",
            primary_key_id="k1",
            wrapping_keys={"k1": "wrapping-key-material"},
        )

    def test_an_untouched_wrapped_key_still_unwraps(self):
        """正向那一条：不钉住它，下面的"被拒"可能只是因为整层都打不开。"""
        provider = self._provider()
        key = b"0123456789abcdef0123456789abcdef"
        assert provider.unwrap_data_key(provider.wrap_data_key(key)) == key

    @pytest.mark.parametrize("how", ["truncate", "empty", "flip", "swap_nonce"])
    def test_a_tampered_wrapped_key_is_refused(self, how):
        provider = self._provider()
        wrapped = provider.wrap_data_key(b"0123456789abcdef0123456789abcdef")
        raw_tag = base64.urlsafe_b64decode(
            wrapped["tag"] + "=" * (-len(wrapped["tag"]) % 4))
        if how == "truncate":
            wrapped["tag"] = base64.urlsafe_b64encode(raw_tag[:4]).decode().rstrip("=")
        elif how == "empty":
            wrapped["tag"] = ""
        elif how == "flip":
            flipped = bytearray(raw_tag)
            flipped[-1] ^= 0x01
            wrapped["tag"] = base64.urlsafe_b64encode(bytes(flipped)).decode().rstrip("=")
        else:
            wrapped["nonce"] = base64.urlsafe_b64encode(b"0" * 16).decode().rstrip("=")
        with pytest.raises(ValueError, match="authentication failed"):
            provider.unwrap_data_key(wrapped)


class TestTheBudgetReceiptCannotBeEdited:
    """连接器预算回执：policy_digest 必须和回执里那份策略对得上。

    回执是给审计看的——"这个任务当时被允许写几次、能连哪些主机"。
    它自带两个摘要（policy_digest 和 receipt_digest），
    存在的理由就是"改一个字就验不过"。而这件事也没有测试：
    同一次变异验证里，把 `hmac.compare_digest(self.policy_digest, …)`
    换成前缀比，1399 条一条不红。

    说清现状：export_assignment_budget 目前只有测试在调，
    生产上还没有消费者。所以这是**埋着的坑**，不是线上故障——
    但回执的全部意义就是"能被重新验一遍"，那条路得是通的。
    """

    @staticmethod
    def _receipt_fields() -> dict:
        import hashlib
        import json

        policy = {
            "allowed_network_hosts": ["api.example.com"],
            "allowed_compensation_operations": ["crm.undo"],
            "max_write_count": 5,
            "max_payload_bytes": 1024,
        }
        digest = hashlib.sha256(json.dumps(
            policy, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            "assignment_id": "assign-1",
            "policy_digest": digest,
            "allowed_network_hosts": policy["allowed_network_hosts"],
            "allowed_compensation_operations": policy["allowed_compensation_operations"],
            "max_write_count": policy["max_write_count"],
            "max_payload_bytes": policy["max_payload_bytes"],
            "write_count": 0,
            "writes": [],
        }

    @staticmethod
    def _sealed(fields: dict):
        """按 connector_sdk 的做法补上 receipt_digest 再构造。"""
        import hashlib
        import json

        from agent_platform.connector_sdk import ConnectorAssignmentBudgetReceipt

        blob = json.dumps({"schema_version": "1.0", **fields}, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        return ConnectorAssignmentBudgetReceipt(
            **fields, receipt_digest=f"sha256:{hashlib.sha256(blob).hexdigest()}")

    def test_a_consistent_receipt_is_accepted(self):
        """正向那一条——否则"改了就拒"可能是靠"一律拒"实现的。"""
        receipt = self._sealed(self._receipt_fields())
        assert receipt.max_write_count == 5

    @pytest.mark.parametrize("field, value", [
        ("max_write_count", 500),
        ("max_payload_bytes", 10 * 1024 * 1024),
        ("allowed_network_hosts", ["api.example.com", "evil.test"]),
        ("allowed_compensation_operations", []),
    ])
    def test_raising_a_limit_by_hand_is_refused(self, field, value):
        """把额度调大、主机名加一个——摘要就对不上了。"""
        fields = self._receipt_fields()
        fields[field] = value
        with pytest.raises(Exception, match="policy digest"):
            self._sealed(fields)

    @pytest.mark.parametrize("bad", ["", "abc", "0" * 64])
    def test_a_short_or_wrong_digest_is_refused(self, bad):
        """空摘要和只对开头的摘要都得拒——正对着前缀比那种实现。"""
        fields = self._receipt_fields()
        fields["policy_digest"] = bad or fields["policy_digest"][:4]
        with pytest.raises(Exception, match="policy digest"):
            self._sealed(fields)
