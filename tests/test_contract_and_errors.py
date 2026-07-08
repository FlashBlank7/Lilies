"""Tests for NodeContract runtime validation and ErrorStrategy extensions."""
from __future__ import annotations

import sys, time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient
from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_models import NodeContract, ErrorStrategy
from agent_platform.workflow_runtime import WorkflowRuntime

H = {"Authorization": "Bearer test-token-2024"}


def mutate(c, aid, rev, op, data):
    r = c.post(f"/api/v1/applications/{aid}/draft", headers=H, json={
        "expected_revision": rev, "idempotency_key": f"ct-{op}-{rev}",
        "op": op, "data": data,
    })
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def add_test_pub(c, aid, rev, name, inputs, assertions, structural=False):
    rev = mutate(c, aid, rev, "add_test", {"test": {
        "name": name, "requirement": "Verify.",
        "inputs": inputs, "assertions": assertions,
        "structural_only": structural,
    }})
    tr = c.post(f"/api/v1/applications/{aid}/tests/run", headers=H)
    assert tr.json().get("passed", False), f"Test {name}: {tr.text[:300]}"
    pr = c.post(f"/api/v1/applications/{aid}/versions", headers=H)
    return rev, pr.json()["version"]


def wr(c, rid):
    for _ in range(200):
        r = c.get(f"/api/v1/runs/{rid}", headers=H)
        if r.json()["status"] in ("succeeded", "failed"):
            return r.json()
        time.sleep(0.2)
    return c.get(f"/api/v1/runs/{rid}", headers=H).json()


def test_contract_validation_emits_warning():
    """Node with contract detects missing output field (lenient mode)."""
    tmp = TemporaryDirectory()
    tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024", data_dir=tp / "data", workspace_root=tp / "workspaces")
    s.prepare()
    (tp / "workspaces").mkdir(parents=True, exist_ok=True)

    app = create_app(settings=s)
    with TestClient(app) as c:
        aid = c.post("/api/v1/applications", headers=H, json={
            "name": "ContractTest", "requirement": "Test contract."
        }).json()["id"]
        d = c.get(f"/api/v1/applications/{aid}/draft", headers=H).json()
        rev = d["revision"]

        # Node with contract enforcing report output (lenient=False to force error)
        nodes = [
            ("s", "start", "S", {"inputs": []}, None),
            ("v", "variable_assigner", "V",
             {"assignments": {"result": 42}},
             NodeContract(outputs={"report": "string"}, enforce=True, lenient=False)),
            ("e", "end", "E", {"outputs": {"out": {"$ref": {"node_id": "v", "path": ["output"]}}}}, None),
        ]
        for nid, nt, nl, nc, ncontract in nodes:
            node_data = {"id": nid, "type": nt, "title": nl, "config": nc}
            if ncontract:
                node_data["contract"] = ncontract.model_dump(mode="json")
            rev = mutate(c, aid, rev, "add_node", {"node": node_data})
        for i in range(len(nodes) - 1):
            rev = mutate(c, aid, rev, "add_edge", {"edge": {
                "id": f"e{i}", "source": nodes[i][0], "target": nodes[i + 1][0],
                "source_port": "text" if nodes[i][1] == "template_transform" else "output", "target_port": "input",
            }})
        rev, v = add_test_pub(c, aid, rev, "Contract", {},
                              [{"path": ["out"], "operator": "exists"}], structural=True)
        rr = c.post(f"/api/v1/applications/{aid}/runs", headers=H, json={
            "inputs": {}, "version": v, "workspace_path": ".",
        })
        rec = wr(c, rr.json()["run_id"])
        assert rec["status"] == "succeeded", rec.get("error", "")
        # Check contract warning was emitted
        events = c.get(f"/v1/streams/{rr.json()['run_id']}", headers=H).json()
        contract_events = [e for e in events if "contract" in e.get("type", "")]
        assert len(contract_events) > 0, "Expected contract warning event"
    tmp.cleanup()


def test_degraded_error_strategy():
    """Node with error_strategy=degraded continues with degraded_value on failure."""
    tmp = TemporaryDirectory()
    tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024", data_dir=tp / "data", workspace_root=tp / "workspaces")
    s.prepare()
    (tp / "workspaces").mkdir(parents=True, exist_ok=True)

    app = create_app(settings=s)
    with TestClient(app) as c:
        aid = c.post("/api/v1/applications", headers=H, json={
            "name": "DegradedTest", "requirement": "Test degraded."
        }).json()["id"]
        d = c.get(f"/api/v1/applications/{aid}/draft", headers=H).json()
        rev = d["revision"]

        # Template node with bad $ref that fails at runtime → degraded handles it
        nodes = [
            ("s", "start", "S", {"inputs": []}),
            ("bad", "template_transform", "Bad",
             {"template": "{{ x }}",
              "variables": {"x": {"$ref": {"node_id": "NONEXISTENT_NODE", "path": ["z"]}}}}),
            ("e", "end", "E", {"outputs": {
                "result": {"$ref": {"node_id": "bad", "path": ["output"]}},
                "degraded": {"$ref": {"node_id": "bad", "path": ["degraded"]}},
            }}),
        ]
        for nid, nt, nl, nc in nodes:
            node_data = {"id": nid, "type": nt, "title": nl, "config": nc}
            if nid == "bad":
                node_data["error_strategy"] = "degraded"
                node_data["degraded_value"] = "fallback_data"
            rev = mutate(c, aid, rev, "add_node", {"node": node_data})
        for i in range(len(nodes) - 1):
            rev = mutate(c, aid, rev, "add_edge", {"edge": {
                "id": f"e{i}", "source": nodes[i][0], "target": nodes[i + 1][0],
                "source_port": "text" if nodes[i][1] == "template_transform" else "output", "target_port": "input",
            }})
        rev, v = add_test_pub(c, aid, rev, "Degraded", {},
                              [{"path": ["result"], "operator": "exists"}], structural=True)
        rr = c.post(f"/api/v1/applications/{aid}/runs", headers=H, json={
            "inputs": {}, "version": v, "workspace_path": ".",
        })
        rec = wr(c, rr.json()["run_id"])
        assert rec["status"] == "succeeded", rec.get("error", "")
        outputs = rec.get("outputs", {})
        assert outputs.get("degraded") is True, f"Expected degraded=True, got {outputs}"
        events = c.get(f"/v1/streams/{rr.json()['run_id']}", headers=H).json()
        degraded_events = [e for e in events if e.get("type") == "node.degraded"]
        assert len(degraded_events) > 0, "Expected node.degraded event"
    tmp.cleanup()


def test_retry_with_fallback_strategy():
    """Node with retry_with_fallback uses fallback_value after retries exhausted."""
    tmp = TemporaryDirectory()
    tp = Path(tmp.name)
    s = Settings(api_token="test-token-2024", data_dir=tp / "data", workspace_root=tp / "workspaces")
    s.prepare()
    (tp / "workspaces").mkdir(parents=True, exist_ok=True)

    app = create_app(settings=s)
    with TestClient(app) as c:
        aid = c.post("/api/v1/applications", headers=H, json={
            "name": "FallbackTest", "requirement": "Test fallback."
        }).json()["id"]
        d = c.get(f"/api/v1/applications/{aid}/draft", headers=H).json()
        rev = d["revision"]

        nodes = [
            ("s", "start", "S", {"inputs": []}),
            ("bad", "template_transform", "Bad",
             {"template": "{{ x }}",
              "variables": {"x": {"$ref": {"node_id": "NONEXISTENT_NODE", "path": ["z"]}}}}),
            ("e", "end", "E", {"outputs": {
                "result": {"$ref": {"node_id": "bad", "path": ["output"]}},
                "fallback": {"$ref": {"node_id": "bad", "path": ["fallback_used"]}},
            }}),
        ]
        for nid, nt, nl, nc in nodes:
            node_data = {"id": nid, "type": nt, "title": nl, "config": nc}
            if nid == "bad":
                node_data["error_strategy"] = "retry_with_fallback"
                node_data["retry"] = {"enabled": True, "max_attempts": 2, "delay_seconds": 0.1}
                node_data["fallback_value"] = "cached_result"
            rev = mutate(c, aid, rev, "add_node", {"node": node_data})
        for i in range(len(nodes) - 1):
            rev = mutate(c, aid, rev, "add_edge", {"edge": {
                "id": f"e{i}", "source": nodes[i][0], "target": nodes[i + 1][0],
                "source_port": "text" if nodes[i][1] == "template_transform" else "output", "target_port": "input",
            }})
        rev, v = add_test_pub(c, aid, rev, "Fallback", {},
                              [{"path": ["result"], "operator": "exists"}], structural=True)
        rr = c.post(f"/api/v1/applications/{aid}/runs", headers=H, json={
            "inputs": {}, "version": v, "workspace_path": ".",
        })
        rec = wr(c, rr.json()["run_id"])
        assert rec["status"] == "succeeded", rec.get("error", "")
        outputs = rec.get("outputs", {})
        assert outputs.get("fallback") is True, f"Expected fallback_used=True, got {outputs}"
    tmp.cleanup()


def test_type_matching():
    """_matches_type correctly identifies types."""
    assert WorkflowRuntime._matches_type("hello", "string")
    assert WorkflowRuntime._matches_type(42, "number")
    assert WorkflowRuntime._matches_type(3.14, "number")
    assert WorkflowRuntime._matches_type(True, "boolean")
    assert WorkflowRuntime._matches_type({"a": 1}, "object")
    assert WorkflowRuntime._matches_type([1, 2], "array")
    assert not WorkflowRuntime._matches_type("hi", "number")
    assert not WorkflowRuntime._matches_type(42, "string")


def test_enum_values():
    """ErrorStrategy enum has all expected values."""
    assert ErrorStrategy.degraded.value == "degraded"
    assert ErrorStrategy.retry_with_fallback.value == "retry_with_fallback"
    assert ErrorStrategy.fail.value == "fail"
    assert ErrorStrategy.continue_on_error.value == "continue"
    assert ErrorStrategy.error_branch.value == "error_branch"


def test_contract_model_roundtrip():
    """NodeContract serializes and deserializes correctly."""
    contract = NodeContract(
        inputs={"task": "string"},
        outputs={"report": "string", "count": "number"},
        enforce=True, lenient=False,
    )
    data = contract.model_dump(mode="json")
    reloaded = NodeContract.model_validate(data)
    assert reloaded.enforce is True
    assert reloaded.lenient is False
    assert reloaded.inputs == {"task": "string"}
    assert reloaded.outputs == {"report": "string", "count": "number"}
