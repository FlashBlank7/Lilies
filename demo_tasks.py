#!/usr/bin/env python3
"""
Lilies 端到端演示 — 三个真实任务 (简化版，直接轮询状态)

任务 1: Builder Team 自动搭建智能问答工作流
任务 2: Agent 自动生成代码审查 Agent
任务 3: Agent 会话执行真实代码修复
"""

from __future__ import annotations

import json, sys, time, shutil
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings

def header(): return {"Authorization": "Bearer test-token-2024"}

RESULTS = []

def check(name, ok, detail=""):
    mark = "✅" if ok else "❌"
    line = f"  {mark} {name}"
    if detail and not ok:
        line += f" — {str(detail)[:200]}"
    print(line)
    RESULTS.append(ok)

def main():
    tmp = TemporaryDirectory()
    tmp_path = Path(tmp.name)
    settings = Settings(api_token="test-token-2024", data_dir=tmp_path/"data", workspace_root=tmp_path/"workspaces")
    settings.prepare()
    (tmp_path/"workspaces").mkdir(parents=True, exist_ok=True)
    (tmp_path/"workspaces").chmod(0o777)

    # 复制 broken_python_project
    example_src = Path(__file__).resolve().parent / "examples" / "broken_python_project"
    ws = tmp_path / "workspaces" / "demo"
    if example_src.exists():
        shutil.copytree(example_src, ws)
        # Ensure Docker sandbox user (uid 10001) can write
        for p in [ws] + list(ws.rglob("*")):
            p.chmod(0o777)
        print(f"📋 测试项目: {ws}\n")

    app = create_app(settings=settings)
    with TestClient(app) as client:

        # ═══════════════════════════════════════════════════════
        # 任务 1: Builder Team
        # ═══════════════════════════════════════════════════════
        print("═" * 60)
        print("  任务 1: Builder Team 自动搭建智能问答工作流")
        print("═" * 60)

        req = (
            "Create a workflow that takes a user question as input. "
            "Use an LLM to classify the question into a category "
            "(technical, creative, or factual), then use a template "
            "to format: 'Category: {category}. Answer: ...'. "
            "Output fields: category, formatted_answer."
        )
        print(f"\n需求: {req[:120]}...\n")

        t1 = time.time()
        app_r = client.post("/api/v1/applications", headers=header(), json={
            "name": "问答工作流", "requirement": req,
        })
        app_id = app_r.json()["id"]

        b_r = client.post(f"/api/v1/applications/{app_id}/builds", headers=header(), json={
            "requirement": req, "auto_publish": True, "max_turns": 30, "max_repair_cycles": 5,
        })
        build_id = b_r.json()["build_id"]
        print(f"Build ID: {build_id[:8]}...")

        for i in range(300):
            b = client.get(f"/api/v1/builds/{build_id}", headers=header()).json()
            s = b.get("status","?")
            if i % 20 == 0:
                print(f"  [{i}s] status={s}")
            if s in ("published","ready","needs_attention","cancelled","failed"):
                break
            time.sleep(1)

        b = client.get(f"/api/v1/builds/{build_id}", headers=header()).json()
        t1e = time.time() - t1
        final = b.get("status","?")

        print(f"\n结果 (耗时 {t1e:.0f}s):")
        check(f"Build 完成 (status={final})", final in ("published","ready"), b.get("error",""))
        pub_ver = (b.get("team_state") or {}).get("published_version") or b.get("published_version")
        check("已发布版本", pub_ver is not None, f"team_state={json.dumps(b.get('team_state',{}))[:200]}")

        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=header()).json()
        nodes = draft["snapshot"]["workflow"]["nodes"]
        edges = draft["snapshot"]["workflow"]["edges"]
        tests = draft["snapshot"].get("tests",[])
        print(f"  工作流: {len(nodes)} 节点, {len(edges)} 连线, {len(tests)} 测试")
        for n in nodes:
            print(f"    [{n['type']}] {n['title']}")

        if final in ("published","ready"):
            run_r = client.post(f"/api/v1/applications/{app_id}/runs", headers=header(), json={
                "inputs": {"question": "How to implement a binary search tree?"},
                "version": b.get("published_version",1), "workspace_path": ".",
            })
            if run_r.status_code == 202:
                rid = run_r.json()["run_id"]
                for _ in range(60):
                    rec = client.get(f"/api/v1/runs/{rid}", headers=header()).json()
                    if rec["status"] in ("succeeded","failed"): break
                    time.sleep(0.2)
                rec = client.get(f"/api/v1/runs/{rid}", headers=header()).json()
                check(f"运行: {rec['status']}", rec["status"]=="succeeded")
                for k,v in rec.get("outputs",{}).items():
                    print(f"    {k}: {str(v)[:200]}")

        # ═══════════════════════════════════════════════════════
        # 任务 2: Agent Generation
        # ═══════════════════════════════════════════════════════
        print("\n" + "═" * 60)
        print("  任务 2: Agent 自动生成 — Python 代码审查专家")
        print("═" * 60)

        agent_req = (
            "Generate a code review agent. It reads Python files, finds bugs "
            "(syntax errors, logical errors, missing edge cases), suggests fixes, "
            "runs pytest to verify, and reports results concisely."
        )
        print(f"\n需求: {agent_req[:120]}...\n")

        t2 = time.time()
        # Agent Generation with retry (DeepSeek JSON is intermittent)
        max_gen_retries = 2
        for gen_attempt in range(max_gen_retries + 1):
            if gen_attempt > 0:
                print(f"\n  🔄 重试 Agent 生成 (第{gen_attempt}次)...")
                agent_req_retry = agent_req + " Keep the agent config concise, under 2000 chars total."
            else:
                agent_req_retry = agent_req

            gr = client.post("/v1/agent-generations", headers=header(), json={
                "requirement": agent_req_retry, "workspace_path": str(ws), "auto_publish": True,
            })
            gid = gr.json()["generation_id"]
            print(f"Generation ID: {gid[:8]}...")

            for i in range(360):
                g = client.get(f"/v1/agent-generations/{gid}", headers=header()).json()
                s = g.get("status","?")
                if i % 45 == 0:
                    print(f"  [{i}s] status={s}")
                if s in ("published","failed","draft"): break
                time.sleep(1)

            g = client.get(f"/v1/agent-generations/{gid}", headers=header()).json()
            agent_id = g.get("agent_id")
            agent_version = g.get("agent_version")
            gen_status = g.get("status","?")

            if gen_status == "published" or (gen_status == "failed" and gen_attempt == max_gen_retries):
                break
            elif gen_status == "failed":
                print(f"  ⚠️ 第{gen_attempt+1}次尝试失败: {g.get('error','')[:150]}")

        t2e = time.time() - t2

        print(f"\n结果 (耗时 {t2e:.0f}s):")
        check(f"生成完成 (status={gen_status})", gen_status in ("published","draft"), g.get("error",""))
        check("Agent ID 返回", bool(agent_id))

        if agent_id:
            # Get agent with specific version (may be draft, not yet published)
            vparam = f"?version={agent_version}" if agent_version else ""
            a = client.get(f"/v1/agents/{agent_id}{vparam}", headers=header()).json()
            spec = a.get("spec",{})
            if not spec and agent_version is None:
                # Try getting the latest version even if not published
                a = client.get(f"/v1/agents/{agent_id}?version=1", headers=header()).json()
                spec = a.get("spec",{})
            print(f"  名称: {spec.get('name','?')}")
            print(f"  工具: {spec.get('tools',[])}")
            print(f"  轮次上限: {spec.get('max_turns','?')}")
            print(f"  System Prompt: {len(spec.get('system_prompt',''))} 字符")
            check("有 System Prompt", len(spec.get("system_prompt","")) > 50)
            check("有工具", len(spec.get("tools",[])) > 0)

        # ═══════════════════════════════════════════════════════
        # 任务 3: Agent 会话修复代码
        # ═══════════════════════════════════════════════════════
        print("\n" + "═" * 60)
        print("  任务 3: Agent 会话 — 修复 Python 测试失败")
        print("═" * 60)

        if not agent_id or not example_src.exists():
            print("  ⚠️ 跳过: 前置条件不满足")
        else:
            print(f"\n原始 calculator.py:")
            for line in (ws/"calculator.py").read_text().split("\n")[:15]:
                print(f"  {line}")

            print(f"\n原始 test_calculator.py:")
            for line in (ws/"test_calculator.py").read_text().split("\n")[:15]:
                print(f"  {line}")

            t3 = time.time()
            sr = client.post("/v1/sessions", headers=header(), json={
                "agent_id": agent_id,
                "workspace_path": str(ws),
                # Pass agent_version so draft agents can be used too
                "agent_version": agent_version if agent_version else None,
            })
            if sr.status_code != 201:
                print(f"  ❌ 会话创建失败: {sr.text[:200]}")
            else:
                sid = sr.json()["session_id"]
                print(f"\nSession ID: {sid[:8]}...")

                task = (
                    f"Read calculator.py and test_calculator.py in the workspace. "
                    f"Run 'python -m pytest test_calculator.py -v' to find failing tests. "
                    f"Fix all bugs in calculator.py. Run tests again to verify all pass. "
                    f"Report what you fixed."
                )
                mr = client.post(f"/v1/sessions/{sid}/messages", headers=header(), json={"content": task})
                print(f"任务已发送: {mr.status_code}")

                # Wait for completion, auto-approving permissions
                approved = set()
                seen_events = 0
                for i in range(240):
                    time.sleep(0.5)
                    # Use list endpoint (not SSE stream) to avoid blocking
                    ev = client.get(f"/v1/streams/{sid}?after={seen_events}", headers=header()).json()
                    for e in ev:
                        seen_events += 1
                        etype = e.get("type","")
                        d = e.get("data",{})
                        if etype == "permission.requested":
                            rid = d.get("request_id","")
                            if rid and rid not in approved:
                                client.post(f"/v1/sessions/{sid}/permissions/{rid}", headers=header(),
                                            json={"behavior":"allow"})
                                approved.add(rid)
                                print(f"  🔓 批准权限")
                        elif etype == "tool.started":
                            print(f"  🔧 [{d.get('tool','')}]")
                        elif etype == "tool.completed":
                            print(f"  ✅ 完成")
                        elif etype == "tool.failed":
                            print(f"  ❌ 失败: {str(d.get('error',''))[:100]}")
                        elif etype == "turn.completed":
                            print(f"  ✅ 回合完成")
                    session = client.get(f"/v1/sessions/{sid}", headers=header()).json()
                    if session["status"] in ("ready","error"): break

                t3e = time.time() - t3
                session = client.get(f"/v1/sessions/{sid}", headers=header()).json()
                print(f"\n结果 (耗时 {t3e:.0f}s):")
                check(f"会话完成", session["status"]=="ready", session.get("error",""))
                check("有工具调用", len(approved) > 0 or seen_events > 5)

                # 展示 Agent 回答
                for msg in reversed(session.get("messages",[])):
                    if msg.get("role")=="assistant":
                        text = "".join(b.get("text","") for b in msg.get("content",[]) if b.get("type")=="text")
                        if text.strip():
                            print(f"\n  Agent 回答:")
                            print(f"  {'─'*50}")
                            for line in text.split("\n")[:25]:
                                print(f"  {line}")
                            print(f"  {'─'*50}")
                            break

                # 验证修复
                if (ws/"calculator.py").exists():
                    print(f"\n  修复后 calculator.py:")
                    for line in (ws/"calculator.py").read_text().split("\n")[:30]:
                        print(f"  {line}")

        # ═══════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════
        print("\n" + "═" * 60)
        passed = sum(RESULTS)
        total = len(RESULTS)
        print(f"  总结: {passed}/{total} 项通过")
        if passed == total:
            print(f"  🎉 全部通过!")
        else:
            for i, r in enumerate(RESULTS):
                if not r: print(f"  ❌ 第{i+1}项未通过")
        print("═" * 60)

    try:
        tmp.cleanup()
    except (PermissionError, OSError):
        pass  # Docker sandbox files owned by uid 10001
    return 0 if passed == total else 1

if __name__ == "__main__":
    raise SystemExit(main())
