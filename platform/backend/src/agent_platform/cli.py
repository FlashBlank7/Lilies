from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

import uvicorn

from .config import get_settings


# 会出现在 URL 里的凭据。访问日志记的是完整路径（含查询串），
# 而业主页/客户使用页每次请求都带 ?code=——那是这两个面**唯一**的凭据。
# 2026-08-29 实测：发一次带 code 的请求，日志里就明文躺着一条。
# 拿到日志的人可以直接拿去用，而日志经常被打包、转发、贴进工单。
_URL_SECRETS = re.compile(
    r"(?i)\b(code|token|api_key|apikey|key|secret|password|signature)=([^&\s\"']+)")


def _redact(text: str) -> str:
    return _URL_SECRETS.sub(lambda m: f"{m.group(1)}=***", text)


class _RedactSecrets(logging.Filter):
    """把日志里 URL 上的凭据抹掉。

    uvicorn 的访问日志把完整路径放在 record.args 里，所以两处都要过：
    格式串本身，以及每一个字符串参数。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact(v) if isinstance(v, str) else v
                               for k, v in record.args.items()}
        return True


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
    # 脱敏挂在 root 和 uvicorn 的访问日志上——凭据从哪条路进来都要抹掉
    redactor = _RedactSecrets()
    for name in ("", "uvicorn.access", "uvicorn.error", "agent_platform"):
        logging.getLogger(name).addFilter(redactor)

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


def warn_if_secrets_are_readable() -> list[str]:
    """.env 里有密钥，权限松就提醒一句（回提醒过的文件，便于测试）。

    2026-08-29 实测：.env 是 0644，里面有 DEEPSEEK_API_KEY（付费）、
    API_TOKEN、LOCAL_MODEL_API_KEY——同机其他用户直接可读。

    这个文件是用户自己建的，不该由程序替他改权限（那是越权，
    而且他可能有意共享给同组）。提醒一句就够：他看得见、也知道怎么办。
    库文件不一样，那是程序自己建的，所以在 Storage.initialize 里直接收。

    同组可读也算松——这台机器上好几个人在同一个组里。
    """
    loose: list[str] = []
    for name in (".env", ".env.local"):
        path = Path(name)
        try:
            if not path.exists():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            loose.append(name)
            logging.getLogger("agent_platform").warning(
                "%s 的权限是 %s，同机其他用户能读到里面的密钥。"
                "建议：chmod 600 %s", name, oct(mode), name)
    return loose


def main() -> None:
    settings = get_settings()
    configure_logging()
    warn_if_secrets_are_readable()
    uvicorn.run("agent_platform.api:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
