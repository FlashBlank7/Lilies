#!/usr/bin/env python3
"""
Lilies 结构能力评估 — 不依赖 LLM API 的全部能力验证

维度 A: 积木组合链 (Context / Model-Loop / Governance / Multi-Agent)
维度 B: Workflow DAG 极端情况 (分支/迭代/循环/暂停恢复/大图)
维度 C: 错误处理与边界条件 (预算/轮次/错误分类/工具解析/图验证)
维度 D: 工具调用路由解析
维度 E: 图结构验证器
"""

from __future__ import annotations

import json, sys, time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_runtime import WorkflowRuntime

def header(): return {"Authorization": "Bearer test-token-2024"}
RESULTS = []

def check(name, ok, detail=""):
    mark = "✅" if ok else "❌"
    line = f"  {mark} {name}"
    if detail and not ok: line += f" — {str(detail)[:200]}"
    print(line); RESULTS.append(ok)

def mutate(client, app_id, rev, op, data):
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
        "expected_revision": rev, "idempotency_key": f"s-{op}-{rev}-{time.time()}",
        "op": op, "data": data,
    })
    if r.status_code != 200: raise RuntimeError(f"Mutation: {r.text[:200]}")
    return r.json()["revision"]

def add_test_publish(client, app_id, rev, name, inputs, assertions):
    rev = mutate(client, app_id, rev, "add_test", {"test": {
        "name": name, "requirement": f"Verify: {name}",
        "inputs": inputs, "assertions": assertions,
    }})
    r = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=header())
    assert r.json()["passed"], f"Test {name}: {r.text[:300]}"
    r = client.post(f"/api/v1/applications/{app_id}/versions", headers=header())
    return rev, r.json()["version"]

def wait_run(client, run_id):
    for _ in range(120):
        r = client.get(f"/api/v1/runs/{run_id}", headers=header())
        if r.json()["status"] in ("succeeded","failed"): return r.json()
        time.sleep(0.2)
    return client.get(f"/api/v1/runs/{run_id}", headers=header()).json()


def main():
    tmp = TemporaryDirectory()
    tmp_path = Path(tmp.name)
    settings = Settings(api_token="test-token-2024", data_dir=tmp_path/"data", workspace_root=tmp_path/"workspaces")
    settings.prepare()
    (tmp_path/"workspaces").mkdir(parents=True, exist_ok=True)
    (tmp_path/"workspaces").chmod(0o777)
    ws = tmp_path / "workspaces" / "eval"
    ws.mkdir(parents=True, exist_ok=True)
    for p in Path(ws).rglob("*"): p.chmod(0o777) if p.exists() else None

    app = create_app(settings=settings)
    with TestClient(app) as client:

        # ═══════════════════════════════════════════════════════
        # 维度 A: 积木组合链
        # ═══════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 A: 积木组合链 (4 条链 × 若干积木)")
        print("█" * 70)

        # A1. Context 链: assembler → injector → memory → compactor
        print("\n── A1. Context 链 (4 积木串联) ──")
        chains = {
            "context": [
                ("ctx","context_assembler","Assemble",{"fragments":["INSTRUCTION","CONTEXT_HINT"]}),
                ("inj","workspace_context_injector","Workspace",{"files":["README.md"],"scope":"current_workspace"}),
                ("mem","conversation_memory","Memory",{"facts":["fact_a","fact_b"]}),
                ("comp","context_compactor","Compact",{"max_chars":400,"preserved_facts":["fact_a"]}),
            ],
            "governance": [
                ("budget","budget_gate","Budget",{"max_cost_usd":100,"spent_cost_usd":10}),
                ("rounds","round_limit","Rounds",{"current_round":3,"max_rounds":30}),
                ("cp","checkpoint_resume","Checkpoint",{"checkpoint_id":"eval-ckpt"}),
                ("cancel","cancellation_point","Cancel",{"cancelled":False}),
                ("rec","event_recorder","Record",{"label":"gov-trace"}),
            ],
            "multi_agent": [
                ("dispatch","task_dispatcher","Dispatch",{"tasks":[
                    {"name":"scan","dependencies":[]},
                    {"name":"fix","dependencies":["scan"]},
                    {"name":"verify","dependencies":["fix"]},
                ]}),
                ("deps","dependency_gate","Deps",{"dependencies":["scan"],"completed":["scan"]}),
                ("err","retry_error_classifier","Classify",{"error":"Timeout after 30s"}),
            ],
            "permission": [
                ("perm_check","permission_gate","Gate",{"auto_approve":True,"reason":"Auto test"}),
                ("norm","tool_result_normalizer","Normalize",{}),
            ],
        }

        for chain_name, chain_nodes in chains.items():
            app_id = client.post("/api/v1/applications", headers=header(), json={
                "name":f"{chain_name}链","requirement":f"Test {chain_name} chain.",
            }).json()["id"]
            rev = client.get(f"/api/v1/applications/{app_id}/draft", headers=header()).json()["revision"]

            # Add start
            rev = mutate(client, app_id, rev, "add_node", {"node":{"id":"start","type":"start","title":"Input","config":{"inputs":[{"name":"value","type":"string","required":False}]}}})
            prev_id = "start"

            # Add chain nodes
            for nid, ntype, ntitle, nsettings in chain_nodes:
                cfg = {"input":{"$ref":{"node_id":prev_id,"path":["output"]}},
                       "settings":nsettings}
                rev = mutate(client, app_id, rev, "add_node", {"node":{"id":nid,"type":ntype,"title":ntitle,"config":cfg}})
                rev = mutate(client, app_id, rev, "add_edge", {"edge":{"id":f"e-{prev_id}-{nid}","source":prev_id,"target":nid,"source_port":"output","target_port":"input"}})
                prev_id = nid

            # Add end
            rev = mutate(client, app_id, rev, "add_node", {"node":{"id":"end","type":"end","title":"Output",
                "config":{"outputs":{"result":{"$ref":{"node_id":prev_id,"path":["output"]}}}}}})
            rev = mutate(client, app_id, rev, "add_edge", {"edge":{"id":"final","source":prev_id,"target":"end","source_port":"output","target_port":"input"}})

            r = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=header())
            v = r.json()
            # "no mandatory test" is expected before we add tests — count only structural errors
            structural_errors = [e for e in v.get("errors",[]) if "test" not in e.lower()]
            check(f"A1 {chain_name} 结构验证", len(structural_errors)==0, str(structural_errors)[:150])

            rev, vn = add_test_publish(client, app_id, rev, f"{chain_name} runs", {},
                                        [{"path":["result"],"operator":"exists"}])
            run_r = client.post(f"/api/v1/applications/{app_id}/runs", headers=header(), json={
                "inputs":{"value":"test"}, "version":vn, "workspace_path":".",
            })
            if run_r.status_code == 202:
                rec = wait_run(client, run_r.json()["run_id"])
                check(f"A1 {chain_name} 运行成功", rec["status"]=="succeeded", rec.get("error",""))
            else:
                check(f"A1 {chain_name} 创建运行", False, run_r.text[:150])

        # ═══════════════════════════════════════════════════════
        # 维度 B: Workflow DAG 极端情况
        # ═══════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 B: Workflow DAG 极端情况")
        print("█" * 70)

        # B1. 多分支 If/Else + Variable Aggregator
        print("\n── B1. 双分支 + 聚合 ──")
        app_b1 = client.post("/api/v1/applications", headers=header(), json={
            "name":"分支聚合","requirement":"Test branches.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_b1}/draft", headers=header()).json()["revision"]
        nodes_b1 = [
            ("s","start","Input",{"inputs":[{"name":"n","type":"number"}]}),
            ("check","if_else","Check",{"cases":[{"id":"big","conditions":[{"value":{"$ref":{"node_id":"s","path":["n"]}},"operator":"gt","expected":10}],"logical_operator":"and"}],"default_branch":"small"}),
            ("big_t","template_transform","Big",{"template":"BIG {{ v }}","variables":{"v":{"$ref":{"node_id":"s","path":["n"]}}}}),
            ("small_t","template_transform","Small",{"template":"small {{ v }}","variables":{"v":{"$ref":{"node_id":"s","path":["n"]}}}}),
            ("agg","variable_aggregator","Merge",{"variables":[
                {"$ref":{"node_id":"big_t","path":["text"],"optional":True}},
                {"$ref":{"node_id":"small_t","path":["text"],"optional":True}},
            ],"mode":"first_non_null"}),
            ("e","end","Output",{"outputs":{"result":{"$ref":{"node_id":"agg","path":["output"]}}}}),
        ]
        edges_b1 = [
            ("s","check","",""),
            ("check","big_t","branch","big"),
            ("check","small_t","branch","small"),
            ("big_t","agg","text",""),
            ("small_t","agg","text",""),
            ("agg","e","output",""),
        ]
        for n in nodes_b1: rev = mutate(client, app_b1, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt,sp,tp in edges_b1:
            br = tp if tp else None
            sp = sp if sp else "output"
            tp = "input" if not br else tp
            rev = mutate(client, app_b1, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,"source_port":sp,"target_port":"input","branch":br}})
        rev, vn = add_test_publish(client, app_b1, rev, "Big branch", {"n":15},
                                    [{"path":["result"],"operator":"contains","expected":"BIG"}])
        r = wait_run(client, client.post(f"/api/v1/applications/{app_b1}/runs", headers=header(), json={
            "inputs":{"n":25}, "version":vn, "workspace_path":".",
        }).json()["run_id"])
        check("B1 大数→BIG分支", "BIG" in str(r.get("outputs",{}).get("result","")))
        r2 = wait_run(client, client.post(f"/api/v1/applications/{app_b1}/runs", headers=header(), json={
            "inputs":{"n":3}, "version":vn, "workspace_path":".",
        }).json()["run_id"])
        check("B1 小数→small分支", "small" in str(r2.get("outputs",{}).get("result","")))

        # B2. Iteration 批量处理
        print("\n── B2. Iteration 迭代 ──")
        app_b2 = client.post("/api/v1/applications", headers=header(), json={
            "name":"迭代","requirement":"Test iteration.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_b2}/draft", headers=header()).json()["revision"]
        nested = {
            "nodes":[
                {"id":"ns","type":"start","title":"NS","config":{"inputs":[{"name":"item","type":"string"}]}},
                {"id":"nt","type":"template_transform","title":"NT","config":{"template":"[{{ item }}]","variables":{"item":{"$ref":{"node_id":"ns","path":["item"]}}}}},
                {"id":"ne","type":"end","title":"NE","config":{"outputs":{"w":{"$ref":{"node_id":"nt","path":["text"]}}}}},
            ],
            "edges":[
                {"id":"ne1","source":"ns","target":"nt","source_port":"output","target_port":"input"},
                {"id":"ne2","source":"nt","target":"ne","source_port":"text","target_port":"input"},
            ],
        }
        for n in [
            ("s","start","Input",{"inputs":[{"name":"items","type":"array"}]}),
            ("iter","iteration","Iter",{"items":{"$ref":{"node_id":"s","path":["items"]}},"workflow":nested,"item_name":"item","output_node_id":"ne","output_path":["w"],"parallelism":4}),
            ("e","end","Output",{"outputs":{"results":{"$ref":{"node_id":"iter","path":["items"]}}}}),
        ]:
            rev = mutate(client, app_b2, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("s","iter"),("iter","e")]:
            rev = mutate(client, app_b2, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,
                "source_port":"items" if src=="iter" else "output","target_port":"input"}})
        rev, vn = add_test_publish(client, app_b2, rev, "Iteration", {"items":["x","y","z"]},
                                    [{"path":["results"],"operator":"exists"}])
        r = wait_run(client, client.post(f"/api/v1/applications/{app_b2}/runs", headers=header(), json={
            "inputs":{"items":["a","b","c"]}, "version":vn, "workspace_path":".",
        }).json()["run_id"])
        check("B2 运行成功", r["status"]=="succeeded")
        check("B2 3项都处理", "[a]" in str(r.get("outputs",{})) and "[c]" in str(r.get("outputs",{})))

        # B3. Loop 循环
        print("\n── B3. Loop 循环 ──")
        app_b3 = client.post("/api/v1/applications", headers=header(), json={
            "name":"循环","requirement":"Test loop.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_b3}/draft", headers=header()).json()["revision"]
        loop_wf = {
            "nodes":[
                {"id":"ls","type":"start","title":"LS","config":{"inputs":[{"name":"i","type":"number"}]}},
                {"id":"lv","type":"variable_assigner","title":"LV","config":{"assignments":{"n":{"$ref":{"node_id":"ls","path":["i"]}}}}},
                {"id":"le","type":"end","title":"LE","config":{"outputs":{"done":True}}},
            ],
            "edges":[{"id":"le1","source":"ls","target":"lv","source_port":"output","target_port":"input"},
                     {"id":"le2","source":"lv","target":"le","source_port":"output","target_port":"input"}],
        }
        for n in [
            ("s","start","Input",{"inputs":[]}),
            ("loop","loop","Loop",{"workflow":loop_wf,"variables":{"i":0},
                                    "break_condition":{"value":True,"operator":"equals","expected":True},
                                    "break_value":True,"max_iterations":3,"output_node_id":"le"}),
            ("e","end","Output",{"outputs":{"iters":{"$ref":{"node_id":"loop","path":["iterations"]}}}}),
        ]:
            rev = mutate(client, app_b3, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("s","loop"),("loop","e")]:
            rev = mutate(client, app_b3, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        # Loop test: try to add test, but this is a complex block; accept if test publish fails
        try:
            rev, vn = add_test_publish(client, app_b3, rev, "Loop", {},
                                        [{"path":["iters"],"operator":"exists"}])
            r = wait_run(client, client.post(f"/api/v1/applications/{app_b3}/runs", headers=header(), json={
                "inputs":{}, "version":vn, "workspace_path":".",
            }).json()["run_id"])
            check("B3 循环运行完成", r["status"] in ("succeeded","failed"))
            check("B3 循环≥1次", r.get("outputs",{}).get("iters",0) >= 1)
        except (RuntimeError, AssertionError) as e:
            check("B3 循环测试", False, str(e)[:150])

        # B4. Human Input + Pause/Resume
        print("\n── B4. Human Input 暂停/恢复 ──")
        app_b4 = client.post("/api/v1/applications", headers=header(), json={
            "name":"人工","requirement":"Test human input.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_b4}/draft", headers=header()).json()["revision"]
        for n in [
            ("s","start","Input",{"inputs":[]}),
            ("h","human_input","Ask",{"title":"Need info","description":"Provide value","fields":[{"name":"answer","label":"Your answer","type":"string","required":True}]}),
            ("e","end","Output",{"outputs":{"provided":{"$ref":{"node_id":"h","path":["answer"]}}}}),
        ]:
            rev = mutate(client, app_b4, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("s","h"),("h","e")]:
            rev = mutate(client, app_b4, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
        # Preset mode
        rev, vn = add_test_publish(client, app_b4, rev, "Preset", {"__human__":{"h":{"answer":"preset_val"}}},
                                    [{"path":["provided"],"operator":"equals","expected":"preset_val"}])
        r = wait_run(client, client.post(f"/api/v1/applications/{app_b4}/runs", headers=header(), json={
            "inputs":{"__human__":{"h":{"answer":"hello_world"}}}, "version":vn, "workspace_path":".",
        }).json()["run_id"])
        check("B4 预填模式", r.get("outputs",{}).get("provided")=="hello_world")
        # Pause/resume mode
        rid = client.post(f"/api/v1/applications/{app_b4}/runs", headers=header(), json={
            "inputs":{}, "version":vn, "workspace_path":".",
        }).json()["run_id"]
        for _ in range(60):
            rec = client.get(f"/api/v1/runs/{rid}", headers=header()).json()
            if rec["status"]=="paused": break
            time.sleep(0.2)
        check("B4 正确暂停", rec["status"]=="paused")
        resumed = client.post(f"/api/v1/runs/{rid}/resume", headers=header(), json={"values":{"answer":"resumed!"}})
        check("B4 恢复请求OK", resumed.status_code==200)
        r = wait_run(client, rid)
        check("B4 恢复后完成", r.get("outputs",{}).get("provided")=="resumed!")

        # B5. 20 节点长链
        print("\n── B5. 20 节点长链 ──")
        app_b5 = client.post("/api/v1/applications", headers=header(), json={
            "name":"长链","requirement":"Test 20 nodes.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_b5}/draft", headers=header()).json()["revision"]
        rev = mutate(client, app_b5, rev, "add_node", {"node":{"id":"s","type":"start","title":"S","config":{"inputs":[{"name":"v","type":"string"}]}}})
        prev = "s"
        for i in range(20):
            nid = f"a{i}"
            rev = mutate(client, app_b5, rev, "add_node", {"node":{"id":nid,"type":"variable_assigner","title":f"A{i}","config":{"assignments":{f"k{i}":{"$ref":{"node_id":prev,"path":["output"]}}}}}})
            rev = mutate(client, app_b5, rev, "add_edge", {"edge":{"id":f"e{i}","source":prev,"target":nid,"source_port":"output","target_port":"input"}})
            prev = nid
        rev = mutate(client, app_b5, rev, "add_node", {"node":{"id":"e","type":"end","title":"E","config":{"outputs":{"final":{"$ref":{"node_id":prev,"path":["output"]}}}}}})
        rev = mutate(client, app_b5, rev, "add_edge", {"edge":{"id":"fe","source":prev,"target":"e","source_port":"output","target_port":"input"}})
        r = client.post(f"/api/v1/applications/{app_b5}/draft/validate", headers=header())
        structural = [e for e in r.json().get("errors",[]) if "test" not in e.lower()]
        check("B5 20节点结构验证", len(structural)==0, str(structural)[:200])
        rev, vn = add_test_publish(client, app_b5, rev, "Long chain", {"v":"test"},
                                    [{"path":["final"],"operator":"exists"}])
        r = wait_run(client, client.post(f"/api/v1/applications/{app_b5}/runs", headers=header(), json={
            "inputs":{"v":"hello"}, "version":vn, "workspace_path":".",
        }).json()["run_id"])
        check("B5 20节点运行成功", r["status"]=="succeeded")
        check("B5 输出非空", r.get("outputs",{}).get("final") is not None)

        # B6. 嵌套工作流
        print("\n── B6. 嵌套子工作流 ──")
        app_b6 = client.post("/api/v1/applications", headers=header(), json={
            "name":"嵌套","requirement":"Test nested workflow.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_b6}/draft", headers=header()).json()["revision"]
        inner = {
            "nodes":[
                {"id":"is","type":"start","title":"IS","config":{"inputs":[{"name":"x","type":"number"}]}},
                {"id":"iv","type":"variable_assigner","title":"IV","config":{"assignments":{"doubled":{"$ref":{"node_id":"is","path":["x"]}}}}},
                {"id":"ie","type":"end","title":"IE","config":{"outputs":{"result":{"$ref":{"node_id":"iv","path":["output"]}}}}},
            ],
            "edges":[{"id":"ie1","source":"is","target":"iv","source_port":"output","target_port":"input"},
                     {"id":"ie2","source":"iv","target":"ie","source_port":"output","target_port":"input"}],
        }
        for n in [
            ("s","start","Input",{"inputs":[{"name":"outer","type":"number"}]}),
            ("iter","iteration","Nested",{"items":[1],"workflow":inner,"item_name":"x","output_node_id":"ie","output_path":["result"],"parallelism":1}),
            ("e","end","Output",{"outputs":{"nested_out":{"$ref":{"node_id":"iter","path":["items"]}}}}),
        ]:
            rev = mutate(client, app_b6, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
        for src,tgt in [("s","iter"),("iter","e")]:
            rev = mutate(client, app_b6, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,
                "source_port":"items" if src=="iter" else "output","target_port":"input"}})
        rev, vn = add_test_publish(client, app_b6, rev, "Nested", {"outer":5},
                                    [{"path":["nested_out"],"operator":"exists"}])
        r = wait_run(client, client.post(f"/api/v1/applications/{app_b6}/runs", headers=header(), json={
            "inputs":{"outer":10}, "version":vn, "workspace_path":".",
        }).json()["run_id"])
        check("B6 嵌套运行成功", r["status"]=="succeeded")

        # ═══════════════════════════════════════════════════════
        # 维度 C: 错误处理与边界条件
        # ═══════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  维度 C: 错误处理与边界条件")
        print("█" * 70)

        # C1. 预算超限
        for label, max_cost, spent, expected in [
            ("C1a 预算充足", 100, 10, True),
            ("C1b 预算刚好", 10, 10, True),
            ("C1c 预算超限", 0.5, 10, False),
        ]:
            app_c1 = client.post("/api/v1/applications", headers=header(), json={
                "name":label,"requirement":"Test.",
            }).json()["id"]
            rev = client.get(f"/api/v1/applications/{app_c1}/draft", headers=header()).json()["revision"]
            for n in [
                ("s","start","S",{"inputs":[]}),
                ("bg","budget_gate","BG",{"input":{"$ref":{"node_id":"s","path":["output"]}},"settings":{"max_cost_usd":max_cost,"spent_cost_usd":spent}}),
                ("e","end","E",{"outputs":{"allowed":{"$ref":{"node_id":"bg","path":["state","allowed"]}}}}),
            ]:
                rev = mutate(client, app_c1, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
            for src,tgt in [("s","bg"),("bg","e")]:
                rev = mutate(client, app_c1, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
            rev, vn = add_test_publish(client, app_c1, rev, label, {},
                                        [{"path":["allowed"],"operator":"equals","expected":expected}])
            r = wait_run(client, client.post(f"/api/v1/applications/{app_c1}/runs", headers=header(), json={
                "inputs":{}, "version":vn, "workspace_path":".",
            }).json()["run_id"])
            check(f"{label}", r.get("outputs",{}).get("allowed")==expected,
                  f"Expected {expected}, got {r.get('outputs',{})}")

        # C2. 轮次限制
        for label, current, max_r, expected in [
            ("C2a 轮次未到", 5, 30, True),
            ("C2b 轮次刚好", 30, 30, False),
            ("C2c 轮次超限", 31, 30, False),
        ]:
            app_c2 = client.post("/api/v1/applications", headers=header(), json={
                "name":label,"requirement":"Test.",
            }).json()["id"]
            rev = client.get(f"/api/v1/applications/{app_c2}/draft", headers=header()).json()["revision"]
            for n in [
                ("s","start","S",{"inputs":[]}),
                ("rl","round_limit","RL",{"input":{"$ref":{"node_id":"s","path":["output"]}},"settings":{"current_round":current,"max_rounds":max_r}}),
                ("e","end","E",{"outputs":{"allowed":{"$ref":{"node_id":"rl","path":["state","allowed"]}}}}),
            ]:
                rev = mutate(client, app_c2, rev, "add_node", {"node":{"id":n[0],"type":n[1],"title":n[2],"config":n[3]}})
            for src,tgt in [("s","rl"),("rl","e")]:
                rev = mutate(client, app_c2, rev, "add_edge", {"edge":{"id":f"e-{src}-{tgt}","source":src,"target":tgt,"source_port":"output","target_port":"input"}})
            rev, vn = add_test_publish(client, app_c2, rev, label, {},
                                        [{"path":["allowed"],"operator":"equals","expected":expected}])
            r = wait_run(client, client.post(f"/api/v1/applications/{app_c2}/runs", headers=header(), json={
                "inputs":{}, "version":vn, "workspace_path":".",
            }).json()["run_id"])
            check(f"{label}", r.get("outputs",{}).get("allowed")==expected,
                  f"Expected {expected}, got {r.get('outputs',{})}")

        # C3. 错误分类器
        print("\n── C3. 错误分类器 ──")
        test_cases = [
            ("timeout", "Connection timeout after 30s", "retryable"),
            ("rate_limit", "Rate limit exceeded, please wait", "retryable"),
            ("network", "Temporary network failure", "retryable"),
            ("api_key", "Invalid API key provided", "fatal"),
            ("quota", "Quota exceeded for this billing period", "fatal"),
            ("permission", "Permission denied: cannot access file", "permission"),
            ("unauthorized", "401 Unauthorized", "permission"),
            ("syntax", "SyntaxError: invalid syntax at line 42", "tool"),
            ("name_error", "NameError: name 'foo' is not defined", "tool"),
            ("unknown", "Something weird just happened", "unknown"),
        ]
        for label, error, expected_class in test_cases:
            result = WorkflowRuntime._classify_error(error, {})
            check(f"C3 {label}→{expected_class}",
                  result["class"]==expected_class,
                  f"Got {result['class']} for '{error[:50]}'")

        # C4. 工具调用解析
        print("\n── C4. 工具调用解析 ──")
        test_parses = [
            ("xml", '<tool_call>{"name":"Read","input":{"path":"f.py"}}</tool_call>', "Read"),
            ("json_fence", '```json\n{"name":"Bash","input":{"cmd":"ls"}}\n```', "Bash"),
            ("function_call", '<function_call>{"name":"Write","input":{"path":"o.txt"}}</function_call>', "Write"),
            ("no_tool", "This is just a normal text response without any tools.", None),
            ("empty", "", None),
        ]
        for label, text, expected_tool in test_parses:
            result = WorkflowRuntime._parse_tool_use_from_text(text)
            if expected_tool is None:
                check(f"C4 {label}→空", len(result)==0, str(result)[:100])
            else:
                check(f"C4 {label}→{expected_tool}",
                      len(result)>0 and result[0]["name"]==expected_tool,
                      str(result)[:100])

        # C5. 拓扑任务排序
        print("\n── C5. 拓扑排序 ──")
        sorter = WorkflowRuntime._topological_task_sort
        # Simple chain
        tasks1 = [{"name":"c","dependencies":["b"]},{"name":"b","dependencies":["a"]},{"name":"a","dependencies":[]}]
        ordered1 = sorter(tasks1)
        check("C5 简单链 a→b→c", [t["name"] for t in ordered1]==["a","b","c"],
              str([t["name"] for t in ordered1]))
        # Diamond dependency
        tasks2 = [{"name":"d","dependencies":["b","c"]},{"name":"c","dependencies":["a"]},
                   {"name":"b","dependencies":["a"]},{"name":"a","dependencies":[]}]
        ordered2 = sorter(tasks2)
        check("C5 菱形依赖 a先d后", ordered2[0]["name"]=="a" and ordered2[-1]["name"]=="d",
              str([t["name"] for t in ordered2]))
        # Cycle detection (should not crash)
        tasks3 = [{"name":"x","dependencies":["y"]},{"name":"y","dependencies":["x"]}]
        ordered3 = sorter(tasks3)
        check("C5 循环依赖不崩溃", len(ordered3)==2,
              f"Got {len(ordered3)} tasks")

        # C6. 图结构验证
        print("\n── C6. 图结构验证 ──")
        app_c6 = client.post("/api/v1/applications", headers=header(), json={
            "name":"图验证","requirement":"Test.",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_c6}/draft", headers=header()).json()["revision"]
        rev = mutate(client, app_c6, rev, "add_node", {"node":{"id":"a","type":"start","title":"A","config":{"inputs":[]}}})
        rev = mutate(client, app_c6, rev, "add_node", {"node":{"id":"b","type":"template_transform","title":"B","config":{"template":"hi","variables":{}}}})
        r = client.post(f"/api/v1/applications/{app_c6}/draft/validate", headers=header())
        v = r.json()
        check("C6 缺 end 检测", not v["valid"], str(v.get("errors",[]))[:150])
        check("C6 缺 end 消息", any("end" in e.lower() or "answer" in e.lower() for e in v.get("errors",[])))

        # Duplicate edge detection
        rev = mutate(client, app_c6, rev, "add_node", {"node":{"id":"c","type":"end","title":"C","config":{"outputs":{}}}})
        rev = mutate(client, app_c6, rev, "add_edge", {"edge":{"id":"e1","source":"a","target":"b","source_port":"output","target_port":"input"}})
        rev = mutate(client, app_c6, rev, "add_edge", {"edge":{"id":"e2","source":"b","target":"c","source_port":"text","target_port":"input"}})
        r = client.post(f"/api/v1/applications/{app_c6}/draft/validate", headers=header())
        structural = [e for e in r.json().get("errors",[]) if "test" not in e.lower()]
        check("C6 完整图结构通过", len(structural)==0, str(structural)[:150])

        # ═══════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════
        print("\n" + "█" * 70)
        print("  结构能力评估总结")
        print("█" * 70)
        passed = sum(RESULTS)
        total = len(RESULTS)
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed < total:
            failed_count = total - passed
            print(f"  ❌ 失败: {failed_count}")
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
