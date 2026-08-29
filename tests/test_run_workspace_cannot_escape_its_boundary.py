"""一次运行选的工作目录，不能跑到它自己那块地之外去。

这道闸有**两层**：`_resolve_scoped_workspace` 先自己比一次，
再让沙箱按全局 workspace_root 比一次，回来后又比一次。
两层各自都能挡住越界——好事，但也正因为这样，
**逐个变异查不出这里有没有测试**：把任何一层单独摘掉，另一层还在，
全量 1582 条照样全绿（2026-08-29 实测，三个变异一个都没红）。

所以这个文件直接从"坏那一侧"进，而且把两层一起考虑：

  · 同前缀的兄弟目录（边界是 `ws`，请求 `ws-evil`）——
    按字符串前缀比的实现会放它进来，按路径分段比的不会。
    今天在 execution_policy 上钉过同一件事，这里是另一处同形状的闸。
  · `..` 爬出去、绝对路径指到别处、指到 workspace_root 本身。
  · 相对路径要落在**边界**里，不是进程的当前目录。

正向也要有：自己的子目录必须放行，否则"全都拒"也能让上面全绿。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.sandbox import SandboxManager
from agent_platform.workflow_runtime import (
    WorkflowRuntime,
    WorkflowWorkspaceBoundaryViolation,
)


class _OnlySandboxes:
    """只用得上 sandboxes 这一件，其余依赖不参与本函数。"""

    def __init__(self, sandboxes) -> None:
        self.sandboxes = sandboxes

    resolve = WorkflowRuntime._resolve_scoped_workspace


@pytest.fixture
def scope(tmp_path: Path):
    root = tmp_path / "workspaces"
    boundary = root / "ws"
    boundary.mkdir(parents=True)
    (root / "ws-evil").mkdir()          # 同前缀的兄弟
    (boundary / "inner").mkdir()
    (tmp_path / "outside").mkdir()
    settings = Settings(api_token="t", data_dir=tmp_path / "d",
                        workspace_root=root)
    runtime = _OnlySandboxes(SandboxManager(settings))
    return runtime, boundary, root, tmp_path


def _resolve(scope, requested: str) -> Path:
    runtime, boundary, _, _ = scope
    return runtime.resolve(requested, boundary)


class TestWhatMustBeRefused:
    def test_a_sibling_sharing_the_string_prefix(self, scope):
        """`ws-evil` 的字符串前缀是 `ws`，但它不在 `ws` 里面。

        这一条是整个文件的重点：按字符串比的实现会放它进来。
        """
        _, _, root, _ = scope
        with pytest.raises(WorkflowWorkspaceBoundaryViolation):
            _resolve(scope, str(root / "ws-evil"))

    def test_climbing_out_with_dot_dot(self, scope):
        with pytest.raises(WorkflowWorkspaceBoundaryViolation):
            _resolve(scope, "../ws-evil")

    def test_an_absolute_path_somewhere_else(self, scope):
        _, _, _, tmp_path = scope
        with pytest.raises(WorkflowWorkspaceBoundaryViolation):
            _resolve(scope, str(tmp_path / "outside"))

    def test_the_workspace_root_itself(self, scope):
        """根目录是所有运行共用的，不是这一次的地盘。"""
        _, _, root, _ = scope
        with pytest.raises(WorkflowWorkspaceBoundaryViolation):
            _resolve(scope, str(root))

    def test_a_directory_that_does_not_exist(self, scope):
        """不存在的目录也要拒——沙箱那层的错也要翻成同一种越界异常，
        不然调用方得同时接两种异常，迟早漏一种。"""
        _, boundary, _, _ = scope
        with pytest.raises(WorkflowWorkspaceBoundaryViolation):
            _resolve(scope, str(boundary / "never-made"))


class TestWhatMustStillWork:
    """少了这一批，"全都拒"能让上面全绿。"""

    def test_the_boundary_itself(self, scope):
        _, boundary, _, _ = scope
        assert _resolve(scope, str(boundary)) == boundary.resolve()

    def test_a_subdirectory(self, scope):
        _, boundary, _, _ = scope
        assert _resolve(scope, str(boundary / "inner")) == (boundary / "inner").resolve()

    def test_a_relative_path_lands_inside_the_boundary(self, scope):
        """相对路径要拼到**边界**上，不是拼到进程当前目录。"""
        _, boundary, _, _ = scope
        assert _resolve(scope, "inner") == (boundary / "inner").resolve()
