from pathlib import Path

import pytest

from agent_platform.models import AgentSpec
from agent_platform.storage import Storage


@pytest.mark.asyncio
async def test_agent_version_publish_and_event_replay(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    await storage.initialize()
    spec = AgentSpec(
        name="tester",
        description="Runs tests",
        system_prompt="Run tests, inspect failures, fix root causes, and verify the result.",
        tools=["Read", "Bash"],
    )
    version = await storage.save_agent_version(spec)
    await storage.publish_agent(spec.id, version)
    restored, restored_version, status = await storage.get_agent(spec.id)
    assert restored == spec
    assert restored_version == 1
    assert status == "published"

    first = await storage.append_event("stream", "one", {"value": 1})
    second = await storage.append_event("stream", "two", {"value": 2})
    assert first.id == 1
    assert second.id == 2
    replay = await storage.list_events("stream", after=1)
    assert [event.type for event in replay] == ["two"]
    assert (tmp_path / "events" / "stream.jsonl").exists()


@pytest.mark.asyncio
async def test_generation_record_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    await storage.initialize()
    await storage.create_generation("g1", "a sufficiently long requirement", "demo")
    await storage.update_generation("g1", status="generating")
    row = await storage.get_generation("g1")
    assert row["status"] == "generating"
    assert row["workspace_path"] == "demo"

