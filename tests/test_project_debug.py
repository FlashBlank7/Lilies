"""Ordinary project users exercise simulation through the real task/HTTP engine."""

from tests.test_users import platform as platform, signup, project  # noqa: F401
from tests.test_projects import graph, node, edge, ref, settled
from tests.test_project_assistant_scope import call


def setup(client):
    _, owner = signup(client, "debug-owner")
    client.headers.update(owner)
    pid = project(client, owner)
    return pid, "/api/v1/projects/" + pid


def service(client, base, **updates):
    response = client.post(
        base + "/simulators",
        json={
            "name": "库存模拟",
            "idempotency_header": "Idempotency-Key",
            "initial_state": {"records": [{"id": 1, "value": 5}, {"id": 2, "value": 8}]},
            **updates,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def read_graph(client, wid):
    graph(
        client,
        wid,
        [
            node("s", "start"),
            node("h", "http_request", service_ref="inventory", path="/records"),
            node("e", "end", outputs={"data": ref("h", "output")}),
        ],
        [edge("s", "h"), edge("h", "e")],
    )


def run(client, pid, base, sid, key="trial", **updates):
    response = client.post(
        base + "/debug/runs",
        json={
            "workflow_id": pid,
            "request_key": key,
            "bindings": {"inventory": {"service_id": sid}},
            **updates,
        },
    )
    assert response.status_code == 202, response.text
    settled(client, base, response.json())
    return client.get(base + "/debug/runs/" + response.json()["id"]).json()


def test_presets_draft_debug_and_no_formal_call_grant(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    read_graph(client, pid)
    assert (
        client.put(
            base + "/space/workflows/" + pid + "/assistant-callable", json={"enabled": False}
        ).status_code
        == 200
    )
    assert call(client, pid, "project_calls", action="start", workflow_id=pid).status_code == 422
    result = call(
        client,
        pid,
        "project_debug",
        action="run",
        run={
            "workflow_id": pid,
            "request_key": "agent-test",
            "bindings": {"inventory": {"service_id": simulator["id"]}},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "succeeded", result.text
    assert result.json()["outputs"]["data"]["total"] == 2
    assert len(result.json()["services"]["inventory"]["requests"]) == 1
    assert call(client, pid, "project_calls", action="start", workflow_id=pid).status_code == 422
    assert run(client, pid, base, simulator["id"], key="agent-test")["id"] == result.json()["id"]


def test_missing_binding_cross_project_and_unsupported_side_effect(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    read_graph(client, pid)
    response = client.post(
        base + "/debug/runs", json={"workflow_id": pid, "request_key": "missing"}
    )
    assert response.status_code == 422 and "缺少模拟" in response.text
    other = client.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    read_graph(client, other)
    response = client.post(
        "/api/v1/projects/" + other + "/debug/runs",
        json={
            "workflow_id": other,
            "request_key": "foreign",
            "bindings": {"inventory": {"service_id": simulator["id"]}},
        },
    )
    assert response.status_code == 422
    graph(
        client,
        pid,
        [
            node("s", "start"),
            node(
                "write",
                "project_record",
                action="put",
                collection="live",
                key="a",
                value={"oops": True},
                expected_revision=0,
            ),
            node("e", "end"),
        ],
        [edge("s", "write"), edge("write", "e")],
    )
    response = client.post(base + "/debug/runs", json={"workflow_id": pid, "request_key": "unsafe"})
    assert response.status_code == 422 and "隔离模拟" in response.text
    assert client.get(base + "/records").json() == []


def partial_graph(client, pid, *, repaired=False):
    nodes = [
        node("s", "start"),
        node(
            "a",
            "http_request",
            service_ref="inventory",
            path="/update",
            method="POST",
            body={"id": 1, "changes": {"done": True}},
        ),
        node(
            "b",
            "http_request",
            service_ref="inventory",
            path="/delete",
            method="POST",
            body={"id": 2},
        ),
        node("read", "http_request", service_ref="inventory", path="/records"),
        node(
            "e",
            "end",
            outputs={"data": ref("read", "output"), "repaired": repaired},
            checks=[
                {"name": "已移除第二条", "value": ref("read", "output", "total"), "expected": 1}
            ],
        ),
    ]
    graph(client, pid, nodes, [edge(a["id"], b["id"]) for a, b in zip(nodes, nodes[1:])])


def test_resume_retains_state_and_reset_is_independent(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(
        client,
        base,
        initial_state={
            "records": [{"id": 1}, {"id": 2}],
            "failures": [{"path": "/delete", "remaining": 3, "status": 503}],
        },
    )
    partial_graph(client, pid)
    failed = run(client, pid, base, simulator["id"])
    assert failed["status"] == "failed", failed
    assert len(failed["services"]["inventory"]["state"]["effects"]) == 1
    response = client.post(base + "/debug/runs/" + failed["id"] + "/resume")
    assert response.status_code == 202, response.text
    settled(client, base, response.json())
    done = client.get(base + "/debug/runs/" + failed["id"]).json()
    assert done["status"] == "succeeded", done
    assert len(done["services"]["inventory"]["state"]["effects"]) == 2
    assert (
        sum(r["request"]["path"] == "/update" for r in done["services"]["inventory"]["requests"])
        == 1
    )
    reset = run(client, pid, base, simulator["id"], key="new")
    assert reset["status"] == "failed"
    assert reset["simulation"]["session_id"] != done["simulation"]["session_id"]
    assert client.get(base + "/debug/runs/" + done["id"]).json()["status"] == "succeeded"


def test_response_lost_and_repair_preserve_operations(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(
        client,
        base,
        initial_state={
            "records": [{"id": 1}, {"id": 2}],
            "failures": [
                {"path": "/update", "remaining": 1, "after_write": True},
                {"path": "/delete", "remaining": 3},
            ],
        },
    )
    partial_graph(client, pid)
    failed = run(client, pid, base, simulator["id"])
    assert failed["status"] == "failed"
    partial_graph(client, pid, repaired=True)
    repaired = run(client, pid, base, simulator["id"], key="repair", recovery_task_id=failed["id"])
    assert repaired["status"] == "succeeded", repaired
    assert len(repaired["services"]["inventory"]["state"]["effects"]) == 2
    assert repaired["simulation"]["session_id"] == failed["simulation"]["session_id"]
    # Repair shares service state, but the old task keeps its own terminal view.
    original = client.get(base + "/debug/runs/" + failed["id"]).json()
    assert len(original["services"]["inventory"]["state"]["effects"]) == 1


def test_samples_freeze_actual_output_chain_and_stale_results(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    first = client.post(base + "/members", json={"name": "盘点"}).json()["id"]
    second = client.post(base + "/members", json={"name": "下游"}).json()["id"]
    read_graph(client, first)
    graph(
        client,
        second,
        [
            node("s", "start", inputs=[{"name": "rows", "type": "object", "required": True}]),
            node("e", "end", outputs={"count": ref("$inputs", "rows", "total")}),
        ],
        [edge("s", "e")],
    )
    initial = run(client, pid, base, simulator["id"], workflow_id=first)
    assert initial["status"] == "succeeded", initial
    rev = client.get(base + "/members/" + second + "/debug-cases").json()["revision"]
    saved = client.put(
        base + "/members/" + second + "/debug-cases",
        json={
            "name": "实际上游输出",
            "expected_revision": rev,
            "source": {
                "kind": "run",
                "task_id": initial["id"],
                "path": ["data"],
                "input_name": "rows",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    case = saved.json()["case"]
    assert case["inputs"]["rows"]["total"] == 2
    alone = run(
        client, pid, base, simulator["id"], key="downstream", workflow_id=second, case_id=case["id"]
    )
    assert alone["status"] == "succeeded" and alone["outputs"]["count"] == 2
    assert len(alone["runs"]) == 1
    revision = client.get("/api/v1/applications/" + pid + "/draft").json()["revision"]
    response = client.put(
        base + "/debug/chain",
        json={
            "expected_revision": revision,
            "steps": [
                {"workflow_id": first},
                {"workflow_id": second, "inputs": {"rows": ref("step_1", "output", "data")}},
            ],
        },
    )
    assert response.status_code == 200, response.text
    chain = run(client, pid, base, simulator["id"], key="chain")
    assert chain["status"] == "succeeded", chain
    assert chain["outputs"]["result"]["count"] == 2 and len(chain["runs"]) == 3
    # Change the service revision, even when its interface remains compatible.
    patch = {k: v for k, v in simulator.items() if k not in {"id", "revision"}}
    patch["expected_revision"] = simulator["revision"]
    patch["name"] = "新的服务版本"
    assert client.put(base + "/simulators/" + simulator["id"], json=patch).status_code == 200
    historic = client.get(base + "/debug/runs/" + chain["id"]).json()
    assert historic["outdated"] is True
    assert historic["service_versions"]["inventory"]["revision"] == 1


def test_saved_case_suite_and_duplicate_payload_conflict(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    read_graph(client, pid)
    revision = client.get(base + "/members/" + pid + "/debug-cases").json()["revision"]
    saved = client.put(
        base + "/members/" + pid + "/debug-cases",
        json={
            "name": "分页样例",
            "expected_revision": revision,
            "bindings": {"inventory": {"service_id": simulator["id"]}},
        },
    )
    assert saved.status_code == 200, saved.text
    cid = saved.json()["case"]["id"]
    response = client.post(base + "/members/" + pid + "/tests/run", json={"case_ids": [cid]})
    assert response.status_code == 200, response.text
    assert response.json()["passed"] and response.json()["environment"] == "simulation"
    assert len(response.json()["results"]) == 1
    run(client, pid, base, simulator["id"], key="same")
    changed = client.post(
        base + "/debug/runs",
        json={
            "workflow_id": pid,
            "request_key": "same",
            "inputs": {"changed": True},
            "bindings": {"inventory": {"service_id": simulator["id"]}},
        },
    )
    assert changed.status_code == 409


def test_legacy_connection_never_falls_back_to_real_network(platform):
    from tests.fixtures.media_http_backend import Backend

    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    backend = Backend()
    backend.add_case("real")
    try:
        connection = client.post(
            base + "/http-connections",
            json={
                "name": "真实目标",
                "base_url": backend.base + "/real",
                "allowed_paths": ["/**"],
                "allowed_methods": ["GET"],
                "credential": "never-visible",
                "auth_header": "X-Test-Key",
            },
        ).json()
        cid = connection["id"]
        graph(
            client,
            pid,
            [
                node("s", "start"),
                node("h", "http_request", connection_id=cid, path="/records"),
                node("e", "end", outputs={"data": ref("h", "output")}),
            ],
            [edge("s", "h"), edge("h", "e")],
        )
        missing = client.post(
            base + "/debug/runs", json={"workflow_id": pid, "request_key": "missing"}
        )
        assert missing.status_code == 422
        result = run(
            client, pid, base, simulator["id"], bindings={cid: {"service_id": simulator["id"]}}
        )
        assert result["status"] == "succeeded", result
        assert backend.audit == []
        assert "never-visible" not in str(result)
    finally:
        backend.close()


def test_simulator_input_errors_are_reviewable(platform):
    client, _ = platform
    pid, base = setup(client)
    bad = client.post(
        base + "/simulators",
        json={"name": "bad", "implementation": "python", "code": "def handle(:"},
    )
    assert bad.status_code == 422 and "语法错误" in bad.text
    wrong = client.post(
        base + "/simulators", json={"name": "bad", "idempotency_header": "Unsupported-Key"}
    )
    assert wrong.status_code == 422 and "Idempotency-Key" in wrong.text
    simulator = service(client, base)
    other = client.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    foreign = client.put(
        "/api/v1/projects/" + other + "/simulators/" + simulator["id"], json={"name": "changed"}
    )
    assert foreign.status_code == 404


def test_concurrent_debug_sessions_and_request_deduplication(platform):
    from concurrent.futures import ThreadPoolExecutor

    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    graph(
        client,
        pid,
        [
            node("s", "start"),
            node(
                "h",
                "http_request",
                service_ref="inventory",
                path="/delete",
                method="POST",
                body={"id": 2},
            ),
            node("e", "end"),
        ],
        [edge("s", "h"), edge("h", "e")],
    )

    def launch(key):
        response = client.post(
            base + "/debug/runs",
            json={
                "workflow_id": pid,
                "request_key": key,
                "bindings": {"inventory": {"service_id": simulator["id"]}},
            },
        )
        assert response.status_code == 202, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = list(pool.map(launch, ["same", "same", "independent"]))
    assert tasks[0]["id"] == tasks[1]["id"] and tasks[0]["id"] != tasks[2]["id"]
    for task in [tasks[0], tasks[2]]:
        settled(client, base, task)
        result = client.get(base + "/debug/runs/" + task["id"]).json()
        assert result["status"] == "succeeded", result
        assert len(result["services"]["inventory"]["state"]["effects"]) == 1
        assert result["services"]["inventory"]["state"]["records"] == [{"id": 1, "value": 5}]


def test_repair_and_resume_cannot_race_the_same_service_state(platform, monkeypatch):
    import asyncio
    import threading

    client, app = platform
    pid, base = setup(client)
    simulator = service(
        client,
        base,
        initial_state={
            "records": [{"id": 1}, {"id": 2}],
            "failures": [{"path": "/delete", "remaining": 3}],
        },
    )
    partial_graph(client, pid)
    failed = run(client, pid, base, simulator["id"])
    assert failed["status"] == "failed"
    debug = app.state.services.projects.debug
    original = debug.dispatch
    gate = threading.Event()

    async def delayed(*args, **kwargs):
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return await original(*args, **kwargs)

    monkeypatch.setattr(debug, "dispatch", delayed)
    body = {
        "workflow_id": pid,
        "request_key": "repair",
        "recovery_task_id": failed["id"],
        "bindings": {"inventory": {"service_id": simulator["id"]}},
    }
    try:
        first = client.post(base + "/debug/runs", json=body)
        assert first.status_code == 202, first.text
        duplicate = client.post(
            base + "/debug/runs", json={**body, "request_key": "another-repair"}
        )
        assert duplicate.status_code == 409 and "另一任务" in duplicate.text
        resume = client.post(base + "/debug/runs/" + failed["id"] + "/resume")
        assert resume.status_code == 409
    finally:
        gate.set()
    done = settled(client, base, first.json())
    assert done["status"] == "succeeded", done


def test_service_discovery_walks_nodes_not_user_payloads(platform):
    client, _ = platform
    pid, base = setup(client)
    simulator = service(client, base)
    nested = client.post(base + "/members", json={"name": "读取"}).json()["id"]
    read_graph(client, nested)
    payload = {"type": "http_request", "config": {"service_ref": "not-a-node"}}
    inner = {
        "nodes": [
            node("begin", "start"),
            node("call", "tool", tool_name="workflow:" + nested, input={}),
            node("done", "end", outputs={"data": ref("call", "output")}),
        ],
        "edges": [edge("begin", "call"), edge("call", "done")],
    }
    graph(
        client,
        pid,
        [
            node("s", "start"),
            node(
                "write",
                "http_request",
                service_ref="inventory",
                path="/update",
                method="POST",
                body={"id": 1, "changes": payload},
            ),
            node("loop", "iteration", items=[1], workflow=inner, output_node_id="done"),
            node("e", "end"),
        ],
        [edge("s", "write"), edge("write", "e"), edge("s", "loop"), edge("loop", "e")],
    )
    overview = client.get(base + "/debug/overview").json()
    main = next(w for w in overview["workflows"] if w["id"] == pid)
    assert main["services"] == ["inventory"]
    assert main["calls"][0]["workflow_id"] == nested and main["calls"][0]["nested"]
    result = run(client, pid, base, simulator["id"])
    assert result["status"] == "succeeded", result
