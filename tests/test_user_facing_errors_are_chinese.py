"""用户直接看得到的报错，一句英文、一个原始异常都不许出去。

回归背景（2026-08-29）：api.py 里 44 处 HTTPException，26 处 detail 是英文。
大部分在运营端点上（worker、connector、template），guanjia 不碰；
但有三处是客户端真会触发的：

  · POST /builds/{id}/resume  → "build cannot resume from failed"
        用户敲 `guanjia resume` 就看到这句，还附带一个状态码
  · POST /applications/{id}/builds → "unknown builder: X (available: …)"
  · POST /users               → "name required (1-40 chars)"
                               → "user exists or invalid: {error}"

最后那条除了英文还更糟：它把原始异常拼进 detail，
而那多半是 "UNIQUE constraint failed: users.name"——
库表结构直接回给调用方，而调用方看了也做不了什么。
"""
import re
import unittest
from pathlib import Path

ENGLISH_RUN = re.compile(r"[A-Za-z]{3,}(?:[ _-][A-Za-z]{3,})+")

# guanjia 会打到的路由前缀；运营端点不在此列（那边的英文是有意保留的）
CLIENT_FACING = ("/api/v1/applications", "/api/v1/runs", "/api/v1/builds",
                 "/api/v1/overview", "/api/v1/assistant", "/api/v1/health-report",
                 "/api/v1/me", "/api/v1/owner", "/api/v1/use", "/api/v1/auth",
                 "/api/v1/users")

SOURCE = Path(__file__).resolve().parents[1] / (
    "platform/backend/src/agent_platform/api.py")


def _client_facing_details() -> list[tuple[str, str]]:
    """回 [(路由, detail 原文)]，只取客户端会打到的那些路由。"""
    source = SOURCE.read_text(encoding="utf-8")
    blocks = re.split(r'@app\.(?:get|post|patch|delete)\(\s*"([^"]+)"', source)
    found = []
    for index in range(1, len(blocks), 2):
        path, body = blocks[index], blocks[index + 1]
        if not path.startswith(CLIENT_FACING):
            continue
        for match in re.finditer(
                r'HTTPException\(\s*\d+\s*,\s*f?["\']([^"\']{4,})', body):
            # 把 f-string 的插值去掉再看：{record.module_ref} 里是**数据**，
            # 不是写给人看的文案，扫描它只会把 module_ref 误判成英文散文。
            # （插值出去的值本身该不该给用户看是另一件事——
            #  比如原先 f"user exists or invalid: {error}" 把
            #  "UNIQUE constraint failed" 抬出去，那条是单独修的。）
            found.append((path, re.sub(r"\{[^}]*\}", "", match.group(1))))
    return found


class ClientFacingErrorsTest(unittest.TestCase):
    def test_no_english_prose_reaches_the_user(self):
        leaked = [(p, d) for p, d in _client_facing_details() if ENGLISH_RUN.search(d)]
        self.assertEqual(leaked, [], "客户端会看到英文报错：\n" + "\n".join(
            f"  {p} → {d}" for p, d in leaked))

    def test_the_scan_actually_finds_something(self):
        """扫不到任何 detail 的话，上一条断言是空过的。"""
        self.assertGreater(len(_client_facing_details()), 5)


class ResumeRefusalTest(unittest.TestCase):
    """续跑被拒时说人话，且不把状态码抬出去。"""

    def _refuse(self, status: str):
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient

        from agent_platform.api import create_app
        from agent_platform.config import Settings

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        app = create_app(Settings(api_token="t", data_dir=root / "d",
                                  workspace_root=root / "w",
                                  scheduler_poll_seconds=3600))
        with TestClient(app) as client:
            app.state.services.workflow_store.get_build = AsyncMock(
                return_value={"id": "b1", "status": status})
            return client.post("/api/v1/builds/b1/resume",
                               headers={"Authorization": "Bearer t"}, json={})

    def test_a_running_build_is_refused_in_plain_chinese(self):
        response = self._refuse("building")
        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertIn("还在跑", detail)
        self.assertNotIn("building", detail)

    def test_the_refusal_never_shows_the_status_code(self):
        for status in ("building", "queued", "running", "unknown_state"):
            detail = self._refuse(status).json()["detail"]
            self.assertIsNone(ENGLISH_RUN.search(detail), detail)
            self.assertNotIn(status, detail)

    def test_a_resumable_build_is_not_refused(self):
        """挡得太宽就是修出个新 bug——needs_attention 必须还能续。"""
        self.assertNotEqual(self._refuse("needs_attention").status_code, 409)


if __name__ == "__main__":
    unittest.main()
