"""CLI 入口：参数解析、用户交互"""

import os
import sys
import argparse
from .agent import generate_commit_message
from .git_utils import stage_all, commit


def display_preview(commit_info: dict) -> bool:
    """展示生成的 commit message（按实际 git log 格式渲染）。"""
    if "error" in commit_info:
        print(f"\n❌ {commit_info['error']}")
        return False

    # 构建最终 commit message 的完整文本
    full_msg = commit_info["title"]
    if commit_info.get("body"):
        full_msg += f"\n\n{commit_info['body']}"

    print("\n" + "=" * 56)
    print(" 📝 生成的 Commit Message")
    print("=" * 56)
    print(full_msg)
    print("=" * 56)
    return True


def confirm_commit() -> bool:
    """交互式确认，返回 True 表示确认提交。"""
    while True:
        choice = input("\n确认提交？[Y]es / [n]o / [q]uit: ").strip().lower()
        if choice in ("y", "yes", ""):
            return True
        elif choice in ("n", "no"):
            print("已取消提交")
            return False
        elif choice in ("q", "quit"):
            print("退出")
            sys.exit(0)
        else:
            print("请输入 Y (提交) / n (取消) / q (退出)")


def main():
    parser = argparse.ArgumentParser(
        description="🤖 AI-powered Git commit message generator (DeepSeek)"
    )
    parser.add_argument(
        "--model", default="deepseek-chat",
        help="模型名称（默认 deepseek-chat）"
    )
    parser.add_argument(
        "--stage", action="store_true",
        help="自动暂存所有变更后再生成 commit"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="跳过确认提示，直接提交"
    )
    args = parser.parse_args()

    try:
        # 读取 API Key
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            print("❌ 请设置环境变量 DEEPSEEK_API_KEY")
            print("   Windows: $env:DEEPSEEK_API_KEY='sk-xxx'")
            print("   Mac/Linux: export DEEPSEEK_API_KEY=sk-xxx")
            sys.exit(1)

        # 可选：自动暂存
        if args.stage:
            print("📦 暂存所有变更...")
            stage_all()

        # 生成 commit message
        print("🤖 正在分析代码变更...")
        commit_info = generate_commit_message(api_key, args.model)

        # 展示和确认
        if not display_preview(commit_info):
            sys.exit(1)

        if args.yes or confirm_commit():
            full_message = commit_info["title"]
            if commit_info.get("body"):
                full_message += f"\n\n{commit_info['body']}"
            commit(full_message)

    except KeyboardInterrupt:
        print("\n\n⚠️  操作已取消")
        sys.exit(130)


if __name__ == "__main__":
    main()
