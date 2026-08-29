"""业主码/使用码不能出现在日志里。

回归背景（2026-08-29 实测）：访问日志记的是完整路径，含查询串。
而业主页和客户使用页每次请求都带 ?code=——那是这两个面**唯一**的凭据。
发一次请求，日志里就明文躺着一条：

    GET /api/v1/use/7317afc0-…/definition?code=CANARY-ACCESS-CODE HTTP/1.1

拿到日志的人可以直接拿去用，而日志经常被打包、转发、贴进工单。
调试页的 EventSource 同理（它把 token 放在 URL 上——
EventSource 设不了请求头，这是没办法的写法，那就在日志这一侧兜住）。
"""
import logging
import unittest

from agent_platform.cli import _RedactSecrets, _redact, configure_logging


class RedactTest(unittest.TestCase):
    def test_an_access_code_is_masked(self):
        self.assertEqual(
            _redact("GET /api/v1/use/abc/definition?code=SECRET123 HTTP/1.1"),
            "GET /api/v1/use/abc/definition?code=*** HTTP/1.1")

    def test_a_token_in_a_stream_url_is_masked(self):
        self.assertNotIn("abc.def", _redact("/v1/streams/x/events?token=abc.def"))

    def test_it_masks_the_whole_family(self):
        for name in ("code", "token", "api_key", "apikey", "key",
                     "secret", "password", "signature"):
            out = _redact(f"/x?{name}=VALUE123")
            self.assertNotIn("VALUE123", out, name)

    def test_it_is_case_insensitive(self):
        self.assertNotIn("V1", _redact("/x?CODE=V1"))

    def test_only_the_value_goes(self):
        """路径本身要留着——不然日志就没法用来排查了。"""
        out = _redact("GET /api/v1/use/app-42/definition?code=S HTTP/1.1")
        self.assertIn("/api/v1/use/app-42/definition", out)
        self.assertIn("HTTP/1.1", out)

    def test_other_query_params_survive(self):
        out = _redact("/x?days=7&code=S&limit=5")
        self.assertIn("days=7", out)
        self.assertIn("limit=5", out)

    def test_a_url_without_secrets_is_untouched(self):
        text = "GET /api/v1/applications HTTP/1.1"
        self.assertEqual(_redact(text), text)


class FilterOnRealRecordsTest(unittest.TestCase):
    """uvicorn 把完整路径放在 record.args 里，光过 msg 是不够的。"""

    def _filtered(self, msg, args):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, msg, args, None)
        _RedactSecrets().filter(record)
        return record.getMessage()

    def test_it_masks_a_path_passed_as_an_argument(self):
        out = self._filtered(
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/api/v1/use/a/definition?code=SECRET", "1.1", 200))
        self.assertNotIn("SECRET", out)
        self.assertIn("/api/v1/use/a/definition", out)

    def test_it_masks_a_secret_in_the_format_string(self):
        self.assertNotIn("SECRET", self._filtered("查了 ?code=SECRET", None))

    def test_a_dict_of_args_is_handled(self):
        """%(name)s 这种写法的 args 是个字典，也要过一遍。

        （LogRecord 只有在 args 是「单个字典」时才按映射格式化，
        所以这里要用 args=({...},) 的形式构造。）
        """
        record = logging.LogRecord("x", logging.INFO, "", 0, "%(u)s",
                                   ({"u": "?code=S"},), None)
        _RedactSecrets().filter(record)
        self.assertNotIn("code=S", record.getMessage())

    def test_non_string_args_are_left_alone(self):
        out = self._filtered("%s %d", ("ok", 200))
        self.assertEqual(out, "ok 200")


class WiredIntoLoggingTest(unittest.TestCase):
    def test_configure_logging_attaches_the_filter(self):
        configure_logging()
        for name in ("uvicorn.access", "agent_platform"):
            self.assertTrue(
                any(isinstance(f, _RedactSecrets)
                    for f in logging.getLogger(name).filters),
                f"{name} 上没挂脱敏，日志照样会落凭据")


if __name__ == "__main__":
    unittest.main()
