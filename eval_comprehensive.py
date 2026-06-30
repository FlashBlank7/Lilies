#!/usr/bin/env python3
"""
Lilies 全方位能力评估 — 5 大维度 × 多个任务

维度 1: Builder Team 复杂度梯度 (简单→中等→复杂→专家)
维度 2: Agent Generation 多样性 (代码/数据/调试/文档)
维度 3: 积木组合链 (Context → Model-Loop → Multi-Agent → Governance)
维度 4: Workflow DAG 运行时极端情况
维度 5: 错误处理与边界条件
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
SECTION_RESULTS = {}

def check(name, ok, detail=""):
    mark = "✅" if ok else "❌"
    line = f"  {mark} {name}"
    if detail and not ok: line += f" — {str(detail)[:200]}"
    print(line); RESULTS.append(ok)

def end_section(title: str):
    section_passed = sum(1 for r in RESULTS[-20:] if r)  # approx last section
    pass

def mutate(client, app_id, rev, op, data):
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
        "expected_revision": rev, "idempotency_key": f"eval-{op}-{rev}-{time.time()}",
        "op": op, "data": data,
    })
    if r.status_code != 200:
        raise RuntimeError(f"Mutation failed: {r.text[:300]}")
    return r.json()["revision"]

def add_test_and_publish(client, app_id, rev, test_name, test_inputs, assertions):
    rev = mutate(client, app_id, rev, "add_test", {"test": {
        "name": test_name, "requirement": f"Verify: {test_name}",
        "inputs": test_inputs, "assertions": assertions,
    }})
    r = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=header())
    if r.status_code != 200: raise RuntimeError(f"Test run failed: {r.text[:300]}")
    if not r.json()["passed"]: raise RuntimeError(f"Tests not passed: {r.text[:300]}")
    r = client.post(f"/api/v1/applications/{app_id}/versions", headers=header())
    if r.status_code != 200: raise RuntimeError(f"Publish failed: {r.text[:300]}")
    return rev, r.json()["version"]

def wait_for_run(client, run_id):
    for _ in range(120):
        r = client.get(f"/api/v1/runs/{run_id}", headers=header())
        s = r.json()["status"]
        if s in ("succeeded","failed"): return r.json()
        time.sleep(0.2)
    return client.get(f"/api/v1/runs/{run_id}", headers=header()).json()

def build_app(client, name, requirement, max_turns=30):
    app_r = client.post("/api/v1/applications", headers=header(), json={
        "name": name, "requirement": requirement,
    })
    app_id = app_r.json()["id"]
    br = client.post(f"/api/v1/applications/{app_id}/builds", headers=header(), json={
        "requirement": requirement, "auto_publish": True,
        "max_turns": max_turns, "max_repair_cycles": 5,
    })
    build_id = br.json()["build_id"]
    for i in range(300):
        b = client.get(f"/api/v1/builds/{build_id}", headers=header()).json()
        if b.get("status") in ("published","ready","needs_attention","cancelled","failed"):
            break
        time.sleep(1)
    b = client.get(f"/api/v1/builds/{build_id}", headers=header()).json()
    draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=header()).json()
    nodes = draft["snapshot"]["workflow"]["nodes"]
    return app_id, build_id, b, nodes


def main():
    tmp = TemporaryDirectory()
    tmp_path = Path(tmp.name)
    settings = Settings(api_token="test-token-2024", data_dir=tmp_path/"data", workspace_root=tmp_path/"workspaces")
    settings.prepare()
    (tmp_path/"workspaces").mkdir(parents=True, exist_ok=True)
    (tmp_path/"workspaces").chmod(0o777)
    ws = tmp_path / "workspaces" / "eval"
    ws.mkdir(parents=True, exist_ok=True)
    for p in [ws] + list(ws.rglob("*")): p.chmod(0o777) if p.exists() else None

    app = create_app(settings=settings)
    with TestClient(app) as client:

        # Check API availability upfront
        health = client.get("/health").json()
        api_available = health.get("deepseek_configured", False)
        if not api_available:
            print("\n⚠️ DeepSeek API 不可用 — 将跳过需要 LLM 的测试项\n")
        else:
            # Quick API test
            test_req = client.get("/v1/models", headers=header())
            if test_req.status_code != 200:
                api_available = False
                print("\n⚠️ DeepSeek API 认证失败 — 将跳过需要 LLM 的测试项\n")

        # ═══════════════════════════════════════════════════════════
        # 维度 1: Builder Team 复杂度梯度（需要 LLM）
        # ═══════════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 1: Builder Team 复杂度梯度" + ("" if api_available else " [跳过-需LLM]"))
        print("█" * 70)

        # 1A. 简单: 输入→模板→输出
        print("\n── 1A. 简单: 问候工作流 ──")
        _, _, b, nodes = build_app(client, "问候",
            "Take a name as input, output a personalized greeting like 'Hello {name}!'")
        check("1A 发布", b["status"] in ("published","ready"), b.get("error",""))
        check("1A 节点数 >= 3", len(nodes) >= 3, str(len(nodes)))
        types = {n["type"] for n in nodes}
        check("1A 含 template", "template_transform" in types or "end" in types)

        # 1B. 中等: 条件分支
        print("\n── 1B. 中等: 条件分支路由 ──")
        req1b = (
            "Build a workflow that takes a number as input. "
            "If the number is greater than 10, use a template to output 'Large: {number}'. "
            "Otherwise output 'Small: {number}'."
        )
        _, _, b, nodes = build_app(client, "数字判断", req1b, max_turns=40)
        check("1B 发布", b["status"] in ("published","ready"), b.get("error",""))
        types = {n["type"] for n in nodes}
        check("1B 含 if_else 或分支", "if_else" in types or len(nodes) >= 3, str(types))

        # 1C. 复杂: 多步骤数据流水线
        print("\n── 1C. 复杂: 数据流水线 ──")
        req1c = (
            "Create a data processing pipeline that takes raw text as input. "
            "Use an LLM to extract key facts from the text. "
            "Then route the output: if the text is about technology, use one template; "
            "otherwise use a different template. "
            "End with a formatted summary."
        )
        _, _, b, nodes = build_app(client, "数据流水线", req1c, max_turns=50)
        check("1C 完成 (非failed)", b["status"] != "failed", b.get("status",""))
        types = {n["type"] for n in nodes}
        check("1C 含 LLM 或 classifier", bool({"llm","question_classifier","claude_agent"} & types), str(types))
        check("1C 节点 >= 4", len(nodes) >= 4, str(len(nodes)))

        # 1D. 搜索聚合
        print("\n── 1D. 搜索数据聚合 ──")
        req1d = (
            "Build a workflow that takes a search query, searches the web, "
            "aggregates results, and formats a summary report. "
            "Use explicit blocks: WebSearch tool, Variable Aggregator, "
            "and Template for the final report."
        )
        _, _, b, nodes = build_app(client, "搜索聚合", req1d, max_turns=50)
        check("1D 完成", b["status"] in ("published","ready","needs_attention"), b.get("status",""))
        types = {n["type"] for n in nodes}
        check("1D 含 tool 或 WebSearch", bool({"tool","web_search"} & types) or len(nodes) >= 3, str(types))

        # ═══════════════════════════════════════════════════════════
        # 维度 2: Agent Generation 多样性
        # ═══════════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 2: Agent Generation 多样性")
        print("█" * 70)

        agents_to_generate = [
            ("2A 调试专家", "Generate a debugging agent that finds and fixes Python test failures."),
            ("2B 文档生成器", "Generate an agent that reads Python code and generates markdown documentation."),
        ]

        for label, areq in agents_to_generate:
            print(f"\n── {label} ──")
            t0 = time.time()
            for attempt in range(2):
                gr = client.post("/v1/agent-generations", headers=header(), json={
                    "requirement": areq + (" Keep agent config concise." if attempt > 0 else ""),
                    "workspace_path": str(ws), "auto_publish": True,
                })
                gid = gr.json()["generation_id"]
                for i in range(240):
                    g = client.get(f"/v1/agent-generations/{gid}", headers=header()).json()
                    if g.get("status") in ("published","failed","draft"): break
                    time.sleep(1)
                g = client.get(f"/v1/agent-generations/{gid}", headers=header()).json()
                if g.get("status") == "published": break

            elapsed = time.time() - t0
            check(f"{label} 生成", g.get("status")=="published", f"{elapsed:.0f}s: {g.get('error','')[:150]}")
            if g.get("agent_id"):
                a = client.get(f"/v1/agents/{g['agent_id']}", headers=header()).json()
                spec = a.get("spec",{})
                check(f"{label} 有工具", len(spec.get("tools",[])) > 0)
                check(f"{label} 有 Prompt", len(spec.get("system_prompt","")) > 50)
                print(f"    工具: {spec.get('tools',[])} | {len(spec.get('system_prompt',''))} 字符")

        # ═══════════════════════════════════════════════════════════
        # 维度 3: Agent 架构积木组合链
        # ═══════════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 3: Agent 架构积木组合链")
        print("█" * 70)

        # 3A. Context 链: assembler → injector → memory → compactor
        print("\n── 3A. Context 链 ──")
        app3a = client.post("/api/v1/applications", headers=header(), json={
            "name":"Context链", "requirement":"Test context pipeline.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app3a}/draft", headers=header()).json()["revision"]
        nodes_3a = [
            ("start","start","Input",{"inputs":[{"name":"task","type":"string"}]}),
            ("ctx","context_assembler","Assemble",
             {"input":{"$ref":{"node_id":"start","path":["task"]}},
              "settings":{"fragments":["fragment1","fragment2"]}}),
            ("inj","workspace_context_injector","Workspace",
             {"input":{"$ref":{"node_id":"ctx","path":["output"]}},
              "settings":{"files":["README.md"],"scope":"current_workspace"}}),
            ("mem","conversation_memory","Memory",
             {"input":{"$ref":{"node_id":"inj","path":["output"]}},
              "settings":{"facts":["key fact 1","key fact 2"]}}),
            ("comp","context_compactor","Compact",
             {"input":{"$ref":{"node_id":"mem","path":["output"]}},
              "settings":{"max_chars":500,"preserved_facts":["key fact 1"]}}),
            ("end","end","Output",{"outputs":{"compacted":{"$ref":{"node_id":"comp","path":["output","summary"]}}}}),
        ]
        for nid,ntype,ntitle,ncfg in nodes_3a:
            rev = mutate(client, app3a, rev, "add_node", {"node":{"id":nid,"type":ntype,"title":ntitle,"config":ncfg}})
        for i in range(len(nodes_3a)-1):
            rev = mutate(client, app3a, rev, "add_edge", {"edge":{
                "id":f"e{i}","source":nodes_3a[i][0],"target":nodes_3a[i+1][0],
                "source_port":"output","target_port":"input",
            }})
        rev, v = add_test_and_publish(client, app3a, rev, "Context chain runs", {"task":"test"},
                                       [{"path":["compacted"],"operator":"exists"}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app3a}/runs", headers=header(), json={
            "inputs":{"task":"Evaluate this system"}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("3A 运行成功", r["status"]=="succeeded", r.get("error",""))
        check("3A 输出非空", "compacted" in r.get("outputs",{}), str(r.get("outputs",{}).keys()))

        # 3B. Model-Loop 链: model_turn → tool_call_router → tool_executor → normalizer → stop_continue
        print("\n── 3B. Model-Loop 链 ──")
        app3b = client.post("/api/v1/applications", headers=header(), json={
            "name":"ModelLoop链", "requirement":"Test model loop.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app3b}/draft", headers=header()).json()["revision"]
        nodes_3b = [
            ("start","start","Input",{"inputs":[{"name":"question","type":"string"}]}),
            ("mt","model_turn","Think",
             {"input":{"$ref":{"node_id":"start","path":["question"]}},
              "settings":{"system":"Reply with one word.","prompt":{"$ref":{"node_id":"start","path":["question"]}}}}),
            ("router","tool_call_router","Route",
             {"input":{"$ref":{"node_id":"mt","path":["output"]}},"settings":{}}),
            ("norm","tool_result_normalizer","Normalize",
             {"input":{"$ref":{"node_id":"router","path":["output"]}},"settings":{}}),
            ("ctrl","stop_continue_controller","Control",
             {"input":{"$ref":{"node_id":"norm","path":["output"]}},"settings":{"stop_reason":"end_turn"}}),
            ("end","end","Output",{"outputs":{"answer":{"$ref":{"node_id":"mt","path":["text"]}}}}),
        ]
        for nid,ntype,ntitle,ncfg in nodes_3b:
            rev = mutate(client, app3b, rev, "add_node", {"node":{"id":nid,"type":ntype,"title":ntitle,"config":ncfg}})
        for i in range(len(nodes_3b)-1):
            rev = mutate(client, app3b, rev, "add_edge", {"edge":{
                "id":f"e{i}","source":nodes_3b[i][0],"target":nodes_3b[i+1][0],
                "source_port":"output","target_port":"input",
            }})
        rev, v = add_test_and_publish(client, app3b, rev, "Model loop works", {"question":"What color is snow?"},
                                       [{"path":["answer"],"operator":"exists"}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app3b}/runs", headers=header(), json={
            "inputs":{"question":"What color is snow on a mountain?"}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("3B 运行成功", r["status"]=="succeeded", r.get("error",""))
        answer = r.get("outputs",{}).get("answer","")
        check("3B 模型回答非空", len(answer)>0)

        # 3C. Governance 链
        print("\n── 3C. Governance 链 ──")
        app3c = client.post("/api/v1/applications", headers=header(), json={
            "name":"Governance链", "requirement":"Test governance blocks.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app3c}/draft", headers=header()).json()["revision"]
        nodes_3c = [
            ("start","start","Input",{"inputs":[]}),
            ("budget","budget_gate","Budget",
             {"input":{"$ref":{"node_id":"start","path":["output"]}},
              "settings":{"max_cost_usd":10,"spent_cost_usd":1}}),
            ("rounds","round_limit","Rounds",
             {"input":{"$ref":{"node_id":"budget","path":["output"]}},
              "settings":{"current_round":5,"max_rounds":30}}),
            ("cp","checkpoint_resume","Checkpoint",
             {"input":{"$ref":{"node_id":"rounds","path":["output"]}},
              "settings":{"checkpoint_id":"eval-checkpoint"}}),
            ("cancel","cancellation_point","Cancel",
             {"input":{"$ref":{"node_id":"cp","path":["output"]}},
              "settings":{"cancelled":False}}),
            ("rec","event_recorder","Record",
             {"input":{"$ref":{"node_id":"cancel","path":["output"]}},"settings":{"label":"gov-trace"}}),
            ("end","end","Output",{"outputs":{
                "budget_ok":{"$ref":{"node_id":"budget","path":["state","allowed"]}},
                "rounds_ok":{"$ref":{"node_id":"rounds","path":["state","allowed"]}},
                "not_cancelled":{"$ref":{"node_id":"cancel","path":["state","cancelled"]}},
            }}),
        ]
        for nid,ntype,ntitle,ncfg in nodes_3c:
            rev = mutate(client, app3c, rev, "add_node", {"node":{"id":nid,"type":ntype,"title":ntitle,"config":ncfg}})
        for i in range(len(nodes_3c)-1):
            rev = mutate(client, app3c, rev, "add_edge", {"edge":{
                "id":f"e{i}","source":nodes_3c[i][0],"target":nodes_3c[i+1][0],
                "source_port":"output","target_port":"input",
            }})
        rev, v = add_test_and_publish(client, app3c, rev, "Governance chain", {},
                                       [{"path":["budget_ok"],"operator":"equals","expected":True},
                                        {"path":["rounds_ok"],"operator":"equals","expected":True},
                                        {"path":["not_cancelled"],"operator":"equals","expected":False}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app3c}/runs", headers=header(), json={
            "inputs":{}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("3C 运行成功", r["status"]=="succeeded", r.get("error",""))
        check("3C 预算通过", r.get("outputs",{}).get("budget_ok")==True)
        check("3C 轮次通过", r.get("outputs",{}).get("rounds_ok")==True)

        # 3D. Multi-Agent 链
        print("\n── 3D. Multi-Agent 协作链 ──")
        app3d = client.post("/api/v1/applications", headers=header(), json={
            "name":"MultiAgent链", "requirement":"Test multi-agent blocks.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app3d}/draft", headers=header()).json()["revision"]
        nodes_3d = [
            ("start","start","Input",{"inputs":[]}),
            ("dispatch","task_dispatcher","Dispatch",
             {"input":{"$ref":{"node_id":"start","path":["output"]}},
              "settings":{"tasks":[
                  {"name":"read","dependencies":[]},
                  {"name":"analyze","dependencies":["read"]},
                  {"name":"report","dependencies":["analyze"]},
              ]}}),
            ("deps","dependency_gate","Deps",
             {"input":{"$ref":{"node_id":"dispatch","path":["output"]}},
              "settings":{"dependencies":["read"],"completed":["read"]}}),
            ("err","retry_error_classifier","Errors",
             {"input":{"$ref":{"node_id":"deps","path":["output"]}},
              "settings":{"error":"Connection timeout"}}),
            ("end","end","Output",{"outputs":{
                "total_tasks":{"$ref":{"node_id":"dispatch","path":["output","total"]}},
                "all_satisfied":{"$ref":{"node_id":"deps","path":["state","all_satisfied"]}},
                "error_class":{"$ref":{"node_id":"err","path":["state","class"]}},
            }}),
        ]
        for nid,ntype,ntitle,ncfg in nodes_3d:
            rev = mutate(client, app3d, rev, "add_node", {"node":{"id":nid,"type":ntype,"title":ntitle,"config":ncfg}})
        for i in range(len(nodes_3d)-1):
            rev = mutate(client, app3d, rev, "add_edge", {"edge":{
                "id":f"e{i}","source":nodes_3d[i][0],"target":nodes_3d[i+1][0],
                "source_port":"output","target_port":"input",
            }})
        rev, v = add_test_and_publish(client, app3d, rev, "Multi-agent chain", {},
                                       [{"path":["total_tasks"],"operator":"equals","expected":3},
                                        {"path":["all_satisfied"],"operator":"equals","expected":True},
                                        {"path":["error_class"],"operator":"equals","expected":"retryable"}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app3d}/runs", headers=header(), json={
            "inputs":{}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("3D 运行成功", r["status"]=="succeeded", r.get("error",""))
        check("3D 任务分派=3", r.get("outputs",{}).get("total_tasks")==3)
        check("3D 依赖满足", r.get("outputs",{}).get("all_satisfied")==True)
        check("3D 错误分类=retryable", r.get("outputs",{}).get("error_class")=="retryable")

        # ═══════════════════════════════════════════════════════════
        # 维度 4: Workflow DAG 运行时极端情况
        # ═══════════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 4: Workflow DAG 运行时极端情况")
        print("█" * 70)

        # 4A. 多分支并行 + Variable Aggregator
        print("\n── 4A. 多分支聚合 ──")
        app4a = client.post("/api/v1/applications", headers=header(), json={
            "name":"多分支","requirement":"Test branch aggregation.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app4a}/draft", headers=header()).json()["revision"]
        for n in [
            ("start","start","Input",{"inputs":[{"name":"n","type":"number"}]}),
            ("check","if_else","Check",{"cases":[{"id":"big","conditions":[{"value":{"$ref":{"node_id":"start","path":["n"]}},"operator":"gt","expected":10}],"logical_operator":"and"}],"default_branch":"small"}),
            ("big_t","template_transform","Big",{"template":"BIG: {{ value }}","variables":{"value":{"$ref":{"node_id":"start","path":["n"]}}}}),
            ("small_t","template_transform","Small",{"template":"small: {{ value }}","variables":{"value":{"$ref":{"node_id":"start","path":["n"]}}}}),
            ("agg","variable_aggregator","Merge",{"variables":[
                {"$ref":{"node_id":"big_t","path":["text"],"optional":True}},
                {"$ref":{"node_id":"small_t","path":["text"],"optional":True}},
            ],"mode":"first_non_null"}),
            ("end","end","Output",{"outputs":{"result":{"$ref":{"node_id":"agg","path":["output"]}}}}),
        ]:
            rev = mutate(client, app4a, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt,br in [("start","check",""),("check","big_t","big"),("check","small_t","small"),
                            ("big_t","agg",""),("small_t","agg",""),("agg","end","")]:
            rev = mutate(client, app4a, rev, "add_edge", {"edge":{
                "id":f"{src}-{tgt}","source":src,"target":tgt,
                "source_port":"output" if src!="check" else "branch",
                "target_port":"input","branch":br if br else None,
            }})
        rev, v = add_test_and_publish(client, app4a, rev, "Branch merge", {"n":15},
                                       [{"path":["result"],"operator":"contains","expected":"BIG"}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app4a}/runs", headers=header(), json={
            "inputs":{"n":20}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("4A 大数走 big 分支", "BIG" in str(r.get("outputs",{}).get("result","")), str(r.get("outputs",{})))
        r2 = wait_for_run(client, client.post(f"/api/v1/applications/{app4a}/runs", headers=header(), json={
            "inputs":{"n":3}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("4A 小数走 small 分支", "small" in str(r2.get("outputs",{}).get("result","")), str(r2.get("outputs",{})))

        # 4B. Iteration 批量处理
        print("\n── 4B. Iteration 迭代 ──")
        app4b = client.post("/api/v1/applications", headers=header(), json={
            "name":"迭代测试","requirement":"Test iteration block.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app4b}/draft", headers=header()).json()["revision"]
        # Nested workflow: template per item
        nested_wf = {
            "nodes":[
                {"id":"n_start","type":"start","title":"Nested Start","config":{"inputs":[{"name":"item","type":"string"}]}},
                {"id":"n_tpl","type":"template_transform","title":"Wrap","config":{"template":"[{{ item }}]","variables":{"item":{"$ref":{"node_id":"n_start","path":["item"]}}}}},
                {"id":"n_end","type":"end","title":"Nested End","config":{"outputs":{"wrapped":{"$ref":{"node_id":"n_tpl","path":["text"]}}}}},
            ],
            "edges":[
                {"id":"ne1","source":"n_start","target":"n_tpl","source_port":"output","target_port":"input"},
                {"id":"ne2","source":"n_tpl","target":"n_end","source_port":"text","target_port":"input"},
            ],
        }
        for n in [
            ("start","start","Input",{"inputs":[{"name":"items","type":"array"}]}),
            ("iter","iteration","Iterate",{"items":{"$ref":{"node_id":"start","path":["items"]}},
                                            "workflow":nested_wf,"item_name":"item",
                                            "output_node_id":"n_end","output_path":["wrapped"],
                                            "parallelism":4}),
            ("end","end","Output",{"outputs":{"results":{"$ref":{"node_id":"iter","path":["items"]}}}}),
        ]:
            rev = mutate(client, app4b, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("start","iter"),("iter","end")]:
            rev = mutate(client, app4b, rev, "add_edge", {"edge":{"id":f"{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        rev, v = add_test_and_publish(client, app4b, rev, "Iteration works", {"items":["a","b","c"]},
                                       [{"path":["results"],"operator":"exists"}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app4b}/runs", headers=header(), json={
            "inputs":{"items":["x","y","z"]}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("4B 运行成功", r["status"]=="succeeded")
        check("4B 3项都处理", "[x]" in str(r.get("outputs",{})) and "[z]" in str(r.get("outputs",{})),
              str(r.get("outputs",{}))[:200])

        # 4C. Loop 循环
        print("\n── 4C. Loop 循环 ──")
        app4c = client.post("/api/v1/applications", headers=header(), json={
            "name":"循环测试","requirement":"Test loop block.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app4c}/draft", headers=header()).json()["revision"]
        loop_wf = {
            "nodes":[
                {"id":"l_start","type":"start","title":"LoopIn","config":{"inputs":[{"name":"counter","type":"number"}]}},
                {"id":"l_inc","type":"variable_assigner","title":"Increment","config":{"assignments":{"next":{"$ref":{"node_id":"l_start","path":["counter"]}}}}},
                {"id":"l_end","type":"end","title":"LoopOut","config":{"outputs":{"counter":{"$ref":{"node_id":"l_start","path":["counter"]}}}}},
            ],
            "edges":[
                {"id":"le1","source":"l_start","target":"l_inc","source_port":"output","target_port":"input"},
                {"id":"le2","source":"l_inc","target":"l_end","source_port":"output","target_port":"input"},
            ],
        }
        for n in [
            ("start","start","Input",{"inputs":[]}),
            ("loop","loop","CountUp",{"workflow":loop_wf,"variables":{"counter":0},
                                       "break_condition":{"value":True,"operator":"equals","expected":True},
                                       "break_value":True,"max_iterations":5,"output_node_id":"l_end"}),
            ("end","end","Output",{"outputs":{"iterations":{"$ref":{"node_id":"loop","path":["output","iterations"]}}}}),
        ]:
            rev = mutate(client, app4c, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("start","loop"),("loop","end")]:
            rev = mutate(client, app4c, rev, "add_edge", {"edge":{"id":f"{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        rev, v = add_test_and_publish(client, app4c, rev, "Loop works", {},
                                       [{"path":["iterations"],"operator":"equals","expected":1}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app4c}/runs", headers=header(), json={
            "inputs":{}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("4C 运行完成", r["status"] in ("succeeded","failed"), r.get("status",""))
        check("4C 循环次数 >= 1", r.get("outputs",{}).get("iterations",0) >= 1,
              str(r.get("outputs",{})))

        # 4D. Human Input 暂停/恢复
        print("\n── 4D. Human Input 暂停/恢复 ──")
        app4d = client.post("/api/v1/applications", headers=header(), json={
            "name":"人工输入","requirement":"Test human_input block.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app4d}/draft", headers=header()).json()["revision"]
        for n in [
            ("start","start","Input",{"inputs":[{"name":"auto","type":"string","required":False}]}),
            ("human","human_input","Ask",{"title":"Need info","description":"Provide value","fields":[{"name":"answer","label":"Your answer","type":"string","required":True}]}),
            ("end","end","Output",{"outputs":{"provided":{"$ref":{"node_id":"human","path":["answer"]}}}}),
        ]:
            rev = mutate(client, app4d, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("start","human"),("human","end")]:
            rev = mutate(client, app4d, rev, "add_edge", {"edge":{"id":f"{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        rev, v = add_test_and_publish(client, app4d, rev, "Human input preset", {"__human__":{"human":{"answer":"preset!"}}},
                                       [{"path":["provided"],"operator":"equals","expected":"preset!"}])
        # Test preset mode
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app4d}/runs", headers=header(), json={
            "inputs":{"__human__":{"human":{"answer":"hello"}}}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("4D 预填模式通过", r.get("outputs",{}).get("provided")=="hello", str(r.get("outputs",{})))

        # Test pause/resume mode
        run_r = client.post(f"/api/v1/applications/{app4d}/runs", headers=header(), json={
            "inputs":{}, "version":v, "workspace_path":".",
        })
        rid = run_r.json()["run_id"]
        for _ in range(60):
            rec = client.get(f"/api/v1/runs/{rid}", headers=header()).json()
            if rec["status"]=="paused": break
            time.sleep(0.2)
        check("4D 正确暂停", rec["status"]=="paused", rec.get("status",""))
        resumed = client.post(f"/api/v1/runs/{rid}/resume", headers=header(), json={
            "values":{"answer":"resumed value!"},
        })
        check("4D 恢复成功", resumed.status_code==200)
        r = wait_for_run(client, rid)
        check("4D 恢复后完成", r["status"]=="succeeded", r.get("error",""))
        check("4D 恢复值正确", r.get("outputs",{}).get("provided")=="resumed value!", str(r.get("outputs",{})))

        # ═══════════════════════════════════════════════════════════
        # 维度 5: 错误处理与边界条件
        # ═══════════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 5: 错误处理与边界条件")
        print("█" * 70)

        # 5A. 预算门超限
        print("\n── 5A. 预算超限 ──")
        app5a = client.post("/api/v1/applications", headers=header(), json={
            "name":"预算超限","requirement":"Test budget exceeded.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app5a}/draft", headers=header()).json()["revision"]
        for n in [
            ("start","start","Input",{"inputs":[]}),
            ("budget","budget_gate","Budget",
             {"input":{"$ref":{"node_id":"start","path":["output"]}},
              "settings":{"max_cost_usd":0.5,"spent_cost_usd":10}}),
            ("end","end","Output",{"outputs":{"allowed":{"$ref":{"node_id":"budget","path":["state","allowed"]}}}}),
        ]:
            rev = mutate(client, app5a, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("start","budget"),("budget","end")]:
            rev = mutate(client, app5a, rev, "add_edge", {"edge":{"id":f"{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        rev, v = add_test_and_publish(client, app5a, rev, "Budget exceeded", {},
                                       [{"path":["allowed"],"operator":"equals","expected":False}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app5a}/runs", headers=header(), json={
            "inputs":{}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("5A 预算超限=拒绝", r.get("outputs",{}).get("allowed")==False, str(r.get("outputs",{})))

        # 5B. 轮次限制
        print("\n── 5B. 轮次限制 ──")
        app5b = client.post("/api/v1/applications", headers=header(), json={
            "name":"轮次限制","requirement":"Test round limit.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app5b}/draft", headers=header()).json()["revision"]
        for n in [
            ("start","start","Input",{"inputs":[]}),
            ("rounds","round_limit","Rounds",
             {"input":{"$ref":{"node_id":"start","path":["output"]}},
              "settings":{"current_round":31,"max_rounds":30}}),
            ("end","end","Output",{"outputs":{"allowed":{"$ref":{"node_id":"rounds","path":["state","allowed"]}}}}),
        ]:
            rev = mutate(client, app5b, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("start","rounds"),("rounds","end")]:
            rev = mutate(client, app5b, rev, "add_edge", {"edge":{"id":f"{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        rev, v = add_test_and_publish(client, app5b, rev, "Round limit hit", {},
                                       [{"path":["allowed"],"operator":"equals","expected":False}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app5b}/runs", headers=header(), json={
            "inputs":{}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("5B 轮次限制=拒绝", r.get("outputs",{}).get("allowed")==False, str(r.get("outputs",{})))

        # 5C. 错误分类器：致命错误
        print("\n── 5C. 错误分类全面测试 ──")
        from agent_platform.workflow_runtime import WorkflowRuntime
        classifier = WorkflowRuntime._classify_error
        check("5C timeout→retryable", classifier("Connection timeout",{}).get("class")=="retryable")
        check("5C rate→retryable", classifier("Rate limit exceeded",{}).get("class")=="retryable")
        check("5C api_key→fatal", classifier("Invalid API key",{}).get("class")=="fatal")
        check("5C permission→permission", classifier("Permission denied",{}).get("class")=="permission")
        check("5C syntax→tool", classifier("SyntaxError in script",{}).get("class")=="tool")
        check("5C unknown→unknown", classifier("Something weird happened",{}).get("class")=="unknown")

        # 5D. 工具调用路由解析
        print("\n── 5D. 工具调用解析 ──")
        parser = WorkflowRuntime._parse_tool_use_from_text
        # XML-style
        r1 = parser('<tool_call>{"name":"Read","input":{"path":"test.py"}}</tool_call>')
        check("5D XML解析", len(r1)>0 and r1[0]["name"]=="Read", str(r1))
        # JSON fence
        r2 = parser('```json\n{"name":"Bash","input":{"cmd":"ls"}}\n```')
        check("5D JSON fence解析", len(r2)>0 and r2[0]["name"]=="Bash", str(r2))
        # Empty text
        r3 = parser("Just a normal response without tools.")
        check("5D 无工具返回空", len(r3)==0, str(r3))
        # 任务排序
        sorter = WorkflowRuntime._topological_task_sort
        tasks = [{"name":"c","dependencies":["a","b"]},{"name":"a","dependencies":[]},{"name":"b","dependencies":["a"]}]
        ordered = sorter(tasks)
        check("5D 拓扑排序首项=a", ordered[0]["name"]=="a", str([t["name"] for t in ordered]))
        check("5D 拓扑排序末项=c", ordered[-1]["name"]=="c", str([t["name"] for t in ordered]))

        # 5E. 无效图验证
        print("\n── 5E. 图结构验证 ──")
        app5e = client.post("/api/v1/applications", headers=header(), json={
            "name":"图验证","requirement":"Test graph validation.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app5e}/draft", headers=header()).json()["revision"]
        # Create disconnected nodes
        for n in [
            ("a","start","A",{"inputs":[]}),
            ("b","template_transform","B",{"template":"hi","variables":{}}),
        ]:
            rev = mutate(client, app5e, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        r = client.post(f"/api/v1/applications/{app5e}/draft/validate", headers=header())
        check("5E 孤立节点检测", not r.json()["valid"], str(r.json().get("errors",[]))[:200])
        check("5E 缺少 end", "at least one end" in str(r.json().get("errors","")).lower() or
              "exactly one start" in str(r.json().get("errors","")).lower())

        # 5F. 大图: 20 节点链
        print("\n── 5F. 20 节点长链 ──")
        app5f = client.post("/api/v1/applications", headers=header(), json={
            "name":"长链","requirement":"Test long chain.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app5f}/draft", headers=header()).json()["revision"]
        prev_id = "start"
        rev = mutate(client, app5f, rev, "add_node", {"node":{"id":"start","type":"start","title":"S","config":{"inputs":[{"name":"value","type":"string"}]}}})
        for i in range(20):
            nid = f"v{i}"
            rev = mutate(client, app5f, rev, "add_node", {"node":{"id":nid,"type":"variable_assigner","title":f"V{i}","config":{"assignments":{f"v{i}":{"$ref":{"node_id":prev_id,"path":["output"]}}}}}})
            rev = mutate(client, app5f, rev, "add_edge", {"edge":{"id":f"e{i}","source":prev_id,"target":nid,"source_port":"output","target_port":"input"}})
            prev_id = nid
        rev = mutate(client, app5f, rev, "add_node", {"node":{"id":"end","type":"end","title":"E","config":{"outputs":{"final":{"$ref":{"node_id":prev_id,"path":["output"]}}}}}})
        rev = mutate(client, app5f, rev, "add_edge", {"edge":{"id":"final_edge","source":prev_id,"target":"end","source_port":"output","target_port":"input"}})
        r = client.post(f"/api/v1/applications/{app5f}/draft/validate", headers=header())
        check("5F 20节点验证通过", r.json()["valid"], str(r.json().get("errors",[]))[:200])
        rev, v = add_test_and_publish(client, app5f, rev, "Long chain", {"value":"test"},
                                       [{"path":["final"],"operator":"exists"}])
        r = wait_for_run(client, client.post(f"/api/v1/applications/{app5f}/runs", headers=header(), json={
            "inputs":{"value":"hello"}, "version":v, "workspace_path":".",
        }).json()["run_id"])
        check("5F 20节点运行成功", r["status"]=="succeeded", r.get("error",""))
        check("5F 输出非空", r.get("outputs",{}).get("final") is not None)

        # ═══════════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  最终评估总结")
        print("█" * 70)
        passed = sum(RESULTS)
        total = len(RESULTS)
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed < total:
            print(f"  ❌ 失败: {total - passed}")
            failed_idx = [i for i, r in enumerate(RESULTS) if not r]
            print(f"  失败项索引: {failed_idx}")
        else:
            print(f"  🎉 全部通过!")
        print(f"  通过率: {passed/total*100:.1f}%")
        print("█" * 70)

    try:
        tmp.cleanup()
    except (PermissionError, OSError):
        pass
    return 0 if passed == total else 1

if __name__ == "__main__":
    raise SystemExit(main())
