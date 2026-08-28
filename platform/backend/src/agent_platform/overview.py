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
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def build_overview(services: Any) -> dict[str, Any]:
    storage = services.workflow_store.storage
    today = _today_prefix()

    def query() -> dict[str, Any]:
        with storage._connect() as conn:
            runs_today = dict(conn.execute(
                "SELECT status, COUNT(*) FROM workflow_runs "
                f"WHERE created_at LIKE ? AND {_REAL_RUN} GROUP BY status",
                (f"{today}%",),
            ).fetchall())
            failures = [dict(r) for r in conn.execute(
                "SELECT r.id, r.application_id, substr(r.created_at,1,19) AS at, "
                # 失败原因权威来源是顶层 error 列；state_json 里没有 error 字段
                # （WorkflowRunState 模型压根没这个字段），只留作老数据兜底。
                "COALESCE(r.error, json_extract(r.state_json,'$.error'), '') AS error, a.name "
                "FROM workflow_runs r JOIN applications a ON a.id=r.application_id "
                # 退休工作流的旧失败没必要继续占着面板
                f"WHERE r.status='failed' AND a.archived_at IS NULL AND r.{_REAL_RUN} "
                "ORDER BY r.created_at DESC LIMIT 8"
            ).fetchall()]
            week_rows = conn.execute(
                "SELECT substr(created_at,1,10) AS day, status, COUNT(*) AS n "
                "FROM workflow_runs WHERE created_at >= date('now','-6 days') "
                f"AND {_REAL_RUN} GROUP BY day, status",
            ).fetchall()
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
                "builds_active": builds_active, "apps": apps, "fires": fires, "drafts": drafts}

    data = await asyncio.to_thread(query)

    schedules = []
    for app in data["apps"]:
        snapshot = data["drafts"].get(app["id"])
        if not snapshot:
            continue
        try:
            nodes = json.loads(snapshot)["workflow"]["nodes"]
        except Exception:
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
        "recent_failures": _dedupe_failures(data["failures"]),
        "published_workflows": len(data["apps"]),
        "week": week_list,
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


def _overdue(config: dict[str, Any], last_fired: str | None,
             now: datetime | None = None, grace_minutes: int = 90) -> tuple[bool, str]:
    """该开火却没开火？返回 (是否逾期, 本该开火的时刻文本)。

    宽限 90 分钟：调度器有抖动、任务本身也要跑一会儿，卡太紧会天天误报。
    """
    expected = _last_expected_fire(config, now)
    if expected is None:
        return False, ""
    label = expected.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
    (re.compile(r"workflow reference could not resolve node='([^']+)'"),
     "引用了不存在的节点「{0}」"),
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
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in failures:
        error = _human_error(item.get("error") or "")
        key = (item["name"], error)
        existing = merged.get(key)
        if existing is None:
            merged[key] = {"run_id": item["id"][:8], "workflow": item["name"],
                           "at": item["at"], "error": error, "count": 1}
        else:
            existing["count"] += 1
    return list(merged.values())


def _human_error(error: str) -> str:
    """把一行运行报错换成业主看得懂的说法；认不出来的原样返回。

    认不出就原样返回是有意的：运行报错里常常带着节点自己抛的中文说明，
    硬套模板反而会把真正有用的话盖掉。
    """
    text = _brief_error(error)
    if not text:
        return ""
    for pattern, template in _RUN_ERROR_RULES:
        match = pattern.search(text)
        if match:
            return template.format(*match.groups()) if match.groups() else template
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
            recent: dict[str, list[str]] = {}
            for row in conn.execute(
                # 先过滤再截窗口：不然 400 条里塞满自测噪音，连败判定看不到真实运行。
                # 带上 error 是为了把"调用方没给参数"这类失败排除在连败判定之外。
                "SELECT application_id, status, "
                "COALESCE(error, json_extract(state_json,'$.error'), '') AS error "
                f"FROM workflow_runs WHERE {_REAL_RUN} "
                "ORDER BY created_at DESC LIMIT 400"
            ).fetchall():
                bucket = recent.setdefault(row["application_id"], [])
                if len(bucket) < 8:      # 放宽一点：调用方错误会被跳过，窗口太小容易看不到真实运行
                    bucket.append((row["status"], row["error"]))
            errors: dict[str, str] = {}
            for row in conn.execute(
                "SELECT application_id, "
                "COALESCE(error, json_extract(state_json,'$.error'), '') AS error "
                f"FROM workflow_runs WHERE status='failed' AND {_REAL_RUN} "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall():
                # 每个应用只留最近一条（结果按时间倒序，先见即最近）
                errors.setdefault(row["application_id"], str(row["error"] or ""))
            drafts = {r["application_id"]: r["snapshot_json"] for r in conn.execute(
                "SELECT v.application_id, v.snapshot_json FROM application_versions v "
                "JOIN applications a ON a.id=v.application_id AND a.active_version=v.version"
            ).fetchall()}
            fires = {r["application_id"]: r["last_fired"] for r in conn.execute(
                "SELECT application_id, MAX(created_at) AS last_fired "
                "FROM schedule_fires GROUP BY application_id"
            ).fetchall()}
        return {"apps": apps, "stats": stats, "recent": recent,
                "drafts": drafts, "errors": errors, "fires": fires}

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
        overdue, expected_at = (
            _overdue(schedule_config, last_fired) if scheduled else (False, ""))

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
        elif scheduled and (overdue or not runs):
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
        })

    rank = {"broken": 0, "stale": 1, "waiting": 2, "ok": 3}
    items.sort(key=lambda item: (rank[item["state"]], -item["runs"]))
    return {
        "days": days,
        "counts": {state: sum(1 for i in items if i["state"] == state)
                   for state in ("broken", "stale", "waiting", "ok")},
        "items": items,
    }
