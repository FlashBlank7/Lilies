"""Four ordinary member workflows and inputs; definitions use only public blocks."""

import copy
from pathlib import Path

from tests.fixtures.media_workflow import workflow, inputs
from tests.fixtures.media_http_backend import fixture
from tests.test_projects import node, edge, ref

HERE = Path(__file__).parent


def graph(nodes):
    return {"nodes": nodes, "edges": [edge(a["id"], b["id"]) for a, b in zip(nodes, nodes[1:])]}


def definitions(*, omit_log=False):
    original = workflow("media", omit_log=omit_log)["nodes"]

    def convert(value):
        if isinstance(value, dict):
            if value.get("type") == "http_request" and "connection_id" in value["config"]:
                value["config"]["service_ref"] = value["config"].pop("connection_id")
            for child in value.values():
                convert(child)
        elif isinstance(value, list):
            for child in value:
                convert(child)

    convert(original)
    byid = {n["id"]: n for n in original}
    inventory = graph(
        [
            node(
                "start",
                "start",
                inputs=[{"name": "first_requests", "type": "array", "required": True}],
            ),
            byid["first"],
            byid["pages"],
            byid["remaining"],
            node(
                "end",
                "end",
                outputs={
                    "inventory": {
                        "first_pages": ref("first", "items"),
                        "other_pages": ref("remaining", "items"),
                    }
                },
            ),
        ]
    )
    decide = copy.deepcopy(byid["decide"])
    decide["config"]["inputs"]["first_pages"] = ref("$inputs", "inventory", "first_pages")
    decide["config"]["inputs"]["other_pages"] = ref("$inputs", "inventory", "other_pages")
    selection = graph(
        [
            node(
                "start",
                "start",
                inputs=[{"name": "inventory", "type": "object", "required": True}]
                + [f for f in byid["start"]["config"]["inputs"] if f["name"] != "first_requests"],
            ),
            decide,
            node(
                "end",
                "end",
                outputs={
                    "analysis": ref("decide", "output"),
                    "targets": ref("decide", "output", "operations"),
                },
            ),
        ]
    )
    actions = copy.deepcopy(byid["actions"])
    actions["config"]["items"] = ref("$inputs", "targets")
    execution = graph(
        [
            node("start", "start", inputs=[{"name": "targets", "type": "array", "required": True}]),
            actions,
            node(
                "end",
                "end",
                outputs={
                    "targets": ref("$inputs", "targets"),
                    "operations": ref("actions", "items"),
                },
            ),
        ]
    )
    verify = copy.deepcopy(byid["verify"])
    verify["config"]["inputs"]["targets"] = ref("$inputs", "targets")
    end = copy.deepcopy(byid["end"])
    end["config"]["outputs"] = {
        "verification": ref("verify", "output"),
        "markdown": ref("verify", "output", "markdown"),
    }
    checking = graph(
        [
            node("start", "start", inputs=[{"name": "targets", "type": "array", "required": True}]),
            byid["readback"],
            byid["verify_pages"],
            byid["verify_remaining"],
            verify,
            end,
        ]
    )
    return [
        ("盘点数据", inventory),
        ("筛选清理目标", selection),
        ("执行清理", execution),
        ("回读核对", checking),
    ]


def service_spec(*, variant=False):
    rows = fixture()
    if variant:
        for r in rows["emby"]:
            r["MediaId"] = "V-" + r.pop("Id").split("-")[1]
            r["DisplayName"] = r.pop("Name")
            r["ProviderIds"]["Tmdb"] = str(int(r["ProviderIds"]["Tmdb"]) + 10000)
        for service in ("radarr", "sonarr"):
            for r in rows[service]:
                r["id"] += 5000
                r["tmdbId"] += 10000
        for r in rows["qbittorrent"]:
            r["hash"] = "NEW-" + r["hash"]
        for r in rows["yamtrack"]:
            r["external_id"] = str(int(r["external_id"]) + 10000)
    state = {"rows": rows, "page_size": 4 if variant else 3, "partial_id": 5710 if variant else 710}
    return {
        "name": "影音模拟服务",
        "description": "合成 HTTP 接口；不是原厂协议仿真。13 条影片与四种清理操作。",
        "implementation": "python",
        "code": (HERE / "media_simulator.py").read_text(),
        "idempotency_header": "Idempotency-Key",
        "initial_state": state,
        "scenarios": {
            fault: {"fault": fault}
            for fault in ["partial", "lost_ack", "transient_read", "auth", "schema", "stale"]
        },
    }


def chain_steps(ids, *, variant=False):
    params = {"retention_hours": 24, **inputs(variant=variant)}
    return [
        {"workflow_id": ids[0], "inputs": {"first_requests": params["first_requests"]}},
        {
            "workflow_id": ids[1],
            "inputs": {
                "inventory": ref("step_1", "output", "inventory"),
                **{k: v for k, v in params.items() if k != "first_requests"},
            },
        },
        {"workflow_id": ids[2], "inputs": {"targets": ref("step_2", "output", "targets")}},
        {"workflow_id": ids[3], "inputs": {"targets": ref("step_2", "output", "targets")}},
    ]


def synthetic_samples(*, variant=False):
    """Independent input fixtures; no upstream workflow runs are needed."""
    from tests.fixtures.media_simulator import handle
    from tests.fixtures.media_decision import main

    state = service_spec(variant=variant)["initial_state"]
    first, others = [], []
    for service in state["rows"]:
        page = 1
        while True:
            response = handle(
                {
                    "method": "GET",
                    "path": "/" + service + "/records",
                    "query": {"page": page},
                    "headers": {},
                    "body": None,
                },
                state,
                {},
            )["response"]
            record = {
                "service": service,
                "url": "/" + service + "/records",
                "payload": response["body"],
                "headers": {k.lower(): v for k, v in response["headers"].items()},
            }
            (first if page == 1 else others).append(record)
            if page >= int(response["headers"]["X-Test-Pages"]):
                break
            page += 1
    params = {"retention_hours": 24, **inputs(variant=variant)}
    selection = {
        **{k: v for k, v in params.items() if k != "first_requests"},
        "inventory": {"first_pages": first, "other_pages": others},
    }
    targets = main({**params, "first_pages": first, "other_pages": others})["operations"]
    return [
        {"first_requests": params["first_requests"]},
        selection,
        {"targets": targets},
        {"targets": targets},
    ]


def completed_state(*, variant=False):
    """Synthetic state for checking independently, with no workflow execution."""
    from tests.fixtures.media_simulator import handle

    state = service_spec(variant=variant)["initial_state"]
    for item in synthetic_samples(variant=variant)[3]["targets"]:
        operations = [
            (item["delete_path"], {"path": item["path"], "etag": item["etag"]}),
            (item["unmonitor_path"], {"id": item["manager_id"]}),
            (item["remove_path"], {"hash": item["hash"]}),
            (item["log_path"], {"media_id": item["media_id"], "event": "cleanup_completed"}),
        ]
        for i, (path, body) in enumerate(operations):
            result = handle(
                {
                    "method": "POST",
                    "path": path,
                    "query": {},
                    "headers": {"idempotency-key": "fixture-" + item["media_id"] + "-" + str(i)},
                    "body": body,
                },
                state,
                {},
            )
            assert result["response"]["status"] == 200
            state = result["state"]
    return state
