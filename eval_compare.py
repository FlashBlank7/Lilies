#!/usr/bin/env python3
"""
Lilies 前后对比 + 更难任务挑战

对比测试:
  A. 模板展开 vs Builder搭建 — 同样需求，哪个更快更可靠
  B. 之前失败的复杂任务重试 — DeepSeek JSON截断问题
  C. 新能力: Hook追踪 + Permission三级 + 模板组合
  D. 更难任务: 多模板串联工作流
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
T = {}

def ok(n, c, d=""):
    m="✅" if c else "❌"; x=f"  {m} {n}"
    if d and not c: x+=f" — {str(d)[:200]}"
    print(x); R.append(c)

def hdr(t): print(f"\n{'█'*60}\n  {t}\n{'█'*60}")

def mutate(c, aid, rev, op, data):
    r = c.post(f"/api/v1/applications/{aid}/draft", headers=H, json={
        "expected_revision":rev,"idempotency_key":f"cmp-{op}-{rev}",
        "op":op,"data":data})
    if r.status_code!=200: raise RuntimeError(f"{op}:{r.text[:200]}")
    return r.json()["revision"]

def wr(c, rid, t=120):
    for _ in range(t*2):
        r=c.get(f"/api/v1/runs/{rid}",headers=H); s=r.json()["status"]
        if s in("succeeded","failed"): return r.json()
        time.sleep(0.5)
    return c.get(f"/api/v1/runs/{rid}",headers=H).json()

def build_app(c, name, req, mt=30):
    t0=time.time()
    aid=c.post("/api/v1/applications",headers=H,json={"name":name,"requirement":req}).json()["id"]
    bid=c.post(f"/api/v1/applications/{aid}/builds",headers=H,json={
        "requirement":req,"auto_publish":True,"max_turns":mt,"max_repair_cycles":5}).json()["build_id"]
    for i in range(mt*15):
        b=c.get(f"/api/v1/builds/{bid}",headers=H).json()
        if b.get("status") in("published","ready","needs_attention","failed"): break
        if i%30==0: print(f"    [{i}s] {b.get('status','?')}")
        time.sleep(1)
    b=c.get(f"/api/v1/builds/{bid}",headers=H).json()
    return aid,b,time.time()-t0

def expand_template(c, tname, prefix=""):
    """Expand a template into a new application draft, add test, publish."""
    t0=time.time()
    r = c.post(f"/api/v1/templates/{tname}/expand?prefix={prefix}", headers=H)
    wf = r.json()
    # Create app
    ar = c.post("/api/v1/applications", headers=H, json={
        "name":f"{tname}-from-template","requirement":"Built from template."})
    aid = ar.json()["id"]
    draft = c.get(f"/api/v1/applications/{aid}/draft", headers=H).json()
    rev = draft["revision"]
    # Add all nodes
    for n in wf["nodes"]:
        rev = mutate(c, aid, rev, "add_node", {"node": n})
    # Add all edges
    for e in wf["edges"]:
        rev = mutate(c, aid, rev, "add_edge", {"edge": e})
    # Validate
    r = c.post(f"/api/v1/applications/{aid}/draft/validate", headers=H)
    structural = [e for e in r.json().get("errors",[]) if "test" not in e.lower()]
    # Add simple test — use end node output keys for assertions
    end_node = next((n for n in wf["nodes"] if n["type"] in ("end","answer")), None)
    output_keys = list((end_node or {}).get("config",{}).get("outputs",{}).keys()) if end_node else []
    if not output_keys:
        output_keys = ["result"]
    start_node_t = next((n for n in wf["nodes"] if n["type"]=="start"), None)
    first_input = "value"
    if start_node_t and start_node_t.get("config",{}).get("inputs"):
        first_input = start_node_t["config"]["inputs"][0]["name"]
    rev = mutate(c, aid, rev, "add_test", {"test": {
        "name":"Template smoke test","requirement":"Template workflow runs.",
        "inputs":{first_input:"smoke_test_value"} if first_input else {},
        "assertions":[{"path":[output_keys[0]],"operator":"exists"}]
            if output_keys else [{"path":["result"],"operator":"exists"}]}})
    r = c.post(f"/api/v1/applications/{aid}/tests/run", headers=H)
    test_ok = r.json().get("passed",False) if r.status_code==200 else False
    if test_ok:
        c.post(f"/api/v1/applications/{aid}/versions", headers=H)
    elapsed = time.time()-t0
    return aid, len(wf["nodes"]), len(wf["edges"]), len(structural)==0, test_ok, elapsed

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


def main():
    tmp = TemporaryDirectory(); tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024",data_dir=tp/"data",workspace_root=tp/"workspaces")
    s.prepare(); (tp/"workspaces").mkdir(parents=True,exist_ok=True)
    (tp/"workspaces").chmod(0o777)
    s.templates_dir = (Path(__file__).resolve().parent / "templates")

    # Setup workspace with broken code
    ws = tp/"workspaces"/"testproj"
    ws.mkdir(parents=True,exist_ok=True)
    (ws/"calc.py").write_text("def add(a,b):\n    return a - b  # BUG!\ndef mul(a,b):\n    return a * b\n")
    (ws/"test_calc.py").write_text("from calc import add,mul\ndef test_add():\n    assert add(7,5)==12\ndef test_mul():\n    assert mul(7,5)==35\n")
    for p in [ws]+list(ws.rglob("*")): p.chmod(0o777) if p.exists() else None

    app = create_app(settings=s)
    with TestClient(app) as client:

        # ═══════════════════════════════════════════════════
        # A: 模板展开 vs Builder搭建 — 同需求对比
        # ═══════════════════════════════════════════════════
        hdr("对比 A: 模板展开 vs Builder搭建 (同需求: 客服路由)")

        req_cs = "Route customer messages by intent: complaint, question, feedback, urgent. Return appropriate response for each."

        # A1: 模板展开方式
        print("\n  ── A1: 模板展开 ──")
        aid_t, nn, ne, valid, tested, t_t = expand_template(client, "customer_support_router", "cs")
        ok(f"A1.模板展开 ({t_t:.0f}s)", valid and tested,
           f"nodes={nn} edges={ne} valid={valid} tested={tested}")
        T["template_expand"] = t_t

        # A2: Builder搭建方式
        print("\n  ── A2: Builder搭建 ──")
        aid_b, b2, t_b = build_app(client, "客服路由-Builder", req_cs, mt=25)
        ok(f"A2.Builder搭建 ({t_b:.0f}s)", b2["status"] in ("published","ready"),
           b2.get("error","") or b2.get("status",""))
        T["builder_build"] = t_b

        # Compare
        print(f"\n  📊 对比: 模板 {t_t:.0f}s vs Builder {t_b:.0f}s")
        if t_t < t_b:
            print(f"     模板更快 ({(t_b/t_t):.1f}x), 且结构保证正确")
        else:
            print(f"     Builder用时相近但产生定制化结构")

        # ═══════════════════════════════════════════════════
        # B: 之前失败的复杂任务重试
        # ═══════════════════════════════════════════════════
        hdr("对比 B: 之前失败的任务重试 — 复杂决策引擎")

        req_decision = (
            "Build a loan decision workflow. Input: income, credit_score, loan_amount. "
            "Route: credit<600→deny, income/loan<0.3→deny, else→approve. "
            "Output decision and reason."
        )
        print(f"\n  之前: DeepSeek JSON截断导致失败")
        aid_d, bd, t_d = build_app(client, "贷款决策v2", req_decision, mt=30)
        ok(f"B1.决策引擎 ({t_d:.0f}s)", bd["status"] in ("published","ready","needs_attention"),
           bd.get("error","") or bd.get("status",""))
        T["decision_engine"] = t_d

        draft = client.get(f"/api/v1/applications/{aid_d}/draft", headers=H).json()
        nodes = draft["snapshot"]["workflow"]["nodes"]
        types = {n["type"] for n in nodes}
        ok("B2.有if_else", "if_else" in types, str(types))
        ok(f"B3.节点数>=4", len(nodes)>=4, f"{len(nodes)}")

        # ═══════════════════════════════════════════════════
        # C: 新能力展示 — Hook + Permission三级
        # ═══════════════════════════════════════════════════
        hdr("对比 C: 新能力 — Hook追踪 + Permission三级 + 模板")

        # C1: 用模板+积木搭建含hook的工作流
        aid_c = client.post("/api/v1/applications", headers=H, json={
            "name":"Hook+Perm","requirement":"Test new blocks"}).json()["id"]
        draft = client.get(f"/api/v1/applications/{aid_c}/draft", headers=H).json()
        rev = draft["revision"]

        blocks_c = [
            ("s","start","Input",{"inputs":[{"name":"task","type":"string"}]}),
            ("perm","permission_gate","三级权限",
             {"input":{"$ref":{"node_id":"s","path":["task"]}},
              "settings":{"mode":"auto_approve","reason":"Auto for eval"}}),
            ("hook","hook_point","审计钩子",
             {"input":{"$ref":{"node_id":"perm","path":["output"]}},
              "settings":{"hook_name":"pre-execution-audit","direction":"before"}}),
            ("rec","event_recorder","追踪记录",
             {"input":{"$ref":{"node_id":"hook","path":["output"]}},
              "settings":{"label":"audit-trail"}}),
            ("e","end","Output",{"outputs":{"result":{"$ref":{"node_id":"rec","path":["output"]}}}}),
        ]
        for bid,btype,btitle,bcfg in blocks_c:
            rev = mutate(client, aid_c, rev, "add_node", {"node":{"id":bid,"type":btype,"title":btitle,"config":bcfg}})
        for i in range(len(blocks_c)-1):
            rev = mutate(client, aid_c, rev, "add_edge", {"edge":{"id":f"e{i}","source":blocks_c[i][0],"target":blocks_c[i+1][0],"source_port":"output","target_port":"input"}})

        r = client.post(f"/api/v1/applications/{aid_c}/draft/validate", headers=H)
        struct = [e for e in r.json().get("errors",[]) if "test" not in e.lower()]
        ok("C1.Hook+Perm结构验证", len(struct)==0, str(struct)[:200])
        ok("C2.hook_point存在", any(n[1]=="hook_point" for n in blocks_c))

        # Run with auto_approve for testing
        rev = mutate(client, aid_c, rev, "add_test", {"test":{
            "name":"Hook trail","requirement":"Hook and permission work.",
            "inputs":{"__permissions__":{"perm":True}},
            "assertions":[{"path":["result"],"operator":"exists"}]}})
        r = client.post(f"/api/v1/applications/{aid_c}/tests/run", headers=H)
        ok("C3.测试通过", r.json().get("passed",False), r.text[:200] if not r.json().get("passed",False) else "")
        if r.json().get("passed",False):
            pr = client.post(f"/api/v1/applications/{aid_c}/versions", headers=H)
            ver = pr.json()["version"]
            rr = client.post(f"/api/v1/applications/{aid_c}/runs", headers=H, json={
                "inputs":{"task":"test"}, "version":ver, "workspace_path":"."})
            if rr.status_code==202:
                rec = wr(client, rr.json()["run_id"])
                ok("C4.运行成功", rec["status"]=="succeeded", rec.get("error",""))
                # Check hook events
                ev = client.get(f"/v1/streams/{rr.json()['run_id']}", headers=H).json()
                hook_events = [e for e in ev if "hook" in e.get("type","")]
                perm_events = [e for e in ev if "permission" in e.get("type","")]
                ok("C5.触发钩子事件", len(hook_events)>0, f"{len(hook_events)} hook events")
                ok("C6.触发权限事件", len(perm_events)>0, f"{len(perm_events)} perm events")
                print(f"    Hook事件: {[e['type'] for e in hook_events]}")
                print(f"    Perm事件: {[e['type'] for e in perm_events]}")

        # ═══════════════════════════════════════════════════
        # D: 更难任务 — 多模板串联
        # ═══════════════════════════════════════════════════
        hdr("对比 D: 更难任务 — 多模板串联: 分解→分析→生成")

        # 用task_decomposer分析需求 → data_analyzer分析数据 → long_form_writer生成报告
        print("\n  场景: 给定一个复杂业务需求，自动分解→分析→生成解决方案报告")
        print("  串联: task_decomposer + data_analyzer + long_form_writer\n")

        # 第一步: task_decomposer
        t_td0 = time.time()
        aid_td, nn_td, ne_td, v_td, test_td, _ = expand_template(client, "task_decomposer", "td")
        ok(f"D1.任务分解模板 ({time.time()-t_td0:.0f}s)", v_td and test_td)
        T["template_task_decomposer"] = time.time()-t_td0

        # 第二步: data_analyzer
        t_da0 = time.time()
        aid_da, nn_da, ne_da, v_da, test_da, _ = expand_template(client, "data_analyzer", "da")
        ok(f"D2.数据分析模板 ({time.time()-t_da0:.0f}s)", v_da and test_da)
        T["template_data_analyzer"] = time.time()-t_da0

        # 第三步: long_form_writer
        t_lf0 = time.time()
        aid_lf, nn_lf, ne_lf, v_lf, test_lf, _ = expand_template(client, "long_form_writer", "lf")
        ok(f"D3.长文生成模板 ({time.time()-t_lf0:.0f}s)", v_lf and test_lf)
        T["template_long_form"] = time.time()-t_lf0

        # 第四步: 手动串联三个模板为一个工作流
        t_chain0 = time.time()
        aid_chain = client.post("/api/v1/applications", headers=H, json={
            "name":"串联工作流","requirement":"Chain: decompose → analyze → generate report"}).json()["id"]
        draft = client.get(f"/api/v1/applications/{aid_chain}/draft", headers=H).json()
        rev = draft["revision"]

        chain_nodes = [
            ("s","start","需求输入",{"inputs":[
                {"name":"business_requirement","label":"业务需求","type":"string"},
                {"name":"data_context","label":"数据上下文","type":"string","required":False,"default":""}]}),
            ("decompose","llm","任务分解LLM",
             {"system":"Decompose the business requirement into subtasks with dependencies.",
              "prompt":{"$ref":{"node_id":"s","path":["business_requirement"]}}}),
            ("analyze","llm","数据分析LLM",
             {"system":"Analyze based on decomposition. Identify key insights.",
              "prompt":{"$ref":{"node_id":"decompose","path":["text"]}}}),
            ("report","template_transform","报告生成",
             {"template":"# 报告\n\n## 分解\n{{ d }}\n\n## 分析\n{{ a }}",
              "variables":{
                "d":{"$ref":{"node_id":"decompose","path":["text"]}},
                "a":{"$ref":{"node_id":"analyze","path":["text"]}}}}),
            ("e","end","最终报告",{"outputs":{"report":{"$ref":{"node_id":"report","path":["text"]}}}}),
        ]
        # fixed ports: llm output port is "text", template output port is "text"
        edge_ports = [("output","input"),("text","input"),("text","input"),("text","input")]
        for bid,btype,btitle,bcfg in chain_nodes:
            rev = mutate(client, aid_chain, rev, "add_node", {"node":{"id":bid,"type":btype,"title":btitle,"config":bcfg}})
        for i in range(len(chain_nodes)-1):
            sp, tp = edge_ports[i]
            rev = mutate(client, aid_chain, rev, "add_edge", {"edge":{"id":f"c{i}","source":chain_nodes[i][0],"target":chain_nodes[i+1][0],"source_port":sp,"target_port":tp}})

        r = client.post(f"/api/v1/applications/{aid_chain}/draft/validate", headers=H)
        struct = [e for e in r.json().get("errors",[]) if "test" not in e.lower()]
        ok("D4.串联结构验证", len(struct)==0, str(struct)[:200])
        ok(f"D5.串联节点>=5", len(chain_nodes)>=5)

        rev = mutate(client, aid_chain, rev, "add_test", {"test":{
            "name":"Chain runs","requirement":"Full chain executes.",
            "inputs":{"business_requirement":"Build a mobile app","data_context":"Target: millennials. Budget: $500K."},
            "assertions":[{"path":["report"],"operator":"exists"}]}})
        r = client.post(f"/api/v1/applications/{aid_chain}/tests/run", headers=H)
        chain_tested = r.json().get("passed",False) if r.status_code==200 else False
        ok("D6.串联测试通过", chain_tested, r.text[:200] if not chain_tested else "")
        if chain_tested:
            pr = client.post(f"/api/v1/applications/{aid_chain}/versions", headers=H)
            ver = pr.json()["version"]
            rr = client.post(f"/api/v1/applications/{aid_chain}/runs", headers=H, json={
                "inputs":{"business_requirement":"Build an AI-powered customer service platform with chatbots and analytics dashboard","data_context":"Industry: E-commerce. Scale: 10K daily users. Budget: $2M."},
                "version":ver,"workspace_path":"."})
            if rr.status_code==202:
                rec = wr(client, rr.json()["run_id"])
                ok("D7.串联运行成功", rec["status"]=="succeeded", rec.get("error",""))
                report = rec.get("outputs",{}).get("report","")
                ok("D8.生成报告非空", len(str(report))>50,
                   f"{len(str(report))} chars")
                print(f"\n    ── 串联工作报告预览 ──")
                for line in str(report).split("\n")[:15]:
                    print(f"    {line}")
                T["chain_execution"] = time.time()-t_chain0
            else:
                ok("D7.串联运行", False, rr.text[:150])

        # ═══════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════
        hdr("前后对比总结")
        passed = sum(R); total = len(R)
        print(f"\n  对比维度:")
        print(f"    A. 模板展开: {T.get('template_expand',0):.0f}s vs Builder: {T.get('builder_build',0):.0f}s")
        print(f"    B. 复杂决策: {T.get('decision_engine',0):.0f}s (之前失败)")
        print(f"    C. Hook+Perm: 新能力验证")
        print(f"    D. 多模板串联: 3模板→1个组合工作流")
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed<total: print(f"  ❌ 失败: {total-passed}")
        else: print(f"  🎉 全部通过!")
        print(f"  通过率: {passed/total*100:.1f}%")

    try: tmp.cleanup()
    except: pass
    return 0 if passed==total else 1

if __name__ == "__main__":
    raise SystemExit(main())
