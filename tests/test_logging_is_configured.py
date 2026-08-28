"""后端的 logger.info 必须真的写得出来。

回归背景（2026-08-29）：整个后端**从来没配过 logging**。
`logging.getLogger(__name__)` 落到 root 的默认级别 WARNING，
于是 5 处 logger.info 一条都没进过日志文件——
事件归档删了多少行、冷文件压缩省了多少空间，全部丢弃。
真机日志里 agent_platform 的记录数：0。

这个 bug 没有任何症状。日志不报错，只是不写；
不去数"应该有几行、实际有几行"就永远发现不了。
是为了给后台维护任务加"跑了没有"的日志，才发现加了也不会显示——
一个 bug 在修另一个 bug 的路上被撞出来。
"""
import logging
import unittest
from unittest.mock import patch

from agent_platform.cli import configure_logging, main


class LoggingIsConfiguredTest(unittest.TestCase):
    def setUp(self) -> None:
        package = logging.getLogger("agent_platform")
        self._level = package.level
        self.addCleanup(package.setLevel, self._level)

    def test_info_from_the_backend_actually_gets_emitted(self):
        """判据是"这条记录出得来"，不是"级别设对了"。"""
        configure_logging()
        with self.assertLogs("agent_platform.api", level="INFO") as captured:
            logging.getLogger("agent_platform.api").info("事件维护完成：归档 %s 行", 12)
        self.assertIn("归档 12 行", captured.output[0])

    def test_the_default_is_info_not_warning(self):
        """默认 WARNING 正是这个 bug——info 全丢。"""
        configure_logging()
        self.assertTrue(
            logging.getLogger("agent_platform.api").isEnabledFor(logging.INFO))

    def test_log_level_env_var_is_honoured(self):
        with patch.dict("os.environ", {"LOG_LEVEL": "WARNING"}):
            configure_logging()
        self.assertFalse(
            logging.getLogger("agent_platform.api").isEnabledFor(logging.INFO))
        self.assertTrue(
            logging.getLogger("agent_platform.api").isEnabledFor(logging.WARNING))

    def test_a_bogus_level_falls_back_to_info_instead_of_crashing(self):
        """打错环境变量不该让服务起不来。"""
        with patch.dict("os.environ", {"LOG_LEVEL": "CHATTY"}):
            self.assertEqual(configure_logging(), logging.INFO)


class ItIsActuallyCalledOnStartupTest(unittest.TestCase):
    """配置函数写好了、没人调，日志照样是空的。

    这一条盯的是调用点。上周的教训：闸是"一个函数 + 一个调用点"，
    函数绿不代表调用点存在——那次删掉调用点，测试全绿。
    """

    def test_main_configures_logging_before_serving(self):
        order = []
        with patch("agent_platform.cli.configure_logging",
                   side_effect=lambda *a, **k: order.append("配日志")), \
             patch("uvicorn.run", side_effect=lambda *a, **k: order.append("起服务")):
            main()
        self.assertEqual(order, ["配日志", "起服务"],
                         "日志得在服务起来之前配好，不然启动阶段的记录还是丢")


if __name__ == "__main__":
    unittest.main()
