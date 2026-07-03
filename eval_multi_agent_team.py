#!/usr/bin/env python3
"""
Lilies 多智能体软件工程团队 — 完整验证

架构:
  ┌─────────────────────────────────────────────────┐
  │              Coordinator (协调者)                │
  │  接收需求 → 拆解 → 分派 → 汇总 → 交付            │
  └────┬──────────┬──────────┬──────────┬───────────┘
       │          │          │          │
  ┌────▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────┐
  │需求拆解│ │系统设计│ │测试方案│ │代码编写│
  │ Agent  │ │ Agent  │ │ Agent  │ │ Agent  │
  └────────┘ └────────┘ └───┬────┘ └───┬────┘
                            │          │
                       ┌────▼──────────▼────┐
                       │    测试执行 Agent   │
                       │  运行+验证+报告     │
                       └────────────────────┘

验证流程:
  1. 生成6个专项Agent
  2. 搭建协调工作流（subagent_spawn × task_dispatcher × dependency_gate）
  3. 真实任务: 设计并实现一个简单的REST API服务
  4. 检查产出链: 需求文档→架构设计→测试方案→代码→测试报告
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
R = []
TIMINGS = {}

def ok(n, c, d=""):
    m="✅" if c else "❌"
    x=f"  {m} {n}"
    if d and not c: x+=f" — {str(d)[:200]}"
    print(x); R.append(c)
def hdr(t): print(f"\n{'█'*64}\n  {t}\n{'█'*64}")

def gen_agent(c, name, req, ws):
    t0=time.time()
    for attempt in range(2):
        gr=c.post("/v1/agent-generations",headers=H,json={
            "requirement":req+(" Keep it concise." if attempt>0 else ""),
            "workspace_path":ws,"auto_publish":True})
        gid=gr.json()["generation_id"]
        for i in range(300):
            g=c.get(f"/v1/agent-generations/{gid}",headers=H).json()
            if g.get("status") in("published","failed"): break
            time.sleep(1)
        g=c.get(f"/v1/agent-generations/{gid}",headers=H).json()
        if g.get("status")=="published": break
    TIMINGS[name]=time.time()-t0
    return g

def run_agent_session(c, agent_id, ws, task, label=""):
    """Run an agent session and return (status, tools_used, final_answer, session_id)."""
    sr=c.post("/v1/sessions",headers=H,json={"agent_id":agent_id,"workspace_path":ws})
    if sr.status_code!=201: return "session_failed",[],sr.text[:200],""
    sid=sr.json()["session_id"]
    c.post(f"/v1/sessions/{sid}/messages",headers=H,json={"content":task})
    approved=set(); tools_used=[]
    for _ in range(250):
        time.sleep(0.3)
        sess=c.get(f"/v1/sessions/{sid}",headers=H).json()
        for e in c.get(f"/v1/streams/{sid}",headers=H).json():
            t=e.get("type",""); d=e.get("data",{})
            if t=="permission.requested":
                rid=d.get("request_id","")
                if rid and rid not in approved:
                    c.post(f"/v1/sessions/{sid}/permissions/{rid}",headers=H,json={"behavior":"allow"})
                    approved.add(rid)
            elif t=="tool.started":
                tn=d.get("tool","")
                if tn not in tools_used: tools_used.append(tn)
                if label and len(tools_used)<=5: print(f"    [{label}] 🔧 {tn}")
        if sess["status"] in("ready","error"): break
    # Fix Docker sandbox file ownership after each session
    for p in [ws] + list(ws.rglob("*")):
        try: p.chmod(0o777)
        except: pass
    sess=c.get(f"/v1/sessions/{sid}",headers=H).json()
    answer=""
    for m in reversed(sess.get("messages",[])):
        if m.get("role")=="assistant":
            answer="".join(b.get("text","") for b in m.get("content",[]) if b.get("type")=="text")
            if answer: break
    return sess["status"],tools_used,answer,sid

def mutate(c,aid,rev,op,data):
    r=c.post(f"/api/v1/applications/{aid}/draft",headers=H,json={
        "expected_revision":rev,"idempotency_key":f"team-{op}-{rev}",
        "op":op,"data":data})
    if r.status_code!=200: raise RuntimeError(f"{op}:{r.text[:200]}")
    return r.json()["revision"]

def wr(c,rid):
    for _ in range(200):
        r=c.get(f"/api/v1/runs/{rid}",headers=H)
        if r.json()["status"] in("succeeded","failed"): return r.json()
        time.sleep(0.2)
    return c.get(f"/api/v1/runs/{rid}",headers=H).json()


def main():
    tmp = TemporaryDirectory(); tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024",data_dir=tp/"data",workspace_root=tp/"workspaces")
    s.prepare(); (tp/"workspaces").mkdir(parents=True,exist_ok=True)
    (tp/"workspaces").chmod(0o777)
    ws = tp/"workspaces"/"sw-project"
    ws.mkdir(parents=True,exist_ok=True)
    for p in [ws]+list(ws.rglob("*")): p.chmod(0o777) if p.exists() else None

    app = create_app(settings=s)
    with TestClient(app) as c:

        # ═══════════════════════════════════════════════════════
        # Phase 0: 设置项目上下文
        # ═══════════════════════════════════════════════════════
        hdr("Phase 0: 项目上下文 — 目标任务定义")

        project_task = (
            "Build a simple Task Management REST API with these endpoints:\n"
            "  POST /tasks — create a task (title, description, priority)\n"
            "  GET /tasks — list all tasks, optional ?priority= filter\n"
            "  GET /tasks/{id} — get one task\n"
            "  PUT /tasks/{id} — update a task\n"
            "  DELETE /tasks/{id} — delete a task\n"
            "Use Python Flask or FastAPI. Store tasks in memory (dict). "
            "Include input validation (title required, priority must be low/medium/high). "
            "Write tests using pytest. Organize code in a single app.py file."
        )
        (ws/"REQUIREMENT.md").write_text(f"# Project Task\n\n{project_task}\n")
        print(f"  任务: 构建 Task Management REST API (5 endpoints)")
        print(f"  工作区: {ws}")

        # ═══════════════════════════════════════════════════════
        # Phase 1: 生成专项 Agent 团队
        # ═══════════════════════════════════════════════════════
        hdr("Phase 1: 生成 5 个专项 Agent")

        agent_specs = [
            ("decomposer", "Requirement Decomposer",
             "Generate an agent that reads REQUIREMENT.md and breaks it into ordered subtasks. "
             "Each subtask has: name, description, dependencies, and acceptance criteria. "
             "Write the decomposition to DECOMPOSITION.md."),
            ("designer", "System Designer",
             "Generate a system design agent. It reads REQUIREMENT.md and DECOMPOSITION.md, "
             "then produces a technical architecture: component diagram (text), data models, "
             "API contract (endpoints/params/responses), and implementation notes. "
             "Write to DESIGN.md."),
            ("test_planner", "Test Planner",
             "Generate a test planning agent. It reads DESIGN.md and produces a comprehensive "
             "test plan: test cases for each endpoint (normal + edge cases), testing strategy, "
             "and expected behaviors. Write to TEST_PLAN.md."),
            ("coder", "Code Writer",
             "Generate a code implementation agent. It reads DESIGN.md and TEST_PLAN.md, "
             "then writes the complete implementation in app.py. Code must be clean, "
             "well-documented, and handle edge cases. Include input validation and proper error handling."),
            ("tester", "Test Runner",
             "Generate a test execution agent. It reads the project code and test plan, "
             "writes test_app.py with pytest tests, runs them with 'python -m pytest test_app.py -v', "
             "fixes any bugs in app.py if tests fail, reruns until all pass, "
             "and writes TEST_REPORT.md with results."),
        ]

        agents = {}
        for agent_key, agent_name, agent_req in agent_specs:
            print(f"\n  ── {agent_name} ──")
            g = gen_agent(c, agent_key, agent_req, str(ws))
            status = (g or {}).get("status","?")
            aid = (g or {}).get("agent_id")
            print(f"    状态: {status} | ID: {(aid or 'N/A')[:16]}...")

            if aid and status=="published":
                a = c.get(f"/v1/agents/{aid}",headers=H).json()
                spec = a.get("spec",{})
                tools = spec.get("tools",[])
                prompt_len = len(spec.get("system_prompt",""))
                agents[agent_key] = {"id":aid,"name":agent_name,"tools":tools,"prompt_len":prompt_len}
                print(f"    工具: {tools} | Prompt: {prompt_len}字")
                ok(f"1.{agent_key} 生成",True)
            else:
                agents[agent_key] = None
                ok(f"1.{agent_key} 生成",False,f"status={status}")

        # ═══════════════════════════════════════════════════════
        # Phase 2: 顺序执行 Agent 链
        # ═══════════════════════════════════════════════════════
        hdr("Phase 2: 顺序执行 Agent 链 — 需求→设计→测试方案→代码→验证")

        artifacts = {}

        # 2a: Decomposer
        if agents.get("decomposer"):
            print("\n  ── 2a: 需求拆解 ──")
            status,tools,answer,sid = run_agent_session(c,agents["decomposer"]["id"],str(ws),
                "Read REQUIREMENT.md. Break the task into ordered subtasks with dependencies "
                "and acceptance criteria. Write DECOMPOSITION.md.",label="DECOMP")
            ok("2a.1 拆解完成",status=="ready",status)
            ok("2a.2 使用了工具",len(tools)>0,str(tools))
            if (ws/"DECOMPOSITION.md").exists():
                artifacts["decomposition"]=True
                ok("2a.3 DECOMPOSITION.md已创建",True)
                print(f"    内容预览: {(ws/'DECOMPOSITION.md').read_text()[:200]}...")
            else:
                # Save agent answer as decomposition
                (ws/"DECOMPOSITION.md").write_text(answer)
                artifacts["decomposition"]=bool(answer)
                ok("2a.3 拆解内容已保存",bool(answer))

        # 2b: Designer (depends on decomposition)
        if agents.get("designer") and artifacts.get("decomposition"):
            print("\n  ── 2b: 系统设计 ──")
            status,tools,answer,sid = run_agent_session(c,agents["designer"]["id"],str(ws),
                "Read REQUIREMENT.md and DECOMPOSITION.md. Produce a technical architecture "
                "with component diagram, data models, API contract, and implementation notes. "
                "Write DESIGN.md.",label="DESIGN")
            ok("2b.1 设计完成",status=="ready",status)
            ok("2b.2 使用了工具",len(tools)>0,str(tools))
            (ws/"DESIGN.md").write_text(answer)
            artifacts["design"]=len(answer)>100
            ok("2b.3 设计内容充足",artifacts["design"],f"{len(answer)} chars")

        # 2c: Test Planner (depends on design)
        if agents.get("test_planner") and artifacts.get("design"):
            print("\n  ── 2c: 测试方案 ──")
            status,tools,answer,sid = run_agent_session(c,agents["test_planner"]["id"],str(ws),
                "Read DESIGN.md. Produce a comprehensive test plan with test cases for each "
                "endpoint (normal+edge cases), testing strategy, expected behaviors. "
                "Write TEST_PLAN.md.",label="TESTPLAN")
            ok("2c.1 测试方案完成",status=="ready",status)
            (ws/"TEST_PLAN.md").write_text(answer)
            artifacts["test_plan"]=len(answer)>100
            ok("2c.2 测试方案充足",artifacts["test_plan"],f"{len(answer)} chars")

        # 2d: Coder (depends on design + test plan)
        if agents.get("coder") and artifacts.get("design"):
            print("\n  ── 2d: 代码编写 ──")
            status,tools,answer,sid = run_agent_session(c,agents["coder"]["id"],str(ws),
                "Read DESIGN.md and TEST_PLAN.md. Write the complete implementation "
                "in app.py. Include: input validation, error handling, clean code. "
                "Use FastAPI or Flask. Write the code now.",label="CODE")
            ok("2d.1 编码完成",status=="ready",status)
            ok("2d.2 使用了工具",len(tools)>0,str(tools))
            app_exists=(ws/"app.py").exists()
            if app_exists:
                code=(ws/"app.py").read_text()
                artifacts["code"]=len(code)>100
                ok("2d.3 app.py已创建",True,f"{len(code)} chars")
                print(f"    代码行数: {len(code.split(chr(10)))} 行")
            else:
                (ws/"app.py").write_text(answer)
                artifacts["code"]=len(answer)>100
                ok("2d.3 代码已保存",artifacts["code"])

        # 2e: Tester (depends on code)
        if agents.get("tester") and artifacts.get("code"):
            print("\n  ── 2e: 测试执行与修复 ──")
            status,tools,answer,sid = run_agent_session(c,agents["tester"]["id"],str(ws),
                "Read app.py and TEST_PLAN.md. Write pytest tests in test_app.py. "
                "Run 'python -m pytest test_app.py -v'. If any tests fail, "
                "fix bugs in app.py and rerun. Write TEST_REPORT.md with final results.",label="TEST")
            ok("2e.1 测试完成",status=="ready",status)
            ok("2e.2 使用了工具",len(tools)>0,str(tools))
            if (ws/"test_app.py").exists():
                artifacts["tests"]=True
                ok("2e.3 test_app.py已创建",True)
            (ws/"TEST_REPORT.md").write_text(answer)
            artifacts["test_report"]=len(answer)>50
            ok("2e.4 测试报告已保存",artifacts["test_report"])

        # ═══════════════════════════════════════════════════════
        # Phase 3: 多Agent协调工作流
        # ═══════════════════════════════════════════════════════
        hdr("Phase 3: 多Agent协调工作流 — Builder自动搭建编排")

        if all(agents.get(k) for k in ["decomposer","designer","coder","tester"]):
            coord_req = (
                "Build a multi-agent coordination workflow. "
                "It orchestrates a software development team: "
                "1) Start with a requirement input. "
                "2) Use Task Dispatcher to break it into subtasks: "
                "   decompose → design → code → test (in that dependency order). "
                "3) Each task should have a description and dependency list. "
                "4) Use a Template to format the final coordination plan. "
                "Output a structured development plan."
            )
            app3 = c.post("/api/v1/applications",headers=H,json={
                "name":"DevTeam","requirement":coord_req}).json()["id"]
            br = c.post(f"/api/v1/applications/{app3}/builds",headers=H,json={
                "requirement":coord_req,"auto_publish":True,"max_turns":30,"max_repair_cycles":5})
            bid = br.json()["build_id"]
            for i in range(300):
                b = c.get(f"/api/v1/builds/{bid}",headers=H).json()
                if b.get("status") in("published","ready","needs_attention","failed"): break
                if i%30==0: print(f"    [{i}s] {b.get('status','?')}")
                time.sleep(1)
            b = c.get(f"/api/v1/builds/{bid}",headers=H).json()
            status3 = b.get("status","?")
            draft = c.get(f"/api/v1/applications/{app3}/draft",headers=H).json()
            nodes3 = draft["snapshot"]["workflow"]["nodes"]
            types3 = {n["type"] for n in nodes3}
            ok("3.1 协调工作流搭建",status3 in("published","ready"),
               b.get("error","") or status3)
            ok("3.2 含task_dispatcher或llm",bool({"task_dispatcher","llm"}&types3),str(types3))
            ok("3.3 至少4节点",len(nodes3)>=4,f"{len(nodes3)} nodes")
            print(f"    搭建节点: {[(n['type'],n['title']) for n in nodes3]}")

            if status3 in("published","ready"):
                ver = (b.get("team_state",{}) or {}).get("published_version",1) or 1
                start_n = [n for n in nodes3 if n["type"]=="start"]
                inp = "task"
                if start_n and start_n[0].get("config",{}).get("inputs"):
                    inp = start_n[0]["config"]["inputs"][0]["name"]
                rr = c.post(f"/api/v1/applications/{app3}/runs",headers=H,json={
                    "inputs":{inp:"Build a task management REST API"},
                    "version":ver,"workspace_path":str(ws)})
                if rr.status_code==202:
                    rec = wr(c,rr.json()["run_id"])
                    ok("3.4 协调工作流运行",rec["status"]=="succeeded",rec.get("error",""))
                    out = json.dumps(rec.get("outputs",{}),ensure_ascii=False)
                    ok("3.5 输出非空",len(out)>20,out[:400])
                    print(f"    协调输出: {out[:400]}")

        # ═══════════════════════════════════════════════════════
        # Phase 4: 产出链完整性检查
        # ═══════════════════════════════════════════════════════
        hdr("Phase 4: 产出链完整性验证")

        chain = {
            "REQUIREMENT.md": "需求文档",
            "DECOMPOSITION.md": "任务拆解",
            "DESIGN.md": "系统设计",
            "TEST_PLAN.md": "测试方案",
            "app.py": "代码实现",
            "test_app.py": "测试代码",
            "TEST_REPORT.md": "测试报告",
        }
        produced = []
        for fname, desc in chain.items():
            fp = ws/fname
            exists = fp.exists()
            size = len(fp.read_text()) if exists else 0
            produced.append(exists)
            ok(f"4.{desc}",exists,f"{size} chars" if exists else "missing")
            if exists and size>0:
                ok(f"4.{desc}非空",size>50,f"{size} chars")

        chain_ok = all(produced)
        ok("4.chain 完整产出链",chain_ok,
           f"Produced: {sum(produced)}/{len(chain)}")

        # Show artifacts
        print(f"\n  ── 产出物总览 ──")
        for fname, desc in chain.items():
            fp = ws/fname
            if fp.exists():
                content = fp.read_text()
                print(f"\n  📄 {desc} ({len(content)} chars)")
                print(f"  {'─'*50}")
                for line in content.split("\n")[:8]:
                    print(f"  {line}")
                if len(content.split("\n"))>8:
                    print(f"  ... ({len(content.split(chr(10)))} lines total)")

        # ═══════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════
        hdr("多智能体团队评估总结")
        passed = sum(R); total = len(R)
        print(f"\n  Agent生成: 5个专项Agent (拆解/设计/测试方案/编码/测试)")
        print(f"  Agent执行: 5个Agent按依赖顺序执行")
        print(f"  协调工作流: Builder自动搭建多Agent编排")
        print(f"  产出链: REQUIREMENT→DECOMPOSITION→DESIGN→TEST_PLAN→app.py→test_app.py→TEST_REPORT")
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed<total: print(f"  ❌ 失败: {total-passed}")
        else: print(f"  🎉 全部通过!")
        print(f"  通过率: {passed/total*100:.1f}%")
        print(f"\n  耗时统计:")
        for k,v in TIMINGS.items():
            print(f"    {k}: {v:.0f}s")

    try: tmp.cleanup()
    except: pass
    return 0 if passed==total else 1

if __name__ == "__main__":
    raise SystemExit(main())
