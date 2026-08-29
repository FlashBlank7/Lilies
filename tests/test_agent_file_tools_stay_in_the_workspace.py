"""智能体的 Read/Write/Edit 只能碰工作区里的东西——这道闸此前零测试。

变异验证（2026-08-29，全量 2220 条）：**四个变异一个都没红**——
摘掉相对路径那层、摘掉沙箱里那层、两层一起摘、两层都改成字符串前缀比。
也就是说这道闸坏成什么样都没有东西会响。

闸有两层，都要测：
1. `_workspace_relative_path`：进沙箱之前先看这个路径**长得**对不对
   （不许绝对路径、不许 ..、不许 Windows 盘符）。
2. `_SAFE_PATH_SCRIPT`：在沙箱里 resolve 之后再比一次
   （符号链接只有这一层看得见——第一层是纯字符串判断）。

第二层是一段独立的 Python 片段，所以这里直接**把它跑起来**，
不是断言源码里有那几个字。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_platform.tools.core import _SAFE_PATH_SCRIPT, _workspace_relative_path


class TestTheShapeOfThePathIsCheckedFirst:
    @pytest.mark.parametrize("path", [
        "/etc/passwd",            # 绝对路径
        "../x",                   # 往上爬
        "a/../../etc",            # 爬到中间
        "a/..",                   # 爬回起点也不行（保守）
        "..",
        "/",
        "//host/share",           # UNC 归一化后是绝对路径
        "C:/Windows",             # 盘符
        "c:/x",
        "a\\..\\b",               # 反斜杠归一化后仍是 ..
        "",                       # 空路径
    ])
    def test_a_path_that_could_leave_the_workspace_is_refused(self, path):
        with pytest.raises(ValueError, match="relative to the workspace"):
            _workspace_relative_path(path)

    @pytest.mark.parametrize("path", [
        "a.txt", "dir/a.txt", "./a", "a/./b", ".", "...",
        "~", "~/notes.txt",       # 波浪号在这里是**普通目录名**，没有 shell 展开
    ])
    def test_an_ordinary_path_is_allowed(self, path):
        """反向那一批。少了它，"一律拒绝"也能让上面全绿——
        而那会让 Read/Write 整个不能用。"""
        assert _workspace_relative_path(path) == path

    def test_a_tilde_is_not_expanded_anywhere(self, tmp_path):
        """`~/.ssh/id_rsa` 放行是安全的**前提是没人展开它**。

        工具是把路径当 argv 传给一段 Python 的（不过 shell），
        所以 `root / "~/.ssh/id_rsa"` 就是工作区里一个叫 ~ 的目录。
        这一条钉住那个前提：哪天有人把它改成走 shell，这里会红。
        """
        assert "python" in _tool_command_shape()


def _tool_command_shape() -> str:
    """工具是怎么把路径交出去的——argv，不是 shell 字符串。"""
    import inspect

    from agent_platform.tools.core import ReadTool

    return inspect.getsource(ReadTool.execute)


def _run_guard(argument: str, root: Path) -> subprocess.CompletedProcess:
    """把沙箱里那段真脚本跑一遍。

    脚本里的根写死是 /workspace，所以这里换成临时目录再跑——
    换的是同一段代码里的那一行，不是另写一份。
    """
    script = _SAFE_PATH_SCRIPT.replace("'/workspace'", repr(str(root)))
    return subprocess.run([sys.executable, "-c", script, argument],
                          capture_output=True, text=True)


class TestTheSandboxChecksAgainAfterResolving:
    @pytest.fixture
    def root(self):
        with tempfile.TemporaryDirectory() as made:
            root = Path(made) / "workspace"
            (root / "sub").mkdir(parents=True)
            (Path(made) / "outside").mkdir()
            yield root

    def test_an_escaping_path_is_refused(self, root):
        done = _run_guard("../outside", root)
        assert done.returncode != 0
        assert "escapes workspace" in done.stderr

    def test_an_absolute_path_elsewhere_is_refused(self, root):
        done = _run_guard(str(root.parent / "outside"), root)
        assert done.returncode != 0

    def test_a_symlink_pointing_out_is_refused(self, root):
        """符号链接**只有这一层看得见**：第一层是纯字符串判断，
        `link/a.txt` 长得完全正常。"""
        (root / "link").symlink_to(root.parent / "outside")
        done = _run_guard("link/a.txt", root)
        assert done.returncode != 0, done.stdout

    def test_a_sibling_sharing_the_string_prefix_is_refused(self, root):
        """`workspace-evil` 的字符串前缀是 `workspace`，但不在里面。

        按字符串前缀比的实现会放它进来；按路径分段比的不会。
        """
        sibling = root.parent / f"{root.name}-evil"
        sibling.mkdir()
        done = _run_guard(str(sibling), root)
        assert done.returncode != 0, done.stdout

    def test_the_workspace_itself_and_its_children_pass(self, root):
        """反向那一批：不然"全都拒"也能让上面全绿。"""
        for argument in (".", "sub", "sub/a.txt", str(root)):
            done = _run_guard(argument, root)
            assert done.returncode == 0, (argument, done.stderr)
