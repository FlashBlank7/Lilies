"""统筹总览：跨工作流的今日运行、定时任务、近期失败聚合。

bench 北极星"统筹管理"支柱的数据面。同一份聚合供三个入口：
GET /api/v1/overview（bench 总览页）、管家工具 platform_overview（对话/CLI）、
后续的告警。只读，聚合不落库。
"""

from __future__ import annotations

# 统计口径（2026-08-28 修正）：只算**发布版的真实运行**（version IS NOT NULL）。
# 草稿自测运行（version 为空、带 draft_revision）是搭建过程中的中间产物——
# 真机上 314 条运行里 255 条是自测，此前全部混进统计：
# 体检会因搭建期的自测失败把工作流判成 broken，而 repair_workflow 在用户没给
# 指示时会拿这个判定去自动开修复构建（唯一会自动花钱的路径）。
_REAL_RUN = "version IS NOT NULL"

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

logger = logging.getLogger(__name__)


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def build_overview(services: Any) -> dict[str, Any]:
    storage = services.workflow_store.storage
    today = _today_prefix()

    def query() -> dict[str, Any]:
        with storage._connect() as conn:
            # 归档过滤：失败清单、应用列表、体检都加了，today 和本周没加，
            # 于是面板自己跟自己打架——真机 2026-08-29 量到本周失败 25 次，
            # 而清单最多只解释得了 13 次，另外 12 次属于已归档的工作流：
            # 数字在，出处查不到。口径要么都算要么都不算，不能一半一半。
            runs_today = dict(conn.execute(
                "SELECT r.status, COUNT(*) FROM workflow_runs r "
                "JOIN applications a ON a.id=r.application_id "
                f"WHERE r.created_at LIKE ? AND a.archived_at IS NULL AND r.{_REAL_RUN} "
                "GROUP BY r.status",
                (f"{today}%",),
            ).fetchall())
            failures = [dict(r) for r in conn.execute(
                # **在 SQL 里数，别取回来再数。**
                #
                # 这个 bug 修过一次没修透：最早是 LIMIT 8，合并在 Python 里做，
                # 于是次数封顶在 8（线上真值 13 显示成 ×8，少报 38%）。
                # 那次改成 LIMIT 500——数量级换了，病还是同一个：
                # 一旦未归档工作流的失败记录超过 500 条，次数又开始少报，
                # 而且是**静默**的：面板上还是个数，只是变小了。
                # 真机现在 13 条，离 500 还远，正因如此才要现在改：
                # 等撞上的时候，看到的是一个不响的错数。
                #
                # GROUP BY 之后再没有这个上限——每个原因的次数是全量真值。
                # 一个聚合的 MIN/MAX 配裸列时，SQLite 保证裸列取自那一行，
                # 所以 r.id 就是最近那次的编号（不是随便一行）。
                "SELECT a.name, COUNT(*) AS n, "
                "MAX(substr(r.created_at,1,19)) AS at, r.id, "
                # 失败原因权威来源是顶层 error 列；state_json 里没有 error 字段
                # （WorkflowRunState 模型压根没这个字段），只留作老数据兜底。
                "COALESCE(r.error, json_extract(r.state_json,'$.error'), '') AS error "
                "FROM workflow_runs r JOIN applications a ON a.id=r.application_id "
                # 退休工作流的旧失败没必要继续占着面板
                f"WHERE r.status='failed' AND a.archived_at IS NULL AND r.{_REAL_RUN} "
                "GROUP BY a.name, error "
                # 这里的 300 截的是「有多少种不同的毛病」，不是「出现过几次」。
                # 截掉的是最久没再犯的那些种类，每一种的次数都仍是全量。
                "ORDER BY at DESC LIMIT 300"
            ).fetchall()]
            week_rows = conn.execute(
                "SELECT substr(r.created_at,1,10) AS day, r.status, COUNT(*) AS n "
                "FROM workflow_runs r JOIN applications a ON a.id=r.application_id "
                "WHERE r.created_at >= date('now','-6 days') "
                f"AND a.archived_at IS NULL AND r.{_REAL_RUN} GROUP BY day, r.status",
            ).fetchall()
            # 每天的失败**分别属于谁**。
            #
            # 起因（2026-08-29 真机）：问「昨天一共有几次失败」，
            # 答「5 次。其中「文本行数与净字数统计」有一次失败记录」——
            # 5 是对的（从周视图读的），"一次"是错的（那 5 次全是它）。
            # 周视图只有每天的总数，失败清单是**整窗合并**的（一个原因一行），
            # 于是"某天的总数"和"哪个工作流"之间没有任何东西连着，
            # 只能猜；而它把清单里的一**行**读成了一**次**。
            #
            # 这个数平台一句 SQL 就有。真机上近 7 天只有 2 行、108 字符——
            # 便宜到没有理由让它去猜。
            week_failures = [dict(r) for r in conn.execute(
                "SELECT substr(r.created_at,1,10) AS day, a.name AS workflow, "
                "COUNT(*) AS failed "
                "FROM workflow_runs r JOIN applications a ON a.id=r.application_id "
                "WHERE r.status='failed' AND r.created_at >= date('now','-6 days') "
                f"AND a.archived_at IS NULL AND r.{_REAL_RUN} "
                "GROUP BY day, a.name ORDER BY day DESC, failed DESC LIMIT 60"
            ).fetchall()]
            builds_active = int(conn.execute(
                "SELECT COUNT(*) FROM builds WHERE status IN ('queued','building')"
            ).fetchone()[0])
            apps = [dict(r) for r in conn.execute(
                # 收起来的不算：业主收它就是为了「别再管它」，
                # 还数进「已发布」会出现收拾完数字反而变大的怪事
                "SELECT id, name, active_version FROM applications "
                "WHERE active_version IS NOT NULL AND archived_at IS NULL"
            ).fetchall()]
            fires = {r["application_id"]: dict(r) for r in conn.execute(
                "SELECT application_id, MAX(created_at) AS last_fired, "
                "MAX(local_date) AS local_date FROM schedule_fires GROUP BY application_id"
            ).fetchall()}
            # 定时是否开火由**发布版**快照决定（scheduler 读的就是它）；
            # 草稿里加了/删了 schedule_trigger 但没发布，调度器根本不知道。
            drafts = {r["application_id"]: r["snapshot_json"] for r in conn.execute(
                "SELECT v.application_id, v.snapshot_json FROM application_versions v "
                "JOIN applications a ON a.id=v.application_id AND a.active_version=v.version"
            ).fetchall()}
        return {"runs_today": runs_today, "failures": failures, "week_rows": week_rows,
                "week_failures": week_failures, "builds_active": builds_active,
                "apps": apps, "fires": fires, "drafts": drafts}

    data = await asyncio.to_thread(query)

    schedules = []
    for app in data["apps"]:
        snapshot = data["drafts"].get(app["id"])
        if not snapshot:
            continue
        try:
            nodes = json.loads(snapshot)["workflow"]["nodes"]
        except Exception as error:  # noqa: BLE001 - 一个坏快照不该让整块统计塌掉
            # 静默 continue 的后果不轻：这个工作流的定时会从面板上消失，
            # 而体检里「有定时却没跑起来」也是基于这张表——
            # 也就是说一个定时任务可以无声地脱离监控，而且看上去像"它没有定时"。
            # 跳过是对的（一个坏快照不能拖垮整块统计），但必须留痕。
            logger.warning("读不出「%s」的流程图，它的定时不会出现在面板上：%s",
                           app.get("name") or app["id"], error)
            continue
        for node in nodes:
            if node.get("type") == "schedule_trigger":
                config = node.get("config") or {}
                fire = data["fires"].get(app["id"]) or {}
                schedules.append({
                    "workflow": app["name"], "application_id": app["id"],
                    "at": f"{int(config.get('hour', 0)):02d}:{int(config.get('minute', 0)):02d}",
                    "timezone": config.get("timezone", "UTC"),
                    "last_fired": fire.get("last_fired"),
                    "last_fire_date": fire.get("local_date"),
                })
    week: dict[str, dict[str, int]] = {}
    for row in data["week_rows"]:
        day = week.setdefault(row["day"], {"ok": 0, "fail": 0, "other": 0})
        if row["status"] == "succeeded":
            day["ok"] += row["n"]
        elif row["status"] == "failed":
            day["fail"] += row["n"]
        else:
            day["other"] += row["n"]
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    days = [( _dt.now(_tz.utc) - _td(days=offset)).strftime("%Y-%m-%d")
            for offset in range(6, -1, -1)]
    week_list = [{"date": day, **week.get(day, {"ok": 0, "fail": 0, "other": 0})}
                 for day in days]

    runs_today = data["runs_today"]
    all_failures = _dedupe_failures(data["failures"])
    return {
        "date_utc": today,
        "runs_today": {
            "total": sum(runs_today.values()),
            "succeeded": runs_today.get("succeeded", 0),
            "failed": runs_today.get("failed", 0),
            "running": runs_today.get("running", 0) + runs_today.get("queued", 0),
        },
        "builds_active": data["builds_active"],
        "schedules": schedules,
        # 合并之后再截：截的是「不同的毛病」，不是「行数」
        "recent_failures": all_failures[:8],
        # 一共有几种不同的毛病。截了就要说出来——不说的话，
        # 面板上 5 行看着像"就这些"，而第 6 种可能才是要命的那个。
        # （本周第四次同一个形状：给一页、不说这是一页。）
        "recent_failures_total": len(all_failures),
        "published_workflows": len(data["apps"]),
        "week": week_list,
        # 每天的失败分别属于哪个工作流（近 7 天）。week 只有每天的总数，
        # 这一份把总数拆到人头上——不然"昨天 5 次失败"是谁干的只能猜。
        "week_failures": data["week_failures"],
    }


def _last_expected_fire(config: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """定时节点配置 → 上一次本该开火的时刻（UTC）。配置读不动就返回 None。

    定时任务最常见的静默失效不是"从没跑过"，而是"跑过、然后悄悄不跑了"——
    调度器挂了、时区改了、发布版被换掉。只看有没有跑过是抓不到的。
    """
    try:
        hour = int(config.get("hour", 0))
        minute = int(config.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        zone_name = str(config.get("timezone") or "UTC")
        try:
            zone = ZoneInfo(zone_name)
        except Exception:  # noqa: BLE001 - 时区名不认识就按 UTC 算，别整个不判
            zone = timezone.utc
    except (TypeError, ValueError):
        return None
    moment = (now or datetime.now(timezone.utc)).astimezone(zone)
    today_fire = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < today_fire:                       # 今天还没到点，上一次是昨天
        today_fire -= timedelta(days=1)
    return today_fire.astimezone(timezone.utc)


def _was_live_at_last_fire(config: dict[str, Any], published_at: str | None,
                           now: datetime | None = None) -> bool:
    """上一次该开火的时刻，这个定时上线了没有。

    取不到发布时刻就当作"上线了"——宁可误报，也别把真逾期漏了。
    """
    if not published_at:
        return True
    expected = _last_expected_fire(config, now)
    if expected is None:
        return True
    try:
        live_since = datetime.fromisoformat(str(published_at))
    except ValueError:
        return True
    if live_since.tzinfo is None:
        live_since = live_since.replace(tzinfo=timezone.utc)
    return live_since <= expected


def _overdue(config: dict[str, Any], last_fired: str | None,
             now: datetime | None = None, grace_minutes: int = 90,
             published_at: str | None = None) -> tuple[bool, str]:
    """该开火却没开火？返回 (是否逾期, 本该开火的时刻文本)。

    宽限 90 分钟：调度器有抖动、任务本身也要跑一会儿，卡太紧会天天误报。

    published_at 是这个定时上线的时刻。没有它就会误报一整类情况：
    下午两点发布一个「每天 8:00」的工作流，last_fired 是空，
    而"上一次该开火的时刻"是今早 8 点——于是刚设好就被判「有定时却没跑起来」。
    用户前脚发布、后脚看见面板说它坏了。
    本该开火的时刻早于上线时刻的，那一炮它没赶上，不算它的账。
    """
    expected = _last_expected_fire(config, now)
    if expected is None:
        return False, ""
    label = expected.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not _was_live_at_last_fire(config, published_at, now):
        return False, label                       # 那一炮响的时候它还没上线
    if not last_fired:
        return True, label                        # 有定时、从没开过火
    try:
        fired = datetime.fromisoformat(str(last_fired))
    except ValueError:
        return False, label
    if fired.tzinfo is None:
        fired = fired.replace(tzinfo=timezone.utc)
    return fired < expected - timedelta(minutes=grace_minutes), label


# 调用方错误：工作流本身没毛病，是这次调用没按输入声明来。
# 不能算进"坏了"——否则任何人从 CLI 试跑忘了带参数，都会把工作流刷成 broken，
# 进而可能触发自动修复构建（花钱）。真机上就是被冒烟脚本刷出来的。
_TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")
_IN_FLIGHT_STATUSES = ("queued", "running", "paused")

_CALLER_ERROR_MARKERS = (
    "missing required input",
    "input validation failed",
    "unknown input",
    "输入校验",
)


def _is_caller_error(error: str) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in _CALLER_ERROR_MARKERS)


# 平台自身拥塞：数据库繁忙、连接排队——不是工作流写错了
_INFRA_ERROR_MARKERS = ("database is locked", "database table is locked",
                        "platform-congestion")


def _is_infra_error(error: str) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in _INFRA_ERROR_MARKERS)


def _not_the_workflows_fault(error: str) -> bool:
    """这次失败该不该记在工作流头上。"""
    return _is_caller_error(error) or _is_infra_error(error)


# 运行失败的常见原因 → 人话。措辞换掉，名字留着：
# 业主要拿那个名字去改东西，翻没了等于没说。
_RUN_ERROR_RULES = (
    (re.compile(r"missing required input:\s*(\S+)"), "缺少必填输入「{0}」"),
    # 这是线上最大一族失败（72 条）。原本译成「引用了不存在的节点」是**反的**：
    # 节点就在图上，取不到的是它的某一项产出。照这个译文去找一个不存在的节点，
    # 只会白忙。container_type 恰好区分了两种真因，一并说清楚：
    #   NoneType → 那一步压根没产出      dict → 产出里没有这一项
    (re.compile(r"could not resolve node='([^']+)' path=\['([^']*)'\]"
                r"(?:[^;]*;)*\s*container_type=(\w+)"),
     "取不到「{0}」的「{1}」——{2}"),
    (re.compile(r"could not resolve node='([^']+)' path=\['([^']*)'\]"),
     "取不到「{0}」的「{1}」"),
    (re.compile(r"workflow reference could not resolve node='([^']+)'"),
     "取不到「{0}」的产出"),
    (re.compile(r"collection expression requires an array"), "汇总的对象不是一个列表"),
    (re.compile(r"record_collection_normalize value must resolve"),
     "要汇总的数据不是一组记录"),
    (re.compile(r"unknown node type '?([\w-]+)'?"), "用了平台没有的节点类型「{0}」"),
    (re.compile(r"^'([^']*)'$"), "配置里取了一个不存在的字段「{0}」"),
    (re.compile(r"connection|timed? ?out|timeout", re.I), "连不上外部服务或等待超时"),
)


def _dedupe_failures(failures: list) -> list[dict[str, Any]]:
    """同一个工作流的同一个原因只占一行，带上次数。

    客户端只显示前几条：同因重复会把别的工作流的问题挤出屏幕，
    真机上就发生过——5 条重复盖掉了另一个工作流的两个不同问题。
    保留最先出现的那条（上游按时间倒序给），次数累加。

    上游已经按 (工作流, **原始**报错) 在 SQL 里聚合过了，每行自带 n。
    这里还要再合一次，因为翻译会把不同的原文归到同一句人话
    （比如同一族错误的不同措辞）。所以次数是 **累加 n**，不是 +1——
    写成 +1 的话，一句人话下面藏着的几十次会被压成"几种"。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in failures:
        error = _human_error(item.get("error") or "")
        key = (item["name"], error)
        times = int(item.get("n") or 1)
        existing = merged.get(key)
        if existing is None:
            merged[key] = {"run_id": str(item["id"])[:8], "workflow": item["name"],
                           "at": item["at"], "error": error, "count": times}
        else:
            existing["count"] += times
            # 上游按 at 倒序给，先来的那条更近；万一顺序变了也认时间
            if str(item.get("at") or "") > str(existing["at"] or ""):
                existing["at"] = item["at"]
                existing["run_id"] = str(item["id"])[:8]
    return list(merged.values())


def _human_error(error: str) -> str:
    """把一行运行报错换成业主看得懂的说法；认不出来的原样返回。

    认不出就原样返回是有意的：运行报错里常常带着节点自己抛的中文说明，
    硬套模板反而会把真正有用的话盖掉。
    """
    text = _brief_error(error)
    if not text:
        return ""
    # 判据在**未截断**的原文上跑：_brief_error 砍到 110 字，
    # 而 container_type 恰好在那之后——截完再匹配就永远读不到真因
    full = " ".join(str(error or "").split())
    for pattern, template in _RUN_ERROR_RULES:
        match = pattern.search(full) or pattern.search(text)
        if match:
            if not match.groups():
                return template
            groups = list(match.groups())
            if len(groups) == 3:      # 第三组是 container_type，翻成真因
                groups[2] = {"NoneType": "那一步没有产出",
                             "dict": "它的产出里没有这一项",
                             "list": "它的产出是个列表，没有这一项"}.get(
                                 groups[2], f"拿到的是 {groups[2]}")
            return template.format(*groups)
    return text


def _brief_error(error: str) -> str:
    """错误文本 → 一行摘要：剥掉 "node X failed: " 前缀、砍到首个换行、限长。
    体检要让人不点进去就知道大概是什么毛病。"""
    text = " ".join(str(error or "").split())
    if not text:
        return ""
    marker = " failed: "
    index = text.find(marker)
    if 0 <= index < 60:
        text = text[index + len(marker):]
    return text[:110]


async def build_health(services: Any, days: int = 7) -> dict[str, Any]:
    """工作流健康度：谁悄悄坏了。

    只看已发布的工作流（草稿没跑过是正常的）。三档：
    - broken：窗口内跑过但一次没成过，或最近一次运行失败且连续失败 >= 3
    - stale：有定时节点，但窗口内一次都没运行（调度没触发/被停了）
    - ok：其余
    """
    storage = services.workflow_store.storage

    def query() -> dict[str, Any]:
        with storage._connect() as conn:
            apps = [dict(r) for r in conn.execute(
                # 收起来的不体检：调度器也不再触发它（list_applications 会过滤），
                # 继续检查的话它永远满足「有定时却没运行」→ 永远被判 stale
                "SELECT id, name FROM applications "
                "WHERE active_version IS NOT NULL AND archived_at IS NULL"
            ).fetchall()]
            # 「这辈子跑过没有」——不带时间窗。
            # 下面那份 stats 全是窗口内的数（连 last_run 也是），
            # 于是"从没跑过"和"最近没跑"在报告里长得一模一样：
            # 都是 runs=0、last_run=None。可这两件事差得远——
            # 一个是安静，另一个是**压根没验过，第一次跑会怎样谁也不知道**。
            # 真机上这句查询 2.1 ms、12 个应用，代价可以忽略。
            ever = {r["application_id"]: dict(r) for r in conn.execute(
                "SELECT application_id, COUNT(*) AS runs, MAX(created_at) AS last_run "
                f"FROM workflow_runs WHERE {_REAL_RUN} GROUP BY application_id"
            ).fetchall()}
            stats = {r["application_id"]: dict(r) for r in conn.execute(
                "SELECT application_id, COUNT(*) AS runs, "
                "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded, "
                # 只有出了终态的运行才能用来判"坏没坏"——
                # 正在跑、排队中、等人工确认的都还没有结论
                "SUM(CASE WHEN status IN ('succeeded','failed','cancelled') "
                "  THEN 1 ELSE 0 END) AS terminal_runs, "
                "SUM(CASE WHEN status IN ('queued','running','paused') "
                "  THEN 1 ELSE 0 END) AS in_flight, "
                "SUM(CASE WHEN status='failed' AND "
                "  COALESCE(error, json_extract(state_json,'$.error'), '') "
                "  LIKE '%missing required input%' THEN 1 ELSE 0 END) AS caller_errors, "
                # 平台自身拥塞（数据库繁忙等）不是工作流的错
                "SUM(CASE WHEN status='failed' AND "
                "  COALESCE(error, json_extract(state_json,'$.error'), '') "
                "  LIKE '%database is locked%' THEN 1 ELSE 0 END) AS infra_errors, "
                "MAX(CASE WHEN status='succeeded' THEN created_at END) AS last_success, "
                "MAX(created_at) AS last_run "
                f"FROM workflow_runs WHERE created_at >= date('now','-{int(days) - 1} days') "
                f"AND {_REAL_RUN} GROUP BY application_id"
            ).fetchall()}
            # 每个应用最近 5 次运行的状态，用来数"连续失败"
            recent: dict[str, list[tuple[str, str]]] = {}
            # 每个应用**各取**最近 8 条，不是全局取 400 条再分桶。
            # 全局窗口的毛病和 recent_failures 那次一模一样：一个忙碌的工作流
            # 就能把别人挤出窗口，于是安静工作流的连败根本看不到，
            # 体检照样报「0 个反复失败」。真机上单个工作流已占全部运行的 56%，
            # 现在 124 条还没撑破 400，但方向是明确的。
            # 带上 error 是为了把「调用方没给参数」这类失败排除在连败判定之外。
            for row in conn.execute(
                "SELECT application_id, status, error FROM ("
                "  SELECT application_id, status, "
                "    COALESCE(error, json_extract(state_json,'$.error'), '') AS error, "
                "    ROW_NUMBER() OVER (PARTITION BY application_id "
                "                       ORDER BY created_at DESC) AS rn "
                f"  FROM workflow_runs WHERE {_REAL_RUN}"
                ") WHERE rn <= 8"
            ).fetchall():
                recent.setdefault(row["application_id"], []).append(
                    (row["status"], row["error"]))
            # 每个应用最近一条失败原因。同样按应用分窗，不用全局 LIMIT——
            # 全局取 200 条的话，一个高频失败的工作流会把别人的原因全挤掉，
            # 于是那些工作流被判成「反复失败」却给不出原因。
            errors: dict[str, str] = {
                row["application_id"]: str(row["error"] or "")
                for row in conn.execute(
                    "SELECT application_id, error FROM ("
                    "  SELECT application_id, "
                    "    COALESCE(error, json_extract(state_json,'$.error'), '') AS error, "
                    "    ROW_NUMBER() OVER (PARTITION BY application_id "
                    "                       ORDER BY created_at DESC) AS rn "
                    f"  FROM workflow_runs WHERE status='failed' AND {_REAL_RUN}"
                    ") WHERE rn = 1"
                ).fetchall()}
            version_rows = conn.execute(
                "SELECT v.application_id, v.snapshot_json, v.created_at "
                "FROM application_versions v "
                "JOIN applications a ON a.id=v.application_id AND a.active_version=v.version"
            ).fetchall()
            drafts = {r["application_id"]: r["snapshot_json"] for r in version_rows}
            # 发布时刻：判"该开火却没开火"要用它排除掉"那时它还没上线"
            published_at = {r["application_id"]: r["created_at"] for r in version_rows}
            fires = {r["application_id"]: r["last_fired"] for r in conn.execute(
                "SELECT application_id, MAX(created_at) AS last_fired "
                "FROM schedule_fires GROUP BY application_id"
            ).fetchall()}
        return {"apps": apps, "stats": stats, "recent": recent, "ever": ever,
                "drafts": drafts, "errors": errors, "fires": fires,
                "published_at": published_at}

    data = await asyncio.to_thread(query)

    items: list[dict[str, Any]] = []
    for app in data["apps"]:
        stat = data["stats"].get(app["id"]) or {}
        runs = int(stat.get("runs") or 0)
        succeeded = int(stat.get("succeeded") or 0)
        streak = 0
        for entry in data["recent"].get(app["id"], []):
            status, error = (entry if isinstance(entry, tuple) else (entry, ""))
            if status in _IN_FLIGHT_STATUSES:
                continue          # 还没有结论，跳过
            if status == "failed" and _not_the_workflows_fault(error):
                continue          # 调用方传错参数 / 平台拥塞：不计数也不中断
            if status == "failed":
                streak += 1
            else:
                break
        scheduled = False
        schedule_config: dict[str, Any] = {}
        snapshot = data["drafts"].get(app["id"])
        if snapshot:
            try:
                for node in json.loads(snapshot)["workflow"]["nodes"]:
                    if node.get("type") == "schedule_trigger":
                        scheduled = True
                        schedule_config = node.get("config") or {}
                        break
            except Exception:  # noqa: BLE001 - 快照坏了不影响体检其余部分
                scheduled = False
        last_fired = (data.get("fires") or {}).get(app["id"])
        live_at = (data.get("published_at") or {}).get(app["id"])
        was_live = _was_live_at_last_fire(schedule_config, live_at) if scheduled else True
        overdue, expected_at = (
            _overdue(schedule_config, last_fired, published_at=live_at)
            if scheduled else (False, ""))

        raw_error = data["errors"].get(app["id"], "")
        last_error = "" if _not_the_workflows_fault(raw_error) else _human_error(raw_error)
        caller_errors = int(stat.get("caller_errors") or 0)
        infra_errors = int(stat.get("infra_errors") or 0)
        in_flight = int(stat.get("in_flight") or 0)
        # 判"坏没坏"只看有结论的运行：调用方传错参数、平台自身拥塞都不算工作流的错
        terminal = int(stat.get("terminal_runs") or 0) - caller_errors - infra_errors
        if terminal > 0 and not succeeded:
            state, reason = "broken", f"近{days}天 {terminal} 次运行全部失败"
        elif streak >= 3:
            state, reason = "broken", f"最近连续失败 {streak} 次"
        elif in_flight and terminal == 0:
            # 还在跑/等人工确认——没有结论，不能当成坏了
            # （此前会判 broken，而 repair_workflow 在没给指示时会拿这个判定自动开构建）
            state, reason = "waiting", "有运行在进行或等待人工确认，尚无终态结果"
        elif overdue and last_fired:
            # 跑过、然后悄悄不跑了——定时任务最常见的静默失效
            state, reason = "stale", (
                f"定时没按时开火：上次 {str(last_fired)[:16].replace('T', ' ')}，"
                f"本该 {expected_at}")
        elif scheduled and was_live and (overdue or not runs):
            # was_live：上一炮响的时候它上线了没有。
            # 少了这个判断，刚发布的定时工作流因为"窗口内零运行"直接判 stale——
            # 和上面 overdue 那条是同一个误报，只是走了另一条分支。
            # 同一个判据没铺满所有分支，今天已经是第 N 次。
            state, reason = "stale", f"有定时任务，但近{days}天一次都没运行"
        else:
            state, reason = "ok", ""
        if state == "broken" and last_error:
            reason = f"{reason}：{last_error}"
        items.append({
            "application_id": app["id"], "workflow": app["name"], "state": state,
            "reason": reason, "last_error": last_error,
            "runs": runs, "succeeded": succeeded,
            "fail_streak": streak, "scheduled": scheduled,
            "last_fired": last_fired, "overdue": overdue,
            "last_success": stat.get("last_success"), "last_run": stat.get("last_run"),
            # 上面两个是**窗口内**的（名字不像，别被骗）；这两个才是全历史
            "ever_ran": bool((data["ever"].get(app["id"]) or {}).get("runs")),
            "last_run_ever": (data["ever"].get(app["id"]) or {}).get("last_run"),
        })

    rank = {"broken": 0, "stale": 1, "waiting": 2, "ok": 3}
    items.sort(key=lambda item: (rank[item["state"]], -item["runs"]))
    return {
        "days": days,
        "counts": {state: sum(1 for i in items if i["state"] == state)
                   for state in ("broken", "stale", "waiting", "ok")},
        "items": items,
        # 单列一格，不进 counts、不新增状态、不动前端渲染。
        #
        # 为什么单列：发布了但一次都没跑过的工作流，在四个状态里会落到 ok
        # （没定时 → 不 stale；没终态 → 不 broken；没在跑 → 不 waiting），
        # 于是面板说"正常"、管家答"都正常"。可"正常"是个结论，
        # 而这种工作流一条证据都没有——它可能第一次跑就炸。
        # 不判成"有问题"也是对的：它确实没坏，刚发布的工作流都要经过这个阶段，
        # 判成问题就是天天报警。所以既不说它好、也不说它坏，把事实摆出来。
        "never_ran": [i["workflow"] for i in items if not i["ever_ran"]],
    }
