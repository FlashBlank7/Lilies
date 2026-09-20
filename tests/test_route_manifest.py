"""路由清单冻结：236 条路由的 (方法, 路径, 处理函数名) 是对外合同。

这道测试的用途不是"防止有人加路由"，而是让 `create_app` 的拆分成为**机械操作**：
把闭包里的路由搬进 APIRouter 时，任何一条被漏掉、改名、路径写错，这里当场红。
没有它，4817 行函数的拆分就是一次没有回头路的手工誊抄。

新增/删除/改名路由是正常开发，重新生成金标即可：
    python -m tests.test_route_manifest --update
提交时金标的 diff 就是这次改了哪些对外接口——评审看这一段就够。
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.routing import APIRoute, APIWebSocketRoute

from agent_platform.api import create_app
from agent_platform.config import Settings

GOLDEN = Path(__file__).parent / "golden" / "route_manifest.txt"


def collect_routes(app: object) -> list[str]:
    lines: list[str] = []
    for route in app.routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                lines.append(f"{method:<7} {route.path:<80} {route.name}")
        elif isinstance(route, APIWebSocketRoute):
            lines.append(f"{'WS':<7} {route.path:<80} {route.name}")
    return sorted(lines)


def build_manifest(tmp_path: Path) -> list[str]:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "ws")
    return collect_routes(create_app(settings))


def test_route_manifest_is_unchanged(tmp_path: Path) -> None:
    actual = build_manifest(tmp_path)
    expected = GOLDEN.read_text(encoding="utf-8").splitlines()

    missing = [line for line in expected if line not in actual]
    added = [line for line in actual if line not in expected]

    detail = []
    if missing:
        detail.append("消失的路由（拆分时漏搬或改错了路径/名字）:\n  " + "\n  ".join(missing))
    if added:
        detail.append("新出现的路由:\n  " + "\n  ".join(added))
    assert not detail, (
        "\n\n".join(detail)
        + "\n\n若这是有意的接口变更，跑 `python -m tests.test_route_manifest --update` 重新生成金标。"
    )


def test_route_names_are_unique() -> None:
    """处理函数名是 OpenAPI operationId 的来源，重名会让客户端生成器静默合并两条接口。"""
    names = [line.split(None, 2)[2] for line in GOLDEN.read_text(encoding="utf-8").splitlines()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"路由处理函数重名：{duplicates}"


if __name__ == "__main__":
    import tempfile

    if "--update" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            GOLDEN.write_text("\n".join(build_manifest(Path(tmp))) + "\n", encoding="utf-8")
        print(f"已重新生成 {GOLDEN}")
