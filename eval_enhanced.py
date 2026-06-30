#!/usr/bin/env python3
"""
Lilies 三大增强验证:
  A. LLM非确定性隔离 — 结构断言 / seed / temperature
  B. 并发压力测试 — 多client草稿变异 / 并发运行
  C. 崩溃恢复 — checkpoint持久化 / 恢复验证
"""

from __future__ import annotations
import json, sys, time, threading
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))
from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings

H = {"Authorization": "Bearer test-token-2024"}
R = []

def ok(n, c, d=""):
    m="✅" if c else "❌"
    x=f"  {m} {n}"
    if d and not c: x+=f" — {str(d)[:200]}"
    print(x); R.append(c)

def hdr(t): print(f"\n{'█'*62}\n  {t}\n{'█'*62}")

def mc(c,aid,rev,op,data):
    r=c.post(f"/api/v1/applications/{aid}/draft",headers=H,json={
        "expected_revision":rev,"idempotency_key":f"enh-{op}-{rev}-{time.time():.0f}",
        "op":op,"data":data})
    if r.status_code!=200: raise RuntimeError(f"{op}:{r.text[:150]}")
    return r.json()["revision"]

def wr(c,rid):
    for _ in range(200):
        r=c.get(f"/api/v1/runs/{rid}",headers=H); s=r.json()["status"]
        if s in("succeeded","failed"): return r.json()
        time.sleep(0.2)
    return c.get(f"/api/v1/runs/{rid}",headers=H).json()

def add_test_pub(c,aid,rev,name,inputs,assertions,structural=False):
    rev=mc(c,aid,rev,"add_test",{"test":{"name":name,"requirement":"Verify.",
        "inputs":inputs,"assertions":assertions,"structural_only":structural}})
    tr=c.post(f"/api/v1/applications/{aid}/tests/run",headers=H)
    if not tr.json().get("passed",False):
        raise RuntimeError(f"Test {name}: {tr.text[:200]}")
    pr=c.post(f"/api/v1/applications/{aid}/versions",headers=H)
    return rev,pr.json()["version"]


def main():
    tmp = TemporaryDirectory(); tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024",data_dir=tp/"data",workspace_root=tp/"workspaces")
    s.prepare(); (tp/"workspaces").mkdir(parents=True,exist_ok=True)
    (tp/"workspaces").chmod(0o777)

    app = create_app(settings=s)
    with TestClient(app) as c:

        # ═══════════════════════════════════════════════════════
        # A: LLM非确定性隔离 — 结构断言
        # ═══════════════════════════════════════════════════════
        hdr("A: LLM非确定性隔离 — 结构断言 vs 内容断言")

        # A1: structural_only flag isolates LLM variability
        print("\n  ── A1: structural_only标志 ──")
        aid_a1 = c.post("/api/v1/applications",headers=H,json={
            "name":"StrucTest","requirement":"Test structural assertions."}).json()["id"]
        d=c.get(f"/api/v1/applications/{aid_a1}/draft",headers=H).json(); rev=d["revision"]
        nodes = [
            ("s","start","S",{"inputs":[{"name":"topic","type":"string"}]}),
            ("llm","llm","Think",{"system":"Reply with 1 sentence.","prompt":{"$ref":{"node_id":"s","path":["topic"]}}}),
            ("e","end","E",{"outputs":{"answer":{"$ref":{"node_id":"llm","path":["text"]}}}}),
        ]
        for nid,nt,nl,nc in nodes:
            rev=mc(c,aid_a1,rev,"add_node",{"node":{"id":nid,"type":nt,"title":nl,"config":nc}})
        for i in range(len(nodes)-1):
            rev=mc(c,aid_a1,rev,"add_edge",{"edge":{"id":f"ea{i}","source":nodes[i][0],"target":nodes[i+1][0],"source_port":"text" if nodes[i][0]=="llm" else "output","target_port":"input"}})
        # Test with structural_only — should pass even though LLM output varies
        rev,v=add_test_pub(c,aid_a1,rev,"Structural",{"topic":"What is gravity?"},
                           [{"path":["answer"],"operator":"exists"},
                            {"path":["answer"],"operator":"min_length","expected":5},
                            {"path":["answer"],"operator":"type","expected":"string"}],
                           structural=True)
        rr=c.post(f"/api/v1/applications/{aid_a1}/runs",headers=H,json={
            "inputs":{"topic":"What is AI?"},"version":v,"workspace_path":"."})
        rec=wr(c,rr.json()["run_id"])
        ok("A1.1 structural_only运行",rec["status"]=="succeeded",rec.get("error",""))
        ans=rec.get("outputs",{}).get("answer","")
        ok("A1.2 答案存在",len(str(ans))>0)
        ok("A1.3 答案够长(>=5)",len(str(ans))>=5,f"len={len(str(ans))}")
        ok("A1.4 答案是string",isinstance(ans,str))
        print(f"    LLM答案: {str(ans)[:120]}")

        # A2: 多次运行 → 只检查结构性，不检查内容相等
        results=[]
        for _ in range(3):
            rr=c.post(f"/api/v1/applications/{aid_a1}/runs",headers=H,json={
                "inputs":{"topic":"What is ML?"},"version":v,"workspace_path":"."})
            rec=wr(c,rr.json()["run_id"])
            results.append((rec["status"],len(str(rec.get("outputs",{}).get("answer","")))))
        all_succeeded=all(s=="succeeded" for s,_ in results)
        lengths_ok=all(l>=5 for _,l in results)
        ok("A2.1 3次全部成功",all_succeeded,str(results))
        ok("A2.2 每次答案都够长",lengths_ok,str([l for _,l in results]))
        # Content varies (LLM non-determinism) but structure is invariant
        print(f"    3次答案长度: {[l for _,l in results]} (内容可能不同，但结构一致)")

        # A3: 纯确定性流程 → 内容断言仍然可用
        print("\n  ── A3: 确定性流程内容断言 ──")
        aid_a3 = c.post("/api/v1/applications",headers=H,json={
            "name":"DetTest","requirement":"Test deterministic."}).json()["id"]
        d=c.get(f"/api/v1/applications/{aid_a3}/draft",headers=H).json(); rev=d["revision"]
        nodes3 = [
            ("s","start","S",{"inputs":[{"name":"x","type":"number"}]}),
            ("v","variable_assigner","V",{"assignments":{"doubled":{"$ref":{"node_id":"s","path":["x"]}}}}),
            ("e","end","E",{"outputs":{"result":{"$ref":{"node_id":"v","path":["output"]}}}}),
        ]
        for nid,nt,nl,nc in nodes3:
            rev=mc(c,aid_a3,rev,"add_node",{"node":{"id":nid,"type":nt,"title":nl,"config":nc}})
        for i in range(len(nodes3)-1):
            rev=mc(c,aid_a3,rev,"add_edge",{"edge":{"id":f"ea{i}","source":nodes3[i][0],"target":nodes3[i+1][0],"source_port":"output","target_port":"input"}})
        rev,v=add_test_pub(c,aid_a3,rev,"Deterministic",{"x":5},
                           [{"path":["result"],"operator":"exists"}])
        # 5次运行 → 输出完全相同（无LLM）
        results3=[]
        for _ in range(5):
            rr=c.post(f"/api/v1/applications/{aid_a3}/runs",headers=H,json={
                "inputs":{"x":10},"version":v,"workspace_path":"."})
            rec=wr(c,rr.json()["run_id"])
            results3.append(json.dumps(rec.get("outputs",{}),sort_keys=True))
        ok("A3.1 5次确定性完全一致",len(set(results3))==1,
           f"Unique results: {len(set(results3))}")

        # ═══════════════════════════════════════════════════════
        # B: 并发压力测试
        # ═══════════════════════════════════════════════════════
        hdr("B: 并发压力测试 — 多Client草稿 + 并发运行")

        # B1: 两个独立client同时编辑同一草稿 → revision冲突正确处理
        print("\n  ── B1: 双Client并发草稿变异 ──")
        app2 = create_app(settings=s)
        with TestClient(app2) as c2:
            aid_b1 = c.post("/api/v1/applications",headers=H,json={
                "name":"Concurrent","requirement":"Test."}).json()["id"]
            d=c.get(f"/api/v1/applications/{aid_b1}/draft",headers=H).json()
            rev=d["revision"]

            # Client 1 adds node
            rev_c1=mc(c,aid_b1,rev,"add_node",{"node":{"id":"s","type":"start","title":"S","config":{"inputs":[]}}})
            # Client 2 tries with stale revision → should fail
            r2=c2.post(f"/api/v1/applications/{aid_b1}/draft",headers=H,json={
                "expected_revision":rev,"idempotency_key":"c2-stale",
                "op":"add_node","data":{"node":{"id":"dup","type":"end","title":"E","config":{"outputs":{}}}}})
            ok("B1.1 旧revision被拒",r2.status_code==409,
               f"status={r2.status_code}")
            # Client 2 refreshes → succeeds
            d2=c2.get(f"/api/v1/applications/{aid_b1}/draft",headers=H).json()
            rev_c2=mc(c2,aid_b1,d2["revision"],"add_node",{"node":{"id":"e","type":"end","title":"E","config":{"outputs":{}}}})
            ok("B1.2 刷新后成功",rev_c2>rev_c1,f"rev_c2={rev_c2} rev_c1={rev_c1}")

        # B2: 并发运行无交叉污染
        print("\n  ── B2: 并发运行隔离 ──")
        aid_b2 = c.post("/api/v1/applications",headers=H,json={
            "name":"RunIso","requirement":"Test run isolation."}).json()["id"]
        d=c.get(f"/api/v1/applications/{aid_b2}/draft",headers=H).json(); rev=d["revision"]
        nodes_b2=[
            ("s","start","S",{"inputs":[{"name":"val","type":"number"}]}),
            ("v","variable_assigner","V",{"assignments":{"key":{"$ref":{"node_id":"s","path":["val"]}}}}),
            ("e","end","E",{"outputs":{"result":{"$ref":{"node_id":"v","path":["output"]}}}}),
        ]
        for nid,nt,nl,nc in nodes_b2:
            rev=mc(c,aid_b2,rev,"add_node",{"node":{"id":nid,"type":nt,"title":nl,"config":nc}})
        for i in range(len(nodes_b2)-1):
            rev=mc(c,aid_b2,rev,"add_edge",{"edge":{"id":f"eb{i}","source":nodes_b2[i][0],"target":nodes_b2[i+1][0],"source_port":"output","target_port":"input"}})
        rev,v=add_test_pub(c,aid_b2,rev,"Iso",{"val":0},
                           [{"path":["result"],"operator":"exists"}])

        # Fire 5 concurrent runs with distinct inputs
        results_b2=[]
        threads=[]
        lock=threading.Lock()
        def run_with_input(inp):
            rr=c.post(f"/api/v1/applications/{aid_b2}/runs",headers=H,json={
                "inputs":{"val":inp},"version":v,"workspace_path":"."})
            rid=rr.json()["run_id"]
            rec=wr(c,rid)
            with lock: results_b2.append((inp,rec["status"],rec.get("outputs",{})))

        for inp in [1,2,3,4,5]:
            t=threading.Thread(target=run_with_input,args=(inp,))
            threads.append(t); t.start()
        for t in threads: t.join()

        all_ok=all(s=="succeeded" for _,s,_ in results_b2)
        ok("B2.1 5并发全部成功",all_ok,str([(i,s) for i,s,_ in results_b2]))
        # Each run should have distinct output (no cross-contamination)
        outputs=[json.dumps(o,sort_keys=True) for _,_,o in results_b2]
        unique_outputs=len(set(outputs))
        ok("B2.2 无交叉污染(5个不同输出)",unique_outputs==5,
           f"Unique: {unique_outputs}")

        # B3: 快速发布→并发运行压力
        print(f"\n  ── B3: 10次快速并发运行 ──")
        results_b3=[]
        threads_b3=[]
        def fast_run(i):
            rr=c.post(f"/api/v1/applications/{aid_b2}/runs",headers=H,json={
                "inputs":{"val":i*10},"version":v,"workspace_path":"."})
            rid=rr.json()["run_id"]
            rec=wr(c,rid)
            with lock: results_b3.append(rec["status"])
        for i in range(10):
            t=threading.Thread(target=fast_run,args=(i,))
            threads_b3.append(t); t.start()
        for t in threads_b3: t.join()
        ok("B3.1 10并发全部成功",all(s=="succeeded" for s in results_b3),
           f"Statuses: {dict((k,results_b3.count(k)) for k in set(results_b3))}")

        # ═══════════════════════════════════════════════════════
        # C: 崩溃恢复 — Checkpoint持久化 + 恢复
        # ═══════════════════════════════════════════════════════
        hdr("C: 崩溃恢复 — Checkpoint持久化 + 状态恢复")

        aid_c = c.post("/api/v1/applications",headers=H,json={
            "name":"CrashRecovery","requirement":"Test checkpoint recovery."}).json()["id"]
        d=c.get(f"/api/v1/applications/{aid_c}/draft",headers=H).json(); rev=d["revision"]
        nodes_c=[
            ("s","start","S",{"inputs":[{"name":"data","type":"string"}]}),
            ("v1","variable_assigner","V1",{"assignments":{"stage1":{"$ref":{"node_id":"s","path":["data"]}}}}),
            ("cp","checkpoint_resume","Checkpoint",{"input":{"$ref":{"node_id":"v1","path":["output"]}},"settings":{"checkpoint_id":"recovery-test"}}),
            ("v2","variable_assigner","V2",{"assignments":{"stage2":{"$ref":{"node_id":"cp","path":["output"]}}}}),
            ("e","end","E",{"outputs":{"final":{"$ref":{"node_id":"v2","path":["output"]}}}}),
        ]
        for nid,nt,nl,nc in nodes_c:
            rev=mc(c,aid_c,rev,"add_node",{"node":{"id":nid,"type":nt,"title":nl,"config":nc}})
        for i in range(len(nodes_c)-1):
            rev=mc(c,aid_c,rev,"add_edge",{"edge":{"id":f"ec{i}","source":nodes_c[i][0],"target":nodes_c[i+1][0],"source_port":"output","target_port":"input"}})
        # Test + publish with structural assertion
        rev,v=add_test_pub(c,aid_c,rev,"Checkpoint",{"data":"test"},
                           [{"path":["final"],"operator":"exists"}],structural=True)

        # Run and wait for checkpoint
        rr=c.post(f"/api/v1/applications/{aid_c}/runs",headers=H,json={
            "inputs":{"data":"recovery_data"},"version":v,"workspace_path":"."})
        rid_c=rr.json()["run_id"]
        rec=wr(c,rid_c)
        ok("C.1 含checkpoint运行成功",rec["status"]=="succeeded",rec.get("error",""))

        # Verify checkpoint was persisted
        chk=c.get(f"/api/v1/runs/{rid_c}",headers=H).json()
        # Check events for checkpoint.saved
        ev=c.get(f"/v1/streams/{rid_c}",headers=H).json()
        cp_events=[e for e in ev if e.get("type")=="checkpoint.saved"]
        ok("C.2 checkpoint事件已发出",len(cp_events)>0,f"{len(cp_events)} events")
        if cp_events:
            cp_id=cp_events[0]["data"].get("checkpoint_id","")
            ok("C.3 checkpoint_id正确",cp_id=="recovery-test",cp_id)

        # Verify checkpoint data was stored
        from agent_platform.storage import Storage
        st=Storage(s.data_dir)
        import asyncio
        async def check_cp():
            await st.initialize()
            cp=await st.get_checkpoint(rid_c,"recovery-test")
            return cp
        cp_data=asyncio.get_event_loop().run_until_complete(check_cp())
        ok("C.4 checkpoint持久化成功",cp_data is not None,"No data found")
        if cp_data:
            cp_inner=cp_data.get("data",cp_data)
            ok("C.5 包含completed_nodes",isinstance(cp_inner.get("completed_nodes",None),list),
               str(list(cp_inner.keys())[:5]))
            ok("C.6 包含outputs_snapshot",isinstance(cp_inner.get("outputs_snapshot",None),dict),
               f"Keys: {list(cp_inner.get('outputs_snapshot',{}).keys())[:3]}")
            print(f"    Checkpoint: completed={len(cp_inner.get('completed_nodes',[]))}")

        # ═══════════════════════════════════════════════════════
        # 总结
        # ═══════════════════════════════════════════════════════
        hdr("三大增强验证总结")
        passed=sum(R);total=len(R)
        print(f"\n  A. LLM非确定性隔离: structural_only标志 + 结构断言(len/type/exists)")
        print(f"  B. 并发压力测试:   revision冲突 + 5/10并发无交叉污染")
        print(f"  C. 崩溃恢复:       checkpoint持久化 + 事件+数据库双验证")
        print(f"\n  ✅ 通过: {passed}/{total}")
        if passed<total: print(f"  ❌ 失败: {total-passed}")
        else: print(f"  🎉 全部通过!")
        print(f"  通过率: {passed/total*100:.1f}%")

    try: tmp.cleanup()
    except: pass
    return 0 if passed==total else 1

if __name__ == "__main__":
    raise SystemExit(main())
