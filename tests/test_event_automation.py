from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import websockets

from agent_platform.blocks import build_block_registry
from agent_platform.event_automation import (
    DurableEventTimerRequest,
    EventAutomationService,
    EventSubscriptionCreateRequest,
)
from agent_platform.workflow_models import EdgeSpec, NodeSpec, WorkflowSpec


class FakeHarness:
    def enforce_network_egress_policy(
        self,
        *,
        surface: str,
        hostname: str,
    ) -> None:
        assert surface == "event_subscription"
        assert hostname in {"127.0.0.1", "localhost"}

    async def inject_secret_references(
        self,
        *,
        owner_id: str,
        payload: Any,
        allow_secret_references: bool,
    ) -> Any:
        assert owner_id
        assert allow_secret_references is True
        return payload


def instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def test_event_blocks_are_two_cohesive_optional_domain_operations() -> None:
    registry = build_block_registry()
    trigger = registry.get("event_subscription_trigger")
    timer = registry.get("durable_event_timer")

    assert trigger.category == "input"
    assert timer.category == "logic"
    workflow = WorkflowSpec(
        nodes=[
            NodeSpec(
                id="source",
                type="event_subscription_trigger",
                title="Generic events",
                config={
                    "subscription_name": "generic-events",
                    "inputs": [
                        {
                            "name": "entity_id",
                            "type": "string",
                            "required": True,
                        }
                    ],
                },
            ),
            NodeSpec(
                id="end",
                type="end",
                title="End",
                config={"outputs": {"entity_id": {"$ref": {"node_id": "source", "path": ["entity_id"]}}}},
            ),
        ],
        edges=[
            EdgeSpec(
                id="source-end",
                source="source",
                target="end",
                source_port="output",
                target_port="input",
            )
        ],
    )
    assert registry.validate_workflow(workflow) == []


@pytest.mark.asyncio
async def test_timer_replay_and_stale_events_are_safe(tmp_path: Path) -> None:
    service = EventAutomationService(
        tmp_path / "events.sqlite3",
        harness=FakeHarness(),  # type: ignore[arg-type]
    )
    await service.initialize()
    opened = datetime.now(timezone.utc) + timedelta(seconds=60)
    scheduled = await service.apply_timer(
        "app",
        str(tmp_path),
        DurableEventTimerRequest(
            operation="schedule",
            timer_key="door:a",
            subject_id="door-a",
            event_id="open-2",
            occurred_at=instant(opened),
            hold_for_seconds=300,
            due_inputs={"kind": "timer_due"},
        ),
    )
    replayed = await service.apply_timer(
        "app",
        str(tmp_path),
        DurableEventTimerRequest(
            operation="schedule",
            timer_key="door:a",
            subject_id="door-a",
            event_id="open-2",
            occurred_at=instant(opened),
            hold_for_seconds=300,
            due_inputs={"kind": "timer_due"},
        ),
    )
    stale_close = await service.apply_timer(
        "app",
        str(tmp_path),
        DurableEventTimerRequest(
            operation="cancel",
            timer_key="door:a",
            subject_id="door-a",
            event_id="close-1",
            occurred_at=instant(opened - timedelta(seconds=1)),
            hold_for_seconds=300,
        ),
    )

    assert scheduled["status"] == "scheduled"
    assert replayed["status"] == "replayed"
    assert stale_close["status"] == "stale_ignored"
    assert (await service.get_timer("door:a"))["status"] == "pending"


@pytest.mark.asyncio
async def test_due_timer_recovers_from_persistent_database(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    first = EventAutomationService(
        database,
        harness=FakeHarness(),  # type: ignore[arg-type]
        timer_poll_seconds=0.01,
    )
    await first.initialize()
    await first.apply_timer(
        "app",
        str(tmp_path),
        DurableEventTimerRequest(
            operation="schedule",
            timer_key="door:restart",
            subject_id="door-restart",
            event_id="open-restart",
            occurred_at=instant(datetime.now(timezone.utc)),
            hold_for_seconds=0.08,
            due_inputs={"kind": "timer_due"},
        ),
    )

    dispatched = asyncio.Event()
    observed: dict[str, Any] = {}
    restarted = EventAutomationService(
        database,
        harness=FakeHarness(),  # type: ignore[arg-type]
        timer_poll_seconds=0.01,
    )

    async def run_callback(
        application_id: str,
        inputs: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        observed.update(
            {
                "application_id": application_id,
                "inputs": inputs,
                "workspace_path": workspace_path,
            }
        )
        dispatched.set()
        return {"run_id": "timer-run"}

    restarted.bind_run_callback(run_callback)
    await restarted.initialize()
    await restarted.start()
    await asyncio.wait_for(dispatched.wait(), timeout=2)
    for _ in range(100):
        if (await restarted.get_timer("door:restart"))["status"] == "dispatched":
            break
        await asyncio.sleep(0.01)
    await restarted.stop()

    assert observed["application_id"] == "app"
    assert observed["inputs"]["kind"] == "timer_due"
    assert observed["inputs"]["__event_automation"]["timer_key"] == "door:restart"
    assert observed["inputs"]["__event_automation"]["recovered_after_restart"] is True
    assert observed["inputs"]["__event_automation"]["recovery_count"] == 1
    assert (await restarted.get_timer("door:restart"))["status"] == "dispatched"


@pytest.mark.asyncio
async def test_generic_websocket_subscription_maps_and_deduplicates_events(
    tmp_path: Path,
) -> None:
    received = asyncio.Event()
    callback_inputs: list[dict[str, Any]] = []

    async def websocket_handler(socket: Any) -> None:
        message = json.loads(await socket.recv())
        assert message == {"type": "subscribe", "channel": "facility"}
        await socket.send(json.dumps({"type": "subscribed", "success": True}))
        event = {
            "type": "event",
            "payload": {
                "id": "event-1",
                "subject": "door-a",
                "state": "open",
            },
        }
        await socket.send(json.dumps(event))
        await socket.send(json.dumps(event))
        await asyncio.sleep(0.1)

    async with websockets.serve(websocket_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        service = EventAutomationService(
            tmp_path / "events.sqlite3",
            harness=FakeHarness(),  # type: ignore[arg-type]
            timer_poll_seconds=0.01,
        )

        async def run_callback(
            application_id: str,
            inputs: dict[str, Any],
            workspace_path: str,
        ) -> dict[str, Any]:
            callback_inputs.append(inputs)
            received.set()
            return {"run_id": "event-run"}

        service.bind_run_callback(run_callback)
        await service.initialize()
        await service.start()
        subscription = await service.create_subscription(
            EventSubscriptionCreateRequest(
                name="generic-events",
                application_id="app",
                websocket_url=f"ws://127.0.0.1:{port}",
                allowed_hosts=["127.0.0.1"],
                subscription_message={
                    "type": "subscribe",
                    "channel": "facility",
                },
                subscription_response_match={
                    "type": "subscribed",
                    "success": True,
                },
                event_match={"type": "event"},
                event_identity_path="payload.id",
                input_mapping={
                    "entity_id": "payload.subject",
                    "new_state": "payload.state",
                },
                workspace_path=str(tmp_path),
                reconnect_seconds=10,
            )
        )
        await asyncio.wait_for(received.wait(), timeout=2)
        await asyncio.sleep(0.05)
        await service.stop()

    assert len(callback_inputs) == 1
    assert callback_inputs[0]["entity_id"] == "door-a"
    assert callback_inputs[0]["new_state"] == "open"
    status = await service.get_subscription(str(subscription["id"]))
    assert status["event_count"] == 1
