import asyncio
from pathlib import Path

import pytest

from agent_platform.blocks import IterationConfig
from agent_platform.workflow_models import ApplicationSnapshot, NodeSpec
from agent_platform.workflow_runtime import HumanInputPause, NodeRun, WorkflowRuntime


@pytest.mark.parametrize("error_type", [RuntimeError, HumanInputPause])
def test_iteration_stops_and_joins_siblings_before_propagating(error_type, tmp_path: Path):
    async def scenario():
        active = asyncio.Event()
        release = asyncio.Event()
        cancelled = []
        effects = []
        children = []
        runtime = object.__new__(WorkflowRuntime)

        async def nested(snapshot, workflow, inputs, workspace, run_id, **kwargs):
            children.append(asyncio.current_task())
            item = inputs["item"]
            if item == "failure":
                await active.wait()
                raise error_type("one branch stopped")
            active.set()
            try:
                await release.wait()
                effects.append(item)
                return {"end": {"value": item}}
            except asyncio.CancelledError:
                # Cleanup must finish before the parent reports its terminal state.
                await asyncio.sleep(0)
                cancelled.append(item)
                raise

        runtime._run_graph = nested
        config = IterationConfig(
            items=["failure", "active", "queued"], workflow={"nodes": [], "edges": []},
            output_node_id="end", output_path=["value"], parallelism=2,
        )
        run = NodeRun(
            snapshot=ApplicationSnapshot(), node=NodeSpec(id="map", type="iteration", title="Map"),
            config=config, context={"inputs": {}, "nodes": {}}, inputs={}, outputs={},
            workspace_path=str(tmp_path), run_id="run", scoped_id="map", state=None,
        )
        try:
            with pytest.raises(error_type, match="one branch stopped"):
                await runtime._exec_iteration(run)
            assert "active" in cancelled
            assert all(task.done() for task in children)
            release.set()
            await asyncio.sleep(0)
            assert effects == []
        finally:
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    asyncio.run(scenario())
