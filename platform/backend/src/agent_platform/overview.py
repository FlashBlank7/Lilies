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
