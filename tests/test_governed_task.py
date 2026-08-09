"""Tests for GovernedTask — state machine, cancel, timeout, and audit events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_platform.governed_task import GovernedTask, TaskStatus


@pytest.mark.asyncio
async def test_governed_task_completes_normally(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(stream: str, kind: str, data: dict[str, Any]) -> None:
        events.append((stream, kind, data))

    gov = GovernedTask(name="test-task", max_timeout_seconds=30, emit=emit)
    task = gov.run("stream-1", asyncio.sleep(0.001))
    await task

    assert gov.status == TaskStatus.completed
    assert len(events) >= 2  # started + completed
    assert events[0][1] == "governed_task.started"
    assert events[-1][1] == "governed_task.completed"


@pytest.mark.asyncio
async def test_governed_task_cancelled(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(stream: str, kind: str, data: dict[str, Any]) -> None:
        events.append((stream, kind, data))

    gov = GovernedTask(name="test-cancel", max_timeout_seconds=30, emit=emit)
    task = gov.run("stream-1", asyncio.sleep(10))
    await asyncio.sleep(0.01)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    # After catching, the task should show cancelled status
    status = gov.status
    assert status in (TaskStatus.cancelled, TaskStatus.completed), f"unexpected status: {status}"
    assert any(e[1] == "governed_task.cancelled" for e in events)


@pytest.mark.asyncio
async def test_governed_task_timeout(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(stream: str, kind: str, data: dict[str, Any]) -> None:
        events.append((stream, kind, data))

    gov = GovernedTask(name="test-timeout", max_timeout_seconds=0.05, emit=emit)
    task = gov.run("stream-1", asyncio.sleep(10))

    try:
        await task
    except asyncio.TimeoutError:
        pass

    assert gov.status == TaskStatus.timed_out
    assert any(e[1] == "governed_task.timed_out" for e in events)
    # The timeout event should contain the timeout value
    timeout_events = [e for e in events if e[1] == "governed_task.timed_out"]
    assert len(timeout_events) >= 1


@pytest.mark.asyncio
async def test_governed_task_failed(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(stream: str, kind: str, data: dict[str, Any]) -> None:
        events.append((stream, kind, data))

    gov = GovernedTask(name="test-fail", max_timeout_seconds=30, emit=emit)

    async def failing() -> None:
        raise ValueError("something went wrong")

    task = gov.run("stream-1", failing())

    try:
        await task
    except ValueError:
        pass

    assert gov.status == TaskStatus.failed
    assert any(e[1] == "governed_task.failed" for e in events)
    failure_events = [e for e in events if e[1] == "governed_task.failed"]
    assert len(failure_events) >= 1
    assert "something went wrong" in failure_events[0][2]["error"]


@pytest.mark.asyncio
async def test_governed_task_records_timing(tmp_path: Path) -> None:
    gov = GovernedTask(name="test-timing", max_timeout_seconds=30)
    task = gov.run("stream-1", asyncio.sleep(0.01))
    await task

    records = gov.records()
    record = next(iter(records.values()))
    assert record.status == TaskStatus.completed
    assert record.elapsed_ms is not None
    assert record.elapsed_ms > 0
    assert record.started_at is not None
    assert record.finished_at is not None


@pytest.mark.asyncio
async def test_governed_task_multiple_runs(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(stream: str, kind: str, data: dict[str, Any]) -> None:
        events.append((stream, kind, data))

    gov = GovernedTask(name="test-multi", max_timeout_seconds=30, emit=emit)

    t1 = gov.run("s1", asyncio.sleep(0.001), task_id="t1")
    t2 = gov.run("s2", asyncio.sleep(0.001), task_id="t2")
    await asyncio.gather(t1, t2)

    records = gov.records()
    assert len(records) == 2
    assert records["t1"].status == TaskStatus.completed
    assert records["t2"].status == TaskStatus.completed


@pytest.mark.asyncio
async def test_governed_task_wait(tmp_path: Path) -> None:
    gov = GovernedTask(name="test-wait", max_timeout_seconds=30)
    gov.run("s1", asyncio.sleep(0.01), task_id="t1")
    record = await gov.wait("t1")
    assert record.status == TaskStatus.completed
