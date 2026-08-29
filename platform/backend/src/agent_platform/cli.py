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

    **不止根目录那两个**：第一版只看 CWD 下的 .env / .env.local，
    而 2026-08-29 扫下来，platform/frontend/.env.local 也是 0644，
    里面同样写着 API_TOKEN。密钥文件不止一个地方，检查也得不止一个地方——
    只查一处的检查，会让人以为"已经查过了"。

    .env.example 是模板，本来就该进版本库、本来就该人人可读，不算。
    """
    loose: list[str] = []
    for path in _env_files():
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            name = str(path)
            loose.append(name)
            logging.getLogger("agent_platform").warning(
                "%s 的权限是 %s，同机其他用户能读到里面的密钥。"
                "建议：chmod 600 %s", name, oct(mode), name)
    return loose


def _env_files() -> list[Path]:
    """要检查的密钥文件：几个固定目录下的 .env*，不递归。

    只在**目录**上写死，文件名用 glob。第一版把四个完整路径写死了，
    于是 .env.production 这种名字一个也查不到；而且那样一来
    「排除 .env.example」成了死代码——写死的清单里本来就没有它，
    测试跟着变成空断言（把排除逻辑删掉，测试照样绿，实测过）。

    不递归是有意的：递归会走进 node_modules 几万个目录，启动白等几秒，
    而密钥文件真正会出现的地方就这么几处。
    """
    places = [Path("."), Path("platform/frontend"), Path("platform/backend")]
    found: list[Path] = []
    for place in places:
        try:
            if not place.is_dir():
                continue
            for path in sorted(place.glob(".env*")):
                # 模板本来就该进版本库、本来就该人人可读，报它是噪音，
                # 而噪音会让人开始无视这类提醒
                if path.is_file() and not path.name.endswith((".example", ".sample")):
                    found.append(path)
        except OSError:
            continue
    return found


def warn_if_the_token_is_still_the_default(settings) -> str:
    """API_TOKEN 还是出厂那个 "change-me" 就提醒；对外开的话把话说重。

    这是配置类问题里最典型的一种：不改也能跑，跑起来一切正常，
    只是**门是虚掩的**。config.py 里 `api_token: str = "change-me"`
    是为了让人能一条命令跑起来，这没错；错的是跑起来之后没人再提醒他。

    对外开（host 不是回环）时另说一句：那时这个众所周知的口令
    就是整个平台的钥匙——密钥、连接器凭据、所有工作流都在它后面。

    **只提醒、不拒绝启动**，和上面查文件权限那条一个道理：
    本地随手跑一个来试试是正当用法，程序不该替用户下这个判断。
    回返回的等级（""/"warn"/"loud"），是为了能测。
    """
    if settings.api_token != "change-me":
        return ""
    logger = logging.getLogger("agent_platform")
    exposed = str(settings.host) not in {"127.0.0.1", "localhost", "::1"}
    if exposed:
        logger.warning(
            "API_TOKEN 还是出厂默认的 change-me，而服务绑在 %s（不是回环）——"
            "任何人只要知道这个默认口令就能拿到平台里的全部密钥和工作流。"
            "先设一个：在 .env 里写 API_TOKEN=<随机串>", settings.host)
        return "loud"
    logger.warning(
        "API_TOKEN 还是出厂默认的 change-me。现在只绑在本机，"
        "但同机其他用户照样连得上；正式用之前在 .env 里换掉。")
    return "warn"


def main() -> None:
    settings = get_settings()
    configure_logging()
    warn_if_secrets_are_readable()
    warn_if_the_token_is_still_the_default(settings)
    uvicorn.run("agent_platform.api:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
