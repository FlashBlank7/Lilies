"""工作区必须留在 workspace_root 里面——而这道闸一直没有测试。

变异验证（2026-08-29）：把 `resolve_workspace` 里那句边界检查整个删掉，
**全套 1260 条测试全绿**。

它管的是沙箱能碰到哪些目录：`workspace_path` 一旦能指到 root 外面，
沙箱就在宿主机上按那个路径读写，而且 `create=True` 时还会**建目录**。
所以顺序也要钉：检查必须在 mkdir **之前**——反过来的话，
一个越界请求会先在宿主机上留下一串目录，再被拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.sandbox import SandboxError, SandboxManager


@pytest.fixture
def manager(tmp_path: Path):
    settings = Settings(api_token="t", data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    settings.prepare()
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    return SandboxManager(settings), (tmp_path / "w").resolve()


def test_a_relative_path_inside_is_fine(manager):
    sandbox, root = manager
    (root / "job").mkdir()
    assert sandbox.resolve_workspace("job") == root / "job"


def test_the_root_itself_is_fine(manager):
    """root 自己不算越界——`resolved != root` 那半个条件就是为它写的。"""
    sandbox, root = manager
    assert sandbox.resolve_workspace(str(root)) == root


def test_a_dot_dot_escape_is_refused(manager):
    sandbox, root = manager
    with pytest.raises(SandboxError) as caught:
        sandbox.resolve_workspace("../../etc")
    assert "inside" in str(caught.value)


def test_an_absolute_path_outside_is_refused(manager):
    sandbox, _ = manager
    with pytest.raises(SandboxError):
        sandbox.resolve_workspace("/etc")


def test_a_sibling_with_the_same_prefix_is_refused(manager):
    """`/tmp/w-evil` 不在 `/tmp/w` 里面——按字符串前缀判会放它过去。

    这就是为什么用 `root in resolved.parents` 而不是 `startswith`。
    """
    sandbox, root = manager
    evil = root.parent / (root.name + "-evil")
    evil.mkdir()
    with pytest.raises(SandboxError):
        sandbox.resolve_workspace(str(evil))


def test_a_symlink_pointing_out_is_refused(manager):
    """软链接要按解析后的真实位置判——`resolve()` 就是干这个的。"""
    sandbox, root = manager
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(SandboxError):
        sandbox.resolve_workspace("link")


def test_creating_outside_does_not_leave_a_directory_behind(manager):
    """越界请求不能先建目录再被拒——顺序错了就是在宿主机上乱撒目录。"""
    sandbox, root = manager
    target = root.parent / "should-not-exist"
    with pytest.raises(SandboxError):
        sandbox.resolve_workspace(str(target), create=True)
    assert not target.exists(), "拒绝之前先把目录建出来了"


def test_creating_inside_does_create(manager):
    """别把闸关死：合法路径 create=True 要真的建出来。"""
    sandbox, root = manager
    made = sandbox.resolve_workspace("brand/new", create=True)
    assert made.is_dir()
    assert made == (root / "brand" / "new").resolve()


def test_a_missing_directory_without_create_is_refused(manager):
    sandbox, _ = manager
    with pytest.raises(SandboxError) as caught:
        sandbox.resolve_workspace("never-made")
    assert "does not exist" in str(caught.value)
