"""E2E scenario P1-3: Enterprise API Delivery (企业系统交付).

connector_action with compensation writeback -> human review gate -> audit trail.

Evidence collected:
  1. A REAL POST/PATCH to a local mock HTTP server (write evidence).
  2. Compensation configured on the write operation + a compensation execution
     that hits the mock's /compensate endpoint.
  3. A human review gate that pauses the run and is resumed via the API.
  4. An audit trail: run events (connector.execution.completed,
     human_input.required, agent_architecture.event, workflow.completed) plus
     connector-scoped audit events.

Run via:  .venv/bin/python e2e_p1_3_delivery.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

# Project root + backend src on sys.path so `import agent_platform` works.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from dotenv import load_dotenv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

load_dotenv(str(ROOT / ".env"))
os.environ["MODEL_EGRESS_ENABLED"] = "true"

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402

H = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}
TENANT_SECRET = "controlled-customer-secret-0001"


# ─────────────────────────────────────────────────────────────────────
# 1. Local mock "customer system" HTTP service.
# ─────────────────────────────────────────────────────────────────────
class CustomerSystemHandler(BaseHTTPRequestHandler):
    writes: list[dict[str, Any]] = []
    compensations: list[dict[str, Any]] = []
    authorization_headers: list[str] = []

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length else b"{}"
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}

    def _send(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args

    def do_PATCH(self) -> None:  # write op update_case -> PATCH /cases/{case_id}
        path = self.path.split("?", 1)[0]
        body = self._json_body()
        type(self).writes.append(
            {
                "method": "PATCH",
                "path": path,
                "body": body,
                "authorization": self.headers.get("Authorization", ""),
                "idempotency_key": self.headers.get("Idempotency-Key", ""),
                "x_lilies_tenant": self.headers.get("X-Lilies-Tenant", ""),
                "x_lilies_actor": self.headers.get("X-Lilies-Actor", ""),
            }
        )
        case_id = path.rsplit("/", 1)[-1]
        self._send(
            200,
            {
                "case_id": case_id,
                "status": "updated",
                "external_id": f"external-{case_id}",
                "compensation_payload": {
                    "case_id": case_id,
                    "previous_decision": "pending",
                },
            },
        )

    def do_POST(self) -> None:  # compensate op restore_case -> POST /cases/{case_id}/compensate
        path = self.path.split("?", 1)[0]
        body = self._json_body()
        type(self).compensations.append(
            {
                "method": "POST",
                "path": path,
                "body": body,
                "authorization": self.headers.get("Authorization", ""),
                "idempotency_key": self.headers.get("Idempotency-Key", ""),
            }
        )
        case_id = path.split("/")[-2]
        self._send(
            200,
            {
                "case_id": case_id,
                "status": "compensated",
                "previous_decision": body.get("previous_decision", ""),
            },
        )


@classmethod
def _reset(cls) -> None:
    cls.writes = []
    cls.compensations = []
    cls.authorization_headers = []


CustomerSystemHandler.reset = _reset


def start_mock_server() -> tuple[ThreadingHTTPServer, str]:
    CustomerSystemHandler.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), CustomerSystemHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


# ─────────────────────────────────────────────────────────────────────
# 2. Connector manifest / secret / binding / policy helpers
# ─────────────────────────────────────────────────────────────────────
def schema(schema_id: str, fields: list[tuple[str, str, bool]]) -> dict[str, Any]:
    return {
        "schema_id": schema_id,
        "version": 1,
        "fields": [
            {"name": name, "value_type": value_type, "required": required}
            for name, value_type, required in fields
        ],
        "additional_properties": False,
    }


def manifest(base_url: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "connector_id": "customer_system",
        "version": 1,
        "title": "Enterprise Customer System",
        "description": "Versioned delivery system contract with compensation.",
        "domain": "customer_case",
        "operations": [
            {
                "id": "update_case",
                "title": "Update case",
                "kind": "write",
                "method": "PATCH",
                "path": "/cases/{case_id}",
                "request_schema": schema(
                    "customer.case.update.request",
                    [("case_id", "string", True), ("decision", "string", True)],
                ),
                "response_schema": schema(
                    "customer.case.update.response",
                    [
                        ("case_id", "string", True),
                        ("status", "string", True),
                        ("external_id", "string", True),
                        ("compensation_payload", "object", True),
                    ],
                ),
                "required_roles": ["operator"],
                "compensation_operation_id": "restore_case",
                "idempotency_semantics": "request_key",
            },
            {
                "id": "restore_case",
                "title": "Restore case",
                "kind": "compensate",
                "method": "POST",
                "path": "/cases/{case_id}/compensate",
                "request_schema": schema(
                    "customer.case.restore.request",
                    [("case_id", "string", True), ("previous_decision", "string", True)],
                ),
                "response_schema": schema(
                    "customer.case.restore.response",
                    [
                        ("case_id", "string", True),
                        ("status", "string", True),
                        ("previous_decision", "string", True),
                    ],
                ),
                "required_roles": ["operator"],
            },
        ],
        "deployment_profiles": [
            {
                "id": "test",
                "environment": "test",
                "base_url": base_url,
                "auth_type": "bearer",
                "allowed_hosts": ["127.0.0.1"],
                "available": True,
                "timeout_seconds": 5,
                "claim_ceiling": "H3",
                "excluded_claims": ["customer production readiness"],
            }
        ],
    }


def register_connector(client: TestClient, base_url: str, app_id: str) -> dict[str, Any]:
    r = client.post("/api/v1/connectors/manifests", headers=H, json=manifest(base_url))
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/v1/platform/secrets",
        headers=H,
        json={
            "owner_id": "test-tenant",
            "name": "customer-system",
            "value": TENANT_SECRET,
            "description": "controlled Connector bearer secret",
        },
    )
    assert r.status_code == 201, r.text
    r = client.put(
        "/api/v1/connectors/bindings",
        headers=H,
        json={
            "expected_revision": 0,
            "binding": {
                "connector_id": "customer_system",
                "connector_version": 1,
                "tenant_id": "test-tenant",
                "external_tenant_id": "customer-acme",
                "profile_id": "test",
                "secret_ref": "secret://test-tenant/customer-system",
                "application_ids": [app_id],
                "allowed_operations": ["update_case", "restore_case"],
                "subjects": [
                    {
                        "external_subject": "subject-operator",
                        "actor_id": "test-operator",
                        "roles": ["operator"],
                    }
                ],
            },
        },
    )
    assert r.status_code == 200, r.text
    r = client.put(
        "/api/v1/connectors/policies",
        headers=H,
        json={
            "expected_revision": 0,
            "policy": {
                "connector_id": "customer_system",
                "connector_version": 1,
                "tenant_id": "test-tenant",
                "domain": "customer_case",
                "allowed_profiles": ["test"],
                "allowed_operations": ["update_case", "restore_case"],
                "required_roles": ["operator"],
                "max_payload_bytes": 10000,
                "mutation_preauthorization_required": True,
                "allow_dry_run": True,
                "allow_compensation_during_stop": True,
                "emergency_stop": False,
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def create_authorization(
    client: TestClient,
    operation_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    r = client.post(
        "/api/v1/connectors/authorizations",
        headers=H,
        json={
            "connector_id": "customer_system",
            "connector_version": 1,
            "tenant_id": "test-tenant",
            "actor_id": "test-operator",
            "profile_id": "test",
            "operation_id": operation_id,
            "payload": payload,
            "expires_in_seconds": 300,
            "max_uses": 10,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ─────────────────────────────────────────────────────────────────────
# 3. Workflow building via draft mutations
# ─────────────────────────────────────────────────────────────────────
def mutate(client: TestClient, app_id: str, revision: int, op: str, data: dict) -> int:
    r = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=H,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def build_workflow(client: TestClient, app_id: str, authorization_id: str) -> None:
    revision = 0
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "Enterprise Delivery Request",
            "config": {"inputs": []},
        },
        {
            "id": "connector",
            "type": "connector_action",
            "title": "Writeback to Enterprise System",
            "description": "Connector write with compensation contract (update_case -> restore_case).",
            "config": {
                "connector_id": "customer_system",
                "connector_version": 1,
                "operation_id": "update_case",
                "tenant_id": "test-tenant",
                "actor_id": "test-operator",
                "actor_roles": ["operator"],
                "profile_id": "test",
                "payload": {"case_id": "case-001", "decision": "approve"},
                "idempotency_key": "p1-3-delivery-write-0001",
                "authorization_id": authorization_id,
                "authorization_mode": "explicit",
                "execution_mode": "execute",
            },
            "retry": {"enabled": True, "max_attempts": 2, "delay_seconds": 0},
        },
        {
            "id": "human",
            "type": "human_input",
            "title": "Human Review Gate",
            "description": "人工复核门 — a human approves or rejects before audit finalizes.",
            "config": {
                "title": "Approve the enterprise writeback?",
                "description": "Review the connector write receipt before the audit is finalized.",
                "fields": [
                    {"name": "approved", "label": "Approved", "type": "boolean", "required": True},
                    {"name": "reviewer", "label": "Reviewer", "type": "string", "required": True},
                ],
            },
        },
        {
            "id": "audit",
            "type": "event_recorder",
            "title": "Audit Trail Recorder",
            "description": "Write structured audit event to the run trace.",
            "config": {
                "input": {"$ref": {"node_id": "human", "path": ["output"]}},
                "settings": {"label": "p1_3_delivery_audit"},
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "Delivery Complete",
            "config": {
                "outputs": {
                    "review_approved": {"$ref": {"node_id": "human", "path": ["approved"]}},
                    "reviewer": {"$ref": {"node_id": "human", "path": ["reviewer"]}},
                    "connector_status": {
                        "$ref": {"node_id": "connector", "path": ["receipt", "status"]}
                    },
                    "compensation_available": {
                        "$ref": {
                            "node_id": "connector",
                            "path": ["receipt", "compensation_available"],
                        }
                    },
                    "external_id": {
                        "$ref": {"node_id": "connector", "path": ["response", "external_id"]}
                    },
                    "execution_id": {
                        "$ref": {"node_id": "connector", "path": ["receipt", "execution_id"]}
                    },
                }
            },
        },
    ]
    for node in nodes:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    for edge in [
        {"id": "e1", "source": "start", "target": "connector", "source_port": "output", "target_port": "input"},
        {"id": "e2", "source": "connector", "target": "human", "source_port": "output", "target_port": "input"},
        {"id": "e3", "source": "human", "target": "audit", "source_port": "output", "target_port": "input"},
        {"id": "e4", "source": "audit", "target": "end", "source_port": "output", "target_port": "input"},
    ]:
        revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
    print(f"  [workflow] draft revision={revision}")


def wait_status(client: TestClient, run_id: str, statuses: set[str], timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    record: dict[str, Any] = {}
    while time.time() < deadline:
        r = client.get(f"/api/v1/runs/{run_id}", headers=H)
        assert r.status_code == 200, r.text
        record = r.json()
        if record["status"] in statuses:
            return record
        time.sleep(0.05)
    return record


def main() -> None:
    data_dir = ROOT / ".e2e-p1-3" / "data"
    workspace_root = ROOT / ".e2e-p1-3" / "workspaces"
    # Fresh state every run: connector manifests are immutable within a version.
    for path in (ROOT / ".e2e-p1-3",):
        if path.exists():
            import shutil

            shutil.rmtree(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        api_token="workflow-test",
        model_egress_enabled=True,
        data_dir=str(data_dir),
        workspace_root=str(workspace_root),
    )
    settings.prepare()

    server, base_url = start_mock_server()
    print(f"[mock] customer system listening at {base_url}")

    app = create_app(settings)  # no explicit provider: real runtime, no model blocks used
    evidence: dict[str, Any] = {}

    try:
        with TestClient(app) as client:
            # ---- connector contract ----
            app_id = client.post(
                "/api/v1/applications",
                headers=H,
                json={
                    "name": "P1-3 Enterprise API Delivery",
                    "requirement": "Connector write with compensation -> human review gate -> audit trail.",
                    "mode": "workflow",
                    "delivery_mode": "governed",
                },
            ).json()["id"]
            print(f"[app] created {app_id}")
            register_connector(client, base_url, app_id)
            authz = create_authorization(
                client,
                "update_case",
                {"case_id": "case-001", "decision": "approve"},
            )
            comp_authz = create_authorization(
                client,
                "restore_case",
                {"case_id": "case-001", "previous_decision": "pending"},
            )
            print(f"[connector] authorization_id={authz['id']} compensation_authz={comp_authz['id']}")

            build_workflow(client, app_id, authz["id"])

            # ---- run ----
            r = client.post(
                f"/api/v1/applications/{app_id}/runs",
                headers=H,
                json={"inputs": {}, "use_draft": True},
            )
            assert r.status_code == 202, r.text
            run_id = r.json()["run_id"]
            print(f"[run] created {run_id}")

            # expect human review gate pause
            paused = wait_status(client, run_id, {"paused", "failed", "succeeded", "cancelled"})
            assert paused["status"] == "paused", paused
            evidence["run_paused_at_human_gate"] = paused["state"]["waiting_node_id"]
            evidence["human_input_fields"] = [
                f["name"] for f in paused["state"].get("human_input_values", {}).values()
            ] or None
            print(f"[human] run paused at gate node={paused['state']['waiting_node_id']}")

            # write evidence: mock received the PATCH
            assert CustomerSystemHandler.writes, "mock did not receive the connector write"
            write_req = CustomerSystemHandler.writes[0]
            assert write_req["method"] == "PATCH"
            evidence["mock_write_request"] = write_req
            evidence["mock_write_received"] = len(CustomerSystemHandler.writes)
            print(f"[connector] mock received write: {write_req['path']} body={write_req['body']}")

            # connector execution exists and is successful
            execs = client.get(
                "/api/v1/connectors/executions",
                headers=H,
                params={"tenant_id": "test-tenant", "status": "succeeded", "limit": 10},
            ).json()["items"]
            write_exec = next(e for e in execs if e["operation_id"] == "update_case")
            execution_id = write_exec["execution_id"]
            evidence["write_receipt"] = write_exec
            print(
                f"[connector] execution {execution_id} status={write_exec['status']} "
                f"compensation_available={write_exec['compensation_available']}"
            )

            # ---- resume human gate (simulate approval) ----
            r = client.post(
                f"/api/v1/runs/{run_id}/resume",
                headers=H,
                json={"values": {"approved": True, "reviewer": "qian-zhuli"}},
            )
            assert r.status_code == 200, r.text
            evidence["human_resume_response"] = r.json()
            print("[human] resumed with values approved=True reviewer=qian-zhuli")

            done = wait_status(client, run_id, {"succeeded", "failed", "cancelled"})
            assert done["status"] == "succeeded", done
            evidence["run_final_status"] = done["status"]
            evidence["run_outputs"] = done.get("outputs")
            print(f"[run] final status={done['status']} outputs={json.dumps(done.get('outputs'), ensure_ascii=False)}")

            # ---- compensation: execute it against the write execution ----
            comp_req = client.post(
                f"/api/v1/connectors/executions/{execution_id}/compensate",
                headers=H,
                json={
                    "actor_id": "test-operator",
                    "actor_roles": ["operator"],
                    "authorization_id": comp_authz["id"],
                    "idempotency_key": "p1-3-delivery-compensate-0001",
                },
            )
            assert comp_req.status_code == 200, comp_req.text
            comp = comp_req.json()  # this is the restore_case compensation execution
            evidence["compensation_execution"] = comp
            # the ORIGINAL write execution should now be marked compensated + linked
            updated_original = client.get(
                f"/api/v1/connectors/executions/{execution_id}", headers=H
            ).json()
            evidence["original_write_after_compensation"] = updated_original["receipt"]
            print(
                f"[compensation] comp execution={comp['execution_id']} status={comp['status']} "
                f"side_effect_state={comp['side_effect_state']}"
            )
            print(
                f"[compensation] original {execution_id} now "
                f"status={updated_original['receipt']['status']} "
                f"compensation_execution_id={updated_original['receipt']['compensation_execution_id']}"
            )
            assert CustomerSystemHandler.compensations, "mock did not receive the compensation POST"
            evidence["mock_compensation_request"] = CustomerSystemHandler.compensations[0]
            print(
                f"[compensation] mock received: {CustomerSystemHandler.compensations[0]['path']} "
                f"body={CustomerSystemHandler.compensations[0]['body']}"
            )

            # ---- audit trail ----
            events = client.get(f"/v1/streams/{run_id}", headers=H).json()
            kinds = [e["type"] for e in events]
            evidence["audit_event_total"] = len(kinds)
            evidence["audit_event_kinds"] = sorted(set(kinds))
            evidence["audit_event_counts"] = {
                k: kinds.count(k) for k in sorted(set(kinds))
            }
            connector_completed = next(
                (e for e in events if e["type"] == "connector.execution.completed"), None
            )
            human_required = next(
                (e for e in events if e["type"] == "human_input.required"), None
            )
            audit_recorded = next(
                (e for e in events if e["type"] == "agent_architecture.event"), None
            )
            evidence["connector_execution_completed_event"] = connector_completed
            evidence["human_required_event"] = human_required
            evidence["audit_recorded_event"] = audit_recorded
            print(f"[audit] run event stream: {evidence['audit_event_counts']}")

            # connector-scoped audit events for the write execution
            exec_events = client.get(
                f"/api/v1/connectors/executions/{execution_id}/events", headers=H
            ).json()
            evidence["connector_audit_event_count"] = len(exec_events)
            evidence["connector_audit_event_types"] = sorted({e["event_type"] for e in exec_events})
            print(f"[audit] connector execution events: {evidence['connector_audit_event_types']}")

            # ---- emergency stop demonstration (policy state + toggle round-trip) ----
            emergency = client.post(
                "/api/v1/connectors/policies/customer_system/1/test-tenant/emergency-stop",
                headers=H,
                json={"enabled": True, "reason": "p1-3 controlled drill", "expected_revision": 1},
            )
            assert emergency.status_code == 200, emergency.text
            assert emergency.json()["emergency_stop"] is True
            off = client.post(
                "/api/v1/connectors/policies/customer_system/1/test-tenant/emergency-stop",
                headers=H,
                json={"enabled": False, "reason": "drill complete", "expected_revision": 2},
            )
            assert off.status_code == 200, off.text
            policy = client.get(
                "/api/v1/connectors/policies",
                headers=H,
                params={"application_id": app_id},
            ).json()[0]
            evidence["emergency_stop_toggled"] = policy["emergency_stop"] is False
            evidence["policy"] = {
                "allow_compensation_during_stop": policy["allow_compensation_during_stop"],
                "emergency_stop": policy["emergency_stop"],
                "allowed_operations": policy["allowed_operations"],
            }
            print("[emergency_stop] toggle drill passed; compensation-during-stop allowed")

            # ---- final summary ----
            print("\n" + "=" * 70)
            print("E2E P1-3 EVIDENCE SUMMARY")
            print("=" * 70)
            print(json.dumps(evidence, indent=2, ensure_ascii=False))

            # sanity assertions
            assert write_req["method"] == "PATCH"
            assert write_req["path"] == "/cases/case-001"
            # case_id is bound to the URL path; only the residual body reaches the mock
            assert write_req["body"] == {"decision": "approve"}
            assert write_req["idempotency_key"] == "p1-3-delivery-write-0001"
            assert write_exec["compensation_available"] is True
            assert comp["status"] == "succeeded"
            assert updated_original["receipt"]["status"] == "compensated"
            assert (
                updated_original["receipt"]["compensation_execution_id"]
                == comp["execution_id"]
            )
            assert evidence["audit_event_total"] >= 6
            assert "connector.execution.completed" in evidence["audit_event_kinds"]
            assert "human_input.required" in evidence["audit_event_kinds"]
            assert "agent_architecture.event" in evidence["audit_event_kinds"]
            assert evidence["run_outputs"]["review_approved"] is True
            print("\nALL EVIDENCE ASSERTIONS PASSED")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
