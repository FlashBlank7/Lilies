"""Browser-equivalent public API acceptance with sandboxed custom simulators."""

import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.fixtures.media_debug_workflows import (
    definitions,
    service_spec,
    chain_steps,
    synthetic_samples,
    completed_state,
)
from tests.test_projects import graph
from tests.test_users import signup, project

pytestmark = pytest.mark.skipif(
    os.getenv("SIMULATION_DOCKER") != "1", reason="real Docker simulation acceptance"
)


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        _env_file=None,
        api_token="local-tests",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        model_egress_enabled=False,
        official_agent_egress_enabled=False,
        automatic_tasks_enabled=False,
        sandbox_image=os.getenv("SANDBOX_IMAGE", "agent-platform-sandbox:pr3-ci-20261018"),
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


def setup(client, *, omit_log=False, variant=False):
    _, auth = signup(client, "media-" + str(uuid4())[:8])
    client.headers.update(auth)
    pid = project(client, auth, "影音工作流拆分与调试")
    base = "/api/v1/projects/" + pid
    response = client.post(base + "/simulators", json=service_spec(variant=variant))
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    ids = []
    for name, definition in definitions(omit_log=omit_log):
        wid = client.post(base + "/members", json={"name": name}).json()["id"]
        ids.append(wid)
        graph(client, wid, **definition)
    revision = client.get("/api/v1/applications/" + pid + "/draft").json()["revision"]
    response = client.put(
        base + "/debug/chain",
        json={"expected_revision": revision, "steps": chain_steps(ids, variant=variant)},
    )
    assert response.status_code == 200, response.text
    return pid, base, sid, ids, auth


def wait(client, base, task):
    for _ in range(1200):
        result = client.get(base + "/tasks/" + task["id"]).json()
        if result["status"] not in {"queued", "running"}:
            return client.get(base + "/debug/runs/" + task["id"]).json()
        time.sleep(0.1)
    raise AssertionError(result)


def run(client, base, wid, sid, *, scenario="", **kwargs):
    response = client.post(
        base + "/debug/runs",
        json={
            "workflow_id": wid,
            "request_key": str(uuid4()),
            "bindings": {"media": {"service_id": sid, "scenario": scenario}},
            **kwargs,
        },
    )
    assert response.status_code == 202, response.text
    return wait(client, base, response.json())


def check(result):
    assert result["status"] == "succeeded", result["error"]
    state = result["services"]["media"]["state"]
    assert len(state["effects"]) == 16
    assert sum(e["action"] == "delete" for e in state["effects"]) == 4
    assert result["outputs"]["result"]["verification"]["target_count"] == 4
    assert len(result["outputs"]["step_2"]["analysis"]["decisions"]) == 13


def test_split_workflows_and_chain(env):
    client, _ = env
    pid, base, sid, ids, _ = setup(client)
    # Test the downstream FIRST with synthetic upstream output.
    samples = synthetic_samples()
    selection = run(client, base, ids[1], sid, inputs=samples[1])
    assert selection["status"] == "succeeded", selection["error"]
    assert {x["media_id"] for x in selection["outputs"]["targets"]} == {
        "E-1",
        "E-10",
        "E-11",
        "E-13",
    }
    assert len(selection["runs"]) == 1 and not selection["services"]["media"]["requests"]
    execution = run(client, base, ids[2], sid, inputs=samples[2])
    assert execution["status"] == "succeeded", execution["error"]
    # An independent checker receives a synthetic completed service state.
    spec = service_spec()
    spec["initial_state"] = completed_state()
    spec["name"] = "已处理状态样例"
    other = client.post(base + "/simulators", json=spec).json()["id"]
    checking = run(client, base, ids[3], other, inputs=samples[3])
    assert checking["status"] == "succeeded", checking["error"]
    assert len(checking["runs"]) == 1
    inventory = run(client, base, ids[0], sid, inputs=samples[0])
    assert inventory["status"] == "succeeded", inventory["error"]
    chain = run(client, base, pid, sid)
    check(chain)
    assert len(chain["runs"]) == 5


@pytest.mark.parametrize("scenario", ["partial", "lost_ack", "transient_read"])
def test_failures_and_resume(env, scenario):
    client, _ = env
    pid, base, sid, _, _ = setup(client)
    result = run(client, base, pid, sid, scenario=scenario)
    if scenario == "partial":
        assert result["status"] == "failed", result
        response = client.post(base + "/debug/runs/" + result["id"] + "/resume")
        assert response.status_code == 202, response.text
        result = wait(client, base, response.json())
    check(result)


def test_repair_and_missing_required_action(env):
    client, _ = env
    pid, base, sid, ids, _ = setup(client, omit_log=True)
    failed = run(client, base, pid, sid)
    assert failed["status"] == "failed" and failed["outputs"] == {}, failed
    graph(client, ids[2], **definitions()[2][1])
    repaired = run(client, base, pid, sid, recovery_task_id=failed["id"])
    check(repaired)
    assert (
        sum(
            r["request"]["path"] == "/nas/delete" for r in repaired["services"]["media"]["requests"]
        )
        == 4
    )


def test_variant_and_restart_recovery(env):
    client, settings = env
    pid, base, sid, ids, auth = setup(client, variant=True)
    result = run(client, base, pid, sid, scenario="partial")
    assert result["status"] == "failed", result["error"]
    # A fresh runtime uses persisted snapshots, HTTP receipts and service state.
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(auth)
        response = restarted.post(base + "/debug/runs/" + result["id"] + "/resume")
        assert response.status_code == 202, response.text
        done = wait(restarted, base, response.json())
        check(done)
        assert all(
            row["id"].startswith("V-")
            for row in done["outputs"]["result"]["verification"]["targets"]
        )


@pytest.mark.parametrize("scenario", ["auth", "schema", "stale"])
def test_invalid_service_results_cannot_pass(env, scenario):
    client, _ = env
    pid, base, sid, _, _ = setup(client)
    result = run(client, base, pid, sid, scenario=scenario)
    assert result["status"] == "failed" and result["outputs"] == {}
