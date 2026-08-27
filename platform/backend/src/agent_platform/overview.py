"""统筹总览：跨工作流的今日运行、定时任务、近期失败聚合。

bench 北极星"统筹管理"支柱的数据面。同一份聚合供三个入口：
GET /api/v1/overview（bench 总览页）、管家工具 platform_overview（对话/CLI）、
后续的告警。只读，聚合不落库。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def build_overview(services: Any) -> dict[str, Any]:
    storage = services.workflow_store.storage
    today = _today_prefix()

    def query() -> dict[str, Any]:
        with storage._connect() as conn:
            runs_today = dict(conn.execute(
                "SELECT status, COUNT(*) FROM workflow_runs WHERE created_at LIKE ? GROUP BY status",
                (f"{today}%",),
            ).fetchall())
            failures = [dict(r) for r in conn.execute(
                "SELECT r.id, r.application_id, substr(r.created_at,1,19) AS at, "
                "json_extract(r.state_json,'$.error') AS error, a.name "
                "FROM workflow_runs r JOIN applications a ON a.id=r.application_id "
                "WHERE r.status='failed' ORDER BY r.created_at DESC LIMIT 8"
            ).fetchall()]
            week_rows = conn.execute(
                "SELECT substr(created_at,1,10) AS day, status, COUNT(*) AS n "
                "FROM workflow_runs WHERE created_at >= date('now','-6 days') "
                "GROUP BY day, status",
            ).fetchall()
            builds_active = int(conn.execute(
                "SELECT COUNT(*) FROM builds WHERE status IN ('queued','building')"
            ).fetchone()[0])
            apps = [dict(r) for r in conn.execute(
                "SELECT id, name, active_version FROM applications WHERE active_version IS NOT NULL"
            ).fetchall()]
            fires = {r["application_id"]: dict(r) for r in conn.execute(
                "SELECT application_id, MAX(created_at) AS last_fired, "
                "MAX(local_date) AS local_date FROM schedule_fires GROUP BY application_id"
            ).fetchall()}
            drafts = {r["application_id"]: r["snapshot_json"] for r in conn.execute(
                "SELECT application_id, snapshot_json FROM application_drafts"
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
        "recent_failures": [
            {"run_id": f["id"][:8], "workflow": f["name"], "at": f["at"],
             "error": (f.get("error") or "")[:120]}
            for f in data["failures"]
        ],
        "published_workflows": len(data["apps"]),
        "week": week_list,
    }


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
                "SELECT id, name FROM applications WHERE active_version IS NOT NULL"
            ).fetchall()]
            stats = {r["application_id"]: dict(r) for r in conn.execute(
                "SELECT application_id, COUNT(*) AS runs, "
                "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded, "
                "MAX(CASE WHEN status='succeeded' THEN created_at END) AS last_success, "
                "MAX(created_at) AS last_run "
                f"FROM workflow_runs WHERE created_at >= date('now','-{int(days) - 1} days') "
                "GROUP BY application_id"
            ).fetchall()}
            # 每个应用最近 5 次运行的状态，用来数"连续失败"
            recent: dict[str, list[str]] = {}
            for row in conn.execute(
                "SELECT application_id, status FROM workflow_runs "
                "ORDER BY created_at DESC LIMIT 400"
            ).fetchall():
                bucket = recent.setdefault(row["application_id"], [])
                if len(bucket) < 5:
                    bucket.append(row["status"])
            drafts = {r["application_id"]: r["snapshot_json"] for r in conn.execute(
                "SELECT application_id, snapshot_json FROM application_drafts"
            ).fetchall()}
        return {"apps": apps, "stats": stats, "recent": recent, "drafts": drafts}

    data = await asyncio.to_thread(query)

    items: list[dict[str, Any]] = []
    for app in data["apps"]:
        stat = data["stats"].get(app["id"]) or {}
        runs = int(stat.get("runs") or 0)
        succeeded = int(stat.get("succeeded") or 0)
        streak = 0
        for status in data["recent"].get(app["id"], []):
            if status == "failed":
                streak += 1
            else:
                break
        scheduled = False
        snapshot = data["drafts"].get(app["id"])
        if snapshot:
            try:
                scheduled = any(
                    node.get("type") == "schedule_trigger"
                    for node in json.loads(snapshot)["workflow"]["nodes"])
            except Exception:  # noqa: BLE001 - 快照坏了不影响体检其余部分
                scheduled = False

        if runs and not succeeded:
            state, reason = "broken", f"近{days}天 {runs} 次运行全部失败"
        elif streak >= 3:
            state, reason = "broken", f"最近连续失败 {streak} 次"
        elif scheduled and not runs:
            state, reason = "stale", f"有定时任务，但近{days}天一次都没运行"
        else:
            state, reason = "ok", ""
        items.append({
            "application_id": app["id"], "workflow": app["name"], "state": state,
            "reason": reason, "runs": runs, "succeeded": succeeded,
            "fail_streak": streak, "scheduled": scheduled,
            "last_success": stat.get("last_success"), "last_run": stat.get("last_run"),
        })

    rank = {"broken": 0, "stale": 1, "ok": 2}
    items.sort(key=lambda item: (rank[item["state"]], -item["runs"]))
    return {
        "days": days,
        "counts": {state: sum(1 for i in items if i["state"] == state)
                   for state in ("broken", "stale", "ok")},
        "items": items,
    }
