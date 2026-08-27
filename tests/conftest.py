from __future__ import annotations

import pytest
from pathlib import Path


# ── 护栏：测试不得连线上库 ────────────────────────────────────────────
# 真事故（2026-08-28）：一个测试忘了 data_dir=tmp_path，TestClient 启动时执行
# "把 queued/building 构建标成 needs_attention"，直接打断了线上在飞的修复构建，
# 还往线上库塞了两个测试应用。忘记加隔离参数是个太容易犯的错，这里从结构上堵死。

@pytest.fixture(autouse=True)
def guard_live_data_dir(monkeypatch, tmp_path):
    """任何测试若用默认 data_dir 构造 Settings，自动改指 tmp_path 并给出提示。"""
    from agent_platform import config as _config

    real_init = _config.Settings.__init__
    default_dir = Path("data").resolve()

    def guarded_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        if Path(self.data_dir).resolve() == default_dir:
            object.__setattr__(self, "data_dir", tmp_path / "guarded-data")
            if Path(self.workspace_root).resolve() == Path("workspaces").resolve():
                object.__setattr__(self, "workspace_root", tmp_path / "guarded-ws")
            self.prepare()

    monkeypatch.setattr(_config.Settings, "__init__", guarded_init)
