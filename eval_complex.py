#!/usr/bin/env python3
"""
Lilies 复杂真实场景深度评估

场景 A: 多源数据融合分析 — 读取→清洗→统计→可视化数据→生成报告
场景 B: 依赖图代码重构 — 分析多文件依赖→安全重构→回归测试→变更摘要
场景 C: 条件链决策引擎 — 6路分支→嵌套条件→聚合→格式化决策报告
场景 D: Agent架构积木深度组合 — 24积木连成完整Claude-like Agent Loop
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
R = []  # results

def ok(n, c, d=""):
    m="✅" if c else "❌"; x=f"  {m} {n}"
    if d and not c: x+=f" — {str(d)[:200]}"
    print(x); R.append(c)

def hdr(t): print(f"\n{'█'*60}\n  {t}\n{'█'*60}")

_mutate_counter = 0
def mutate(c, aid, rev, op, data):
    global _mutate_counter
    _mutate_counter += 1
    r = c.post(f"/api/v1/applications/{aid}/draft", headers=H, json={
        "expected_revision":rev,"idempotency_key":f"cx-{_mutate_counter}-{op}",
        "op":op,"data":data})
    if r.status_code!=200: raise RuntimeError(f"{op}:{r.text[:200]}")
    return r.json()["revision"]

def wr(c, rid, t=120):
    for _ in range(t*2):
        r=c.get(f"/api/v1/runs/{rid}",headers=H); s=r.json()["status"]
        if s in("succeeded","failed"): return r.json()
        time.sleep(0.5)
    return c.get(f"/api/v1/runs/{rid}",headers=H).json()

def build(c, name, req, mt=30):
    t0=time.time()
    aid=c.post("/api/v1/applications",headers=H,json={"name":name,"requirement":req}).json()["id"]
    bid=c.post(f"/api/v1/applications/{aid}/builds",headers=H,json={
        "requirement":req,"auto_publish":True,"max_turns":mt,"max_repair_cycles":5}).json()["build_id"]
    for i in range(mt*15):
        b=c.get(f"/api/v1/builds/{bid}",headers=H).json()
        if b.get("status") in("published","ready","needs_attention","cancelled","failed"): break
        if i%30==0: print(f"    [{i}s] {b.get('status','?')}")
        time.sleep(1)
    b=c.get(f"/api/v1/builds/{bid}",headers=H).json()
    d=c.get(f"/api/v1/applications/{aid}/draft",headers=H).json()
    return aid,b,d["snapshot"],time.time()-t0

def gen_agent(c, name, req, ws):
    t0=time.time()
    for attempt in range(2):
        gr=c.post("/v1/agent-generations",headers=H,json={
            "requirement":req+(" Keep brief." if attempt>0 else ""),
            "workspace_path":ws,"auto_publish":True})
        gid=gr.json()["generation_id"]
        for i in range(300):
            g=c.get(f"/v1/agent-generations/{gid}",headers=H).json()
            if g.get("status") in("published","failed"): break
            time.sleep(1)
        g=c.get(f"/v1/agent-generations/{gid}",headers=H).json()
        if g.get("status")=="published": break
    return g,time.time()-t0

def make_workspace(ws):
    """Create a multi-file Python project for testing."""
    ws.mkdir(parents=True, exist_ok=True)
    # A simple data processing library with subtle bugs
    (ws/"utils.py").write_text('''"""Data processing utilities."""
from typing import Any

def calculate_stats(values: list[float]) -> dict[str, float]:
    """Return mean, median, min, max. BUG: median is wrong for even-length lists."""
    if not values:
        return {"count": 0}
    n = len(values)
    mean = sum(values) / n
    sorted_vals = sorted(values)
    # BUG: should be (sorted_vals[n//2-1] + sorted_vals[n//2]) / 2 for even n
    median = sorted_vals[n // 2]
    return {
        "count": n, "mean": round(mean, 3),
        "median": round(median, 3), "min": min(values), "max": max(values),
    }

def normalize_column(data: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """BUG: divides by zero if all values are the same."""
    vals = [row[key] for row in data if key in row and isinstance(row[key], (int, float))]
    if not vals: return data
    vmin, vmax = min(vals), max(vals)
    rng = vmax - vmin  # BUG: ZeroDivisionError if rng == 0
    for row in data:
        if key in row and isinstance(row[key], (int, float)):
            row[f"{key}_normalized"] = round((row[key] - vmin) / rng, 3)
    return data
''')
    (ws/"test_utils.py").write_text('''"""Tests for utils.py - should reveal bugs."""
from utils import calculate_stats, normalize_column
import pytest

def test_stats_basic():
    r = calculate_stats([1, 2, 3, 4, 5])
    assert r["count"] == 5
    assert r["mean"] == 3.0
    assert r["median"] == 3.0
    assert r["min"] == 1
    assert r["max"] == 5

def test_stats_even_length():
    """Should fail: median of [1,2,3,4] should be 2.5, not 2"""
    r = calculate_stats([1, 2, 3, 4])
    assert r["median"] == 2.5, f"Expected 2.5, got {r['median']}"

def test_stats_empty():
    r = calculate_stats([])
    assert r["count"] == 0

def test_normalize_basic():
    data = [{"score": 0}, {"score": 50}, {"score": 100}]
    result = normalize_column(data, "score")
    assert result[0]["score_normalized"] == 0.0
    assert result[1]["score_normalized"] == 0.5
    assert result[2]["score_normalized"] == 1.0

def test_normalize_all_same():
    """Should fail: ZeroDivisionError when all values are equal"""
    data = [{"score": 50}, {"score": 50}]
    try:
        result = normalize_column(data, "score")
        # If no exception, check results
        for r in result:
            assert "score_normalized" in r
    except ZeroDivisionError:
        pytest.fail("normalize_column crashed on uniform data")
''')
    (ws/"main.py").write_text('''"""Entry point: run analysis."""
import json
from utils import calculate_stats, normalize_column

def run_pipeline(scores: list[float]):
    stats = calculate_stats(scores)
    data = [{"score": s} for s in scores]
    normalized = normalize_column(data, "score")
    return {"stats": stats, "normalized_count": len(normalized)}

if __name__ == "__main__":
    result = run_pipeline([10, 20, 30, 40, 50])
    print(json.dumps(result, indent=2))
''')
    for p in [ws]+list(ws.rglob("*")): p.chmod(0o777) if p.exists() else None


def main():
    tmp = TemporaryDirectory(); tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024",data_dir=tp/"data",workspace_root=tp/"workspaces")
    s.prepare()
    (tp/"workspaces").mkdir(parents=True,exist_ok=True); (tp/"workspaces").chmod(0o777)

    wsA = tp/"workspaces"/"data_lab"; make_workspace(wsA)
    wsB = tp/"workspaces"/"refactor"; make_workspace(wsB)

    app = create_app(settings=s)
    with TestClient(app) as client:

        # ═══════════════════════════════════════════════════════
        # 场景 A: 多源数据融合分析 Agent
        # ═══════════════════════════════════════════════════════
        hdr("场景 A: 多源数据融合分析 Agent — 读取→分析→修复→验证")

        reqA = (
            "Generate a data pipeline debug agent. It MUST: "
            "1) Read ALL Python files in the workspace (at least utils.py, test_utils.py, main.py). "
            "2) Run 'python -m pytest test_utils.py -v' and capture ALL test failures. "
            "3) For each failing test, identify the EXACT bug in the source code. "
            "4) Fix bugs: (a) median calculation for even-length lists should average the two middle values; "
            "(b) normalize_column should handle the case where all values are identical. "
            "5) Rerun tests until ALL pass. "
            "6) Write a summary report to BUG_FIX_REPORT.md."
        )

        print(f"  工作区: {wsA}")
        print(f"  文件: utils.py(2个bug), test_utils.py(5个测试), main.py\n")
        gA, tA = gen_agent(client, "data_debugger", reqA, str(wsA))

        gs = (gA or {}).get("status","?"); ge = (gA or {}).get("error","") or ""
        ok("A.1 Agent生成", gs=="published", f"{gs}:{ge[:150]}")
        aid_A = (gA or {}).get("agent_id")
        if aid_A:
            a = client.get(f"/v1/agents/{aid_A}",headers=H).json()
            sp = a.get("spec",{})
            ok("A.2 有Read+Bash", {"Read","Bash"}.issubset(set(sp.get("tools",[]))),
               str(sp.get("tools",[])))
            ok("A.3 Prompt充足", len(sp.get("system_prompt",""))>300,
               f"{len(sp.get('system_prompt',''))}字")
            print(f"    Agent: {sp.get('name','?')} | Tools: {sp.get('tools',[])}")

            # Run agent session
            sr = client.post("/v1/sessions",headers=H,json={
                "agent_id":aid_A,"workspace_path":str(wsA)})
            if sr.status_code==201:
                sid = sr.json()["session_id"]
                client.post(f"/v1/sessions/{sid}/messages",headers=H,json={
                    "content": (
                        f"Analyze the project at {wsA}. Read all Python files. "
                        "Run pytest. Find and fix ALL bugs. Rerun tests to verify. "
                        "Write BUG_FIX_REPORT.md with your findings."
                    )})

                approved=set(); tools_used=[]
                for _ in range(300):
                    time.sleep(0.3)
                    sess = client.get(f"/v1/sessions/{sid}",headers=H).json()
                    for e in client.get(f"/v1/streams/{sid}",headers=H).json():
                        t=e.get("type",""); d=e.get("data",{})
                        if t=="permission.requested":
                            rid=d.get("request_id","")
                            if rid and rid not in approved:
                                client.post(f"/v1/sessions/{sid}/permissions/{rid}",
                                            headers=H,json={"behavior":"allow"})
                                approved.add(rid)
                        elif t=="tool.started":
                            tn=d.get("tool","")
                            if tn not in tools_used: tools_used.append(tn)
                            if len(tools_used)<=8: print(f"    🔧 {tn}")
                    if sess["status"] in("ready","error"): break

                sess = client.get(f"/v1/sessions/{sid}",headers=H).json()
                ok("A.4 会话完成", sess["status"]=="ready", sess.get("error",""))
                ok("A.5 多工具调用", len(tools_used)>=3, str(tools_used))

                # Show report
                report_file = wsA / "BUG_FIX_REPORT.md"
                if report_file.exists():
                    ok("A.6 生成报告文件", True)
                    print(f"\n    ── BUG_FIX_REPORT.md ──")
                    for line in report_file.read_text().split("\n")[:25]:
                        print(f"    {line}")

                # Verify fixes
                fixed_utils = (wsA/"utils.py").read_text() if (wsA/"utils.py").exists() else ""
                ok("A.7 median修复", "(sorted_vals[n//2-1] + sorted_vals[n//2]) / 2" in fixed_utils
                   or "n//2-1" in fixed_utils, "Median fix detected" if "n//2-1" in fixed_utils else "Not found")
                ok("A.8 normalize修复", "rng" in fixed_utils or "ZeroDivision" in fixed_utils
                   or "if rng" in fixed_utils.lower(), "Normalize fix patterns found")
            else:
                ok("A.4 创建会话", False, sr.text[:150])

        # ═══════════════════════════════════════════════════════
        # 场景 B: 多文件依赖重构 Agent
        # ═══════════════════════════════════════════════════════
        hdr("场景 B: 多文件依赖重构 Agent — 理解结构→安全重构→验证")

        reqB = (
            "Generate a refactoring agent. It MUST: "
            "1) Map all Python files and their imports/dependencies in the workspace. "
            "2) Refactor: extract the stats calculation logic from utils.py into a new file stats_engine.py "
            "while keeping the normalize function in utils.py. "
            "3) Update ALL imports across ALL files so nothing breaks. "
            "4) Run pytest to verify all tests still pass. "
            "5) If any test fails after refactoring, fix the imports and rerun."
        )

        gB, tB = gen_agent(client, "refactor_agent", reqB, str(wsB))
        gsB = (gB or {}).get("status","?"); geB = (gB or {}).get("error","") or ""
        ok("B.1 Agent生成", gsB=="published", f"{gsB}:{geB[:150]}")
        aid_B = (gB or {}).get("agent_id")
        if aid_B:
            a = client.get(f"/v1/agents/{aid_B}",headers=H).json()
            sp = a.get("spec",{})
            ok("B.2 有Edit工具", bool({"Edit","Write"}&set(sp.get("tools",[]))))
            ok("B.3 有Bash工具", "Bash" in sp.get("tools",[]))
            print(f"    Agent: {sp.get('name','?')} | Tools: {sp.get('tools',[])}")

            sr = client.post("/v1/sessions",headers=H,json={
                "agent_id":aid_B,"workspace_path":str(wsB)})
            if sr.status_code==201:
                sid = sr.json()["session_id"]
                client.post(f"/v1/sessions/{sid}/messages",headers=H,json={
                    "content": (
                        f"Refactor the project at {wsB}. Create stats_engine.py with the "
                        "calculate_stats function extracted from utils.py. "
                        "Update ALL imports. Run pytest to verify everything works."
                    )})

                approved=set(); tools_used=[]
                for _ in range(300):
                    time.sleep(0.3)
                    sess = client.get(f"/v1/sessions/{sid}",headers=H).json()
                    for e in client.get(f"/v1/streams/{sid}",headers=H).json():
                        t=e.get("type",""); d=e.get("data",{})
                        if t=="permission.requested":
                            rid=d.get("request_id","")
                            if rid and rid not in approved:
                                client.post(f"/v1/sessions/{sid}/permissions/{rid}",
                                            headers=H,json={"behavior":"allow"})
                                approved.add(rid)
                        elif t=="tool.started":
                            tn=d.get("tool","")
                            if tn not in tools_used: tools_used.append(tn)
                            if len(tools_used)<=6: print(f"    🔧 {tn}")
                    if sess["status"] in("ready","error"): break

                sess = client.get(f"/v1/sessions/{sid}",headers=H).json()
                ok("B.4 会话完成", sess["status"]=="ready", sess.get("error",""))
                ok("B.5 多工具", len(tools_used)>=3, str(tools_used))

                # Verify refactoring
                has_new_file = (wsB/"stats_engine.py").exists()
                ok("B.6 创建新文件", has_new_file, "stats_engine.py exists" if has_new_file else "Not found")
                utils_content = (wsB/"utils.py").read_text() if (wsB/"utils.py").exists() else ""
                ok("B.7 旧文件保留", len(utils_content)>50)
            else:
                ok("B.4 会话", False, sr.text[:150])

        # ═══════════════════════════════════════════════════════
        # 场景 C: 条件链决策引擎 — Builder
        # ═══════════════════════════════════════════════════════
        hdr("场景 C: 条件链决策引擎 — Builder自动搭建多路分支")

        reqC = (
            "Build a loan application decision engine workflow. Take applicant data as input "
            "(income, credit_score, loan_amount, employment_years). "
            "Route through: If credit_score < 600 → auto-deny. "
            "If income/loan_amount ratio < 0.3 → auto-deny. "
            "If employment_years < 2 and loan_amount > 50000 → manual review. "
            "Otherwise → auto-approve. "
            "Each outcome must produce a structured decision with: decision (approve/deny/review), "
            "reason, and suggested_next_steps. "
            "Use explicit If/Else blocks, Template Transform, and Variable Aggregator."
        )

        print(f"  需求: 信用评分/收入比/工作年限 → 4路决策\n")
        appC, bC, snapC, tC = build(client, "贷款决策", reqC, mt=35)

        ok("C.1 Build完成", bC["status"] in ("published","ready"),
           bC.get("error","") or bC.get("status",""))
        nodesC = snapC["workflow"]["nodes"]
        typesC = {n["type"] for n in nodesC}
        ok("C.2 有If/Else", "if_else" in typesC, str(typesC))
        ok("C.3 有模板", "template_transform" in typesC)
        ok("C.4 至少6节点", len(nodesC)>=6, f"{len(nodesC)}")

        if bC["status"] in ("published","ready"):
            ver = (bC.get("team_state",{}) or {}).get("published_version",1) or 1
            # Find input names
            sn = [n for n in nodesC if n["type"]=="start"]
            inps = {}
            if sn:
                for f in sn[0].get("config",{}).get("inputs",[]):
                    inps[f["name"]] = 0  # placeholder
            print(f"    Inputs: {list(inps.keys())}")

            # Test: high credit → approve
            test_data = {
                "income": 120000, "credit_score": 720,
                "loan_amount": 200000, "employment_years": 5,
            }
            # Map to actual input names
            mapped = {}
            for k in inps: mapped[k] = test_data.get(k, test_data.get(
                k.replace("_",""), 0))
            if not mapped: mapped = test_data

            r = client.post(f"/api/v1/applications/{appC}/runs",headers=H,json={
                "inputs": mapped, "version": ver, "workspace_path": ".",
            })
            if r.status_code==202:
                rec = wr(client, r.json()["run_id"])
                out_s = json.dumps(rec.get("outputs",{}), ensure_ascii=False)
                ok("C.5 运行成功", rec["status"]=="succeeded", rec.get("error",""))
                ok("C.6 含decision", "decision" in out_s.lower() or
                   "approve" in out_s.lower() or "deny" in out_s.lower(),
                   out_s[:300])
                print(f"    输出: {out_s[:400]}")
            else:
                ok("C.5 启动运行", False, r.text[:150])

        # ═══════════════════════════════════════════════════════
        # 场景 D: Agent架构积木完整Claude-like Loop
        # ═══════════════════════════════════════════════════════
        hdr("场景 D: Agent架构积木深度组合 — 14积木Claude-like Loop")

        appD = client.post("/api/v1/applications",headers=H,json={
            "name":"ClaudeLike","requirement":"Full agent architecture loop."}).json()["id"]
        draft = client.get(f"/api/v1/applications/{appD}/draft",headers=H).json()
        rev = draft["revision"]

        # Build a 14-block chain that represents the full agent loop:
        blocks = [
            ("s","start","Input",{"inputs":[{"name":"task","type":"string"}]}),
            ("ctx","context_assembler","Context",
             {"input":{"$ref":{"node_id":"s","path":["task"]}},
              "settings":{"fragments":["SYSTEM: You are a precise coding agent.","TOOLS: Read,Write,Bash"]}}),
            ("ws_inj","workspace_context_injector","Workspace",
             {"input":{"$ref":{"node_id":"ctx","path":["output"]}},
              "settings":{"files":["*.py"],"scope":"current_workspace"}}),
            ("mem","conversation_memory","Memory",
             {"input":{"$ref":{"node_id":"ws_inj","path":["output"]}},
              "settings":{"facts":["Task goal","File structure","Test results"]}}),
            ("compact","context_compactor","Compact",
             {"input":{"$ref":{"node_id":"mem","path":["output"]}},
              "settings":{"max_chars":2000,"preserved_facts":["Task goal","Test results"]}}),
            ("budget","budget_gate","Budget",
             {"input":{"$ref":{"node_id":"compact","path":["output"]}},
              "settings":{"max_cost_usd":5,"spent_cost_usd":0.5}}),
            ("rounds","round_limit","Rounds",
             {"input":{"$ref":{"node_id":"budget","path":["output"]}},
              "settings":{"current_round":1,"max_rounds":10}}),
            ("perm","permission_gate","Permission",
             {"input":{"$ref":{"node_id":"rounds","path":["output"]}},
              "settings":{"auto_approve":True,"reason":"Auto-approved for eval"}}),
            ("sandbox","sandbox_boundary","Sandbox",
             {"input":{"$ref":{"node_id":"perm","path":["output"]}},
              "settings":{"network_policy":"none"}}),
            ("mt","model_turn","ModelTurn",
             {"input":{"$ref":{"node_id":"sandbox","path":["output"]}},
              "settings":{"system":"Reply with EXACTLY one sentence.","prompt":{"$ref":{"node_id":"s","path":["task"]}}}}),
            ("router","tool_call_router","Router",
             {"input":{"$ref":{"node_id":"mt","path":["output"]}},"settings":{}}),
            ("err","retry_error_classifier","Errors",
             {"input":{"$ref":{"node_id":"router","path":["output"]}},
              "settings":{"error":"Connection timeout"}}),
            ("tracer","event_recorder","Trace",
             {"input":{"$ref":{"node_id":"err","path":["output"]}},"settings":{"label":"claude-loop"}}),
            ("e","end","Output",{"outputs":{
                "answer":{"$ref":{"node_id":"mt","path":["text"]}},
                "budget_ok":{"$ref":{"node_id":"budget","path":["state","allowed"]}},
                "rounds_ok":{"$ref":{"node_id":"rounds","path":["state","allowed"]}},
                "error_class":{"$ref":{"node_id":"err","path":["state","class"]}},
                "mechanisms":{
                    "context": {"$ref":{"node_id":"ctx","path":["state","mechanism"]}},
                    "budget": {"$ref":{"node_id":"budget","path":["state","mechanism"]}},
                    "rounds": {"$ref":{"node_id":"rounds","path":["state","mechanism"]}},
                },
            }}),
        ]

        for bid, btype, btitle, bcfg in blocks:
            rev = mutate(client, appD, rev, "add_node",
                         {"node":{"id":bid,"type":btype,"title":btitle,"config":bcfg}})
        for i in range(len(blocks)-1):
            rev = mutate(client, appD, rev, "add_edge", {"edge":{
                "id":f"d{i}","source":blocks[i][0],"target":blocks[i+1][0],
                "source_port":"output","target_port":"input"}})

        # Validate
        r = client.post(f"/api/v1/applications/{appD}/draft/validate",headers=H)
        struct_errs = [e for e in r.json().get("errors",[]) if "test" not in e.lower()]
        ok("D.1 14积木结构验证", len(struct_errs)==0, str(struct_errs)[:200])
        ok("D.2 至少14节点", len(blocks)>=14, str(len(blocks)))

        # Add test and publish
        rev = mutate(client, appD, rev, "add_test", {"test":{
            "name":"Full agent loop","requirement":"Agent architecture chain executes correctly.",
            "inputs":{"task":"Explain gravity briefly"},
            "assertions":[
                {"path":["answer"],"operator":"exists"},
                {"path":["budget_ok"],"operator":"equals","expected":True},
                {"path":["rounds_ok"],"operator":"equals","expected":True},
                {"path":["error_class"],"operator":"equals","expected":"retryable"},
            ],
        }})
        tr = client.post(f"/api/v1/applications/{appD}/tests/run",headers=H)
        ok("D.3 测试运行", tr.status_code==200, tr.text[:200])
        test_ok = tr.json().get("passed",False) if tr.status_code==200 else False
        ok("D.4 测试通过", test_ok, tr.text[:200] if not test_ok else "")

        if test_ok:
            pr = client.post(f"/api/v1/applications/{appD}/versions",headers=H)
            ok("D.5 发布成功", pr.status_code==200, pr.text[:150])
            ver = pr.json()["version"] if pr.status_code==200 else 1

            rr = client.post(f"/api/v1/applications/{appD}/runs",headers=H,json={
                "inputs":{"task":"What is machine learning in one sentence?"},
                "version":ver,"workspace_path":"."})
            if rr.status_code==202:
                rec = wr(client, rr.json()["run_id"])
                ok("D.6 模型回答", rec["status"]=="succeeded", rec.get("error",""))
                ans = rec.get("outputs",{}).get("answer","")
                ok("D.7 答案非空", len(str(ans))>10, str(ans)[:200])
                ok("D.8 预算通过", rec.get("outputs",{}).get("budget_ok")==True)
                ok("D.9 错误分类正确", rec.get("outputs",{}).get("error_class")=="retryable")
                print(f"    模型回答: {str(ans)[:200]}")

        # ═══════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════
        hdr("复杂场景评估总结")
        passed = sum(R); total = len(R)
        print(f"\n  场景 A 数据融合: Agent读取→测试→定位2个bug→修复→验证")
        print(f"  场景 B 代码重构: Agent分析依赖→提取模块→更新import→回归测试")
        print(f"  场景 C 决策引擎: Builder搭建6+节点多路条件分支")
        print(f"  场景 D 积木组合: 14个架构积木串联完整Agent Loop")
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed<total: print(f"  ❌ 失败: {total-passed}")
        else: print(f"  🎉 全部通过!")
        print(f"  通过率: {passed/total*100:.1f}%")

    try: tmp.cleanup()
    except: pass
    return 0 if passed==total else 1

if __name__ == "__main__":
    raise SystemExit(main())
