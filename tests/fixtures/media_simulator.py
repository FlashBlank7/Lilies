"""Portable v1 service implementation, deliberately synthetic (not vendor APIs)."""


def handle(request, state, context):
    import copy
    import math

    state = copy.deepcopy(state)
    parts = request["path"].strip("/").split("/")

    def reply(status, body, headers=None, fault=""):
        return {
            "response": {"status": status, "body": body, "headers": headers or {}},
            "state": state,
            "fault": fault,
        }

    if len(parts) != 2 or parts[0] not in state["rows"]:
        return reply(404, {"error": "unknown endpoint"})
    service, action = parts
    method, body, query = request["method"], request.get("body") or {}, request["query"]
    counts = state.setdefault("counts", {})
    counter = service + "/" + action
    counts[counter] = counts.get(counter, 0) + 1
    fault = state.get("fault", "")
    if fault == "auth":
        return reply(401, {"error": "authentication rejected"})
    if action == "records" and method == "GET":
        page = int(query.get("page", 1))
        size = state.get("page_size", 3)
        if (
            service == "emby"
            and page == 2
            and fault == "transient_read"
            and not counts.get("read_failed")
        ):
            counts["read_failed"] = 1
            return reply(503, {"error": "temporary failure"})
        rows = state["rows"][service]
        values = rows[(page - 1) * size : page * size]
        payload = (
            {"Items": values, "TotalRecordCount": len(rows)}
            if service == "emby"
            else {"records": values, "totalRecords": len(rows)}
            if service in {"radarr", "sonarr"}
            else values
            if service == "qbittorrent"
            else {"data": values, "total": len(rows)}
        )
        if fault == "schema" and service == "emby":
            payload = {"changed_items": values}
        return reply(
            200,
            payload,
            {"X-Test-Pages": str(max(1, math.ceil(len(rows) / size))), "X-Test-Page": str(page)},
        )
    if method != "POST":
        return reply(405, {"error": "method"})
    key = request["headers"].get("idempotency-key", "")
    if not key:
        return reply(400, {"error": "operation ID required"})
    receipts = state.setdefault("receipts", {})
    if key in receipts:
        prior = receipts[key]
        if prior["body"] != body or prior["path"] != request["path"]:
            return reply(409, {"error": "changed operation"})
        return reply(200, prior["result"])
    if service == "nas" and action == "delete":
        target = next((r for r in state["rows"][service] if r["path"] == body.get("path")), None)
        if fault == "stale" and target:
            target["etag"] = "new-version"
        if not target or target["etag"] != body.get("etag"):
            return reply(412, {"error": "file version changed"})
        target["exists"] = False
    elif service in {"radarr", "sonarr"} and action == "unmonitor":
        if (
            fault == "partial"
            and body.get("id") == state.get("partial_id", 710)
            and counts.get("partial_failures", 0) < 3
        ):
            counts["partial_failures"] = counts.get("partial_failures", 0) + 1
            return reply(503, {"error": "manager unavailable after delete"})
        target = next((r for r in state["rows"][service] if r["id"] == body.get("id")), None)
        if not target:
            return reply(404, {"error": "manager ID"})
        target["monitored"] = False
    elif service == "qbittorrent" and action == "remove":
        target = next((r for r in state["rows"][service] if r["hash"] == body.get("hash")), None)
        if not target or target.get("seed_goal_met") is not True:
            return reply(409, {"error": "seeding"})
        target["removed"] = True
    elif service == "yamtrack" and action == "log":
        state["rows"][service].append(body)
    else:
        return reply(404, {"error": "write endpoint"})
    result = {"ok": True, "service": service, "action": action}
    state.setdefault("effects", []).append(
        {"service": service, "action": action, "body": body, "key": key}
    )
    receipts[key] = {"path": request["path"], "body": body, "result": result}
    if fault == "lost_ack" and service == "nas" and not counts.get("lost"):
        counts["lost"] = 1
        return reply(200, result, fault="disconnect")
    return reply(200, result)
