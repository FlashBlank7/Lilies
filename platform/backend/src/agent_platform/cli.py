from __future__ import annotations

import logging
import os

import uvicorn

from .config import get_settings


def configure_logging(level: str | None = None) -> int:
    """把后端自己的日志接到 uvicorn 的输出上。

    2026-08-29 发现：整个后端**从来没配过 logging**。
    `logging.getLogger(__name__)` 落到 root 的默认级别 WARNING，
    于是 5 处 logger.info 一条都没进过日志文件——
    事件归档删了多少行、冷文件压缩省了多少空间，全部丢弃。
    真机日志里 agent_platform 的记录数：0。

    这件事本身不难，难在它没有任何症状：日志不报错，只是不写。
    是为了给后台维护任务加"跑了没有"的日志，才发现加了也不会显示。

    级别取 LOG_LEVEL 环境变量，默认 INFO。uvicorn 已经装了 handler，
    这里只把自家 logger 的级别放开并挂上同一个 handler，不重复配 root——
    重复配会让每行日志打两遍。
    """
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    numeric = getattr(logging, resolved, logging.INFO)
    package = logging.getLogger("agent_platform")
    package.setLevel(numeric)
    if not package.handlers and not logging.getLogger().handlers:
        # 独立跑（没有 uvicorn 兜底）时也要有地方输出
        logging.basicConfig(
            level=numeric,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    return numeric


def main() -> None:
    settings = get_settings()
    configure_logging()
    uvicorn.run("agent_platform.api:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
