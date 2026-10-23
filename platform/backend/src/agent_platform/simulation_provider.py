"""Version 1 simulator contract. Presets and user Python implement the same handle()."""

from __future__ import annotations

import copy
import json

from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Literal


class SimulatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    implementation: Literal["records", "python"] = "records"
    code: str = Field(default="", max_length=200000)
    allowed_paths: list[str] = Field(default_factory=lambda: ["/**"], min_length=1)
    allowed_methods: list[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]] = Field(
        default_factory=lambda: ["GET", "POST"]
    )
    idempotency_header: str = ""
    initial_state: dict[str, Any] = Field(default_factory=lambda: {"records": []})
    scenarios: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)


class SimulatorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response: dict[str, Any]
    state: dict[str, Any]
    fault: Literal["", "disconnect"] = ""


def handle(request, state, context):
    """Documented /records preset: paged GET, ID GET, POST update/delete."""
    state = copy.deepcopy(state)
    method, path = request["method"], request["path"]
    query, body = request["query"], request.get("body") or {}
    records = state.setdefault("records", [])
    state["requests"] = state.get("requests", 0) + 1
    response = {"status": 200, "headers": {}, "body": {}}
    result = {"response": response, "state": state, "fault": ""}
    faults = state.get("failures", [])
    fault = next(
        (
            f
            for f in faults
            if f.get("remaining", 0) > 0
            and f.get("path", path) == path
            and f.get("method", method) == method
        ),
        None,
    )
    if fault:
        fault["remaining"] -= 1
        if not fault.get("after_write"):
            response.update(status=fault.get("status", 503), body={"error": "模拟服务故障"})
            return result
    key = request["headers"].get("idempotency-key", "")
    signature = json.dumps([method, path, body], sort_keys=True)
    receipts = state.setdefault("receipts", {})
    if method != "GET" and key and key in receipts:
        old = receipts[key]
        response.update(
            old["response"]
            if old["signature"] == signature
            else {"status": 409, "body": {"error": "操作编号对应的请求已变化"}}
        )
        return result
    if method == "GET" and path == "/records":
        size = max(1, min(int(state.get("page_size", 3)), 1000))
        page = max(1, int(query.get("page", 1)))
        response["body"] = {
            "records": records[(page - 1) * size : page * size],
            "total": len(records),
            "page": page,
        }
    elif method == "GET" and path.startswith("/records/"):
        matches = [r for r in records if str(r.get("id")) == path.split("/")[-1]]
        response.update(
            {"body": matches[0]}
            if len(matches) == 1
            else {"status": 404, "body": {"error": "编号缺失或不唯一"}}
        )
    elif method == "POST" and path in {"/update", "/delete"}:
        matches = [r for r in records if r.get("id") == body.get("id")]
        if len(matches) != 1:
            response.update(status=409, body={"error": "编号缺失或不唯一"})
        else:
            if path == "/delete":
                records.remove(matches[0])
            else:
                matches[0].update(body.get("changes", {}))
            state.setdefault("effects", []).append({"path": path, "body": body})
            response["body"] = {"applied": True, "id": body["id"]}
            if key:
                receipts[key] = {"signature": signature, "response": copy.deepcopy(response)}
    else:
        response.update(status=404, body={"error": "模拟接口不存在"})
    if fault and fault.get("after_write"):
        result["fault"] = "disconnect"
    return result


PRESETS = [
    {
        "id": "records",
        "name": "分页记录与状态修改",
        "description": "GET /records 分页；GET /records/{id} 查询；POST /update、/delete 修改。支持失败次数及写入后断开。",
        "spec": SimulatorSpec(name="记录服务", idempotency_header="Idempotency-Key").model_dump(),
    }
]

# Run only inside the existing no-network sandbox, never eval/exec on the host.
RUNNER = """import json, sys, random
from pathlib import Path
payload = json.loads(sys.stdin.read())
random.seed(payload['context']['seed'])
namespace = {}
exec(compile(Path('provider.py').read_text(), 'provider.py', 'exec'), namespace)
result = namespace['handle'](payload['request'], payload['state'], payload['context'])
print(json.dumps(result, ensure_ascii=False, allow_nan=False))
"""
