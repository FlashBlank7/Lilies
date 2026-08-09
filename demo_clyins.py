#!/usr/bin/env python3
"""
Clyins AI 项目经理 — 端到端演示

演示 Clyins BlockFlow 的完整生命周期：
1. 从模板市场加载 Clyins 模板
2. 创建 Application + 展开 Clyins 模板到草稿
3. 运行工作流（使用示例会议记录）
4. 展示生成的日程表和行动项

前置条件: Lilies 后端已启动 (./scripts/dev_platform.sh)
用法:
  python demo_clyins.py                    # 对运行中的 Lilies 进行测试
  python demo_clyins.py --dry-run          # 仅验证模板结构，不调用 API
"""

from __future__ import annotations

import json
import sys
import time
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings


def header():
    token = os.environ.get("API_TOKEN", "change-me")
    return {"Authorization": f"Bearer {token}"}


# ── 示例会议记录 ──────────────────────────────────────────────────
SAMPLE_MEETING_TRANSCRIPT = """\
# 周例会记录 - 2026年7月16日

## 参会人员
若楠（项目经理）、昊天（后端）、钟昊洋（架构）、明白自己（产品）

## 讨论内容

若楠：上周大家辛苦了。我们先过一下上周的进度。昊天，那个数据分析模块怎么样了？

昊天：数据分析模块的核心算法已经写完了，但是前端可视化部分还没开始。需要等钟昊洋把 API 接口定下来我才能继续。

钟昊洋：API 接口设计文档我明天下午之前可以给出。另外，Lilies 的 Builder Team 测试中发现了一个并发 bug，我需要优先修这个。

若楠：bug 严重吗？会影响下周的演示吗？

钟昊洋：中等严重程度。并发场景下偶尔会出现 revision 冲突没有正确处理，大概 5% 的概率复现。修复方案我已经想好了，大概需要2天时间。

若楠：好，那优先级这么排：bug 修复最优先，然后是 API 接口文档。下周演示之前这两个必须完成。

明白自己：我这边产品需求文档初稿已经完成了，需要大家 review 一下。另外，Clyins 的项目经理模板我已经设计好了，可以用今天的会议记录测试一下效果。

若楠：太好了。那大家 review 产品文档的时间定在周五下午？每人提前看，会上集中讨论修改意见。

昊天：我周五下午有个外部会议，能不能改到周四下午？

若楠：周四下午可以。明白自己，会议邀请你来发一下。

明白自己：没问题，我明天上午发送会议邀请，并把产品文档一起发出来。

若楠：还有一个事情——下周要给客户做演示，需要一个可视化的 Demo 页面。谁来负责？

昊天：Demo 页面可以等 API 接口定下来后我来做，大概需要一天半。

钟昊洋：我可以帮忙写 Demo 页面的后端接口。

若楠：好，那钟昊洋先修 bug（明天开始，预计2天），然后写 API 文档（明天下午），再帮昊天写 Demo 后端接口。昊天等 API 文档出来后就启动 Demo 前端，争取下周三之前完成。

## 决定事项
1. bug 修复为最高优先级（钟昊洋，2天）
2. 产品文档 review 改到周四下午
3. 下周客户演示需要可视化 Demo 页面

## 后续步骤
- 明天上午：明白自己发会议邀请和产品文档
- 明天开始：钟昊洋修复并发 bug
- 明天下午：钟昊洋交付 API 接口文档
- 周四下午：产品文档 review 会
- 下周周三前：Demo 页面完成
"""

SAMPLE_TEAM_CONTEXT = """\
团队成员：
- 钟昊洋：架构师，擅长后端开发、Lilies 平台维护
- 昊天：后端开发，擅长算法实现和前端可视化
- 明白自己：产品经理，负责需求文档和 Clyins 设计
- 若楠：项目经理，负责进度跟踪和客户沟通
"""


def dry_run_validation():
    """仅验证模板结构，不调用 API（不需要启动 Lilies 后端）。"""
    from agent_platform.template_store import TemplateStore
    from agent_platform.blocks import build_block_registry

    print("=" * 60)
    print("Clyins 模板结构验证 (dry run)")
    print("=" * 60)

    registry = build_block_registry()
    store = TemplateStore()
    templates_dir = Path(__file__).resolve().parent / "templates"
    loaded = store.load_builtins(templates_dir)
    print(f"✅ 模板市场加载完成: {loaded} 个模板")

    template = store.get("clyins")
    print(f"\n📋 模板: {template.meta.title}")
    print(f"   分类: {template.meta.category}")
    print(f"   描述: {template.meta.description[:100]}...")
    print(f"   置信度: {template.meta.confidence}")

    errors = registry.validate_workflow(template.workflow)
    if errors:
        for e in errors:
            print(f"   ❌ {e}")
        return False
    print("✅ 工作流结构验证: 0 errors")

    print(f"\n📊 积木链路 ({len(template.workflow.nodes)} 节点, {len(template.workflow.edges)} 边):")
    for i, node in enumerate(template.workflow.nodes):
        arrow = " → " if i > 0 else "   "
        print(f"   {arrow}[{node.id}] {node.title} ({node.type})")

    # Expand and verify
    expanded = store.expand_into_workflow("clyins", prefix="demo", x=0, y=0)
    expand_errors = registry.validate_workflow(expanded)
    assert not expand_errors, f"Expand errors: {expand_errors}"
    print(f"\n✅ 模板展开验证: 正确重写所有 ID 和 $ref 引用")

    print("\n📝 模板输入:")
    for k, v in template.meta.expected_inputs.items():
        print(f"   - {k}: {v}")

    print("\n📤 模板输出:")
    for k, v in template.meta.expected_outputs.items():
        print(f"   - {k}: {v}")

    return True


def run_demo(api_base: str = "http://127.0.0.1:8001"):
    """运行完整的 Clyins 端到端演示（需要 Lilies 后端已启动）。"""
    import requests

    print("=" * 60)
    print("Clyins AI 项目经理 — 端到端演示")
    print("=" * 60)

    token = os.environ.get("API_TOKEN", "change-me")
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def api(method, path, **kwargs):
        url = f"{api_base}{path}"
        resp = getattr(requests, method)(url, headers=h, **kwargs)
        if resp.status_code >= 400:
            print(f"   ❌ {method.upper()} {path} → {resp.status_code}: {resp.text[:200]}")
        return resp

    # 1. Check health
    print("\n1. 检查 Lilies 后端健康状态...")
    resp = api("get", "/health")
    if resp.status_code != 200:
        print("   ❌ Lilies 后端未启动，请先运行 ./scripts/dev_platform.sh")
        return
    health = resp.json()
    print(f"   ✅ 后端正常: provider={health.get('provider')}, tools={len(health.get('tools', []))}")

    # 2. List templates
    print("\n2. 查找 Clyins 模板...")
    resp = api("get", "/api/v1/templates", params={"query": "clyins"})
    templates = resp.json()
    clyins_templates = [t for t in templates if t["name"] == "clyins"]
    if not clyins_templates:
        print("   ❌ Clyins 模板未找到")
        return
    print(f"   ✅ 找到 Clyins 模板: {clyins_templates[0]['title']} (置信度: {clyins_templates[0].get('confidence')})")

    # 3. Create Application from Clyins template
    print("\n3. 创建 Application 并展开 Clyins 模板...")
    resp = api("post", "/api/v1/applications", json={
        "name": "Clyins Demo - 周例会项目管理",
        "description": "使用 Clyins 模板从周例会记录自动生成日程表",
        "requirement": "从周例会记录中提取行动项并生成日程表",
        "mode": "workflow",
    })
    if resp.status_code != 201:
        print(f"   ❌ 创建应用失败: {resp.text[:200]}")
        return
    app_id = resp.json()["id"]
    print(f"   ✅ 应用创建: {app_id}")

    # 4. Expand template into draft
    print("\n4. 展开 Clyins 模板到应用草稿...")
    resp = api("post", f"/api/v1/templates/clyins/expand", params={"prefix": "clyins"})
    if resp.status_code != 200:
        print(f"   ❌ 展开模板失败: {resp.text[:200]}")
        return
    expanded = resp.json()
    print(f"   ✅ 模板展开: {len(expanded['nodes'])} 节点, {len(expanded['edges'])} 边")

    # 5. Add all nodes to draft
    print("\n5. 将展开的节点添加到草稿...")
    draft_resp = api("get", f"/api/v1/applications/{app_id}/draft")
    revision = draft_resp.json()["revision"]

    node_id_map = {}
    for node in expanded["nodes"]:
        resp = api("post", f"/api/v1/applications/{app_id}/draft", json={
            "expected_revision": revision,
            "idempotency_key": f"clyins-demo-add-node-{node['id']}",
            "op": "add_node",
            "data": {"node": node},
        })
        if resp.status_code != 200:
            print(f"   ❌ 添加节点 {node['id']} 失败: {resp.text[:100]}")
            return
        revision = resp.json()["revision"]

    for edge in expanded["edges"]:
        resp = api("post", f"/api/v1/applications/{app_id}/draft", json={
            "expected_revision": revision,
            "idempotency_key": f"clyins-demo-add-edge-{edge['id']}",
            "op": "add_edge",
            "data": {"edge": edge},
        })
        if resp.status_code != 200:
            print(f"   ❌ 添加边 {edge['id']} 失败: {resp.text[:100]}")
            return
        revision = resp.json()["revision"]

    print(f"   ✅ 所有节点和边已添加到草稿 (最终 revision: {revision})")

    # 6. Validate draft
    print("\n6. 验证草稿...")
    resp = api("post", f"/api/v1/applications/{app_id}/draft/validate")
    validation = resp.json()
    if validation["valid"]:
        print(f"   ✅ 草稿验证通过")
    else:
        print(f"   ⚠️ 草稿验证警告: {validation.get('warnings', [])}")

    # 7. Run the workflow with sample meeting transcript
    print("\n7. 使用示例会议记录运行 Clyins 工作流...")
    resp = api("post", f"/api/v1/applications/{app_id}/runs", json={
        "inputs": {
            "meeting_transcript": SAMPLE_MEETING_TRANSCRIPT,
            "team_context": SAMPLE_TEAM_CONTEXT,
            "meeting_date": "2026-07-16",
        },
        "use_draft": True,
        "workspace_path": ".",
    })
    if resp.status_code != 202:
        print(f"   ❌ 创建工作流运行失败: {resp.text[:300]}")
        return
    run = resp.json()
    run_id = run["run_id"]
    print(f"   ✅ 工作流运行已创建: {run_id}")

    # 8. Wait for completion or human_input pause
    print("\n8. 等待工作流执行...")
    for i in range(60):
        time.sleep(1)
        resp = api("get", f"/api/v1/runs/{run_id}")
        status = resp.json()
        run_status = status["status"]
        if i % 5 == 0:
            print(f"   ... 状态: {run_status}")
        if run_status in ("succeeded", "failed", "paused"):
            break

    if run_status == "paused":
        print(f"\n   ⏸️  工作流已暂停，等待人工核验")
        print(f"   等待节点: {status['state'].get('waiting_node_id', 'unknown')}")
        print(f"\n   📋 要恢复工作流，请调用:")
        print(f"   curl -X POST {api_base}/api/v1/runs/{run_id}/resume \\")
        print(f"     -H 'Authorization: Bearer {token}' \\")
        print(f"     -H 'Content-Type: application/json' \\")
        print(f"     -d '{{\"values\": {{\"approved\": true, \"corrections\": \"\", \"assign_to_lilies\": true}}}}'")

        # Auto-resume for demo
        print(f"\n   🔄 自动批准以继续演示...")
        api("post", f"/api/v1/runs/{run_id}/resume", json={
            "values": {
                "approved": True,
                "corrections": "",
                "assign_to_lilies": True,
            }
        })
        for i in range(30):
            time.sleep(1)
            resp = api("get", f"/api/v1/runs/{run_id}")
            status = resp.json()
            run_status = status["status"]
            if run_status in ("succeeded", "failed"):
                break

    if run_status == "succeeded":
        outputs = status.get("outputs", {})
        print(f"\n   ✅ 工作流执行成功!")
        print(f"\n{'='*60}")
        print(f"📋 Clyins 输出成果:")
        print(f"{'='*60}")

        schedule = outputs.get("schedule", "")
        if schedule:
            print(schedule[:3000])
            if len(schedule) > 3000:
                print(f"\n... (截断，完整内容共 {len(schedule)} 字符)")

        print(f"\n📊 结构化输出:")
        print(f"   摘要: {str(outputs.get('summary', ''))[:200]}")
        tasks = outputs.get("tasks", [])
        print(f"   任务数: {len(tasks) if isinstance(tasks, list) else 'N/A'}")

        print(f"\n🔗 与 Lilies 集成:")
        if outputs.get("assign_to_lilies"):
            print(f"   ✅ 用户已批准将自动化任务提交给 Lilies Builder Team")
            print(f"   下一步: 对每个可自动化任务调用 POST /api/v1/applications/{{id}}/builds")
        else:
            print(f"   ℹ️ 用户未启用自动提交到 Lilies")

    elif run_status == "failed":
        print(f"\n   ❌ 工作流执行失败: {status.get('error', 'unknown error')[:300]}")
    else:
        print(f"\n   ⚠️ 工作流状态: {run_status}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        success = dry_run_validation()
        sys.exit(0 if success else 1)
    else:
        # Try dry-run first to validate the template
        if not dry_run_validation():
            print("\n❌ 模板验证失败，请检查模板文件")
            sys.exit(1)

        print("\n" + "=" * 60)
        # Then try live API
        api_base = os.environ.get("LILIES_API", "http://127.0.0.1:8001")
        try:
            run_demo(api_base)
        except Exception as e:
            print(f"\n⚠️  实时 API 测试需要 Lilies 后端已启动")
            print(f"   错误: {e}")
            print(f"   请运行 ./scripts/dev_platform.sh 启动后端")
            print(f"   或使用 --dry-run 仅验证模板结构")
            sys.exit(1)
