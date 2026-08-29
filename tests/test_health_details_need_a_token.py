"""存活探测公开，细节要令牌。

回归背景（2026-08-29）：/health 整份都是免鉴权的，里面有

  · 精确的 git 提交、分支、工作区是否干净
  · 20 个路由的可用性图（等于一张 API 地图）
  · 有没有配模型、Docker 可用不可用、模型出网开没开
  · **运行时工具清单**——里面有 Bash

绑在 127.0.0.1 上不算什么。可 Dockerfile 里默认 API_HOST=0.0.0.0，
那就是一个不用登录就能拿到的指纹面。

存活探测本身要保持公开：k8s、反向代理、guanjia doctor 的匿名可达性检查
都只需要知道它答不答话。所以不带令牌照样 200，只是内容最小化。
"""
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings

TOKEN = "health-token"


class HealthDetailsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.app = create_app(Settings(api_token=TOKEN, data_dir=root / "d",
                                       workspace_root=root / "w",
                                       scheduler_poll_seconds=3600))

    def _get(self, token: str | None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with TestClient(self.app) as client:
            return client.get("/health", headers=headers)

    def test_anonymous_still_gets_200(self):
        """存活探测不能因为加固就变成 401——反代会把服务判成挂了。"""
        self.assertEqual(self._get(None).status_code, 200)
        self.assertEqual(self._get(None).json()["status"], "ok")

    def test_anonymous_gets_nothing_else(self):
        body = self._get(None).json()
        self.assertEqual(set(body), {"status"}, f"匿名还能看到：{sorted(body)}")

    def test_a_wrong_token_is_treated_as_anonymous(self):
        # HTTP 头只能是 ASCII，别拿中文当假令牌（第一版就是这么写红的）
        self.assertEqual(set(self._get("wrong-guess").json()), {"status"})

    def test_the_right_token_gets_the_details(self):
        body = self._get(TOKEN).json()
        self.assertIn("runtime", body)
        self.assertIn("commit", body["runtime"]["git"])
        self.assertIn("route_availability", body["runtime"])

    def test_the_fingerprint_bits_are_all_behind_the_token(self):
        anonymous = self._get(None).json()
        for leak in ("git", "route_availability", "tools", "docker_available",
                     "deepseek_configured", "model_egress_enabled", "provider"):
            self.assertNotIn(leak, str(anonymous), leak)

# 这里原本还有一条 inspect.getsource 查 compare_digest 的断言。删了——
# 断言源码长什么样，换个写法就骗过去。"用对令牌能看到、用错看不到"
# 这个**行为**上面几条已经盖住了；常数时间比较是纵深防御，
# 理由写在实现那一侧的注释里。


if __name__ == "__main__":
    unittest.main()


class EveryRegisteredRouteActuallyExistsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.app = create_app(Settings(api_token=TOKEN, data_dir=root / "d",
                                       workspace_root=root / "w",
                                       scheduler_poll_seconds=3600))

    """RUNTIME_ROUTE_CHECKS 里登记的 20 条路由，得真的还在。

    这张表是手写的，路由是另写的——两边迟早分家。
    分家之后的表现很温和：/health 的 route_availability 里
    那一条永远是 false，而它只有拿对令牌去查 /health 才看得见，
    没有任何东西会响。运维照着这个面板判断"哪些能力可用"，
    看到的会是一个早就改名的接口一直"不可用"。

    反过来也一样：真删了一个接口而表里还留着，等于把一个已经消失的
    能力持续报成"我们查过了"。
    """

    def test_no_registered_route_is_stale(self):
        from agent_platform.api import route_availability

        gone = sorted(name for name, ok in route_availability(self.app).items() if not ok)
        self.assertEqual(gone, [], f"表里登记了、实际不存在的路由：{gone}")

    def test_the_table_is_not_empty(self):
        """空表会让上一条永远绿——那是"检查存在但什么也没查"。"""
        from agent_platform.api import RUNTIME_ROUTE_CHECKS

        self.assertGreater(len(RUNTIME_ROUTE_CHECKS), 10)

    def test_a_made_up_route_is_reported_missing(self):
        """闸本身要能响：登记一个不存在的路由，必须报不可用。"""
        from agent_platform.api import RUNTIME_ROUTE_CHECKS, route_availability

        with patch.dict(RUNTIME_ROUTE_CHECKS,
                        {"根本没有这个": ("GET", "/api/v1/根本没有这个")}):
            self.assertFalse(route_availability(self.app)["根本没有这个"])
