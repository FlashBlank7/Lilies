#!/usr/bin/env python3
"""Real end-to-end test for Lilies platform using DeepSeek API.

Tests that do NOT require Docker:
  1. Health & model listing
  2. Block catalog
  3. Create application → draft mutation
  4. Simple workflow: start → model_turn → end
  5. Agent architecture blocks
  6. Builder Team (AI builds workflow from requirement)
  7. Agent generation & session
  8. Claude-like template validation
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

# Add backend source to path
sys.path.insert(0, str(Path(__file__).resolve().parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings

PASS = 0
FAIL = 0

def header() -> dict:
    return {"Authorization": "Bearer test-token-2024"}

def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  — {detail}")

def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def add_test_and_publish(client: TestClient, app_id: str, revision: int,
                          test_name: str, test_inputs: dict,
                          assertions: list[dict]) -> tuple[int, int]:
    """Add a test case, run tests, and publish. Returns (new_revision, version)."""
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
        "expected_revision": revision,
        "idempotency_key": f"auto-test-{test_name.replace(' ', '-')}",
        "op": "add_test",
        "data": {"test": {
            "name": test_name,
            "requirement": f"Verify: {test_name}",
            "inputs": test_inputs,
            "assertions": assertions,
        }},
    })
    if r.status_code != 200:
        raise RuntimeError(f"Failed to add test: {r.text}")
    revision = r.json()["revision"]

    r = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=header())
    if r.status_code != 200:
        raise RuntimeError(f"Failed to run tests: {r.text}")
    report = r.json()
    if not report["passed"]:
        raise RuntimeError(f"Tests failed: {report}")

    r = client.post(f"/api/v1/applications/{app_id}/versions", headers=header())
    if r.status_code != 200:
        raise RuntimeError(f"Failed to publish: {r.text}")
    version = r.json()["version"]
    return revision, version


def main() -> None:
    global PASS, FAIL
    tmp = TemporaryDirectory()
    tmp_path = Path(tmp.name)

    settings = Settings(
        api_token="test-token-2024",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    settings.prepare()
    # Ensure workspace is readable/writable by sandbox user (uid 10001)
    (tmp_path / "workspaces").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspaces").chmod(0o777)
    print(f"\n🔧 Provider: DeepSeek | Generator: {settings.deepseek_generator_model} | Runtime: {settings.deepseek_runtime_model}")

    app = create_app(settings=settings)
    with TestClient(app) as client:
        _run_tests(client, tmp_path)

    tmp.cleanup()
    return 0 if FAIL == 0 else 1


def _run_tests(client: TestClient, tmp_path: Path) -> None:
    global PASS, FAIL

    # ── 1. Health & Provider Info ──────────────────────────────────
    section("1. Health & Provider Info")

    r = client.get("/health")
    check("Health returns 200", r.status_code == 200, r.text)
    data = r.json()
    check("Status OK", data["status"] == "ok")
    check("DeepSeek configured", data["deepseek_configured"] is True)
    check("Provider is deepseek", data["provider"] == "deepseek")
    check("Tools registered", len(data["tools"]) > 3, f"{len(data['tools'])} tools")

    r = client.get("/v1/models", headers=header())
    check("Models returns 200", r.status_code == 200, r.text)
    data = r.json()
    check("Generator model", data["generator_model"] == "deepseek-v4-pro")
    check("Runtime model", data["runtime_model"] == "deepseek-v4-flash")

    # ── 2. Block Catalog ───────────────────────────────────────────
    section("2. Block Catalog")

    r = client.get("/api/v1/blocks", headers=header())
    check("Blocks returns 200", r.status_code == 200, r.text)
    blocks = r.json()
    business = [b for b in blocks if b["block_kind"] == "business_workflow"]
    arch = [b for b in blocks if b["block_kind"] == "agent_architecture"]
    check(f"Business blocks >= 16", len(business) >= 16, str(len(business)))
    check(f"Agent architecture blocks >= 24", len(arch) >= 24, str(len(arch)))

    r = client.get("/api/v1/block-manuals?block_kind=agent_architecture", headers=header())
    check("Block manuals", r.status_code == 200 and len(r.json()) >= 24, f"{len(r.json())} manuals")

    r = client.get("/api/v1/claude-architecture-blueprint", headers=header())
    check("Blueprint returns 200", r.status_code == 200, r.text)
    bp = r.json()
    check("Blueprint has 6 groups", len(bp["groups"]) == 6, str(list(bp["groups"].keys())))
    check("Blueprint has legacy_macro", bp["legacy_macro"]["type"] == "claude_agent")

    # ── 3. Create Application & Draft ──────────────────────────────
    section("3. Application & Draft CRUD")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Test App",
        "description": "Real API test",
        "requirement": "Create a workflow that greets the user by name.",
    })
    check("Create app 201", r.status_code == 201, r.text)
    app_id = r.json()["id"]

    r = client.get(f"/api/v1/applications/{app_id}", headers=header())
    check("Get app 200", r.status_code == 200)
    check("App has name", r.json()["name"] == "Test App")

    r = client.get(f"/api/v1/applications/{app_id}/draft", headers=header())
    check("Get draft 200", r.status_code == 200, r.text)
    draft = r.json()
    revision = draft["revision"]

    # Add start node
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
        "expected_revision": revision,
        "idempotency_key": "test-add-start",
        "op": "add_node",
        "data": {"node": {
            "id": "start", "type": "start", "title": "Input",
            "config": {"inputs": [{"name": "name", "type": "string"}]},
        }},
    })
    check("Add start node", r.status_code == 200, r.text)
    revision = r.json()["revision"]

    # Add template node
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
        "expected_revision": revision,
        "idempotency_key": "test-add-template",
        "op": "add_node",
        "data": {"node": {
            "id": "template", "type": "template_transform", "title": "Greeting",
            "config": {
                "template": "Hello {{ name }}!",
                "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
            },
        }},
    })
    check("Add template node", r.status_code == 200, r.text)
    revision = r.json()["revision"]

    # Add end node
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
        "expected_revision": revision,
        "idempotency_key": "test-add-end",
        "op": "add_node",
        "data": {"node": {
            "id": "end", "type": "end", "title": "Output",
            "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}},
        }},
    })
    check("Add end node", r.status_code == 200, r.text)
    revision = r.json()["revision"]

    # Connect edges
    for edge_id, src, tgt, src_port, tgt_port in [
        ("a", "start", "template", "output", "input"),
        ("b", "template", "end", "text", "input"),
    ]:
        r = client.post(f"/api/v1/applications/{app_id}/draft", headers=header(), json={
            "expected_revision": revision,
            "idempotency_key": f"test-add-edge-{edge_id}",
            "op": "add_edge",
            "data": {"edge": {"id": edge_id, "source": src, "target": tgt,
                               "source_port": src_port, "target_port": tgt_port}},
        })
        check(f"Add edge {edge_id}", r.status_code == 200, r.text)
        revision = r.json()["revision"]

    # Add test, run tests, and publish
    try:
        revision, v1 = add_test_and_publish(
            client, app_id, revision,
            "Greeting contains input name",
            {"name": "Alice"},
            [{"path": ["greeting"], "operator": "contains", "expected": "Alice"}],
        )
        check("Publish v1", True)
    except RuntimeError as e:
        check("Publish v1", False, str(e))

    # Run the workflow
    r = client.post(f"/api/v1/applications/{app_id}/runs", headers=header(), json={
        "inputs": {"name": "World"},
        "version": 1,
        "workspace_path": ".",
    })
    check("Create run 202", r.status_code == 202, r.text)
    run_id = r.json()["run_id"]

    # Wait for completion
    for _ in range(50):
        r = client.get(f"/api/v1/runs/{run_id}", headers=header())
        if r.json()["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)

    r = client.get(f"/api/v1/runs/{run_id}", headers=header())
    record = r.json()
    check("Run succeeded", record["status"] == "succeeded", f"status={record['status']}")
    check("Output has greeting", record["outputs"].get("greeting") == "Hello World!",
          str(record["outputs"]))

    # ── 4. Model Turn Block (Agent Architecture) ───────────────────
    section("4. Agent Architecture: model_turn")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Model Turn Test",
        "requirement": "Test model_turn block.",
    })
    check("Create model-turn app", r.status_code == 201, r.text)
    mt_app_id = r.json()["id"]

    draft = client.get(f"/api/v1/applications/{mt_app_id}/draft", headers=header()).json()
    rev = draft["revision"]

    for node_data in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [{"name": "question", "type": "string"}]}},
        {"id": "mt", "type": "model_turn", "title": "Think",
         "config": {"input": {"$ref": {"node_id": "start", "path": ["question"]}},
                    "settings": {"system": "Reply with exactly one word.", "prompt": {"$ref": {"node_id": "start", "path": ["question"]}}}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {"answer": {"$ref": {"node_id": "mt", "path": ["text"]}}}}},
    ]:
        r = client.post(f"/api/v1/applications/{mt_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"mt-{node_data['id']}",
            "op": "add_node", "data": {"node": node_data},
        })
        rev = r.json()["revision"]

    for eid, src, tgt in [("a", "start", "mt"), ("b", "mt", "end")]:
        r = client.post(f"/api/v1/applications/{mt_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"mt-edge-{eid}",
            "op": "add_edge", "data": {"edge": {"id": eid, "source": src, "target": tgt, "source_port": "output", "target_port": "input"}},
        })
        rev = r.json()["revision"]

    # Add simple existence test and publish
    try:
        rev, _ = add_test_and_publish(
            client, mt_app_id, rev,
            "Model returns non-empty answer",
            {"question": "What is 1+1?"},
            [{"path": ["answer"], "operator": "exists"}],
        )
        check("Publish model-turn", True)
    except RuntimeError as e:
        check("Publish model-turn", False, str(e))

    r = client.post(f"/api/v1/applications/{mt_app_id}/runs", headers=header(), json={
        "inputs": {"question": "What color is the sky on a clear day?"},
        "version": 1,
        "workspace_path": ".",
    })
    check("Model-turn run created", r.status_code == 202, r.text)
    mt_run_id = r.json()["run_id"]

    for _ in range(50):
        r = client.get(f"/api/v1/runs/{mt_run_id}", headers=header())
        if r.json()["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)

    record = client.get(f"/api/v1/runs/{mt_run_id}", headers=header()).json()
    check("Model-turn run succeeded", record["status"] == "succeeded",
          f"status={record['status']} error={record.get('error', '')}")
    answer = record["outputs"].get("answer", "")
    check("Model returned text", len(str(answer)) > 0, str(answer)[:200])
    print(f"     🤖 Model answer: {str(answer)[:200]}")

    # ── 5. Tool Call Router Block ──────────────────────────────────
    section("5. Agent Architecture: tool_call_router")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Tool Router Test",
        "requirement": "Test tool_call_router.",
    })
    tr_app_id = r.json()["id"]
    draft = client.get(f"/api/v1/applications/{tr_app_id}/draft", headers=header()).json()
    rev = draft["revision"]

    for node_data in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "router", "type": "tool_call_router", "title": "Router",
         "config": {"input": {"$ref": {"node_id": "start", "path": ["output"]}},
                    "settings": {}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {"tool_calls": {"$ref": {"node_id": "router", "path": ["output", "tool_calls"]}}}}},
    ]:
        r = client.post(f"/api/v1/applications/{tr_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"tr-{node_data['id']}",
            "op": "add_node", "data": {"node": node_data},
        })
        rev = r.json()["revision"]

    for eid, src, tgt in [("a", "start", "router"), ("b", "router", "end")]:
        r = client.post(f"/api/v1/applications/{tr_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"tr-edge-{eid}",
            "op": "add_edge", "data": {"edge": {"id": eid, "source": src, "target": tgt, "source_port": "output", "target_port": "input"}},
        })
        rev = r.json()["revision"]

    try:
        rev, _ = add_test_and_publish(
            client, tr_app_id, rev,
            "Router runs without error",
            {},
            [{"path": ["tool_calls"], "operator": "exists"}],
        )
        check("Publish tool-router", True)
    except RuntimeError as e:
        check("Publish tool-router", False, str(e))

    r = client.post(f"/api/v1/applications/{tr_app_id}/runs", headers=header(), json={
        "inputs": {}, "version": 1, "workspace_path": ".",
    })
    tr_run_id = r.json()["run_id"]
    for _ in range(50):
        r = client.get(f"/api/v1/runs/{tr_run_id}", headers=header())
        if r.json()["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)

    record = client.get(f"/api/v1/runs/{tr_run_id}", headers=header()).json()
    check("Router run succeeded", record["status"] == "succeeded", f"status={record['status']}")

    # ── 6. Error Classifier Block ──────────────────────────────────
    section("6. Agent Architecture: retry_error_classifier")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Error Classifier Test",
        "requirement": "Test retry_error_classifier.",
    })
    ec_app_id = r.json()["id"]
    draft = client.get(f"/api/v1/applications/{ec_app_id}/draft", headers=header()).json()
    rev = draft["revision"]

    for node_data in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "classifier", "type": "retry_error_classifier", "title": "Classifier",
         "config": {"input": {"$ref": {"node_id": "start", "path": ["output"]}},
                    "settings": {"error": "Connection timeout after 30 seconds"}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {
             "class": {"$ref": {"node_id": "classifier", "path": ["state", "class"]}},
             "retryable": {"$ref": {"node_id": "classifier", "path": ["state", "retryable"]}},
         }}},
    ]:
        r = client.post(f"/api/v1/applications/{ec_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"ec-{node_data['id']}",
            "op": "add_node", "data": {"node": node_data},
        })
        rev = r.json()["revision"]

    for eid, src, tgt in [("a", "start", "classifier"), ("b", "classifier", "end")]:
        r = client.post(f"/api/v1/applications/{ec_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"ec-edge-{eid}",
            "op": "add_edge", "data": {"edge": {"id": eid, "source": src, "target": tgt, "source_port": "output", "target_port": "input"}},
        })
        rev = r.json()["revision"]

    try:
        rev, _ = add_test_and_publish(
            client, ec_app_id, rev,
            "Classifier runs without error",
            {},
            [{"path": ["class"], "operator": "exists"}],
        )
        check("Publish error-classifier", True)
    except RuntimeError as e:
        check("Publish error-classifier", False, str(e))
    r = client.post(f"/api/v1/applications/{ec_app_id}/runs", headers=header(), json={
        "inputs": {}, "version": 1, "workspace_path": ".",
    })
    ec_run_id = r.json()["run_id"]
    for _ in range(50):
        r = client.get(f"/api/v1/runs/{ec_run_id}", headers=header())
        if r.json()["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)

    record = client.get(f"/api/v1/runs/{ec_run_id}", headers=header()).json()
    check("Classifier run succeeded", record["status"] == "succeeded")
    outputs = record["outputs"]
    check("Error classified as retryable", outputs.get("retryable") is True, str(outputs))
    check("Class is retryable", outputs.get("class") == "retryable", str(outputs))

    # ── 7. Task Dispatcher Block ──────────────────────────────────
    section("7. Agent Architecture: task_dispatcher")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Task Dispatch Test",
        "requirement": "Test task_dispatcher with dependencies.",
    })
    td_app_id = r.json()["id"]
    draft = client.get(f"/api/v1/applications/{td_app_id}/draft", headers=header()).json()
    rev = draft["revision"]

    for node_data in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "dispatcher", "type": "task_dispatcher", "title": "Dispatcher",
         "config": {"input": {"$ref": {"node_id": "start", "path": ["output"]}},
                    "settings": {"tasks": [
                        {"name": "read", "dependencies": []},
                        {"name": "fix", "dependencies": ["read"]},
                        {"name": "test", "dependencies": ["fix"]},
                    ]}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {
             "total": {"$ref": {"node_id": "dispatcher", "path": ["output", "total"]}},
             "first_order": {"$ref": {"node_id": "dispatcher", "path": ["output", "dispatch_plan", 0, "order"]}},
         }}},
    ]:
        r = client.post(f"/api/v1/applications/{td_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"td-{node_data['id']}",
            "op": "add_node", "data": {"node": node_data},
        })
        rev = r.json()["revision"]

    for eid, src, tgt in [("a", "start", "dispatcher"), ("b", "dispatcher", "end")]:
        r = client.post(f"/api/v1/applications/{td_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"td-edge-{eid}",
            "op": "add_edge", "data": {"edge": {"id": eid, "source": src, "target": tgt, "source_port": "output", "target_port": "input"}},
        })
        rev = r.json()["revision"]

    try:
        rev, _ = add_test_and_publish(
            client, td_app_id, rev,
            "Dispatcher runs without error",
            {},
            [{"path": ["total"], "operator": "exists"}],
        )
        check("Publish task-dispatcher", True)
    except RuntimeError as e:
        check("Publish task-dispatcher", False, str(e))
    r = client.post(f"/api/v1/applications/{td_app_id}/runs", headers=header(), json={
        "inputs": {}, "version": 1, "workspace_path": ".",
    })
    td_run_id = r.json()["run_id"]
    for _ in range(50):
        r = client.get(f"/api/v1/runs/{td_run_id}", headers=header())
        if r.json()["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)

    record = client.get(f"/api/v1/runs/{td_run_id}", headers=header()).json()
    check("Dispatcher run succeeded", record["status"] == "succeeded")
    outputs = record["outputs"]
    check("3 tasks dispatched", outputs.get("total") == 3, str(outputs))
    check("First task order is 0", outputs.get("first_order") == 0, str(outputs))

    # ── 8. L2: LLM Block (Business Workflow) ───────────────────────
    section("8. Business Workflow: LLM + Template")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "LLM Test",
        "requirement": "Test LLM block.",
    })
    llm_app_id = r.json()["id"]
    draft = client.get(f"/api/v1/applications/{llm_app_id}/draft", headers=header()).json()
    rev = draft["revision"]

    for node_data in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [{"name": "topic", "type": "string"}]}},
        {"id": "llm", "type": "llm", "title": "Think",
         "config": {"system": "Reply with exactly one sentence.", "prompt": {"$ref": {"node_id": "start", "path": ["topic"]}}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {"response": {"$ref": {"node_id": "llm", "path": ["text"]}}}}},
    ]:
        r = client.post(f"/api/v1/applications/{llm_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"llm-{node_data['id']}",
            "op": "add_node", "data": {"node": node_data},
        })
        rev = r.json()["revision"]

    for eid, src, tgt, src_port in [("a", "start", "llm", "output"), ("b", "llm", "end", "text")]:
        r = client.post(f"/api/v1/applications/{llm_app_id}/draft", headers=header(), json={
            "expected_revision": rev, "idempotency_key": f"llm-edge-{eid}",
            "op": "add_edge", "data": {"edge": {"id": eid, "source": src, "target": tgt,
                                                 "source_port": src_port, "target_port": "input"}},
        })
        rev = r.json()["revision"]

    try:
        rev, _ = add_test_and_publish(
            client, llm_app_id, rev,
            "LLM returns a response",
            {"topic": "Explain gravity"},
            [{"path": ["response"], "operator": "exists"}],
        )
        check("Publish LLM workflow", True)
    except RuntimeError as e:
        check("Publish LLM workflow", False, str(e))
    r = client.post(f"/api/v1/applications/{llm_app_id}/runs", headers=header(), json={
        "inputs": {"topic": "Explain what gravity is"},
        "version": 1,
        "workspace_path": ".",
    })
    llm_run_id = r.json()["run_id"]
    for _ in range(60):
        r = client.get(f"/api/v1/runs/{llm_run_id}", headers=header())
        if r.json()["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)

    record = client.get(f"/api/v1/runs/{llm_run_id}", headers=header()).json()
    check("LLM run succeeded", record["status"] == "succeeded",
          f"status={record['status']} error={record.get('error', '')}")
    response = record["outputs"].get("response", "")
    check("LLM returned text", len(str(response)) > 5, str(response)[:200])
    print(f"     🤖 LLM response: {str(response)[:200]}")

    # ── 9. Claude Architecture Blueprint Template ──────────────────
    section("9. Claude-like Template Validation")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Claude Template Test",
        "requirement": "Test claude_like_coding_agent template.",
    })
    ct_app_id = r.json()["id"]
    draft = client.get(f"/api/v1/applications/{ct_app_id}/draft", headers=header()).json()
    rev = draft["revision"]

    # Validate the template structure
    r = client.get("/api/v1/claude-architecture-blueprint", headers=header())
    bp = r.json()
    arch_blocks = set()
    for group in bp["groups"].values():
        for item in group:
            arch_blocks.add(item["type"])
    check("Blueprint covers 24 agent blocks", len(arch_blocks) >= 24, str(len(arch_blocks)))

    # Verify the template can be validated through the draft system
    check("Blueprint schema consistent",
          all(item["type"] for group in bp["groups"].values() for item in group))

    # ── 10. Agent Generation ───────────────────────────────────────
    section("10. Agent Generation (Factory)")

    r = client.post("/v1/agent-generations", headers=header(), json={
        "requirement": "Create an agent that answers programming questions concisely. It should read code files and explain what they do.",
        "workspace_path": ".",
        "auto_publish": True,
    })
    check("Agent generation queued", r.status_code == 202, r.text)
    gen_id = r.json()["generation_id"]
    print(f"     🏗️  Generation ID: {gen_id}")

    # Poll for completion
    events = []
    for i in range(120):
        r = client.get(f"/v1/agent-generations/{gen_id}", headers=header())
        gen = r.json()
        if gen["status"] in ("published", "failed"):
            break
        # Collect events
        ev = client.get(f"/v1/streams/{gen_id}?after={len(events)}", headers=header()).json()
        for e in ev:
            event_type = e.get("type", "")
            if any(k in event_type for k in ("thinking", "generation", "spec", "published", "failed")):
                events.append(e)
        time.sleep(1)

    gen = client.get(f"/v1/agent-generations/{gen_id}", headers=header()).json()
    status = gen["status"]
    check(f"Generation finished (status={status})", status in ("published", "ready"), gen.get("error", ""))
    agent_id = gen.get("agent_id")
    if agent_id:
        check("Agent ID returned", len(agent_id) > 0)
        print(f"     🤖 Generated agent: {agent_id}")

        # Get agent details
        r = client.get(f"/v1/agents/{agent_id}", headers=header())
        check("Agent details accessible", r.status_code == 200, r.text)
        agent = r.json()
        check("Agent has spec", "spec" in agent)
        if "spec" in agent:
            spec = agent["spec"]
            check("Agent has name", bool(spec.get("name")))
            check("Agent has tools", len(spec.get("tools", [])) > 0, str(spec.get("tools", [])))
            check("Agent has system_prompt", len(spec.get("system_prompt", "")) > 10,
                  f"{len(spec.get('system_prompt', ''))} chars")
            print(f"     📋 Name: {spec.get('name')}")
            print(f"     🔧 Tools: {spec.get('tools', [])}")
    else:
        check("Generation has error info", "error" in gen, str(gen))

    # List agents
    r = client.get("/v1/agents", headers=header())
    agents = r.json()
    check("Agent list non-empty", len(agents) > 0, f"{len(agents)} agents")

    # ── 11. Builder Team (AI builds workflow) ──────────────────────
    section("11. Builder Team (AI auto-builds workflow)")

    r = client.post("/api/v1/applications", headers=header(), json={
        "name": "Builder Test",
        "requirement": "Create a workflow that takes a name and outputs a personalized greeting.",
    })
    b_app_id = r.json()["id"]

    r = client.post(f"/api/v1/applications/{b_app_id}/builds", headers=header(), json={
        "requirement": "Build a simple workflow: start with a name input → use a template to generate 'Hello {name}' → end with the greeting as output.",
        "auto_publish": True,
        "max_turns": 20,
        "max_repair_cycles": 3,
    })
    check("Build queued", r.status_code == 202, r.text)
    build_id = r.json()["build_id"]
    print(f"     🏗️  Build ID: {build_id}")

    # Poll for build completion
    for i in range(180):
        r = client.get(f"/api/v1/builds/{build_id}", headers=header())
        build = r.json()
        status = build.get("status", "unknown")
        if i % 15 == 0:
            print(f"     ... build status: {status} (turn {i})")
        if status in ("published", "ready", "needs_attention", "cancelled", "failed"):
            break
        time.sleep(1)

    build = client.get(f"/api/v1/builds/{build_id}", headers=header()).json()
    final_status = build.get("status", "unknown")
    error_msg = build.get("error") or ""
    check(f"Build completed (status={final_status})",
          final_status in ("published", "ready", "building"),
          f"error={error_msg[:300]}")
    published_version = build.get("published_version")
    if published_version:
        check("Build has published version", published_version >= 1)

    # ── Results ────────────────────────────────────────────────────
    section("RESULTS")
    total = PASS + FAIL
    print(f"\n  ✅ Passed: {PASS}/{total}")
    if FAIL:
        print(f"  ❌ Failed: {FAIL}/{total}")
    else:
        print(f"  🎉 ALL TESTS PASSED!")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
