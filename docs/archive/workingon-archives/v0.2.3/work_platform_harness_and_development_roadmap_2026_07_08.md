# work_platform_harness_and_development_roadmap_2026_07_08

## 1. Goal

根据当前 intellectual assets、`Lilies_竞品研究论文与未来方向报告.docx`、v0.2.1/v0.2.2 stage reports，提取功能开发任务并完成可落地部分。

本轮目标是把“未来路线”中最影响可靠性的功能做成代码能力，而不是继续停留在文档建议。

## 2. Source Review

主要输入：

- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
- `docs/source-materials/2026-07_initial_architecture_research/Lilies_竞品研究论文与未来方向报告.docx`
- `docs/stage-report-archives/v0.2.x/v0.2.1_docs_consolidation_and_asset_baseline.md`
- `docs/stage-report-archives/v0.2.x/v0.2.2_apply_lilies_inspiration_notes.md`

## 3. Functional Task Extraction

| Priority | Task | Source | Decision |
| --- | --- | --- | --- |
| P0 | Platform Harness / task monitor boundary | assets + future report + v0.2.1/v0.2.2 | Implement now. |
| P0/P1 | Builder benchmark v1 | future report + v0.2.1 | Implement minimal benchmark runner now if Platform Harness v1 lands cleanly. |
| P1 | Frontend readable test report | v0.2.2 | Implement if backend Harness does not consume the whole slice. |
| P1 | NaturalLanguageDraftPatch preview | v0.2.2 deferred task | Implement only if permission and mutation preview can remain non-destructive. |
| P1/P2 | Template embedding RAG / marketplace | future report | Defer; needs benchmark and quality gates first. |
| P2 | Builder-as-workflow | future report | Defer; explicitly depends on Harness and benchmark. |
| P2 | Cross-framework import/export | future report | Defer; no stable quality gate yet. |

## 4. Current Design Plan

| Order | Design | Status |
| --- | --- | --- |
| 1 | `docs/historical-designs/v0.2.3_design_platform_harness_task_monitor_v1.md` | implemented |
| 2 | `docs/historical-designs/v0.2.3_design_builder_benchmark_v1.md` | implemented |
| 3 | `docs/historical-designs/v0.2.3_design_natural_language_draft_patch_preview.md` | implemented |

## 5. Acceptance Criteria

- Platform Harness v1 creates visible monitored task records for workflow runs, builds, tests, scheduler triggers, model calls, tools, and nodes.
- Resource events are emitted and counted outside soft workflow blocks.
- Public API exposes task monitor records.
- Focused backend tests pass.
- If implemented, Builder benchmark v1 runs deterministic cases and returns structural metrics.
- If implemented, natural language draft patch preview is non-destructive.

## 6. Status

2026-07-08:

- Source review completed.
- Functional priority selected: Platform Harness v1 first.
- Implemented Platform Harness v1, Builder benchmark v1, and deterministic natural-language draft patch preview.
- Live paid DeepSeek acceptance completed with refreshed `.env` key:
  - direct provider smoke: `deepseek-v4-flash` SSE response received.
  - Builder live acceptance: `deepseek-v4-pro`, max 12 turns, status `ready`.
  - Harness task status `succeeded`, usage counts `model_call=12`, `tool_call=21`.
- Live acceptance exposed that the Builder can omit mandatory tests. Added deterministic Builder preflight smoke-test fallback before ready/publish gates.
- Live acceptance also exposed an auto-extract snapshot access bug. Fixed `ApplicationSnapshot` object access in auto template extraction.
- Verification:
  - `.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/builder.py platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/scheduler.py platform/backend/src/agent_platform/workflow_runtime.py platform/backend/src/agent_platform/platform_harness.py platform/backend/src/agent_platform/builder_benchmark.py platform/backend/src/agent_platform/draft_patch_preview.py tests/test_workflow.py`
  - `.venv/bin/python -m pytest -q` -> 54 passed, 1 warning.
- Implementation evidence: `docs/workingon-archives/v0.2.3/implementation_platform_harness_and_development_roadmap_2026_07_08.md`.
- No archive until user requests it.

## 7. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_task_monitor_v1.md` | proceed to review | Code, tests, and live paid acceptance passed. | Wait for user review before archive. |
| `design_builder_benchmark_v1.md` | proceed to review | Deterministic API and scoring test implemented. | Later stage can add model-run benchmark suites. |
| `design_natural_language_draft_patch_preview.md` | proceed to review | Non-destructive preview endpoint and test implemented. | Later stage can add model-assisted patch proposals behind permission gates. |
