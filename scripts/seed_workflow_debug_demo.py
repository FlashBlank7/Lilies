"""Create an isolated synthetic demo through ordinary project/assistant APIs.

PYTHONPATH=platform/backend/src:. .venv/bin/python scripts/seed_workflow_debug_demo.py
No model/provider calls. Credentials are saved only in ignored .local-debug.
"""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from tests.fixtures.media_debug_workflows import (
    definitions,
    service_spec,
    chain_steps,
    synthetic_samples,
    completed_state,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:18541")
    parser.add_argument("--web", default="http://127.0.0.1:18542")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.api, timeout=120)
    name = "workflow-demo-" + str(uuid4())[:8]
    password = "simulation-demo-" + str(uuid4())
    response = client.post("/api/v1/auth/register", json={"name": name, "password": password})
    response.raise_for_status()
    account = response.json()
    client.headers["Authorization"] = "Bearer " + account["token"]
    assert account["user"]["role"] == "member"
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "影音项目 · 拆分与模拟调试",
            "description": "普通项目用户；合成 HTTP 服务；真实影音服务尚未接入。",
        },
    )
    response.raise_for_status()
    pid = response.json()["id"]
    base = "/api/v1/projects/" + pid

    def tool(tool_name, **arguments):
        response = client.post(
            base + "/assistant-tools", json={"name": tool_name, "arguments": arguments}
        )
        response.raise_for_status()
        return response.json()

    spec = service_spec()
    spec["scenarios"]["completed"] = completed_state()
    sid = tool("project_services", action="save", spec=spec)["id"]
    ids = []
    for name, definition in definitions():
        member = tool(
            "project_workflows",
            action="create",
            name=name,
            description={
                "盘点数据": "完整读取各服务分页，输出原始记录及分页信息。",
                "筛选清理目标": "依据 Keep、观看完成时间、做种情况与稳定编号，固定清理对象。",
                "执行清理": "按原目标逐项删除、取消监控、移除下载并写入日志；失败可继续。",
                "回读核对": "围绕原目标逐项核对四种必要动作。",
            }[name],
        )
        wid = member.get("id") or member["workflow_id"]
        ids.append(wid)
        revision = client.get("/api/v1/applications/" + wid + "/draft").json()["revision"]
        tool(
            "workflow_draft",
            workflow_id=wid,
            operation={
                "expected_revision": revision,
                "idempotency_key": str(uuid4()),
                "op": "replace_workflow",
                "data": {"workflow": definition},
            },
        )
    revision = client.get("/api/v1/applications/" + pid + "/draft").json()["revision"]
    tool(
        "project_debug",
        action="chain",
        chain={"expected_revision": revision, "steps": chain_steps(ids)},
    )
    samples = synthetic_samples()
    for wid, sample in zip(ids, samples):
        revision = tool("project_debug", action="cases", workflow_id=wid)["revision"]
        tool(
            "project_debug",
            action="save_case",
            workflow_id=wid,
            case={
                "name": "合成样例 · 可独立运行",
                "expected_revision": revision,
                "inputs": sample,
                "bindings": {
                    "media": {"service_id": sid, "scenario": "completed" if wid == ids[3] else ""}
                },
                "source": {"kind": "synthetic", "label": "合成数据，保留稳定编号、版本和时间"},
            },
        )
    for name, scenario in [
        ("完整链路", ""),
        ("删除后取消监控失败", "partial"),
        ("写入成功但响应丢失", "lost_ack"),
    ]:
        revision = tool("project_debug", action="cases", workflow_id=pid)["revision"]
        tool(
            "project_debug",
            action="save_case",
            workflow_id=pid,
            case={
                "name": name,
                "expected_revision": revision,
                "inputs": {},
                "bindings": {"media": {"service_id": sid, "scenario": scenario}},
            },
        )
    progress = client.get(base + "/progress").json()
    progress["value"].update(
        goal="清理符合保留规则的已观看本地影片",
        completion="每个原目标均核对文件、监控、下载任务和处理日志；模拟通过后再按范围接入真实服务。",
        platform_scope="完整分页、稳定标识匹配、规则筛选、分步调用、失败恢复、回读核对。",
        external_scope="NAS 实际存储和删除、下载与做种、影音播放、转码及各服务的真实 API。",
        open_questions="真实影音服务尚未接入；接口与认证需要对照部署版本适配。当前为合成接口，不是原厂兼容证明。",
    )
    client.put(
        base + "/progress",
        json={"expected_revision": progress["revision"], "value": progress["value"]},
    ).raise_for_status()
    target = Path(".local-debug/workflow-demo.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "project_id": pid,
                "workflow_ids": ids,
                "service_id": sid,
                "user": account["user"]["name"],
                "password": password,
                "token": account["token"],
                "api_url": args.api,
                "web_url": args.web,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    os.chmod(target, 0o600)
    print(
        json.dumps(
            {
                "project_id": pid,
                "workflow_ids": ids,
                "url": args.web + "/projects/" + pid + "?tab=flow",
                "role": account["user"]["role"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
