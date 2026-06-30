"""Git 操作封装"""

import os
import subprocess
import tempfile


class GitCommandError(RuntimeError):
    """Git 命令执行失败时抛出，包含 stderr 用于诊断。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _run_git(*args: str) -> str:
    """执行 git 命令的底层封装。失败时抛出 GitCommandError 而非静默返回空字符串。"""
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GitCommandError(
            f"git {' '.join(args)} 失败 (exit {result.returncode})"
            + (f": {stderr}" if stderr else "")
        )
    return result.stdout


def is_git_repo() -> bool:
    """检查当前目录是否在 Git 仓库中。"""
    try:
        _run_git("rev-parse", "--git-dir")
        return True
    except GitCommandError:
        return False


def get_full_diff() -> str:
    """获取工作区所有变更（staged + unstaged）。

    如果不在 Git 仓库中则抛出 GitCommandError。
    """
    if not is_git_repo():
        raise GitCommandError("当前目录不是 Git 仓库，请在 Git 仓库中运行此命令")

    staged = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    unstaged = subprocess.run(
        ["git", "diff"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return staged + unstaged


def get_staged_diff() -> str:
    """仅获取已暂存的变更。"""
    if not is_git_repo():
        raise GitCommandError("当前目录不是 Git 仓库，请在 Git 仓库中运行此命令")
    return _run_git("diff", "--cached")


def stage_all() -> None:
    """暂存所有变更。"""
    _run_git("add", "-A")
    print("✅ 已暂存所有变更")


def commit(message: str) -> None:
    """执行 git commit，使用临时文件传递消息以避免 shell 转义问题。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(message)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["git", "commit", "-F", tmp_path],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            raise GitCommandError(f"提交失败:\n{result.stderr}")
        print(result.stdout.strip())
    finally:
        os.unlink(tmp_path)
