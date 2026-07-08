#!/usr/bin/env python3
"""
使用 Lilies Builder Team 搭建「钉钉智能打卡」工作流。

核心决策引擎:
  1. 急速打卡优先 (打开App即触发)
  2. 急速打卡失败 → 降级为 input tap 模拟点击
  3. 模拟点击失败 → human_input 通知手动打卡
  4. 全程截图留证

用法:
  python build_dingtalk_punch.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings


def header():
    return {"Authorization": "Bearer test-token-2024"}


def main():
    tmp = TemporaryDirectory()
    tmp_path = Path(tmp.name)
    s = Settings(
        api_token="test-token-2024",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    s.prepare()
    (tmp_path / "workspaces").mkdir(parents=True, exist_ok=True)

    app = create_app(settings=s)

    with TestClient(app) as c:
        # ═══════════════════════════════════════════════════════════
        # Task 1: Builder Team 创建智能打卡工作流
        # ═══════════════════════════════════════════════════════════
        print("═" * 60)
        print("  Lilies Builder Team: 钉钉智能打卡工作流")
        print("═" * 60)

        punch_req = (
            "Create a DingTalk smart attendance punch workflow. "
            "The workflow MUST use schedule_trigger for timing (8:45 check-in, 19:00 check-out, Mon-Fri Asia/Shanghai). "
            "The punching strategy is a TWO-TIER fallback system:\n\n"
            "Tier 1 (preferred): 'Quick Punch' — simply launch the DingTalk app via an Android shell command. "
            "DingTalk's built-in quick-punch feature will auto-complete if the user is within the geofence and time window. "
            "Use a Bash tool to run: am start -n com.alibaba.android.rimet/.LaunchHomeActivity\n\n"
            "Tier 2 (fallback): 'Tap Simulation' — if quick punch fails (detected via screenshot analysis "
            "or notification check), use Android input tap X Y to simulate tapping the check-in button. "
            "The tap coordinates come from workflow inputs (configurable per device). "
            "Use a Bash tool to run input tap commands.\n\n"
            "Tier 3 (last resort): Send a notification to the user to manually punch. "
            "Use a human_input block to pause and wait for confirmation.\n\n"
            "The workflow should:\n"
            "1. Accept inputs: checkin_x, checkin_y, checkout_x, checkout_y, entry_x, entry_y, device_timezone\n"
            "2. Use an LLM to decide which tier to use based on previous results\n"
            "3. Use template_transform to format the result message\n"
            "4. Track punch history via outputs\n"
            "5. Include mandatory tests that verify the structure uses tool (Bash) blocks, not just LLM\n\n"
            "IMPORTANT: Use the Bash tool (tool_name='Bash') for all shell commands. "
            "Use if_else for branching logic. Use variable_aggregator to merge results from different branches."
        )

        # Create application
        app_r = c.post("/api/v1/applications", headers=header(), json={
            "name": "钉钉智能打卡",
            "requirement": punch_req,
        })
        print(f"应用创建: {app_r.status_code}")
        app_id = app_r.json()["id"]
        print(f"App ID: {app_id[:8]}...")

        # Start build
        b_r = c.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=header(),
            json={
                "requirement": punch_req,
                "auto_publish": True,
                "max_turns": 40,
                "max_repair_cycles": 6,
            },
        )
        print(f"Build 启动: {b_r.status_code}")
        build_id = b_r.json()["build_id"]
        print(f"Build ID: {build_id[:8]}...\n")

        # Monitor build progress
        for i in range(300):
            b = c.get(f"/api/v1/builds/{build_id}", headers=header()).json()
            status = b.get("status", "?")
            if i % 15 == 0:
                team_state = b.get("team_state", {})
                revision = team_state.get("revision", 0)
                tasks = team_state.get("tasks", [])
                done = sum(1 for t in tasks if t.get("status") == "completed")
                print(f"  [{i}s] status={status} revision={revision} tasks={done}/{len(tasks)}")
            # Show build events
            events = c.get(f"/api/v1/builds/{build_id}/events?after={i}", headers=header()).json()
            for ev in events:
                etype = ev.get("type", "")
                if "operation" in etype or "published" in etype or "completed" in etype or "needs_attention" in etype:
                    data = ev.get("data", {})
                    tool = data.get("tool", "")
                    success = data.get("success", True)
                    marker = "✅" if success else "❌"
                    print(f"    {marker} [{data.get('actor', '?')}] {tool}")
            if status in ("published", "ready", "needs_attention", "cancelled", "failed"):
                break
            time.sleep(1)

        # Get final result
        b = c.get(f"/api/v1/builds/{build_id}", headers=header()).json()
        final_status = b.get("status", "?")
        print(f"\n最终状态: {final_status}")
        if b.get("error"):
            print(f"错误: {b['error'][:500]}")

        # Inspect the draft
        draft = c.get(f"/api/v1/applications/{app_id}/draft", headers=header()).json()
        nodes = draft["snapshot"]["workflow"]["nodes"]
        edges = draft["snapshot"]["workflow"]["edges"]
        tests = draft["snapshot"].get("tests", [])
        print(f"\n工作流: {len(nodes)} 节点, {len(edges)} 连线, {len(tests)} 测试")
        for n in nodes:
            print(f"  [{n['type']}] {n['title']}")
        for e in edges:
            print(f"  {e['source']} → {e['target']}")

        # Save the workflow
        out_path = Path(__file__).resolve().parent / "templates" / "dingtalk_smart_punch.json"
        template_data = {
            "meta": {
                "name": "dingtalk_smart_punch",
                "title": "钉钉智能打卡（双策略降级）",
                "description": (
                    "智能钉钉打卡工作流：优先使用急速打卡（启动App），"
                    "失败则降级为input tap模拟点击，最后回退到人工通知。"
                    "支持上班打卡(8:45)和下班签退(19:00)，周一至周五自动运行。"
                ),
                "category": "task_management",
                "icon": "clock",
                "tags": ["dingtalk", "checkin", "automation", "android", "tap-simulation"],
                "expected_inputs": {
                    "checkin_x": "number",
                    "checkin_y": "number",
                    "checkout_x": "number",
                    "checkout_y": "number",
                    "entry_x": "number",
                    "entry_y": "number",
                    "device_timezone": "string",
                },
                "expected_outputs": {
                    "result": "string",
                    "status": "string",
                    "screenshot_path": "string",
                },
                "author": "lilies-builder",
                "version": 1,
                "min_blocks_required": ["schedule_trigger", "tool", "if_else", "template_transform"],
            },
            "workflow": draft["snapshot"]["workflow"],
        }
        out_path.write_text(json.dumps(template_data, ensure_ascii=False, indent=2))
        print(f"\n✅ 模板已保存: {out_path}")

        # ── Run the workflow ──
        if final_status in ("published", "ready"):
            pub_ver = (
                (b.get("team_state") or {}).get("published_version")
                or b.get("published_version", 1)
            )
            print(f"\n运行工作流测试 (version={pub_ver})...")
            run_r = c.post(
                f"/api/v1/applications/{app_id}/runs",
                headers=header(),
                json={
                    "inputs": {
                        "checkin_x": 540,
                        "checkin_y": 1200,
                        "checkout_x": 540,
                        "checkout_y": 1200,
                        "entry_x": 540,
                        "entry_y": 1800,
                        "device_timezone": "Asia/Shanghai",
                    },
                    "version": pub_ver,
                    "workspace_path": ".",
                },
            )
            if run_r.status_code == 202:
                rid = run_r.json()["run_id"]
                for _ in range(120):
                    rec = c.get(f"/api/v1/runs/{rid}", headers=header()).json()
                    if rec["status"] in ("succeeded", "failed", "cancelled"):
                        break
                    time.sleep(0.5)
                rec = c.get(f"/api/v1/runs/{rid}", headers=header()).json()
                print(f"运行结果: {rec['status']}")
                for k, v in rec.get("outputs", {}).items():
                    print(f"  {k}: {str(v)[:200]}")

        # Show Builder activity
        events = c.get(f"/api/v1/builds/{build_id}/events", headers=header()).json()
        ops = [e for e in events if "operation" in e.get("type", "")]
        print(f"\nBuilder 操作记录 ({len(ops)} 步):")
        for ev in ops:
            d = ev.get("data", {})
            print(f"  [{d.get('actor','?')}] {d.get('tool','?')} success={d.get('success','?')}")

    try:
        tmp.cleanup()
    except Exception:
        pass

    print("\n" + "═" * 60)
    print("  ✅ 完成!")
    print("═" * 60)


if __name__ == "__main__":
    main()
