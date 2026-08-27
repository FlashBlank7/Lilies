"""业主通道的响应形状：resume 与 get 必须给同一种对象。

真机缺陷（2026-08-28 审计）：
- use_resume 投影的是 runtime.resume 的**命令回执**（只有 run_id/status），
  投影出来 id=""、空快照、created_at=null，还少了 stages/view 两个键；
- owner_state 的 total_cases 取了报告里根本不存在的键，恒为 None，
  业主页在"部分失败"这条路径上恒显「1 / ? 通过」。
"""

from __future__ import annotations

from agent_platform.customer_runtime_projection import project_runtime_run


def test_projection_of_a_receipt_does_not_produce_empty_id() -> None:
    """拿到命令回执时至少要保住 id，不能投影出空壳。"""
    receipt = {"run_id": "r-42", "status": "queued"}
    projected = project_runtime_run(receipt)
    assert projected["id"] == "r-42"


def test_projection_of_a_real_run_keeps_id() -> None:
    run = {"id": "r-1", "status": "succeeded", "state": {}, "outputs": {}}
    assert project_runtime_run(run)["id"] == "r-1"


def test_owner_state_counts_cases_like_other_endpoints() -> None:
    """三处报告投影必须给同一个答案：全都数 cases 的长度，没有 total_cases 这个键。"""
    from pathlib import Path

    source = Path("platform/backend/src/agent_platform/api.py").read_text(encoding="utf-8")
    assert '"total_cases": acceptance.get("total_cases")' not in source
    assert source.count('"total_cases": len(') == 3


def test_use_resume_endpoint_reads_the_run_back() -> None:
    """resume 端点必须回读运行记录再投影（而不是投影回执）。"""
    from pathlib import Path

    source = Path("platform/backend/src/agent_platform/api.py").read_text(encoding="utf-8")
    start = source.index("async def use_resume_run")
    body = source[start:start + 1600]
    assert "await services.workflow_runtime.resume(run_id, body.values)" in body
    assert "run = await _use_run(application_id, run_id)" in body
    assert "project_view_run(" in body          # 与 use_get_run 同一条投影
    assert "project_runtime_run(" not in body   # 不再投影回执
