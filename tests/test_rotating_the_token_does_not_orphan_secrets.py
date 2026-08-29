"""换一次 API 令牌，存量密钥就永远读不出来了——这条路要么走通、要么当场报错。

实测（2026-08-29，三步都跑过）：

  1. 信封密钥没单独配时取 API 令牌
     （agent_runtime_factory：`… or settings.api_token`），
     而 key_id 默认是 "local" 不动。
  2. 管理员轮换一次 API 令牌——一个再常规不过的动作——
     存量密钥当场读不出来，报的是
     「platform secret envelope authentication failed」，
     一个字都没提这跟换钥匙有关。
  3. 他照着 previous_keys 这个机制想救：把旧令牌填进去、id 还是 "local"。
     **救不回来**——当前密钥在 keyring 里把同名的旧密钥顶掉了
     （keyring == {'local': 新令牌}）。

也就是说这个配置本身是矛盾的（同一个 id 指两把钥匙），
而它的表现是**安静地丢数据**。改成构造时就报错，并把错误信息写清楚。

正确姿势也钉一条：新密钥换个 key_id，旧的按原 id 留在 previous_keys。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_platform.platform_harness import PlatformHarness, PlatformHarnessViolation
from agent_platform.storage import Storage

SECRET = "sk-crm-123456789"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    made = Storage(tmp_path / "s")
    asyncio.run(made.initialize())
    return made


def _harness(storage, **kwargs) -> PlatformHarness:
    return PlatformHarness(storage=storage, **kwargs)


def _write(storage, *, key: str, key_id: str = "local") -> None:
    harness = _harness(storage, secret_envelope_key=key, secret_envelope_key_id=key_id)
    asyncio.run(harness.save_secret(owner_id="own", name="crm",
                                    value=SECRET, description=""))


def _read(harness) -> str:
    return asyncio.run(harness.inject_secret_references(
        owner_id="own", payload={"$secret": "crm"}))


class TestTheContradictoryConfigIsRefusedUpFront:
    def test_the_same_key_id_cannot_hold_two_different_keys(self, storage):
        """当前密钥和旧密钥同名——安静地顶掉，等于安静地丢数据。"""
        with pytest.raises(ValueError, match="both the current key and a previous key"):
            _harness(storage, secret_envelope_key="token-new",
                     secret_envelope_key_id="local",
                     secret_envelope_previous_keys={"local": "token-old"})

    def test_the_message_says_what_to_do(self, storage):
        """报错要给出路，不然管理员只会把 previous_keys 删了了事。"""
        with pytest.raises(ValueError) as caught:
            _harness(storage, secret_envelope_key="token-new",
                     secret_envelope_key_id="local",
                     secret_envelope_previous_keys={"local": "token-old"})
        assert "new key id" in str(caught.value)

    def test_repeating_the_same_key_under_the_same_id_is_fine(self, storage):
        """同一个 id 指的是同一把钥匙——那只是写重了，不是矛盾，别拦。"""
        harness = _harness(storage, secret_envelope_key="token-a",
                           secret_envelope_key_id="local",
                           secret_envelope_previous_keys={"local": "token-a"})
        assert harness.secret_envelope_keyring == {"local": "token-a"}


class TestRotationDoneRightStillReadsOldSecrets:
    def test_a_new_key_id_plus_the_old_key_in_previous_works(self, storage):
        _write(storage, key="token-old", key_id="local")
        rotated = _harness(storage, secret_envelope_key="token-new",
                           secret_envelope_key_id="local-v2",
                           secret_envelope_previous_keys={"local": "token-old"})
        assert _read(rotated) == SECRET

    def test_new_secrets_after_rotation_use_the_new_key(self, storage):
        _write(storage, key="token-old", key_id="local")
        rotated = _harness(storage, secret_envelope_key="token-new",
                           secret_envelope_key_id="local-v2",
                           secret_envelope_previous_keys={"local": "token-old"})
        asyncio.run(rotated.save_secret(owner_id="own", name="fresh",
                                        value="sk-fresh-000", description=""))
        # 只带新密钥的进程也读得到新写的那条
        only_new = _harness(storage, secret_envelope_key="token-new",
                            secret_envelope_key_id="local-v2")
        assert asyncio.run(only_new.inject_secret_references(
            owner_id="own", payload={"$secret": "fresh"})) == "sk-fresh-000"


class TestTheFailureSaysWhyWhenTheKeyIsSimplyGone:
    def test_reading_with_a_different_key_explains_itself(self, storage):
        """旧密钥彻底没了的时候仍然会失败——但要告诉人为什么。

        原来只有一句「authentication failed」，让人从 HMAC 开始查；
        而头号原因是钥匙换了。
        """
        _write(storage, key="token-old", key_id="local")
        stranger = _harness(storage, secret_envelope_key="token-new",
                            secret_envelope_key_id="local")
        with pytest.raises(PlatformHarnessViolation) as caught:
            _read(stranger)
        message = str(caught.value)
        assert "rotated" in message, message
        assert "local" in message, "要说清是哪个 key id"

    def test_the_right_key_still_reads_it(self, storage):
        """反向那一条：钥匙对的时候不许报错。"""
        _write(storage, key="token-old", key_id="local")
        same = _harness(storage, secret_envelope_key="token-old",
                        secret_envelope_key_id="local")
        assert _read(same) == SECRET
