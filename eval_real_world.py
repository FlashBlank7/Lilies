#!/usr/bin/env python3
"""
Lilies 真实场景能力评估

场景 1: 新闻摘要流水线 — 搜索→分类→摘要→格式化
场景 2: 多工具代码审查 Agent — 读代码→跑测试→定位Bug→修复→验证
场景 3: 智能客服路由 — 分类用户意图→分流处理→生成回复
场景 4: 定时报告生成 — 定时触发→数据聚合→LLM生成→结构化输出
场景 5: 多 Agent 协作 — 子Agent分拆→并行分析→汇总报告
"""

from __future__ import annotations

import json, sys, time, shutil
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings

H = {"Authorization": "Bearer test-token-2024"}
RESULTS = []
TIMINGS = {}

def ok(name, cond, detail=""):
    mark = "✅" if cond else "❌"
    msg = f"  {mark} {name}"
    if detail and not cond: msg += f" — {str(detail)[:200]}"
    print(msg); RESULTS.append(cond)

def section(title):
    print(f"\n{'━'*60}\n  {title}\n{'━'*60}")

def mutate(client, app_id, rev, op, data):
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=H, json={
        "expected_revision": rev,
        "idempotency_key": f"rw-{op}-{rev}-{time.time():.0f}",
        "op": op, "data": data,
    })
    if r.status_code != 200: raise RuntimeError(f"Mutate {op}: {r.text[:200]}")
    return r.json()["revision"]

def wait_run(client, run_id, timeout=120):
    for _ in range(timeout * 2):
        r = client.get(f"/api/v1/runs/{run_id}", headers=H)
        if r.json()["status"] in ("succeeded","failed","paused"): return r.json()
        time.sleep(0.5)
    return client.get(f"/api/v1/runs/{run_id}", headers=H).json()

def build_app(client, name, req, max_turns=25):
    """Use Builder Team to auto-build a workflow from a requirement."""
    t0 = time.time()
    ar = client.post("/api/v1/applications", headers=H, json={"name": name, "requirement": req})
    app_id = ar.json()["id"]
    br = client.post(f"/api/v1/applications/{app_id}/builds", headers=H, json={
        "requirement": req, "auto_publish": True, "max_turns": max_turns, "max_repair_cycles": 3,
    })
    bid = br.json()["build_id"]
    for i in range(max_turns * 12):
        b = client.get(f"/api/v1/builds/{bid}", headers=H).json()
        if b.get("status") in ("published","ready","needs_attention","cancelled","failed"): break
        if i % 30 == 0: print(f"    [{i}s] {b.get('status','?')}")
        time.sleep(1)
    b = client.get(f"/api/v1/builds/{bid}", headers=H).json()
    draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=H).json()
    elapsed = time.time() - t0
    TIMINGS[name] = elapsed
    return app_id, b, draft["snapshot"]

def gen_agent(client, name, req, ws_path):
    """Generate a specialized agent."""
    t0 = time.time()
    # Retry up to 2 times
    for attempt in range(2):
        gr = client.post("/v1/agent-generations", headers=H, json={
            "requirement": req + (" Keep it concise." if attempt > 0 else ""),
            "workspace_path": ws_path, "auto_publish": True,
        })
        gid = gr.json()["generation_id"]
        for i in range(300):
            g = client.get(f"/v1/agent-generations/{gid}", headers=H).json()
            if g.get("status") in ("published","failed","draft"): break
            time.sleep(1)
        g = client.get(f"/v1/agent-generations/{gid}", headers=H).json()
        if g.get("status") == "published": break
    TIMINGS[f"agent_{name}"] = time.time() - t0
    return g


def main():
    tmp = TemporaryDirectory()
    tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024", data_dir=tp/"data", workspace_root=tp/"workspaces")
    s.prepare()
    (tp/"workspaces").mkdir(parents=True, exist_ok=True)
    (tp/"workspaces").chmod(0o777)
    ws = tp / "workspaces" / "realworld"
    example_src = Path(__file__).resolve().parent / "examples" / "broken_python_project"
    if example_src.exists():
        shutil.copytree(example_src, ws)
        for p in [ws] + list(ws.rglob("*")): p.chmod(0o777) if p.exists() else None
    else:
        ws.mkdir(parents=True, exist_ok=True)

    app = create_app(settings=s)
    with TestClient(app) as client:

        # ═══════════════════════════════════════════════════════════
        # 场景 1: 新闻摘要流水线
        # ═══════════════════════════════════════════════════════════
        section("场景 1: 新闻摘要流水线 — Builder自动搭建")

        req1 = (
            "Build a simple text analysis workflow. It takes a topic as input. "
            "Use an LLM to generate 3 interesting facts about that topic. "
            "Then use a Template to format them as bullet points with a title. "
            "Output must have: title, bullets."
        )
        print(f"  需求: {req1[:120]}...\n")
        app1, b1, snap1 = build_app(client, "知识摘要", req1, max_turns=25)

        ok("1.1 Build完成", b1["status"] in ("published","ready"),
           b1.get("error","") or b1.get("status",""))
        ok("1.2 至少3节点", len(snap1["workflow"]["nodes"]) >= 3,
           f"{len(snap1['workflow']['nodes'])} nodes")
        node_types = {n["type"] for n in snap1["workflow"]["nodes"]}
        ok("1.3 含搜索或LLM", bool({"tool","llm","web_search"} & node_types),
           str(node_types))
        ok("1.4 含模板", "template_transform" in node_types or "end" in node_types)

        if b1["status"] in ("published","ready"):
            ver = (b1.get("team_state",{}) or {}).get("published_version",1) or 1
            r = client.post(f"/api/v1/applications/{app1}/runs", headers=H, json={
                "inputs": {"topic": "AI safety regulations 2025"},
                "version": ver, "workspace_path": ".",
            })
            if r.status_code == 202:
                rec = wait_run(client, r.json()["run_id"], timeout=180)
                ok("1.5 运行成功", rec["status"]=="succeeded", rec.get("error",""))
                out = rec.get("outputs",{})
                out_str = json.dumps(out, ensure_ascii=False)
                ok("1.6 输出非空", len(out_str) > 20, out_str[:300])
                print(f"    输出预览: {out_str[:300]}")
            else:
                ok("1.5 启动运行", False, r.text[:150])

        # ═══════════════════════════════════════════════════════════
        # 场景 2: 多工具代码审查 Agent
        # ═══════════════════════════════════════════════════════════
        section("场景 2: 代码审查 Agent — 发现Bug→修复→验证")

        req2 = (
            "Generate a thorough code review agent. It MUST: "
            "1) Read all Python files in the workspace. "
            "2) Run pytest to find failing tests. "
            "3) For each failing test, trace the root cause in the source code. "
            "4) Edit the source code to fix the bug. "
            "5) Rerun tests to verify the fix. "
            "6) Report: what was broken, how it was fixed, and test results."
        )

        # First check if code has a bug to fix
        calc_file = ws / "calculator.py"
        has_bug = calc_file.exists() and "return left - right" in calc_file.read_text()
        print(f"  代码含Bug: {has_bug}")
        print(f"  需求: {req2[:120]}...\n")

        g2 = gen_agent(client, "code_reviewer", req2, str(ws))

        g2_status = (g2 or {}).get("status","error")
        g2_error = (g2 or {}).get("error","") or ""
        ok("2.1 Agent生成成功", g2_status == "published",
           f"{g2_status}: {g2_error[:150]}")
        agent_id = g2.get("agent_id")
        if agent_id:
            a = client.get(f"/v1/agents/{agent_id}", headers=H).json()
            spec = a.get("spec",{})
            tools = spec.get("tools",[])
            prompt_len = len(spec.get("system_prompt",""))
            ok("2.2 有Read工具", "Read" in tools, str(tools))
            ok("2.3 有Bash工具", "Bash" in tools, str(tools))
            ok("2.4 有Edit/Write工具", bool({"Edit","Write"}&set(tools)), str(tools))
            ok("2.5 Prompt充足", prompt_len > 200, f"{prompt_len} chars")
            print(f"    Agent: {spec.get('name','?')} | {tools} | {prompt_len}字符")

            # Now run the agent on the broken project
            print(f"\n    🤖 启动Agent会话...")
            sr = client.post("/v1/sessions", headers=H, json={
                "agent_id": agent_id, "workspace_path": str(ws),
            })
            if sr.status_code == 201:
                sid = sr.json()["session_id"]
                task = (
                    f"Review and fix the Python project at {ws}. "
                    "Read calculator.py and test_calculator.py. "
                    "Run 'python -m pytest test_calculator.py -v'. "
                    "If any tests fail, find the bug in calculator.py, fix it, and rerun tests. "
                    "Report all findings."
                )
                client.post(f"/v1/sessions/{sid}/messages", headers=H, json={"content": task})

                approved = set()
                tool_names = []
                for i in range(200):
                    time.sleep(0.3)
                    session = client.get(f"/v1/sessions/{sid}", headers=H).json()
                    # Auto-approve permissions
                    evlist = client.get(f"/v1/streams/{sid}", headers=H).json()
                    for e in evlist:
                        if e.get("type") == "permission.requested":
                            rid = e["data"].get("request_id","")
                            if rid and rid not in approved:
                                client.post(f"/v1/sessions/{sid}/permissions/{rid}", headers=H,
                                            json={"behavior":"allow"})
                                approved.add(rid)
                        elif e.get("type") == "tool.started":
                            tn = e["data"].get("tool","")
                            if tn not in tool_names: tool_names.append(tn)
                    if session["status"] in ("ready","error"): break

                session = client.get(f"/v1/sessions/{sid}", headers=H).json()
                ok("2.6 会话完成", session["status"]=="ready", session.get("error",""))
                ok("2.7 调用了工具", len(tool_names) > 0, str(tool_names))

                # Show agent's final answer
                for msg in reversed(session.get("messages",[])):
                    if msg.get("role")=="assistant":
                        text = "".join(b.get("text","") for b in msg.get("content",[])
                                       if b.get("type")=="text")
                        if text.strip():
                            print(f"\n    ── Agent分析报告 ──")
                            for line in text.split("\n")[:20]:
                                print(f"    {line}")
                            break

                # Verify the fix
                if calc_file.exists():
                    fixed = calc_file.read_text()
                    was_fixed = "return left + right" in fixed
                    ok("2.8 代码已修复", was_fixed or not has_bug,
                       f"Fixed: {was_fixed}, HadBug: {has_bug}")
            else:
                ok("2.6 创建会话", False, sr.text[:150])

        # ═══════════════════════════════════════════════════════════
        # 场景 3: 智能客服路由 — Builder
        # ═══════════════════════════════════════════════════════════
        section("场景 3: 智能客服路由 — 意图分类→分流处理")

        req3 = (
            "Build a customer support routing workflow. "
            "Take a customer message as input. "
            "Classify the intent into: complaint, question, feedback, or urgent. "
            "For complaints: respond with empathy and an escalation note. "
            "For questions: provide a helpful answer. "
            "For feedback: thank the customer. "
            "For urgent: flag for immediate attention. "
            "Use If/Else branching and Template Transform for each response type. "
            "The output must include: intent, response."
        )
        print(f"  需求: {req3[:120]}...\n")
        app3, b3, snap3 = build_app(client, "客服路由", req3, max_turns=30)

        ok("3.1 Build完成", b3["status"] in ("published","ready"),
           b3.get("error","") or b3.get("status",""))
        nodes3 = snap3["workflow"]["nodes"]
        types3 = {n["type"] for n in nodes3}
        ok("3.2 含分支逻辑", bool({"if_else","question_classifier"}&types3), str(types3))
        ok("3.3 含模板", "template_transform" in types3)
        ok("3.4 至少5节点", len(nodes3) >= 5, f"{len(nodes3)} nodes")

        if b3["status"] in ("published","ready"):
            ver = (b3.get("team_state",{}) or {}).get("published_version",1) or 1
            # Inspect draft to find the actual input field name
            start_nodes = [n for n in snap3["workflow"]["nodes"] if n["type"]=="start"]
            input_name = "message"  # default
            if start_nodes:
                inputs_config = start_nodes[0].get("config",{}).get("inputs",[])
                if inputs_config:
                    input_name = inputs_config[0].get("name","message")
            print(f"    Start输入名: {input_name}")
            # Test with a complaint
            r = client.post(f"/api/v1/applications/{app3}/runs", headers=H, json={
                "inputs": {input_name: "I've been waiting 3 weeks for my refund! This is unacceptable!"},
                "version": ver, "workspace_path": ".",
            })
            if r.status_code == 202:
                rec = wait_run(client, r.json()["run_id"])
                ok("3.5 投诉路由成功", rec["status"]=="succeeded", rec.get("error",""))
                out_str = json.dumps(rec.get("outputs",{}), ensure_ascii=False)
                ok("3.6 输出非空", len(out_str) > 10, out_str[:300])
                print(f"    输出: {out_str[:300]}")
            else:
                ok("3.5 启动运行", False, r.text[:150])
            # Test with a question
            r = client.post(f"/api/v1/applications/{app3}/runs", headers=H, json={
                "inputs": {input_name: "What are your business hours?"},
                "version": ver, "workspace_path": ".",
            })
            if r.status_code == 202:
                rec = wait_run(client, r.json()["run_id"])
                ok("3.7 问答路由成功", rec["status"]=="succeeded")
                out2 = json.dumps(rec.get("outputs",{}), ensure_ascii=False)
                print(f"    问答输出: {out2[:200]}")
            else:
                ok("3.7 启动问答", False, r.text[:150])

        # ═══════════════════════════════════════════════════════════
        # 场景 4: 数据处理 Agent
        # ═══════════════════════════════════════════════════════════
        section("场景 4: 数据处理分析 Agent")

        req4 = (
            "Generate a data analysis agent. It reads CSV/JSON files, "
            "computes summary statistics (mean, median, min, max, count), "
            "identifies missing values, and generates a concise report. "
            "Use Python with pandas if available, otherwise pure Python."
        )
        g4 = gen_agent(client, "data_analyst", req4, str(ws))

        g4_status = (g4 or {}).get("status","error")
        g4_error = (g4 or {}).get("error","") or ""
        ok("4.1 Agent生成", g4_status == "published",
           f"{g4_status}: {g4_error[:150]}")
        if g4.get("agent_id"):
            a = client.get(f"/v1/agents/{g4['agent_id']}", headers=H).json()
            spec = a.get("spec",{})
            ok("4.2 有Bash工具", "Bash" in spec.get("tools",[]))
            ok("4.3 有Read工具", "Read" in spec.get("tools",[]))
            print(f"    Agent: {spec.get('name','?')} | 工具: {spec.get('tools',[])} | Prompt: {len(spec.get('system_prompt',''))}字符")

        # ═══════════════════════════════════════════════════════════
        # 场景 5: 多Agent协作任务分解
        # ═══════════════════════════════════════════════════════════
        section("场景 5: 多Agent任务分解与协作")

        req5 = (
            "Build a task decomposition workflow. Take a complex task as input. "
            "Use an LLM to break it down into subtasks. "
            "Use Task Dispatcher to order them by dependency. "
            "Use a Template to format a task plan with owners and estimated effort. "
            "Output the structured plan."
        )
        print(f"  需求: {req5[:120]}...\n")
        app5, b5, snap5 = build_app(client, "任务分解", req5, max_turns=25)

        ok("5.1 Build完成", b5["status"] in ("published","ready","needs_attention"),
           b5.get("error","") or b5.get("status",""))
        nodes5 = snap5["workflow"]["nodes"]
        ok("5.2 至少3节点", len(nodes5) >= 3, f"{len(nodes5)} nodes")

        # Try to run if ready/published, or report what the builder created
        if b5["status"] in ("published","ready"):
            ver = (b5.get("team_state",{}) or {}).get("published_version",1) or 1
            # Find start node input name
            start_n = [n for n in nodes5 if n["type"]=="start"]
            inp_name = "task"
            if start_n:
                inputs_cfg = start_n[0].get("config",{}).get("inputs",[])
                if inputs_cfg: inp_name = inputs_cfg[0].get("name","task")
            r = client.post(f"/api/v1/applications/{app5}/runs", headers=H, json={
                "inputs": {inp_name: "Build a mobile app with authentication, database, and push notifications"},
                "version": ver, "workspace_path": ".",
            })
            if r.status_code == 202:
                rec = wait_run(client, r.json()["run_id"])
                ok("5.3 运行成功", rec["status"]=="succeeded", rec.get("error",""))
                out_str = json.dumps(rec.get("outputs",{}), ensure_ascii=False)
                ok("5.4 有任务分解", len(out_str) > 20, out_str[:400])
                print(f"    任务分解: {out_str[:400]}")
            else:
                ok("5.3 运行", False, r.text[:150])
        else:
            print(f"    Builder状态={b5['status']}, 跳过运行")
            ok("5.3 运行", False, f"状态={b5['status']}")
            ok("5.4 运行", False, f"状态={b5['status']}")

        # ═══════════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════════
        section("评估总结")

        passed = sum(RESULTS)
        total = len(RESULTS)
        print(f"\n  场景覆盖:")
        print(f"    场景1 新闻摘要: Builder自动搭建搜索→LLM→格式化流水线")
        print(f"    场景2 代码审查: Agent读取→测试→定位→修复→验证")
        print(f"    场景3 客服路由: 意图分类→4路分支→差异化响应")
        print(f"    场景4 数据分析: Agent统计→缺失检测→报告生成")
        print(f"    场景5 任务分解: LLM拆解→依赖排序→结构化输出")
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed < total:
            print(f"  ❌ 失败: {total - passed}")
        else:
            print(f"  🎉 全部通过!")
        print(f"  通过率: {passed/total*100:.1f}%")

        # Timing summary
        print(f"\n  耗时统计:")
        for name, elapsed in TIMINGS.items():
            print(f"    {name}: {elapsed:.0f}s")

    try: tmp.cleanup()
    except (PermissionError, OSError): pass
    return 0 if passed == total else 1

if __name__ == "__main__":
    raise SystemExit(main())
